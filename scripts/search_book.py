#!/usr/bin/env python3
"""对教材索引执行关键词检索，返回最相关的教材片段和页码。"""

import argparse
import json
import re
from pathlib import Path

from _shared import (
    DEFAULT_INDEX,
    SearchResult,
    classify_query,
    extract_comparison_targets,
    extract_terms,
    looks_like_exercise,
    looks_like_reference,
    looks_like_toc,
    normalize_for_match,
    page_hint_boost,
    query_type_preferences,
    score_text,
)


# ── CLI 参数 ──────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="检索教材 JSONL 索引。")
    parser.add_argument("query", help="检索字符串。")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX, help="JSONL 索引路径。")
    parser.add_argument("--top-k", type=int, default=5, help="返回命中数量。")
    parser.add_argument("--max-chars", type=int, default=260, help="每条摘要的最大字符数。")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式。")
    return parser.parse_args()


# ── 检索逻辑 ──────────────────────────────────────────────────────────────────


def load_results(index_path: Path, query: str, top_k: int) -> list[SearchResult]:
    """搜索索引并返回得分最高的命中记录。"""
    if not index_path.exists():
        raise FileNotFoundError(f"索引文件未找到: {index_path}")

    terms = extract_terms(query)
    query_mode = classify_query(query)
    preferences = query_type_preferences(query_mode)
    comparison_targets = extract_comparison_targets(query) if query_mode == "comparison" else []
    results: list[SearchResult] = []

    with index_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            text = str(record.get("text", ""))
            score = score_text(query=query, terms=terms, text=text)
            page_type = str(record.get("page_type", "body"))
            chapter_title = str(record.get("chapter_title") or "")

            # 前几页通常是封面/版权页，降低权重
            page_num = int(record["page"])
            if page_num <= 5:
                score *= 0.35
            score += page_hint_boost(query, page_num)

            if page_type == "toc" or looks_like_toc(text):
                continue
            if page_type == "reference" or looks_like_reference(text):
                continue
            if page_type == "exercise" or looks_like_exercise(text):
                score *= 0.18

            normalized_chapter = normalize_for_match(chapter_title)
            if normalized_chapter:
                chapter_hits = sum(1 for term in terms[:8] if term in normalized_chapter)
                score += chapter_hits * 12.0

            if query_mode == "summary":
                if any(marker in text for marker in ("过程包括", "主要作用", "管理基础", "过程概述")):
                    score += 24.0
                if any(marker in text for marker in ("输入", "工具与技术", "输出")) and "过程包括" not in text:
                    score -= 10.0
                if preferences.get("prefer_process_markers") and any(marker in text for marker in ("过程包括", "核心过程", "主要内容")):
                    score += 14.0

            if query_mode == "list":
                if any(marker in text for marker in ("主要包括", "主要有", "注意以下", "注意事项", "原因主要包括", "应该注意")):
                    score += 26.0
                if "●" in text or re.search(r"\(\d+\)", text):
                    score += 12.0
                if any(marker in text for marker in ("输入", "工具与技术", "输出")) and "主要包括" not in text:
                    score -= 12.0

            if query_mode == "definition":
                if any(marker in text for marker in ("是指", "是为了", "记录了", "授权")):
                    score += 16.0
                if preferences.get("prefer_definition_markers") and any(marker in text for marker in ("是指", "是为了", "是编写")):
                    score += 12.0

            if query_mode == "comparison" and comparison_targets:
                target_hits = sum(1 for target in comparison_targets if target in text)
                if target_hits >= 2:
                    score += 40.0
                elif target_hits == 1:
                    score += 12.0
                if preferences.get("prefer_relation_sentences") and any(marker in text for marker in ("有时被称为", "更广", "着眼于")):
                    score += 18.0

            if "参考答案" in text:
                score *= 0.1

            if score <= 0:
                continue

            results.append(
                SearchResult(
                    id=str(record["id"]),
                    page=page_num,
                    chunk_index=int(record["chunk_index"]),
                    score=score,
                    text=text,
                )
            )

    results.sort(key=lambda item: (-item.score, item.page, item.chunk_index))
    return results[:top_k]


# ── 输出格式化 ────────────────────────────────────────────────────────────────


def trim_excerpt(text: str, max_chars: int) -> str:
    """裁剪文本块用于紧凑展示。"""
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


def to_json(results: list[SearchResult], max_chars: int) -> str:
    """将命中结果序列化为 JSON 字符串。"""
    payload = [
        {
            "id": r.id,
            "page": r.page,
            "chunk_index": r.chunk_index,
            "score": round(r.score, 2),
            "excerpt": trim_excerpt(r.text, max_chars=max_chars),
            "text": r.text,
        }
        for r in results
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_markdown(query: str, results: list[SearchResult], max_chars: int) -> str:
    """将命中结果序列化为可读 Markdown。"""
    lines: list[str] = [f"# 检索结果: {query}", ""]
    if not results:
        lines.append("未找到命中结果。")
        return "\n".join(lines)

    for idx, r in enumerate(results, start=1):
        lines.extend([
            f"{idx}. PDF 第 {r.page} 页 | score={r.score:.2f} | {r.id}",
            trim_excerpt(r.text, max_chars=max_chars),
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


# ── 入口 ─────────────────────────────────────────────────────────────────────


def main() -> int:
    """执行检索命令。"""
    args = parse_args()
    results = load_results(
        index_path=args.index,
        query=args.query,
        top_k=args.top_k,
    )

    if args.json:
        print(to_json(results, max_chars=args.max_chars))
    else:
        print(to_markdown(query=args.query, results=results, max_chars=args.max_chars))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
