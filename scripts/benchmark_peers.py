# coding=utf-8
"""Horizontal benchmark: smart-slice against other chunking libraries.

    python scripts/benchmark_peers.py               # full table
    python scripts/benchmark_peers.py --reps 3      # faster, noisier
    python scripts/benchmark_peers.py --json out.json
    python scripts/benchmark_peers.py --only chonkie

Peers are optional: anything not installed is reported as ``not installed``
instead of failing the run.  ``pip install chonkie langchain-text-splitters``
covers the two compared here.

What is measured, and what is not
---------------------------------
Every library optimises for something different, so a single "winner" number
would be misleading.  Two families of numbers are reported side by side.

*throughput*  wall-clock per pass over the same 551 KB structured-markdown
              corpus (the one ``scripts/benchmark.py`` generates), plus the
              chunk-count and chunk-length distribution it produced.

*structure*   cheap, model-free proxies for "is the output still usable for
              retrieval":

              recall   multiset recall of ``\\w+`` tokens - did any source
                       *wording* get lost.  Markdown syntax is excluded, because
                       moving a ``#`` into a title field is a feature, not a loss.
              units    fraction of sampled blank-line-delimited source segments
                       (>= 40 chars) found intact inside a single chunk - i.e.
                       does a retrieval hit return a whole paragraph / table row /
                       code fence rather than half of one.
              ctx      fraction of the corpus's ATX heading lines whose text
                       appears in the first 300 characters of some chunk - does
                       the section context travel with the passage.
              fence    chunks with an unbalanced number of ``` markers: a code
                       block cut in half.
              tbl      chunks holding a markdown table row without that table's
                       header row.
              dup      total chunk characters / source characters (>1 means
                       overlap or repeated headers).

Retrieval quality itself - recall@k against a labelled query set, the metric
Chonkie's ``BENCHMARKS.md`` and Chroma's *Evaluating Chunking Strategies for
Retrieval* report - needs an embedding model and a gold corpus, so it is out of
scope for an offline script.  The proxies above are what can be measured without
one; they are proxies, not a substitute.

Units are not comparable across libraries and are stated per row: Chonkie's
default tokenizer is character-level, ``langchain``'s ``TokenTextSplitter`` counts
tiktoken tokens, everything else counts characters.  Read the ``mean`` column
(realised chunk length in characters) before comparing two rows.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from benchmark import build_corpus  # noqa: E402  (same corpus as PERFORMANCE.md)

SEED = 11
#: markdown table header of the generated corpus (used by the orphan-row metric)
_TABLE_HEADER = "| key | value | note |"
_HEADING_TEXT = re.compile(r"^#+\s*")
#: word tokens for the recall metric (lower-cased, markdown syntax excluded)
_WORDS = re.compile(r"\w+")


# --------------------------------------------------------------------------- #
# structure metrics
# --------------------------------------------------------------------------- #


def _chunks_of(value: Any) -> List[str]:
    """Normalise a library's return value to a list of strings."""
    out: List[str] = []
    for item in value or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            text = item.get("content") or item.get("text") or item.get("page_content") or ""
            out.append(text if isinstance(text, str) else str(text))
        else:
            text = getattr(item, "text", None)
            if text is None:
                text = getattr(item, "page_content", None)
            out.append(text if isinstance(text, str) else str(item))
    return out


