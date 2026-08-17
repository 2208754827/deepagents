#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
需求文档预处理器

将 requirements/ 目录中的 Word / PDF 源文件转换为同名 Markdown。
默认采用幂等策略：如果对应的 .md 已存在，则跳过转换，不覆盖已有内容。

支持：
- .docx：优先使用 mammoth 转 HTML → markdownify 转 Markdown（标题/表格识别准确）；
          mammoth 不可用时 fallback 到手写 XML 解析
- .doc：通过 LibreOffice / soffice 另存为临时 docx 后再解析（可选）
- .pdf：优先使用已安装的 PDF 解析库提取文本
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable, List, Optional
from xml.etree import ElementTree as ET

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
}
SUPPORTED_EXTENSIONS = {".doc", ".docx", ".pdf"}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


# ============================================================
# mammoth + markdownify 路径（优先）
# ============================================================

def _docx_to_markdown_mammoth(docx_path: Path) -> str:
    """使用 mammoth 转 HTML，再 markdownify 转 Markdown。

    优势：正确识别自定义 styleId 的标题（如 styleId="1" → name="heading 1"），
    表格自动渲染为 Markdown 管道表格。
    """
    import mammoth
    import markdownify

    with open(str(docx_path), "rb") as f:
        result = mammoth.convert_to_html(f)

    html = result.value

    # 去掉 base64 内嵌图片（需求文档的 UI 原型截图，测试用例不需要；避免 12MB+ 体积）
    html = re.sub(r"<img[^>]*>", "", html)

    # HTML → Markdown（ATX 风格标题，去掉 <a> 锚点标签）
    md = markdownify.markdownify(html, heading_style="ATX", strip=["a"])

    return md.strip() + "\n"


# ============================================================
# 手写 XML 解析路径（fallback）
# ============================================================

def _get_paragraph_style(paragraph: ET.Element) -> str:
    style = paragraph.find("w:pPr/w:pStyle", NS)
    if style is None:
        return ""
    return str(style.attrib.get(f"{{{NS['w']}}}val", "") or "")


def _get_paragraph_indent(paragraph: ET.Element) -> int:
    num_pr = paragraph.find("w:pPr/w:numPr", NS)
    if num_pr is None:
        return 0
    ilvl = num_pr.find("w:ilvl", NS)
    if ilvl is None:
        return 0
    raw = ilvl.attrib.get(f"{{{NS['w']}}}val", "0")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _is_list_paragraph(paragraph: ET.Element) -> bool:
    if paragraph.find("w:pPr/w:numPr", NS) is not None:
        return True
    style = _get_paragraph_style(paragraph).lower()
    return "list" in style or "bullet" in style


