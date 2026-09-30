# Benchmark

What `smart-slice` measures about itself, what it measures against other
libraries, and one archived retrieval run that is kept for the record but is
**not** a project KPI.

| axis | is it a KPI here? | where |
|------|-------------------|-------|
| slicing throughput | **yes** - it is the thing this library can improve without depending on anyone's model | §1, [PERFORMANCE.md](PERFORMANCE.md) |
| structural fidelity of the output | **yes** - measurable offline, no model, no labels | §2 |
| retrieval quality (recall@k) | **no** - it needs an embedding model and a labelled query set, so it measures the embedding model at least as much as the chunker | §4 (archived, one-off) |

That split is deliberate. `smart-slice` ships no embedding model and no
evaluated query set, so a recall number produced here would be a statement about
whichever small model happened to be installed. §4 records one such run against
the only authoritative public chunking benchmark that exists, with its limitations
stated, and it is not repeated.

---

## 1. Throughput (the KPI)

Same 551,565-character structured markdown corpus throughout - 40 sections x 6
subsections, each with a 12-row table and a code fence; 1,693 paragraphs out at
`limit=1000`. Generated from a fixed seed by `scripts/benchmark.py`.

| version | ms / document | MB/s | vs 0.1.0 |
|---------|---------------|------|----------|
| 0.1.0 (as extracted) | 394 | 1.40 | 1.00x |
| 0.2.0 pure Python | 144 | 3.83 | 2.74x |
| 0.2.0 + C accelerator | 139 | 3.97 | 2.83x |
| 0.5.0 | 124-131 | 4.2-4.4 | **~3.1x** |

Where each step came from, and what did *not* help:

| change | effect | measured how |
|--------|--------|--------------|
| skip provably-empty regex passes (heading prefilter) | the single biggest win; `re.Pattern.findall` calls fell from 97,272 to 1,934 per document | cProfile, 12 reps |
| do not mask code fences that are not there | removes a full `list()`/`join()` copy per block | cProfile |
| bypass `re._compile` dispatch for compiled patterns | 194,544 calls removed | cProfile |
| kill an O(n^2) flatten | Python calls/document 4,333,837 -> 1,546,718 (-64%) | cProfile |
| optional C accelerator (`smart-slice[accel]`) | isolated heading scan 2.42 ms -> 0.55 ms (**4.4x**); end to end 133.0 -> 125.3 ms (**1.061x**) | interleaved A/B |
| 0.5.0 pattern-level cache | `parse_title_level` 4.36 us -> 3.91 us (**-10.3%**); ~0.9 ms of the ~128 ms pass, i.e. at this machine's noise floor | 20k-call timeit x 45 interleaved samples; end-to-end paired A/B (60 pairs) gave +0.5% median / +2.1% min, paired mean +2.26 ms with stdev 14.7 ms - **not significant on its own**, and reported as such |

Every one of those is asserted output-identical by the test suite, so the speed
was not bought with behaviour.

### Throughput across a corpus

Single-document latency is not the whole story; a corpus is sliced in parallel.
`scripts/benchmark.py --batch` on a 12-core laptop:

| batch | serial | thread x4 | process x4 |
|-------|--------|-----------|------------|
| 12 docs / 6.6 MB | 2618.9 ms (1.00x) | 2454.9 ms (1.07x) | 3790.2 ms (0.69x) |
| 48 docs / 26.5 MB | 10055.7 ms (1.00x) | 10615.3 ms (0.95x) | 6254.5 ms (**1.61x**) |

Honest reading: threads do **not** help pure-Python slicing (the GIL), and a
process pool only pays for itself once the batch is large enough to amortise
pickling - below that it is a net loss. That is why `concurrency="auto"` keeps the
width conservative (3 workers above six cores) rather than grabbing every core.

---

## 2. Peer comparison: throughput and structure

```bash
pip install chonkie langchain-text-splitters tiktoken
python scripts/benchmark_peers.py --reps 5 --json docs/benchmark_peers.json
```

Same corpus, interleaved repetitions (A,B,C,...,A,B,C so thermal drift hits every
method equally), construction excluded. Raw numbers:
[`benchmark_peers.json`](benchmark_peers.json).

