# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] - 2026-09-30

Benchmark documentation and tooling only - **no library behaviour changes**. The
`smart_slice` package is byte-identical to 0.5.0 apart from the version string.

### Added

- **`scripts/benchmark_chroma.py` + `docs/benchmark_chroma.json`** - a one-off,
  archived run of the only authoritative public chunking benchmark: the framework
  published with the Chroma Technical Report *Evaluating Chunking Strategies for
  Retrieval* (Smith & Troynikov, 2024), package `chunking_evaluation` (MIT). Real
  data, not a synthetic corpus: 5 shipped corpora (1.45 MB), **472 questions with
  ground-truth reference spans**, recall / precision / IoU / `precision_omega`, and
  the frozen query embeddings that ship with the package. 26 method configurations
  scored, including smart-slice at four budgets, a merge-to-token-budget variant,
  the report's `FixedTokenChunker` / `RecursiveTokenChunker` /
  `ClusterSemanticChunker`, and `langchain` / `chonkie` adapters.
- **An oracle self-test in that script.** Before any method is reported it chunks
  the corpora into exactly the ground-truth spans and verifies the harness can find
  them: 790 references, exact 695, trailing-period 95, **mislocated 0**. If the
  oracle fails the script aborts rather than printing numbers.
- **A verbatim-tiling adapter.** The harness locates every chunk by searching for it
  in the source, which `slice_text` output is not (headings move into `title`). The
  adapter re-walks the paragraph bodies and emits spans that concatenate back to the
  source *exactly* - asserted with `"".join(chunks) == text` - so smart-slice can be
  scored without weakening its fidelity guarantee.

### Changed

- **`docs/BENCHMARK.md` restructured to lead with throughput**, which is the axis
  this library can actually move, and to state up front that retrieval recall is
  **not** a project KPI: scoring it needs an embedding model and a labelled query
  set, so the number says as much about the model as about the chunker. The Chroma
  run moved to a clearly-labelled archived section with its conclusion stated
  plainly - nothing dominates, plain fixed-size chunking at ~200 tokens led on
  recall (0.7806), smart-slice is mid-pack and size-competitive rather than ahead,
  HNSW retrieval noise is ~+-0.005, and the oracle ceiling on this dataset is
  0.8179.
- README / README_CN: the performance claim now leads with the release-over-release
  speed evidence (394 ms -> 124 ms, ~3.1x from 0.1.0 to 0.5.0, every step asserted
  output-identical, plus 1.61x from the batch scheduler on 48 docs / 26.5 MB) and
  points at the archived retrieval run as context rather than as a claim.

### Notes

- Two honest costs recorded for anyone re-running the Chroma benchmark:
  `ClusterSemanticChunker(max=400)` **did not terminate** (18 h wall clock, 6.1 h
  CPU, 775 MB RSS) and was killed, so only its `max=200` row is reported; and the
  harness's `RecursiveTokenChunker` inherits `length_function=len`, so its
  `chunk_size` is characters despite the class name.
- `sentence-transformers` (hence torch) could not be installed on the machine used -
  an application-control policy blocks `torch_python.dll` - so chromadb's ONNX
  `all-MiniLM-L6-v2` was substituted. The substitution was verified rather than
  assumed: cosine **1.000000** against the shipped frozen query embeddings (worst of
  40 sampled questions), which is why the frozen 384-d set could be reused as-is.
- The Meta-Chunking benchmarks (CRUD, LongBench, MultiHop-RAG, RAG-Bench) were
  assessed and not run: that harness scores end-to-end RAG answers through an LLM
  over datasets distributed on Google Drive, which measures answer quality rather
  than chunking.
## [0.5.0] - 2026-09-28

Two themes: **multimodal** - the pictures come out of the document and land next
to the paragraphs they illustrate, ready for a vision-language embedding model -
and **an honest horizontal benchmark** against the other chunking libraries,
published together with its raw data.

### Added

