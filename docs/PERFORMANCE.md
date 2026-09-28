# Performance

Measured numbers, what produced them, and what did **not** help. Everything here
was profiled rather than assumed; the methodology is at the bottom so it can be
reproduced.

## Headline

Slicing a 551 KB structured markdown corpus (40 sections × 6 subsections, each with
a 12-row table and a code fence; 1,693 paragraphs out):

| Version | Time per document | Throughput |
|---------|-------------------|------------|
| 0.1.0 (as extracted) | 394 ms | 1.40 MB/s |
| 0.2.0 pure Python | 144 ms | 3.83 MB/s |
| 0.2.0 + C accelerator | 139 ms | 3.97 MB/s |
| 0.5.0 (+ pattern-level cache) | 124-131 ms | 4.2-4.4 MB/s |

**~3x faster overall.** The bulk of that gain came from algorithmic fixes in
Python; the C extension contributed about 4%.

Machine: laptop CPU, CPython 3.12.10, Windows, single-threaded, 8-12 repetitions
per measurement after one warm-up call.

For the comparison against other chunking libraries - where `smart-slice` wins on
structure and loses badly on raw throughput - see [BENCHMARK.md](BENCHMARK.md).

## Where the time actually went

Profile of 0.1.0 on that corpus (cProfile, 12 repetitions):

```
   ncalls  tottime  cumtime  function
    97272    2.393    2.393  {method 'findall' of 're.Pattern'}
    97272    0.748    1.687  chunker.py:168(mask_code_blocks)
   117588    0.566    0.566  {method 'join' of 'str'}
    97272    0.358    5.196  chunker.py:184(parse_level)
   194544    0.286    0.448  re/__init__.py:280(_compile)
```

The regexes are CPython's C `sre` engine and were never the real cost. The cost was
**how many times they were invoked**, plus pure-Python work wrapped around them.

## What fixed it

### 1. Skipping provably-empty regex passes (biggest win)

`parse_title_level` cascades down the heading levels until one matches:

```python
result = parse_level(text, pattern[index])     # whole-block regex scan
if not result:
    return parse_title_level(text, pattern, index + 1)
```

A block whose deepest heading is level 3 pays for three whole-block scans, two of
which are guaranteed to return nothing. Now a single linear scan establishes which
heading levels the block can possibly contain, and levels absent from that set are
skipped:

| | before | after |
|---|---|---|
| `re.findall` calls / document | 97,272 | 11,604 |
| `findall` tottime | 2.393 s | 0.353 s |

**Why this is safe.** The scan accepts any line starting with one or more `#`
followed by a space, while the regexes add further guards (`(?!#)`, the level-1
`(?!--*- coding:)`). The scan is therefore a strict *superset*: it may report extra
candidates (costing one regex scan that returns empty) but can never miss a real
one. "The scan reports no level-L candidate" implies "the level-L regex matches
nothing", so skipping that scan cannot change the output.

The premise is tested rather than trusted - see `SupersetPremiseTests` in
`tests/test_c_speedup.py` (systematic enumeration of hash counts × indents ×
separators × tails × fence states, plus fuzz corpora, asserting no level where the
regex matched is missing from the scan). `EndToEndEquivalenceTests` additionally
asserts that slicing with the prefilter disabled produces byte-identical output on
structured, adversarial and fuzz corpora.

The scan is only consulted for the six canonical markdown heading patterns
(`heading_level_of` matches them by exact source string). A custom pattern scheme
takes the regex path unchanged.

### 2. Not masking code fences that aren't there

`mask_code_blocks` allocated a full character list and re-joined it on every call,
even for text with no fence:

```python
result = list(text)      # full copy, always
...
return ''.join(result)   # full rebuild, always
```

Now the common case short-circuits:

```python
if '```' not in text:
    return text
