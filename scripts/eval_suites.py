#!/usr/bin/env python3
"""维护核心回归、bug 回归与自动生成变体的评测集合。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _shared import classify_query


SCRIPT_DIR: Path = Path(__file__).resolve().parent
EVALS_DIR: Path = SCRIPT_DIR.parent / "evals"
DEFAULT_CORE_EVALS: Path = EVALS_DIR / "core_evals.json"
DEFAULT_BUG_REGRESSIONS: Path = EVALS_DIR / "bug_regressions.json"
DEFAULT_GENERATED_VARIANTS: Path = EVALS_DIR / "generated_variants.json"
DEFAULT_COMBINED_EVALS: Path = EVALS_DIR / "evals.json"

INSTRUCTION_PREFIXES: tuple[str, ...] = (
    "按教材回答：",
    "按教材回答:",
    "只按教材回答：",
    "只按教材回答:",
    "根据高项教材，",
    "根据高项教材,",
    "根据教材，",
    "根据教材,",
    "请按教材回答：",
    "请按教材回答:",
)


def timestamp() -> str:
    """返回统一格式的 UTC 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


def empty_suite(kind: str) -> dict[str, Any]:
    """构造空的 suite 负载。"""
    return {
        "meta": {
            "kind": kind,
            "generated_at": timestamp(),
        },
        "tests": [],
    }


def load_suite(path: Path, kind: str) -> dict[str, Any]:
    """读取评测集合，不存在时返回空结构。"""
    if not path.exists():
        return empty_suite(kind)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty_suite(kind)
    if not isinstance(payload, dict):
        return empty_suite(kind)
    tests = payload.get("tests", [])
    if not isinstance(tests, list):
        tests = []
    payload["tests"] = [normalize_test(test) for test in tests if isinstance(test, dict)]
    meta = payload.get("meta", {})
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("kind", kind)
    payload["meta"] = meta
    return payload


def save_suite(path: Path, payload: dict[str, Any]) -> None:
    """写回评测集合。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    meta = payload.setdefault("meta", {})
    if isinstance(meta, dict):
        meta["generated_at"] = timestamp()
    payload["tests"] = [normalize_test(test) for test in payload.get("tests", []) if isinstance(test, dict)]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_expected_behavior(raw: Any) -> list[str]:
    """把各种 expected 输入归一化为字符串列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        chunks = re.split(r"[\n；;]+", raw)
    elif isinstance(raw, list):
        chunks = [str(item) for item in raw]
    else:
        chunks = [str(raw)]

    cleaned: list[str] = []
    for chunk in chunks:
        line = re.sub(r"\s+", " ", str(chunk)).strip(" -\t\r\n")
        if line and line not in cleaned:
            cleaned.append(line)
    return cleaned


def normalize_test(test: dict[str, Any]) -> dict[str, Any]:
    """规范化单条测试。"""
    prompt = re.sub(r"\s+", " ", str(test.get("prompt", ""))).strip()
    files = test.get("files", [])
    if not isinstance(files, list):
        files = []
    normalized: dict[str, Any] = {
        "prompt": prompt,
        "expected_behavior": normalize_expected_behavior(test.get("expected_behavior", [])),
        "files": [str(item) for item in files if str(item).strip()],
    }
    for key in ("issue_type", "query_type", "source", "source_event_id", "source_prompt"):
        value = test.get(key)
        if value not in (None, "", []):
            normalized[key] = value
    return normalized


