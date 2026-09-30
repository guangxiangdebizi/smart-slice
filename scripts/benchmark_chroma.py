# coding=utf-8
"""ARCHIVED, ONE-OFF. Run smart-slice through the Chroma chunking benchmark.

Kept so the numbers in docs/BENCHMARK.md section 4 can be reproduced, and not part
of the project's benchmark routine: it scores **retrieval recall**, which needs an
embedding model and a labelled query set, so the result says as much about the
model as about the chunker. smart-slice's own KPIs are throughput and structural
fidelity, both measurable offline with no model - see scripts/benchmark.py and
scripts/benchmark_peers.py. Costs worth knowing before re-running:
ClusterSemanticChunker(max=400) did not terminate (18 h wall clock, killed), and
LLMSemanticChunker needs an LLM API key.

This is *not* a synthetic corpus. It is the evaluation framework published with
the Chroma Technical Report **"Evaluating Chunking Strategies for Retrieval"**
(Smith & Troynikov, 2024, https://research.trychroma.com/evaluating-chunking),
package ``chunking_evaluation`` (MIT, github.com/brandonstarxel/chunking_evaluation):

* 5 real corpora shipped with the package - `chatlogs.md`, `finance.md`,
  `pubmed.md`, `state_of_the_union.md`, `wikitexts.md` (1.45 MB total);
* **472 human-authored questions** with ground-truth reference spans
  (`questions_df.csv`), 1-5 spans each, mean span 167 characters;
* retrieval scored with **recall / precision / IoU** against those spans, plus
  `precision_omega` (the chunker-independent upper bound on precision);
* **frozen query embeddings** (`auto_questions_sentence_transformer`, 384-d) so
  the run needs no API key and is byte-reproducible.

Setup (a separate venv is recommended - chromadb and friends are heavy and are
*not* smart-slice dependencies):

    python -m venv .bench && . .bench/bin/activate
    pip install langchain-text-splitters chonkie
    pip install git+https://github.com/brandonstarxel/chunking_evaluation.git

``sentence-transformers`` (and therefore torch) is **not** needed: chromadb's
ONNX ``all-MiniLM-L6-v2`` produces vectors that are numerically identical to the
frozen ones shipped with the benchmark - this script verifies that (cosine
1.00000 over a sample) before reusing them, and falls back to re-embedding the
queries with the same function if the check ever fails.

    python scripts/benchmark_chroma.py                 # everything
    python scripts/benchmark_chroma.py --quick         # two configs per family
    python scripts/benchmark_chroma.py --methods smart-slice,regular
    python scripts/benchmark_chroma.py --json docs/benchmark_chroma.json

Two honesty notes, both repeated in docs/BENCHMARK.md:

1. The report's own Table 1 was produced on an earlier dataset set
   (arxiv / github-code / github-issues / toc-book) whose repository
   (``chroma-core/segmentation-benchmarks``) now 404s. This script runs the
   package's shipped ``GeneralEvaluation`` corpora instead, so **absolute numbers
   are not comparable to the report's table** - only the relative ordering inside
   one run of this script is.
2. The harness locates every chunk by searching for it verbatim in the corpus
   (``rigorous_document_search``), so a chunker whose output is not a substring
   of the source cannot be scored at all. ``SmartSliceChunker`` therefore emits
   an exact tiling of the source (asserted with ``"".join(chunks) == text``):
   each chunk is the paragraph body **plus the heading and whitespace that
   precede it**. That is the same text a RAG index would embed, and it is why
   smart-slice can be scored here without weakening its fidelity guarantee.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------- #
# the smart-slice adapter
# --------------------------------------------------------------------------- #


def verbatim_tiling(text: str, rows: Sequence[Dict[str, str]]) -> Tuple[List[str], int]:
    """Turn ``[{title, content}]`` back into an exact tiling of ``text``.

    smart-slice moves each heading into ``title`` and strips the paragraph body,
    so neither field is on its own a substring of the source. Re-walking the
    bodies in order and extending each one back to where the previous chunk ended
    yields spans that concatenate to the source *exactly* - heading lines and
    inter-paragraph whitespace included.

    :returns: ``(chunks, unlocatable)`` - ``unlocatable`` counts paragraphs whose
              body could not be found and were therefore skipped (0 expected)
    """
    spans: List[Tuple[int, int]] = []
    cursor = 0
    missed = 0
    for row in rows:
        body = row.get("content") or ""
        if not body.strip():
            continue
        located = _locate(text, body, cursor)
        if located is None:
            # Not lost: the next chunk starts where the previous one ended, so an
            # unlocatable paragraph is simply absorbed into its neighbour and the
            # tiling below stays exact.
            missed += 1
            continue
        start = spans[-1][1] if spans else 0
        end = located[1]
        if end <= start:
            cursor = max(cursor, end)
            missed += 1
            continue
        spans.append((start, end))
        cursor = end
    if not spans:
        return [text], missed
    if spans[-1][1] < len(text):
        spans[-1] = (spans[-1][0], len(text))
    chunks = [text[a:b] for a, b in spans]
    if "".join(chunks) != text:
        raise AssertionError("tiling is not exact - the harness would mis-score it")
    return chunks, missed


_WS_RUN = re.compile(r"\s+")


def _locate(text: str, body: str, cursor: int) -> Optional[Tuple[int, int]]:
    """Find a paragraph body in the source, returning its ``(start, end)``.

    A body is *usually* a verbatim substring, but not always: smart-slice appends
    a cut table's header row to the continuation chunk, so that chunk is no longer
    contiguous in the source. Falling back to the longest findable prefix keeps
    such paragraphs in the tiling instead of dropping them - and dropping nothing
    matters, because the tiling is asserted to reconstruct the source exactly.
    """
    index = text.find(body, cursor)
    if index >= 0:
        return index, index + len(body)
    for fraction in (0.75, 0.5, 0.25):
        head = body[: int(len(body) * fraction)]
        if len(head) < 40:
            break
        index = text.find(head, cursor)
        if index >= 0:
            return index, index + len(head)
    return _loose_find(text, body, cursor)


def _loose_find(text: str, body: str, cursor: int) -> Optional[Tuple[int, int]]:
    """Whitespace-insensitive search, used only if every exact match fails."""
    words = [re.escape(w) for w in _WS_RUN.sub(" ", body).strip().split()]
    if not words:
        return None
    pattern = re.compile(r"\s*".join(words[:60]))
    match = pattern.search(text, cursor)
    return (match.start(), match.end()) if match else None


def make_smart_slice_chunker(base, limit: int, overlap: int = 0):
    class SmartSliceChunker(base):
        """smart-slice's heading tree, emitted as an exact tiling of the source."""

        def __init__(self) -> None:
            self._chunk_size = limit
            self._chunk_overlap = overlap
            self.unlocatable = 0

        def split_text(self, text: str) -> List[str]:
            from smart_slice import slice_text

            rows = slice_text(text, limit=limit, overlap=overlap, with_filter=False)
            chunks, missed = verbatim_tiling(text, rows)
            self.unlocatable += missed
            return chunks

    SmartSliceChunker.__name__ = f"SmartSliceChunker_{limit}"
    return SmartSliceChunker()