def measure_structure(source: str, chunks: Sequence[str], *, unit_sample: int = 200) -> Dict[str, Any]:
    """Model-free structure metrics (see the module docstring for what they mean)."""
    from collections import Counter

    # \w+ tokens, not whitespace tokens: markdown syntax ("#", "|", "```") is
    # formatting a splitter is allowed to move or drop, and counting it would
    # penalise exactly the structure-aware splitters being compared here.
    source_words = Counter(_WORDS.findall(source))
    chunk_words: Counter = Counter()
    for chunk in chunks:
        chunk_words.update(_WORDS.findall(chunk))
    total_words = sum(source_words.values()) or 1
    word_recall = sum((source_words & chunk_words).values()) / total_words

    # natural units: blank-line-delimited segments big enough to be meaningful.
    # A splitter that keeps them intact lets a retrieval hit return a whole
    # paragraph / table row / code fence instead of half of one.
    units = [unit.strip() for unit in re.split(r"\n\s*\n", source) if len(unit.strip()) >= 40]
    rnd = random.Random(SEED)
    if len(units) > unit_sample:
        units = rnd.sample(units, unit_sample)
    heads = [chunk[:300] for chunk in chunks]
    unit_keep = sum(1 for unit in units if any(unit in chunk for chunk in chunks)) / (len(units) or 1)

    heading_texts = [_HEADING_TEXT.sub("", line).strip()
                     for line in source.splitlines() if line.startswith("#")]
    heading_texts = [h for h in heading_texts if h]
    context = sum(1 for text in heading_texts
                  if any(text in head for head in heads)) / (len(heading_texts) or 1)

    broken_fences = sum(1 for chunk in chunks if chunk.count("```") % 2)
    orphan_rows = sum(
        1 for chunk in chunks
        if any(line.startswith("| k") for line in chunk.splitlines()) and _TABLE_HEADER not in chunk
    )
    lengths = [len(chunk) for chunk in chunks] or [0]
    return {
        "chunks": len(chunks),
        "chars_mean": round(statistics.mean(lengths), 1),
        "chars_median": statistics.median(lengths),
        "chars_min": min(lengths),
        "chars_max": max(lengths),
        "word_recall": round(word_recall, 4),
        "unit_keep": round(unit_keep, 4),
        "context": round(context, 4),
        "broken_fences": broken_fences,
        "orphan_table_rows": orphan_rows,
        "dup": round(sum(lengths) / len(source), 3) if source else 0.0,
    }


# --------------------------------------------------------------------------- #
# methods under test
# --------------------------------------------------------------------------- #


class Method:
    """One chunking configuration: a label, a budget description and a callable."""

    def __init__(self, label: str, family: str, budget: str, fn: Callable[[str], List[str]],
                 note: str = "") -> None:
        self.label = label
        self.family = family
        self.budget = budget
        self.fn = fn
        self.note = note
        self.error: Optional[str] = None
        self.seconds: List[float] = []
        self.structure: Dict[str, Any] = {}

    def run_once(self, text: str) -> List[str]:
        return _chunks_of(self.fn(text))


