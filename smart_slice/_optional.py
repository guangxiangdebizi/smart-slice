# coding=utf-8
"""Optional-dependency bookkeeping.

Every format parser is an optional extra (see ``pyproject.toml``).  A handler
whose parser is not installed must degrade to "this format is not supported"
instead of breaking ``import smart_slice`` for everybody.

This module is the single place that knows which top-level import belongs to
which pip extra, so the error text a user sees always names the exact command
that fixes it::

    Unsupported file format: .pptx (install smart-slice[office] to enable it)

Handlers use it in two places:

``support()``
    returns False when the parser is missing, so dispatch skips the handler and
    a later one (or the 400 from the service layer) answers instead;
``handle()`` / ``get_content()``
    calls :func:`require` first, so a direct call - or a delegation from another
    handler such as ``WpsSplitHandle`` - raises a clear 400 rather than a
    ``NameError``.
"""
from typing import Dict, List, Optional

from .exceptions import UnsupportedFormatError

__all__ = ["EXTRA_FOR", "REQUIREMENT_FOR", "extra_for", "require", "missing_hint", "installed"]

#: top-level import name -> pip extra that provides it
EXTRA_FOR: Dict[str, str] = {
    "markdownify": "markup",
    "bs4": "markup",
    "docx": "office",
    "pptx": "office",
    "openpyxl": "office",
    "xlrd": "office",
    "olefile": "office",
    "striprtf": "office",
    "pypdf": "pdf",
    "PIL": "pdf",
    "extract_msg": "mail",
    "py7zr": "archive",
    "mobi": "ebook",
    "pillow_heif": "image",
    "jieba": "keywords",
    "rapidocr_onnxruntime": "ocr",
    "uuid_utils": "fastuuid",
}

#: top-level import name -> the requirement string quoted in the error message
REQUIREMENT_FOR: Dict[str, str] = {
    "markdownify": "markdownify>=0.13",
    "bs4": "beautifulsoup4>=4.12",
    "docx": "python-docx>=1.1",
    "pptx": "python-pptx>=1.0",
    "openpyxl": "openpyxl>=3.1",
    "xlrd": "xlrd>=2.0",
    "olefile": "olefile>=0.46",
    "striprtf": "striprtf>=0.0.26",
    "pypdf": "pypdf>=4.0",
    "PIL": "pillow>=10.0",
    "extract_msg": "extract-msg>=0.48",
    "py7zr": "py7zr>=0.20",
    "mobi": "mobi>=0.3.3",
    "pillow_heif": "pillow-heif>=0.13",
    "jieba": "jieba>=0.42",
    "rapidocr_onnxruntime": "rapidocr-onnxruntime>=1.3",
    "uuid_utils": "uuid-utils>=0.6",
}


def extra_for(module: str) -> str:
    """Return the pip extra that provides ``module`` (``all`` when unknown)."""
    return EXTRA_FOR.get(module.split(".")[0], "all")


def missing_hint(module: str) -> str:
    """Install instruction for a missing parser, e.g. ``pip install smart-slice[office]``."""
    return f"pip install smart-slice[{extra_for(module)}]"


def installed(module: str) -> bool:
    """Is the optional parser importable right now?"""
    import importlib.util

    try:
        return importlib.util.find_spec(module.split(".")[0]) is not None
    except (ImportError, ValueError):  # pragma: no cover - broken install
        return False


def require(module: str, extension: Optional[str] = None) -> None:
    """Raise :class:`UnsupportedFormatError` unless ``module`` is importable.

    :param module:    top-level import name, e.g. ``"openpyxl"``
    :param extension: the file extension being parsed, used in the message
    """
    if installed(module):
        return
    subject = extension or module
    raise UnsupportedFormatError(
        f"Unsupported file format: {subject} "
        f"(missing optional parser '{module}'; run {missing_hint(module)})"
    )


def missing_extras() -> List[Dict[str, str]]:
    """Every optional parser that is not importable, as dicts for introspection."""
    out = []
    for module, requirement in REQUIREMENT_FOR.items():
        if not installed(module):
            out.append({"module": module, "extra": EXTRA_FOR[module], "requirement": requirement})
    return out
