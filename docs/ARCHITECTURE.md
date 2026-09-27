# Architecture

How `smart-slice` turns a document into retrieval-ready paragraphs.

## Layering

```
public API  (smart_slice/__init__.py)
   slice_text / slice_bytes / slice_path / extract_text / chunk_paragraphs / detect_handler
        |
        v
service  (smart_slice/service.py)
   split_document(): bytes -> file adapter -> handler dispatch -> image rewrite/OCR -> normalise
        |
        v
handlers  (smart_slice/handlers/*)
   SPLIT_HANDLERS ordered registry; each implements support/handle/get_content
   per-format text extraction (PDF layout, OOXML parts, MIME, archives, ...)
        |
        v
chunker  (smart_slice/chunker.py)
   SplitModel: heading tree -> blank-line/sentence splitting -> length budget
   -> paragraph assembly with heading-chain titles
```

Each layer has one job:

- **Public API** — the ergonomics layer. Normalises arguments, picks defaults
  (`DEFAULT_LIMIT`, per-format patterns), and exposes the convenience verbs.
- **service** — orchestration. It is the single funnel every entry point goes
  through, so image handling, reference rewriting and output normalisation happen
  in exactly one place.
- **handlers** — format knowledge. A handler knows how to get *text* (and
  embedded images) out of one family of formats; it delegates the actual cutting
  to the chunker.
- **chunker** — the slicing algorithm, format-agnostic. It only ever sees a string.

## The handler contract

Every handler subclasses `BaseSplitHandle` and implements three methods:

| Method | Role |
|--------|------|
| `support(file, get_buffer) -> bool` | claim the input by name and, where cheap, by sniffing bytes |
| `handle(file, pattern_list, with_filter, limit, get_buffer, save_image)` | extract text, then slice it into paragraphs |
| `get_content(file, save_image)` | extract text only (no slicing) — used by `extract_text` and archive bundling |

`file` is a minimal object exposing `.name`, `.read()` and `.chunks()`; the
service adapts raw bytes to this shape with `BytesSplitFile`. `get_buffer` is a
`FileBufferHandle` that reads the file once and caches the bytes so several
handlers can sniff without re-reading.

### Dispatch

`split_document` iterates `SPLIT_HANDLERS` **in order** and uses the first handler
whose `support` returns True. Order therefore *is* priority:

1. specific structured formats first (HTML, MHTML, DOCX, PDF, spreadsheets, CSV),
2. `Fb2SplitHandle` **before** `ZipSplitHandle` - `.fb2.zip` is a FictionBook in a
   zip container, not a generic archive, so it must be claimed first,
3. ZIP, XMind, the Office/ebook/mail/archive families,
4. the pure-stdlib structured-text handlers (SVG, ipynb, subtitle, mbox,
   iCalendar/vCard) - they must precede the text fallback or their extensions would
   either hit the exclusion list (`.svg` -> 400) or be parsed with the markdown
   heading patterns (`.ipynb`/`.ics` -> metadata extracted as titles),
5. images (`ImageSplitHandle`, then `ExtendedImageSplitHandle` which subclasses it
   for the extra Pillow-decodable raster containers),
6. `TextSplitHandle` **last**, as the fallback for anything else that decodes as
   text.

`TextSplitHandle` accepts a broad set of text/source extensions and otherwise
falls back to "decodes cleanly as text". Extensions in `TEXT_EXTENSIONS` that are
not prose (`.py`, `.java`, `.json`, `.yaml`, ...) also land in `LITERAL_EXTENSIONS`,
which splits on blank lines/length only - a `#` comment in source code is data, not
a heading. A separate exclusion list keeps known binary/media extensions out of
that fallback so a mis-detected binary is not force-decoded into garbage. When no
handler claims the input, the service raises
`SliceError(400, "Unsupported file format: <ext>")`.

Archives recurse: the zip/tar/7z handlers unpack and run each inner file through
their own list (`zip_handler.split_handles`), which tracks `SPLIT_HANDLERS` for the
document formats but **deliberately omits the image handlers** - embedded images
travel through the zip handler's markdown reference collection and the `save_image`
callback instead of becoming standalone image paragraphs (a Phase 2-B decision,
pinned by `tests/test_service.py::OcrInjectionBranchTests`). So a `.zip` of `.docx`
files slices each document normally, while a loose image inside it is skipped and
logged rather than claimed.

### Optional dependencies never break import

Every format parser except `charset-normalizer` is an optional extra, but
`smart_slice.handlers` builds all handler singletons at import time. Two rules keep
a partial install working:

- each module's third-party import is wrapped in `try/except ImportError`, binding
  the missing names to `None` and setting an `*_AVAILABLE` flag;
- each handler's `support()` returns False when its flag is off.

So a missing parser makes the format *unclaimed* - dispatch continues, and the
service layer answers `400 Unsupported file format: .pdf` - rather than raising
`ModuleNotFoundError` from `import smart_slice`. `smart_slice._optional` is the
single mapping of import name -> pip extra, which both drives those flags and
produces the actionable message text (`pip install smart-slice[office]`).
Nothing is touched at import time beyond binding `None`: the one module-level
expression that needed a parser (`doc.py`'s namespace map) was made lazy for the
same reason.

## The slicing algorithm (chunker)

`SplitModel.parse(text)` is the heart. It is driven by an **ordered list of
regular expressions** (`content_level_pattern`): index 0 is the outermost heading
level, index 1 the next, and so on. The default family
(`patterns.DEFAULT_PATTERNS`) is the six markdown ATX levels followed by a
blank-line rule.

### 1. Build the heading tree — `parse_to_tree(text, index)`

- `parse_title_level` finds the first pattern (from `index`) that matches; the
  matches are the titles at this level. `parse_level` masks ` ``` ` code fences
  (`mask_code_blocks`) so a `#` inside a code block is never mistaken for a
  heading.
- If no pattern matches at all, the text becomes **blocks**: it is length-split by
  `smart_split_paragraph` and returned as leaf nodes.
- Otherwise the text between consecutive titles is captured with
  `get_level_block` and recursed into at `index + 1` (the next heading level),
  building a tree of `title` and `block` nodes.
- Guard: if a title's children contain **no block descendant**
  (`has_block_descendant`), the raw block is re-attached as a leaf. This stops the
  blank-line rule from swallowing a title's body when two headings are separated by
  whitespace only.

### 2. Split blocks by budget — `smart_split_paragraph(content, limit)`

A block longer than `limit` is cut, but not blindly. Scanning back from the limit
(it keeps at least half the block), it prefers, in order:

1. a sentence end (`。 . ! ！ ? ？`),
2. failing that, a line boundary,
3. failing that, a hard character cut at `limit`.

A special case protects markdown tables: if the chosen split lands inside a line
that starts with `|`, the cut is pulled back to the previous row boundary so a
table row is never sliced mid-cell.

### 3. Assemble paragraphs — `result_tree_to_paragraph(...)`

The tree is flattened depth-first. Each `block` becomes
`{"title": <heading chain>, "content": <text>}`, where the heading chain is the
space-joined titles from the root to that block — this is what gives every
paragraph its section context. As blocks are emitted,
`append_table_header_if_cut` carries a small cross-block state (`header`,
`prev_in_table`) and **prepends** the last-seen table header + separator to a
continuation chunk that starts mid-table. This is an addition, not a rewrite: the
original chunk text is byte-for-byte preserved.

### 4. Post-process — `post_reset_paragraph(...)`

- `content_is_null` — a title with an empty body is folded into a paragraph so the
  heading text itself is not lost.
- `filter_title_special_characters` — strips `#`, newlines and stray whitespace
  from titles.
- `sub_title` — a title longer than 255 chars spills its overflow into the content.
- Empty paragraphs are dropped at the end.

`with_filter` (off by default in `slice_text`, on for file entry points) runs
`filter_special_char`, which collapses blank runs and multiple spaces and removes
**line-initial** heading markers only — and it is code-fence aware
(`strip_heading_marker_outside_code`), so a `#` comment inside a ` ``` ` block is
kept. It never deletes a `#` in the middle of a line (colour codes, inline tags).

## Image pipeline

Handlers never persist anything. When a document embeds images:

1. the handler builds an `ImageAsset` (id, file name, raw bytes in `meta["content"]`)
   and writes a markdown reference `![name](./oss/file/{id})` into the paragraph
   text;
2. it passes the collected assets to the `save_image` **callback**;
3. the callback may return a `{new_id: existing_id}` map (e.g. after content-hash
   dedup against a store). `replace_image_file_ids` then rewrites every reference
   in the result tree to the surviving ids;
4. if an `image_text_extractor` is supplied, inline images are OCR'd
   (`extract_inline_image_texts`, deduplicated by SHA-256, capped, tiny-image
   filtered) and the text is injected after each reference
   (`inject_image_ocr_text`).

This keeps storage, dedup and OCR policy entirely in the caller's hands.

## Defensive parsing guards

`_validation.py` centralises untrusted-input handling so a hostile or damaged file
cannot exhaust memory:

- `decode_text` — BOM detection, charset sniffing, binary-signature and
  control-character rejection (raises `SliceError(400)` rather than returning mojibake).
- `parse_xml` / `_BoundedTreeBuilder` — caps XML depth and element count, rejects DTDs.
- `document_package` — caps archive member count and expanded size, rejects
  encrypted/duplicate-member packages.
- `validate_ooxml` — checks the `[Content_Types].xml` manifest before trusting a part.

Caps come from `ParserLimits` (`_config._load_limits_from_env`), each overridable
via `SMART_SLICE_PARSER_<FIELD>`. Hitting a cap raises `ResourceLimitError`, which
callers can distinguish from a plain parse failure.

## Output shapes

- `normalize=True` (default for `slice_bytes`/`slice_text`/`slice_path`) →
  a flat `[{"title", "content"}]` via `normalize_split_rows`, which flattens
  per-sheet/per-inner-file groups and applies the title fallback chain
  (paragraph title → group name → file name), truncated to 256 chars.
- `normalize=False` → the handler's raw structure (nested groups for multi-sheet
  workbooks and archives), which preview UIs consume.

