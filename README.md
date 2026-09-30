# smart-slice

**Fidelity-first document slicing for RAG pipelines.** Feed it 197 file extensions across 30 handlers, get back retrieval-ready paragraphs that keep every original character reachable - plus the document's pictures, joined to the paragraphs they belong to.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-415%20passed-brightgreen.svg)]()
[![PyPI version](https://img.shields.io/pypi/v/smart-slice.svg)](https://pypi.org/project/smart-slice/)

```python
from smart_slice import slice_text

paragraphs = slice_text("# Chapter\n\nbody text", limit=1000)
# [{'title': 'Chapter', 'content': 'body text'}]
```

---

## Why another splitter?

Most text splitters treat a document as a bag of characters and chop it on a fixed grid. That loses the two things retrieval actually depends on: **where a passage sits in the document's structure**, and **the passage's exact original wording**. `smart-slice` is built around five rules.

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

### 3. Measurably fast - and honestly measured

Slicing a 550 KB structured document takes ~125 ms (≈4.4 MB/s) on a laptop CPU.
The hot path is a single linear scan that decides which heading levels a block can
possibly contain, so provably-empty regex passes are skipped; an optional C
extension (`pip install smart-slice[accel]`) runs that scan natively. Numbers and
the reasoning are in [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md).

Speed is the KPI, and it is tracked release over release: **394 ms -> 124 ms
(~3.1x) from 0.1.0 to 0.5.0** on the same corpus, every step asserted
output-identical, with the batch scheduler adding a further 1.61x on a 48-document
/ 26.5 MB corpus. [`docs/BENCHMARK.md`](docs/BENCHMARK.md) has the per-change
breakdown, the peer throughput comparison, and what did *not* help.

Throughput is not the only axis, and on that one `smart-slice` is honestly beaten:
a plain character splitter runs ~36x faster. What the extra time buys is measured
against `chonkie` and `langchain-text-splitters` on the same corpus - `smart-slice`
is the only row that keeps every natural unit intact, carries its heading chain,
never cuts a code fence and never orphans a table row from its header. Retrieval
recall is deliberately **not** a KPI here: it needs an embedding model and a
labelled query set, so one archived run against the Chroma benchmark is recorded in
`docs/BENCHMARK.md` section 4 with its limitations stated, and is not repeated.

### 4. No framework

A pure library: no Django, no ORM, no network calls, no logging configuration (it emits records on the `smart_slice` logger and leaves routing to you). Errors are `SliceError` with an HTTP-style `.code` (`400` = input problem, `500` = parse failure). Images pulled out of a document are handed to a `save_image` **callback** — the library never persists anything, and a remote `<img src="https://...">` is recorded, never fetched.

### 5. Pictures are part of the document

A vision-language embedding model wants the figure next to the paragraph that
discusses it, not a text-only index of a document that was half diagrams.
`slice_multimodal` returns both, already joined — see
[Multimodal slicing](#multimodal-slicing-pictures--paragraphs).

---

## Installation

The core install is deliberately tiny (just a text decoder). Format parsers are optional extras, and a handler whose parser is missing simply reports "unsupported" instead of crashing.

```bash
pip install smart-slice                 # core: text/markdown/config/source files
pip install smart-slice[all]            # every format below
```

Published on PyPI as [`smart-slice`](https://pypi.org/project/smart-slice/). The
`0.4.0` wheel and sdist are also attached to the
[GitHub Release](https://github.com/guangxiangdebizi/smart-slice/releases/tag/v0.4.0),
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

**Pictures** come out of every one of these containers - `docx`/`pptx`/`pdf`/`xlsx`/`zip` through their handlers, and `html`/`epub`/`eml`/`mhtml`/`odf`/`fb2`/standalone image files through the multimodal probe. See [Multimodal slicing](#multimodal-slicing-pictures--paragraphs).

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

## Multimodal slicing (pictures + paragraphs)

Text-only indexing throws away the figures, charts and screenshots in a document -
exactly the content a vision-language embedding model can use. `slice_multimodal`
returns the paragraphs *and* the pictures, already joined:

```python
from smart_slice import slice_multimodal

result = slice_multimodal(path="deck.pptx", limit=1000)

result.summary()          # '14 paragraphs, 6 images (6 attached, 0 unattached, 0.42 MB)'
result.coverage           # 1.0 - every image landed on a paragraph

for row in result.paired():            # only paragraphs that carry a picture
    print(row["title"], len(row["content"]), row["images"][0]["mime_type"])
    send_to_vl_model(row["title"], row["content"],
                     [i["data_uri"] for i in row["images"]])
```

Each entry of `result.records()` / `result.paired()` is plain JSON:

```python
{
  "index": 3,
  "title": "Q3 Results  Revenue by region",
  "content": "Revenue grew ... ![chart](./oss/file/0198f...)",
  "images": [{
      "id": "0198f...", "file_name": "chart1.png", "mime_type": "image/png",
      "bytes": 41233, "sha256": "9d1e...", "width": 940, "height": 512,
      "source": "zip", "member": "ppt/media/image3.png", "match": "reference",
      "data_uri": "data:image/png;base64,iVBORw0..."     # with with_data=True
  }]
}
```

### Where the pictures come from

Two independent channels, deduplicated against each other by content hash:

1. **the handlers** - `docx`, `pptx`, `pdf`, `xlsx` and `zip` already emit
   `ImageAsset` objects through `save_image`. `slice_multimodal` installs an
   `ImageCollector` as that callback, so they are captured instead of dropped.
2. **the probe** - when the handlers produced nothing for this document,
   `scan_media` opens the container itself. This is what recovers the formats that
   only ever yielded text:

| container | what the probe reads |
|-----------|----------------------|
| `zip` (OOXML / ODF / EPUB / any archive) | `word/media/*`, `ppt/media/*`, `xl/media/*`, `Pictures/*`, and every other image member; then text members for `<img src>` and `![]()` references, resolved relative to the member they were found in |
| `pdf` | `page.images`, tagged with the page number (needs the `pdf` extra) |
| `mime` (`.eml` `.mhtml`) | image parts, indexed by `Content-ID` / `Content-Location` so `cid:` references in the body resolve |
| `markup` (`.html` `.md` ...) | `data:` URIs decoded; remote URLs **recorded, never fetched** |
| standalone picture | the file itself, pinned to its one paragraph |

`probe="auto"` (the default) means "only when the handlers came up empty", so a
`.docx` is not read twice. `probe=True` always probes, `probe=False` never does.

### How a picture is joined to a paragraph

Four passes, strongest signal first; each placed image records how it matched in
`meta["match"]`, and anything left over lands in `result.unattached` rather than
being dropped:

| pass | signal | reliability |
|------|--------|-------------|
| `reference` | the `./oss/file/{id}` the handler wrote into the paragraph text | exact |
| `heading` | the heading the picture sat under in the source markup | strong |
| `anchor` | the ~96 characters of visible text that preceded the tag | heuristic |
| `document` | a standalone picture file *is* the document | exact by definition |

**The sliced text is never modified** - attachment is a join, not a rewrite, and
the test suite asserts `result.paragraphs == slice_bytes(...)` for every container.

### Using the collector on its own

```python
from smart_slice import slice_bytes, ImageCollector

collector = ImageCollector()                       # dedupes by sha256
rows = slice_bytes(data, "report.docx", save_image=collector)
collector.assets                                   # [ImageAsset, ...]
collector.duplicates                               # how many repeats it folded away
```

`ImageCollector` returns the `{new_id: kept_id}` map the `save_image` contract
expects, so duplicate pictures collapse onto one id *and* every
`./oss/file/{id}` reference in the paragraphs is rewritten to match.

`ImageAsset` grew computed views (the constructor is unchanged): `.content`,
`.size`, `.sha256`, `.mime_type`, `.suffix`, `.dimensions`, `.width`, `.height`,
`.data_uri()`, `.to_dict(with_data=...)`. MIME type and pixel size come from the
magic bytes and the file header - no Pillow required.

### Tuning

```python
slice_multimodal(
    data, "page.html",
    min_side=20,            # drop bullets, icons and tracking pixels
    include_vector=True,    # also return SVG / EMF / WMF (off: a VL model cannot eat them)
    include_external=False, # do not record remote <img src> URLs
    max_images=512,         # per-document ceiling
    max_image_bytes=64 << 20,
    keep_content=False,     # metadata only - frees the bytes after slicing
)
```

### A whole corpus at once

```python
from smart_slice import slice_many

report = slice_many(paths, backend="thread", concurrency=4, collect_images=True)
report.paragraphs        # unchanged: every document's paragraphs, in input order
report.images            # every picture in the batch, deduplicated across documents
```

`collect_images=True` needs a thread or serial backend - the collector holds a
lock, and a lock is not picklable - so `backend="process"` raises instead of
silently returning an empty list.

### On the command line

```bash
smart-slice media deck.pptx                  # what is in there, and where it belongs
smart-slice media page.html --json           # machine readable
smart-slice media book.epub --out-dir imgs/  # also write the picture bytes to disk
smart-slice media scan.pdf --min-side 20 --probe always
```

---
## Command line

```bash
smart-slice slice report.pdf --limit 1000 --format json -o out.json
smart-slice slice notes.md --overlap 150 --stats          # 15% overlap on a 1000-char budget
smart-slice slice notes.md --overlap-ratio 0.15 --format text
smart-slice chunk notes.md --chunk-size 256 --chunk-overlap 40 --carry-title
smart-slice detect mystery.bin
smart-slice formats            # list handlers + missing optional deps
smart-slice batch docs/*.pdf -j 4 --backend process --stats   # a whole corpus
smart-slice media deck.pptx --out-dir imgs/       # pictures out, and where they belong
smart-slice cores              # detected cores, derived width, affinity mask
python -m smart_slice slice notes.md --format md   # module form
```

- `slice` — `--format {json,jsonl,text,md}`, `--limit`, `--overlap`, `--overlap-ratio`, `--overlap-section-only`, `--no-filter`, `--title-prefix`, `-o/--output`, `--stats` (summary to stderr).
- `chunk` — slices first, then cuts paragraphs to an embedding window: `--chunk-size`, `--chunk-overlap`, `--carry-title`, `-o/--output`, `--stats`. Emits one JSON object per chunk.
- `batch` — slices many documents under one scheduling policy: `-j/--concurrency {N,auto,cores,serial,2x}`, `--backend {thread,process,serial}`, `--pin-cores`, `--max-concurrency`, `--error-policy {raise_first,collect}`, `--unordered`, `--timeout`, plus the `slice` options; `--format {json,jsonl,text,summary}`, `-o/--output`, `--stats`. Exit 1 names every failed document, exit 2 is a usage error.
- `media` — slices the document and reports every picture in it, joined to the paragraph it belongs to: `--limit`, `--probe {auto,always,never}`, `--include-vector`, `--min-side`, `--no-external`, `--out-dir` (write the image bytes to disk), `--json`.
- `cores` — the scheduling facts for this machine: usable cores, derived width, affinity mask, pinning support, and which environment variables are set (`--json` for machines).
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

### Batch slicing and scheduling

One document is a `slice_path` call. A corpus is a scheduling problem, and the
scheduler that sat under the source platform's slicing path ships with the
package - every knob is a parameter, so you choose the policy per call:

```python
from smart_slice import slice_paths

report = slice_paths(["a.pdf", "b.docx", "c.md"], limit=1000, concurrency=4)
report.ok                 # True when every document sliced
report.results            # per-document paragraphs, in input order
report.paragraphs         # all of them concatenated, in input order
print(report.summary())   # 3/3 documents sliced in 412 ms (width=4, backend=thread, ...)
```

Width is a *variable*, not a constant. Accepted forms: an integer (`4`), or
`"auto"` (the default), `"cores"`, `"serial"`, or a multiple of the core count
(`"2x"`, `"0.5x"`):

```python
slice_paths(paths, concurrency="cores")     # every usable core
slice_paths(paths, concurrency=2)           # exactly two workers
slice_paths(paths, backend="serial")        # no pool at all
```

`"auto"` applies the allocation rule the scheduler was extracted from: 3 workers
on a machine with more than six cores, half the cores below that, never fewer
than one. `available_cores()` reports what the process may actually use - the
Linux affinity mask, so `taskset` and cgroup limits are honoured rather than the
host's core count:

```python
from smart_slice import available_cores, auto_concurrency
available_cores(), auto_concurrency()       # (12, 3) on a 12-core laptop
```

Core allocation is opt-in: `pin_cores=True` binds each task to one core of the
mask, round-robin, and restores the mask when the task ends (Linux
`sched_setaffinity`, Windows `SetThreadAffinityMask`, a logged no-op on macOS).
`TaskOutcome.core` records what each task actually got.

Batches can mix input shapes, and one policy object can be reused:

```python
from smart_slice import SchedulerPolicy, SliceJob, slice_many

policy = SchedulerPolicy(concurrency=4, pin_cores=True, error_policy="collect")
report = slice_many([
    "report.pdf",                                    # path
    ("upload.docx", uploaded_bytes),                  # (name, bytes)
    SliceJob.from_path("big.md", limit=4000),         # per-document override
    {"name": "notes.md", "content": raw},             # mapping
    upload_handle,                                    # anything with .name/.read()
], policy=policy, limit=1000)

for outcome in report.outcomes:
    if not outcome.ok:
        log.warning("%s failed: %s", outcome.name, outcome.error)   # code in .error.code
```

Failure semantics are the ported ones and they are a choice:

| `error_policy` | Behaviour |
|----------------|-----------|
| `"raise_first"` (default) | every document is still attempted, then the error of the **lowest failing index** is re-raised as the original object (traceback intact) and the remaining failures are logged |
| `"collect"` | nothing is raised; inspect `report.failures` / `outcome.error` |

Two backends, and the choice matters more than the width:

| Backend | Use it when | Cost |
|---------|-------------|------|
| `"thread"` (default) | the worker also does I/O (database writes, embedding calls, downloads), or you pass callbacks (`save_image`, `progress_hook`, a tokenizer in `length_fn`) | none; but the GIL means pure-Python slicing does not speed up |
| `"process"` | a CPU-bound corpus of plain files, big enough to amortise startup | interpreter startup plus pickling the bytes both ways; inputs must be picklable |

Measured on a 12-core laptop (551 KB structured markdown per document, CPython
3.12, Windows) - `python scripts/benchmark.py --batch`:

| Batch | serial | threads x4 | processes x4 |
|-------|--------|------------|--------------|
| 12 documents (6.6 MB) | 1.00x | 1.07x | 0.69x |
| 48 documents (26.5 MB) | 1.00x | 0.95x | **1.61x** |

So: threads buy you overlap only where the GIL is released; processes buy real
throughput on CPU-bound batches from a few dozen documents up. Details and the
reasoning are in [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md).

Underneath, `run_parallel(items, worker, policy)` is the same ordered,
error-isolating parallel map without any slicing in it - use it when you want
the scheduling for your own worker.

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

Batch scheduling is configured the same way, and every variable is overridden by
the matching argument:

| Variable | Default | Meaning |
|----------|---------|---------|
| `SMART_SLICE_CONCURRENCY` | `auto` | batch width: an integer, `auto`, `cores`, `serial`, or a multiple (`2x`) |
| `SMART_SLICE_MAX_CONCURRENCY` | `32` | ceiling for a derived width |
| `SMART_SLICE_SCHEDULER_BACKEND` | `thread` | `thread`, `process` or `serial` |
| `SMART_SLICE_PIN_CORES` | `0` | allocate one core per task, round-robin |

Or override per call by passing a `ParserLimits` instance to the validators, or a
`SchedulerPolicy` to `slice_many` / `run_parallel`.

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
| `slice_many(inputs, *, concurrency, backend, pin_cores, collect_images, ...)` | slice a corpus in one call -> `BatchReport` |
| `slice_paths(paths, ...)` | `slice_many` over filesystem paths |
| `slice_multimodal(content=None, name=None, *, path, limit, probe, ...)` | paragraphs **and** pictures, already joined -> `MultimodalResult` |
| `slice_path_multimodal(path, ...)` | `slice_multimodal` for a file on disk |
| `extract_media(content, name, ...)` / `scan_media(...)` | pull the images out of a container, no slicing |
| `attach_images(paragraphs, assets)` | join images to paragraphs |
| `ImageCollector` | a `save_image` sink that dedupes and returns the id-remap contract |
| `MultimodalResult` / `MultimodalParagraph` / `MediaExtraction` | multimodal result types |
| `container_kind(content, name)` | which reader the probe would use |
| `run_parallel(items, worker, policy)` | the ordered, error-isolating parallel map |
| `SchedulerPolicy` / `SliceJob` / `BatchReport` / `TaskOutcome` | scheduling configuration and results |
| `available_cores()` / `auto_concurrency()` / `resolve_concurrency()` | core discovery and width derivation |
| `use_policy(policy)` | publish a policy for a block (contextvar-scoped) |
| `split_document(...)` | the low-level orchestration entry point |

Building blocks: `SplitModel`, `smart_split_paragraph`, `filter_special_char`, `MarkChunkHandle`, `ParserLimits`, `ImageAsset` (with `.mime_type`, `.sha256`, `.dimensions`, `.data_uri()`, `.to_dict()`), `SPLIT_HANDLERS`, `MARKDOWN_HEADINGS`, `DEFAULT_PATTERNS`, `BLANK_LINE`, `LITERAL_PATTERNS`.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how slicing works internally, [`docs/BENCHMARK.md`](docs/BENCHMARK.md) for the comparison against other chunkers, and [`docs/PORTING.md`](docs/PORTING.md) for how this was extracted from the source platform.

---

## Development

```bash
git clone https://github.com/guangxiangdebizi/smart-slice.git
cd smart-slice
pip install -e ".[dev]"
pytest                                   # 415 tests, all offline
ruff check smart_slice tests

python scripts/benchmark.py              # throughput + profile + batch scaling
pip install chonkie langchain-text-splitters tiktoken
python scripts/benchmark_peers.py        # peer throughput + structure comparison
```

---

## License

GPL-3.0. See [LICENSE](LICENSE).
