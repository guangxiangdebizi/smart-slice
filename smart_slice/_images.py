# coding=utf-8
"""Dependency-free image primitives: MIME sniffing, pixel size, content hash.

Pillow is an *optional* extra here (it arrives with ``smart-slice[office]`` and
``[pdf]``), but multimodal output is only useful when every install can tell a
PNG from a WMF and report its pixel size.  The header parsers below cover the
formats that actually occur inside documents; anything unrecognised falls back
to Pillow when it happens to be installed, and to ``None`` otherwise.

Nothing in this module imports the rest of the package, so ``types.py`` can use
it for computed properties without creating a cycle.
"""
from __future__ import annotations

import hashlib
import re
import struct
from typing import Optional, Tuple

__all__ = [
    "RASTER_MIMES",
    "VECTOR_MIMES",
    "IMAGE_SUFFIXES",
    "sniff_mime",
    "is_image_bytes",
    "image_dimensions",
    "suffix_for_mime",
    "sha256_hex",
]

#: MIME types that carry real pixel data (usable by a VL embedding model).
RASTER_MIMES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/bmp", "image/tiff",
    "image/webp", "image/x-icon", "image/heic", "image/heif", "image/avif",
})

#: MIME types that are images to a document, but not to an embedding model:
#: vector formats need a renderer before they become pixels.
VECTOR_MIMES = frozenset({"image/svg+xml", "image/x-emf", "image/x-wmf"})

_SIGNATURES: Tuple[Tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)

#: file extension -> MIME, for the cases where only a name is available
_MIME_BY_SUFFIX = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".webp": "image/webp", ".ico": "image/x-icon",
    ".heic": "image/heic", ".heif": "image/heif", ".avif": "image/avif",
    ".svg": "image/svg+xml", ".emf": "image/x-emf", ".wmf": "image/x-wmf",
}

#: MIME -> canonical extension (the reverse of the above, one winner per MIME)
_SUFFIX_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
    "image/bmp": ".bmp", "image/tiff": ".tif", "image/webp": ".webp",
    "image/x-icon": ".ico", "image/heic": ".heic", "image/heif": ".heif",
    "image/avif": ".avif", "image/svg+xml": ".svg", "image/x-emf": ".emf",
    "image/x-wmf": ".wmf",
}

#: every extension this package recognises as an image
IMAGE_SUFFIXES = frozenset(_MIME_BY_SUFFIX)


def mime_for_suffix(name: str) -> Optional[str]:
    """MIME type implied by a file name's extension, or ``None``."""
    if not name:
        return None
    dot = name.rfind(".")
    if dot < 0:
        return None
    return _MIME_BY_SUFFIX.get(name[dot:].lower())


def suffix_for_mime(mime: Optional[str]) -> str:
    """Canonical extension for a MIME type (``.png``); ``.img`` when unknown."""
    return _SUFFIX_BY_MIME.get(mime or "", ".img")


def sha256_hex(data) -> str:
    """Hex sha256 of ``data`` (empty string hashes to the empty-input digest)."""
    return hashlib.sha256(bytes(data or b"")).hexdigest()


def _looks_like_svg(data: bytes) -> bool:
    """True when the *root element* is ``<svg>``.

    Deliberately strict: an XHTML chapter inside an EPUB routinely contains an
    inline ``<svg>``, and misreading it as an image would inject a whole text
    file into the picture list.
    """
    head = bytes(data[:4096])
    if b"<html" in head[:1024].lower():
        return False
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    for _ in range(4):  # XML decl / doctype / comments before the root element
        if text.startswith(b"<?"):
            end = text.find(b"?>")
            if end < 0:
                return False
            text = text[end + 2:].lstrip()
        elif text.startswith(b"<!--"):
            end = text.find(b"-->")
            if end < 0:
                return False
            text = text[end + 3:].lstrip()
        elif text.startswith(b"<!"):
            end = text.find(b">")
            if end < 0:
                return False
            text = text[end + 1:].lstrip()
        else:
            break
    return text[:4].lower() == b"<svg"


