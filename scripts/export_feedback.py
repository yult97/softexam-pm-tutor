#!/usr/bin/env python3
"""导出可共享的反馈包，用于汇入共享记忆池。"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from _shared import DEFAULT_FEEDBACK_LOG


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="导出可共享的教材问答反馈包。")
    parser.add_argument("--log", type=Path, default=DEFAULT_FEEDBACK_LOG, help="本地反馈日志路径。")
    parser.add_argument("--output", type=Path, required=True, help="导出 JSON 文件路径。")
    parser.add_argument(
        "--source-id",
        default="anonymous",
        help="导出源标识，可用匿名团队/站点名称。",
    )
    parser.add_argument(
        "--include-text",
        action="store_true",
        help="是否导出自由文本字段 feedback/expected；默认不导出。",
    )
    return parser.parse_args()


def load_events(log_path: Path) -> list[dict]:
    """读取本地反馈事件。"""
    if not log_path.exists():
        return []

    events: list[dict] = []
    with log_path.open("r", encoding="utf-8") as handle:
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


def sanitize_event(event: dict, include_text: bool) -> dict:
    """生成可共享的脱敏事件。"""
    sanitized = {
        "timestamp": event.get("timestamp"),
        "query": event.get("query"),
        "query_type": event.get("query_type"),
        "issue_type": event.get("issue_type"),
        "pages": event.get("pages", []),
        "alias_pairs": event.get("alias_pairs", []),
        "preferred_domains": event.get("preferred_domains", []),
        "blocked_domains": event.get("blocked_domains", []),
        "confidence": event.get("confidence"),
        "source": event.get("source", "user_feedback"),
    }
    if include_text:
        sanitized["feedback"] = event.get("feedback")
        sanitized["expected"] = event.get("expected")

    fingerprint_input = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
    sanitized["event_id"] = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:16]
    return sanitized


def main() -> int:
    """导出反馈包。"""
    args = parse_args()
    raw_events = load_events(args.log)
    events = [sanitize_event(event, include_text=args.include_text) for event in raw_events]

    export_payload = {
        "version": 1,
        "skill": "softexam-pm-tutor",
        "source_id": args.source_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "event_count": len(events),
        "events": events,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(export_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        json.dumps(
            {
                "success": True,
                "output": str(args.output),
                "event_count": len(events),
                "source_id": args.source_id,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