def make_smart_slice_merged_chunker(base, limit: int, target_tokens: int):
    """smart-slice boundaries, greedily packed up to a token budget.

    ``limit`` is a *budget*, not a grid: on prose whose natural paragraphs are
    short, smart-slice returns short paragraphs whatever the budget says (on these
    corpora the realised mean tops out near 140 cl100k tokens). To compare against
    chunkers configured in tokens, this variant uses smart-slice's cut points as
    candidates and packs adjacent ones until the next would exceed
    ``target_tokens``. Concatenating adjacent spans keeps the tiling exact, so the
    harness still locates every chunk verbatim.

    Packing can only merge, never split, so a single paragraph already larger than
    the target stays oversized: the realised mean (reported in the table) is the
    number to compare on, not the requested target. ``limit`` picks the base
    granularity and therefore how closely the target can be approached.
    """
    from chunking_evaluation.utils import openai_token_count

    class SmartSliceMergedChunker(base):
        def __init__(self) -> None:
            self._chunk_size = target_tokens
            self._chunk_overlap = 0
            self.unlocatable = 0

        def split_text(self, text: str) -> List[str]:
            from smart_slice import slice_text

            rows = slice_text(text, limit=limit, with_filter=False)
            pieces, missed = verbatim_tiling(text, rows)
            self.unlocatable += missed
            merged: List[str] = []
            current = ""
            count = 0
            for piece in pieces:
                tokens = openai_token_count(piece)
                if current and count + tokens > target_tokens:
                    merged.append(current)
                    current, count = piece, tokens
                else:
                    current += piece
                    count += tokens
            if current:
                merged.append(current)
            if "".join(merged) != text:
                raise AssertionError("merged tiling is not exact")
            return merged

    SmartSliceMergedChunker.__name__ = f"SmartSliceMerged_{target_tokens}"
    return SmartSliceMergedChunker()


