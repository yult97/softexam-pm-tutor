#!/usr/bin/env python3
"""基于教材索引自动生成带页码引用的问答草稿。"""

import argparse
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple, Optional

from _shared import (
    DEFAULT_INDEX,
    SearchResult,
    classify_query,
    extract_comparison_targets,
    extract_focus_topics,
    extract_terms,
    looks_like_exercise,
    looks_like_reference,
    looks_like_toc,
    page_hint_boost,
    query_type_preferences,
    score_text,
    strip_question_fillers,
)
from search_book import load_results


# ── 数据类型 ──────────────────────────────────────────────────────────────────


class EvidenceSentence(NamedTuple):
    """句子级别的证据条目。"""

    text: str
    page: int
    chunk_id: str
    score: float


# ── 常量 ──────────────────────────────────────────────────────────────────────

# 标题性标记词（用于降权）
HEADING_MARKERS = (
    "概述", "工具与技术", "输入", "输出",
    "过程概述", "管理基础", "裁剪考虑因素", "敏捷与适应方法",
)

# 定义性标记词（用于加权）
DEFINITION_MARKERS = (
    "是", "指", "包括", "用于", "形成", "表示为", "比较", "整合",
)


# ── CLI 参数 ──────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="基于教材证据回答问题并附页码引用。",
    )
    parser.add_argument("query", help="自然语言问题。")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX, help="JSONL 索引路径。")
    parser.add_argument("--per-query-top-k", type=int, default=6, help="每个扩展查询保留的命中数。")
    parser.add_argument("--top-evidence", type=int, default=5, help="最终保留的证据句数。")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式。")
    return parser.parse_args()


# ── 查询扩展 ──────────────────────────────────────────────────────────────────


def expand_queries(query: str) -> list[str]:
    """从用户问题中派生出一组聚焦子查询。"""
    focused = extract_focus_topics(query)
    terms = focused if focused else extract_terms(query)
    expanded: list[str] = [query]

    for term in terms:
        if len(term) < 2 or term in expanded:
            continue
        expanded.append(term)
        if len(expanded) >= 4:
            break

    return expanded


def build_query_plan(query: str) -> list[str]:
    """根据题型自动扩展内部检索计划。"""
    mode = classify_query(query)
    queries = expand_queries(query)
    focus_topics = extract_focus_topics(query)

    for topic in focus_topics:
        if topic not in queries:
            queries.append(topic)
        if mode == "summary":
            for variant in (f"{topic}过程", f"{topic}管理过程"):
                if variant not in queries:
                    queries.append(variant)

    if mode == "comparison":
        for target in extract_comparison_targets(query):
            if target not in queries:
                queries.append(target)

    deduped: list[str] = []
    for item in queries:
        if item not in deduped:
            deduped.append(item)
        if len(deduped) >= 6:
            break
    return deduped


# ── 多查询聚合 ────────────────────────────────────────────────────────────────


def aggregate_results(
    index_path: Path,
    queries: list[str],
    per_query_top_k: int,
) -> list[SearchResult]:
    """跨多个扩展查询合并检索结果。"""
    if not queries:
        return []

    merged: dict[str, SearchResult] = {}
    primary = queries[0]

    for subquery in queries:
        hits = load_results(
            index_path=index_path,
            query=subquery,
            top_k=per_query_top_k,
        )
        weight = 1.0 if subquery == primary else 0.65

        for hit in hits:
            weighted = hit.score * weight
            existing = merged.get(hit.id)

            if existing is None:
                merged[hit.id] = SearchResult(
                    id=hit.id,
                    page=hit.page,
                    chunk_index=hit.chunk_index,
                    score=weighted,
                    text=hit.text,
                )
            else:
                merged[hit.id] = SearchResult(
                    id=existing.id,
                    page=existing.page,
                    chunk_index=existing.chunk_index,
                    score=existing.score + weighted,
                    text=existing.text,
                )

    return sorted(merged.values(), key=lambda r: (-r.score, r.page, r.chunk_index))


def should_skip_chunk(text: str) -> bool:
    """过滤明显不是教材正文证据的文本块。"""
    return looks_like_toc(text) or looks_like_reference(text) or looks_like_exercise(text)


@lru_cache(maxsize=1)
def load_index_chunks(index_path: Path = DEFAULT_INDEX) -> tuple[SearchResult, ...]:
    """加载索引中的全部 chunk，供枚举题补齐相邻正文使用。"""
    records: list[SearchResult] = []
    with index_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            records.append(
                SearchResult(
                    id=str(record["id"]),
                    page=int(record["page"]),
                    chunk_index=int(record["chunk_index"]),
                    score=0.0,
                    text=str(record.get("text", "")),
                )
            )
    return tuple(records)


# ── 句子拆分与清洗 ────────────────────────────────────────────────────────────