- **`smart_slice.multimodal`** - the picture layer, sitting beside the public API
  so no generated handler had to change:
  - `slice_multimodal(content, name, ...)` / `slice_path_multimodal(path, ...)` -
    one call returns a `MultimodalResult` holding `.paragraphs`, `.images`,
    `.groups` (paragraph + its pictures), `.unattached`, `.records()`,
    `.paired()`, `.stats()`, `.coverage` and `.summary()`;
  - `ImageCollector` - a ready-made, thread-safe `save_image` sink. It
    deduplicates by sha256 and returns the `{new_id: kept_id}` map the service
    layer already applies, so duplicate pictures collapse onto one id *and* every
    `./oss/file/{id}` reference in the paragraphs is rewritten to match;
  - `scan_media(content, name)` / `extract_media(...)` / `container_kind(...)` -
    open a container and recover its pictures without slicing it. Covers `zip`
    (OOXML `word|ppt|xl/media/*`, ODF `Pictures/*`, EPUB chapters, plain image
    archives), `pdf` (`page.images`, tagged with the page number), `mime`
    (`.eml` / `.mhtml`, indexed by `Content-ID` and `Content-Location` so `cid:`
    references resolve), `markup` (`<img src>` and `![]()`, `data:` URIs decoded)
    and standalone picture files. Returns a `MediaExtraction` report
    (`.assets`, `.kind`, `.scanned`, `.skipped`, `.errors`);
  - `attach_images(paragraphs, assets)` - joins pictures to paragraphs in four
    passes: the exact `./oss/file/{id}` reference, then the heading the picture
    sat under, then the ~96 characters of visible text preceding the tag, then
    "a standalone picture file is its own document". Whatever is left goes to
    `.unattached`, never dropped. Each placed image records how it matched in
    `meta["match"]`.
- **`smart_slice._images`** - dependency-free image primitives: MIME sniffing
  from magic bytes (PNG, JPEG, GIF, BMP, TIFF, WEBP, ICO, HEIC/HEIF, AVIF, SVG,
  EMF, WMF) and pixel dimensions read straight out of the headers, with Pillow
  only as an optional fallback. SVG detection requires `<svg>` to be the *root*
  element, so an EPUB chapter with an inline `<svg>` is not mistaken for a
  picture.
- **`ImageAsset` computed views** - `.size`, `.sha256`, `.mime_type`, `.suffix`,
  `.dimensions`, `.width`, `.height`, `.data_uri()` and `.to_dict(with_data=...)`,
  all cached on the instance. The constructor signature is unchanged, so every
  generated handler keeps working.
- **`slice_many(..., collect_images=True)`** - routes each job through
  `slice_multimodal` with one shared collector and publishes the batch's pictures
  on `BatchReport.images`; `report.summary()` mentions the count. Per-document
  ownership is tracked by the sink closure, so a thread pool cannot attribute one
  document's pictures to another. Combining it with `backend="process"` raises a
  `ValueError` instead of silently returning an empty list.
- **`smart-slice media` subcommand** - report (and with `--out-dir`, extract) the
  pictures in a document and the paragraph each belongs to: `--limit`,
  `--probe {auto,always,never}`, `--include-vector`, `--min-side`,
  `--no-external`, `--json`.
- **`scripts/benchmark_peers.py`, `docs/BENCHMARK.md` and
  `docs/benchmark_peers.json`** - the same 551 KB corpus run through
  `smart-slice`, `chonkie` 1.7.0 and `langchain-text-splitters` 1.1.2, with
  interleaved repetitions and five model-free structure metrics (word recall,
  natural-unit survival, heading context, broken code fences, orphaned table
  rows). Raw numbers are committed.
- **`examples/multimodal.py`** - eight offline scenarios, fixtures included.
- **83 new tests** (`tests/test_multimodal.py`), bringing the suite to 415. They
  build valid PNGs from the specification with `zlib`, so the multimodal suite
  needs neither Pillow nor a network. Fidelity cases assert
  `slice_multimodal(...).paragraphs == slice_bytes(...)` for every container.

### Changed

- `parse_title_level` memoises the heading level of each pattern against the
  pattern list itself (a single-entry identity memo plus a `len()` guard, the same
  shape as the existing `_SCAN_MEMO` / `_MASK_MEMO`). The cascade was asking
  `heading_level_of` 8,106 times per 551 KB document for seven possible answers.
  Isolated effect: **4.36 us -> 3.91 us per call (-10.3%)**, about 0.9 ms of the
  ~128 ms end-to-end pass - at this machine's noise floor, and reported as such in
  `docs/PERFORMANCE.md`. Output is byte-identical.
