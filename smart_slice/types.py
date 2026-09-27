# coding=utf-8
"""Value types shared by handlers and the public API."""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

__all__ = ["ImageAsset", "Paragraph", "SplitResult", "ImageSink"]


@dataclass
class ImageAsset:
    """An image extracted out of a document during slicing.

    Handlers never persist anything: they collect assets and hand them to the
    caller-provided ``save_image`` sink.  ``meta["content"]`` carries the raw
    bytes, ``id`` is the identifier already written into the markdown reference
    ``![name](./oss/file/{id})`` inside the produced paragraphs.

    A sink may return ``{new_id: existing_id}`` to remap references (for example
    after content-hash deduplication against a store); ``split_document`` applies
    that mapping to the whole result tree before returning.
    """

    id: Any
    file_name: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def content(self) -> bytes:
        """Raw image bytes (empty when the producer did not attach any)."""
        value = self.meta.get("content")
        return value if isinstance(value, (bytes, bytearray)) else b""

    def __repr__(self) -> str:  # keep bytes out of logs / tracebacks
        return f"ImageAsset(id={self.id!r}, file_name={self.file_name!r}, bytes={len(self.content)})"


#: One produced paragraph: ``{"title": str, "content": str}``.
Paragraph = Dict[str, str]

#: Handler output.  Single-document handlers return
#: ``{"name": <file name>, "content": [Paragraph, ...]}``; container handlers
#: (xlsx per sheet, zip per inner file) return a list of such groups.
SplitResult = Any

#: Callback receiving the images extracted from one document.
ImageSink = Any


def paragraph(title: str, content: str) -> Paragraph:
    """Convenience constructor for a paragraph dict."""
    return {"title": title or "", "content": content or ""}


def optional_text(value: Optional[str]) -> str:
    return value if isinstance(value, str) else ""