# coding=utf-8
"""Reproduce the numbers in docs/PERFORMANCE.md.

    python scripts/benchmark.py            # timing table
    python scripts/benchmark.py --profile  # cProfile breakdown of the hot path
    python scripts/benchmark.py --ab       # C accelerator vs pure-Python scan
    python scripts/benchmark.py --batch    # batch scheduler: serial vs threads vs processes

The corpus is generated from a fixed seed, so runs are comparable across machines
in relative terms (absolute times obviously depend on the CPU).

For the comparison against *other* chunking libraries (chonkie,
langchain-text-splitters) on this same corpus, see scripts/benchmark_peers.py and
docs/BENCHMARK.md.
"""
from __future__ import annotations

import argparse
import os
import random
import string
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SEED = 7


def build_corpus(sections=40, subs=6, paras=4, body_paras=6) -> str:
    """A structured markdown document: headings, prose, tables and code fences."""
    rnd = random.Random(SEED)

    def sentence(words=14):
        return " ".join(
            "".join(rnd.choices(string.ascii_lowercase, k=rnd.randint(3, 9)))
            for _ in range(words)
        ) + "."

    out = []
    for s in range(sections):
        out.append(f"# Section {s} Overview")
        out.append("")
        for _ in range(body_paras):
            out.append(sentence(rnd.randint(20, 40)))
            out.append("")
        for sub in range(subs):
            out.append(f"## {s}.{sub} Subsystem Detail")
            out.append("")
            out.append("| key | value | note |")
            out.append("| --- | --- | --- |")
            for r in range(12):
                out.append(f"| k{s}_{sub}_{r} | {rnd.randint(1, 9999)} | {sentence(6)} |")
            out.append("")
            out.append("```python")
            out.append("# a comment that must survive")
            out.append("x = 0xFF00AA")
            out.append("```")
            out.append("")
            for _ in range(paras):
                out.append(sentence(rnd.randint(25, 60)))
                out.append("")
    return "\n".join(out)


def timeit(fn, repetitions=8):
    fn()  # warm-up
    start = time.perf_counter()
    for _ in range(repetitions):
        fn()
    return (time.perf_counter() - start) / repetitions


def cmd_timing(doc: str) -> None:
    from smart_slice import chunk_paragraphs, slice_text

    size_mb = len(doc) / 1e6
    print(f"corpus: {len(doc):,} characters ({size_mb:.2f} MB)")
    print()

    base = timeit(lambda: slice_text(doc, limit=1000))
    rows = slice_text(doc, limit=1000)
    print(f"slice_text  limit=1000 overlap=0    {base * 1000:8.1f} ms   {size_mb / base:6.2f} MB/s   {len(rows)} paragraphs")

    for overlap in (100, 150, 300):
        t = timeit(lambda: slice_text(doc, limit=1000, overlap=overlap))
        out = slice_text(doc, limit=1000, overlap=overlap)
        chars = sum(len(r["content"]) for r in out)
        print(f"slice_text  limit=1000 overlap={overlap:<4} {t * 1000:8.1f} ms   {size_mb / t:6.2f} MB/s   {chars:,} chars")

    print()
    for overlap in (0, 40):
        t = timeit(lambda: chunk_paragraphs(rows, chunk_size=256, chunk_overlap=overlap or None))
        print(f"chunk_paragraphs size=256 overlap={overlap:<3} {t * 1000:8.1f} ms")


def cmd_profile(doc: str) -> None:
    import cProfile
    import io
    import pstats

    from smart_slice import slice_text

    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(6):
        slice_text(doc, limit=1000)
    profiler.disable()

    stream = io.StringIO()
    pstats.Stats(profiler, stream=stream).sort_stats("tottime").print_stats(14)
    print(stream.getvalue())