- README: the performance claim now states the honest trade (fastest structure,
  not fastest clock) and links the peer benchmark. The test badge reads 415.

### Notes and known limits

- **No network I/O, still.** A remote `<img src="https://...">` becomes an asset
  with zero bytes and `meta["src"]` set; downloading it is the caller's decision.
- **Vector formats are opt-in.** `include_vector` defaults to False: SVG, EMF and
  WMF are images to a document but not to a VL embedding model without a renderer.
- **Spreadsheet cell pictures.** The generated `xlsx` handler still emits nothing
  for cell images written by `openpyxl` (the drawing relationships it produces do
  not match what the handler walks). The probe recovers them from `xl/media/*`,
  so `slice_multimodal` returns them - as `.unattached`, since a cell anchor has
  no paragraph to attach to.
- **`.msg` (OLE) pictures are not recovered** by the probe; only what the handler
  itself emits. Same for legacy binary `.doc` / `.ppt` / `.wps`.
- `collect_images=True` and `slice_multimodal` hold image bytes in memory;
  `keep_content=False` records metadata only. `max_images` (default 512) and
  `max_image_bytes` (default 64 MiB) cap a single document.
## [0.4.0] - 2026-09-28

One theme: **batch scheduling**. Slicing a single document was already fast;
ingesting a corpus meant writing your own pool. The concurrency-allocation layer
that sat under the source platform's smart-slicing path is now part of the
library, and every knob is a parameter you can pass at slice time.

### Added

- **`smart_slice.scheduler`** - the batch scheduler, with the semantics the
  source platform's multi-file slicing path was built on:
  - `slice_many(inputs, ...)` / `slice_paths(paths, ...)` - slice a corpus in one
    call and get a `BatchReport` back;
  - `run_parallel(items, worker, ...)` - the generic ordered parallel map under
    it, for callers who want the scheduling without the slicing;
  - `SchedulerPolicy` - the reusable configuration object (`concurrency`,
    `backend`, `pin_cores`, `error_policy`, `ordered`, `timeout`,
    `max_concurrency`, `task_setup`/`task_teardown`), plus `use_policy()` for
    contextvar scoping;
  - `SliceJob` / `as_slice_jobs` - one batch can mix paths, `(name, bytes)`
    pairs, mappings, file-like upload handles and ready-made jobs.
- **Derived width instead of a hard-coded one.** `available_cores()` reports the
  cores this process may actually use (Linux affinity mask, `process_cpu_count`
  elsewhere), and `auto_concurrency()` applies the source platform's allocation
  rule: 3 workers above six cores, half the cores below that, never fewer than 1.
  Accepted wherever a width is wanted: `4`, `"auto"`, `"cores"`, `"serial"`,
  `"2x"`, `"0.5x"`.
- **Core allocation (`pin_cores=True`).** Tasks are bound to one core each,
  round-robin over the process mask, and the mask is restored when the task ends
  - including when it fails. The source platform did this with
  `sched_setaffinity` and documented Windows as unsupported; here the same
  allocation also runs on Windows via `SetThreadAffinityMask` (with correct
  64-bit prototypes - the default `ctypes` `restype` truncates the handle and
  silently turns pinning into a no-op), and degrades to a logged no-op on macOS.
- **Process backend** (`backend="process"`, `spawn` context) for CPU-bound
  corpora, alongside the default thread backend and `backend="serial"`.
- **Environment overrides**, resolved per call: `SMART_SLICE_CONCURRENCY`,
  `SMART_SLICE_MAX_CONCURRENCY`, `SMART_SLICE_SCHEDULER_BACKEND`,
  `SMART_SLICE_PIN_CORES`.
- **CLI**: `smart-slice batch a.pdf b.docx -j 4 --backend process --pin-cores
  --error-policy collect --format jsonl`, and `smart-slice cores` to show the
  detected cores, derived width, affinity mask and pinning support.
- **`scripts/benchmark.py --batch`** - reproduces the backend/width table below.