def merge_tests(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """按 prompt 合并两条测试。"""
    merged = normalize_test(existing)
    candidate = normalize_test(incoming)
    merged_behavior = list(merged.get("expected_behavior", []))
    for line in candidate.get("expected_behavior", []):
        if line not in merged_behavior:
            merged_behavior.append(line)
    merged["expected_behavior"] = merged_behavior

    files = list(merged.get("files", []))
    for path in candidate.get("files", []):
        if path not in files:
            files.append(path)
    merged["files"] = files

    for key in ("issue_type", "query_type", "source", "source_event_id", "source_prompt"):
        if key not in merged and key in candidate:
            merged[key] = candidate[key]
    return merged


def upsert_test(tests: list[dict[str, Any]], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """按 prompt 写入或合并测试。"""
    candidate = normalize_test(candidate)
    if not candidate["prompt"]:
        return tests

    updated = list(tests)
    for index, test in enumerate(updated):
        if str(test.get("prompt", "")).strip() == candidate["prompt"]:
            updated[index] = merge_tests(test, candidate)
            return updated
    updated.append(candidate)
    return updated


def strip_instruction_prefix(prompt: str) -> str:
    """去掉提示词，保留核心问法。"""
    cleaned = prompt.strip()
    for prefix in INSTRUCTION_PREFIXES:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
            break
    cleaned = re.sub(r"(再结合.*补充.*|不要联网补充。?|不需要联网补充。?)$", "", cleaned).strip()
    return cleaned


def ensure_question_mark(prompt: str) -> str:
    """给问句补足中文问号。"""
    cleaned = prompt.strip()
    if not cleaned:
        return cleaned
    if cleaned[-1] in "？?":
        return cleaned
    return cleaned + "？"


def maybe_topic_from_definition(prompt: str) -> str:
    """从定义题中提取主题。"""
    for marker in ("什么是", "是什么", "怎么定义", "如何理解", "是什么意思"):
        if marker in prompt:
            topic = prompt.split(marker, 1)[0].strip(" ：:，,")
            if topic:
                return topic
    return ""


def apply_substitutions(prompt: str, substitutions: list[tuple[str, str]]) -> list[str]:
    """按替换规则生成近义问法。"""
    generated: list[str] = []
    for old, new in substitutions:
        if old not in prompt:
            continue
        variant = prompt.replace(old, new)
        if variant != prompt and variant not in generated:
            generated.append(variant)
    return generated


def generate_prompt_variants(prompt: str, query_type: str | None = None, limit: int = 6) -> list[str]:
    """根据原始 prompt 规则生成若干近义问法。"""
    base = strip_instruction_prefix(prompt)
    if not base:
        return []

    query_type = query_type or classify_query(base)
    variants: list[str] = []

    for candidate in (
        ensure_question_mark(base),
        ensure_question_mark(f"按教材回答：{base}"),
        ensure_question_mark(f"根据高项教材，{base}"),
        ensure_question_mark(f"书里怎么说{base}"),
    ):
        if candidate not in variants:
            variants.append(candidate)

    if query_type == "definition":
        topic = maybe_topic_from_definition(base)
        if topic:
            for candidate in (
                ensure_question_mark(f"{topic}怎么定义"),
                ensure_question_mark(f"什么是{topic}"),
            ):
                if candidate not in variants:
                    variants.append(candidate)

    if query_type == "list":
        substitutions = [
            ("拆分", "分解"),
            ("需要注意的点或者说原则", "需要注意什么"),
            ("需要注意的点", "需要注意什么"),
            ("原则", "注意事项"),
            ("原因", "主要原因"),
            ("具体在哪些方面展现出来的", "具体表现在哪些方面"),
        ]
        for candidate in apply_substitutions(base, substitutions):
            normalized = ensure_question_mark(candidate)
            if normalized not in variants:
                variants.append(normalized)

    if query_type == "comparison":
        for candidate in apply_substitutions(base, [("区别", "不同"), ("关系", "区别")]):
            normalized = ensure_question_mark(candidate)
            if normalized not in variants:
                variants.append(normalized)

    return [item for item in variants if item != ensure_question_mark(prompt.strip())][:limit]


def issue_type_defaults(issue_type: str, query_type: str, pages: list[int]) -> list[str]:
    """根据 issue type 生成默认期望。"""
    expectations: list[str] = []
    if query_type == "list":
        expectations.append("如教材为枚举题，应完整列出教材条目，不应漏项或串到别的列表")
    if query_type == "definition":
        expectations.append("应优先回到教材定义句，而不是泛泛解释")
    if query_type == "comparison":
        expectations.append("应分别说明比较对象，并补出两者关系或区别")
    if query_type == "summary":
        expectations.append("应覆盖核心过程或要点，而不是只截取单一句子")

    defaults = {
        "wrong_chapter": "应命中正确章节，不应串到相似术语或相邻章节",
        "toc_noise": "不应把目录页或页眉页脚当作主依据",
        "exercise_noise": "不应把练习题或参考答案当成主结论",
        "definition_missed": "应优先命中教材定义句",
        "comparison_missed_relation": "应明确给出两者的关系句或区别点",
        "summary_undercoverage": "应覆盖教材中的核心过程、要点或范围",
        "citation_wrong": "应给出正确教材页码",
        "alias_missing": "应识别常见简称、缩写或别名",
        "answer_not_grounded": "应以教材原文和教材依据为主，不应自由发挥",
        "web_source_preference": "联网补充应优先使用高质量官方来源，并放在教材答案之后",
    }
    if issue_type in defaults:
        expectations.append(defaults[issue_type])

    if pages:
        if len(set(pages)) == 1:
            expectations.append(f"应引用教材第 {pages[0]} 页")
        else:
            ordered = sorted(set(pages))
            expectations.append(f"应优先命中教材第 {ordered[0]}-{ordered[-1]} 页")

    deduped: list[str] = []
    for item in expectations:
        if item not in deduped:
            deduped.append(item)
    return deduped


def build_bug_regression_test(
    query: str,
    issue_type: str,
    expected: str | list[str] | None = None,
    feedback: str | None = None,
    pages: list[int] | None = None,
    source_event_id: str | None = None,
) -> dict[str, Any]:
    """把一次真实 bug 反馈转换成 bug 回归用例。"""
    prompt = ensure_question_mark(strip_instruction_prefix(query))
    query_type = classify_query(prompt)
    expectations = normalize_expected_behavior(expected)
    if not expectations and feedback:
        expectations = normalize_expected_behavior(feedback)
    defaults = issue_type_defaults(issue_type, query_type, pages or [])
    for line in defaults:
        if line not in expectations:
            expectations.append(line)

    test: dict[str, Any] = {
        "prompt": prompt,
        "expected_behavior": expectations,
        "files": [],
        "issue_type": issue_type,
        "query_type": query_type,
        "source": "bug_regression",
    }
    if source_event_id:
        test["source_event_id"] = source_event_id
    return normalize_test(test)


def regenerate_generated_variants(
    bug_suite_path: Path = DEFAULT_BUG_REGRESSIONS,
    generated_suite_path: Path = DEFAULT_GENERATED_VARIANTS,
) -> dict[str, Any]:
    """根据 bug 回归自动派生近义问法集合。"""
    bug_suite = load_suite(bug_suite_path, "bug_regressions")
    generated_tests: list[dict[str, Any]] = []

    for test in bug_suite.get("tests", []):
        prompt = str(test.get("prompt", "")).strip()
        query_type = str(test.get("query_type", "")).strip() or classify_query(prompt)
        for variant in generate_prompt_variants(prompt, query_type=query_type):
            generated_tests = upsert_test(
                generated_tests,
                {
                    "prompt": variant,
                    "expected_behavior": test.get("expected_behavior", []),
                    "files": test.get("files", []),
                    "issue_type": test.get("issue_type"),
                    "query_type": query_type,
                    "source": "generated_variant",
                    "source_prompt": prompt,
                },
            )

    payload = {
        "meta": {
            "kind": "generated_variants",
            "source_bug_tests": len(bug_suite.get("tests", [])),
        },
        "tests": generated_tests,
    }
    save_suite(generated_suite_path, payload)
    return payload


def rebuild_combined_evals(
    core_suite_path: Path = DEFAULT_CORE_EVALS,
    bug_suite_path: Path = DEFAULT_BUG_REGRESSIONS,
    generated_suite_path: Path = DEFAULT_GENERATED_VARIANTS,
    output_path: Path = DEFAULT_COMBINED_EVALS,
) -> dict[str, Any]:
    """将三层 eval 合并为兼容旧链路的 evals.json。"""
    merged: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    for path, kind in (
        (core_suite_path, "core_evals"),
        (bug_suite_path, "bug_regressions"),
        (generated_suite_path, "generated_variants"),
    ):
        suite = load_suite(path, kind)
        counts[kind] = len(suite.get("tests", []))
        for test in suite.get("tests", []):
            merged = upsert_test(merged, test)

    payload = {
        "meta": {
            "kind": "combined_evals",
            "counts": counts,
            "total_tests": len(merged),
        },
        "tests": merged,
    }
    save_suite(output_path, payload)
    return payload


def append_bug_regression(
    query: str,
    issue_type: str,
    expected: str | list[str] | None = None,
    feedback: str | None = None,
    pages: list[int] | None = None,
    source_event_id: str | None = None,
    bug_suite_path: Path = DEFAULT_BUG_REGRESSIONS,
    generated_suite_path: Path = DEFAULT_GENERATED_VARIANTS,
    combined_suite_path: Path = DEFAULT_COMBINED_EVALS,
) -> dict[str, Any]:
    """追加一条真实 bug 回归，并自动刷新变体与聚合文件。"""
    bug_suite = load_suite(bug_suite_path, "bug_regressions")
    candidate = build_bug_regression_test(
        query=query,
        issue_type=issue_type,
        expected=expected,
        feedback=feedback,
        pages=pages or [],
        source_event_id=source_event_id,
    )
    bug_suite["tests"] = upsert_test(bug_suite.get("tests", []), candidate)
    save_suite(bug_suite_path, bug_suite)

    generated_payload = regenerate_generated_variants(
        bug_suite_path=bug_suite_path,
        generated_suite_path=generated_suite_path,
    )
    combined_payload = rebuild_combined_evals(
        bug_suite_path=bug_suite_path,
        generated_suite_path=generated_suite_path,
        output_path=combined_suite_path,
    )
    return {
        "bug_regressions": len(bug_suite["tests"]),
        "generated_variants": len(generated_payload["tests"]),
        "combined_evals": len(combined_payload["tests"]),
        "latest_prompt": candidate["prompt"],
    }