def sniff_mime(data) -> Optional[str]:
    """Return the image MIME type of ``data``, or ``None`` when it is not one.

    Magic bytes only - the file name is never consulted, so a mislabelled
    member inside an archive is still classified correctly.
    """
    if not data:
        return None
    buf = bytes(data)
    head = buf[:32]
    for signature, mime in _SIGNATURES:
        if head.startswith(signature):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"hevx"):
            return "image/heic"
        if brand in (b"mif1", b"msf1"):
            return "image/heif"
        if brand in (b"avif", b"avis"):
            return "image/avif"
    if head[:4] == b"\x00\x00\x01\x00" and len(buf) >= 6:
        count = struct.unpack_from("<H", buf, 4)[0]
        if 0 < count <= 1024 and len(buf) >= 6 + 16 * count:
            return "image/x-icon"
    if head[:4] == b"\xd7\xcd\xc6\x9a":
        return "image/x-wmf"
    if head[:4] == b"\x01\x00\x00\x00" and len(buf) >= 8:
        # EMF record header: type=1 then the record size; a real EMF header
        # record is at least 88 bytes.  The extra check keeps arbitrary binary
        # that happens to start with 01 00 00 00 out of the picture list.
        size = struct.unpack_from("<I", buf, 4)[0]
        if 88 <= size <= len(buf):
            return "image/x-emf"
    if _looks_like_svg(buf):
        return "image/svg+xml"
    return None


def is_image_bytes(data, *, include_vector: bool = True) -> bool:
    """True when ``data`` is an image this package can identify."""
    mime = sniff_mime(data)
    if mime is None:
        return False
    return include_vector or mime not in VECTOR_MIMES

# --------------------------------------------------------------------------- #
# pixel dimensions
# --------------------------------------------------------------------------- #

_JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


def _jpeg_size(d: bytes) -> Optional[Tuple[int, int]]:
    """Walk JPEG segments to the first Start-Of-Frame header."""
    n = len(d)
    i = 2
    while i + 9 < n:
        if d[i] != 0xFF:
            i += 1
            continue
        marker = d[i + 1]
        if marker == 0xFF:            # fill byte
            i += 1
            continue
        if marker == 0x01 or marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            i += 2                    # stand-alone markers carry no payload
            continue
        length = struct.unpack_from(">H", d, i + 2)[0]
        if marker in _JPEG_SOF:
            h, w = struct.unpack_from(">HH", d, i + 5)
            return (w, h) if w and h else None
        i += 2 + max(length, 2)
    return None


def _tiff_size(d: bytes) -> Optional[Tuple[int, int]]:
    endian = "<" if d[:2] == b"II" else ">"
    try:
        offset = struct.unpack_from(endian + "I", d, 4)[0]
        count = struct.unpack_from(endian + "H", d, offset)[0]
        w = h = None
        for k in range(min(count, 512)):
            base = offset + 2 + k * 12
            if base + 12 > len(d):
                break
            tag, kind = struct.unpack_from(endian + "HH", d, base)
            if kind == 3:   # SHORT
                value = struct.unpack_from(endian + "H", d, base + 8)[0]
            else:           # LONG (and anything else we treat as 4 bytes)
                value = struct.unpack_from(endian + "I", d, base + 8)[0]
            if tag == 256:
                w = value
            elif tag == 257:
                h = value
            if w and h:
                break
        return (w, h) if w and h else None
    except struct.error:
        return None


def _webp_size(d: bytes) -> Optional[Tuple[int, int]]:
    chunk = d[12:16]
    try:
        if chunk == b"VP8X" and len(d) >= 30:
            w = int.from_bytes(d[24:27], "little") + 1
            h = int.from_bytes(d[27:30], "little") + 1
            return (w, h) if w > 0 and h > 0 else None
        if chunk == b"VP8 " and d[23:26] == b"\x9d\x01\x2a":
            w = struct.unpack_from("<H", d, 26)[0] & 0x3FFF
            h = struct.unpack_from("<H", d, 28)[0] & 0x3FFF
            return (w, h) if w and h else None
        if chunk == b"VP8L" and d[20:21] == b"\x2f" and len(d) >= 25:
            b0, b1, b2, b3 = d[21], d[22], d[23], d[24]
            w = 1 + (((b1 & 0x3F) << 8) | b0)
            h = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
            return (w, h)
    except struct.error:
        return None
    return None


def _ispe_size(d: bytes) -> Optional[Tuple[int, int]]:
    """HEIC/HEIF/AVIF: find the ``ispe`` (image spatial extents) property."""
    limit = min(len(d), 16384)
    i = d.find(b"ispe", 0, limit)
    while i >= 0 and i + 12 <= len(d):
        try:
            w, h = struct.unpack_from(">II", d, i + 8)
        except struct.error:
            return None
        if w and h:
            return (w, h)
        i = d.find(b"ispe", i + 4, limit)
    return None