### Changed

- `docs/PERFORMANCE.md` no longer lists parallelism under "not tried": it is
  shipped, with the measurements that say when it pays (spoiler: threads do not
  speed up pure-Python slicing; processes do, from a few dozen documents up).

### Fixed

- **`SliceError` and its subclasses could not be pickled.** `Exception.__reduce__`
  replays `args`, which for these classes is only `(message,)`, so
  reconstruction raised `TypeError` and dropped `code`. Each now defines
  `__reduce__`, which is what lets a slicing failure travel back from a process
  worker as the same type with the same code.
- **Ambient `ChunkingOptions` were silently dropped inside worker threads.** A
  new thread starts with an empty contextvar context, so a batch run under
  `with use_options(opts):` would have sliced with the defaults. `slice_many`
  now captures the ambient options and passes them explicitly; explicit
  `limit=`/`overlap=`/`options=` keywords still win.
- **The CLI could not be configured through the environment.** `--concurrency`
  defaulted to the literal `"auto"`, which outranked `SMART_SLICE_CONCURRENCY`
  and made the variable dead for every CLI user. It now defaults to unset.
- **A failed batch dumped a traceback.** The CLI collects outcomes internally and
  prints one line naming the document, its message and its code, then exits 1.

### Measured (12 cores, CPython 3.12, Windows; 551 KB structured markdown each)

| Batch | serial | threads x4 | processes x4 |
|-------|--------|------------|--------------|
| 12 documents (6.6 MB) | 2619 ms (1.00x) | 2455 ms (1.07x) | 3790 ms (0.69x) |
| 48 documents (26.5 MB) | 10056 ms (1.00x) | 10615 ms (0.95x) | 6255 ms (**1.61x**) |

Read this before choosing a backend: the slicer is pure Python, so threads only
overlap the GIL-releasing work inside parsers (zip inflate, image decode, OCR)
and the caller's own I/O - which is exactly the case the source platform tuned
its width of 3 for, since each of its tasks also wrote to a database and
dispatched embedding jobs. For a CPU-bound corpus, `backend="process"` is what
pays, and only once the batch is big enough to amortise interpreter startup and
pickling. Threads stay the default because they accept callbacks (`save_image`,
`progress_hook`, a tokenizer in `length_fn`) and have no startup cost.

## [0.3.0] - 2026-09-27

Two themes: a **correctness fix for the published wheel** (a clean install could not
even `import smart_slice`) and a **broad format expansion** (78 -> 197 declared
extensions, 22 -> 30 handlers).

### Fixed

- **The wheel crashed on import in a clean environment.** 34 module-level imports of
  optional parsers (`py7zr`, `python-docx`, `openpyxl`, `pypdf`, `markdownify`, ...)
  were unguarded, so `pip install smart-slice` (core only) raised
  `ModuleNotFoundError` at `import smart_slice` - directly contradicting the documented
  "a handler whose parser is missing simply reports unsupported instead of crashing".
  Every optional import is now wrapped, and each handler's `support()` returns False
  when its parser is absent, so the format degrades to `400 Unsupported file format:
  .pdf` instead of breaking the package. Verified in a bare venv with only
  `charset-normalizer` installed.
- **Legacy binary `.doc` (OLE compound file) reported `500` instead of `400`.**
  `DocSplitHandle` claimed `.doc` by extension, then handed it to `python-docx`, which
  raised `BadZipFile`. A binary `.doc` is now detected by its OLE magic and rejected
  with `400 ... convert the document to .docx first`, matching the existing
  `.wps`/`.et` legacy-binary behaviour. `DocSplitHandle.handle` also gained the
  `except (SliceError, ResourceLimitError): raise` passthrough the other handlers have,
  so a 400 is no longer swallowed and re-raised as 500.
- **Source/config extensions were silently mis-sliced.** `.java`, `.go`, `.c`, `.rs`,
  `.properties`, `.adoc`, `.diff` and ~50 more were only reachable through the
  "decodes as text" fallback, which used the *markdown* heading patterns - so a
  `# comment` line in source code was extracted as a section title. They are now first-
  class members of `TEXT_EXTENSIONS`, which routes them to `LITERAL_EXTENSIONS`
  (blank-line/length splitting only); a `#` in code stays in the body.