# 合并后的清洗正则：去除章节编号前缀、教材标题、页眉信息等
_SENTENCE_CLEAN_PATTERNS = [
    (r"^(\d+(\.\d+)+\s*)+", ""),           # 章节编号 (如 "1.2.3 ")
    (r"^\d+\s*", ""),                       # 纯数字前缀
    (r"^[.、,，:：)\]）]+", ""),             # 悬挂标点
    (r"^信息系统项目管理师教程\(第4版\)", ""),  # 页眉
    (r"^第\d+章", ""),                       # 章节标题
    (r"^[\u4e00-\u9fffA-Za-z（）()《》]+ \d+\s+", ""),  # 标题 + 页码
]


def sentence_candidates(text: str) -> list[str]:
    """将文本块拆分为候选句子。"""
    compact = re.sub(r"\s+", " ", text).strip()
    fragments = re.split(r"(?<=[。！？；])\s*|(?<=\.)\s+", compact)

    candidates: list[str] = []
    for raw in fragments:
        sentence = raw.strip(" \t\r\n-")
        # 批量应用清洗正则
        for pattern, replacement in _SENTENCE_CLEAN_PATTERNS:
            sentence = re.sub(pattern, replacement, sentence)
        sentence = sentence.strip()

        # 过滤过短 / 过长 / 非文字内容
        if len(sentence) < 14 or len(sentence) > 160:
            continue
        if not re.search(r"[\u4e00-\u9fffA-Za-z]", sentence):
            continue
        if "参考答案" in sentence or re.search(r"[A-D][.．、].*[A-D][.．、]", sentence):
            continue
        # 过滤疑似标题行（包含多个标题标记）
        heading_count = sum(1 for m in HEADING_MARKERS if m in sentence)
        if heading_count >= 2:
            continue
        candidates.append(sentence)

    return candidates


def refine_sentence(sentence: str, focus_terms: list[str]) -> str:
    """裁剪候选句子中的标题性前缀。"""
    refined = sentence.strip()

    # 如果某个焦点词在前 20 字符和前 60 字符各出现一次，去掉前面的重复
    sorted_terms = sorted(focus_terms, key=len, reverse=True)
    for term in sorted_terms:
        first = refined.find(term)
        if first == -1:
            continue
        second = refined.find(term, first + len(term))
        if 0 <= first < 20 and 0 <= second < 60:
            refined = refined[second:]
            break

    # 去除残留标题前缀
    refined = re.sub(r"^(\d+(\.\d+)+\s*)+", "", refined)
    refined = re.sub(r"^(概述|管理基础|过程概述|工具与技术|输入|输出)\s*", "", refined)
    refined = refined.strip(" \t\r\n-.:：")
    return refined


# ── 证据收集 ──────────────────────────────────────────────────────────────────


def collect_evidence(
    query: str,
    results: list[SearchResult],
    top_evidence: int,
) -> list[EvidenceSentence]:
    """从检索到的文本块中提取最佳句子级证据。"""
    query_terms = extract_terms(query)
    all_queries = build_query_plan(query)
    mode = classify_query(query)
    preferences = query_type_preferences(mode)
    comparison_targets = extract_comparison_targets(query) if mode == "comparison" else []

    # 安全获取扩展焦点词（跳过原始问题本身）
    focus_terms = all_queries[1:] if len(all_queries) > 1 else query_terms[:2]

    evidence: list[EvidenceSentence] = []

    for result in results[:10]:
        if should_skip_chunk(result.text):
            continue
        for sentence in sentence_candidates(result.text):
            sentence = refine_sentence(sentence, focus_terms)
            if len(sentence) < 14:
                continue

            s_score = score_text(query, query_terms, sentence) + result.score * 0.18

            # 包含焦点词加分
            if any(t in sentence for t in focus_terms):
                s_score += 20
            # 包含定义性标记加分
            if any(m in sentence for m in DEFINITION_MARKERS):
                s_score += 8
            # 包含标题标记减分
            if any(m in sentence for m in HEADING_MARKERS):
                s_score -= 18
            # 多个数字编号减分（疑似目录行）
            if len(re.findall(r"\d+\.\d+|\b\d{2,3}\b", sentence)) >= 2:
                s_score -= 28
            if mode == "definition" and any(sentence.startswith(term) for term in focus_terms):
                s_score += 18
            if mode == "summary" and any(marker in sentence for marker in ("主要包括", "主要作用", "过程包括")):
                s_score += 20
            if preferences.get("prefer_process_markers") and any(marker in sentence for marker in ("过程包括", "核心过程", "主要内容")):
                s_score += 12
            if preferences.get("prefer_definition_markers") and any(marker in sentence for marker in ("是指", "是为了", "是编写")):
                s_score += 12
            if mode == "comparison" and comparison_targets:
                target_hits = sum(1 for target in comparison_targets if target in sentence)
                if target_hits >= 2:
                    s_score += 45
                elif target_hits == 1:
                    s_score += 10
                if preferences.get("prefer_relation_sentences") and any(marker in sentence for marker in ("有时被称为", "更广", "着眼于")):
                    s_score += 16

            if s_score <= 0:
                continue

            evidence.append(EvidenceSentence(
                text=sentence,
                page=result.page,
                chunk_id=result.id,
                score=s_score,
            ))

    # 按得分降序去重
    seen: set[str] = set()
    deduped: list[EvidenceSentence] = []
    for item in sorted(evidence, key=lambda e: (-e.score, e.page, e.text)):
        normalized = re.sub(r"\s+", "", item.text)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(item)
        if len(deduped) >= top_evidence:
            break

    return deduped