| method | ms/pass | MB/s | chunks | mean | recall | units | ctx | fence | tbl | dup |
|---|---|---|---|---|---|---|---|---|---|---|
| **smart-slice slice_text(1000) + title** | 127.4 | 4.33 | 1693 | 382 | **1.000** | **1.000** | **1.000** | **0** | **0** | 1.17 |
| smart-slice slice_text(1000) | 124.2 | 4.39 | 1693 | 341 | 0.986 | 1.000 | 0.462 | 0 | 0 | 1.05 |
| smart-slice +chunk(256, carry_title) | 135.1 | 4.08 | 3462 | 208 | 1.000 | 0.325 | 1.000 | 480 | 506 | 1.30 |
| smart-slice 1000 overlap=128 | 131.0 | 4.21 | 1693 | 507 | 1.000 | 1.000 | 1.000 | 0 | 455 | 1.56 |
| chonkie RecursiveChunker(1000) | **9.9** | **55.4** | 621 | 888 | 1.000 | 0.880 | 0.583 | 4 | 222 | 1.00 |
| chonkie SentenceChunker(1000) | 110.3 | 5.00 | 620 | 890 | 1.000 | 0.880 | 0.569 | 4 | 225 | 1.00 |
| chonkie TokenChunker(1000) | 101.7 | 5.43 | 552 | 999 | 0.995 | 0.660 | 0.681 | 25 | 172 | 1.00 |
| langchain RecursiveCharacterTextSplitter(1000) | **3.5** | **159.2** | 741 | 742 | 1.000 | 1.000 | 0.638 | 0 | 0 | 1.00 |
| langchain RecursiveCharacter(1000, ovl=128) | 3.2 | 171.4 | 755 | 755 | 1.000 | 1.000 | 0.948 | 0 | 0 | 1.03 |
| langchain MarkdownTextSplitter(1000) | 8.4 | 65.5 | 745 | 739 | 1.000 | 0.865 | 0.640 | 480 | 0 | 1.00 |
| langchain TokenTextSplitter(256 tokens) | 106.3 | 5.19 | 1218 | 453 | 0.989 | 0.430 | 0.844 | 45 | 408 | 1.00 |
| langchain MarkdownHeaderTextSplitter | 41.3 | 13.4 | 280 | 1974 | 1.000 | 1.000 | 0.538 | 0 | 0 | 1.00 |

Environment: CPython 3.12.10 on a 12-core Windows laptop, smart-slice 0.5.0 with
the C accelerator active, chonkie 1.7.0, langchain-text-splitters 1.1.2,
tiktoken 0.14.0.

Metric definitions - all model-free, all computed from the chunks alone:

| column | meaning |
|--------|---------|
| `recall` | multiset recall of `\w+` tokens: did any source *wording* get lost. Markdown syntax is excluded on purpose - moving a `#` into a `title` field is a feature, not a loss |
| `units` | 200 sampled blank-line-delimited source segments (>= 40 chars) found **intact inside a single chunk** - does a hit return a whole paragraph / table row / code fence, or half of one |
| `ctx` | the corpus's 280 ATX heading lines whose text appears in the first 300 characters of some chunk - does section context travel with the passage |
| `fence` | chunks with an unbalanced number of ``` markers: a code block cut in half |
| `tbl` | chunks holding a markdown table data row without that table's header row |
| `dup` | total chunk characters / source characters (>1 = overlap or repeated headers) |

### Reading it

**On throughput, `smart-slice` loses, and by a lot.** 127 ms against
`langchain RecursiveCharacterTextSplitter`'s 3.5 ms is a factor of ~36, and
`chonkie RecursiveChunker` is ~13x faster. The reason is not an unoptimised inner
loop - it is that the peers do one recursive `split` over a separator list while
`smart-slice` builds a six-level heading tree, masks code fences, re-appends cut
table headers to continuation chunks, splits on CJK-aware sentence boundaries and
assembles heading chains. `cProfile` puts `re.Pattern.findall` alone at 62 ms of
the 127 ms. It is also a *document* library: those 127 ms are usually preceded by
parsing a `.docx`, `.pdf` or `.xlsx`, which typically costs more than the slicing.

| situation | verdict |
|-----------|---------|
| offline ingest of a corpus | throughput is not the bottleneck - `slice_many(backend="process")` gave 1.61x on 48 docs / 26.5 MB |
| one 5 MB document at upload time | ~1.1 s of slicing on top of the parse; fine for an async ingest job, noticeable inside a synchronous request |
| chunking a string you already hold, in a hot loop | use `langchain` or `chonkie` - an order of magnitude faster and, on text with no tables or code fences, nearly as faithful |

**On structure, `smart-slice` is the only row at 1.000 on all three fidelity
metrics with zero broken fences and zero orphaned table rows.** Each of those is a
different mechanism, and each is what the extra time buys: `units` 1.000 because
the length budget is applied *after* the heading tree and the blank-line pass;
`ctx` 1.000 because the heading **chain** is a field rather than something that
has to survive by luck of positioning (best peer: 0.948, and only with overlap
on); `fence` 0 because code fences are masked before heading detection
(`langchain MarkdownTextSplitter` cuts 480 fences in half here); `tbl` 0 because a
cut table gets its header row appended to the continuation chunk
(`chonkie TokenChunker` orphans 172 rows, `langchain TokenTextSplitter` 408).

The one smart-slice row that is not 1.000 everywhere is `+chunk(256)`: a
256-character window cannot hold a code fence or a table, so `units` drops to
0.325 and fences get cut. That is arithmetic, not a defect - every library
degrades the same way at that budget.

Two library gotchas worth knowing, both verified rather than assumed:
**Chonkie's default tokenizer is character-level** (`TokenChunker(chunk_size=10)`
emits chunks of exactly 10 characters), so anything benchmarked against Chonkie
defaults is a character-budget comparison; and **`langchain
MarkdownHeaderTextSplitter` has no length cap** (chunks up to 2,542 characters
here), so it needs a second splitter behind it.

---

## 3. Reproducing

```bash
pip install -e ".[dev]"
python scripts/benchmark.py            # throughput table + cProfile
python scripts/benchmark.py --profile  # hot-path breakdown
python scripts/benchmark.py --ab       # C vs pure-Python accelerator
python scripts/benchmark.py --batch --documents 48   # serial vs threads vs processes

