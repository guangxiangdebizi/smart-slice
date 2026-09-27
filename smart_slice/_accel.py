# coding=utf-8
"""Accelerator resolution: prefer the C scan, fall back to pure Python.

The chunker's hot path is "find the heading lines of this block".  The regular
expression path runs up to six whole-block scans per recursion level (one per
heading level, cascading until one matches).  A single linear scan that reports
every candidate heading line with its hash count replaces all of them.

This module resolves which scan implementation to use:

1. ``smart_slice._speedup`` - the optional C extension (see csrc/_speedup.c);
2. ``smart_slice._speedup_py`` - an identical pure-Python scan, always present.

Both return ``[(line_start, line_end, hashes), ...]`` with the same acceptance
rule, so the chunker is agnostic.  ``ACCELERATOR`` records which one is active for
diagnostics; it never affects results (the equivalence test asserts that).
"""
from typing import Callable, List, Optional, Tuple

from smart_slice._speedup_py import scan_heading_candidates as _py_scan

__all__ = [
    "scan_heading_candidates",
    "ACCELERATOR",
    "CANONICAL_HEADING_LEVELS",
    "heading_level_of",
]

try:  # optional C extension
    from smart_slice._speedup import scan_heading_candidates as _c_scan  # type: ignore

    scan_heading_candidates: Callable[[str], List[Tuple[int, int, int]]] = _c_scan
    ACCELERATOR = "c"
except Exception:  # noqa: BLE001 - any import/build problem falls back cleanly
    scan_heading_candidates = _py_scan
    ACCELERATOR = "python"


# The canonical markdown heading patterns, by exact source string, mapped to their
# 1-based level.  A pattern is eligible for the scan fast path only if its
# `.pattern` matches one of these verbatim - any custom or edited pattern takes the
# regular-expression path, so the optimisation can never change custom behaviour.
_CANONICAL_PATTERNS = (
    r'(?<=^)# (?!-\*- coding:).*|(?<=\n)# (?!-\*- coding:).*',
    r'(?<=\n)(?<!#)## (?!#).*|(?<=^)(?<!#)## (?!#).*',
    r"(?<=\n)(?<!#)### (?!#).*|(?<=^)(?<!#)### (?!#).*",
    r"(?<=\n)(?<!#)#### (?!#).*|(?<=^)(?<!#)#### (?!#).*",
    r"(?<=\n)(?<!#)##### (?!#).*|(?<=^)(?<!#)##### (?!#).*",
    r"(?<=\n)(?<!#)###### (?!#).*|(?<=^)(?<!#)###### (?!#).*",
)

CANONICAL_HEADING_LEVELS = {pattern: level + 1 for level, pattern in enumerate(_CANONICAL_PATTERNS)}


def heading_level_of(pattern) -> Optional[int]:
    """Return the heading level (1-6) for a canonical heading pattern, else None.

    ``None`` means "not a canonical markdown heading" - the caller must use the
    regular-expression path for that pattern (custom schemes, the blank-line rule,
    etc.).  Accepts either a compiled pattern or a raw string.
    """
    source = getattr(pattern, "pattern", pattern)
    if not isinstance(source, str):
        return None
    return CANONICAL_HEADING_LEVELS.get(source)