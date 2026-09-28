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
    SchedulerPolicy,
    __version__,
    detect_handler,
    missing_dependencies,
    slice_many,
    slice_path,
    supported_extensions,
)
from .exceptions import SliceError
from .scheduler import available_cores, auto_concurrency, default_policy, resolve_concurrency


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

    batch = sub.add_parser(
        "batch",
        help="slice many documents under a scheduling policy (concurrency / core allocation)",
    )
    batch.add_argument("paths", nargs="+", help="documents to slice")
    batch.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"max chars per paragraph (default {DEFAULT_LIMIT})")
    batch.add_argument("--overlap", type=int, default=None, help="chars of context shared by consecutive paragraphs")
    batch.add_argument("--overlap-ratio", type=float, default=None, help="alternative to --overlap, as a fraction of --limit")
    batch.add_argument("--no-filter", action="store_true", help="keep heading markers / blank runs verbatim")
    batch.add_argument(
        "-j", "--concurrency", default=None,
        help='batch width: an integer, or "auto" (default), "cores", "serial", or a multiple like "2x"; '
             "when omitted, SMART_SLICE_CONCURRENCY is consulted first",
    )
    batch.add_argument(
        "--backend", choices=("thread", "process", "serial"), default=None,
        help="worker backend (default: thread; process bypasses the GIL but needs picklable input)",
    )
    batch.add_argument(
        "--max-concurrency", type=int, default=None,
        help="hard ceiling for a derived width (default: SMART_SLICE_MAX_CONCURRENCY or 32)",
    )
    batch.add_argument("--pin-cores", action="store_true", help="allocate one CPU core per task, round-robin")
    batch.add_argument(
        "--error-policy", choices=("raise_first", "collect"), default="raise_first",
        help="raise_first: fail the batch with the lowest-index error (default); collect: report every document",
    )
    batch.add_argument("--unordered", action="store_true", help="emit results in completion order")
    batch.add_argument("--timeout", type=float, default=None, help="per-document wall-clock seconds")
    batch.add_argument(
        "--format", choices=("json", "jsonl", "text", "summary"), default="jsonl",
        help="output encoding (default jsonl: one JSON object per document)",
    )
    batch.add_argument("--output", "-o", help="write to a file instead of stdout")
    batch.add_argument("--stats", action="store_true", help="print the scheduling report to stderr")

    cores = sub.add_parser("cores", help="show detected cores and the derived batch width")
    cores.add_argument("--json", action="store_true", help="machine readable output")

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

    if args.command == "cores":
        mask = _affinity_mask()
        policy = default_policy()
        info = {
            "available_cores": available_cores(),
            "auto_concurrency": auto_concurrency(),
            "affinity_mask": mask,
            "pin_cores_supported": bool(mask),
            "env_concurrency": os.environ.get("SMART_SLICE_CONCURRENCY"),
            "env_backend": os.environ.get("SMART_SLICE_SCHEDULER_BACKEND"),
            "env_pin_cores": os.environ.get("SMART_SLICE_PIN_CORES"),
            "default_backend": policy.backend,
            "max_concurrency": resolve_concurrency("cores", maximum=None),
        }
        if args.json:
            sys.stdout.write(json.dumps(info, ensure_ascii=False, indent=2) + "\n")
        else:
            for key, value in info.items():
                sys.stdout.write(f"{key:<20} {value}\n")
        return 0

    if args.command == "batch":
        return _run_batch(args)

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


def _affinity_mask() -> List[int]:
    from .scheduler import _affinity_mask as mask_of

    return mask_of()


def _run_batch(args) -> int:
    """Execute the ``batch`` subcommand: slice every path under one policy."""
    try:
        policy = SchedulerPolicy(
            concurrency=args.concurrency,
            backend=args.backend or default_policy().backend,
            max_concurrency=args.max_concurrency,
            pin_cores=args.pin_cores or default_policy().pin_cores,
            error_policy=args.error_policy,
            ordered=not args.unordered,
            timeout=args.timeout,
        )
    except ValueError as error:
        sys.stderr.write(f"smart-slice: {error}\n")
        return 2

    slice_kwargs = dict(
        limit=args.limit,
        overlap=args.overlap,
        overlap_ratio=args.overlap_ratio,
        with_filter=not args.no_filter,
    )
    missing = [path for path in args.paths if not os.path.exists(path)]
    if missing and args.error_policy == "raise_first":
        sys.stderr.write(f"smart-slice: cannot read {missing[0]}\n")
        return 1

    # The CLI always collects: a batch tool has to be able to name the document
    # that failed, which an exception raised out of the scheduler cannot do.
    # The user-facing --error-policy then decides how much of that is reported.
    report = slice_many(args.paths, policy=policy.with_(error_policy="collect"), **slice_kwargs)

    rows = []
    for outcome in sorted(report.outcomes, key=lambda item: item.index):
        if outcome.ok:
            rows.append({"name": outcome.name, "ok": True, "paragraphs": outcome.value})
        else:
            error = outcome.error
            rows.append({
                "name": outcome.name,
                "ok": False,
                "index": outcome.index,
                "error": {
                    "type": type(error).__name__,
                    "code": getattr(error, "code", None),
                    "message": str(error),
                },
            })

    if args.format == "json":
        payload = json.dumps(
            {"report": {"summary": report.summary(), "width": report.width,
                        "elapsed_ms": round(report.elapsed * 1000, 1)},
             "results": rows},
            ensure_ascii=False, indent=2,
        )
    elif args.format == "jsonl":
        payload = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    elif args.format == "summary":
        payload = "\n".join(
            f"{'ok ' if row['ok'] else 'ERR'} {row['name']}"
            + ("" if row["ok"] else f"  {row['error']['message']}")
            for row in rows
        )
    else:  # text
        chunks = []
        for row in rows:
            if not row["ok"]:
                continue
            for paragraph in row["paragraphs"] or []:
                title = paragraph.get("title") if isinstance(paragraph, dict) else ""
                body = paragraph.get("content", "") if isinstance(paragraph, dict) else str(paragraph)
                chunks.append((f"# {title}\n\n" if title else "") + body)
        payload = "\n\n".join(chunks)

    if args.output:
        directory = os.path.dirname(os.path.abspath(args.output))
        os.makedirs(directory, exist_ok=True)
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload + "\n")
    else:
        sys.stdout.write(payload + "\n")

    if args.stats:
        sys.stderr.write(f"smart-slice: {report.summary()}\n")

    failures = sorted(report.failures, key=lambda outcome: outcome.index)
    if not failures:
        return 0

    def describe(outcome) -> str:
        error = outcome.error
        code = getattr(error, "code", None)
        suffix = f" (code {code})" if code is not None else ""
        return f"smart-slice: {outcome.name}: {error}{suffix}"

    if args.error_policy == "raise_first":
        # whole-batch failure semantics: report the lowest-index failure, and
        # say how many others were hit so nothing looks silently dropped
        sys.stderr.write(describe(failures[0]) + "\n")
        if len(failures) > 1:
            sys.stderr.write(
                f"smart-slice: {len(failures) - 1} more document(s) failed "
                f"(--error-policy collect to list them all)\n"
            )
    else:
        for outcome in failures:
            sys.stderr.write(describe(outcome) + "\n")
        sys.stderr.write(f"smart-slice: {len(failures)} of {len(report)} documents failed\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
