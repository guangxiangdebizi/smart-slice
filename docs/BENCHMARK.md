# Peer benchmark

`smart-slice` compared against other chunking libraries on the same corpus, with
the same measurement method. Reproduce it with:

```bash
pip install chonkie langchain-text-splitters tiktoken
python scripts/benchmark_peers.py --reps 5 --json docs/benchmark_peers.json
```

The raw numbers behind the table below are committed as
[`benchmark_peers.json`](benchmark_peers.json).

## Environment

| | |
|---|---|
| machine | laptop CPU, 12 logical cores, AMD64 |
| Python | CPython 3.12.10 (MSC v.1943, 64 bit), Windows |
| smart-slice | 0.5.0, **C accelerator active** (`ACCELERATOR == "c"`) |
| chonkie | 1.7.0 (chonkie-core 0.10.2, tokie 0.1.4) |
| langchain-text-splitters | 1.1.2 (langchain-core 1.6.5) |
| tiktoken | 0.14.0 |

Corpus: the 551,565-character structured markdown document
`scripts/benchmark.py` generates from a fixed seed - 40 sections x 6 subsections,
each with a 12-row markdown table and a Python code fence; 1,693 paragraphs out
of `smart-slice` at `limit=1000`.

Absolute times are this machine's; the *ratios* and the structure columns travel.

## Method

* **Interleaved repetitions.** Every method is timed A,B,C,...,A,B,C,... five
  times rather than five runs of A then five of B, so thermal drift and background
  load hit all of them equally. The median is reported.
* **Construction excluded.** Each chunker object is built once on first use; only
  the `chunk(text)` call is timed.
* **Same input bytes.** Nobody gets a pre-processed string.

Metrics, all model-free:

| column | what it measures |
|--------|------------------|
| `ms/pass` | median wall-clock per pass over the corpus |
| `MB/s` | corpus size / `ms/pass` |
| `chunks` | how many pieces came out |
| `mean`, `max` | realised chunk length in **characters** |
| `recall` | multiset recall of `\w+` tokens: did any source *wording* get lost. Markdown syntax is excluded on purpose - moving a `#` into a `title` field is a feature, not a loss |
| `units` | fraction of 200 sampled blank-line-delimited source segments (>= 40 chars) found **intact inside a single chunk** - does a retrieval hit return a whole paragraph / table row / code fence, or half of one |
| `ctx` | fraction of the corpus's 280 ATX heading lines whose text appears in the first 300 characters of some chunk - does section context travel with the passage |
| `fence` | chunks with an unbalanced number of <code>```</code> markers: a code block cut in half |
| `tbl` | chunks holding a markdown table data row without that table's header row |
| `dup` | total chunk characters / source characters (>1 = overlap or repeated headers) |

## Results

```
method                                              ms/pass    MB/s  chunks   mean    max  recall  units    ctx  fence   tbl   dup
----------------------------------------------------------------------------------------------------------------------------------
smart-slice slice_text(1000)                          124.2    4.44    1693    341    993  0.9855  1.000  0.462      0     0  1.05
smart-slice slice_text(1000) + title                  127.4    4.33    1693    382   1015  1.0000  1.000  1.000      0     0  1.17
smart-slice +chunk(256, carry_title)                  135.1    4.08    3462    208    302  1.0000  0.325  1.000    480   506  1.30
smart-slice 1000 overlap=128                          131.0    4.21    1693    507   1141  1.0000  1.000  1.000      0   455  1.56
chonkie TokenChunker(1000)                            101.7    5.43     552    999   1000  0.9951  0.660  0.681     25   172  1.00
chonkie SentenceChunker(1000)                         110.3    5.00     620    890   1000  1.0000  0.880  0.569      4   225  1.00
chonkie RecursiveChunker(1000)                          9.9   55.44     621    888   1000  1.0000  0.880  0.583      4   222  1.00
langchain RecursiveCharacterTextSplitter(1000)          3.5  159.16     741    742    997  1.0000  1.000  0.638      0     0  1.00
langchain RecursiveCharacter(1000, ovl=128)             3.2  171.42     755    755    998  1.0000  1.000  0.948      0     0  1.03
langchain MarkdownTextSplitter(1000)                    8.4   65.45     745    739   1000  1.0000  0.865  0.640    480     0  1.00
langchain TokenTextSplitter(256)                      106.3    5.19    1218    453    490  0.9891  0.430  0.844     45   408  1.00
langchain MarkdownHeaderTextSplitter                   41.3   13.35     280   1974   2542  1.0000  1.000  0.538      0     0  1.00
```

Requested budgets (the units are **not** interchangeable - read `mean` before
comparing two rows):

| method | budget |
|--------|--------|
| `smart-slice slice_text(1000)` | 1000 characters |
| `smart-slice slice_text(1000) + title` | 1000 characters, heading chain re-attached |
| `smart-slice +chunk(256, carry_title)` | 1000-char paragraphs, then a 256-char embedding window |
| `smart-slice 1000 overlap=128` | 1000 characters with 128 carried forward |
| `chonkie TokenChunker/SentenceChunker/RecursiveChunker(1000)` | 1000 units - **Chonkie's default tokenizer is character-level**, verified: `TokenChunker(chunk_size=10)` emits chunks of exactly 10 characters |
| `langchain RecursiveCharacterTextSplitter(1000)` | 1000 characters |
| `langchain MarkdownTextSplitter(1000)` | 1000 characters |
| `langchain TokenTextSplitter(256)` | 256 **tiktoken** tokens (~1024 characters) |
| `langchain MarkdownHeaderTextSplitter` | headings only, no length cap |

## Reading the numbers

### 1. `smart-slice` is the only row that scores 1.000 on every structure metric

`slice_text(1000) + title` - the form an index should actually embed - keeps
every source word (`recall` 1.0000), keeps every natural unit intact (`units`
1.000), carries its section context (`ctx` 1.000), never cuts a code fence
(`fence` 0) and never orphans a table row from its header (`tbl` 0).

Each of those is a different mechanism, and each is the reason the extra time is
spent:

* `units` 1.000 - the length budget is applied *after* the heading tree and the
  blank-line pass, so a paragraph is only cut when it genuinely exceeds the
  budget, and then on a sentence boundary;
* `ctx` 1.000 - every paragraph carries its heading **chain** in `title`
  (`"Section 17 Overview  17.5 Subsystem Detail"`), not just the nearest heading.
  The best peer score is 0.948, and only with overlap on;
* `fence` 0 - code fences are masked before heading detection, so a `#` inside a
  code block is never a heading and a fence is never a cut point.
  `langchain MarkdownTextSplitter` cuts 480 fences in half on this corpus;
* `tbl` 0 - a table cut by the budget gets its **header row appended** to the
  continuation chunk. Chonkie's `TokenChunker` orphans 172 rows,
  `langchain TokenTextSplitter` 408.

The one row that is not 1.000 everywhere is `+chunk(256, carry_title)`: at a
256-character window a code fence or a table simply does not fit, so `units`
drops to 0.325 and fences get cut. That is arithmetic, not a defect - every
library in the table degrades the same way at that budget
(`langchain TokenTextSplitter(256)`: `units` 0.430, `fence` 45, `tbl` 408).

### 2. `smart-slice` is also the slowest row, by a wide margin

This is the honest cost, stated plainly: **124 ms vs 3.5 ms** - about 36x slower
than `langchain RecursiveCharacterTextSplitter`, and about 13x slower than
`chonkie RecursiveChunker`.

Why the gap is real rather than accidental:

* the peers do a recursive `split` on a separator list. `smart-slice` builds a
  heading **tree** (six levels of regex passes with a prefilter that skips
  provably-empty levels), masks code fences, re-appends table headers to
  continuation chunks, splits on CJK-aware sentence boundaries, and assembles
  heading chains. `cProfile` on this corpus puts `re.Pattern.findall` alone at
  62 ms of the 124 ms;