_SVG_ROOT = re.compile(rb"<svg\b[^>]*>", re.IGNORECASE | re.DOTALL)
_SVG_NUM = re.compile(rb"""(width|height|viewBox)\s*=\s*("([^"]*)"|'([^']*)')""", re.IGNORECASE)
_CSS_UNITS = {"": 1.0, "px": 1.0, "pt": 96.0 / 72.0, "pc": 16.0,
              "in": 96.0, "cm": 96.0 / 2.54, "mm": 96.0 / 25.4,
              "q": 96.0 / 25.4 / 4.0}
_NUM_RE = re.compile(r"^([-+]?[0-9]*\.?[0-9]+)([a-z%]*)$", re.IGNORECASE)


def _svg_length(value: str) -> Optional[float]:
    match = _NUM_RE.match(value.strip())
    if not match:
        return None
    unit = match.group(2).lower()
    if unit == "%" or unit not in _CSS_UNITS:
        return None
    return float(match.group(1)) * _CSS_UNITS[unit]


def _svg_size(d: bytes) -> Optional[Tuple[int, int]]:
    """Pixel size of an SVG: ``width``/``height`` first, then ``viewBox``."""
    root = _SVG_ROOT.search(d[:8192])
    if root is None:
        return None
    attrs = {}
    for name, _q, dq, sq in _SVG_NUM.findall(root.group(0)):
        attrs[name.decode("ascii", "replace").lower()] = (dq or sq).decode("utf-8", "replace")
    w = _svg_length(attrs.get("width", "")) if "width" in attrs else None
    h = _svg_length(attrs.get("height", "")) if "height" in attrs else None
    if w and h:
        return int(round(w)), int(round(h))
    box = attrs.get("viewbox")
    if box:
        parts = box.replace(",", " ").split()
        if len(parts) == 4:
            try:
                bw, bh = float(parts[2]), float(parts[3])
            except ValueError:
                return None
            if bw > 0 and bh > 0:
                return int(round(bw)), int(round(bh))
    if w or h:
        side = int(round(w or h or 0))
        return (side, side) if side else None
    return None


def _bmp_size(d: bytes) -> Optional[Tuple[int, int]]:
    try:
        w = struct.unpack_from("<i", d, 18)[0]
        h = abs(struct.unpack_from("<i", d, 22)[0])
    except struct.error:
        return None
    return (w, h) if w > 0 and h > 0 else None


def _ico_size(d: bytes) -> Optional[Tuple[int, int]]:
    """Largest frame in an ICO directory.

    Each side is one byte where 0 means 256.  A multi-resolution icon holds
    several frames; reporting the biggest one matches what Pillow reports and
    what a VL model would want to see.
    """
    count = struct.unpack_from("<H", d, 4)[0]
    best: Optional[Tuple[int, int]] = None
    for k in range(min(count, 1024)):
        base = 6 + k * 16
        if base + 2 > len(d):
            break
        w = d[base] or 256
        h = d[base + 1] or 256
        if best is None or w * h > best[0] * best[1]:
            best = (w, h)
    return best


def _pillow_size(d: bytes) -> Optional[Tuple[int, int]]:
    try:
        import io

        from PIL import Image
    except Exception:  # noqa: BLE001 - Pillow is an optional extra
        return None
    try:
        with Image.open(io.BytesIO(d)) as img:
            return tuple(int(v) for v in img.size)  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 - undecodable payload
        return None


def image_dimensions(data, mime: Optional[str] = None) -> Optional[Tuple[int, int]]:
    """``(width, height)`` in pixels, or ``None`` when the header is unreadable.

    Native parsers handle PNG/GIF/BMP/ICO/JPEG/WEBP/TIFF/HEIF/SVG; everything
    else is offered to Pillow when it is installed.  Never raises.
    """
    if not data:
        return None
    d = bytes(data)
    mime = mime or sniff_mime(d)
    if mime == "image/png" and len(d) >= 24 and d[12:16] == b"IHDR":
        w, h = struct.unpack_from(">II", d, 16)
        return (w, h) if w and h else None
    if mime == "image/gif" and len(d) >= 10:
        w, h = struct.unpack_from("<HH", d, 6)
        return (w, h) if w and h else None
    if mime == "image/bmp":
        return _bmp_size(d)
    if mime == "image/x-icon" and len(d) >= 8:
        return _ico_size(d)
    if mime == "image/jpeg":
        return _jpeg_size(d) or _pillow_size(d)
    if mime == "image/webp":
        return _webp_size(d) or _pillow_size(d)
    if mime == "image/tiff":
        return _tiff_size(d) or _pillow_size(d)
    if mime in ("image/heic", "image/heif", "image/avif"):
        return _ispe_size(d) or _pillow_size(d)
    if mime == "image/svg+xml":
        return _svg_size(d)
    return _pillow_size(d)