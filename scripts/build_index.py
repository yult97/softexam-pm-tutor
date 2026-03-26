#!/usr/bin/env python3
"""从教材 PDF 提取文本并构建 JSONL 搜索索引。"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

SCRIPT_DIR: Path = Path(__file__).resolve().parent
VENDOR_DIR: Path = SCRIPT_DIR / "_vendor"
if VENDOR_DIR.exists() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

from pypdf import PdfReader  # noqa: E402  # _vendor 动态路径

DEFAULT_PDF: Path = SCRIPT_DIR.parent / "assets" / "信息系统项目管理师教程(可搜索版).pdf"
DEFAULT_OUTPUT: Path = SCRIPT_DIR.parent / "references" / "book_chunks.jsonl"
DEFAULT_TOC_OUTPUT: Path = SCRIPT_DIR.parent / "references" / "toc.md"
CHAPTER_PATTERN = re.compile(r"第(\d+)章([^\n]+)")


# ── CLI 参数 ──────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="从教材 PDF 提取文本并构建 JSONL 搜索索引。",
    )
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF, help="源 PDF 路径。")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="输出 JSONL 索引路径。")
    parser.add_argument("--toc-output", type=Path, default=DEFAULT_TOC_OUTPUT, help="输出目录 Markdown 路径。")
    parser.add_argument("--chunk-size", type=int, default=900, help="每块最大字符数。")
    parser.add_argument("--overlap", type=int, default=150, help="块之间重叠字符数。")
    parser.add_argument("--toc-start-page", type=int, default=2, help="目录起始页码（1-based）。")
    parser.add_argument("--toc-page-count", type=int, default=4, help="提取目录的页数。")
    return parser.parse_args()


# ── 文本清洗 ──────────────────────────────────────────────────────────────────


def clean_text(raw: str) -> str:
    """清洗 PDF 提取的原始文本，保留段落结构。"""
    text = raw.replace("\x00", "")
    cleaned: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            # 保留单个空行作为段落分隔
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        # 合并连续空白
        stripped = re.sub(r"\s+", " ", stripped)
        # 去除中文字符间的多余空格
        stripped = re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])", "", stripped)
        cleaned.append(stripped)

    result = "\n".join(cleaned)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


# ── 分块 ─────────────────────────────────────────────────────────────────────


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """将文本按字符数分成重叠的块。"""
    text_len = len(text)
    if text_len <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0

    while start < text_len:
        end = min(start + chunk_size, text_len)

        # 尽量在换行处断开
        if end < text_len:
            cut_point = text.rfind("\n", start, end)
            threshold = start + int(chunk_size * 0.6)
            if cut_point > threshold:
                end = cut_point

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= text_len:
            break

        start = max(end - overlap, start + 1)

    return chunks


def detect_page_type(page_number: int, text: str, toc_start_page: int, toc_end_page: int) -> str:
    """根据页内容判断页类型。"""
    compact = re.sub(r"\s+", " ", text)
    if toc_start_page <= page_number <= toc_end_page and "目录" in compact:
        return "toc"
    if "目录" in compact and len(re.findall(r"第\d+章", compact)) >= 2:
        return "toc"
    if "参考文献" in compact[:80] or len(re.findall(r"\[\d+\]", compact)) >= 5:
        return "reference"
    if "本章练习" in compact or ("选择题" in compact and "参考答案" in compact):
        return "exercise"
    return "body"


def extract_chapter_meta(
    text: str,
    current_number: Optional[int],
    current_title: Optional[str],
) -> tuple[Optional[int], Optional[str]]:
    """从页面文本中提取当前章节信息。"""
    match = CHAPTER_PATTERN.search(text)
    if not match:
        return current_number, current_title

    chapter_number = int(match.group(1))
    chapter_title = re.sub(r"\s+", "", match.group(2)).strip("：: ")
    return chapter_number, chapter_title


# ── PDF 页面提取 ──────────────────────────────────────────────────────────────


def extract_pages(reader: PdfReader) -> list[tuple[int, str]]:
    """提取并清洗每一页的文本，返回 (页码, 清洗文本) 列表。"""
    pages: list[tuple[int, str]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        raw = page.extract_text() or ""
        cleaned = clean_text(raw)
        pages.append((page_number, cleaned))
    return pages


# ── 目录输出 ──────────────────────────────────────────────────────────────────


def write_toc(
    toc_output: Path,
    toc_pages: list[tuple[int, str]],
    source_pdf: Path,
) -> None:
    """将提取的目录页写入 Markdown 文件。"""
    toc_output.parent.mkdir(parents=True, exist_ok=True)

    page_nums = ", ".join(str(p) for p, _ in toc_pages)
    lines: list[str] = [
        "# 教材目录摘录",
        "",
        f"- 来源 PDF: `{source_pdf}`",
        f"- 提取页码: {page_nums}",
        "",
    ]
    for page_number, text in toc_pages:
        body = text if text else "（该页未提取到文本）"
        lines.extend([f"## PDF 第 {page_number} 页", "", body, ""])

    toc_output.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


# ── 索引构建 ──────────────────────────────────────────────────────────────────


def build_index(
    pdf_path: Path,
    output_path: Path,
    toc_output: Path,
    chunk_size: int,
    overlap: int,
    toc_start_page: int,
    toc_page_count: int,
) -> dict[str, int]:
    """构建 JSONL 索引并输出目录摘录，返回统计信息。"""
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF 文件未找到: {pdf_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(pdf_path))

    toc_end_page = toc_start_page + toc_page_count - 1
    toc_pages: list[tuple[int, str]] = []
    total_chunks = 0
    pages_with_text = 0
    current_chapter_number: Optional[int] = None
    current_chapter_title: Optional[str] = None

    all_pages = extract_pages(reader)

    with output_path.open("w", encoding="utf-8") as out:
        for page_number, cleaned in all_pages:
            if toc_start_page <= page_number <= toc_end_page:
                toc_pages.append((page_number, cleaned))

            if not cleaned:
                continue

            pages_with_text += 1
            current_chapter_number, current_chapter_title = extract_chapter_meta(
                cleaned,
                current_chapter_number,
                current_chapter_title,
            )
            page_type = detect_page_type(
                page_number=page_number,
                text=cleaned,
                toc_start_page=toc_start_page,
                toc_end_page=toc_end_page,
            )
            page_chunks = chunk_text(cleaned, chunk_size=chunk_size, overlap=overlap)

            for chunk_idx, chunk in enumerate(page_chunks):
                record = {
                    "id": f"p{page_number:04d}-c{chunk_idx:03d}",
                    "page": page_number,
                    "chunk_index": chunk_idx,
                    "source_pdf": str(pdf_path),
                    "text": chunk,
                    "page_type": page_type,
                    "chapter_number": current_chapter_number,
                    "chapter_title": current_chapter_title,
                }
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                total_chunks += 1

    write_toc(toc_output=toc_output, toc_pages=toc_pages, source_pdf=pdf_path)

    return {
        "pages": len(reader.pages),
        "pages_with_text": pages_with_text,
        "chunks": total_chunks,
    }


# ── 入口 ─────────────────────────────────────────────────────────────────────


def main() -> int:
    """执行索引构建命令。"""
    args = parse_args()

    try:
        stats = build_index(
            pdf_path=args.pdf,
            output_path=args.output,
            toc_output=args.toc_output,
            chunk_size=args.chunk_size,
            overlap=args.overlap,
            toc_start_page=args.toc_start_page,
            toc_page_count=args.toc_page_count,
        )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "success": True,
                "pdf": str(args.pdf),
                "output": str(args.output),
                "toc_output": str(args.toc_output),
                **stats,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