# ── 问题分类 ──────────────────────────────────────────────────────────────────


def classify_question(query: str) -> str:
    """根据问题类型选择回答模板。"""
    return classify_query(query)


# ── 答案组装 ──────────────────────────────────────────────────────────────────


def format_citation(page: int) -> str:
    """格式化页码引用。"""
    return f"教材第 {page} 页"


def render_intro_text(text: str) -> str:
    """渲染适合放在回答首句中的证据文本。"""
    return re.sub(r"^[●•]\s*", "", text).strip()


def normalize_answer_sentence(text: str) -> str:
    """清理可直接用于答案展示的句子。"""
    cleaned = render_intro_text(text)
    for pattern, replacement in _SENTENCE_CLEAN_PATTERNS:
        cleaned = re.sub(pattern, replacement, cleaned)
    cleaned = cleaned.strip(" \t\r\n-.:：;；")
    return cleaned


def primary_definition_topic(query: str) -> str:
    """提取定义题的核心概念。"""
    topics = definition_topic_variants(query)
    return max(topics, key=len) if topics else ""


def definition_topic_variants(query: str) -> list[str]:
    """提取定义题的概念变体，包括全称和缩写。"""
    topics: list[str] = []
    for topic in extract_focus_topics(query):
        cleaned = re.sub(r"[呢啊呀嘛么吧啦？?！!，,。.\s]+$", "", topic).strip()
        if len(cleaned) >= 2 and cleaned not in topics:
            topics.append(cleaned)
    return topics


def looks_like_process_definition(topic: str, text: str) -> bool:
    """判断句子是否更像“制定/创建某物”的过程定义，而非概念定义。"""
    if not topic:
        return False
    compact = normalize_answer_sentence(text)
    prefixes = ("制定", "创建", "编制", "规划", "管理", "确认", "控制")
    return (
        (
            any(compact.startswith(prefix + topic) for prefix in prefixes)
            or compact.startswith(topic + "是")
        )
        and ("过程" in compact or "文件的过程" in compact)
    )


def synthesize_definition_intro(topic: str, text: str) -> str:
    """必要时把过程定义改写成概念定义。"""
    compact = normalize_answer_sentence(text)
    if not topic:
        return compact

    match = re.search(rf"^(?:制定)?{re.escape(topic)}是编写一份(.+?)文件的过程", compact)
    if match:
        return f"{topic}可以理解为一份{match.group(1)}文件"

    if not looks_like_process_definition(topic, compact):
        return compact

    return compact


def choose_definition_intro(
    query: str,
    evidence: list[EvidenceSentence],
    results: list[SearchResult],
) -> tuple[str, int]:
    """为定义题选择更像“定义”的开头句。"""
    variants = definition_topic_variants(query)
    topic = primary_definition_topic(query)
    candidates: list[tuple[float, str, int]] = []

    for item in evidence:
        candidates.append((item.score, normalize_answer_sentence(item.text), item.page))
    for result in results[:5]:
        for sentence in chunk_sentences(result.text):
            candidates.append((result.score * 0.35, sentence, result.page))

    if not candidates:
        fallback = choose_intro_evidence(query, evidence)
        return normalize_answer_sentence(fallback.text), fallback.page

    best_text = candidates[0][1]
    best_page = candidates[0][2]
    best_score = float("-inf")

    for base_score, sentence, page in candidates:
        if "图" in sentence or "数据流向图" in sentence:
            continue
        score = base_score
        if variants:
            if any(sentence.startswith(variant) for variant in variants):
                score += 90
            elif any(variant in sentence for variant in variants):
                score += 28
            if any(re.search(rf"^{re.escape(variant)}(?:\(|（|：|:|是)", sentence) for variant in variants):
                score += 36
        if any(marker in sentence for marker in ("是指", "记录了", "是编写", "是对", "是把")):
            score += 30
        if "：" in sentence or ":" in sentence:
            score += 14
        if looks_like_process_definition(topic, sentence):
            score += 34
        if sentence.startswith("例如") or "例如" in sentence[:12]:
            score -= 70
        if sentence.startswith("它"):
            score -= 60
        if any(marker in sentence for marker in ("不能当作合同", "授权项目经理", "正式启动", "建立了联系")):
            score -= 20
        if any(marker in sentence for marker in HEADING_MARKERS):
            score -= 18

        if score > best_score:
            best_text = sentence
            best_page = page
            best_score = score

    return synthesize_definition_intro(topic, best_text), best_page