def _extract_paragraph_text(paragraph: ET.Element) -> str:
    parts: List[str] = []
    for node in paragraph.iter():
        if node is paragraph:
            continue
        name = _local_name(node.tag)
        if name == "t" and node.text:
            parts.append(node.text)
        elif name == "tab":
            parts.append("\t")
        elif name in {"br", "cr"}:
            parts.append("\n")
    text = "".join(parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def _heading_prefix(style: str) -> str:
    if not style:
        return ""
    normalized = style.replace(" ", "").lower()
    if normalized == "title":
        return "#"
    match = re.search(r"heading(\d)", normalized)
    if match:
        level = max(1, min(6, int(match.group(1))))
        return "#" * level
    return ""


def _escape_markdown_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>").strip()


def _render_table(rows: List[List[str]]) -> List[str]:
    if not rows:
        return []

    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    lines = [
        "| " + " | ".join(_escape_markdown_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(_escape_markdown_cell(cell) for cell in row) + " |")
    return lines


def _extract_table_rows(table: ET.Element) -> List[List[str]]:
    rows: List[List[str]] = []
    for tr in table.findall("w:tr", NS):
        row: List[str] = []
        for tc in tr.findall("w:tc", NS):
            cell_paragraphs = []
            for para in tc.findall("w:p", NS):
                text = _extract_paragraph_text(para)
                if text:
                    cell_paragraphs.append(text)
            row.append("\n".join(cell_paragraphs).strip())
        rows.append(row)
    return rows


def _docx_to_markdown_xml(docx_path: Path) -> str:
    """手写 XML 解析：直接解压 docx 读取 document.xml。

    已知缺陷：只识别 styleId 包含 "Heading" 的标题样式，
    无法识别 styleId 为纯数字（如 "1" → name="heading 1"）的自定义样式。
    保留作 mammoth 不可用时的 fallback。
    """
    with zipfile.ZipFile(docx_path, "r") as zf:
        try:
            document_xml = zf.read("word/document.xml")
        except KeyError as exc:
            raise ValueError(f"{docx_path.name} 不是有效的 DOCX 文件") from exc

    root = ET.fromstring(document_xml)
    body = root.find("w:body", NS)
    if body is None:
        raise ValueError(f"{docx_path.name} 中未找到正文内容")

    lines: List[str] = []
    for child in body:
        name = _local_name(child.tag)
        if name == "p":
            text = _extract_paragraph_text(child)
            if not text:
                if lines and lines[-1] != "":
                    lines.append("")
                continue

            style = _get_paragraph_style(child)
            prefix = _heading_prefix(style)
            if prefix:
                lines.append(f"{prefix} {text}")
            elif _is_list_paragraph(child):
                indent = "  " * _get_paragraph_indent(child)
                lines.append(f"{indent}- {text}")
            else:
                lines.append(text)
        elif name == "tbl":
            if lines and lines[-1] != "":
                lines.append("")
            rows = _extract_table_rows(child)
            lines.extend(_render_table(rows))
        elif name in {"sdt", "altChunk"}:
            continue
        else:
            continue

    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines).strip() + ("\n" if lines else "")


# ============================================================
# 统一入口：优先 mammoth，fallback 手写 XML
# ============================================================

def _docx_to_markdown(docx_path: Path) -> str:
    """将 .docx 转为 Markdown。

    优先使用 mammoth + markdownify（标题识别准确，支持自定义 styleId）；
    mammoth 不可用时 fallback 到手写 XML 解析。
    """
    try:
        return _docx_to_markdown_mammoth(docx_path)
    except ImportError:
        pass
    except Exception as exc:
        print(f"  [警告] mammoth 转换失败，回退到 XML 解析: {exc}", file=sys.stderr)

    return _docx_to_markdown_xml(docx_path)


def _doc_to_docx(doc_path: Path) -> Path:
    """
    将旧版 .doc 转换为临时 .docx。

    依赖 LibreOffice / soffice。转换后的文件会保留在调用方提供的临时目录中。
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError(
            f"无法转换 {doc_path.name}：系统未找到 LibreOffice/soffice，请先另存为 .docx 或安装 LibreOffice"
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="doc_to_docx_"))
    cmd = [soffice, "--headless", "--convert-to", "docx", "--outdir", str(tmp_dir), str(doc_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"LibreOffice 转换 {doc_path.name} 失败: {stderr or '未知错误'}")

    produced = tmp_dir / f"{doc_path.stem}.docx"
    if not produced.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"LibreOffice 未生成预期的 DOCX 文件: {produced}")

    return produced


def _doc_to_markdown(doc_path: Path) -> str:
    produced = _doc_to_docx(doc_path)
    try:
        return _docx_to_markdown(produced)
    finally:
        # 仅清理临时转换目录；若失败则保留路径信息便于排查
        shutil.rmtree(produced.parent, ignore_errors=True)


def _extract_text_with_pdf_library(pdf_path: Path) -> Optional[str]:
    # pypdf / PyPDF2
    for module_name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(module_name, fromlist=["PdfReader"])
            reader = module.PdfReader(str(pdf_path))
            pages = []
            for page in reader.pages:
                pages.append(page.extract_text() or "")
            text = "\n\n".join(page.strip() for page in pages if page.strip()).strip()
            if text:
                return text
        except Exception:
            pass

    # pdfplumber
    try:
        import pdfplumber  # type: ignore

        pages = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
        text = "\n\n".join(page.strip() for page in pages if page.strip()).strip()
        if text:
            return text
    except Exception:
        pass

    # PyMuPDF
    try:
        import fitz  # type: ignore

        doc = fitz.open(str(pdf_path))
        pages = []
        for page in doc:
            pages.append(page.get_text("text") or "")
        text = "\n\n".join(page.strip() for page in pages if page.strip()).strip()
        if text:
            return text
    except Exception:
        pass

    # pdfminer.six
    try:
        from pdfminer.high_level import extract_text  # type: ignore

        text = (extract_text(str(pdf_path)) or "").strip()
        if text:
            return text
    except Exception:
        pass

    return None


def _pdf_to_markdown(pdf_path: Path) -> str:
    text = _extract_text_with_pdf_library(pdf_path)
    if not text:
        raise RuntimeError(
            f"无法从 {pdf_path.name} 提取可用文本。请确认 PDF 不是纯图片扫描件，或先做 OCR 后再转换。"
        )

    paragraphs = [segment.strip() for segment in re.split(r"\n{2,}", text) if segment.strip()]
    return "\n\n".join(paragraphs).strip() + "\n"


def _convert_source_to_markdown(source_path: Path) -> str:
    suffix = source_path.suffix.lower()
    if suffix == ".docx":
        return _docx_to_markdown(source_path)
    if suffix == ".doc":
        docx_path = _doc_to_docx(source_path)
        return _docx_to_markdown(docx_path)
    if suffix == ".pdf":
        return _pdf_to_markdown(source_path)
    raise ValueError(f"不支持的文件类型: {source_path.suffix}")


def _iter_source_files(root: Path, recursive: bool = False) -> Iterable[Path]:
    iterator = root.rglob("*") if recursive else root.iterdir()
    for path in iterator:
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            yield path


def _write_text_atomic(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.stem + ".", suffix=".tmp", dir=str(target.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(target)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def preprocess_requirements(root: Path, recursive: bool = False, force: bool = False, dry_run: bool = False) -> dict:
    converted: List[Path] = []
    skipped: List[Path] = []
    failed: List[str] = []

    if not root.exists():
        raise FileNotFoundError(f"目录不存在: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"不是目录: {root}")

    for source in sorted(_iter_source_files(root, recursive=recursive), key=lambda p: str(p).lower()):
        target = source.with_suffix(".md")
        if target.exists() and not force:
            skipped.append(source)
            print(f"已跳过: {source.name} -> {target.name}（对应 Markdown 已存在）")
            continue

        try:
            markdown = _convert_source_to_markdown(source)
            if dry_run:
                converted.append(source)
                continue
            _write_text_atomic(target, markdown)
            converted.append(source)
            print(f"已转换: {source.name} -> {target.name}")
        except Exception as exc:
            failed.append(f"{source.name}: {exc}")
            print(f"转换失败: {source.name} -> {exc}", file=sys.stderr)

    summary = {
        "converted": len(converted),
        "skipped": len(skipped),
        "failed": len(failed),
        "converted_files": [str(path) for path in converted],
        "skipped_files": [str(path) for path in skipped],
        "failed_files": failed,
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="将 requirements/ 中的 Word / PDF 文件转换为 Markdown")
    parser.add_argument("--root", default="requirements", help="需求目录（默认：requirements）")
    parser.add_argument("--recursive", action="store_true", help="递归扫描子目录")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的同名 Markdown")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不写入文件")
    args = parser.parse_args()

    root = Path(args.root)
    try:
        summary = preprocess_requirements(root, recursive=args.recursive, force=args.force, dry_run=args.dry_run)
        print(
            f"完成：转换 {summary['converted']} 个，跳过 {summary['skipped']} 个，失败 {summary['failed']} 个"
        )
        if summary["failed"]:
            sys.exit(1)
    except Exception as exc:
        print(f"预处理失败: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
