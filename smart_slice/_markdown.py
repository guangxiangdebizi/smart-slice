# coding=utf-8
"""Markdown reference scanners.

Derived from the source platform text helpers (only these two pure-text scanners were
carried over; the rest of that module was framework-specific).

Both are used when a document arrives as a bundle (a zip of markdown pages plus
their images): the inner text references sibling files by relative path, and those
references have to be rewritten to the ``./oss/file/{id}`` form once the assets are
collected.
"""
import re
from typing import List

__all__ = ["parse_md_image", "parse_md_file_link"]


def parse_md_image(content: str) -> List[str]:
    """Return every markdown image reference (``![alt](target)``) found in ``content``."""
    matches = re.finditer(r"!\[.*?\]\(.*?\)", content)
    return [match.group() for match in matches]


def parse_md_file_link(content: str) -> List[str]:
    """Return non-image markdown links like [text](file.mp4) and HTML src/href referencing files."""
    results = []
    # Regular markdown links (not images): [text](path)
    for m in re.finditer(r"(?<!!)\[.*?\]\((.*?)\)", content):
        results.append(m.group())
    # HTML tags with src attribute, e.g. <video src="...">
    for m in re.finditer(r'<\w+[^>]+\bsrc=["\']([^"\']+)["\']', content):
        results.append(m.group())
    return results