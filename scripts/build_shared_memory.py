#!/usr/bin/env python3
"""从共享反馈池构建共享记忆文件。"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from _shared import (
    DEFAULT_ALIAS_MAP,
    DEFAULT_SHARED_ALIAS_MEMORY,
    DEFAULT_SHARED_FEEDBACK_POOL,
    DEFAULT_SHARED_PATTERN_MEMORY,
    DEFAULT_SHARED_RETRIEVAL_MEMORY,
    DEFAULT_SHARED_WEB_SOURCE_MEMORY,
    clear_shared_memory_cache,
    extract_focus_topics,
    normalize_for_match,
    strip_question_fillers,
)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="从共享反馈池构建共享记忆。")
    parser.add_argument("--pool", type=Path, default=DEFAULT_SHARED_FEEDBACK_POOL, help="共享反馈池路径。")
    parser.add_argument("--alias-map", type=Path, default=DEFAULT_ALIAS_MAP, help="核心 alias map 路径。")
    parser.add_argument(
        "--alias-output",
        type=Path,
        default=DEFAULT_SHARED_ALIAS_MEMORY,
        help="共享 alias memory 输出路径。",
    )
    parser.add_argument(
        "--retrieval-output",
        type=Path,
        default=DEFAULT_SHARED_RETRIEVAL_MEMORY,
        help="共享 retrieval memory 输出路径。",
    )
    parser.add_argument(
        "--pattern-output",
        type=Path,
        default=DEFAULT_SHARED_PATTERN_MEMORY,
        help="共享 pattern memory 输出路径。",
    )
    parser.add_argument(
        "--web-output",
        type=Path,
        default=DEFAULT_SHARED_WEB_SOURCE_MEMORY,
        help="共享联网来源记忆输出路径。",
    )
    parser.add_argument(
        "--promotion-threshold",
        type=int,
        help="晋升阈值；默认读取 references/alias_map.json 中的 meta.promotion_threshold。",
    )
    return parser.parse_args()


def load_events(pool_path: Path) -> list[dict]:
    """读取共享反馈池事件。"""
    if not pool_path.exists():
        return []
    events: list[dict] = []
    with pool_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def load_threshold(alias_map_path: Path, override: Optional[int]) -> int:
    """读取晋升阈值。"""
    if override is not None:
        return override
    if not alias_map_path.exists():
        return 3
    try:
        payload = json.loads(alias_map_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 3
    meta = payload.get("meta", {})
    if not isinstance(meta, dict):
        return 3
    return int(meta.get("promotion_threshold", 3))


def build_alias_memory(events: list[dict], threshold: int) -> tuple[dict, int]:
    """构建共享 alias memory。"""
    alias_counts: dict[str, Counter] = defaultdict(Counter)
    for event in events:
        for pair in event.get("alias_pairs", []):
            canonical = str(pair.get("canonical", "")).strip()
            alias = str(pair.get("alias", "")).strip()
            if not canonical or not alias:
                continue
            alias_counts[canonical][alias] += 1

    promoted_count = 0
    aliases: dict[str, list[str]] = {}
    sources: dict[str, dict[str, int]] = {}
    for canonical, counter in alias_counts.items():
        bucket: list[str] = []
        source_bucket: dict[str, int] = {}
        for alias, count in counter.most_common():
            if count < threshold:
                continue
            bucket.append(alias)
            source_bucket[alias] = count
            promoted_count += 1
        if bucket:
            aliases[canonical] = bucket
            sources[canonical] = source_bucket

    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "promotion_threshold": threshold,
            "source_events": len(events),
        },
        "aliases": aliases,
        "alias_counts": sources,
    }
    return payload, promoted_count


def build_retrieval_memory(events: list[dict]) -> dict:
    """构建共享 retrieval memory。"""
    query_page_hints: dict[str, Counter] = defaultdict(Counter)
    focus_page_hints: dict[str, Counter] = defaultdict(Counter)
    query_type_map: dict[str, Counter] = defaultdict(Counter)

    for event in events:
        query = str(event.get("query", "")).strip()
        if not query:
            continue
        pages = [int(page) for page in event.get("pages", []) if isinstance(page, int)]
        if not pages:
            continue

        query_key = normalize_for_match(strip_question_fillers(query))
        query_type = str(event.get("query_type", "general"))
        query_type_map[query_key][query_type] += 1
        for page in pages:
            query_page_hints[query_key][page] += 1

        for topic in extract_focus_topics(query):
            topic_key = normalize_for_match(topic)
            query_type_map[topic_key][query_type] += 1
            for page in pages:
                focus_page_hints[topic_key][page] += 1

    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_events": len(events),
        },
        "query_page_hints": serialize_page_counters(query_page_hints, query_type_map),
        "focus_page_hints": serialize_page_counters(focus_page_hints, query_type_map),
    }
    return payload


def serialize_page_counters(
    counters: dict[str, Counter],
    query_type_map: dict[str, Counter],
) -> dict[str, dict]:
    """将 Counter 转换成 JSON 结构。"""
    serialized: dict[str, dict] = {}
    for key, counter in counters.items():
        pages = [
            {"page": page, "count": count}
            for page, count in counter.most_common(5)
        ]
        query_type = query_type_map[key].most_common(1)[0][0] if query_type_map.get(key) else "general"
        serialized[key] = {
            "query_type": query_type,
            "pages": pages,
        }
    return serialized


def build_pattern_memory(events: list[dict], threshold: int) -> dict:
    """构建共享 pattern memory。"""
    issue_counts = Counter(str(event.get("issue_type", "other")) for event in events)
    query_type_issue_counts: dict[str, Counter] = defaultdict(Counter)

    for event in events:
        query_type = str(event.get("query_type", "general"))
        issue_type = str(event.get("issue_type", "other"))
        query_type_issue_counts[query_type][issue_type] += 1

    preferences = {
        "summary": {
            "prefer_process_markers": issue_counts.get("summary_undercoverage", 0) >= threshold,
        },
        "definition": {
            "prefer_definition_markers": issue_counts.get("definition_missed", 0) >= threshold,
        },
        "comparison": {
            "prefer_relation_sentences": issue_counts.get("comparison_missed_relation", 0) >= threshold,
        },
    }

    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_events": len(events),
            "promotion_threshold": threshold,
        },
        "issue_type_counts": dict(issue_counts),
        "query_type_issue_counts": {
            query_type: dict(counter)
            for query_type, counter in query_type_issue_counts.items()
        },
        "query_type_preferences": preferences,
    }
    return payload


def build_web_source_memory(events: list[dict], threshold: int) -> dict:
    """构建共享联网来源记忆。"""
    preferred_counts = Counter()
    blocked_counts = Counter()
    preferred_by_query_type: dict[str, Counter] = defaultdict(Counter)

    for event in events:
        query_type = str(event.get("query_type", "general"))
        for domain in event.get("preferred_domains", []):
            if not isinstance(domain, str):
                continue
            cleaned = domain.strip().lower()
            if not cleaned:
                continue
            preferred_counts[cleaned] += 1
            preferred_by_query_type[query_type][cleaned] += 1

        for domain in event.get("blocked_domains", []):
            if not isinstance(domain, str):
                continue
            cleaned = domain.strip().lower()
            if not cleaned:
                continue
            blocked_counts[cleaned] += 1

    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_events": len(events),
            "promotion_threshold": threshold,
        },
        "observed_preferred_domains": [
            {"domain": domain, "count": count}
            for domain, count in preferred_counts.most_common()
        ],
        "observed_blocked_domains": [
            {"domain": domain, "count": count}
            for domain, count in blocked_counts.most_common()
        ],
        "preferred_domains": [
            {"domain": domain, "count": count}
            for domain, count in preferred_counts.most_common()
            if count >= threshold
        ],
        "blocked_domains": [
            {"domain": domain, "count": count}
            for domain, count in blocked_counts.most_common()
            if count >= threshold
        ],
        "preferred_by_query_type": {
            query_type: [
                {"domain": domain, "count": count}
                for domain, count in counter.most_common()
                if count >= threshold
            ]
            for query_type, counter in preferred_by_query_type.items()
        },
    }
    return payload


def write_json(path: Path, payload: dict) -> None:
    """写入 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    """构建共享记忆。"""
    args = parse_args()
    events = load_events(args.pool)
    threshold = load_threshold(args.alias_map, args.promotion_threshold)

    alias_payload, promoted_count = build_alias_memory(events, threshold)
    retrieval_payload = build_retrieval_memory(events)
    pattern_payload = build_pattern_memory(events, threshold)
    web_payload = build_web_source_memory(events, threshold)

    write_json(args.alias_output, alias_payload)
    write_json(args.retrieval_output, retrieval_payload)
    write_json(args.pattern_output, pattern_payload)
    write_json(args.web_output, web_payload)
    clear_shared_memory_cache()

    print(
        json.dumps(
            {
                "success": True,
                "events": len(events),
                "promotion_threshold": threshold,
                "alias_output": str(args.alias_output),
                "retrieval_output": str(args.retrieval_output),
                "pattern_output": str(args.pattern_output),
                "web_output": str(args.web_output),
                "promoted_aliases": promoted_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
