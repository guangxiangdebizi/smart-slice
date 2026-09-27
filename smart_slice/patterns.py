# coding=utf-8
"""Default heading / paragraph patterns.

The slicer is driven by an ordered list of regular expressions: index 0 is the
outermost heading level, index 1 the next, and so on.  Text that matches none of
them becomes a *block* (body text) and is length-split by ``limit``.

Two pattern families ship with the package:

``MARKDOWN_HEADINGS``
    The six markdown ATX heading levels, then a blank-line rule.  This is the
    "smart slicing" default: a document with headings is cut along its heading
    tree; a document without them still splits on blank lines instead of
    degrading into one giant paragraph or a blind character cut.
``BLANK_LINE``
    Paragraph splitting only.

Heading patterns carry a ``(?!-\\*- coding:)`` guard so the ``# -*- coding: utf-8
-*-`` line of source files is not mistaken for a heading, and ``(?<!#)`` guards
so ``###`` is matched by its own level rather than by the ``#`` and ``##`` rules.
"""
import re
from typing import List, Pattern, Tuple

__all__ = [
    "MARKDOWN_HEADINGS",
    "BLANK_LINE",
    "DEFAULT_PATTERNS",
    "LITERAL_PATTERNS",
    "patterns_for",
]

#: markdown ATX levels 1-6, guarded against ``#`` runs and coding declarations
MARKDOWN_HEADINGS: Tuple[Pattern[str], ...] = (
    re.compile(r'(?<=^)# (?!-\*- coding:).*|(?<=\n)# (?!-\*- coding:).*'),
    re.compile(r'(?<=\n)(?<!#)## (?!#).*|(?<=^)(?<!#)## (?!#).*'),
    re.compile(r"(?<=\n)(?<!#)### (?!#).*|(?<=^)(?<!#)### (?!#).*"),
    re.compile(r"(?<=\n)(?<!#)#### (?!#).*|(?<=^)(?<!#)#### (?!#).*"),
    re.compile(r"(?<=\n)(?<!#)##### (?!#).*|(?<=^)(?<!#)##### (?!#).*"),
    re.compile(r"(?<=\n)(?<!#)###### (?!#).*|(?<=^)(?<!#)###### (?!#).*"),
)

#: blank line paragraph separator (does not match at the very start of the text)
BLANK_LINE: Tuple[Pattern[str], ...] = (re.compile(r"(?<!\n)\n\n+"),)

#: default for prose documents: heading tree first, blank lines second
DEFAULT_PATTERNS: Tuple[Pattern[str], ...] = MARKDOWN_HEADINGS + BLANK_LINE

#: structured/literal formats (yaml, json, source code): blank lines only - a
#: ``#`` in a config file or a comment is data, not a heading.
LITERAL_PATTERNS: Tuple[Pattern[str], ...] = BLANK_LINE

#: file name -> pattern family
_PATTERN_BY_SUFFIX = {
    ".md": DEFAULT_PATTERNS,
    ".markdown": DEFAULT_PATTERNS,
    ".txt": DEFAULT_PATTERNS,
    ".rst": DEFAULT_PATTERNS,
}


def patterns_for(name: str) -> List[Pattern[str]]:
    """Return the default pattern list for a file name.

    Prose formats get the heading tree; everything else (source code, config,
    data files) gets blank-line splitting so ``#`` comments survive untouched.
    """
    suffix = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
    return list(_PATTERN_BY_SUFFIX.get(suffix, LITERAL_PATTERNS))