```

| | before | after |
|---|---|---|
| `mask_code_blocks` calls | 97,272 | 11,604 |
| associated `str.join` | 117,588 | 13,291 |

The remaining calls are further reduced by memoising the masked text per block,
since the cascade masks the same block once per heading level.

### 3. Removing `re._compile` dispatch

194,544 lookups per document, because raw/compiled patterns were handed to
`re.findall`, which re-resolves the cache each time. Compiled patterns now call
their own method.

### 4. Killing an O(n²) flatten

```python
reduce(lambda x, y: [*x, *y], list(map(lambda row: [*row], result)), [])
```

builds a fresh list at every step. Replaced with a single pass using
`list.append`.

### 5. Caching the heading level of each pattern (0.5.0)

`parse_title_level` asks `heading_level_of(pattern)` whether a pattern is one of
the six canonical markdown heading patterns, once per pattern per block. On the
551 KB corpus that is **8,106 questions per document** with only seven possible
answers, and each one costs a Python call, a `getattr`, an `isinstance` and a
`dict.get`.

The pattern list is one object for the whole document
(`SplitModel.content_level_pattern`), so the answers are memoised against it -
the same single-entry identity memo already used for `_SCAN_MEMO` and
`_MASK_MEMO`, plus a `len()` comparison so a caller who appends to the list in
place cannot read a stale cache.

Measured two ways, because the end-to-end effect is at this machine's noise floor:

| measurement | result |
|---|---|
| `parse_title_level` in isolation (level-4 block, 20k calls x 45 interleaved samples) | 4.36 us -> 3.91 us, **-10.3%** |
| predicted end-to-end (1,934 calls/document x 0.45 us) | ~0.9 ms of ~128 ms, ~0.7% |
| end-to-end paired A/B (60 interleaved pairs) | +0.5% (median) to +2.1% (min); paired mean +2.26 ms, stdev 14.7 ms - **not significant on its own** |

Output is byte-identical: `tests/test_c_speedup.py`'s equivalence suite and the
full 415-test run pass unchanged, and the A/B harness asserts the two
implementations produce equal paragraph lists before timing them.

### Net effect

| | before | after |
|---|---|---|
| Python calls / document | 4,333,837 | 1,546,718 (-64%) |
| profiled tottime (12 reps) | 6.60 s | 2.35 s (-64%) |

## The C accelerator: honest accounting

`csrc/_speedup.c` implements the heading scan natively.

Isolated benchmark on the masked 551 KB corpus (280 heading candidates):

| Implementation | Time | Throughput |
|----------------|------|------------|
| C (`_speedup`) | 0.55 ms | 994 MB/s |
| Python (`_speedup_py`) | 2.33 ms | 236 MB/s |
| six regex scans (what the prefilter replaces) | 120.34 ms | — |

So the C scan is **4.2x** faster than the Python scan — but end-to-end that is only
139 ms vs 145 ms (**~4%**), because after fix #1 the scan runs 11,604 times instead
of 97,272 and no longer dominates. The extension is worth having (particularly for
heading-dense documents where the scan runs more often) and it is fully optional:
`smart_slice._accel` falls back to the identical pure-Python scan when it is not
built, and results are unchanged either way (`ACCELERATOR` reports which is active).

The published wheel is pure Python. Build the extension only if you want it:

```bash
pip install "smart-slice[accel]"
python scripts/build_ext.py          # uses zig cc; no Visual Studio needed
```

## What overlap costs

Overlap is applied as one post-assembly pass over the paragraph list, so it is
O(paragraphs) and independent of document structure:

| Corpus | overlap=0 | overlap=150 |
|--------|-----------|-------------|
| 551 KB document | 142 ms | 159 ms (+12%) |

Second-stage chunking (`chunk_paragraphs`) costs more per character than slicing
because it re-runs the splitter on every paragraph: 13 ms (no overlap) vs 27 ms
(overlap 40) for the same corpus.

## Batch scheduling: what parallelism actually buys

0.1.0-0.3.0 shipped no parallelism, on the reasoning recorded below ("callers can
parallelise across documents themselves"). 0.4.0 ships the scheduler anyway,
because bulk ingestion is the common case and the ordering/failure semantics are
easy to get subtly wrong. The measurements are here so the choice of backend is
made from numbers rather than from the word "parallel".

`python scripts/benchmark.py --batch` on a 12-core laptop, CPython 3.12, Windows,
each document the 551 KB corpus described above, best of N runs:

**12 documents (6.62 MB), 3 repetitions**

| Policy | Batch time | Throughput | Speedup |
|--------|-----------|------------|---------|
| serial | 2618.9 ms | 2.53 MB/s | 1.00x |
| thread x2 | 2489.2 ms | 2.66 MB/s | 1.05x |
| thread x3 (the `auto` width) | 2485.4 ms | 2.66 MB/s | 1.05x |
| thread x4 | 2454.9 ms | 2.70 MB/s | 1.07x |
| thread x12 | 2489.6 ms | 2.66 MB/s | 1.05x |
| process x2 | 3103.3 ms | 2.13 MB/s | 0.84x |
| process x4 | 3790.2 ms | 1.75 MB/s | 0.69x |

**48 documents (26.48 MB), 1 repetition**

| Policy | Batch time | Throughput | Speedup |
|--------|-----------|------------|---------|
| serial | 10055.7 ms | 2.63 MB/s | 1.00x |
| thread x2 | 10643.0 ms | 2.49 MB/s | 0.94x |
| thread x4 | 10615.3 ms | 2.49 MB/s | 0.95x |
| thread x12 | 11319.3 ms | 2.34 MB/s | 0.89x |
| process x2 | 7421.8 ms | 3.57 MB/s | 1.35x |
| process x4 | 6254.5 ms | 4.23 MB/s | **1.61x** |

### Reading the numbers

**Threads do not speed up this workload, and on a large batch they make it
slightly worse.** Slicing is CPU-bound pure Python: the heading scan, the tree
walk and the paragraph assembly all hold the GIL, so N threads time-slice one
core and pay context-switching on top. The 1.05-1.07x at 12 documents is the
file-read and `zipfile`/`charset-normalizer` C calls overlapping, not the slicer
parallelising; by 48 documents even that is gone (0.89-0.95x). Width makes no
difference - x2 and x12 land within noise of each other, which is the signature
of a serialised bottleneck.

Threads still earn their place as the default for two reasons the benchmark
cannot show: they accept unpicklable arguments (`save_image`, `progress_hook`, a
tokenizer in `length_fn`), and they overlap *the caller's* I/O. That second point
is why the source platform ran a width of 3 rather than 1 - each of its tasks also
wrote rows to a database and dispatched embedding jobs, both of which release the
GIL. If your worker does the same, measure your own pipeline rather than this one.

**Processes are the only backend that scales this workload, and only once the
batch is big enough.** At 12 documents they are 0.69-0.84x: `spawn` starts a fresh
interpreter per worker and the document bytes are pickled in both directions,
which for 6.6 MB costs more than the 2.6 s of work saved. At 48 documents the
fixed cost is amortised over 4x more work and process x4 reaches 1.61x (4.23 MB/s
vs 2.63 MB/s). The crossover on this machine is somewhere between the two -
roughly a few dozen documents, or sooner if the documents are large.

Scaling is sub-linear (1.61x on 4 workers) for the expected reasons: the GIL is
gone but memory bandwidth, the pickle copies and Windows process startup are not.
Linux `spawn` is materially cheaper than Windows, so the crossover arrives earlier
there; the numbers above are the pessimistic case.

### What this means in practice

| Situation | Recommendation |
|-----------|----------------|
| a handful of documents | `backend="serial"` or the default; scheduling cannot pay for itself |
| CPU-bound corpus, tens of documents or more, plain files | `backend="process"`, `concurrency="cores"` |
| worker also does I/O (database, object store, embedding calls) | `backend="thread"`, `concurrency=3` or `auto` |
| callbacks needed (`save_image`, `progress_hook`, custom `length_fn`) | `backend="thread"` - processes cannot pickle them |
| shared machine, must not take every core | `pin_cores=True` with an explicit `concurrency`, or `max_concurrency` |

`pin_cores` was not benchmarked: binding a thread to a core does not create
parallelism where the GIL removed it, and for the process backend the OS already
places short-lived children sensibly. It exists for shared hosts where
predictable placement matters more than peak throughput.

## Not tried / not done

- **Rewriting the heading regexes in C.** The patterns use lookbehind and lookahead
  that would have to be reimplemented by hand. Given that regex time already fell
  from 2.39 s to 0.35 s, the remaining headroom does not justify the semantic risk.
- **Rewriting the rest of the pipeline in C.** `parse_to_tree` recursion,
  `result_tree_to_paragraph` and the table-header pass together account for well
  under a second of the profiled total. Optimising them would add a large C surface
  for a small gain.
- **A single-pass heading scanner replacing the per-level regex cascade.** This is
  the remaining order-of-magnitude gap against `langchain`'s character splitter
  (`re.Pattern.findall` is 62 ms of the 124 ms; see [BENCHMARK.md](BENCHMARK.md)).
  It would mean deriving the tree from one linear scan instead of six filtered
  regex passes - i.e. reimplementing the lookbehind/lookahead semantics of the six
  canonical patterns by hand, in the one function the fidelity guarantees depend
  on. Not worth the semantic risk for an ingest-time cost.

## Reproducing

```bash
pip install -e ".[dev]"
python scripts/benchmark.py            # timing table + profile
python scripts/benchmark.py --profile  # cProfile breakdown of the hot path
python scripts/benchmark.py --ab       # C vs pure-Python accelerator comparison
python scripts/benchmark.py --batch --documents 48   # scheduler: serial vs threads vs processes

# other libraries, same corpus (optional peers)
pip install chonkie langchain-text-splitters tiktoken
python scripts/benchmark_peers.py --reps 5 --json docs/benchmark_peers.json
```

`scripts/benchmark.py` builds the corpus described above from a fixed seed, so the
numbers are reproducible rather than incidental. Correctness of the optimisation is
covered by `tests/test_c_speedup.py` (superset premise + end-to-end equivalence) and
`tests/test_options_overlap.py`.