def cmd_ab(doc: str) -> None:
    import smart_slice._accel as accel
    from smart_slice._speedup_py import scan_heading_candidates as python_scan
    from smart_slice.chunker import mask_code_blocks

    masked = mask_code_blocks(doc)
    print(f"accelerator active: {accel.ACCELERATOR}")
    print(f"masked corpus: {len(masked):,} chars, "
          f"{len(python_scan(masked))} heading candidates")
    print()

    def bench(fn, repetitions=40):
        fn()
        start = time.perf_counter()
        for _ in range(repetitions):
            fn()
        return (time.perf_counter() - start) / repetitions * 1000

    t_python = bench(lambda: python_scan(masked))
    print(f"pure-Python scan      {t_python:8.2f} ms   {len(masked) / 1e6 / (t_python / 1000):7.1f} MB/s")

    try:
        from smart_slice._speedup import scan_heading_candidates as c_scan
    except ImportError:
        print("C extension not built -> run: python scripts/build_ext.py")
    else:
        assert c_scan(masked) == python_scan(masked), "C and Python scans disagree"
        t_c = bench(lambda: c_scan(masked))
        print(f"C scan                {t_c:8.2f} ms   {len(masked) / 1e6 / (t_c / 1000):7.1f} MB/s")
        print(f"speedup               {t_python / t_c:8.1f}x")

    from smart_slice import slice_text

    print()
    print(f"end-to-end slice_text with {accel.ACCELERATOR}: "
          f"{timeit(lambda: slice_text(doc, limit=1000)) * 1000:.1f} ms")
    print("Rename smart_slice/_speedup*.pyd away and re-run to measure the Python path.")


def cmd_batch(doc: str, documents: int, repetitions: int) -> None:
    """Measure the batch scheduler: serial vs thread widths vs process widths.

    Builds ``documents`` real files on disk (so the measurement includes the
    read, not just the slice) and times a full batch pass for each policy.
    """
    import tempfile

    from smart_slice import available_cores, auto_concurrency, slice_paths

    directory = tempfile.mkdtemp(prefix="ss-bench-batch-")
    paths = []
    for index in range(documents):
        path = os.path.join(directory, f"bench{index:03d}.md")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(doc)
        paths.append(path)

    total_mb = sum(os.path.getsize(path) for path in paths) / 1e6
    print(f"corpus: {documents} documents, {total_mb:.2f} MB total "
          f"({total_mb / documents:.2f} MB each)")
    print(f"machine: {available_cores()} usable cores, auto width = {auto_concurrency()}")
    print()

    configs = [("serial", "serial", None)]
    for width in (2, 4, auto_concurrency(), available_cores()):
        configs.append((f"thread x{width}", "thread", width))
    for width in (2, 4):
        configs.append((f"process x{width}", "process", width))

    baseline = None
    print(f"{'policy':<16} {'batch time':>12} {'throughput':>12} {'speedup':>9}")
    for label, backend, width in configs:
        best = None
        for _ in range(repetitions):
            start = time.perf_counter()
            report = slice_paths(paths, backend=backend, concurrency=width, limit=1000)
            elapsed = time.perf_counter() - start
            assert report.ok, [str(o.error) for o in report.failures]
            best = elapsed if best is None else min(best, elapsed)
        if baseline is None:
            baseline = best
        print(f"{label:<16} {best * 1000:9.1f} ms {total_mb / best:9.2f} MB/s "
              f"{baseline / best:8.2f}x")

    print()
    print("Threads only overlap the GIL-releasing work inside the parsers; the")
    print("pure-Python slicer itself serialises.  Processes bypass the GIL but pay")
    print("interpreter startup (spawn) and pickle the document bytes both ways.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--profile", action="store_true", help="cProfile breakdown")
    mode.add_argument("--ab", action="store_true", help="C vs Python accelerator")
    mode.add_argument("--batch", action="store_true", help="batch scheduler: serial vs threads vs processes")
    mode.add_argument("--dump-corpus", metavar="PATH", help="write the corpus and exit")
    parser.add_argument("--documents", type=int, default=12, help="batch size for --batch (default 12)")
    parser.add_argument("--repetitions", type=int, default=3, help="runs per policy for --batch (best of N)")
    args = parser.parse_args(argv)

    doc = build_corpus()
    if args.dump_corpus:
        Path(args.dump_corpus).write_text(doc, encoding="utf-8", newline="\n")
        print(f"wrote {args.dump_corpus}")
        return 0

    if args.profile:
        cmd_profile(doc)
    elif args.ab:
        cmd_ab(doc)
    elif args.batch:
        cmd_batch(doc, args.documents, args.repetitions)
    else:
        cmd_timing(doc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())