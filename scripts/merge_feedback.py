#!/usr/bin/env python3
"""合并多个用户导出的反馈包，生成共享反馈池。"""

import argparse
import json
from pathlib import Path

from _shared import DEFAULT_SHARED_FEEDBACK_POOL


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="合并多个教材问答反馈包。")
    parser.add_argument("packages", nargs="+", type=Path, help="导出的反馈包 JSON 文件。")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_SHARED_FEEDBACK_POOL,
        help="共享反馈池输出路径。",
    )
    return parser.parse_args()


def read_existing_ids(output_path: Path) -> set[str]:
    """读取已存在的 event_id。"""
    if not output_path.exists():
        return set()

    event_ids: set[str] = set()
    with output_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_id = str(event.get("event_id", "")).strip()
            if event_id:
                event_ids.add(event_id)
    return event_ids


def load_package(path: Path) -> list[dict]:
    """读取单个反馈包。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    events = payload.get("events", [])
    return events if isinstance(events, list) else []


def main() -> int:
    """执行合并。"""
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    existing_ids = read_existing_ids(args.output)
    merged_count = 0

    with args.output.open("a", encoding="utf-8") as handle:
        for package_path in args.packages:
            events = load_package(package_path)
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_id = str(event.get("event_id", "")).strip()
                if not event_id or event_id in existing_ids:
                    continue
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                existing_ids.add(event_id)
                merged_count += 1

    print(
        json.dumps(
            {
                "success": True,
                "output": str(args.output),
                "merged_count": merged_count,
                "total_unique_events": len(existing_ids),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