## Chunking policy

Chunking lives in `chunker.py`, which is first-party source (it graduated from the
generated set because policy now lives there - see `docs/PORTING.md`).

`SplitModel.parse(text)` walks the heading tree, cuts oversized blocks with
`smart_split_paragraph`, and assembles `{"title", "content"}` paragraphs. Two
policies are configured through `options.ChunkingOptions`:

- **size** (`limit`, `length_fn`) - the per-paragraph budget, measured in characters
  by default or in tokens if a tokenizer is supplied;
- **overlap** - applied by `apply_paragraph_overlap` as a distinct pass *after* the
  tree is assembled.

Overlap is deliberately not applied inside `parse_to_tree`. That function recovers
block positions with `str.index()` over produced content, which requires chunks to
stay disjoint substrings of the source; overlapping them makes the lookup ambiguous
and silently reshuffles boundaries. Applying overlap post-assembly keeps it purely
additive - a paragraph's own text is never altered.

Options reach the chunker through a `contextvars` slot (`options.use_options`) that
the public entry points publish for the duration of a call. This is why the 24
generated handlers keep their original `handle(...)` signature yet still honour
`overlap`: `SplitModel` reads the ambient options when it is constructed. Context
variables are per-task, so concurrent slices with different options cannot interfere.

## The heading prefilter (and the optional C scan)

`parse_title_level` cascades down heading levels until one matches. Rather than run
a whole-block regex for each level, one linear scan first establishes which levels
the block can contain, and provably-empty levels are skipped. The scan is a strict
*superset* of the regex acceptance rule (it omits their `(?!#)` / `(?!--*- coding:)`
guards), so it can only over-report - never miss a heading - which is what makes
skipping safe. The premise and the end-to-end equivalence are both tested
(`tests/test_c_speedup.py`).

The scan has two interchangeable implementations resolved by `_accel.py`:
`_speedup` (optional C, `csrc/_speedup.c`) and `_speedup_py` (always present). The
canonical markdown patterns are recognised by exact source string
(`heading_level_of`); any custom pattern bypasses the prefilter and takes the regex
path unchanged. Measured impact is in `docs/PERFORMANCE.md`.

## Decoupling from the source platform

This package was extracted from a Django-based agent platform. The extraction
re-routed the framework couplings onto small, dependency-free shims so the
slicing engine stands alone:

| Coupling in the source platform | smart-slice replacement |
|---------------------------------|-------------------------|
| the platform's HTTP API exception (`code`, `message`) | `SliceError(code, message)` (`smart_slice.exceptions`) |
| the platform's resource-limit exception | `ResourceLimitError` |
| the platform's ORM file/asset model | `ImageAsset` (plain dataclass, `smart_slice.types`) |
| the platform's configured logger | `logging.getLogger("smart_slice...")` via `_logging.get_logger` |
| `django.utils.translation.gettext_lazy` | `smart_slice._i18n.gettext` (stdlib gettext, no catalogue required) |
| `uuid_utils.compat.uuid7` | `smart_slice._uuid.uuid7` (uses `uuid_utils` if present, else a pure-Python v7) |
| the platform's settings/env config helper | `smart_slice._config` (environment only) |

Parsing and splitting logic is unchanged - only the imports and these
framework-facing symbols were substituted. See `docs/PORTING.md`.