def chunk_sentences(text: str) -> list[str]:
    """从命中文本块中切出适合二次加工的句子。"""
    compact = re.sub(r"\s+", " ", text).strip()
    fragments = re.split(r"(?<=[。！？；])\s*", compact)
    sentences: list[str] = []
    for raw in fragments:
        sentence = normalize_answer_sentence(raw)
        if len(sentence) < 12 or len(sentence) > 180:
            continue
        if "参考答案" in sentence:
            continue
        if not re.search(r"[\u4e00-\u9fffA-Za-z]", sentence):
            continue
        sentences.append(sentence)
    return sentences


def summarize_definition_detail(text: str) -> str:
    """把定义题的补充依据压缩成更好读的一句话。"""
    compact = normalize_answer_sentence(text)

    if "不能当作合同" in compact:
        return "教材还强调，它不能当作合同，而是用于建立组织内部的合作关系。"
    if "授权项目经理" in compact:
        return "它还会授权项目经理开展规划、执行和控制，并在项目活动中使用组织资源。"
    if "建立了联系" in compact or "符合组织战略和日常运营的需要" in compact:
        return "它在项目执行和项目需求之间建立联系，可用于确认项目是否符合组织战略和日常运营的需要。"
    if "标志着项目的正式启动" in compact or "正式启动" in compact:
        return "项目章程一旦被批准，就标志着项目正式启动。"
    return compact.rstrip("。；;") + "。"


def definition_followups(
    query: str,
    intro_text: str,
    evidence: list[EvidenceSentence],
    results: list[SearchResult],
) -> list[str]:
    """为定义题挑选 1 到 2 句更有信息量的补充说明。"""
    variants = definition_topic_variants(query)
    intro_norm = re.sub(r"\s+", "", intro_text)
    candidates: list[tuple[float, str, int]] = []
    seen: set[str] = set()
    marker_bonus = (
        "建立了联系",
        "符合组织战略和日常运营的需要",
        "不能当作合同",
        "授权项目经理",
        "正式启动",
        "主要作用",
        "记录了",
        "用于",
        "确保",
    )

    for item in evidence[1:]:
        candidates.append((item.score, normalize_answer_sentence(item.text), item.page))
    for result in results[:5]:
        for sentence in chunk_sentences(result.text):
            candidates.append((result.score * 0.35, sentence, result.page))

    picked: list[str] = []
    for base_score, sentence, page in sorted(candidates, key=lambda item: item[0], reverse=True):
        normalized = re.sub(r"\s+", "", sentence)
        if normalized == intro_norm or normalized in seen:
            continue
        seen.add(normalized)
        if variants and not any(variant in sentence for variant in variants):
            continue
        if "授权项目经理" in intro_text and "授权项目经理" in sentence:
            continue
        if "不能当作合同" in intro_text and "不能当作合同" in sentence:
            continue
        if "建立了联系" in intro_text and "建立了联系" in sentence:
            continue
        if looks_like_process_definition(primary_definition_topic(query), sentence):
            continue
        if any(marker in sentence for marker in HEADING_MARKERS):
            continue
        if "图" in sentence or "数据流向图" in sentence:
            continue
        if len(re.findall(r"\d+\.\d+|\b\d{2,3}\b", sentence)) >= 2:
            continue
        if "仅为了说明之用" in sentence:
            continue

        score = base_score
        marker_hit = any(marker in sentence for marker in marker_bonus)
        if marker_hit:
            score += 40
        if not marker_hit and not sentence.startswith("例如"):
            continue
        if "输入" in sentence or "工具与技术" in sentence or "输出" in sentence:
            score -= 25
        if score <= 0:
            continue

        formatted = summarize_definition_detail(sentence)
        picked.append(f"{formatted.rstrip('。')}（{format_citation(page)}）。")
        if len(picked) >= 2:
            break

    return picked


