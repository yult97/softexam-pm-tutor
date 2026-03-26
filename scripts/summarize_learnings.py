#!/usr/bin/env python3
"""汇总反馈事件并生成可晋升的学习建议。"""

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from _shared import (
    DEFAULT_ALIAS_MAP,
    DEFAULT_FEEDBACK_LOG,
    DEFAULT_PROMOTION_CANDIDATES,
    clear_alias_cache,
)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="汇总教材问答反馈并生成学习建议。")
    parser.add_argument("--log", type=Path, default=DEFAULT_FEEDBACK_LOG, help="反馈日志路径。")
    parser.add_argument("--alias-map", type=Path, default=DEFAULT_ALIAS_MAP, help="别名表路径。")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_PROMOTION_CANDIDATES,
        help="学习建议输出路径。",
    )
    parser.add_argument(
        "--promotion-threshold",
        type=int,
        help="晋升阈值；默认读取 alias_map.json 中的 meta.promotion_threshold。",
    )
    parser.add_argument(
        "--promote-aliases",
        action="store_true",
        help="将达到阈值的 alias 候选写回 alias_map.json。",
    )
    return parser.parse_args()


def load_events(log_path: Path) -> list[dict]:
    """读取反馈事件。"""
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


def load_alias_payload(alias_map_path: Path) -> dict:
    """读取完整 alias 配置。"""
    if not alias_map_path.exists():
        return {"meta": {"promotion_threshold": 3}, "aliases": {}}
    try:
        payload = json.loads(alias_map_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"meta": {"promotion_threshold": 3}, "aliases": {}}
    if not isinstance(payload, dict):
        return {"meta": {"promotion_threshold": 3}, "aliases": {}}
    payload.setdefault("meta", {"promotion_threshold": 3})
    payload.setdefault("aliases", {})
    return payload


def aggregate_alias_pairs(events: list[dict]) -> dict[str, Counter]:
    """聚合 alias 候选频次。"""
    alias_counts: dict[str, Counter] = defaultdict(Counter)
    for event in events:
        for pair in event.get("alias_pairs", []):
            canonical = str(pair.get("canonical", "")).strip()
            alias = str(pair.get("alias", "")).strip()
            if not canonical or not alias:
                continue
            alias_counts[canonical][alias] += 1
    return alias_counts


def top_examples(events: list[dict], issue_type: str, limit: int = 3) -> list[str]:
    """返回某类问题的示例 query。"""
    matched = [str(event.get("query", "")).strip() for event in events if event.get("issue_type") == issue_type]
    deduped: list[str] = []
    for query in matched:
        if query and query not in deduped:
            deduped.append(query)
        if len(deduped) >= limit:
            break
    return deduped


def build_report(
    events: list[dict],
    issue_counts: Counter,
    alias_counts: dict[str, Counter],
    threshold: int,
) -> str:
    """生成 Markdown 报告。"""
    lines = [
        "# Learning Summary",
        "",
        f"- generated_at: {datetime.now(timezone.utc).isoformat()}",
        f"- total_events: {len(events)}",
        f"- promotion_threshold: {threshold}",
        "",
        "## Issue Counts",
        "",
    ]

    if not issue_counts:
        lines.extend(["暂无反馈事件。", ""])
    else:
        for issue_type, count in issue_counts.most_common():
            lines.append(f"- {issue_type}: {count}")
        lines.append("")

    lines.extend(["## High-Frequency Examples", ""])
    if not issue_counts:
        lines.extend(["暂无高频问题。", ""])
    else:
        for issue_type, count in issue_counts.most_common(5):
            examples = top_examples(events, issue_type)
            if examples:
                lines.append(f"- {issue_type} ({count}): " + " | ".join(examples))
        lines.append("")

    lines.extend(["## Alias Promotion Candidates", ""])
    has_candidate = False
    for canonical, counter in sorted(alias_counts.items()):
        promoted = [f"{alias} ({count})" for alias, count in counter.most_common() if count >= threshold]
        observed = [f"{alias} ({count})" for alias, count in counter.most_common()]
        if promoted:
            has_candidate = True
            lines.append(f"- {canonical}: promote -> " + ", ".join(promoted))
        elif observed:
            lines.append(f"- {canonical}: observe -> " + ", ".join(observed))
    if not alias_counts:
        lines.append("暂无 alias 候选。")
    lines.append("")

    lines.extend(["## Eval Suggestions", ""])
    if not issue_counts:
        lines.append("暂无建议。")
    else:
        for issue_type, count in issue_counts.most_common(3):
            examples = top_examples(events, issue_type, limit=1)
            if not examples:
                continue
            lines.append(f"- 为 `{issue_type}` 增加 eval：`{examples[0]}`")
    lines.append("")

    if has_candidate:
        lines.append("## Promotion Rule")
        lines.append("")
        lines.append("- 达到阈值的 alias 候选可在通过回归验证后写入 `references/alias_map.json`。")
        lines.append("- 晋升后应重新运行代表性问答与 `evals/evals.json`。")
        lines.append("")

    return "\n".join(lines)


def promote_aliases(alias_map_path: Path, alias_counts: dict[str, Counter], threshold: int) -> list[dict[str, str]]:
    """将达到阈值的 alias 候选写回 alias_map.json。"""
    payload = load_alias_payload(alias_map_path)
    aliases = payload.setdefault("aliases", {})
    if not isinstance(aliases, dict):
        aliases = {}
        payload["aliases"] = aliases

    promoted: list[dict[str, str]] = []
    for canonical, counter in alias_counts.items():
        existing = aliases.get(canonical, [])
        if not isinstance(existing, list):
            existing = []
        for alias, count in counter.items():
            if count < threshold or alias in existing:
                continue
            existing.append(alias)
            promoted.append({"canonical": canonical, "alias": alias})
        aliases[canonical] = existing

    if promoted:
        alias_map_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        clear_alias_cache()

    return promoted


def main() -> int:
    """运行学习汇总。"""
    args = parse_args()
    events = load_events(args.log)
    alias_payload = load_alias_payload(args.alias_map)
    threshold = args.promotion_threshold or int(alias_payload.get("meta", {}).get("promotion_threshold", 3))
    issue_counts = Counter(str(event.get("issue_type", "other")) for event in events)
    alias_counts = aggregate_alias_pairs(events)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = build_report(events, issue_counts, alias_counts, threshold)
    args.output.write_text(report, encoding="utf-8")

    promoted: list[dict[str, str]] = []
    if args.promote_aliases:
        promoted = promote_aliases(args.alias_map, alias_counts, threshold)

    print(
        json.dumps(
            {
                "success": True,
                "events": len(events),
                "output": str(args.output),
                "threshold": threshold,
                "promoted_aliases": promoted,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