- **mbox bodies were dropped.** `mailbox.mbox` builds legacy `Compat32` messages that
  have no `get_content()`, so every body part failed and was skipped. The mailbox is now
  parsed with `policy=email.policy.default` (matching `EmlSplitHandle`); multi-message
  archives produce one correctly-titled paragraph per message.

### Added

- **8 new handlers**, all pure-stdlib so they work even in a bare install:
  - `SvgSplitHandle` - `.svg` `.svgz` (gzip-aware): extracts visible `<text>`/`<tspan>`,
    drops `<script>`/`<style>`/`<defs>`.
  - `IpynbSplitHandle` - `.ipynb`: markdown cells kept, code cells fenced (so their `#`
    comments are not mistaken for headings), `text/plain` outputs collected.
  - `SubtitleSplitHandle` - `.srt` `.vtt` `.ass` `.ssa` `.sub`: strips indices,
    timecodes and ASS `Script Info`/`Format` metadata, keeps only cue text.
  - `Fb2SplitHandle` - `.fb2` `.fb2.zip`: FictionBook XML; section titles map to
    Markdown headings. Registered *before* `ZipSplitHandle` so `.fb2.zip` is not treated
    as a generic archive.
  - `MboxSplitHandle` - `.mbox`: per-message subject + body.
  - `VcalendarSplitHandle` / `VcardSplitHandle` - `.ics` `.ifb` / `.vcf`: unfold
    RFC 5545/6350 lines, keep only human-readable fields (event summary -> title;
    time/location/description, or name/phone/email/address -> key/value rows).
  - `ExtendedImageSplitHandle` - 25 more Pillow-decodable raster containers
    (`.ico` `.tga` `.pcx` `.dds` `.sgi` `.ppm`/`.pgm`/`.pbm` `.im` `.icns` `.qoi`
    `.jfif` `.apng` `.xbm` `.psd` + aliases). Subclasses `ImageSplitHandle`, so the OCR
    branch and `save_image` contract are inherited unchanged.
- **OOXML variants that were declared but rejected now parse:** `.pptm` `.ppsm` `.potm`
  (added to the presentation content-type map) and `.dotx` `.dotm` `.xltx` `.xltm`
  (declared and claimed). `.ppsx`/`.potx` content types were also completed.
- **`.tar.xz` / `.txz`** (stdlib `tarfile` already supported xz), **`.azw1`/`.azw4`/
  `.prc`** Kindle variants, **`.xhtml`/`.shtml`**, **`.tab`** (CSV), **`.xlt`** (binary
  Excel template) are now recognised and declared.
- **`smart_slice._optional`** - single source of truth mapping each optional import to
  its pip extra, powering both the `support()` gates and the "run pip install
  smart-slice[office] to enable it" error text.
- Tests: `tests/test_format_expansion.py` (32 cases) covering every new handler, the
  bare-environment degradation, the source-fidelity fix, and the OOXML variant
  declarations. Suite is now 216 tests.

### Changed

- `HANDLER_EXTENSIONS` grew from 78 to 197 declared extensions across 30 handlers;
  README / README_CN format tables were regenerated from the live registry and now
  include a parser column and an explicit "not supported (400)" table.
- The zip/tar/7z **inner-file** dispatch list was extended with the new structured
  text handlers (`Fb2`, `Svg`, `Ipynb`, `Subtitle`, `Mbox`, `Vcalendar`, `Vcard`), so
  those formats are also recognised inside archives. Image handlers stay out of that
  list on purpose: excluding `ImageSplitHandle` from archive members is a deliberate
  Phase 2-B decision (embedded images travel through the zip handler's own markdown
  reference collection + `save_image` path), pinned by
  `test_zip_inner_images_do_not_receive_extractor`. Adding it back was attempted and
  reverted; changing embedded-image semantics needs its own reviewed change.

## [0.2.0] - 2026-09-27

Adds configurable chunking (size **and** overlap) in both default and fully
user-controlled modes, plus a substantial performance pass on the slicing engine.

### Added

