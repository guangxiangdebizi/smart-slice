# coding=utf-8
"""Command line interface: ``python -m smart_slice`` / ``smart-slice``."""
import argparse
import json
import os
import sys
from typing import List, Optional, Sequence

from . import (
    DEFAULT_LIMIT,
    ChunkingOptions,
    __version__,
    detect_handler,
    missing_dependencies,
    slice_path,
    supported_extensions,
)
from .exceptions import SliceError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smart-slice",
        description="Fidelity-first document slicing for RAG pipelines.",
    )
    parser.add_argument("--version", action="version", version=f"smart-slice {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("slice", help="slice a file into paragraphs")
    run.add_argument("path", help="document to slice")
    run.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"max chars per paragraph (default {DEFAULT_LIMIT})")
    run.add_argument(
        "--overlap", type=int, default=None,
        help="characters of context shared by consecutive paragraphs (default 0 = none)",
    )
    run.add_argument(
        "--overlap-ratio", type=float, default=None,
        help="alternative to --overlap, as a fraction of --limit (e.g. 0.15 for 15%%)",
    )
    run.add_argument(
        "--overlap-section-only", action="store_true",
        help="carry context only between paragraphs in the same section",
    )
    run.add_argument("--no-filter", action="store_true", help="keep heading markers / blank runs verbatim")
    run.add_argument(
        "--format",
        choices=("json", "jsonl", "text", "md"),
        default="json",
        help="output encoding (default json)",
    )
    run.add_argument("--output", "-o", help="write to a file instead of stdout")
    run.add_argument("--title-prefix", action="store_true", help="prefix each paragraph with its title chain")
    run.add_argument("--stats", action="store_true", help="print a paragraph/character summary to stderr")

    chunk = sub.add_parser("chunk", help="slice, then chunk paragraphs for an embedding window")
    chunk.add_argument("path", help="document to slice and chunk")
    chunk.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="paragraph budget (default %(default)s)")
    chunk.add_argument("--chunk-size", type=int, default=256, help="embedding chunk size (default %(default)s)")
    chunk.add_argument("--chunk-overlap", type=int, default=None, help="chars shared by consecutive chunks")
    chunk.add_argument("--carry-title", action="store_true", help="prefix each chunk with its heading chain")
    chunk.add_argument("--output", "-o", help="write JSON lines to a file instead of stdout")
    chunk.add_argument("--stats", action="store_true", help="print a chunk summary to stderr")

    probe = sub.add_parser("detect", help="show which handler claims a file")
    probe.add_argument("path", help="document to inspect")

    info = sub.add_parser("formats", help="list supported extensions and missing optional deps")
    info.add_argument("--json", action="store_true", help="machine readable output")
    return parser


def _emit(rows: List[dict], fmt: str, output: Optional[str], title_prefix: bool) -> None:
    def encode(row: dict) -> str:
        content = row.get("content", "")
        if title_prefix and row.get("title"):
            content = f"# {row['title']}\n\n{content}"
        return content

    if fmt == "json":
        payload = json.dumps(rows, ensure_ascii=False, indent=2)
    elif fmt == "jsonl":
        payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    elif fmt == "text":
        payload = "\n\n".join(encode(row) for row in rows)
    else:  # md
        payload = "\n\n".join(
            (f"# {row.get('title')}\n\n" if row.get("title") else "") + row.get("content", "") for row in rows
        )

    if output:
        directory = os.path.dirname(os.path.abspath(output))
        os.makedirs(directory, exist_ok=True)
        with open(output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
    else:
        sys.stdout.write(payload + "\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "formats":
        extensions = supported_extensions()
        gaps = missing_dependencies()
        if args.json:
            sys.stdout.write(json.dumps({"handlers": extensions, "missing_optional": gaps}, ensure_ascii=False, indent=2) + "\n")
            return 0
        for handler, exts in extensions.items():
            sys.stdout.write(f"{handler:<22} {', '.join(exts)}\n")
        if gaps:
            sys.stdout.write("\nOptional extras not installed:\n")
            for gap in gaps:
                sys.stdout.write(f"  smart-slice[{gap['extra']}]  ->  {gap['requirement']}\n")
        return 0

    if args.command == "detect":
        with open(args.path, "rb") as handle:
            content = handle.read()
        handler = detect_handler(os.path.basename(args.path), content)
        sys.stdout.write((handler or "<no handler>") + "\n")
        return 0 if handler else 2

    if args.command == "chunk":
        from . import chunk_paragraphs

        try:
            paragraphs = slice_path(args.path, limit=args.limit)
        except SliceError as error:
            sys.stderr.write(f"smart-slice: {error.message} (code {error.code})\n")
            return 1
        except OSError as error:
            sys.stderr.write(f"smart-slice: cannot read {args.path}: {error}\n")
            return 1

        pieces = chunk_paragraphs(
            paragraphs,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
            carry_title=args.carry_title or None,
        )
        payload = "\n".join(json.dumps({"content": piece}, ensure_ascii=False) for piece in pieces)
        if args.output:
            directory = os.path.dirname(os.path.abspath(args.output))
            os.makedirs(directory, exist_ok=True)
            with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + ("\n" if pieces else ""))
        else:
            sys.stdout.write(payload + ("\n" if pieces else ""))
        if args.stats:
            characters = sum(len(piece) for piece in pieces)
            sys.stderr.write(
                f"smart-slice: {len(pieces)} chunks from {len(paragraphs)} paragraphs, "
                f"{characters} characters, chunk_size={args.chunk_size}, "
                f"chunk_overlap={args.chunk_overlap or 0}\n"
            )
        return 0

    # slice
    try:
        opts = ChunkingOptions(
            limit=args.limit,
            overlap=args.overlap,
            overlap_ratio=args.overlap_ratio,
            overlap_within_section=args.overlap_section_only,
        )
        result = slice_path(args.path, limit=args.limit, options=opts,
                            with_filter=not args.no_filter)
    except SliceError as error:
        sys.stderr.write(f"smart-slice: {error.message} (code {error.code})\n")
        return 1
    except OSError as error:
        sys.stderr.write(f"smart-slice: cannot read {args.path}: {error}\n")
        return 1

    rows = [row for row in result if isinstance(row, dict)]
    _emit(rows, args.format, args.output, args.title_prefix)
    if args.stats:
        characters = sum(len(row.get("content", "")) for row in rows)
        overlap = opts.effective_overlap
        sys.stderr.write(
            f"smart-slice: {len(rows)} paragraphs, {characters} characters, "
            f"limit={args.limit}, overlap={overlap}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())