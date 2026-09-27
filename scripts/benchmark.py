# coding=utf-8
"""Reproduce the numbers in docs/PERFORMANCE.md.

    python scripts/benchmark.py            # timing table
    python scripts/benchmark.py --profile  # cProfile breakdown of the hot path
    python scripts/benchmark.py --ab       # C accelerator vs pure-Python scan

The corpus is generated from a fixed seed, so runs are comparable across machines
in relative terms (absolute times obviously depend on the CPU).
"""
from __future__ import annotations

import argparse
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--profile", action="store_true", help="cProfile breakdown")
    mode.add_argument("--ab", action="store_true", help="C vs Python accelerator")
    mode.add_argument("--dump-corpus", metavar="PATH", help="write the corpus and exit")
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
    else:
        cmd_timing(doc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())