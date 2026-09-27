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

**2.8x faster overall.** Roughly 96% of that gain came from algorithmic fixes in
Python; the C extension contributed about 4%.

Machine: laptop CPU, CPython 3.12.10, Windows, single-threaded, 8-12 repetitions
per measurement after one warm-up call.

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

## Not tried / not done

- **Parallelism.** Slicing is CPU-bound pure Python and would release the GIL only
  with multiprocessing. Callers can trivially parallelise across documents
  themselves, which is where the real throughput is for bulk ingestion; doing it
  inside the library would impose a process/thread model on every consumer.
- **Rewriting the heading regexes in C.** The patterns use lookbehind and lookahead
  that would have to be reimplemented by hand. Given that regex time already fell
  from 2.39 s to 0.35 s, the remaining headroom does not justify the semantic risk.
- **Rewriting the rest of the pipeline in C.** `parse_to_tree` recursion,
  `result_tree_to_paragraph` and the table-header pass together account for well
  under a second of the profiled total. Optimising them would add a large C surface
  for a small gain.

## Reproducing

```bash
pip install -e ".[dev]"
python scripts/benchmark.py            # timing table + profile
python scripts/benchmark.py --profile  # cProfile breakdown of the hot path
python scripts/benchmark.py --ab       # C vs pure-Python accelerator comparison
```

`scripts/benchmark.py` builds the corpus described above from a fixed seed, so the
numbers are reproducible rather than incidental. Correctness of the optimisation is
covered by `tests/test_c_speedup.py` (superset premise + end-to-end equivalence) and
`tests/test_options_overlap.py`.
