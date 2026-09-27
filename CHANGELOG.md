# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/guangxiangdebizi/smart-slice/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/guangxiangdebizi/smart-slice/releases/tag/v0.1.0