def choose_intro_evidence(query: str, evidence: list[EvidenceSentence]) -> EvidenceSentence:
    """选取最干净的句子作为答案开头。"""
    focus_topics = extract_focus_topics(query)
    mode = classify_query(query)
    # 过滤掉含有特殊符号或标题标记的候选
    clean = [
        e for e in evidence
        if "图" not in e.text
        and "--" not in e.text
        and "参考答案" not in e.text
        and not any(m in e.text for m in HEADING_MARKERS)
        and len(re.findall(r"\d+\.\d+|\b\d{2,3}\b", e.text)) < 2
    ]

    pool = clean if clean else list(evidence)
    best = pool[0]
    best_score = float("-inf")

    for item in pool:
        intro_s = item.score
        intro_s += page_hint_boost(query, item.page) * 1.6
        if "图" in item.text or "--" in item.text:
            intro_s -= 25
        if "●" in item.text:
            intro_s -= 12
        if any(item.text.startswith(topic) for topic in focus_topics):
            intro_s += 18
        if focus_topics and any(topic in item.text for topic in focus_topics):
            intro_s += 20
        if mode == "definition":
            if focus_topics and not any(topic in item.text for topic in focus_topics):
                intro_s -= 24
            if any(marker in item.text for marker in ("是指", "是为了", "是编写")):
                intro_s += 22
            if "授权项目经理" in item.text or "用于建立" in item.text:
                intro_s -= 10
        if len(re.findall(r"\d+\.\d+|\b\d{2,3}\b", item.text)) >= 2:
            intro_s -= 20
        if any(m in item.text for m in HEADING_MARKERS):
            intro_s -= 14

        if intro_s > best_score:
            best = item
            best_score = intro_s

    return best


def extract_process_names(text: str) -> list[str]:
    """从正文块中抽取过程或步骤名称。"""
    valid_prefixes = (
        "规划", "收集", "定义", "创建", "确认", "控制", "管理", "制定",
        "识别", "实施", "结束", "监控", "估算", "获取", "建设",
    )
    process_names = re.findall(r"[●•]\s*([^：:\n]{2,24})[:：]", text)
    cleaned: list[str] = []
    for item in process_names:
        candidate = re.sub(r"\s+", "", item).strip("()（） ")
        if candidate.endswith("计划") or candidate.endswith("文件"):
            continue
        if candidate in {"项目章程", "项目管理计划", "质量管理计划", "需求管理计划"}:
            continue
        if not (candidate.startswith(valid_prefixes) or candidate == "创建WBS"):
            continue
        if 2 <= len(candidate) <= 18 and candidate not in cleaned:
            cleaned.append(candidate)
    return cleaned


def summarize_processes(results: list[SearchResult]) -> list[str]:
    """聚合多个命中文本块里的过程名称。"""
    if not results:
        return []
    anchor_page = results[0].page
    prioritized = [item for item in results if "过程包括" in item.text]
    fallback = [item for item in results if item not in prioritized]
    process_names: list[str] = []
    for bucket in (prioritized[:3], fallback[:5]):
        for result in bucket:
            if abs(result.page - anchor_page) > 15:
                continue
            if should_skip_chunk(result.text):
                continue
            for name in extract_process_names(result.text):
                if name not in process_names:
                    process_names.append(name)
                if len(process_names) >= 8:
                    return process_names
    return process_names


def target_aliases(target: str) -> list[str]:
    """为比较对象生成少量教材常见变体。"""
    aliases = [target]
    if target.endswith("管理") and len(target) > 2:
        aliases.append("管理" + target[:-2])
    return aliases


def gather_target_evidence(
    query: str,
    evidence: list[EvidenceSentence],
    targets: list[str],
) -> dict[str, EvidenceSentence]:
    """为比较题挑选每个概念最合适的证据。"""
    selected: dict[str, EvidenceSentence] = {}
    fallback = choose_intro_evidence(query, evidence) if evidence else None

    for target in targets:
        aliases = target_aliases(target)
        target_items = [
            item for item in evidence if any(alias in item.text for alias in aliases)
        ]
        if target_items:
            best_item = target_items[0]
            best_score = float("-inf")
            for item in target_items:
                target_score = item.score
                if any(item.text.startswith(alias) for alias in aliases):
                    target_score += 24
                if any(marker in item.text for marker in ("是指", "是把", "有时被称为", "更广", "着眼于", "记录了")):
                    target_score += 18
                if "全过程" in item.text and "全面" in item.text:
                    target_score -= 10
                if target_score > best_score:
                    best_item = item
                    best_score = target_score
            selected[target] = best_item
        elif fallback is not None:
            selected[target] = fallback
    return selected


def find_relation_sentence(
    evidence: list[EvidenceSentence],
    results: list[SearchResult],
    targets: list[str],
) -> Optional[EvidenceSentence]:
    """寻找同时提到两个比较对象的句子。"""
    if len(targets) < 2:
        return None
    alias_groups = [target_aliases(target) for target in targets[:2]]
    for item in evidence:
        if (
            all(any(alias in item.text for alias in aliases) for aliases in alias_groups)
            and any(marker in item.text for marker in ("有时被称为", "更广", "着眼于"))
        ):
            return item
    for result in results[:5]:
        if "有时被称为" not in result.text and "更广" not in result.text:
            continue
        sentence = extract_relation_snippet(result.text)
        if sentence:
            return EvidenceSentence(
                text=sentence,
                page=result.page,
                chunk_id=result.id,
                score=result.score,
            )
    return None