- **`ChunkingOptions`** (`smart_slice.options`) - one immutable object describing
  chunk size and overlap behaviour: `limit`, `overlap`, `overlap_ratio`,
  `boundary`, `lookback`, `overlap_boundary`, `min_chunk`, `carry_title`,
  `overlap_within_section`, and `length_fn` (measure size in tokens instead of
  characters by passing a tokenizer).
- **Paragraph overlap** - consecutive paragraphs can share trailing context.
  Exposed at every entry point as `overlap=` / `overlap_ratio=` / `options=`,
  with `overlap=0` (the default) preserving the previous exact-tiling behaviour.
- **`apply_paragraph_overlap`** - overlap is applied as a distinct pass *after*
  the heading tree is assembled, never inside it (see Fixed for why). It is purely
  additive: no paragraph's own text is altered, dropped or reordered.
- **Second-stage chunk overlap** - `chunk_paragraphs(..., chunk_overlap=,
  chunk_overlap_ratio=)` and `OverlapChunkHandle`, so embedding chunks can also
  carry context.
- **`carry_title`** - prefix *every* chunk with its paragraph's heading chain, so
  section context survives chunking instead of appearing only in the first chunk.
- **`overlap_within_section`** - restrict overlap to paragraphs inside the same
  heading chain.
- **CLI**: `slice --overlap N`, `--overlap-ratio F`, `--overlap-section-only`; a
  new `chunk` subcommand (`--chunk-size`, `--chunk-overlap`, `--carry-title`).
- **Optional C accelerator** (`csrc/_speedup.c`, extra `accel`) - a single-pass
  heading scan. Purely an optimisation: `smart_slice._speedup_py` provides an
  identical pure-Python scan and is used whenever the extension is absent.
- **`scripts/build_ext.py`** - builds the extension in place, using `zig cc` on
  Windows where setuptools ignores `CC` and insists on `cl.exe`.
- Tests: `tests/test_options_overlap.py` (45) and `tests/test_c_speedup.py` (19),
  including the equivalence property that the heading prefilter never changes
  slicing output.

### Changed

- **Performance: ~2.7x faster slicing** on a 550 KB structured corpus
  (394 ms -> 144 ms; 1.4 -> 3.8 MB/s), from algorithmic fixes rather than the C
  extension:
  - `parse_title_level` no longer runs up to six whole-document regex scans per
    recursion level. One linear scan establishes which heading levels are present
    and provably-empty levels are skipped. Regex calls: 48,636 -> 11,604 per
    document.
  - `mask_code_blocks` returns the input unchanged when there is no code fence
    (previously it always allocated a full character list and re-joined it).
    Masking calls: 97,272 -> 11,604.
  - Masked text is memoised across heading levels for the same block.
  - Compiled patterns bypass `re.findall`'s dispatch, removing 194,544
    `re._compile` cache lookups per document.
  - `re_findall` flattens match groups in one pass instead of
    `reduce([*x, *y], ...)`, which was O(n^2) in the number of matches.
  - Total Python calls per document: 4.33M -> 1.55M (-64%).
- The C extension adds ~4% on top of that (139 vs 145 ms end-to-end), because the
  algorithmic pass had already removed most of the scan cost. Measured, not
  assumed - the extension is worth having for heading-dense documents but is not
  where the win came from.
- `smart_slice/chunker.py` **graduated from generated to first-party source**:
  chunking policy now lives there, so it is no longer regenerated from the source
  platform. Documented in `docs/PORTING.md`; `CONTRIBUTING.md` explains which
  files are generated and which are editable.
- Sentence boundaries now include the fullwidth `！` (U+FF01) and `？` (U+FF1F).
  The previous implementation listed ASCII `!` and `?` twice, so CJK text ending in
  a fullwidth mark was never treated as a sentence end (see Fixed).

### Fixed

- **Overlap inside the heading tree corrupted paragraph boundaries.** `parse_to_tree`
  recovers block positions with `str.index()` over produced content, so chunks must
  stay disjoint substrings of the source; overlapping them makes the lookup
  ambiguous and silently reshuffles boundaries (symptom: identical total character
  count, different paragraph split). Overlap is therefore applied after assembly.
