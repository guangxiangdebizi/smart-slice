# Contributing to smart-slice

Thanks for the interest. This document covers the development setup, the rules
that keep the package faithful to its source, and the pull-request conventions.

## Development setup

```bash
git clone https://github.com/guangxiangdebizi/smart-slice.git
cd smart-slice
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

`[dev]` pulls in every format extra plus `pytest`, `ruff` and `mypy`.

## Running the checks

```bash
pytest                      # full suite (offline, no DB/network/credentials)
ruff check smart_slice tests
python scripts/benchmark.py            # timing (see docs/PERFORMANCE.md)
```

The C accelerator is optional; the pure-Python scan is used when it is absent:

```bash
python scripts/build_ext.py            # builds smart_slice/_speedup via zig cc
```

All tests build their fixtures in memory or in a temp directory; none reach the
network. Keep it that way for new tests.

## Code style

- Line length 120 (see `[tool.ruff]`).
- Ruff currently enforces the fatal rule set (`E9`, `F63`, `F7`, `F82`) - syntax
  errors and undefined names. Do not weaken this.
- Public API lives in `smart_slice/__init__.py`; add new entry points there and
  to `__all__`, with a docstring and a type hint.
- The library never configures logging handlers or levels. Emit records on the
  `smart_slice` logger via `smart_slice._logging.get_logger(...)` only.
- No new mandatory third-party dependencies without discussion: a format parser
  belongs in an optional extra, and its handler must import it lazily or guard the
  import so a missing parser degrades to "unsupported" rather than crashing.

## Generated vs hand-written modules (important)

Most modules under `smart_slice/` were **mechanically derived** from the source
platform by an internal porting tool (not distributed with the package, since it
needs the platform's private sources). The transform only re-routes imports and
renames framework-facing symbols; it does not touch parsing logic.

Modules carrying the header comment

```python
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
```

are generated. Treat them as read-only:

- **Do not hand-edit a generated file.** A behavioural change there would be lost
  the next time the porting tool is run, and would break the guarantee that the
  package matches the source it was extracted from. Raise it as an issue instead,
  so it can be folded back into the transform.
- Everything else is ordinary hand-written source and may be edited directly: the
  public facade (`__init__.py`), `chunker.py` (chunking policy - it graduated out of
  the generated set), `options.py`, `patterns.py`, `_config.py`, `_logging.py`,
  `_i18n.py`, `_uuid.py`, `_markdown.py`, `_accel.py`, `_speedup_py.py`,
  `exceptions.py`, `types.py`, `handlers/__init__.py`, `qa/__init__.py`,
  `chunking/`, and the CLI (`__main__.py`).

Note the two scan implementations must stay in lock-step: if you change
`_speedup_py.scan_heading_candidates`, mirror it in `csrc/_speedup.c`. The scan is a
superset of the heading regexes on purpose (over-reporting is safe, under-reporting
would drop headings); `tests/test_c_speedup.py` asserts C and Python agree and that
the prefilter never changes slicing output.

See `docs/PORTING.md` for how the extraction works and its deliberate deviations.

## Adding a format handler

1. Create `smart_slice/handlers/<fmt>.py` implementing `BaseSplitHandle`
   (`support` / `handle` / `get_content`).
2. Register it in `SPLIT_HANDLERS` in `smart_slice/handlers/__init__.py` at the
   correct priority (before the `TextSplitHandle` fallback) and add its extensions
   to `HANDLER_EXTENSIONS`.
3. Add any parser dependency to a sensible extra in `pyproject.toml` and to
   `_OPTIONAL_IMPORTS` in `handlers/__init__.py` so `missing_dependencies()` sees it.
4. Add offline tests that construct the format's bytes in memory.

## Pull requests

- One logical change per PR; keep the diff scoped.
- Add or update tests; a behaviour change without a test will be sent back.
- Run the three checks above and mention the results in the PR description.
- Update `CHANGELOG.md` under an `## [Unreleased]` section.
- Target the `main` branch.

## License

By contributing, you agree your contributions will be licensed under GPL-3.0,
the same license as the project.