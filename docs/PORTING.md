# Porting from the source platform

`smart-slice` was extracted from an enterprise agent platform (a Django project).
The extraction goal was to publish the document-slicing engine as a standalone
library **without changing its behaviour**. This document records how that was
done and how to keep it true.

## Approach: a mechanical, reproducible transform

The extraction was performed by a porting script rather than by hand. The script
is deliberately limited in what it can do, which is what makes the result
trustworthy:

- it **re-routes imports** from platform module paths to package module paths,
  according to an explicit mapping table;
- it **renames framework-facing symbols** to the package's own equivalents
  (see the table below), using whole-word replacement so identifiers cannot be
  half-renamed;
- it applies a small set of **explicit, anchored patches** for the few places
  where the package intentionally differs from the platform (listed below). Every
  patch has an exact anchor string; if the anchor stops matching, the script fails
  loudly instead of silently dropping the change;
- it **sanitises comments and docstrings**, replacing internal product, module
  and integration names with neutral wording. This touches documentation text
  only, never logic.

Parsing and splitting code is otherwise byte-identical to the platform version.

The porting script itself is an internal maintenance tool and is not part of the
published package: it needs access to the platform's private sources, which are
not distributed.

## Symbol replacements

| Coupling in the source platform | smart-slice replacement |
|---------------------------------|-------------------------|
| HTTP API exception carrying `(code, message)` | `smart_slice.exceptions.SliceError(code, message)` — same shape, `code` keeps HTTP semantics |
| resource-limit exception (a `RuntimeError`) | `smart_slice.exceptions.ResourceLimitError` — still distinct from `SliceError` |
| ORM model used to carry extracted images (`id`, `file_name`, `meta`) | `smart_slice.types.ImageAsset` — a plain dataclass with the same constructor shape; `.content` reads `meta["content"]` |
| the platform's configured logger | `_log = get_logger("<module>")` — standard `logging` under the `smart_slice` namespace, no handler configuration |
| `django.utils.translation.gettext_lazy as _` | `smart_slice._i18n.gettext as _` — stdlib gettext with a null translation by default; `install_translations()` can load a real catalogue |
| `uuid_utils.compat` (`uuid7`, `UUID`) | `smart_slice._uuid` — uses `uuid_utils.uuid7` when installed, otherwise a pure-Python RFC 9562 v7 generator; call sites unchanged |
| settings helper backed by framework config | `smart_slice._config` — environment-only resolution |
| two pure-text markdown reference scanners that lived in an ORM-heavy utility module | `smart_slice._markdown` — lifted out on their own |
| streaming/bounded file wrappers used by the import pipeline | `smart_slice.files` — already framework-free (tempfile + hashlib) |

`ParserLimits` stayed with the format validators, where the handlers import it
from; `_config.py` supplies the generic `SMART_SLICE_PARSER_<FIELD>` resolution it
delegates to. Vendor-specific environment variable names from the platform are
gone — the canonical `SMART_SLICE_PARSER_*` names are the only ones read.

## Intentional deviations

These are the only places where the package differs from a pure re-route. Each is
encoded as an anchored patch in the porting script, so it survives regeneration.

1. **jieba is optional.** The platform imported `jieba` at module level in the
   chunker. The package wraps it in `try/except ImportError`; the keyword helpers
   (`get_keyword`, `to_paragraph`, `to_block_paragraph`) raise a clear
   `RuntimeError` telling the caller to install `smart-slice[keywords]`.
   `SplitModel.parse` — the actual slicing path — never touches jieba, so the core
   works without it.

2. **Local OCR is an environment switch.** The platform hard-coded OCR off. The
   package reads `SMART_SLICE_OCR_ENABLED` (default off) so a deployment can
   enable it without patching an installed wheel. Behaviour when off is identical:
   `get_ocr_engine()` returns `None` and every consumer degrades exactly as before.

3. **The handler registry has one source of truth.** The platform kept a second
   copy of the handler list in its service layer. The package builds it once in
   `smart_slice/handlers/__init__.py` (`SPLIT_HANDLERS`) and the service module
   re-exports it under the same name. Same order, same singletons.

4. **Pillow globals are no longer mutated at import.** The platform's spreadsheet
   image module set `ImageFile.LOAD_TRUNCATED_IMAGES = True` and
   `Image.MAX_IMAGE_PIXELS = None` at module import, which silently disables the
   *host process's* decompression-bomb guard. The package scopes both to a
   `_permissive_pil()` context manager around the image parse and restores the
   previous values afterwards.

## Ported test suite

The platform's own slicing tests were ported the same way — import re-routing,
removal of the framework bootstrap (`django.setup()` / `DJANGO_SETTINGS_MODULE`),
and the same symbol renames using `as` aliases, so the original test bodies and
assertions stay untouched:

- `tests/test_fidelity.py` — the fidelity guarantees (heading-marker preservation,
  table-header inheritance, blank-line splitting, oversized-row re-split) plus the
  multi-sheet / merged-cell / formula-cache / docx-structure matrix.
- `tests/test_full_format.py` — one round trip per supported format, built offline.
- `tests/test_service.py` — the orchestration layer: dual-path equivalence,
  normalisation, OCR-injection branching, image-id rewriting, progress hooks.

Plus `tests/test_public_api.py`, written for this package, covering the public
façade and the CLI.

Current status: **119 passed, 1 skipped** on Python 3.12 (the skip is an
optional-dependency branch).

## Keeping the port honest

Because the transform is mechanical and deterministic, "the library behaves like
the platform" is a checkable property rather than a claim: regeneration plus a
byte-exact diff against the committed tree is the drift guard. Any difference
means either

- the platform module changed after this snapshot — re-port deliberately and
  review the diff; or
- someone hand-edited a ported file — move the edit into an anchored patch so it
  stays reproducible.

Contributors should treat files under `smart_slice/` that carry the
"Derived from the source platform" header as generated: do not hand-edit them.
The hand-written modules are the public façade (`__init__.py`), `patterns.py`,
`_config.py`, `_logging.py`, `_i18n.py`, `_uuid.py`, `_markdown.py`,
`exceptions.py`, `types.py`, and the three package `__init__.py` registries.