#!/usr/bin/env python3
"""记录用户反馈，供 skill 后续自学习使用。"""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from _shared import DEFAULT_FEEDBACK_LOG, DEFAULT_SHARED_FEEDBACK_POOL, classify_query


COMMON_ISSUE_TYPES = (
    "wrong_chapter",
    "toc_noise",
    "exercise_noise",
    "definition_missed",
    "comparison_missed_relation",
    "summary_undercoverage",
    "citation_wrong",
    "alias_missing",
    "web_source_preference",
    "answer_not_grounded",
    "other",
)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="记录教材问答反馈事件。")
    parser.add_argument("query", help="原始用户问题。")
    parser.add_argument("--feedback", required=True, help="用户反馈或纠正内容。")
    parser.add_argument(
        "--issue-type",
        default="other",
        help="问题类型，例如: " + ", ".join(COMMON_ISSUE_TYPES),
    )
    parser.add_argument("--expected", help="更期望的答案或正确方向。")
    parser.add_argument("--answer", help="当时给出的回答，可选。")
    parser.add_argument("--confidence", type=float, help="当时回答置信度，可选。")
    parser.add_argument("--page", type=int, action="append", default=[], help="涉及页码，可重复。")
    parser.add_argument(
        "--alias-pair",
        action="append",
        default=[],
        help='别名候选，格式为 "规范术语=别名"，可重复。',
    )
    parser.add_argument(
        "--preferred-domain",
        action="append",
        default=[],
        help='推荐优先查询的域名，可重复，例如 "pmi.org"。',
    )
    parser.add_argument(
        "--blocked-domain",
        action="append",
        default=[],
        help='建议避免使用的域名，可重复，例如 "example.com"。',
    )
    parser.add_argument("--log", type=Path, default=DEFAULT_FEEDBACK_LOG, help="反馈日志路径。")
    parser.add_argument(
        "--shared-pool",
        type=Path,
        default=DEFAULT_SHARED_FEEDBACK_POOL,
        help="共享反馈池路径；默认自动同步到这里。",
    )
    parser.add_argument(
        "--no-sync-shared",
        action="store_true",
        help="只记录本地日志，不自动同步到共享反馈池。",
    )
    parser.add_argument(
        "--no-refresh-shared-memory",
        action="store_true",
        help="同步共享池后不自动重建共享记忆。",
    )
    return parser.parse_args()


def parse_alias_pairs(raw_pairs: list[str]) -> list[dict[str, str]]:
    """解析别名对。"""
    parsed: list[dict[str, str]] = []
    for raw in raw_pairs:
        if "=" not in raw:
            continue
        canonical, alias = raw.split("=", 1)
        canonical = canonical.strip()
        alias = alias.strip()
        if not canonical or not alias:
            continue
        parsed.append({"canonical": canonical, "alias": alias})
    return parsed


def normalize_domains(raw_domains: list[str]) -> list[str]:
    """归一化域名列表。"""
    normalized: list[str] = []
    for domain in raw_domains:
        cleaned = domain.strip().lower()
        cleaned = cleaned.removeprefix("https://").removeprefix("http://")
        cleaned = cleaned.split("/", 1)[0].strip()
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def sanitize_for_shared(event: dict) -> dict:
    """生成可直接写入共享反馈池的脱敏事件。"""
    shared_event = {
        "timestamp": event.get("timestamp"),
        "query": event.get("query"),
        "query_type": event.get("query_type"),
        "issue_type": event.get("issue_type"),
        "pages": event.get("pages", []),
        "alias_pairs": event.get("alias_pairs", []),
        "confidence": event.get("confidence"),
        "source": event.get("source", "user_feedback"),
        "preferred_domains": event.get("preferred_domains", []),
        "blocked_domains": event.get("blocked_domains", []),
    }
    fingerprint_input = json.dumps(shared_event, ensure_ascii=False, sort_keys=True)
    shared_event["event_id"] = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:16]
    return shared_event


def shared_event_exists(pool_path: Path, event_id: str) -> bool:
    """检查共享反馈池中是否已存在相同事件。"""
    if not pool_path.exists():
        return False
    with pool_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(existing.get("event_id", "")).strip() == event_id:
                return True
    return False


def sync_to_shared_pool(shared_event: dict, pool_path: Path) -> bool:
    """将事件写入共享反馈池。"""
    pool_path.parent.mkdir(parents=True, exist_ok=True)
    event_id = str(shared_event.get("event_id", "")).strip()
    if event_id and shared_event_exists(pool_path, event_id):
        return False
    with pool_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(shared_event, ensure_ascii=False) + "\n")
    return True


def refresh_shared_memory(pool_path: Path) -> dict:
    """重建共享记忆。"""
    script_path = Path(__file__).with_name("build_shared_memory.py")
    completed = subprocess.run(
        [sys.executable, str(script_path), "--pool", str(pool_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    stdout = completed.stdout.strip() or "{}"
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"success": True, "raw_output": stdout}


def main() -> int:
    """写入一条反馈事件。"""
    args = parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": args.query,
        "query_type": classify_query(args.query),
        "feedback": args.feedback,
        "issue_type": args.issue_type,
        "expected": args.expected,
        "answer": args.answer,
        "confidence": args.confidence,
        "pages": args.page,
        "alias_pairs": parse_alias_pairs(args.alias_pair),
        "preferred_domains": normalize_domains(args.preferred_domain),
        "blocked_domains": normalize_domains(args.blocked_domain),
        "source": "user_feedback",
    }

    with args.log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    shared_synced = False
    shared_event = sanitize_for_shared(event)
    if not args.no_sync_shared:
        shared_synced = sync_to_shared_pool(shared_event, args.shared_pool)

    refresh_result = None
    if not args.no_sync_shared and not args.no_refresh_shared_memory:
        refresh_result = refresh_shared_memory(args.shared_pool)

    print(
        json.dumps(
            {
                "success": True,
                "log": str(args.log),
                "issue_type": args.issue_type,
                "query_type": event["query_type"],
                "alias_pairs": event["alias_pairs"],
                "preferred_domains": event["preferred_domains"],
                "blocked_domains": event["blocked_domains"],
                "shared_pool": str(args.shared_pool),
                "shared_synced": shared_synced,
                "shared_event_id": shared_event["event_id"],
                "shared_memory_refresh": refresh_result,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
