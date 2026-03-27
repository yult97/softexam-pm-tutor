#!/usr/bin/env python3
"""重建核心集、bug 回归集、自动生成变体与聚合 evals。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval_suites import (
    DEFAULT_BUG_REGRESSIONS,
    DEFAULT_COMBINED_EVALS,
    DEFAULT_CORE_EVALS,
    DEFAULT_GENERATED_VARIANTS,
    rebuild_combined_evals,
    regenerate_generated_variants,
)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="重建 softexam-pm-tutor 的多层 eval 集合。")
    parser.add_argument("--core", type=Path, default=DEFAULT_CORE_EVALS, help="核心回归集路径。")
    parser.add_argument("--bugs", type=Path, default=DEFAULT_BUG_REGRESSIONS, help="bug 回归集路径。")
    parser.add_argument(
        "--generated",
        type=Path,
        default=DEFAULT_GENERATED_VARIANTS,
        help="自动生成变体集路径。",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_COMBINED_EVALS, help="聚合输出路径。")
    parser.add_argument(
        "--skip-generated",
        action="store_true",
        help="只重建聚合 evals.json，不刷新 generated_variants.json。",
    )
    return parser.parse_args()


def main() -> int:
    """执行重建。"""
    args = parse_args()

    generated_payload = None
    if not args.skip_generated:
        generated_payload = regenerate_generated_variants(
            bug_suite_path=args.bugs,
            generated_suite_path=args.generated,
        )

    combined_payload = rebuild_combined_evals(
        core_suite_path=args.core,
        bug_suite_path=args.bugs,
        generated_suite_path=args.generated,
        output_path=args.output,
    )

    print(
        json.dumps(
            {
                "success": True,
                "core": str(args.core),
                "bugs": str(args.bugs),
                "generated": str(args.generated),
                "output": str(args.output),
                "generated_variants": len(generated_payload["tests"]) if generated_payload else None,
                "combined_evals": len(combined_payload["tests"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