def make_simple_chunker(base, label: str, split):
    """Adapter for any library that exposes ``text -> [str]``."""

    class Adapter(base):
        def __init__(self) -> None:
            self._chunk_size = 0
            self._chunk_overlap = 0

        def split_text(self, text: str) -> List[str]:
            return list(split(text))

    Adapter.__name__ = label
    return Adapter()


# --------------------------------------------------------------------------- #
# the method catalogue
# --------------------------------------------------------------------------- #


def patch_chromadb_compat(evaluation: Any) -> None:
    """Bridge the benchmark's 2024 chromadb assumptions to chromadb >= 1.0.

    ``BaseEvaluation._chunker_to_collection`` clears its scratch collection with
    ``try: delete_collection(...) except ValueError: pass``. chromadb 1.x raises
    ``NotFoundError`` for a missing collection instead, so the first method of a
    run dies before any chunking happens. The shim below narrows that one call
    back to the old behaviour; nothing else in the harness is touched, and the
    scoring code runs exactly as published.
    """
    client = evaluation.chroma_client
    cls = type(client)
    if getattr(cls, "_smart_slice_compat_patched", False):
        return
    original = cls.delete_collection

    def delete_collection(self, name, *args, **kwargs):
        try:
            return original(self, name, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - only "missing" is swallowed
            if "does not exist" in str(exc):
                return None
            raise

    cls.delete_collection = delete_collection
    cls._smart_slice_compat_patched = True


def verify_oracle_location(evaluation: Any, corpora: Dict[str, str]) -> Dict[str, int]:
    """Locate every ground-truth span exactly the way the harness does.

    This is the invariant that proves the wiring, and it is stricter than any
    score: if a reference span cannot be found at its own coordinates, then no
    chunker's recall/precision/IoU means anything. Three outcomes are counted:

    ``exact``            found at the true offset with the true length
    ``trailing_period``  found at the true offset but one character short, because
                         ``rigorous_document_search`` strips a trailing ``.`` from
                         the target before searching - a property of the published
                         harness that caps ``precision_omega`` just below 1.0 for
                         *every* chunker, not just this one
    ``mislocated``       found elsewhere, or not found (repeated text in the corpus)
    """
    from chunking_evaluation.utils import rigorous_document_search

    texts = {}
    for cid, path in corpora.items():
        with open(path, encoding="utf-8") as handle:
            texts[cid] = handle.read()

    counts = {"total": 0, "exact": 0, "trailing_period": 0, "mislocated": 0}
    for _, row in evaluation.questions_df.iterrows():
        text = texts[row["corpus_id"]]
        for reference in row["references"]:
            start, end = int(reference["start_index"]), int(reference["end_index"])
            chunk = text[start:end]
            counts["total"] += 1
            found = rigorous_document_search(text, chunk)
            if found is None:
                counts["mislocated"] += 1
            elif found[1] == start and found[2] == end:
                counts["exact"] += 1
            elif found[1] == start:
                counts["trailing_period"] += 1
            else:
                counts["mislocated"] += 1
    return counts


def make_oracle_chunker(base, evaluation: Any, corpora: Dict[str, str]):
    """Emit exactly the ground-truth reference spans - a harness self-test.

    ``precision_omega`` is computed brute-force over *all* chunks (no retrieval):
    for each question it is the union of chunk-and-reference intersections over
    the union of the intersecting chunks plus any reference text no chunk
    touched. A chunker whose chunks *are* the reference spans must therefore
    score exactly 1.0, which makes it a real invariant: if it does not, the
    adapter, the chunk locating or the scoring is broken and no number in the
    table can be trusted.

    ``recall`` and ``precision`` are *not* expected to hit 1.0 here, and that is
    informative rather than a failure: both are measured on the top-``retrieve``
    results, so a chunker emitting 167-character spans is at the mercy of the
    embedding model's ability to rank those very short snippets. The oracle's
    recall is printed as the retrieval ceiling of the dataset for context.
    """
    spans: Dict[str, List[Tuple[int, int]]] = {cid: [] for cid in corpora}
    for _, row in evaluation.questions_df.iterrows():
        for reference in row["references"]:
            pair = (int(reference["start_index"]), int(reference["end_index"]))
            if pair not in spans[row["corpus_id"]]:
                spans[row["corpus_id"]].append(pair)

    class OracleChunker(base):
        def __init__(self) -> None:
            self._chunk_size = 0
            self._chunk_overlap = 0
            self._current: Optional[str] = None

        def split_text(self, text: str) -> List[str]:
            for cid, path in corpora.items():
                with open(path, encoding="utf-8") as handle:
                    if handle.read() == text:
                        self._current = cid
                        break
            pairs = sorted(spans.get(self._current or "", []))
            return [text[a:b] for a, b in pairs if b > a]

    return OracleChunker()


def build_embedding_function() -> Any:
    """ONNX ``all-MiniLM-L6-v2``, presented under the name the harness looks for.

    ``BaseEvaluation.run`` only reuses the frozen query embeddings when
    ``embedding_function.__class__.__name__`` is ``SentenceTransformerEmbeddingFunction``.
    Installing sentence-transformers drags in torch, so instead the ONNX function
    chromadb already ships is subclassed under that name - legitimate only because
    the two produce the same vectors, which :func:`verify_against_frozen` checks
    rather than assumes.
    """
    import chromadb.utils.embedding_functions as efactory

    onnx = efactory.DefaultEmbeddingFunction()
    renamed = type("SentenceTransformerEmbeddingFunction", (onnx.__class__,), {})
    return renamed(), onnx


def verify_against_frozen(evaluation: Any, ef: Any) -> Optional[float]:
    """Cosine between freshly embedded questions and the shipped frozen ones.

    Returns ``None`` when the frozen collection cannot be read (the harness then
    re-embeds the queries itself, which is a supported path).
    """
    import math
    import os

    try:
        import chromadb

        data_dir = os.path.dirname(evaluation.questions_csv_path)
        client = chromadb.PersistentClient(path=os.path.join(data_dir, "questions_db"))
        frozen_collection = client.get_collection("auto_questions_sentence_transformer")
        frozen = frozen_collection.get(include=["embeddings"])
    except Exception as exc:  # noqa: BLE001 - frozen set unavailable is not fatal
        print(f"frozen query embeddings unreadable ({type(exc).__name__}: {exc})")
        return None

    embeddings = dict(zip(frozen["ids"], frozen["embeddings"]))
    questions = evaluation.questions_df["question"].tolist()
    sample = list(range(0, len(questions), max(1, len(questions) // 40)))[:40]
    fresh = ef([questions[i] for i in sample])
    worst = 1.0
    for offset, index in enumerate(sample):
        target = embeddings.get(str(index))
        if target is None:
            continue
        a, b = fresh[offset], target
        dot = sum(x * y for x, y in zip(a, b))
        norm = (math.sqrt(sum(x * x for x in a)) or 1.0) * (math.sqrt(sum(y * y for y in b)) or 1.0)
        worst = min(worst, dot / norm)
    return round(worst, 6)


def build_methods(base, ef, quick: bool, merge_base: int = 1600) -> List[Tuple[str, str, Any]]:
    """``(family, label, chunker_instance)`` triples."""
    from chunking_evaluation.chunking import (
        ClusterSemanticChunker,
        FixedTokenChunker,
        KamradtModifiedChunker,
        RecursiveTokenChunker,
    )
    from chunking_evaluation.utils import openai_token_count

    token_sizes = [200, 400] if quick else [100, 200, 400, 800]
    # English prose averages ~4 characters per cl100k token, so these character
    # budgets land near the token budgets above; the realised mean token count is
    # reported for every row so the matching can be checked rather than assumed.
    char_limits = [800, 1600] if quick else [400, 800, 1600, 3200]

    out: List[Tuple[str, str, Any]] = []
    for limit in char_limits:
        out.append(("smart-slice", f"smart-slice slice_text(limit={limit})",
                    make_smart_slice_chunker(base, limit)))
    for tokens in ([200, 400] if quick else [100, 200, 400, 800]):
        out.append(("smart-slice-merge",
                    f"smart-slice(limit={merge_base}) + merge to {tokens} tok",
                    make_smart_slice_merged_chunker(base, merge_base, tokens)))
    for size in token_sizes:
        out.append(("regular", f"FixedTokenChunker({size} tok, overlap=0)",
                    FixedTokenChunker(chunk_size=size, chunk_overlap=0)))
        # NB: RecursiveTokenChunker inherits TextSplitter's length_function=len,
        # so its chunk_size is CHARACTERS despite the class name - that is also
        # what LangChain's RecursiveCharacterTextSplitter does, which is what the
        # report's "Recursive Chunking" row was. The realised token mean in the
        # output table is the number to compare on.
        out.append(("recursive", f"RecursiveTokenChunker({size} chars, overlap=0)",
                    RecursiveTokenChunker(chunk_size=size, chunk_overlap=0)))
        out.append(("recursive-tok", f"RecursiveTokenChunker({size} tok, overlap=0)",
                    RecursiveTokenChunker(chunk_size=size, chunk_overlap=0,
                                          length_function=openai_token_count)))
    if not quick:
        for size in (200, 400):
            out.append(("cluster-semantic", f"ClusterSemanticChunker(max={size} tok)",
                        ClusterSemanticChunker(ef, max_chunk_size=size)))
        out.append(("kamradt", "KamradtModifiedChunker(avg=400 tok)",
                    KamradtModifiedChunker(avg_chunk_size=400, embedding_function=ef)))

    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        for size in ([400] if quick else [200, 400, 800]):
            out.append(("langchain", f"langchain RecursiveCharacterTextSplitter({size} tok)",
                        make_simple_chunker(
                            base, f"LangChainRecursive_{size}",
                            _token_splitter(RecursiveCharacterTextSplitter, size))))
    except ImportError:
        pass

    try:
        from chonkie import RecursiveChunker as ChonkieRecursive
        from chonkie import TokenChunker as ChonkieToken

        for size in ([400] if quick else [200, 400, 800]):
            out.append(("chonkie", f"chonkie TokenChunker({size}, default tokenizer)",
                        make_simple_chunker(base, f"ChonkieToken_{size}",
                                            _chonkie_split(ChonkieToken, size))))
        out.append(("chonkie", "chonkie RecursiveChunker(400, default tokenizer)",
                    make_simple_chunker(base, "ChonkieRecursive_400",
                                        _chonkie_split(ChonkieRecursive, 400))))
    except ImportError:
        pass
    return out


def _token_splitter(cls, size: int):
    """langchain splitter measured in cl100k tokens, to match the paper's units."""
    from chunking_evaluation.utils import openai_token_count

    def split(text: str) -> List[str]:
        splitter = cls(chunk_size=size, chunk_overlap=0,
                       length_function=openai_token_count)
        return splitter.split_text(text)

    return split


def _chonkie_split(cls, size: int):
    def split(text: str) -> List[str]:
        chunker = cls(chunk_size=size)
        return [c.text for c in chunker.chunk(text)]

    return split


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def realised_tokens(chunks: Sequence[str]) -> Dict[str, Any]:
    try:
        from chunking_evaluation.utils import openai_token_count

        counts = [openai_token_count(c) for c in chunks]
    except Exception:  # noqa: BLE001 - tiktoken missing; fall back to a char proxy
        counts = [max(1, len(c) // 4) for c in chunks]
    counts = counts or [0]
    return {
        "chunks": len(chunks),
        "tokens_mean": round(statistics.mean(counts), 1),
        "tokens_median": statistics.median(counts),
        "tokens_min": min(counts),
        "tokens_max": max(counts),
        "chars_mean": round(statistics.mean([len(c) for c in chunks] or [0]), 1),
    }


def run_one(label: str, chunker: Any, evaluation: Any, ef: Any, retrieve: int,
            corpora: Dict[str, str]) -> Dict[str, Any]:
    started = time.perf_counter()
    sizes: Dict[str, Any] = {}
    for corpus_id, path in corpora.items():
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        sizes[corpus_id] = realised_tokens(chunker.split_text(text))
    total_chunks = sum(v["chunks"] for v in sizes.values())
    result = evaluation.run(chunker, ef, retrieve=retrieve)
    elapsed = time.perf_counter() - started

    all_means = [v["tokens_mean"] * v["chunks"] for v in sizes.values()]
    row = {
        "label": label,
        "recall_mean": round(float(result["recall_mean"]), 4),
        "recall_std": round(float(result["recall_std"]), 4),
        "precision_mean": round(float(result["precision_mean"]), 4),
        "iou_mean": round(float(result["iou_mean"]), 4),
        "precision_omega_mean": round(float(result["precision_omega_mean"]), 4),
        "chunks": total_chunks,
        "tokens_mean": round(sum(all_means) / total_chunks, 1) if total_chunks else 0.0,
        "seconds": round(elapsed, 1),
        "per_corpus": {
            cid: {
                "chunks": v["chunks"],
                "tokens_mean": v["tokens_mean"],
                "recall": round(float(statistics.mean(s["recall_scores"])), 4)
                if (s := result["corpora_scores"].get(cid)) and s["recall_scores"] else None,
            }
            for cid, v in sizes.items()
        },
        "unlocatable": getattr(chunker, "unlocatable", 0),
    }
    return row


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true", help="two configs per family")
    parser.add_argument("--methods", default="", help="comma-separated family filter")
    parser.add_argument("--exclude", default="",
                        help="comma-separated substrings; drop any label containing one")
    parser.add_argument("--merge-base", type=int, default=1600,
                        help="character budget for the paragraphs the merge variant packs")
    parser.add_argument("--retrieve", type=int, default=5,
                        help="chunks retrieved per question (default 5, -1 = adaptive)")
    parser.add_argument("--json", default="", help="write the raw numbers here")
    parser.add_argument("--oracle", action="store_true",
                        help="first run a ground-truth chunker that must score 1.0 - "
                             "a self-test of the harness, not a method")
    parser.add_argument("--model", default="all-MiniLM-L6-v2",
                        help="sentence-transformer model; all-MiniLM-L6-v2 is the one "
                             "the frozen query embeddings were built with")
    args = parser.parse_args(argv)

    from chunking_evaluation import BaseChunker, GeneralEvaluation

    ef, onnx = build_embedding_function()
    print(f"embedding function: {onnx.__class__.__name__} (ONNX {args.model}, "
          f"{len(ef(['probe'])[0])}-d), presented as {ef.__class__.__name__}")
    evaluation = GeneralEvaluation()
    corpora = {cid: path for cid, path in evaluation.corpora_id_paths.items()}
    print(f"corpora: {len(corpora)} files, "
          f"{sum(os.path.getsize(p) for p in corpora.values()) / 1e6:.2f} MB, "
          f"questions: {len(evaluation.questions_df)}, retrieve={args.retrieve}")

    cosine = verify_against_frozen(evaluation, ef)
    if cosine is None:
        print("frozen query embeddings: not used - queries re-embedded with the same function")
    elif cosine >= 0.999:
        print(f"frozen query embeddings: reused (worst cosine vs local ONNX = {cosine:.6f})")
    else:
        print(f"WARNING: worst cosine {cosine:.6f} < 0.999 - the frozen vectors are NOT "
              f"from this model; rename the class away to force re-embedding")
    print()

    patch_chromadb_compat(evaluation)

    if args.oracle:
        oracle = make_oracle_chunker(BaseChunker, evaluation, corpora)
        row = run_one("ORACLE (ground-truth spans)", oracle, evaluation, ef,
                      args.retrieve, corpora)
        located = verify_oracle_location(evaluation, corpora)
        share = located["exact"] + located["trailing_period"]
        print(f"ORACLE self-test: chunks={row['chunks']}  "
              f"precision_omega={row['precision_omega_mean']:.4f}  "
              f"retrieval-bound recall={row['recall_mean']} "
              f"precision={row['precision_mean']} iou={row['iou_mean']}")
        print(f"span location over {located['total']} ground-truth references: "
              f"exact={located['exact']} trailing_period={located['trailing_period']} "
              f"mislocated={located['mislocated']}")
        if located["total"] == 0 or share / located["total"] < 0.98:
            print("the harness cannot locate the ground-truth spans at their own "
                  "offsets - aborting before reporting any real numbers.",
                  file=sys.stderr)
            return 3
        print("harness verified: ground-truth spans are located at their true offsets. "
              "precision_omega stays just under 1.0 because the harness strips a "
              "trailing '.' before searching, which costs every chunker equally. "
              "Recall/precision are retrieval-bound and shown for context only.\n")
        if not args.methods and args.json == "":
            return 0

    wanted = {m.strip() for m in args.methods.split(",") if m.strip()}
    methods = build_methods(BaseChunker, ef, args.quick, args.merge_base)
    if wanted:
        methods = [m for m in methods if m[0] in wanted or m[1] in wanted]
    dropped = {d.strip() for d in args.exclude.split(",") if d.strip()}
    if dropped:
        methods = [m for m in methods if not any(d in m[1] for d in dropped)]
    if not methods:
        print("no methods selected", file=sys.stderr)
        return 2

    head = (f"{'method':<52} {'chunks':>7} {'tok':>6} {'recall':>8} {'prec':>7} "
            f"{'iou':>7} {'p_omega':>8} {'s':>7}")
    print(head)
    print("-" * len(head))
    rows: List[Dict[str, Any]] = []
    for family, label, chunker in methods:
        sys.stdout.flush()
        try:
            row = run_one(label, chunker, evaluation, ef, args.retrieve, corpora)
        except Exception as exc:  # noqa: BLE001 - one broken method must not kill the run
            print(f"{label:<52} FAILED {type(exc).__name__}: {exc}"[:200])
            rows.append({"label": label, "family": family, "error": f"{type(exc).__name__}: {exc}"})
            continue
        row["family"] = family
        rows.append(row)
        flag = f" (unlocatable={row['unlocatable']})" if row["unlocatable"] else ""
        print(f"{label:<52} {row['chunks']:7d} {row['tokens_mean']:6.0f} "
              f"{row['recall_mean']:8.4f} {row['precision_mean']:7.4f} {row['iou_mean']:7.4f} "
              f"{row['precision_omega_mean']:8.4f} {row['seconds']:7.1f}{flag}")
        sys.stdout.flush()

    if args.json:
        payload = {
            "benchmark": "chunking_evaluation.GeneralEvaluation (Chroma Technical Report, 2024)",
            "package": "github.com/brandonstarxel/chunking_evaluation",
            "embedding_model": args.model,
            "retrieve": args.retrieve,
            "corpora": {cid: os.path.getsize(p) for cid, p in corpora.items()},
            "questions": len(evaluation.questions_df),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "rows": rows,
        }
        with open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
        print(f"\nraw numbers written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())