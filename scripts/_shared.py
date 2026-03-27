#!/usr/bin/env python3
"""公共模块：检索类型定义、文本归一化、题型识别与打分逻辑。"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple


# ── 路径常量 ──────────────────────────────────────────────────────────────────

SCRIPT_DIR: Path = Path(__file__).resolve().parent
DEFAULT_INDEX: Path = SCRIPT_DIR.parent / "references" / "book_chunks.jsonl"
DEFAULT_ALIAS_MAP: Path = SCRIPT_DIR.parent / "references" / "alias_map.json"
LEARNINGS_DIR: Path = SCRIPT_DIR.parent / "learnings"
DEFAULT_FEEDBACK_LOG: Path = LEARNINGS_DIR / "feedback_events.jsonl"
DEFAULT_PROMOTION_CANDIDATES: Path = LEARNINGS_DIR / "promotion_candidates.md"
SHARED_MEMORY_DIR: Path = SCRIPT_DIR.parent / "shared_memory"
DEFAULT_SHARED_FEEDBACK_POOL: Path = SHARED_MEMORY_DIR / "feedback_pool.jsonl"
DEFAULT_SHARED_ALIAS_MEMORY: Path = SHARED_MEMORY_DIR / "alias_memory.json"
DEFAULT_SHARED_RETRIEVAL_MEMORY: Path = SHARED_MEMORY_DIR / "retrieval_memory.json"
DEFAULT_SHARED_PATTERN_MEMORY: Path = SHARED_MEMORY_DIR / "pattern_memory.json"
DEFAULT_SHARED_WEB_SOURCE_MEMORY: Path = SHARED_MEMORY_DIR / "web_source_memory.json"


# ── 数据类型 ──────────────────────────────────────────────────────────────────

class SearchResult(NamedTuple):
    """一条检索命中记录。"""

    id: str
    page: int
    chunk_index: int
    score: float
    text: str


# ── 停用词 ────────────────────────────────────────────────────────────────────

COMMON_CN_STOP_TERMS: frozenset[str] = frozenset({
    "什么", "怎么", "如何", "请问", "一下", "一下子", "一下吧",
    "有关", "关于", "根据", "教材", "书里", "本书",
    "内容", "解释", "说明", "总结", "分析",
})

QUESTION_FILLERS: tuple[str, ...] = (
    "什么是", "是什么", "怎么理解", "如何理解", "请解释", "解释",
    "说明", "介绍", "根据教材", "按教材", "书里怎么说", "帮我",
    "请问", "讲讲", "总结", "概括", "归纳", "考点", "提纲", "一下",
    "呢", "呀", "啊", "嘛", "么", "吧",
)

SUMMARY_HINTS: tuple[str, ...] = ("总结", "概括", "归纳", "考点", "提纲")
DEFINITION_HINTS: tuple[str, ...] = ("什么是", "是什么", "定义", "含义", "解释", "理解")
COMPARISON_HINTS: tuple[str, ...] = ("区别", "比较", "关系", "不同", "异同")
LIST_HINTS: tuple[str, ...] = (
    "注意事项", "需要注意", "应注意", "注意什么",
    "原因", "有哪些", "哪几点", "哪几项", "包括哪些", "包括什么",
    "表现", "体现", "展现", "具体表现",
)


# ── 文本归一化 ────────────────────────────────────────────────────────────────

def normalize_for_match(text: str) -> str:
    """去除空白并转小写，用于短语匹配。"""
    return re.sub(r"\s+", "", text).lower()


@lru_cache(maxsize=8)
def load_alias_map(alias_path: Path = DEFAULT_ALIAS_MAP) -> dict[str, list[str]]:
    """读取术语别名表。"""
    cleaned = _read_alias_file(alias_path)
    shared = _read_alias_file(DEFAULT_SHARED_ALIAS_MEMORY)
    return merge_alias_maps(cleaned, shared)


def clear_alias_cache() -> None:
    """清理别名缓存，供反馈晋升后刷新。"""
    load_alias_map.cache_clear()


def clear_shared_memory_cache() -> None:
    """清理所有共享记忆缓存。"""
    clear_alias_cache()
    load_retrieval_memory.cache_clear()
    load_pattern_memory.cache_clear()
    load_web_source_memory.cache_clear()


def _read_alias_file(alias_path: Path) -> dict[str, list[str]]:
    """读取单个 alias 文件。"""
    if not alias_path.exists():
        return {}

    try:
        payload = json.loads(alias_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    aliases = payload.get("aliases", {})
    if not isinstance(aliases, dict):
        return {}

    cleaned: dict[str, list[str]] = {}
    for canonical, alias_list in aliases.items():
        if not isinstance(canonical, str):
            continue
        if not isinstance(alias_list, list):
            continue
        deduped: list[str] = []
        for alias in alias_list:
            if isinstance(alias, str) and alias not in deduped:
                deduped.append(alias)
        cleaned[canonical] = deduped
    return cleaned


def merge_alias_maps(*maps: dict[str, list[str]]) -> dict[str, list[str]]:
    """合并多个 alias map。"""
    merged: dict[str, list[str]] = {}
    for alias_map in maps:
        for canonical, aliases in alias_map.items():
            bucket = merged.setdefault(canonical, [])
            for alias in aliases:
                if alias not in bucket:
                    bucket.append(alias)
    return merged


@lru_cache(maxsize=4)
def load_retrieval_memory(memory_path: Path = DEFAULT_SHARED_RETRIEVAL_MEMORY) -> dict:
    """读取共享检索记忆。"""
    return _read_json_file(memory_path, {"meta": {}, "query_page_hints": {}, "focus_page_hints": {}})


@lru_cache(maxsize=4)
def load_pattern_memory(memory_path: Path = DEFAULT_SHARED_PATTERN_MEMORY) -> dict:
    """读取共享题型记忆。"""
    return _read_json_file(memory_path, {"meta": {}, "query_type_preferences": {}, "issue_type_counts": {}})


@lru_cache(maxsize=4)
def load_web_source_memory(memory_path: Path = DEFAULT_SHARED_WEB_SOURCE_MEMORY) -> dict:
    """读取共享联网来源记忆。"""
    return _read_json_file(
        memory_path,
        {
            "meta": {},
            "observed_preferred_domains": [],
            "observed_blocked_domains": [],
            "preferred_domains": [],
            "blocked_domains": [],
            "preferred_by_query_type": {},
        },
    )


def _read_json_file(path: Path, default: dict) -> dict:
    """读取 JSON 文件，失败则返回默认值。"""
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
    return payload if isinstance(payload, dict) else default


def alias_neighbors(term: str, alias_path: Path = DEFAULT_ALIAS_MAP) -> list[str]:
    """返回与术语相关的规范词和别名。"""
    normalized_term = normalize_for_match(term)
    related: list[str] = []

    for canonical, aliases in load_alias_map(alias_path).items():
        candidates = [canonical, *aliases]
        normalized_candidates = [normalize_for_match(item) for item in candidates]
        if normalized_term not in normalized_candidates:
            continue
        for candidate in candidates:
            if candidate != term and candidate not in related:
                related.append(candidate)

    return related


def expand_with_aliases(terms: list[str], max_extra: int = 6) -> list[str]:
    """基于别名表扩展术语列表。"""
    expanded = list(terms)
    extra_count = 0
    for term in list(terms):
        for neighbor in alias_neighbors(term):
            if neighbor in expanded:
                continue
            expanded.append(neighbor)
            extra_count += 1
            if extra_count >= max_extra:
                return expanded
    return expanded


def strip_question_fillers(query: str) -> str:
    """移除问题中的通用提示词，保留核心主题。"""
    cleaned = query
    for filler in QUESTION_FILLERS:
        cleaned = cleaned.replace(filler, " ")
    cleaned = re.sub(r'[？?！!。，""\"\'、：:（）()\[\]{}]', " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def classify_query(query: str) -> str:
    """识别题型，用于调整检索与答案生成策略。"""
    if any(marker in query for marker in SUMMARY_HINTS):
        return "summary"
    if any(marker in query for marker in COMPARISON_HINTS):
        return "comparison"
    if any(marker in query for marker in DEFINITION_HINTS):
        return "definition"
    if any(marker in query for marker in LIST_HINTS):
        return "list"
    return "general"


def extract_focus_topics(query: str) -> list[str]:
    """提取问题中的核心主题词。"""
    cleaned = strip_question_fillers(query)
    topics: list[str] = []
    for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9_.+-]+", cleaned):
        token = token.strip()
        if len(token) >= 2 and token not in topics:
            topics.append(token)
    return expand_with_aliases(topics, max_extra=4)


def extract_comparison_targets(query: str) -> list[str]:
    """从比较题中抽取两侧概念。"""
    cleaned = strip_question_fillers(query)
    cleaned = re.sub(r"(的区别|区别|比较|关系|不同|异同)$", "", cleaned).strip()
    parts = re.split(r"\s*(?:和|与|及|跟|相比|相对于)\s*", cleaned)
    targets = [part.strip() for part in parts if len(part.strip()) >= 2]
    deduped: list[str] = []
    for target in targets:
        if target not in deduped:
            deduped.append(target)
    return deduped[:3]


def page_hint_boost(query: str, page: int) -> float:
    """根据共享检索记忆为相似问题的页码加权。"""
    memory = load_retrieval_memory()
    query_key = normalize_for_match(strip_question_fillers(query))
    boost = 0.0

    query_hints = memory.get("query_page_hints", {})
    if isinstance(query_hints, dict):
        hint = query_hints.get(query_key, {})
        boost += _extract_page_boost(hint, page, exact=True)

    focus_hints = memory.get("focus_page_hints", {})
    if isinstance(focus_hints, dict):
        for topic in extract_focus_topics(query):
            topic_key = normalize_for_match(topic)
            hint = focus_hints.get(topic_key, {})
            boost += _extract_page_boost(hint, page, exact=False)

    return boost


def _extract_page_boost(hint: dict, page: int, exact: bool) -> float:
    """从单条页码提示中提取分值。"""
    if not isinstance(hint, dict):
        return 0.0
    pages = hint.get("pages", [])
    if not isinstance(pages, list):
        return 0.0

    boost = 0.0
    for entry in pages:
        if not isinstance(entry, dict):
            continue
        if int(entry.get("page", -1)) != page:
            continue
        count = int(entry.get("count", 0))
        boost += min(count, 5) * (10.0 if exact else 4.0)
    return boost


def query_type_preferences(query_type: str) -> dict[str, bool]:
    """读取共享题型偏好。"""
    memory = load_pattern_memory()
    preferences = memory.get("query_type_preferences", {})
    if not isinstance(preferences, dict):
        return {}
    selected = preferences.get(query_type, {})
    return selected if isinstance(selected, dict) else {}


# ── 中文 n-gram ──────────────────────────────────────────────────────────────

def chinese_ngrams(sequence: str, min_n: int = 2, max_n: int = 4) -> list[str]:
    """从连续中文字符序列中生成 n-gram 列表。"""
    length = len(sequence)
    upper = min(max_n, length) + 1
    grams: list[str] = []
    for size in range(min_n, upper):
        for start in range(length - size + 1):
            grams.append(sequence[start : start + size])
    return grams


# ── 检索词提取 ────────────────────────────────────────────────────────────────

def extract_terms(query: str) -> list[str]:
    """从自然语言问题中提取加权检索词列表。"""
    terms: list[str] = []

    # 1. 全句归一化短语
    normalized = normalize_for_match(query)
    if len(normalized) >= 2:
        terms.append(normalized)

    # 2. 英文 / 数字 token
    for word in re.findall(r"[a-zA-Z0-9_.+-]+", query.lower()):
        if len(word) >= 2:
            terms.append(word)

    # 3. 连续中文片段 + n-gram
    for segment in re.findall(r"[\u4e00-\u9fff]+", query):
        if len(segment) >= 2 and segment not in COMMON_CN_STOP_TERMS:
            terms.append(segment)
            for gram in chinese_ngrams(segment):
                if gram not in COMMON_CN_STOP_TERMS:
                    terms.append(gram)

    # 4. 按长度降序去重
    seen: set[str] = set()
    unique: list[str] = []
    for term in sorted(terms, key=len, reverse=True):
        if term not in seen:
            seen.add(term)
            unique.append(term)
    return expand_with_aliases(unique)


def unique_term_coverage(terms: list[str], text: str) -> float:
    """计算文本覆盖了多少不同检索词。"""
    if not terms:
        return 0.0
    normalized = normalize_for_match(text)
    unique = [term for term in terms if len(term) >= 2]
    if not unique:
        return 0.0
    hit_count = sum(1 for term in unique if term in normalized)
    return hit_count / len(unique)


def looks_like_toc(text: str) -> bool:
    """判断文本是否更像目录页。"""
    compact = re.sub(r"\s+", " ", text)
    chapter_hits = len(re.findall(r"第\d+章", compact))
    leader_hits = compact.count("……") + compact.count("...")
    return "目录" in compact and (chapter_hits >= 2 or leader_hits >= 4)


def looks_like_reference(text: str) -> bool:
    """判断文本是否更像参考文献页。"""
    compact = re.sub(r"\s+", " ", text)
    bracket_refs = len(re.findall(r"\[\d+\]", compact))
    return "参考文献" in compact[:80] or bracket_refs >= 5


def looks_like_exercise(text: str) -> bool:
    """判断文本是否更像练习题或答案页。"""
    compact = re.sub(r"\s+", " ", text)
    option_hits = len(re.findall(r"[A-D][.．、]", compact))
    return (
        "本章练习" in compact
        or "参考答案" in compact
        or "选择题" in compact
        or option_hits >= 4
    )


# ── 文本打分 ──────────────────────────────────────────────────────────────────

def score_text(query: str, terms: list[str], text: str) -> float:
    """基于短语匹配和关键词出现频率计算相关度得分。"""
    norm_text = normalize_for_match(text)
    norm_query = normalize_for_match(query)
    core_query = normalize_for_match(strip_question_fillers(query))
    score = 0.0

    # 完整短语匹配
    if norm_query and norm_query in norm_text:
        score += 80.0 + 10.0 * norm_text.count(norm_query)
    if core_query and core_query != norm_query and len(core_query) >= 4 and core_query in norm_text:
        score += 90.0 + 12.0 * norm_text.count(core_query)

    # 逐词匹配
    for term in terms:
        count = norm_text.count(term)
        if count == 0:
            continue

        if len(term) >= 6:
            weight = 20.0
        elif len(term) >= 4:
            weight = 12.0
        elif len(term) == 3:
            weight = 7.0
        else:
            weight = 3.0

        score += min(count, 5) * weight

    score += unique_term_coverage(terms, text) * 30.0
    return score
