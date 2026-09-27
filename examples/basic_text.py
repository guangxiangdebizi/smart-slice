"""Slice an in-memory markdown string and show the heading chain.

Run: python examples/basic_text.py
"""
from smart_slice import slice_text

DOCUMENT = """# User Guide

Welcome to the guide.

## Installation

Install with pip.

### From PyPI

    pip install smart-slice

### From source

    git clone https://example.invalid/repo.git

## Usage

Call slice_text on a string.

# Appendix

Extra notes live here.
"""


def main() -> None:
    rows = slice_text(DOCUMENT, limit=1000)
    print(f"{len(rows)} paragraphs\n" + "-" * 60)
    for row in rows:
        title = row["title"] or "(no heading)"
        body = row["content"].strip().replace("\n", " / ")
        print(f"[{title}]\n    {body[:70]}")
        print()


if __name__ == "__main__":
    main()