def smart_slice_methods(limit: int, window: int) -> List[Method]:
    def paragraphs(text: str) -> List[str]:
        from smart_slice import slice_text

        return [row["content"] for row in slice_text(text, limit=limit)]

    def titled(text: str) -> List[str]:
        from smart_slice import slice_text

        return [(f"# {row['title']}\n" if row["title"] else "") + row["content"]
                for row in slice_text(text, limit=limit)]

    def windows(text: str) -> List[str]:
        from smart_slice import chunk_paragraphs, slice_text

        return chunk_paragraphs(slice_text(text, limit=limit), chunk_size=window, carry_title=True)

    def overlapped(text: str) -> List[str]:
        from smart_slice import slice_text

        return [(f"# {row['title']}\n" if row["title"] else "") + row["content"]
                for row in slice_text(text, limit=limit, overlap=window // 2)]

    return [
        Method(f"smart-slice slice_text({limit})", "smart-slice", f"{limit} chars", paragraphs,
               "raw API output: heading lives in row['title'], not in the text"),
        Method(f"smart-slice slice_text({limit}) + title", "smart-slice", f"{limit} chars", titled,
               "heading chain re-attached - what an index should embed"),
        Method(f"smart-slice +chunk({window}, carry_title)", "smart-slice",
               f"{limit} then {window} chars", windows, "paragraphs cut down to an embedding window"),
        Method(f"smart-slice {limit} overlap={window // 2}", "smart-slice",
               f"{limit} chars +{window // 2}", overlapped, "overlap on, tiles the source >1x"),
    ]


def chonkie_methods(limit: int) -> List[Method]:
    try:
        from chonkie import RecursiveChunker, SentenceChunker, TokenChunker
    except Exception as exc:  # noqa: BLE001
        return [Method("chonkie (not installed)", "chonkie", "-", _unavailable(exc))]

    def token_chunker():
        chunker = TokenChunker(chunk_size=limit)
        return lambda text: chunker.chunk(text)

    def sentence_chunker():
        chunker = SentenceChunker(chunk_size=limit)
        return lambda text: chunker.chunk(text)

    def recursive_chunker():
        chunker = RecursiveChunker(chunk_size=limit)
        return lambda text: chunker.chunk(text)

    return [
        Method(f"chonkie TokenChunker({limit})", "chonkie", f"{limit} chars (default tokenizer)",
               _lazy(token_chunker)),
        Method(f"chonkie SentenceChunker({limit})", "chonkie", f"{limit} chars (default tokenizer)",
               _lazy(sentence_chunker)),
        Method(f"chonkie RecursiveChunker({limit})", "chonkie", f"{limit} chars (default tokenizer)",
               _lazy(recursive_chunker)),
    ]


def langchain_methods(limit: int, window: int) -> List[Method]:
    try:
        from langchain_text_splitters import (
            MarkdownHeaderTextSplitter,
            MarkdownTextSplitter,
            RecursiveCharacterTextSplitter,
            TokenTextSplitter,
        )
    except Exception as exc:  # noqa: BLE001
        return [Method("langchain-text-splitters (not installed)", "langchain", "-", _unavailable(exc))]

    def recursive():
        splitter = RecursiveCharacterTextSplitter(chunk_size=limit, chunk_overlap=0)
        return lambda text: splitter.split_text(text)

    def recursive_overlap():
        splitter = RecursiveCharacterTextSplitter(chunk_size=limit, chunk_overlap=window // 2)
        return lambda text: splitter.split_text(text)

    def markdown():
        splitter = MarkdownTextSplitter(chunk_size=limit, chunk_overlap=0)
        return lambda text: splitter.split_text(text)

    def token():
        splitter = TokenTextSplitter(chunk_size=window, chunk_overlap=0)
        return lambda text: splitter.split_text(text)

    def markdown_headers():
        headers = [(f"{'#' * level}", f"h{level}") for level in range(1, 7)]
        splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers, strip_headers=False)
        return lambda text: [row.page_content for row in splitter.split_text(text)]

    return [
        Method(f"langchain RecursiveCharacterTextSplitter({limit})", "langchain", f"{limit} chars",
               _lazy(recursive)),
        Method(f"langchain RecursiveCharacter({limit}, ovl={window // 2})", "langchain",
               f"{limit} chars +{window // 2}", _lazy(recursive_overlap)),
        Method(f"langchain MarkdownTextSplitter({limit})", "langchain", f"{limit} chars", _lazy(markdown)),
        Method(f"langchain TokenTextSplitter({window})", "langchain",
               f"{window} tiktoken tokens (~{window * 4} chars)", _lazy(token)),
        Method("langchain MarkdownHeaderTextSplitter", "langchain", "headings only, no length cap",
               _lazy(markdown_headers)),
    ]


def _lazy(build: Callable[[], Callable[[str], Any]]) -> Callable[[str], Any]:
    """Construct the chunker once, on first use (import + model warm-up excluded)."""
    holder: Dict[str, Any] = {}

    def call(text: str) -> Any:
        if "fn" not in holder:
            holder["fn"] = build()
        return holder["fn"](text)

    return call


def _unavailable(exc: Exception) -> Callable[[str], Any]:
    def call(text: str) -> Any:
        raise RuntimeError(f"not installed: {type(exc).__name__}: {exc}")

    return call


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def build_methods(limit: int, window: int) -> List[Method]:
    return smart_slice_methods(limit, window) + chonkie_methods(limit) + langchain_methods(limit, window)


def run(methods: List[Method], text: str, reps: int, warmup: int = 1) -> None:
    """Interleaved repetitions: A,B,C,A,B,C,... so drift hits everyone equally."""
    for method in methods:
        for _ in range(warmup):
            try:
                chunks = method.run_once(text)
            except Exception as exc:  # noqa: BLE001 - one bad library must not kill the run
                method.error = f"{type(exc).__name__}: {exc}"
                break
        if method.error:
            continue
        try:
            method.structure = measure_structure(text, chunks)
        except Exception as exc:  # noqa: BLE001
            method.error = f"metrics failed: {type(exc).__name__}: {exc}"

    for _ in range(reps):
        for method in methods:
            if method.error:
                continue
            started = time.perf_counter()
            try:
                method.run_once(text)
            except Exception as exc:  # noqa: BLE001
                method.error = f"{type(exc).__name__}: {exc}"
                continue
            method.seconds.append(time.perf_counter() - started)


def print_table(methods: List[Method], text: str, reps: int) -> None:
    size_mb = len(text) / 1e6
    print(f"corpus: {len(text):,} characters ({size_mb:.2f} MB), {reps} interleaved repetitions")
    print(f"python {sys.version.split()[0]}, {sys.platform}")
    print()
    head = (f"{'method':<50} {'ms/pass':>8} {'MB/s':>7} {'chunks':>7} {'mean':>6} {'max':>6} "
            f"{'recall':>7} {'units':>6} {'ctx':>6} {'fence':>6} {'tbl':>5} {'dup':>5}")
    print(head)
    print("-" * len(head))
    for method in methods:
        if method.error:
            print(f"{method.label:<50} {'-':>8} {'-':>7}  {method.error[:110]}")
            continue
        median = statistics.median(method.seconds) if method.seconds else float("nan")
        info = method.structure
        print(f"{method.label:<50} {median * 1000:8.1f} {size_mb / median:7.2f} "
              f"{info['chunks']:7d} {info['chars_mean']:6.0f} {info['chars_max']:6d} "
              f"{info['word_recall']:7.4f} {info['unit_keep']:6.3f} {info['context']:6.3f} "
              f"{info['broken_fences']:6d} {info['orphan_table_rows']:5d} {info['dup']:5.2f}")
    print()
    print("what each method was asked for:")
    for method in methods:
        print(f"  {method.label:<50} {method.budget}")
        if method.note:
            print(f"  {'':<50} {method.note}")
    print()
    print("recall = multiset recall of \\w+ word tokens: is any source wording lost (1.0 = nothing lost)")
    print("units  = blank-line-delimited source segments (>=40 chars, 200 sampled) found intact in one chunk")
    print("ctx    = heading lines whose text appears in the first 300 chars of some chunk")
    print("fence  = chunks with an unbalanced ``` count   tbl = table rows separated from their header")
    print("dup    = total chunk characters / source characters (>1 = overlap or repeated headers)")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Compare smart-slice with peer chunking libraries.")
    parser.add_argument("--reps", type=int, default=3, help="interleaved repetitions per method (default 3)")
    parser.add_argument("--limit", type=int, default=1000, help="character budget for the char-based methods")
    parser.add_argument("--window", type=int, default=256, help="embedding-window budget for the token-based ones")
    parser.add_argument("--only", default="", help="substring filter on the method label")
    parser.add_argument("--json", default="", help="also write the raw numbers to this path")
    parser.add_argument("--sections", type=int, default=40, help="corpus size knob (build_corpus sections)")
    args = parser.parse_args(argv)

    text = build_corpus(sections=args.sections)
    methods = build_methods(args.limit, args.window)
    if args.only:
        methods = [m for m in methods if args.only.lower() in m.label.lower()]
        if not methods:
            print(f"no method matches {args.only!r}", file=sys.stderr)
            return 2
    run(methods, text, args.reps)
    print_table(methods, text, args.reps)

    if args.json:
        payload = {
            "corpus_chars": len(text),
            "reps": args.reps,
            "limit": args.limit,
            "window": args.window,
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "methods": [
                {
                    "label": m.label, "family": m.family, "budget": m.budget, "note": m.note,
                    "error": m.error,
                    "seconds": m.seconds,
                    "median_ms": (statistics.median(m.seconds) * 1000) if m.seconds else None,
                    "structure": m.structure,
                }
                for m in methods
            ],
        }
        with open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
        print(f"raw numbers written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())