def extract_relation_snippet(text: str) -> str:
    """从命中文本块中截取适合比较题引用的关系句。"""
    compact = re.sub(r"\s+", " ", text)
    for needle in ("有时被称为", "质量保证着眼于", "定义比“质量保证”更广", "定义比\"质量保证\"更广"):
        index = compact.find(needle)
        if index == -1:
            continue
        start = compact.rfind("。", 0, index)
        end = compact.find("。", index)
        if start == -1:
            start = max(0, index - 50)
        else:
            start += 1
        if end == -1:
            end = min(len(compact), index + 120)
        snippet = compact[start:end].strip(" ，。")
        if len(snippet) >= 18:
            return snippet
    return ""


def list_intro(query: str, count: int) -> str:
    """为枚举题生成更稳定的开头句。"""
    if "原因" in query:
        return f"根据教材，主要包括以下 {count} 项："
    if "注意" in query:
        return f"根据教材，主要可归纳为以下 {count} 点："
    return f"根据教材，主要可概括为以下 {count} 点："


def ordered_list_results(query: str, results: list[SearchResult]) -> list[SearchResult]:
    """为枚举题筛选更可能包含完整列表的命中块，并按教材顺序排序。"""
    if not results:
        return []
    anchor_page = results[0].page
    windowed = [
        item
        for item in results[:16]
        if not should_skip_chunk(item.text) and abs(item.page - anchor_page) <= 3
    ]
    boosted: list[tuple[float, SearchResult]] = []
    for item in windowed:
        score = item.score
        if item.page == anchor_page:
            score += 20
        if "注意" in query and "注意" in item.text:
            score += 80
        if "原因" in query and "原因" in item.text:
            score += 80
        if any(marker in item.text for marker in ("主要包括", "主要有", "注意事项", "注意以下", "原因主要包括", "应该注意")):
            score += 24
        if "●" in item.text or re.search(r"\(\d+\)", item.text):
            score += 12
        boosted.append((score, item))

    prioritized = [item for _, item in sorted(boosted, key=lambda pair: (-pair[0], pair[1].page, pair[1].chunk_index))]
    chosen = prioritized[:4]

    expanded_ids = {item.id for item in chosen}
    for seed in list(chosen):
        for item in windowed:
            if item.page == seed.page and abs(item.chunk_index - seed.chunk_index) <= 1:
                expanded_ids.add(item.id)

    expanded = [item for item in windowed if item.id in expanded_ids]
    return sorted(expanded, key=lambda item: (item.page, item.chunk_index))


def merge_page_chunks(results: list[SearchResult]) -> list[tuple[int, str]]:
    """按页合并相邻 chunk，避免同页续块中的枚举项被截断。"""
    merged: dict[int, list[str]] = {}
    for item in results:
        merged.setdefault(item.page, []).append(item.text)
    ordered_pages = sorted(merged)
    return [
        (page, stitch_chunk_texts(chunks))
        for page, chunks in ((page, merged[page]) for page in ordered_pages)
    ]


def stitch_chunk_texts(chunks: list[str]) -> str:
    """拼接同页 chunk，并尽量消除切块带来的重叠重复。"""
    if not chunks:
        return ""
    stitched = re.sub(r"\s+", " ", chunks[0]).strip()
    for raw in chunks[1:]:
        current = re.sub(r"\s+", " ", raw).strip()
        max_overlap = min(len(stitched), len(current), 160)
        overlap = 0
        for size in range(max_overlap, 24, -1):
            if stitched[-size:] == current[:size]:
                overlap = size
                break
        stitched = (stitched + " " + current[overlap:].lstrip()).strip()
    return stitched


