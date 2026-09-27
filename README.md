# smart-slice

**Fidelity-first document slicing for RAG pipelines.** Feed it 197 file extensions across 30 handlers, get back retrieval-ready paragraphs that keep every original character reachable.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-220%20passed-brightgreen.svg)]()
[![PyPI version](https://img.shields.io/pypi/v/smart-slice.svg)](https://pypi.org/project/smart-slice/)

```python
from smart_slice import slice_text

paragraphs = slice_text("# Chapter\n\nbody text", limit=1000)
# [{'title': 'Chapter', 'content': 'body text'}]
```

---

## Why another splitter?

Most text splitters treat a document as a bag of characters and chop it on a fixed grid. That loses the two things retrieval actually depends on: **where a passage sits in the document's structure**, and **the passage's exact original wording**. `smart-slice` is built around three rules.

### 1. Fidelity before cleverness

No cleaning step deletes source text.

- Heading markers (`#`) are stripped **only at a line start** and **only outside ` ``` ` code fences** — a `#RRGGBB` colour or a Python comment in a code block survives.
- A markdown table cut by the length budget gets its **header row appended** to the continuation chunk, not rewritten into it. The original rows are untouched.
- An oversized spreadsheet row is **re-split**, never truncated.

### 2. Structure before length

`limit` is a *budget*, not a grid. The slicer walks the document's own shape first:

1. the markdown heading tree (levels 1–6),
2. then blank-line paragraph breaks,
3. then sentence boundaries (`。 . ! ！ ? ？`),
4. and only as a last resort, a hard character cut.

Each paragraph carries a `title` field holding its **heading chain** (`"Part I  Chapter 3"`), so downstream chunks keep their section context.

### 3. Measurably fast

Slicing a 550 KB structured document takes ~140 ms (≈3.9 MB/s) on a laptop CPU.
The hot path is a single linear scan that decides which heading levels a block can
possibly contain, so provably-empty regex passes are skipped; an optional C
extension (`pip install smart-slice[accel]`) runs that scan natively. Numbers and
the reasoning are in [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md).

### 4. No framework

A pure library: no Django, no ORM, no network calls, no logging configuration (it emits records on the `smart_slice` logger and leaves routing to you). Errors are `SliceError` with an HTTP-style `.code` (`400` = input problem, `500` = parse failure). Images pulled out of a document are handed to a `save_image` **callback** — the library never persists anything.

---

## Installation

The core install is deliberately tiny (just a text decoder). Format parsers are optional extras, and a handler whose parser is missing simply reports "unsupported" instead of crashing.

```bash
pip install smart-slice                 # core: text/markdown/config/source files
pip install smart-slice[all]            # every format below
```

Published on PyPI as [`smart-slice`](https://pypi.org/project/smart-slice/). The
`0.3.0` wheel and sdist are also attached to the
[GitHub Release](https://github.com/guangxiangdebizi/smart-slice/releases/tag/v0.3.0),
built from the tagged commit by `.github/workflows/release.yml`.

Targeted extras:

| Extra | Unlocks |
|-------|---------|
| `markup` | HTML, MHTML, EPUB, EML, MSG, MOBI (markdownify + bs4) |
| `office` | DOCX, PPTX, PPT, XLSX, XLS, WPS, ET, RTF, ODF |
| `pdf` | PDF (pypdf + Pillow) |
| `mail` | MSG (extract-msg) |
| `archive` | 7z (zip/tar are stdlib) |
| `ebook` | MOBI/AZW |
| `image` | HEIC/HEIF images |
| `ocr` | local OCR for embedded images (RapidOCR, opt-in at runtime) |
| `keywords` | jieba-backed keyword helpers |
| `fastuuid` | time-sortable image ids (a pure-Python fallback is built in) |
| `test` | everything in `all` plus `xlwt`, which the test suite needs to *write* legacy `.xls` fixtures (`xlrd` only reads them) |
| `dev` | `test` + accel, pytest, pytest-cov, ruff, mypy |

Call `smart_slice.missing_dependencies()` at runtime to see exactly which extras would unlock more formats.

---

## Quickstart

### Slice a string

```python
from smart_slice import slice_text

rows = slice_text("# Guide\n\nIntro text.\n\n## Setup\n\nStep one.", limit=1000)
for r in rows:
    print(r["title"], "->", r["content"][:40])
# Guide -> Intro text.
# Guide  Setup -> Step one.
```

### Slice a file from disk

```python
from smart_slice import slice_path

rows = slice_path("report.pdf", limit=1000)        # name decides the handler
```

### Slice from bytes (with image capture)

```python
from smart_slice import slice_bytes, ImageAsset

images = []
def save_image(assets):           # assets: list[ImageAsset]
    images.extend(assets)

rows = slice_bytes(raw_bytes, "deck.pptx", limit=1000, save_image=save_image)
# each ImageAsset has .id, .file_name, .content (bytes); the paragraph text
# already references it as ![name](./oss/file/{id})
```

### Output shape

Every entry point returns a list of `{"title": str, "content": str}`:

```python
[
    {"title": "Part I  Chapter 3", "content": "the paragraph body..."},
    ...
]
```

`title` is the heading chain (empty when the document has no headings); `content` is the paragraph's verbatim text.

---

## Supported formats

30 handlers covering 197 declared extensions. Every parser except `charset-normalizer` (text decoding) is an **optional extra**: a handler whose parser is missing reports `400 Unsupported file format` instead of breaking `import smart_slice`.

| Category | Handler | Extensions | Parser |
|----------|---------|-----------|--------|
| Web | `HTMLSplitHandle` | `.html` `.htm` `.xhtml` `.shtml` | markdownify + bs4 |
| Web | `MhtmlSplitHandle` | `.mhtml` `.mht` | markdownify |
| Office | `DocSplitHandle` | `.docx` `.docm` `.doc` `.dotx` `.dotm` | python-docx |
| PDF | `PdfSplitHandle` | `.pdf` | pypdf + Pillow |
| Spreadsheet | `XlsxSplitHandle` | `.xlsx` `.xlsm` `.xltx` `.xltm` | openpyxl |
| Spreadsheet | `XlsSplitHandle` | `.xls` | xlrd |
| Spreadsheet | `CsvSplitHandle` | `.csv` `.tsv` `.tab` | stdlib csv |
| Archive | `ZipSplitHandle` | `.zip` | stdlib zipfile |
| Mind map | `XmindSplitHandle` | `.xmind` | stdlib zipfile + json |
| Office | `PptxSplitHandle` | `.pptx` `.pptm` `.ppsx` `.ppsm` `.potx` `.potm` | python-pptx |
| Office | `PptSplitHandle` | `.ppt` `.dps` | olefile / python-pptx |
| Office | `WpsSplitHandle` | `.wps` `.et` | python-docx / openpyxl |
| Office | `RtfSplitHandle` | `.rtf` | striprtf |
| Office | `OdfSplitHandle` | `.odt` `.ods` `.odp` `.fodt` `.fods` `.fodp` | stdlib zipfile + xml |
| Ebook | `EpubSplitHandle` | `.epub` | stdlib zipfile + bs4 |
| Mail | `EmlSplitHandle` | `.eml` | stdlib email + bs4 |
| Mail | `MsgSplitHandle` | `.msg` | extract-msg + bs4 |
| Ebook | `MobiSplitHandle` | `.mobi` `.azw` `.azw1` `.azw3` `.azw4` `.prc` | mobi + bs4 |
| Archive | `TarSplitHandle` | `.tar` `.tar.gz` `.tgz` `.tar.bz2` `.tar.xz` `.txz` | stdlib tarfile |
| Archive | `SevenZipSplitHandle` | `.7z` | py7zr |
| Image | `ImageSplitHandle` | `.jpg` `.jpeg` `.png` `.gif` `.bmp` `.tiff` `.tif` `.webp` `.heic` `.heif` | Pillow (+ pillow-heif) |
| Ebook | `Fb2SplitHandle` | `.fb2` `.fb2.zip` | stdlib zipfile + xml |
| Vector / structured | `SvgSplitHandle` | `.svg` `.svgz` | stdlib xml |
| Vector / structured | `IpynbSplitHandle` | `.ipynb` | stdlib json |
| Vector / structured | `SubtitleSplitHandle` | `.srt` `.vtt` `.ass` `.ssa` `.sub` | stdlib re |
| Mail | `MboxSplitHandle` | `.mbox` | stdlib mailbox |
| Vector / structured | `VcalendarSplitHandle` | `.ics` `.ifb` | stdlib |
| Vector / structured | `VcardSplitHandle` | `.vcf` | stdlib |
| Image | `ExtendedImageSplitHandle` | `.ico` `.cur` `.tga` `.pcx` `.dds` `.sgi` `.ppm` `.pgm` `.pbm` `.pnm` `.pfm` `.im` `.icns` `.qoi` `.jfif` `.jpe` `.apng` `.xbm` `.psd` (+ aliases) | Pillow |
| Text / source | `TextSplitHandle` | `.txt` `.md` `.markdown` `.log` `.json` `.jsonl` `.ndjson` `.yaml` `.yml` `.toml` `.ini` `.cfg` `.conf` `.rst` `.tex` `.sql` and 60+ source/config extensions (`.py` `.js` `.ts` `.java` `.go` `.c` `.cpp` `.rs` `.rb` `.php` `.cs` `.swift` `.kt` `.scala` `.lua` `.r` `.sh` `.ps1` `.css` `.scss` `.xml` `.proto` `.graphql` `.adoc` `.org` `.diff` `.patch` `.po` `.properties` `.env` ...) | charset-normalizer |

Archives are unpacked recursively; each inner file goes through the same dispatch. The text handler is the fallback for any decodable content whose extension is not otherwise claimed.

### Not supported (rejected with `400`)

These are deliberately rejected rather than force-decoded into garbage:

| Category | Extensions | Why |
|----------|-----------|-----|
| Video | `.mp4` `.avi` `.mov` `.mkv` `.flv` `.wmv` `.webm` `.mpeg` `.mpg` `.3gp` `.rmvb` | no transcription pipeline |
| Audio | `.mp3` `.wav` `.flac` `.aac` `.ogg` `.m4a` `.wma` `.opus` `.alac` `.aiff` `.amr` | no ASR |
| Archive | `.rar` | no pure-Python decoder (rarfile needs the system `unrar` binary) |
| Apple iWork | `.key` `.pages` `.numbers` | proprietary IWA container |
| Legacy binary Office | old OLE `.wps` / `.et` / `.doc` | only the OOXML/zip variants are parsed; convert to `.docx` |
| Executable / image | `.exe` `.dll` `.msi` `.dmg` `.apk` `.iso` | not documents |
| Vector / raw photo | `.raw` | no universal decoder (vendor-private) |

**Content-level rejections** (right extension, unparsable content): encrypted PDFs and
image-only/scanned PDFs with no extractable text raise `500` naming the cause; encrypted or
oversized archives raise `ResourceLimitError`. Local OCR is **off by default**
(`SMART_SLICE_OCR_ENABLED=0`); enable it with the `ocr` extra to recover text from embedded
and scanned images.

---

## Command line

```bash
smart-slice slice report.pdf --limit 1000 --format json -o out.json
smart-slice slice notes.md --overlap 150 --stats          # 15% overlap on a 1000-char budget
smart-slice slice notes.md --overlap-ratio 0.15 --format text
smart-slice chunk notes.md --chunk-size 256 --chunk-overlap 40 --carry-title
smart-slice detect mystery.bin
smart-slice formats            # list handlers + missing optional deps
python -m smart_slice slice notes.md --format md   # module form
```

- `slice` — `--format {json,jsonl,text,md}`, `--limit`, `--overlap`, `--overlap-ratio`, `--overlap-section-only`, `--no-filter`, `--title-prefix`, `-o/--output`, `--stats` (summary to stderr).
- `chunk` — slices first, then cuts paragraphs to an embedding window: `--chunk-size`, `--chunk-overlap`, `--carry-title`, `-o/--output`, `--stats`. Emits one JSON object per chunk.
- `detect` — prints the handler class that would claim the file (exit 2 if none).
- `formats` — supported extensions and which extras are not installed (`--json` for machines).

---

## Advanced usage

### Chunk size and overlap

Both stages default to sensible values and both are fully configurable — pass
nothing for defaults, or pass numbers/objects for control.

| Stage | Knob | Default | Customise |
|-------|------|---------|-----------|
| slicing (paragraphs) | `limit` | `1000` chars | `limit=800` |
| slicing (paragraphs) | `overlap` | `0` (none) | `overlap=150` or `overlap_ratio=0.15` |
| chunking (embeddings) | `chunk_size` | `256` chars | `chunk_size=512` |
| chunking (embeddings) | `chunk_overlap` | `0` (none) | `chunk_overlap=40` |

```python
from smart_slice import slice_bytes, chunk_paragraphs, ChunkingOptions

# defaults: 1000-char paragraphs, no overlap
rows = slice_bytes(data, "doc.md")

# custom: 800-char paragraphs carrying 15% of context forward
rows = slice_bytes(data, "doc.md", limit=800, overlap_ratio=0.15)

# reuse one configuration across many documents
opts = ChunkingOptions(limit=600, overlap=90, carry_title=True)
for name, blob in documents.items():
    rows = slice_bytes(blob, name, options=opts)

# second stage: embedding chunks with their own overlap
chunks = chunk_paragraphs(rows, chunk_size=256, chunk_overlap=40, carry_title=True)
```

**What overlap does to the output.** Paragraph *count* and *titles* are unchanged;
each paragraph simply gains the previous paragraph's tail:

```python
plain    = slice_bytes(data, "doc.md", limit=500)                # 42 paragraphs
overlapped = slice_bytes(data, "doc.md", limit=500, overlap=100) # 42 paragraphs
assert len(plain) == len(overlapped)
assert plain[0]["content"] in overlapped[0]["content"]   # additive, never lossy
```

With `overlap=0` (the default) chunks tile the source exactly once, so
concatenating them reproduces the original text. A positive overlap trades that
strict tiling property for cross-boundary retrieval recall — the right choice when
an answer spans a cut point, the wrong one when you need to reconstruct the
document from its chunks.

`ChunkingOptions` fields:

| Field | Default | Meaning |
|-------|---------|---------|
| `limit` | `1000` | maximum chunk size, measured with `length_fn` |
| `overlap` | `None` | characters of shared context; clamped to `limit // 2` |
| `overlap_ratio` | `None` | `overlap` as a fraction of `limit` (`0.15` = 15%) |
| `boundary` | `True` | snap cuts to sentence ends instead of cutting mid-sentence |
| `lookback` | `0.5` | fraction of `limit` searched backwards for a boundary |
| `overlap_boundary` | `True` | start carried context at a sentence/whitespace edge |
| `overlap_within_section` | `False` | only carry context inside one heading chain |
| `min_chunk` | `0` | merge chunks shorter than this into the previous one |
| `carry_title` | `False` | prefix **every** chunk with its heading chain |
| `length_fn` | `len` | size metric — pass a tokenizer to budget in tokens |

Token-based budgeting needs no extra plumbing:

```python
import tiktoken
enc = tiktoken.encoding_for_model("gpt-4o-mini")
opts = ChunkingOptions(limit=400, overlap=60, length_fn=lambda s: len(enc.encode(s)))
rows = slice_bytes(data, "doc.md", options=opts)   # 400-token paragraphs
```

### Custom heading patterns

```python
import re
from smart_slice import slice_bytes, MARKDOWN_HEADINGS, BLANK_LINE

# slice on a bespoke "SECTION n:" scheme, then blank lines
patterns = [re.compile(r"(?m)^SECTION \d+:.*"), *BLANK_LINE]
rows = slice_bytes(data, "spec.txt", limit=800, patterns=patterns)
```

### Raw (non-normalised) structure

`normalize=False` returns the handler's native shape — nested groups for multi-sheet workbooks and archives, which preview UIs want:

```python
raw = slice_bytes(data, "book.xlsx", limit=1000, normalize=False)
# [{"name": "Sheet1", "content": [...]}, {"name": "Sheet2", "content": [...]}]
```

### Image OCR hook

```python
from smart_slice import slice_bytes, build_image_text_extractor

ocr = build_image_text_extractor()   # None unless SMART_SLICE_OCR_ENABLED=1 and `ocr` extra installed
rows = slice_bytes(data, "scan.docx", limit=1000, image_text_extractor=ocr)
```

### Progress heartbeat

`progress_hook` is a zero-arg callback fired before each handler, each archive member, and each OCR call — wire it to a job heartbeat while parsing large files:

```python
slice_bytes(data, "huge.pdf", limit=1000, progress_hook=lambda: job.touch())
```

### Second-stage chunking for embeddings

Slicing yields semantic paragraphs; embedding models still need fixed-size input.
`chunk_paragraphs` cuts paragraphs to a window while the paragraph stays the
retrieval unit. See [Chunk size and overlap](#chunk-size-and-overlap) for the
overlap and `carry_title` options.

### Error handling

```python
from smart_slice import slice_bytes, SliceError

try:
    slice_bytes(data, "movie.mp4")
except SliceError as e:
    print(e.code, e.message)   # 400 Unsupported file format: .mp4
```

---

## Configuration

Defensive parser caps (anti decompression-bomb guards) come from the environment, canonical name `SMART_SLICE_PARSER_<FIELD>`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `SMART_SLICE_PARSER_MAX_INPUT_BYTES` | 134217728 | max input size |
| `SMART_SLICE_PARSER_MAX_XML_BYTES` | 33554432 | max XML member size |
| `SMART_SLICE_PARSER_MAX_PACKAGE_MEMBERS` | 10000 | max archive entries |
| `SMART_SLICE_PARSER_MAX_PACKAGE_BYTES` | 134217728 | max expanded archive size |
| `SMART_SLICE_PARSER_MAX_XML_ELEMENTS` | 100000 | max XML nodes |
| `SMART_SLICE_PARSER_MAX_XML_DEPTH` | 128 | max XML nesting |
| `SMART_SLICE_PARSER_MAX_TABLE_CELLS` | 1000000 | max spreadsheet cells |
| `SMART_SLICE_PARSER_MAX_MIME_PARTS` | 1000 | max MIME parts |
| `SMART_SLICE_PARSER_MAX_MIME_DEPTH` | 32 | max MIME nesting |
| `SMART_SLICE_PARSER_MAX_ODF_TEXT_BYTES` | 33554432 | max ODF extracted text |
| `SMART_SLICE_OCR_ENABLED` | `0` | enable local OCR (needs the `ocr` extra) |

Or override per call by passing a `ParserLimits` instance to the validators.

---

## API reference

| Function | Purpose |
|----------|---------|
| `slice_text(text, *, limit, patterns, with_filter, name)` | slice an in-memory string |
| `slice_bytes(content, name, *, limit, ...)` | slice raw bytes (full options) |
| `slice_path(path, *, limit, ...)` | read a file, then slice it |
| `extract_text(content, name, *, save_image)` | extract text only, no slicing |
| `detect_handler(name, content=None)` | which handler claims this input |
| `supported_extensions()` | handler → extensions map |
| `missing_dependencies()` | optional extras not installed |
| `chunk_paragraphs(paragraphs, *, chunk_size)` | second-stage embedding chunking |
| `chunk(text, *, chunk_size)` | convenience wrapper |
| `split_document(...)` | the low-level orchestration entry point |

Building blocks: `SplitModel`, `smart_split_paragraph`, `filter_special_char`, `MarkChunkHandle`, `ParserLimits`, `ImageAsset`, `SPLIT_HANDLERS`, `MARKDOWN_HEADINGS`, `DEFAULT_PATTERNS`, `BLANK_LINE`, `LITERAL_PATTERNS`.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how slicing works internally and [`docs/PORTING.md`](docs/PORTING.md) for how this was extracted from the source platform.

---

## Development

```bash
git clone https://github.com/guangxiangdebizi/smart-slice.git
cd smart-slice
pip install -e ".[dev]"
pytest
ruff check smart_slice tests
```

---

## License

GPL-3.0. See [LICENSE](LICENSE).
