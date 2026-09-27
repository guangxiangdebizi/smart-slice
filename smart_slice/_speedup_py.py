# coding=utf-8
r"""Pure-Python implementation of the optional C accelerator.

Mirrors :mod:`smart_slice._speedup` (C) exactly, function for function, so the
chunker can fall back to it on platforms without a compiler and tests can assert
the two agree.  This module is the *reference* implementation: the C version must
match it, not the other way around.

Both exist only to remove repeated whole-document regex scans; neither changes
what a heading is.  See :func:`smart_slice.chunker._candidate_titles` for how the
result is consumed and for the guard that keeps custom pattern lists on the regex
path.
"""
from typing import List, Tuple

__all__ = ["scan_heading_candidates", "IMPLEMENTATION"]

IMPLEMENTATION = "python"


def scan_heading_candidates(text: str) -> List[Tuple[int, int, int]]:
    r"""Report each line that could be an ATX heading, in document order.

    Acceptance rule (identical to the canonical patterns in
    :mod:`smart_slice.patterns`):

    - the line's first character is ``#`` with **no** leading whitespace, since the
      patterns anchor with ``(?<=^)`` / ``(?<=\\n)`` immediately before the hashes;
    - followed by a run of ``#`` and then a single ASCII space (U+0020).  A tab does
      not qualify - every pattern requires a literal space, which is also why
      ``## `` never matches a ``###`` line;
    The scan is deliberately a **superset** of the regular expressions: it does not
    reproduce their extra guards (the level-1 ``(?!-\*- coding:)`` exclusion, the
    ``(?!#)`` level guards).  A superset is the only safe direction for a prefilter -
    reporting an extra candidate merely costs one regex scan that returns empty,
    whereas missing one would silently drop headings.  Keeping the scan free of
    per-level special cases is also what lets the C and Python versions stay
    trivially identical.

    :param text: the (code-fence-masked) document text
    :return: ``[(line_start, line_end, hashes), ...]`` where ``line_start`` is the
             index of the first ``#`` and ``line_end`` is one past the last
             character of the line (newline excluded)
    """
    candidates: List[Tuple[int, int, int]] = []
    length = len(text)
    line_start = 0

    while line_start < length:
        line_end = text.find("\n", line_start)
        if line_end == -1:
            line_end = length

        cursor = line_start
        while cursor < line_end and text[cursor] == "#":
            cursor += 1
        hashes = cursor - line_start

        if hashes and cursor < line_end and text[cursor] == " ":
            candidates.append((line_start, line_end, hashes))

        line_start = line_end + 1

    return candidates