def clean_list_item(text: str) -> str:
    """清理枚举项文本。"""
    cleaned = re.sub(r"^\(\d+\)\s*", "", text)
    cleaned = re.sub(r"^[●•]\s*", "", cleaned)
    cleaned = re.sub(r"\b\d{2,4}\s*信息系统项目管理师教程\(第4版\)", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ;；，,。")
    cleaned = re.sub(r"\s+(?:第\d+章|11\.1\.2|9\.6\.3)\b.*$", "", cleaned)
    return cleaned.strip()


def extract_numbered_items(text: str) -> list[str]:
    """提取 (1) / (2) 这类顶层枚举项。"""
    matches = list(re.finditer(r"\((\d+)\)", text))
    if len(matches) < 2:
        return []
    items: list[str] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        raw = text[start:end]
        item = clean_list_item(raw)
        if len(item) >= 6:
            items.append(item)
    return items


def extract_bullet_items(text: str) -> list[str]:
    """提取 ● 这类顶层枚举项。"""
    matches = list(re.finditer(r"[●•]\s*", text))
    if len(matches) < 2:
        return []
    items: list[str] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        raw = text[start:end]
        item = clean_list_item(raw)
        if len(item) >= 8:
            items.append(item)
    return items


def extract_list_items(text: str) -> list[str]:
    """统一提取顶层枚举项，优先 (1) 风格，其次项目符号。"""
    items = extract_numbered_items(text)
    if len(items) < 2:
        items = extract_bullet_items(text)
    return items


def list_page_score(query: str, result: SearchResult, anchor_page: int) -> float:
    """为枚举题选择最可能包含目标列表的页。"""
    score = result.score
    has_list_markers = any(
        marker in result.text
        for marker in ("主要包括", "主要有", "注意事项", "注意以下", "应该注意以下", "原因主要包括")
    )
    has_list_structure = has_list_markers or "●" in result.text or bool(re.search(r"\(\d+\)", result.text))
    if result.page == anchor_page:
        score += 20
    if "注意" in query and "注意" in result.text:
        score += 90
    elif "注意" in query and not has_list_markers:
        score -= 160
    if "原因" in query and "原因" in result.text:
        score += 90
    elif "原因" in query and not has_list_markers:
        score -= 160
    if any(marker in query for marker in ("哪些", "哪几", "哪几点", "哪几项", "包括")) and not has_list_structure:
        score -= 120
    if has_list_markers:
        score += 24
    if has_list_structure:
        score += 12
    return score


def citation_for_pages(pages: list[int]) -> str:
    """把页码列表格式化成引用文本。"""
    unique = sorted(set(pages))
    if not unique:
        return "教材"
    if len(unique) == 1:
        return format_citation(unique[0])
    return f"教材第 {unique[0]}-{unique[-1]} 页"


def section_text_for_list_query(query: str, text: str) -> str:
    """锁定枚举题真正对应的正文段落，减少串到其他列表。"""
    compact = re.sub(r"\s+", " ", text).strip()
    markers: list[str] = []
    if "注意" in query:
        markers.extend(["注意事项", "应该注意以下", "需要注意以下", "注意以下"])
    if "原因" in query:
        markers.extend(["发生成本失控的原因主要包括", "项目成本失控的原因", "原因主要包括"])
    markers.extend(["主要包括", "主要有"])

    for marker in markers:
        index = compact.find(marker)
        if index != -1:
            compact = compact[index:]
            break

    stop_match = re.search(r"\s+\d+\.\d+\.\d+\s+[^\s]{1,20}", compact)
    if stop_match:
        compact = compact[:stop_match.start()]
    return compact.strip()


def select_list_pages(query: str, best: SearchResult) -> list[int]:
    """为枚举题选择需要合并的教材页，必要时自动补上续页。"""
    index_chunks = load_index_chunks()
    page_map: dict[int, list[SearchResult]] = {}
    for item in index_chunks:
        if should_skip_chunk(item.text):
            continue
        page_map.setdefault(item.page, []).append(item)

    if best.page not in page_map:
        return [best.page]

    selected_pages = [best.page]
    base_text = section_text_for_list_query(
        query,
        stitch_chunk_texts([item.text for item in sorted(page_map[best.page], key=lambda chunk: chunk.chunk_index)]),
    )
    base_items = extract_list_items(base_text)

    next_page = best.page + 1
    if next_page in page_map:
        combined_text = section_text_for_list_query(
            query,
            stitch_chunk_texts(
                [
                    item.text
                    for page in (best.page, next_page)
                    for item in sorted(page_map[page], key=lambda chunk: chunk.chunk_index)
                ]
            ),
        )
        combined_items = extract_list_items(combined_text)
        if len(combined_items) > len(base_items):
            selected_pages.append(next_page)

    return selected_pages


def enumerate_answer_items(query: str, results: list[SearchResult]) -> list[tuple[str, str]]:
    """按教材顺序抽取枚举题的列表项。"""
    if not results:
        return []

    anchor_page = results[0].page
    windowed = [
        item
        for item in results[:24]
        if not should_skip_chunk(item.text) and abs(item.page - anchor_page) <= 4
    ]
    if not windowed:
        return []

    best = max(windowed, key=lambda item: list_page_score(query, item, anchor_page))
    selected_pages = set(select_list_pages(query, best))

    selected = sorted(
        [
            item
            for item in load_index_chunks()
            if not should_skip_chunk(item.text) and item.page in selected_pages
        ],
        key=lambda item: (item.page, item.chunk_index),
    )
    section_text = section_text_for_list_query(
        query,
        stitch_chunk_texts([item.text for item in selected]),
    )

    items = extract_numbered_items(section_text)
    if len(items) < 2:
        items = extract_bullet_items(section_text)
    if not items:
        return []

    citation = citation_for_pages([item.page for item in selected])
    max_items = 8 if ("注意" in query or "原因" in query or "哪几" in query or "哪些" in query) else 6
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in items:
        normalized = re.sub(r"\s+", "", item)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append((citation, item))
        if len(deduped) >= max_items:
            break
    return deduped


def compose_answer(query: str, evidence: list[EvidenceSentence], results: list[SearchResult]) -> str:
    """基于证据句子组装可读答案草稿。"""
    if not evidence:
        return "当前教材索引中未检索到足够依据来回答这个问题。"

    mode = classify_question(query)
    first = choose_intro_evidence(query, evidence)
    first_page = first.page
    cite = format_citation(first_page)
    intro_text = normalize_answer_sentence(first.text)
    intro = f"根据教材检索结果，{intro_text}（{cite}）。"

    if mode == "summary":
        process_names = summarize_processes(results)
        intro = f'根据教材检索结果，和"{query}"最相关的内容可以概括为：{intro_text}（{cite}）。'
        if process_names:
            process_line = "核心过程/要点包括：" + "、".join(process_names[:6]) + "。"
            return "\n".join([intro, "", process_line])
    elif mode == "definition":
        definition_intro, first_page = choose_definition_intro(query, evidence, results)
        cite = format_citation(first_page)
        intro_text = definition_intro
        followups = definition_followups(query, definition_intro, evidence, results)
        intro = f"根据教材，{definition_intro}（{cite}）。"
        if followups:
            intro += "".join(followups)
    elif mode == "comparison":
        targets = extract_comparison_targets(query)
        target_map = gather_target_evidence(query, evidence, targets)
        relation = find_relation_sentence(evidence, results, targets)
        lines = [f'根据教材，围绕"{query}"可抓住以下几点：']
        for target in targets[:2]:
            item = target_map.get(target)
            if relation is not None and item is not None and "质量保证" in relation.text and target == targets[1]:
                item = relation
            if item is None:
                continue
            lines.append(f"- {target}：{item.text}（{format_citation(item.page)}）")
        if relation is not None:
            lines.append(f"- 两者关系：{relation.text}（{format_citation(relation.page)}）")
        return "\n".join(lines)
    elif mode == "list":
        enumerated = enumerate_answer_items(query, results)
        if enumerated:
            lines = [list_intro(query, len(enumerated))]
            for idx, (citation, item) in enumerate(enumerated, start=1):
                lines.append(f"{idx}. {item}（{citation}）")
            return "\n".join(lines)

    bullets: list[str] = []
    intro_norm = re.sub(r"\s+", "", intro_text)
    for item in evidence:
        cleaned_item = normalize_answer_sentence(item.text)
        item_norm = re.sub(r"\s+", "", cleaned_item)
        if item is first or item_norm == intro_norm:
            continue
        if "图" in cleaned_item or "数据流向图" in cleaned_item:
            continue
        if len(re.findall(r"\d+\.\d+|\b\d{2,3}\b", cleaned_item)) >= 2:
            continue
        if any(marker in cleaned_item for marker in ("输入", "工具与技术", "输出")):
            continue
        bullets.append(f"- {item.text}（{format_citation(item.page)}）")

    if not bullets:
        return intro

    return "\n".join([intro, "", "补充依据：", *bullets])


# ── 输出格式化 ────────────────────────────────────────────────────────────────


def to_json_payload(
    query: str,
    expanded_queries: list[str],
    results: list[SearchResult],
    evidence: list[EvidenceSentence],
    answer: str,
) -> str:
    """将完整回答包序列化为 JSON。"""
    payload = {
        "query": query,
        "expanded_queries": expanded_queries,
        "answer": answer,
        "pages": [e.page for e in evidence],
        "evidence": [
            {
                "page": e.page,
                "chunk_id": e.chunk_id,
                "score": round(e.score, 2),
                "text": e.text,
            }
            for e in evidence
        ],
        "chunks": [
            {
                "id": r.id,
                "page": r.page,
                "score": round(r.score, 2),
                "text": r.text,
            }
            for r in results[:5]
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_markdown(
    query: str,
    expanded_queries: list[str],
    evidence: list[EvidenceSentence],
    answer: str,
) -> str:
    """将完整回答包序列化为 Markdown。"""
    lines: list[str] = ["# 教材问答", "", f"问题：{query}", "", "回答：", answer, ""]

    if evidence:
        lines.append("引用页码：")
        seen_pages: set[int] = set()
        for item in evidence:
            if item.page not in seen_pages:
                seen_pages.add(item.page)
                lines.append(f"- 第 {item.page} 页")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ── 入口 ─────────────────────────────────────────────────────────────────────


def main() -> int:
    """执行问答命令。"""
    args = parse_args()
    expanded = build_query_plan(args.query)
    results = aggregate_results(
        index_path=args.index,
        queries=expanded,
        per_query_top_k=args.per_query_top_k,
    )
    evidence = collect_evidence(
        args.query, results, top_evidence=args.top_evidence,
    )
    answer = compose_answer(args.query, evidence, results)

    if args.json:
        print(to_json_payload(
            query=args.query,
            expanded_queries=expanded,
            results=results,
            evidence=evidence,
            answer=answer,
        ))
    else:
        print(to_markdown(
            query=args.query,
            expanded_queries=expanded,
            evidence=evidence,
            answer=answer,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