- **Fullwidth `！`/`？` were not sentence boundaries.** The source `split_chars`
  list contained ASCII `!` and `?` twice each, never U+FF01/U+FF1F, despite the
  comment claiming "中英文感叹号/问号". Corrected, with a regression test.
- **`post_handler_paragraph` raised `NameError`.** It called `functools.reduce`,
  which the module never imported. The function had no callers in the source
  platform or in this package, so the defect stayed latent; the `reduce` flatten
  (O(n^2)) is now a single pass and the two `if len(x) > 4096: pass` no-op
  statements are gone.
- **`ChunkingOptions.with_(overlap_ratio=...)` was a no-op.** `__post_init__`
  materialised `overlap=None` to `0`, which then outranked the ratio. `overlap`
  keeps its `None` sentinel and resolution happens in `effective_overlap`.
- **`carry_title` only prefixed the first chunk of a paragraph.** It now prefixes
  every chunk, which is the point of the option.
- **Importing the package mutated global Pillow state.** `_xlsx_images` set
  `ImageFile.LOAD_TRUNCATED_IMAGES = True` and `Image.MAX_IMAGE_PIXELS = None` at
  import time, silently disabling the *host process's* decompression-bomb guard.
  Both are now scoped to `_permissive_pil()` around the image parse and restored
  afterwards.

## [0.1.0] - 2026-09-26

Initial public release, extracted from an enterprise agent platform's document
ingestion layer. The parsing and slicing behaviour is carried over verbatim; the
web-framework coupling is removed.

### Added

- **Slicing engine** (`smart_slice.chunker`): heading-tree parsing, blank-line and
  sentence-boundary splitting, a length budget (`limit`), heading-chain `title`
  output, fidelity-preserving cleaning (line-start, code-fence-aware heading
  marker removal), markdown table header re-insertion across cut boundaries, and
  oversized-row re-splitting.
- **~30 format handlers** dispatched by a single ordered registry
  (`SPLIT_HANDLERS`): HTML, MHTML, DOCX/DOCM, PDF, XLSX/XLSM, XLS, CSV/TSV, ZIP,
  XMind, PPTX family, PPT, WPS/ET, RTF, ODF family, EPUB, EML, MSG, MOBI/AZW,
  TAR family, 7z, images, and a text/source-code fallback.
- **Public API**: `slice_text`, `slice_bytes`, `slice_path`, `extract_text`,
  `detect_handler`, `supported_extensions`, `missing_dependencies`,
  `chunk_paragraphs`, `chunk`, plus the low-level `split_document`.
- **Command line interface** (`smart-slice` / `python -m smart_slice`) with
  `slice`, `detect`, and `formats` subcommands and json/jsonl/text/md output.
- **QA and table parsers** (`smart_slice.qa`): question/answer extraction from
  markdown, CSV and spreadsheets, and header-aware table row expansion.
- **Embedding chunking** (`smart_slice.chunking`): second-stage fixed-size
  chunking of already-sliced paragraphs.
- **Image pipeline**: images extracted from documents are delivered through a
  `save_image` callback as `ImageAsset` objects, with reference remapping for
  deduplication and an optional local-OCR text-extraction hook.
- **Defensive parsing guards** (`ParserLimits`): byte, XML node/depth, archive
  member, MIME part/depth and table-cell caps, configurable via
  `SMART_SLICE_PARSER_*` environment variables.
- **Optional-dependency model**: a minimal core install plus per-format extras;
  handlers degrade to "unsupported" rather than crashing when a parser is absent.
- **Test suite**: the source platform's slicing tests ported to run standalone
  plus public-API and CLI tests.

### Notes

- Local image OCR is disabled by default; set `SMART_SLICE_OCR_ENABLED=1` and
  install the `ocr` extra to enable it.
- Licensed under GPL-3.0, matching the upstream platform.

[0.5.1]: https://github.com/guangxiangdebizi/smart-slice/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/guangxiangdebizi/smart-slice/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/guangxiangdebizi/smart-slice/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/guangxiangdebizi/smart-slice/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/guangxiangdebizi/smart-slice/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/guangxiangdebizi/smart-slice/releases/tag/v0.1.0