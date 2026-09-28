# coding=utf-8
"""Batch slicing under a scheduling policy.

    python examples/batch_scheduling.py [directory]

Builds a small corpus (or uses the directory you point it at), then slices it
several ways - serial, threads, pinned threads, processes - so the scheduling
knobs and the difference between backends are visible side by side.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smart_slice import (
    SchedulerPolicy,
    SliceJob,
    auto_concurrency,
    available_cores,
    slice_many,
    slice_paths,
)


def build_corpus(directory, documents=8):
    """Markdown files with headings, a table and prose."""
    paths = []
    for index in range(documents):
        path = os.path.join(directory, f"doc{index:02d}.md")
        parts = [f"# Document {index}", ""]
        for section in range(4):
            parts += [f"## Section {index}.{section}", "", "| key | value |", "| --- | --- |"]
            parts += [f"| k{row} | {row * index} |" for row in range(6)]
            parts += ["", ("paragraph body text " * 30).strip(), ""]
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(parts))
        paths.append(path)
    return paths


def timed(label, call):
    start = time.perf_counter()
    report = call()
    elapsed = time.perf_counter() - start
    print(f"{label:<26} {elapsed * 1000:8.1f} ms   {report.summary()}")
    return report


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    if args:
        directory = args[0]
        paths = sorted(
            str(item) for item in Path(directory).rglob("*") if item.is_file()
        )
    else:
        directory = tempfile.mkdtemp(prefix="smart-slice-batch-")
        paths = build_corpus(directory)

    print(f"corpus: {len(paths)} documents from {directory}")
    print(f"machine: {available_cores()} usable cores -> auto width {auto_concurrency()}")
    print()

    serial = timed("serial", lambda: slice_paths(paths, backend="serial", limit=800))
    threaded = timed("threads (auto width)", lambda: slice_paths(paths, limit=800))
    threaded4 = timed("threads x4", lambda: slice_paths(paths, concurrency=4, limit=800))
    pinned = timed("threads x4 + pin_cores", lambda: slice_paths(
        paths, concurrency=4, pin_cores=True, limit=800))
    try:
        processed = timed("processes x4", lambda: slice_paths(
            paths, backend="process", concurrency=4, limit=800))
    except OSError as error:
        processed = None
        print(f"{'processes x4':<26} unavailable: {error}")

    # Every backend must produce identical output: the scheduler decides *when* a
    # document is sliced, never *how*.
    for label, report in (("threads", threaded), ("threads x4", threaded4),
                          ("pinned", pinned), ("processes", processed)):
        if report is not None:
            assert report.results == serial.results, f"{label} differs from serial"
    print()
    print("all backends produced identical paragraphs")

    # Per-document overrides and mixed input shapes in a single batch
    with open(paths[0], "rb") as handle:
        content = handle.read()
    report = slice_many(
        [
            SliceJob.from_path(paths[0]),
            SliceJob.from_path(paths[1], limit=200),   # this one slices finer
            (os.path.basename(paths[2]), content),     # (name, bytes)
            {"name": "mapping.md", "content": content},
        ],
        policy=SchedulerPolicy(concurrency=2, error_policy="collect"),
        limit=800,
    )
    print()
    print(f"mixed batch: {[outcome.name for outcome in report.outcomes]}")
    print(f"paragraph counts: {[len(rows) for rows in report.results]}")

    # Failure handling: one unsupported document must not lose the others
    bad = os.path.join(directory, "movie.mp4")
    with open(bad, "wb") as handle:
        handle.write(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32)
    report = slice_many(paths[:3] + [bad], concurrency=4, error_policy="collect", limit=800)
    print()
    print(f"with an unsupported file: ok={report.ok} "
          f"failed={[outcome.name for outcome in report.failures]} "
          f"code={report.failures[0].error.code}")
    print(f"succeeded anyway: {len([rows for rows in report.results if rows])} documents")


if __name__ == "__main__":
    main()