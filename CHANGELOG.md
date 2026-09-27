# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-26

Initial public release, extracted from an enterprise agent platform's document
ingestion layer. The parsing and slicing behaviour is carried over verbatim
the web-framework coupling is removed.

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
  (119 passed, 1 skipped) plus public-API and CLI tests.

### Notes

- Local image OCR is disabled by default; set `SMART_SLICE_OCR_ENABLED=1` and
  install the `ocr` extra to enable it.
- Licensed under GPL-3.0, matching the upstream platform.

[0.1.0]: https://github.com/smart-slice/smart-slice/releases/tag/v0.1.0