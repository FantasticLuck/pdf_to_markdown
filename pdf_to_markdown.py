#!/usr/bin/env python3
"""Convert a PDF book to Markdown, plain text, or both.

The converter works in page chunks and writes a checkpoint after each chunk.
This keeps completed pages when a large or problematic PDF fails part-way
through conversion.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from getpass import getpass
from pathlib import Path
import shutil
import sys
import tempfile
import traceback
from typing import Any, Iterable

import pymupdf
import pymupdf4llm
from pymupdf4llm.helpers.document_layout import parse_document


STATE_VERSION = 1
DEFAULT_CHUNK_PAGES = 20


class ConversionIncomplete(RuntimeError):
    """Raised when one or more pages could not be extracted."""

    def __init__(self, pages: list[int], work_dir: Path):
        self.pages = pages
        self.work_dir = work_dir
        page_list = ", ".join(str(page) for page in pages[:20])
        if len(pages) > 20:
            page_list += ", ..."
        super().__init__(
            f"仍有 {len(pages)} 页未完成（页码：{page_list}）。"
            f"检查点已保存在：{work_dir}"
        )


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数。") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数。")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert every page of a PDF to Markdown, plain text, or both. "
            "The conversion is checkpointed by page chunk and resumes automatically."
        )
    )
    parser.add_argument("pdf", type=Path, help="source PDF file")
    parser.add_argument(
        "--format",
        choices=("md", "markdown", "txt", "text", "both"),
        default="md",
        help="output format (default: md)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help=(
            "output file path; with --format both, this is the base name "
            "for the .md and .txt files"
        ),
    )
    parser.add_argument(
        "--ocr-language",
        default="eng",
        help=(
            "OCR language hint (default: eng; for Chinese scans try "
            "chi_sim+eng when using a compatible OCR engine)"
        ),
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="disable OCR for scanned pages",
    )
    parser.add_argument(
        "--chunk-pages",
        type=positive_int,
        default=DEFAULT_CHUNK_PAGES,
        metavar="N",
        help=f"pages to process in one chunk (default: {DEFAULT_CHUNK_PAGES})",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="discard the checkpoint for this output and start from page 1",
    )
    return parser.parse_args()


def normalize_format(output_format: str) -> str:
    return {"markdown": "md", "text": "txt"}.get(output_format, output_format)


def get_output_paths(
    source: Path, output: Path | None, output_format: str
) -> list[tuple[str, Path]]:
    normalized_format = normalize_format(output_format)
    output = output.expanduser() if output else None

    if normalized_format == "both":
        base = output.with_suffix("") if output else source.with_suffix("")
        return [("md", base.with_suffix(".md")), ("txt", base.with_suffix(".txt"))]

    suffix = ".md" if normalized_format == "md" else ".txt"
    destination = output if output else source.with_suffix(suffix)
    return [(normalized_format, destination)]


def output_base(output_paths: list[tuple[str, Path]]) -> Path:
    return output_paths[0][1].with_suffix("")


def work_directory(output_paths: list[tuple[str, Path]]) -> Path:
    base = output_base(output_paths)
    return base.parent / f".{base.name}.pdf_to_markdown.work"


def set_hidden_directory(path: Path) -> None:
    """Mark the checkpoint directory hidden on Windows when possible."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)
    except Exception:
        # The dot-prefixed name still keeps the work directory out of the
        # normal output listing if the Windows attribute cannot be set.
        pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for block in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_fingerprint(source: Path) -> dict[str, Any]:
    stat = source.stat()
    return {
        "path": str(source),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(source),
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_text(value: Any) -> str:
    """Normalize converter output to text before passing it to file writers.

    PyMuPDF4LLM page chunks are normally dictionaries containing a string, but
    some versions/paths can return a list of text fragments for a page.
    Joining those fragments avoids a late ``TextIOWrapper.write(list)`` error.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if "text" in value:
            return as_text(value["text"])
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "\n".join(as_text(item).rstrip("\n") for item in value)
    return str(value)


def atomic_write_text(path: Path, content: Any) -> None:
    content = as_text(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary_path, path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            raise


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def append_error(
    work_dir: Path,
    *,
    stage: str,
    pages: Iterable[int],
    error: BaseException,
) -> None:
    error_traceback = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    payload = {
        "time": utc_now(),
        "stage": stage,
        "pages": [page + 1 for page in pages],
        "type": type(error).__name__,
        "message": str(error),
        "traceback": error_traceback,
    }
    error_path = work_dir / "errors.jsonl"
    error_path.parent.mkdir(parents=True, exist_ok=True)
    with error_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def open_document(
    source: Path, password: str | None = None
) -> tuple[pymupdf.Document, str | None]:
    try:
        document = pymupdf.open(source)
    except Exception as exc:
        raise RuntimeError(f"无法打开 PDF：{exc}") from exc

    if not document.needs_pass:
        return document, password

    if password is None:
        if not sys.stdin.isatty():
            document.close()
            raise RuntimeError("PDF 受密码保护；请在交互式终端中运行以输入密码。")
        password = getpass("请输入 PDF 密码：")
    if not document.authenticate(password):
        document.close()
        raise RuntimeError("PDF 密码错误。")
    return document, password


def page_file(work_dir: Path, output_format: str, page_number: int) -> Path:
    return (
        work_dir
        / "pages"
        / output_format
        / f"page-{page_number + 1:06d}.{output_format}"
    )


def page_files_exist(
    work_dir: Path, output_formats: list[str], page_number: int
) -> bool:
    return all(
        page_file(work_dir, output_format, page_number).is_file()
        for output_format in output_formats
    )


def new_page_state() -> dict[str, Any]:
    return {"status": "pending", "mode": None, "error": None}


def initial_state(
    *,
    source_info: dict[str, Any],
    page_count: int,
    output_paths: list[tuple[str, Path]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    output_formats = [output_format for output_format, _ in output_paths]
    return {
        "version": STATE_VERSION,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "source": source_info,
        "page_count": page_count,
        "formats": output_formats,
        "outputs": [str(destination) for _, destination in output_paths],
        "options": {
            "chunk_pages": args.chunk_pages,
            "ocr_language": args.ocr_language,
            "use_ocr": not args.no_ocr,
        },
        "pages": {str(page): new_page_state() for page in range(page_count)},
    }


def state_path(work_dir: Path) -> Path:
    return work_dir / "state.json"


def save_state(work_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    atomic_write_json(state_path(work_dir), state)


def state_matches(
    state: dict[str, Any],
    *,
    source_info: dict[str, Any],
    page_count: int,
    output_paths: list[tuple[str, Path]],
    args: argparse.Namespace,
) -> bool:
    expected_formats = [output_format for output_format, _ in output_paths]
    expected_outputs = [str(destination) for _, destination in output_paths]
    expected_options = {
        "chunk_pages": args.chunk_pages,
        "ocr_language": args.ocr_language,
        "use_ocr": not args.no_ocr,
    }
    return (
        state.get("version") == STATE_VERSION
        and state.get("source") == source_info
        and state.get("page_count") == page_count
        and state.get("formats") == expected_formats
        and state.get("outputs") == expected_outputs
        and state.get("options") == expected_options
    )


def load_or_create_state(
    *,
    work_dir: Path,
    source_info: dict[str, Any],
    page_count: int,
    output_paths: list[tuple[str, Path]],
    args: argparse.Namespace,
) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    set_hidden_directory(work_dir)
    checkpoint = state_path(work_dir)

    if checkpoint.is_file():
        try:
            state = json.loads(checkpoint.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"检查点文件无法读取：{checkpoint}。使用 --restart 重新开始。"
            ) from exc
        if not state_matches(
            state,
            source_info=source_info,
            page_count=page_count,
            output_paths=output_paths,
            args=args,
        ):
            raise RuntimeError(
                "现有检查点与当前 PDF、输出路径或转换参数不一致；"
                "如需重新开始，请使用 --restart。"
            )
        return state

    other_files = [path for path in work_dir.iterdir() if path.name != "errors.jsonl"]
    if other_files:
        raise RuntimeError(
            f"检查点目录缺少 state.json：{work_dir}。使用 --restart 重新开始。"
        )

    state = initial_state(
        source_info=source_info,
        page_count=page_count,
        output_paths=output_paths,
        args=args,
    )
    save_state(work_dir, state)
    return state


def reconcile_page_state(
    state: dict[str, Any], work_dir: Path, output_formats: list[str]
) -> None:
    """Recover page completion from fragment files after an interrupted write."""
    for page_number in range(state["page_count"]):
        entry = state["pages"].setdefault(str(page_number), new_page_state())
        if page_files_exist(work_dir, output_formats, page_number):
            if entry.get("status") != "done":
                entry["status"] = "done"
                entry["mode"] = entry.get("mode") or "normal"
                entry["error"] = None
        elif entry.get("status") == "done":
            entry["status"] = "pending"
            entry["error"] = "fragment file is missing"


def chunk_ranges(page_count: int, chunk_pages: int) -> Iterable[tuple[int, int]]:
    for start in range(0, page_count, chunk_pages):
        yield start, min(start + chunk_pages, page_count)


def parse_pages(
    document: pymupdf.Document,
    source: Path,
    pages: list[int],
    args: argparse.Namespace,
) -> Any:
    # parse_document is the layout path used by the pinned pymupdf4llm version.
    # Parsing a chunk once lets both Markdown and text serialization reuse it.
    return parse_document(
        document,
        filename=str(source),
        pages=pages,
        show_progress=False,
        force_text=True,
        use_ocr=not args.no_ocr,
        ocr_language=args.ocr_language,
    )


def serialize_parsed(
    parsed: Any,
    output_formats: list[str],
    expected_pages: int,
) -> tuple[dict[str, list[str]], dict[str, BaseException]]:
    outputs: dict[str, list[str]] = {}
    errors: dict[str, BaseException] = {}

    for output_format in output_formats:
        try:
            if output_format == "md":
                chunks = parsed.to_markdown(
                    page_chunks=True,
                    page_separators=True,
                    show_progress=False,
                )
            else:
                chunks = parsed.to_text(page_chunks=True, show_progress=False)
            if not isinstance(chunks, list) or len(chunks) != expected_pages:
                raise RuntimeError(
                    f"{output_format} serializer returned "
                    f"{len(chunks) if isinstance(chunks, list) else 'non-list'} "
                    f"chunks; expected {expected_pages}."
                )
            outputs[output_format] = [
                as_text(chunk.get("text", "") if isinstance(chunk, dict) else chunk)
                for chunk in chunks
            ]
        except Exception as exc:
            errors[output_format] = exc
    return outputs, errors


def raw_page_text(document: pymupdf.Document, page_number: int) -> str:
    page = document.load_page(page_number)
    text = page.get_text("text", sort=True)
    if text.strip():
        return text
    # An image-only page with no text usually means that OCR/layout extraction
    # failed. A truly blank page is still a valid empty page.
    if page.get_images(full=True):
        raise RuntimeError("普通文本提取为空；页面包含图片，可能需要 OCR。")
    return text


def markdown_fallback(text: str, page_number: int) -> str:
    body = text.rstrip()
    marker = f"<!-- page {page_number + 1}: plain-text fallback -->"
    if body:
        return (
            f"{marker}\n\n{body}\n\n"
            f"--- end of page {page_number + 1} ---\n\n"
        )
    return f"{marker}\n\n--- end of page {page_number + 1} ---\n\n"


def write_page_fragment(
    work_dir: Path, output_format: str, page_number: int, content: str
) -> None:
    atomic_write_text(page_file(work_dir, output_format, page_number), content)


def mark_page(
    state: dict[str, Any],
    page_number: int,
    *,
    work_dir: Path,
    output_formats: list[str],
    mode: str | None = None,
    error: BaseException | None = None,
) -> bool:
    entry = state["pages"].setdefault(str(page_number), new_page_state())
    if page_files_exist(work_dir, output_formats, page_number):
        entry["status"] = "done"
        if mode:
            entry["mode"] = mode
        elif not entry.get("mode"):
            entry["mode"] = "normal"
        entry["error"] = None
        return True

    entry["status"] = "failed" if error else "pending"
    entry["mode"] = mode
    entry["error"] = str(error) if error else None
    return False


def fallback_page(
    *,
    document: pymupdf.Document,
    source: Path,
    page_number: int,
    args: argparse.Namespace,
    output_formats: list[str],
    work_dir: Path,
    state: dict[str, Any],
) -> bool:
    """Try one page with layout parsing, then plain text extraction."""
    degraded = False
    available: dict[str, str] = {}
    serializer_errors: dict[str, BaseException] = {}

    try:
        parsed = parse_pages(document, source, [page_number], args)
        available, serializer_errors = serialize_parsed(parsed, output_formats, 1)
        for output_format, error in serializer_errors.items():
            append_error(
                work_dir,
                stage=f"page-{output_format}-serialize",
                pages=[page_number],
                error=error,
            )
    except Exception as exc:
        append_error(work_dir, stage="page-layout", pages=[page_number], error=exc)
        serializer_errors = {output_format: exc for output_format in output_formats}

    missing_formats = [
        output_format
        for output_format in output_formats
        if output_format not in available
    ]
    if missing_formats:
        try:
            text = raw_page_text(document, page_number)
            for output_format in missing_formats:
                available[output_format] = (
                    markdown_fallback(text, page_number)
                    if output_format == "md"
                    else text
                )
            degraded = True
        except Exception as exc:
            append_error(work_dir, stage="page-plain-text", pages=[page_number], error=exc)
            mark_page(
                state,
                page_number,
                work_dir=work_dir,
                output_formats=output_formats,
                error=exc,
            )
            return False

    for output_format, content in available.items():
        write_page_fragment(work_dir, output_format, page_number, content)

    return mark_page(
        state,
        page_number,
        work_dir=work_dir,
        output_formats=output_formats,
        mode="degraded" if degraded else "normal",
    )


def store_bulk_outputs(
    *,
    work_dir: Path,
    state: dict[str, Any],
    output_formats: list[str],
    pages: list[int],
    outputs: dict[str, list[str]],
) -> list[int]:
    missing_pages: list[int] = []
    for local_index, page_number in enumerate(pages):
        for output_format in output_formats:
            contents = outputs.get(output_format)
            if contents is not None:
                write_page_fragment(
                    work_dir,
                    output_format,
                    page_number,
                    contents[local_index],
                )
        if not mark_page(
            state,
            page_number,
            work_dir=work_dir,
            output_formats=output_formats,
            mode="normal",
        ):
            missing_pages.append(page_number)
    return missing_pages


def merge_outputs(
    *,
    work_dir: Path,
    output_paths: list[tuple[str, Path]],
    page_count: int,
) -> None:
    for output_format, destination in output_paths:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
            text=True,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(
                descriptor, "w", encoding="utf-8", newline="\n"
            ) as output_file:
                for page_number in range(page_count):
                    fragment = page_file(work_dir, output_format, page_number)
                    content = fragment.read_text(encoding="utf-8")
                    if page_number:
                        output_file.write("\n\n")
                    output_file.write(content.rstrip("\n"))
                    output_file.write("\n")
            os.replace(temporary_path, destination)
        except Exception:
            try:
                temporary_path.unlink(missing_ok=True)
            finally:
                raise


def count_page_modes(state: dict[str, Any]) -> tuple[int, int]:
    degraded = sum(
        1
        for entry in state["pages"].values()
        if entry.get("status") == "done" and entry.get("mode") == "degraded"
    )
    failed = sum(
        1 for entry in state["pages"].values() if entry.get("status") != "done"
    )
    return degraded, failed


def convert(
    source: Path,
    output_paths: list[tuple[str, Path]],
    args: argparse.Namespace,
) -> None:
    work_dir = work_directory(output_paths)
    if args.restart and work_dir.exists():
        shutil.rmtree(work_dir)

    source_info = source_fingerprint(source)
    document, password = open_document(source)
    try:
        output_formats = [output_format for output_format, _ in output_paths]
        state = load_or_create_state(
            work_dir=work_dir,
            source_info=source_info,
            page_count=document.page_count,
            output_paths=output_paths,
            args=args,
        )
        reconcile_page_state(state, work_dir, output_formats)
        save_state(work_dir, state)

        resumed_pages = sum(
            1 for entry in state["pages"].values() if entry.get("status") == "done"
        )
        if resumed_pages:
            print(f"已恢复 {resumed_pages}/{document.page_count} 页的已完成内容。")

        for start, end in chunk_ranges(document.page_count, args.chunk_pages):
            pending_pages = [
                page_number
                for page_number in range(start, end)
                if not page_files_exist(work_dir, output_formats, page_number)
            ]
            if not pending_pages:
                continue

            print(
                f"正在处理第 {start + 1}-{end}/{document.page_count} 页 "
                f"（本次待处理 {len(pending_pages)} 页）…"
            )

            # A clean chunk gets one layout pass. If an earlier interruption
            # left partial page fragments, retry only the missing pages instead
            # of parsing the whole chunk again.
            has_partial_fragments = any(
                page_file(work_dir, output_format, page_number).exists()
                for page_number in pending_pages
                for output_format in output_formats
            )
            missing_after_bulk: list[int] = []
            if not has_partial_fragments and len(pending_pages) == end - start:
                try:
                    parsed = parse_pages(document, source, pending_pages, args)
                    outputs, serializer_errors = serialize_parsed(
                        parsed, output_formats, len(pending_pages)
                    )
                    for output_format, error in serializer_errors.items():
                        append_error(
                            work_dir,
                            stage=f"chunk-{output_format}-serialize",
                            pages=pending_pages,
                            error=error,
                        )
                    missing_after_bulk = store_bulk_outputs(
                        work_dir=work_dir,
                        state=state,
                        output_formats=output_formats,
                        pages=pending_pages,
                        outputs=outputs,
                    )
                except Exception as exc:
                    append_error(
                        work_dir, stage="chunk-layout", pages=pending_pages, error=exc
                    )
                    missing_after_bulk = pending_pages
                    # A failed layout pass can mutate page state while doing
                    # OCR. Reopen the PDF before isolated page retries.
                    document.close()
                    document, password = open_document(source, password)
            else:
                missing_after_bulk = pending_pages

            if missing_after_bulk:
                # Isolated retries are deliberately sequential: OCR/layout
                # uses process-global model state and this keeps memory bounded.
                for page_number in missing_after_bulk:
                    if page_files_exist(work_dir, output_formats, page_number):
                        continue
                    fallback_page(
                        document=document,
                        source=source,
                        page_number=page_number,
                        args=args,
                        output_formats=output_formats,
                        work_dir=work_dir,
                        state=state,
                    )

            save_state(work_dir, state)

        unresolved = [
            page_number
            for page_number in range(document.page_count)
            if not page_files_exist(work_dir, output_formats, page_number)
        ]
        if unresolved:
            save_state(work_dir, state)
            raise ConversionIncomplete(unresolved, work_dir)

        for _, destination in output_paths:
            destination.parent.mkdir(parents=True, exist_ok=True)
        merge_outputs(
            work_dir=work_dir,
            output_paths=output_paths,
            page_count=document.page_count,
        )

        degraded, _ = count_page_modes(state)
        for _, destination in output_paths:
            print(f"已保存：{destination}")
        if degraded:
            print(f"警告：{degraded} 页使用了普通文本降级结果。")
        try:
            shutil.rmtree(work_dir)
        except OSError as exc:
            print(f"提示：最终文件已生成，但临时检查点未能删除：{exc}")
    finally:
        document.close()


def main() -> int:
    args = parse_args()
    source = args.pdf.expanduser().resolve()
    if not source.is_file():
        print(f"找不到 PDF 文件：{source}", file=sys.stderr)
        return 2
    if source.suffix.lower() != ".pdf":
        print("输入文件必须是 .pdf。", file=sys.stderr)
        return 2

    output_paths = [
        (output_format, destination.expanduser().resolve())
        for output_format, destination in get_output_paths(
            source, args.output, args.format
        )
    ]
    for _, destination in output_paths:
        if destination == source:
            print("输出路径不能与输入 PDF 相同。", file=sys.stderr)
            return 2

    try:
        convert(source, output_paths, args)
    except ConversionIncomplete as exc:
        print(f"转换暂未完成：{exc}", file=sys.stderr)
        print(
            "下次使用相同命令即可自动继续；如需重来请加 --restart。",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("转换已中断；已写入的页面会在下次运行时自动保留。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"转换失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
