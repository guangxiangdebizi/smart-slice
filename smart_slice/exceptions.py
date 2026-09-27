# coding=utf-8
"""Package exceptions.

``SliceError`` is the single error type raised for every expected failure inside
the slicing pipeline (unsupported input, damaged document, resource limit hit by
a defensive parser guard).  It mirrors the ``(code, message)`` shape used by HTTP
front-ends: ``code`` follows HTTP semantics (400 = caller/input problem,
500 = parsing failure) so a web layer can serialise it directly, while a CLI or
batch job can simply log ``str(error)``.

``ResourceLimitError`` is deliberately *not* a ``SliceError`` subclass: it marks
"the defensive guard fired" (byte/element/member caps), which callers usually
want to handle differently from "this file cannot be parsed at all".
"""
from typing import Any

__all__ = [
    "SliceError",
    "UnsupportedFormatError",
    "ParseError",
    "ResourceLimitError",
]


class SliceError(Exception):
    """Base error carrying an HTTP-style ``code`` and a human readable ``message``."""

    code = 500

    def __init__(self, code: int, message: Any):
        self.code = code
        # gettext-style lazy objects are accepted: normalise to str eagerly so that
        # logging / serialisation never has to deal with a proxy object.
        self.message = message if isinstance(message, str) else str(message)
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


class UnsupportedFormatError(SliceError):
    """No handler supports the input, or the bytes are not what the extension claims."""

    code = 400

    def __init__(self, message: Any, code: int = 400):
        super().__init__(code, message)


class ParseError(SliceError):
    """A handler matched the input but failed to extract usable content."""

    code = 500

    def __init__(self, message: Any, code: int = 500):
        super().__init__(code, message)


class ResourceLimitError(RuntimeError):
    """A defensive resource cap (bytes / XML nodes / archive members / cells) fired."""