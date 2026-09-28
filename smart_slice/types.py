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

    # -- computed views (all cached on the instance, none of them change the
    # -- constructor signature the generated handlers rely on) ---------------

    @property
    def size(self) -> int:
        """Length of the raw image bytes (0 when the producer attached none)."""
        return len(self.content)

    @property
    def sha256(self) -> str:
        """Content hash - the deduplication key used by :mod:`smart_slice.multimodal`."""
        cached = self.__dict__.get("_sha256")
        if cached is None:
            from smart_slice._images import sha256_hex

            cached = sha256_hex(self.content)
            self.__dict__["_sha256"] = cached
        return cached

    @property
    def mime_type(self) -> str:
        """MIME type: producer-supplied ``meta["mime"]``, else magic bytes,
        else the file-name extension, else ``application/octet-stream``."""
        cached = self.__dict__.get("_mime_type")
        if cached is None:
            declared = self.meta.get("mime")
            from smart_slice._images import mime_for_suffix, sniff_mime

            cached = (
                declared if isinstance(declared, str) and declared else None
            ) or sniff_mime(self.content) or mime_for_suffix(self.file_name) or "application/octet-stream"
            self.__dict__["_mime_type"] = cached
        return cached

    @property
    def suffix(self) -> str:
        """Canonical file extension for :attr:`mime_type` (``.png``)."""
        from smart_slice._images import suffix_for_mime

        return suffix_for_mime(self.mime_type)

    @property
    def dimensions(self) -> Optional[tuple]:
        """``(width, height)`` in pixels, or ``None`` when the header is unreadable."""
        cached = self.__dict__.get("_dimensions")
        if cached is None:
            from smart_slice._images import image_dimensions

            declared = self.meta.get("dimensions")
            if isinstance(declared, (tuple, list)) and len(declared) == 2:
                cached = (int(declared[0]), int(declared[1]))
            else:
                cached = image_dimensions(self.content, self.mime_type) or False
            self.__dict__["_dimensions"] = cached
        return tuple(cached) if cached else None

    @property
    def width(self) -> Optional[int]:
        """Pixel width, or ``None``."""
        dims = self.dimensions
        return dims[0] if dims else None

    @property
    def height(self) -> Optional[int]:
        """Pixel height, or ``None``."""
        dims = self.dimensions
        return dims[1] if dims else None

    def data_uri(self) -> str:
        """``data:<mime>;base64,<payload>`` - the form VL embedding APIs take.

        External references (an ``<img src="https://...">`` that was recorded but
        not downloaded) have no bytes; those return the source URL instead so the
        caller can still fetch them.
        """
        payload = self.content
        if not payload:
            src = self.meta.get("src")
            return src if isinstance(src, str) else ""
        import base64

        return "data:%s;base64,%s" % (self.mime_type, base64.b64encode(payload).decode("ascii"))

    def to_dict(self, *, with_data: bool = False) -> Dict[str, Any]:
        """Plain-JSON view for logs, indices and multimodal payloads.

        :param with_data: include the base64 ``data_uri`` (large - off by default)
        """
        dims = self.dimensions
        out: Dict[str, Any] = {
            "id": str(self.id),
            "file_name": self.file_name,
            "mime_type": self.mime_type,
            "bytes": self.size,
            "sha256": self.sha256,
            "width": dims[0] if dims else None,
            "height": dims[1] if dims else None,
        }
        for key in ("source", "member", "page", "src", "anchor", "role"):
            value = self.meta.get(key)
            if value is not None:
                out[key] = value
        if with_data:
            out["data_uri"] = self.data_uri()
        return out

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