pip install chonkie langchain-text-splitters tiktoken
python scripts/benchmark_peers.py --reps 5 --json docs/benchmark_peers.json
```

Measurements are interleaved rather than blocked, and the optimisation claims are
checked for output identity before they are timed - single-shot timings on a
laptop were repeatedly misleading during this work (the pattern-level cache
measured +3.6% in one blocked A/B and +0.46% in an interleaved one; the isolated
function-level number is the trustworthy one).

---

## 4. Archived: the Chroma retrieval benchmark (one-off, not a KPI)

Kept for the record. **This is not repeated**, and no claim in this repository
rests on it.

The only authoritative public benchmark for chunking is the framework published
with the Chroma Technical Report *Evaluating Chunking Strategies for Retrieval*
(Smith & Troynikov, 2024, https://research.trychroma.com/evaluating-chunking),
package `chunking_evaluation` (MIT,
github.com/brandonstarxel/chunking_evaluation, 511 stars). It is real rather than
synthetic:

* 5 shipped corpora - `chatlogs.md`, `finance.md`, `pubmed.md`,
  `state_of_the_union.md`, `wikitexts.md`, 1.45 MB total;
* **472 questions with ground-truth reference spans** (1-5 spans each, mean span
  167 characters);
* recall / precision / IoU against those spans, plus `precision_omega`, the
  chunker-independent ceiling;
* **frozen query embeddings** shipped in the repo, so no API key is needed.

Run it with `scripts/benchmark_chroma.py`; raw output in
[`benchmark_chroma.json`](benchmark_chroma.json).

### How it was run, and what was verified first

`sentence-transformers` (hence torch) could not be used - the machine's
application-control policy blocks `torch_python.dll` - so chromadb's ONNX
`all-MiniLM-L6-v2` was used instead. That substitution was **checked, not
assumed**: the ONNX vectors reproduce the shipped frozen query embeddings at
cosine **1.000000** (worst of 40 sampled questions), and the frozen 384-d set is
what the run used. chromadb 1.5.9 needed one compatibility shim (`delete_collection`
now raises `NotFoundError` where the 2024 harness catches `ValueError`).

An **oracle self-test** runs before any method is reported: chunk the corpora into
exactly the ground-truth spans and check the harness can find them. Result -
790 references, **exact 695, trailing-period 95, mislocated 0**, i.e. every span
located at its true offset. `precision_omega` comes out 0.9875 rather than 1.0
because `rigorous_document_search` strips a trailing `.` before searching; that
costs every method equally. Had the oracle failed, the script aborts instead of
printing numbers.

`smart-slice` cannot be scored as-is, because the harness locates each chunk by
searching for it verbatim in the source and `slice_text` moves headings into
`title`. The adapter therefore emits an **exact tiling** of the source - each
chunk is the paragraph body plus the heading and whitespace preceding it - and
asserts `"".join(chunks) == text`. `unlocatable` in the JSON counts paragraphs
whose body is not a contiguous substring (the table-header carry); they are
absorbed into the neighbouring chunk, so no text is dropped.

### Results

`retrieve=5` (the harness default), ONNX `all-MiniLM-L6-v2`, sorted by **realised**
mean chunk size in cl100k tokens - that column, not the requested budget, is what
makes rows comparable:

| method | chunks | tok | recall | prec | iou | p_omega |
|---|---|---|---|---|---|---|
| RecursiveTokenChunker(200 chars) | 10989 | 30 | 0.5172 | 0.1652 | 0.1450 | 0.6798 |
| smart-slice slice_text(limit=400) | 5428 | 61 | 0.6507 | 0.1213 | 0.1165 | 0.5367 |
| RecursiveTokenChunker(400 chars) | 5208 | 63 | 0.6587 | 0.1163 | 0.1112 | 0.5047 |
| chonkie RecursiveChunker(400) | 5159 | 64 | 0.6655 | 0.1163 | 0.1113 | 0.4479 |
| RecursiveTokenChunker(100 tok) | 4698 | 70 | 0.6824 | 0.1054 | 0.1022 | 0.4875 |
| chonkie TokenChunker(400) | 3612 | 92 | 0.6700 | 0.0843 | 0.0812 | 0.3458 |
| smart-slice slice_text(limit=800) | 3478 | 95 | 0.7145 | 0.0872 | 0.0859 | 0.3724 |
| **ClusterSemanticChunker(max=200)** | 3324 | 98 | **0.7558** | 0.0726 | 0.0717 | 0.3363 |
| FixedTokenChunker(100 tok) | 3285 | 100 | 0.6960 | 0.0770 | 0.0751 | 0.3253 |
| smart-slice slice_text(limit=1600) | 2634 | 125 | 0.7494 | 0.0686 | 0.0680 | 0.2747 |
| RecursiveTokenChunker(800 chars) | 2594 | 126 | 0.7343 | 0.0685 | 0.0675 | 0.3302 |
| RecursiveTokenChunker(200 tok) | 2386 | 137 | 0.7540 | 0.0616 | 0.0613 | 0.2994 |
| langchain RecursiveCharacterTextSplitter(200 tok) | 2335 | 140 | 0.7537 | 0.0580 | 0.0577 | 0.2918 |
| smart-slice slice_text(limit=3200) | 2315 | 142 | 0.7118 | 0.0587 | 0.0579 | 0.2256 |
| chonkie TokenChunker(800) | 1807 | 183 | 0.7542 | 0.0524 | 0.0520 | 0.2269 |
| **FixedTokenChunker(200 tok)** | 1644 | 200 | **0.7806** | 0.0461 | 0.0458 | 0.2098 |
| smart-slice + merge to 100 tok | 1462 | 225 | 0.7076 | 0.0370 | 0.0368 | 0.1707 |
| RecursiveTokenChunker(400 tok) | 1187 | 276 | 0.7378 | 0.0316 | 0.0315 | 0.1774 |
| smart-slice + merge to 200 tok | 1190 | 277 | 0.7414 | 0.0280 | 0.0280 | 0.1391 |
| langchain RecursiveCharacterTextSplitter(400 tok) | 1183 | 277 | 0.7325 | 0.0318 | 0.0317 | 0.1722 |
| FixedTokenChunker(400 tok) | 824 | 398 | 0.7279 | 0.0230 | 0.0230 | 0.1250 |
| smart-slice + merge to 400 tok | 820 | 401 | 0.7157 | 0.0184 | 0.0184 | 0.1018 |
| RecursiveTokenChunker(800 tok) | 522 | 628 | 0.7120 | 0.0140 | 0.0140 | 0.0883 |
| langchain RecursiveCharacterTextSplitter(800 tok) | 522 | 628 | 0.7110 | 0.0139 | 0.0139 | 0.0869 |
| smart-slice + merge to 800 tok | 462 | 711 | 0.6909 | 0.0111 | 0.0111 | 0.0682 |
| FixedTokenChunker(800 tok) | 413 | 795 | 0.6654 | 0.0107 | 0.0107 | 0.0694 |

For context, the oracle (chunks = the ground-truth spans, ~40 tokens) scored
recall 0.8179 - so with this embedding model and top-5 retrieval, **0.82 is the
practical ceiling**, and every method above is being compared inside a 0.52-0.78
band underneath it.

### Conclusion, stated plainly

* **Nothing dominates.** The best recall on this dataset belongs to plain
  `FixedTokenChunker` at 200 tokens (0.7806), with the report's own
  `ClusterSemanticChunker` second at ~98 tokens (0.7558). Structure-aware
  chunking does not automatically win - which is also what the Chroma report
  concluded for its own datasets.
* **`smart-slice` is mid-pack and size-competitive, not ahead.** At its natural
  granularity (`limit=1600`, 125 realised tokens) it scores 0.7494, against
  0.7343 for the character-budget recursive splitter at 126 tokens and 0.7540 /
  0.7537 for the token-budget recursive and langchain splitters at 137-140
  tokens. At ~277 tokens it is 0.7414 against langchain's 0.7325. Those gaps are
  a few points wide on a 472-question set with a 384-d mini model; they do not
  support a claim in either direction.
* **Retrieval is approximate.** chromadb's HNSW index is not deterministic across
  runs: the identical configuration scored recall 0.6543 then 0.6507 with the same
  chunk count and the same `precision_omega`. Treat +-0.005 as noise, which is the
  same order as several of the differences above.
* **`smart-slice`'s `limit` is a budget, not a grid.** On prose whose natural
  paragraphs are short the realised mean tops out near 142 tokens no matter how
  large the budget gets, so reaching a 400- or 800-token target needs the
  merge-to-budget adapter (`smart-slice + merge`), which can only pack adjacent
  paragraphs, never split one - hence realised means above target at the small
  settings.

### Why this is not repeated

Scoring chunking by recall requires an embedding model and a labelled query set,
and neither is part of this library. Whatever number comes out is then a property
of that model at least as much as of the chunker - here a 384-d MiniLM retrieving
top-5 against 167-character ground-truth spans, with a 0.82 oracle ceiling.
`smart-slice` optimises throughput and structural fidelity, both of which are
measurable offline with no model and no labels, and those are what §1-§3 track.

Two other costs are worth recording for anyone tempted to re-run it:
`ClusterSemanticChunker(max=400)` **did not terminate** - 18 h wall clock, 6.1 h
CPU, 775 MB RSS - and was killed, so only its `max=200` row is reported;
`KamradtModifiedChunker` and `LLMSemanticChunker` were skipped (the latter needs
an LLM API key). Also note the harness's own `RecursiveTokenChunker` inherits
`length_function=len`, so its `chunk_size` is **characters** despite the class
name - rows labelled `chars` above are as configured, rows labelled `tok` pass
`openai_token_count` explicitly.

### Other authoritative benchmarks not run

The Meta-Chunking paper (Zhao et al., arXiv 2410.12788, Apache-2.0,
github.com/IAAR-Shanghai/Meta-Chunking) evaluates over **CRUD, LongBench,
MultiHop-RAG and RAG-Bench**, but its harness runs a full RAG pipeline - vector
store, LLM answer generation, then answer scoring - over datasets distributed on
Google Drive. That needs an LLM API and measures end-to-end answer quality rather
than chunking, so it was not attempted.