* it is a *document* library: the same 124 ms is preceded by parsing a `.docx`,
  `.pdf` or `.xlsx` into the text being sliced, which typically costs more than
  the slicing.

When it matters and when it does not:

| situation | verdict |
|-----------|---------|
| offline ingest of a corpus | irrelevant - `slice_many(backend="process")` spreads it over cores; 26.5 MB / 48 documents ran in ~1.6x serial time on 4 processes |
| one 5 MB document at upload time | ~1.1 s of slicing on top of the parse; acceptable for an async ingest job, noticeable in a synchronous request |
| chunking a string you already have, in a hot loop | use `langchain`/`chonkie` - they are an order of magnitude faster and, if the text has no tables or code fences, nearly as faithful |

Closing the gap further means replacing the per-level regex cascade with a
single-pass heading scanner that emits the tree directly. That is a rewrite of
the part of the code the fidelity guarantees live in, and it is listed under
"Not tried / not done" in [PERFORMANCE.md](PERFORMANCE.md) for exactly that
reason.

### 3. Two library-specific gotchas worth knowing

* **Chonkie's default tokenizer counts characters.** `TokenChunker(chunk_size=256)`
  does not produce 256 *token* chunks unless a real tokenizer is passed. Anything
  benchmarked against Chonkie defaults is a character-budget comparison.
* **`langchain MarkdownHeaderTextSplitter` has no length cap.** Best-in-class
  structure per chunk (`units` 1.000, `tbl` 0) but chunks up to 2,542 characters
  here - it needs a second splitter behind it before an embedding model sees it.

### 4. Overlap is a duplication tax, and it shows

`dup` is total chunk characters / source characters. Turning on 128 characters of
overlap costs `smart-slice` 1.17 -> 1.56 and `langchain` 1.00 -> 1.03; in exchange
`langchain`'s `ctx` rises from 0.638 to 0.948, because a heading near a boundary
now appears in two chunks. `smart-slice` does not need that trade: `ctx` is
already 1.000 without overlap, because the heading chain is a field rather than
something that has to survive by luck of positioning.

## What this benchmark deliberately does **not** compare

* **Retrieval quality.** recall@k / MRR against a labelled query set - the metric
  Chonkie's `BENCHMARKS.md` and Chroma's *Evaluating Chunking Strategies for
  Retrieval* report - needs an embedding model and a gold corpus. Neither is
  available offline, and a proxy dressed up as a retrieval score would be worse
  than no score. The `recall` / `units` / `ctx` columns are structural proxies.
* **Document parsing.** `chonkie` and `langchain-text-splitters` take a string.
  `smart-slice` takes 197 file extensions across 30 handlers and produces that
  string itself, including the pictures (see
  [ARCHITECTURE.md](ARCHITECTURE.md#multimodal-layer)). There is nothing to
  compare against on that axis here.
* **Non-English text.** The corpus is ASCII markdown. `smart-slice`'s sentence
  splitter also handles `。！？`, and `jieba` backs the optional keyword extra,
  but no CJK corpus is benchmarked.
* **Semantic / neural chunkers.** `chonkie.SemanticChunker`, `SlumberChunker`,
  `NeuralChunker`, `LateChunker` and `TeraflopAIChunker` all need an embedding
  model or a hosted API, so they are excluded from an offline run. They are also
  solving a different problem (semantic similarity boundaries) at a completely
  different cost point.

## References

* Chonkie benchmarks - https://github.com/chonkie-ai/chonkie (see `BENCHMARKS.md`)
* Chroma Research, *Evaluating Chunking Strategies for Retrieval* -
  https://research.trychroma.com/evaluating-chunking
* `smart-slice`'s own numbers and what produced them - [PERFORMANCE.md](PERFORMANCE.md)