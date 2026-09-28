# coding=utf-8
"""Multimodal slicing: get the pictures out, and line them up with the paragraphs.

The text handlers already emit :class:`~smart_slice.types.ImageAsset` objects for
the containers they understand (docx, pptx, pdf, xlsx, zip) through the
``save_image`` callback.  Three gaps made that insufficient for a vision-language
embedding pipeline:

1. **Nothing collected them.**  ``save_image`` is a caller-supplied hook, so a
   caller who wanted the pictures had to write the sink, the deduplication and
   the id remapping themselves.  :class:`ImageCollector` is that sink.
2. **Some formats never emitted any.**  ``<img>`` in HTML, EPUB chapters,
   standalone image files, MIME mail bodies and (in practice) spreadsheet cell
   pictures produced text but no assets.  :func:`extract_media` reads the
   container directly and recovers them, independent of the handlers.
3. **Nothing joined them back to the text.**  An image is only useful next to the
   paragraph it illustrates.  :func:`attach_images` matches on the
   ``./oss/file/{id}`` reference the handlers write into the markdown, then falls
   back to the nearest heading / preceding sentence for recovered images that
   have no reference.

The package still performs **no network I/O**: an ``<img src="https://...">`` is
recorded as an asset with ``meta["src"]`` and zero bytes, never downloaded.

Typical use::

    from smart_slice import slice_multimodal

    result = slice_multimodal(path="report.docx", limit=1000)
    for record in result.records(with_data=True):
        embed(record["title"], record["content"], record["images"])
"""
from __future__ import annotations

import base64
import binascii
import io
import os
import posixpath
import re
import threading
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from smart_slice._images import (
    VECTOR_MIMES,
    image_dimensions,
    is_image_bytes,
    mime_for_suffix,
    sniff_mime,
    suffix_for_mime,
)
from smart_slice._logging import get_logger
from smart_slice.types import ImageAsset, Paragraph

_log = get_logger("multimodal")

__all__ = [
    "ImageCollector",
    "MediaExtraction",
    "MultimodalParagraph",
    "MultimodalResult",
    "attach_images",
    "container_kind",
    "extract_media",
    "scan_media",
    "slice_multimodal",
    "slice_path_multimodal",
    "DEFAULT_MAX_IMAGES",
    "DEFAULT_MAX_IMAGE_BYTES",
    "OSS_REF",
]

#: default ceiling on how many images one document may contribute
DEFAULT_MAX_IMAGES = 512
#: default ceiling on a single image's byte size (64 MiB)
DEFAULT_MAX_IMAGE_BYTES = 64 * 1024 * 1024

#: text-ish members inside a container that may carry image references
_TEXT_SUFFIXES = (
    ".html", ".htm", ".xhtml", ".shtml", ".xml", ".md", ".markdown",
    ".txt", ".svg", ".opf", ".ncx", ".css",
)

#: extensions whose bytes *are* the image
_STANDALONE_IMAGE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".ico", ".heic", ".heif", ".avif", ".svg", ".emf", ".wmf",
)

_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_ATTR_SRC = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE)
_MD_IMG = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)")
# IGNORECASE: markup in the wild upper-cases both the scheme and the parameters
_DATA_URI = re.compile(r"^data:([^;,]*)?(;[^,]*)?,(.*)$", re.DOTALL | re.IGNORECASE)
_TAG_STRIP = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_HTML_HEADING = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
#: ``./oss/file/{id}`` reference written into paragraph text by the handlers
OSS_REF = re.compile(r"\./oss/file/([0-9A-Za-z][0-9A-Za-z._\-]*)")
_ANCHOR_CHARS = 96
#: bytes read from an archive member before deciding whether it is a picture
_SNIFF_PREFIX = 1024


def _decode(data: bytes) -> str:
    """Best-effort text decode for reference scanning (never raises)."""
    for encoding in ("utf-8", "utf-16", "cp1252"):
        try:
            return bytes(data).decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return bytes(data).decode("utf-8", "replace")

# --------------------------------------------------------------------------- #
# the sink
# --------------------------------------------------------------------------- #


class ImageCollector:
    """A ``save_image`` sink that keeps every image a document produced.

    Drop it straight into :func:`smart_slice.slice_bytes`::

        collector = ImageCollector()
        paragraphs = slice_bytes(data, "report.docx", save_image=collector)
        collector.assets          # -> [ImageAsset, ...]

    or let :func:`slice_multimodal` do it for you.

    **Deduplication.**  Documents repeat images (a logo on every slide, the same
    chart pasted twice).  With ``dedupe=True`` - the default - the first asset
    carrying a given sha256 is kept and later ones are dropped, and the call
    returns ``{duplicate_id: kept_id}``.  That is exactly the contract
    :func:`smart_slice.split_document` implements for ``save_image``: it applies
    the mapping to the whole result tree, so every ``./oss/file/{id}`` reference
    in the paragraphs converges on the surviving id.  Assets without bytes are
    never deduplicated (they would all hash to the empty digest).

    **Thread safety.**  :func:`smart_slice.slice_many` shares one sink across all
    workers, so every mutation happens under a lock.  ``backend="process"``
    cannot use it at all: a lock is not picklable.
    """

    def __init__(
        self,
        *,
        dedupe: bool = True,
        keep_content: bool = True,
        max_images: Optional[int] = DEFAULT_MAX_IMAGES,
        on_asset: Optional[Any] = None,
    ) -> None:
        self.dedupe = dedupe
        self.keep_content = keep_content
        self.max_images = max_images
        self.on_asset = on_asset
        self._lock = threading.Lock()
        self._assets: List[ImageAsset] = []
        self._by_sha: Dict[str, ImageAsset] = {}
        self._remapped: Dict[str, str] = {}
        self._dropped = 0
        self._overflow = 0

    # -- sink protocol ----------------------------------------------------- #

    def __call__(self, image_list: Optional[Iterable[Any]]) -> Dict[str, str]:
        """Receive the assets one handler produced; return the dedup mapping."""
        return self.collect(image_list)[0]

    def collect(self, image_list: Optional[Iterable[Any]]) -> Tuple[Dict[str, str], List[ImageAsset]]:
        """``({dropped_id: kept_id}, [asset this batch of references resolves to])``.

        The second element is what :func:`slice_multimodal` uses to know which
        pictures belong to the document it is slicing: the returned assets are the
        ones *this call* is responsible for, whether they were freshly added or
        deduplicated onto something an earlier call had already stored.
        """
        mapping: Dict[str, str] = {}
        kept: List[ImageAsset] = []
        if not image_list:
            return mapping, kept
        seen: set = set()
        with self._lock:
            for asset in image_list:
                survivor = self._add_locked(asset)
                if survivor is None or id(survivor) in seen:
                    continue
                seen.add(id(survivor))
                kept.append(survivor)
                if survivor is not asset:
                    mapping[str(asset.id)] = str(survivor.id)
            if mapping:
                self._remapped.update(mapping)
        return mapping, kept

    def _add_locked(self, asset: Any) -> Optional[ImageAsset]:
        if not isinstance(asset, ImageAsset):
            return None
        payload = asset.content
        if self.dedupe and payload:
            digest = asset.sha256
            existing = self._by_sha.get(digest)
            if existing is not None:
                self._dropped += 1
                self._merge_locked(existing, asset)
                return existing
        if self.max_images is not None and len(self._assets) >= self.max_images:
            self._overflow += 1
            return None
        if not self.keep_content and "content" in asset.meta:
            asset.meta = {k: v for k, v in asset.meta.items() if k != "content"}
        asset.meta.setdefault("mime", asset.mime_type)
        self._assets.append(asset)
        if payload:
            self._by_sha[asset.sha256] = asset
        return asset

    @staticmethod
    def _merge_locked(kept: ImageAsset, duplicate: ImageAsset) -> None:
        """Fold a duplicate's provenance into the surviving asset."""
        for key in ("anchor", "heading", "src", "page", "member"):
            if kept.meta.get(key) in (None, "") and duplicate.meta.get(key) not in (None, ""):
                kept.meta[key] = duplicate.meta[key]
        refs = kept.meta.setdefault("also_referenced_as", [])
        if isinstance(refs, list) and str(duplicate.id) not in refs:
            refs.append(str(duplicate.id))

    # -- collection helpers ------------------------------------------------ #

    def extend(self, assets: Iterable[Any]) -> List[ImageAsset]:
        """Add already-built assets (e.g. from :func:`extract_media`)."""
        accepted: List[ImageAsset] = []
        with self._lock:
            for asset in assets:
                kept = self._add_locked(asset)
                if kept is asset:
                    accepted.append(asset)
        return accepted

    @property
    def assets(self) -> List[ImageAsset]:
        """Every unique image collected so far, in arrival order."""
        with self._lock:
            return list(self._assets)

    @property
    def remapped(self) -> Dict[str, str]:
        """``{dropped_id: surviving_id}`` for every duplicate seen so far."""
        with self._lock:
            return dict(self._remapped)

    @property
    def duplicates(self) -> int:
        """How many assets were dropped as content-identical repeats."""
        with self._lock:
            return self._dropped

    @property
    def overflow(self) -> int:
        """How many assets were dropped because ``max_images`` was reached."""
        with self._lock:
            return self._overflow

    def known_sha(self) -> set:
        """Content hashes already held - lets a caller avoid re-extracting."""
        with self._lock:
            return set(self._by_sha)

    def clear(self) -> None:
        with self._lock:
            self._assets.clear()
            self._by_sha.clear()
            self._remapped.clear()
            self._dropped = 0
            self._overflow = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._assets)

    def __iter__(self):
        return iter(self.assets)

    def __repr__(self) -> str:
        return (
            f"ImageCollector(images={len(self)}, bytes={sum(a.size for a in self)}, "
            f"duplicates={self.duplicates}, dropped={self.overflow})"
        )

# --------------------------------------------------------------------------- #
# container detection
# --------------------------------------------------------------------------- #


def container_kind(content: bytes, name: str = "") -> str:
    """Classify the bytes so :func:`extract_media` knows how to open them.

    One of ``"zip"`` (OOXML / ODF / EPUB / plain archive), ``"pdf"``,
    ``"mime"`` (eml / mhtml), ``"image"`` (the file *is* a picture),
    ``"markup"`` (HTML / markdown / any text that can hold an image
    reference) or ``"unknown"``.
    """
    data = bytes(content or b"")
    head = data[:16]
    low = (name or "").lower()
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head[:5] == b"%PDF-":
        return "pdf"
    if low.endswith((".eml", ".mht", ".mhtml")):
        return "mime"
    if sniff_mime(data):
        return "image"
    if low.endswith(_STANDALONE_IMAGE_SUFFIXES):
        return "image"
    window = data[:65536]
    if re.search(rb"(?im)^MIME-Version:", window) and b"Content-" in window:
        return "mime"
    if low.endswith((".html", ".htm", ".xhtml", ".shtml", ".svg", ".xml", ".md", ".markdown")):
        return "markup"
    if re.search(rb"(?i)<img[\s>/]", window) or b"data:image/" in window or b"![[" in window:
        return "markup"
    if re.search(rb"(?i)!\[[^\]]*\]\(\s*(data:image/|[^)]+\.(png|jpe?g|gif|webp|bmp|svg))", window):
        return "markup"
    return "unknown"


# --------------------------------------------------------------------------- #
# the scanner
# --------------------------------------------------------------------------- #


@dataclass
class MediaExtraction:
    """What one :func:`scan_media` pass did.

    :param assets:   recovered images, in document order, deduplicated
    :param kind:     :func:`container_kind` of the input
    :param scanned:  members / parts / tags inspected
    :param skipped:  candidates dropped by policy (too big, too small, vector,
                     over the image cap)
    :param errors:   non-fatal problems worth logging (a corrupt member, a
                     missing optional dependency)
    """

    assets: List[ImageAsset] = field(default_factory=list)
    kind: str = "unknown"
    scanned: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.assets)

    def __iter__(self):
        return iter(self.assets)

    @property
    def external(self) -> List[ImageAsset]:
        """References that were recorded but not fetched (no bytes, has a URL)."""
        return [a for a in self.assets if not a.content and a.meta.get("src")]

    def summary(self) -> str:
        return (
            f"{len(self.assets)} images from a {self.kind} container "
            f"({self.scanned} candidates scanned, {self.skipped} skipped"
            + (f", {len(self.errors)} errors" if self.errors else "")
            + ")"
        )


class _Scanner:
    """Accumulator shared by every container reader."""

    def __init__(self, *, max_images: Optional[int], max_image_bytes: Optional[int],
                 include_vector: bool, include_external: bool, min_side: Optional[int],
                 mime_by_member: Optional[Dict[str, str]] = None) -> None:
        self.max_images = max_images
        self.max_image_bytes = max_image_bytes
        self.include_vector = include_vector
        self.include_external = include_external
        self.min_side = min_side
        self.report = MediaExtraction()
        self._by_sha: Dict[str, ImageAsset] = {}
        self._by_ref: Dict[str, ImageAsset] = {}
        self._order: List[ImageAsset] = []
        self.mime_by_member = mime_by_member or {}

    # -- guards ----------------------------------------------------------- #

    def _full(self) -> bool:
        return self.max_images is not None and len(self._order) >= self.max_images

    # -- intake ----------------------------------------------------------- #

    def add_bytes(self, data: bytes, *, file_name: str = "", mime: Optional[str] = None,
                  ref: Optional[str] = None, **meta: Any) -> Optional[ImageAsset]:
        """Register raw picture bytes; returns the asset that ended up keeping them."""
        if self._full():
            self.report.skipped += 1
            return None
        if not data:
            return None
        if self.max_image_bytes is not None and len(data) > self.max_image_bytes:
            self.report.skipped += 1
            return None
        detected = mime or sniff_mime(data)
        if detected is None:
            detected = mime_for_suffix(file_name)
        if detected is None or detected == "application/octet-stream":
            self.report.skipped += 1
            return None
        if not self.include_vector and detected in VECTOR_MIMES:
            self.report.skipped += 1
            return None
        if self.min_side:
            dims = image_dimensions(data, detected)
            if dims and min(dims) < self.min_side:
                self.report.skipped += 1
                return None
        payload = {k: v for k, v in meta.items() if v not in (None, "")}
        payload["content"] = bytes(data)
        payload["mime"] = detected
        from smart_slice import _uuid

        asset = ImageAsset(id=_uuid.uuid7(), file_name=file_name or f"image{suffix_for_mime(detected)}",
                           meta=payload)
        digest = asset.sha256
        existing = self._by_sha.get(digest)
        if existing is not None:
            self._merge(existing, asset)
            if ref:
                self._by_ref[ref] = existing
            return existing
        self._by_sha[digest] = asset
        self._order.append(asset)
        if ref:
            self._by_ref[ref] = asset
        return asset

    def add_reference(self, src: str, *, file_name: str = "", mime: Optional[str] = None,
                      ref: Optional[str] = None, **meta: Any) -> Optional[ImageAsset]:
        """Register an image the package will not fetch (a remote URL)."""
        if ref and ref in self._by_ref:
            return self._by_ref[ref]
        if not self.include_external or self._full():
            self.report.skipped += 1
            return None
        payload = {k: v for k, v in meta.items() if v not in (None, "")}
        payload["src"] = src
        payload["external"] = True
        if mime:
            payload["mime"] = mime
        from smart_slice import _uuid

        asset = ImageAsset(id=_uuid.uuid7(), file_name=file_name or src.rsplit("/", 1)[-1][:120],
                           meta=payload)
        self._order.append(asset)
        if ref:
            self._by_ref[ref] = asset
        return asset

    @staticmethod
    def _merge(kept: ImageAsset, duplicate: ImageAsset) -> None:
        for key, value in duplicate.meta.items():
            if key in ("content", "mime"):
                continue
            if kept.meta.get(key) in (None, "", []):
                kept.meta[key] = value

    def lookup(self, ref: str) -> Optional[ImageAsset]:
        return self._by_ref.get(ref)

    def finish(self) -> MediaExtraction:
        self.report.assets = list(self._order)
        return self.report

# --------------------------------------------------------------------------- #
# reference scanning (HTML / markdown / MHTML bodies)
# --------------------------------------------------------------------------- #


def _decode_data_uri(src: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Split a ``data:`` URI into ``(payload, mime)``; ``(None, mime)`` if broken."""
    match = _DATA_URI.match(src.strip())
    if match is None:
        return None, None
    # lower-cased: markup in the wild upper-cases the declared media type
    mime = (match.group(1) or "").strip().lower() or None
    params = (match.group(2) or "").lower()
    payload = re.sub(r"\s+", "", match.group(3) or "")
    if "base64" in params or _is_base64(payload):
        try:
            return base64.b64decode(payload, validate=False), mime
        except (binascii.Error, ValueError):
            return None, mime
    from urllib.parse import unquote_to_bytes

    try:
        return unquote_to_bytes(payload), mime
    except Exception:  # noqa: BLE001 - malformed escape sequence
        return None, mime


_BASE64ISH = re.compile(r"^[A-Za-z0-9+/\-_=]+$")


def _is_base64(payload: str) -> bool:
    return len(payload) >= 24 and bool(_BASE64ISH.match(payload))


def _context(text: str, pos: int) -> Tuple[str, str]:
    """Nearest preceding heading and a short plain-text anchor for ``text[pos:]``.

    Both are *heuristics* used only to line a recovered image up with a
    paragraph; they never modify the sliced text.  The search window is capped so
    a page with many images stays linear rather than quadratic.
    """
    window = text[max(0, pos - 20000):pos]
    heading = ""
    found = None
    for found in _MD_HEADING.finditer(window):
        pass
    if found is not None:
        heading = found.group(2).strip()
    else:
        for found in _HTML_HEADING.finditer(window):
            pass
        if found is not None:
            heading = _WS.sub(" ", _TAG_STRIP.sub(" ", found.group(2))).strip()
    plain = _WS.sub(" ", _TAG_STRIP.sub(" ", window)).strip()
    return heading[:256], plain[-_ANCHOR_CHARS:]


def _annotate(asset: ImageAsset, heading: str, anchor: str, member: str) -> None:
    if heading and not asset.meta.get("heading"):
        asset.meta["heading"] = heading
    if anchor and not asset.meta.get("anchor"):
        asset.meta["anchor"] = anchor
    if member:
        seen = asset.meta.setdefault("referenced_in", [])
        if isinstance(seen, list) and member not in seen:
            seen.append(member)


def _emit_src(scanner: "_Scanner", src: str, *, source: str, member: str = "",
              base: str = "", heading: str = "", anchor: str = "",
              resolver=None) -> Optional[ImageAsset]:
    # ``resolver`` is supplied by container readers that can turn a relative
    # reference back into a member they already scanned (EPUB chapters, MHTML
    # bodies); ``None`` means every non-data src is an un-fetched external URL.
    src = (src or "").strip()
    if not src or src.startswith("#"):
        return None
    if src.lower().startswith("data:"):
        payload, mime = _decode_data_uri(src)
        if not payload:
            scanner.report.skipped += 1
            scanner.report.errors.append(f"undecodable data URI in {member or source}")
            return None
        detected = mime or sniff_mime(payload)
        return scanner.add_bytes(
            payload,
            file_name="inline" + suffix_for_mime(detected),
            mime=detected,
            source=source, member=member, heading=heading, anchor=anchor, inline=True,
        )
    if resolver is not None:
        target = resolver(src, base)
        if target:
            existing = scanner.lookup(target)
            if existing is not None:
                _annotate(existing, heading, anchor, member)
                return existing
    return scanner.add_reference(src, source=source, member=member,
                                 mime=mime_for_suffix(src.split("?")[0]),
                                 heading=heading, anchor=anchor)


def _scan_markup(scanner: "_Scanner", text: str, *, source: str, member: str = "",
                 base: str = "", resolver=None) -> None:
    """Pick every ``<img src>`` and markdown ``![](...)`` out of a text body."""
    for match in _IMG_TAG.finditer(text):
        scanner.report.scanned += 1
        attr = _ATTR_SRC.search(match.group(0))
        if attr is None:
            continue
        src = attr.group(1) or attr.group(2) or attr.group(3)
        heading, anchor = _context(text, match.start())
        _emit_src(scanner, src, source=source, member=member, base=base,
                  heading=heading, anchor=anchor, resolver=resolver)
    for match in _MD_IMG.finditer(text):
        scanner.report.scanned += 1
        heading, anchor = _context(text, match.start())
        _emit_src(scanner, match.group(1), source=source, member=member, base=base,
                  heading=heading, anchor=anchor, resolver=resolver)


# --------------------------------------------------------------------------- #
# container readers
# --------------------------------------------------------------------------- #


def _scan_zip(scanner: "_Scanner", data: bytes, name: str) -> None:
    """OOXML (docx/pptx/xlsx), ODF, EPUB and plain image archives.

    Two passes: picture members first (so a later reference can be resolved to
    an asset that already exists), then text members for ``data:`` URIs and for
    ``<img src="images/foo.png">`` style links.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - a corrupt archive is not fatal
        scanner.report.errors.append(f"cannot open zip container: {exc}")
        return
    with archive:
        try:
            infos = [info for info in archive.infolist() if not info.is_dir()]
        except Exception as exc:  # noqa: BLE001
            scanner.report.errors.append(f"cannot list zip container: {exc}")
            return
        members: Dict[str, ImageAsset] = {}
        for info in infos:
            scanner.report.scanned += 1
            if info.file_size == 0:
                continue
            if scanner.max_image_bytes is not None and info.file_size > scanner.max_image_bytes:
                scanner.report.skipped += 1
                continue
            try:
                # Sniff a prefix first: an archive full of documents would
                # otherwise be decompressed twice (once here, once by the
                # handler) just to discover it holds no pictures.
                with archive.open(info) as stream:
                    payload = stream.read(_SNIFF_PREFIX)
                detected = sniff_mime(payload)
                if detected is None and mime_for_suffix(info.filename):
                    payload = archive.read(info)
                    detected = sniff_mime(payload)
                elif detected is not None and info.file_size > len(payload):
                    payload = archive.read(info)
            except Exception as exc:  # noqa: BLE001 - one bad member does not end the scan
                scanner.report.errors.append(f"unreadable zip member {info.filename}: {exc}")
                continue
            if detected is None:
                continue
            key = posixpath.normpath(info.filename)
            asset = scanner.add_bytes(
                payload,
                file_name=posixpath.basename(info.filename),
                mime=detected,
                ref=key,
                source="zip",
                member=info.filename,
                container=name,
            )
            if asset is not None:
                members[key] = asset
                members[key.lower()] = asset
                members[posixpath.basename(key)] = asset
                members[posixpath.basename(key).lower()] = asset
        scanner.mime_by_member = {k: v.mime_type for k, v in members.items()}

        def resolve(src: str, base: str = "") -> Optional[str]:
            """Map a relative ``src`` onto a member already scanned for pictures."""
            from urllib.parse import unquote

            candidate = unquote(src.split("#")[0].split("?")[0])
            if not candidate or candidate.lower().startswith(("cid:", "mailto:", "javascript:")):
                return None
            guesses = [candidate]
            if base:
                guesses.insert(0, posixpath.normpath(posixpath.join(base, candidate)))
            for guess in guesses:
                normalised = posixpath.normpath(guess).lstrip("/")
                for key in (normalised, normalised.lower(),
                            posixpath.basename(normalised), posixpath.basename(normalised).lower()):
                    if key in members:
                        return key
            return None

        for info in infos:
            low = info.filename.lower()
            if not low.endswith(_TEXT_SUFFIXES) or low.endswith(".svg"):
                continue
            if info.file_size > 8 * 1024 * 1024:
                continue
            try:
                payload = archive.read(info)
            except Exception:  # noqa: BLE001
                continue
            text = _decode(payload)
            if "<img" not in text.lower() and "![" not in text:
                continue
            scanner.report.scanned += 1
            _scan_markup(scanner, text, source="zip", member=info.filename,
                         base=posixpath.dirname(info.filename), resolver=resolve)


def _load_pdf_reader():
    for module, symbol in (("pypdf", "PdfReader"), ("PyPDF2", "PdfReader")):
        try:
            return getattr(__import__(module, fromlist=[symbol]), symbol)
        except Exception:  # noqa: BLE001 - try the next backend
            continue
    return None


def _scan_pdf(scanner: "_Scanner", data: bytes, name: str) -> None:
    reader_cls = _load_pdf_reader()
    if reader_cls is None:
        scanner.report.errors.append("no PDF backend installed (pip install smart-slice[pdf])")
        return
    try:
        reader = reader_cls(io.BytesIO(data))
        pages = list(reader.pages)
    except Exception as exc:  # noqa: BLE001
        scanner.report.errors.append(f"cannot open pdf: {exc}")
        return
    for number, page in enumerate(pages, start=1):
        try:
            # pypdf hands back a lazy list, so materialising it can already fail
            # on a broken xobject - the whole page has to be inside the guard.
            images = list(page.images)
        except Exception as exc:  # noqa: BLE001 - inline images / broken xobjects
            scanner.report.errors.append(f"page {number} images unreadable: {exc}")
            continue
        for image in images:
            scanner.report.scanned += 1
            try:
                payload = image.data
                label = getattr(image, "name", "") or ""
            except Exception as exc:  # noqa: BLE001
                scanner.report.errors.append(f"page {number} image unreadable: {exc}")
                continue
            scanner.add_bytes(payload, file_name=label, source="pdf", page=number, container=name)


def _scan_mime(scanner: "_Scanner", data: bytes, name: str) -> None:
    """eml / mhtml: image parts plus the references inside the HTML body."""
    import email
    import email.policy

    try:
        message = email.message_from_bytes(data, policy=email.policy.compat32)
    except Exception as exc:  # noqa: BLE001
        scanner.report.errors.append(f"cannot parse MIME message: {exc}")
        return
    bodies: List[Tuple[str, str]] = []
    refs: Dict[str, ImageAsset] = {}
    for part in message.walk():
        scanner.report.scanned += 1
        ctype = (part.get_content_type() or "").lower()
        try:
            payload = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001
            payload = None
        cid = (part.get("Content-ID") or "").strip().strip("<>")
        location = (part.get("Content-Location") or "").strip()
        if ctype.startswith("image/"):
            asset = scanner.add_bytes(
                payload or b"",
                file_name=part.get_filename() or "",
                mime=ctype,
                source="mime", part=ctype, container=name,
                **({"content_id": cid} if cid else {}),
                **({"content_location": location} if location else {}),
            )
            if asset is not None:
                for key in filter(None, (f"cid:{cid}" if cid else "", location,
                                         posixpath.basename(location) if location else "")):
                    refs[key] = asset
                    refs[key.lower()] = asset
                    scanner._by_ref[key] = asset
        elif ctype in ("text/html", "text/xhtml", "text/plain") and payload:
            bodies.append((_decode(payload), ctype))

    def resolve(src: str, base: str = "") -> Optional[str]:
        """``cid:`` ids and ``Content-Location`` values both name a MIME part."""
        if not src:
            return None
        for key in (src, src.lower(), posixpath.basename(src),
                    posixpath.basename(src).lower()):
            if key in refs:
                return key
        return None

    for text, ctype in bodies:
        if "<img" not in text.lower() and "![" not in text:
            continue
        _scan_markup(scanner, text, source="mime", member=ctype, base="", resolver=resolve)


def _scan_markup_container(scanner: "_Scanner", data: bytes, name: str) -> None:
    text = _decode(data)
    _scan_markup(scanner, text, source="file", member=name, base=posixpath.dirname(name))


def _scan_standalone(scanner: "_Scanner", data: bytes, name: str) -> None:
    """The file *is* the picture: tag it so it can be pinned to the one paragraph."""
    scanner.add_bytes(data, file_name=name or "image", source="file",
                      container=name, role="document")


def scan_media(
    content: bytes,
    name: str = "",
    *,
    max_images: Optional[int] = DEFAULT_MAX_IMAGES,
    max_image_bytes: Optional[int] = DEFAULT_MAX_IMAGE_BYTES,
    include_vector: bool = False,
    include_external: bool = True,
    min_side: Optional[int] = None,
) -> MediaExtraction:
    """Recover every image reachable from ``content``, whatever the container.

    This is the half of multimodal support the format handlers do *not* cover:
    ``<img>`` tags in HTML, EPUB chapters, MIME mail bodies, PDF pages and
    standalone picture files.  It reads the container itself, so it works even
    for formats whose handler emits text only - and it deduplicates by content
    hash, so running it next to a handler that already produced the same picture
    costs memory but never yields a duplicate.

    :param content:         complete file bytes
    :param name:            file name - decides the container when magic bytes
                            are ambiguous (a bare ``.md`` string, say)
    :param max_images:      cap on recovered images per document (``None`` = no cap)
    :param max_image_bytes: skip any single image larger than this (``None`` = no cap)
    :param include_vector:  also return SVG/EMF/WMF.  Off by default: a VL
                            embedding model cannot consume them without a renderer
    :param include_external: record ``<img src="https://...">`` as an asset with
                            zero bytes and ``meta["src"]`` set.  The package never
                            performs network I/O - fetching is the caller's job
    :param min_side:        drop images whose smaller side is below this many
                            pixels (decorative icons, bullets, tracking pixels);
                            ``None`` keeps everything
    :returns:               a :class:`MediaExtraction`
    """
    data = bytes(content or b"")
    kind = container_kind(data, name)
    scanner = _Scanner(
        max_images=max_images, max_image_bytes=max_image_bytes,
        include_vector=include_vector, include_external=include_external,
        min_side=min_side,
    )
    scanner.report.kind = kind
    try:
        if kind == "zip":
            _scan_zip(scanner, data, name)
        elif kind == "pdf":
            _scan_pdf(scanner, data, name)
        elif kind == "mime":
            _scan_mime(scanner, data, name)
        elif kind == "image":
            _scan_standalone(scanner, data, name)
        elif kind == "markup":
            _scan_markup_container(scanner, data, name)
        else:
            scanner.report.errors.append("container not recognised; nothing extracted")
    except Exception as exc:  # noqa: BLE001 - extraction is best effort by contract
        _log.warning(f"media scan failed for {name or '<bytes>'}: {exc}")
        scanner.report.errors.append(f"{type(exc).__name__}: {exc}")
    return scanner.finish()


def extract_media(
    content: bytes,
    name: str = "",
    **options: Any,
) -> List[ImageAsset]:
    """Shortcut for ``scan_media(content, name, **options).assets``."""
    return scan_media(content, name, **options).assets

# --------------------------------------------------------------------------- #
# joining pictures back to paragraphs
# --------------------------------------------------------------------------- #


def _normalise_title(value: Any) -> str:
    return _WS.sub(" ", _TAG_STRIP.sub(" ", str(value or ""))).strip().lower()


def _plain(text: str) -> str:
    return _WS.sub(" ", _TAG_STRIP.sub(" ", text or "")).strip()


@dataclass
class MultimodalParagraph:
    """One sliced paragraph plus the pictures that belong to it."""

    index: int
    title: str
    content: str
    images: List[ImageAsset] = field(default_factory=list)

    @property
    def has_images(self) -> bool:
        return bool(self.images)

    def to_dict(self, *, with_data: bool = False) -> Dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "content": self.content,
            "images": [image.to_dict(with_data=with_data) for image in self.images],
        }


@dataclass
class MultimodalResult:
    """Paragraphs and pictures, already lined up.

    :param paragraphs:   the ``[{title, content}]`` rows :func:`slice_bytes` produced
    :param images:       every image belonging to the document (handler-emitted
                         plus recovered), deduplicated by content hash
    :param groups:       one :class:`MultimodalParagraph` per paragraph, aligned
                         by index - this is where the join lives
    :param unattached:   images that no paragraph claims (a logo in the file but
                         not in the text, a remote URL, a spreadsheet cell picture)
    :param name:         source file name
    :param extraction:   the :class:`MediaExtraction` report, when probing ran
    :param collector:    the :class:`ImageCollector` used, when one was installed
    """

    paragraphs: List[Paragraph] = field(default_factory=list)
    images: List[ImageAsset] = field(default_factory=list)
    groups: List[MultimodalParagraph] = field(default_factory=list)
    unattached: List[ImageAsset] = field(default_factory=list)
    name: str = ""
    extraction: Optional[MediaExtraction] = None
    collector: Optional[ImageCollector] = None

    def __len__(self) -> int:
        return len(self.groups)

    def __iter__(self):
        return iter(self.groups)

    def __getitem__(self, index: int) -> MultimodalParagraph:
        return self.groups[index]

    @property
    def attached(self) -> List[ImageAsset]:
        """Images that landed on a paragraph."""
        return [image for group in self.groups for image in group.images]

    @property
    def coverage(self) -> float:
        """Fraction of images attached to a paragraph (0.0 - 1.0)."""
        if not self.images:
            return 1.0
        return len(self.attached) / len(self.images)

    def records(self, *, with_data: bool = False) -> List[Dict[str, Any]]:
        """Every paragraph as a plain dict with an ``images`` list.

        This is the shape a multimodal index builder wants: text for the text
        model, and per-paragraph picture payloads for the VL model.
        """
        return [group.to_dict(with_data=with_data) for group in self.groups]

    def paired(self, *, with_data: bool = True) -> List[Dict[str, Any]]:
        """Only the paragraphs that actually carry at least one image."""
        return [group.to_dict(with_data=with_data) for group in self.groups if group.images]

    def stats(self) -> Dict[str, Any]:
        by_mime: Dict[str, int] = {}
        total = 0
        for image in self.images:
            by_mime[image.mime_type] = by_mime.get(image.mime_type, 0) + 1
            total += image.size
        by_match: Dict[str, int] = {}
        for image in self.attached:
            how = str(image.meta.get("match") or "reference")
            by_match[how] = by_match.get(how, 0) + 1
        return {
            "name": self.name,
            "paragraphs": len(self.paragraphs),
            "images": len(self.images),
            "attached": len(self.attached),
            "unattached": len(self.unattached),
            "coverage": round(self.coverage, 4),
            "bytes": total,
            "by_mime": by_mime,
            "by_match": by_match,
            "external": sum(1 for i in self.images if i.meta.get("external")),
            "extraction": self.extraction.summary() if self.extraction else None,
        }

    def summary(self) -> str:
        info = self.stats()
        return (
            f"{info['paragraphs']} paragraphs, {info['images']} images "
            f"({info['attached']} attached, {info['unattached']} unattached, "
            f"{info['bytes'] / 1e6:.2f} MB)"
        )


def attach_images(
    paragraphs: Sequence[Any],
    assets: Iterable[ImageAsset],
    *,
    heading_fallback: bool = True,
    anchor_fallback: bool = True,
) -> MultimodalResult:
    """Line images up with the paragraphs that mention them.

    Three passes, strongest signal first:

    1. **reference** - the handlers write ``![name](./oss/file/{id})`` (or an
       ``<img src>``) into the paragraph text; matching on that id is exact.
    2. **heading** - a recovered image knows the heading it sat under in the
       source markup, so it goes to the paragraph with that title.
    3. **anchor** - failing that, the ~96 characters of visible text that
       preceded the tag are looked up in the paragraph bodies.

    A standalone picture file has no text to match against at all, so a fourth
    pass pins it to the first paragraph - for that input the image *is* the
    document.

    Passes 2-4 are heuristics and are individually switchable; whatever they
    cannot place lands in :attr:`MultimodalResult.unattached` rather than being
    silently dropped.  Each placed image records how it matched in
    ``meta["match"]``.  Paragraph text is never modified.
    """
    groups: List[MultimodalParagraph] = []
    for index, row in enumerate(paragraphs or []):
        if isinstance(row, dict):
            title = str(row.get("title") or "")
            content = row.get("content")
            content = content if isinstance(content, str) else str(content or "")
        else:
            title, content = "", str(row or "")
        groups.append(MultimodalParagraph(index=index, title=title, content=content))

    assets = list(assets or [])
    by_id: Dict[str, ImageAsset] = {}
    for asset in assets:
        by_id.setdefault(str(asset.id), asset)

    placed: set = set()

    # -- pass 1: exact ./oss/file/{id} references ------------------------- #
    for group in groups:
        local: set = set()
        for match in OSS_REF.finditer(group.content):
            key = match.group(1)
            asset = by_id.get(key)
            if asset is None or key in local:
                continue
            local.add(key)
            group.images.append(asset)
            placed.add(id(asset))
            asset.meta.setdefault("match", "reference")

    # -- pass 2: nearest heading ------------------------------------------ #
    if heading_fallback:
        by_title: Dict[str, int] = {}
        for group in groups:
            key = _normalise_title(group.title)
            if key and key not in by_title:
                by_title[key] = group.index
        for asset in assets:
            if id(asset) in placed:
                continue
            heading = _normalise_title(asset.meta.get("heading"))
            if len(heading) < 2:
                continue
            target = by_title.get(heading)
            if target is None:
                for key, index in by_title.items():
                    if heading in key or key in heading:
                        target = index
                        break
            if target is not None:
                groups[target].images.append(asset)
                placed.add(id(asset))
                asset.meta.setdefault("match", "heading")

    # -- pass 3: preceding-text anchor ------------------------------------ #
    if anchor_fallback:
        plains = [_plain(group.content) for group in groups]
        for asset in assets:
            if id(asset) in placed:
                continue
            anchor = _plain(str(asset.meta.get("anchor") or ""))
            if len(anchor) < 12:
                continue
            probes = [anchor]
            if len(anchor) > 60:
                probes.append(anchor[-60:])
            if len(anchor) > 32:
                probes.append(anchor[-32:])
            for probe in probes:
                probe = probe.strip()
                if len(probe) < 12:
                    continue
                for index, plain in enumerate(plains):
                    if probe in plain:
                        groups[index].images.append(asset)
                        placed.add(id(asset))
                        asset.meta.setdefault("match", "anchor")
                        break
                if id(asset) in placed:
                    break

    # -- pass 4: the file *is* the picture (standalone image input) -------- #
    if groups:
        for asset in assets:
            if id(asset) in placed or asset.meta.get("role") != "document":
                continue
            groups[0].images.append(asset)
            placed.add(id(asset))
            asset.meta.setdefault("match", "document")

    unattached = [asset for asset in assets if id(asset) not in placed]
    return MultimodalResult(
        paragraphs=list(paragraphs or []),
        images=assets,
        groups=groups,
        unattached=unattached,
    )

# --------------------------------------------------------------------------- #
# the one-call entry points
# --------------------------------------------------------------------------- #


def slice_multimodal(
    content: Optional[bytes] = None,
    name: Optional[str] = None,
    *,
    path: Optional[Any] = None,
    limit: Optional[int] = None,
    overlap: Optional[int] = None,
    overlap_ratio: Optional[float] = None,
    options: Optional[Any] = None,
    patterns: Optional[Sequence[Any]] = None,
    with_filter: bool = True,
    image_text_extractor: Optional[Any] = None,
    fallback_title: Optional[str] = None,
    progress_hook: Optional[Any] = None,
    collector: Optional[ImageCollector] = None,
    collect: bool = True,
    save_image: Optional[Any] = None,
    probe: Any = "auto",
    dedupe: bool = True,
    keep_content: bool = True,
    max_images: Optional[int] = DEFAULT_MAX_IMAGES,
    max_image_bytes: Optional[int] = DEFAULT_MAX_IMAGE_BYTES,
    include_vector: bool = False,
    include_external: bool = True,
    min_side: Optional[int] = None,
    heading_fallback: bool = True,
    anchor_fallback: bool = True,
) -> MultimodalResult:
    """Slice a document *and* hand back its pictures, joined to the paragraphs.

    ::

        from smart_slice import slice_multimodal

        result = slice_multimodal(path="deck.pptx", limit=1000)
        result.summary()      # '96 paragraphs, 14 images (11 attached, ...)'
        for row in result.paired():
            send_to_vl_model(row["title"], row["content"], row["images"])

    :param content:  file bytes (omit when ``path`` is given)
    :param name:     file name - decides the handler and the container probe
    :param path:     read the file from disk instead of passing bytes
    :param collector: use this :class:`ImageCollector` instead of building one -
                     how a batch shares a single sink across documents
                     (:func:`smart_slice.slice_many` with ``collect_images=True``).
                     ``dedupe`` / ``keep_content`` / ``max_images`` then belong to
                     the supplied instance and are ignored here
    :param save_image: your own sink.  It is chained *after* the collector, so
                     persisting images yourself does not cost you the multimodal
                     result, and any ``{new_id: kept_id}`` mapping it returns is
                     merged with the collector's before the references are rewritten
    :param collect:  install an :class:`ImageCollector` as ``save_image`` so the
                     handlers' own pictures are captured (default True)
    :param probe:    ``"auto"`` (default) runs :func:`extract_media` only when the
                     handlers produced no image - that recovers HTML/EPUB/MIME/
                     standalone-picture formats without re-reading a docx they
                     already covered.  ``True`` always probes, ``False`` never
    :param dedupe:   collapse content-identical images onto one id (default True)
    :param keep_content: keep the raw bytes on each asset (``False`` records
                     metadata only and frees the memory)
    :param min_side: drop decorative images below this many pixels on the short side
    :param include_vector: also return SVG/EMF/WMF (useless to a VL model without
                     a renderer, so off by default)
    :param include_external: record remote ``<img src>`` URLs without fetching them

    The remaining keyword arguments are the :func:`smart_slice.slice_bytes` ones
    (``limit``, ``overlap``, ``patterns``, ``with_filter``,
    ``image_text_extractor``, ``fallback_title``, ``progress_hook``).

    Paragraphs always come back flat (``[{title, content}]``) - the nested
    per-sheet / per-member structure ``normalize=False`` returns has no single
    place to hang a picture.  OCR still works exactly as before: pass
    ``image_text_extractor`` and the text lands inside the paragraph, while the
    bytes stay available on the asset for a VL model.
    """
    from smart_slice import slice_bytes

    if content is None:
        if path is None:
            raise TypeError("slice_multimodal() needs either content= or path=")
        with open(path, "rb") as handle:
            content = handle.read()
        if name is None:
            name = os.path.basename(str(path))
    if name is None:
        name = "document"
    content = bytes(content)

    if collector is None and collect:
        collector = ImageCollector(
            dedupe=dedupe, keep_content=keep_content, max_images=max_images,
        )
    # Per-document ownership.  A batch shares one collector across worker
    # threads, so "what did *this* document produce" has to be recorded by the
    # sink closure installed here; reading the shared list before and after the
    # parse would pick up whatever another thread added in between.
    owned: List[ImageAsset] = []
    owned_ids: set = set()
    owned_lock = threading.Lock()

    def _own(items: Sequence[ImageAsset]) -> None:
        with owned_lock:
            for item in items:
                if id(item) not in owned_ids:
                    owned_ids.add(id(item))
                    owned.append(item)

    def sink(image_list):
        mapping: Dict[str, str] = {}
        if collector is not None:
            produced, kept = collector.collect(image_list)
            mapping.update(produced)
            _own(kept)
        if save_image is not None:
            extra = save_image(image_list)
            if isinstance(extra, dict):
                mapping.update(extra)
        return mapping

    paragraphs = slice_bytes(
        content,
        name,
        limit=limit,
        overlap=overlap,
        overlap_ratio=overlap_ratio,
        options=options,
        patterns=patterns,
        with_filter=with_filter,
        normalize=True,
        save_image=sink if (collector is not None or save_image is not None) else None,
        image_text_extractor=image_text_extractor,
        fallback_title=fallback_title,
        progress_hook=progress_hook,
    )
    if not isinstance(paragraphs, list):
        paragraphs = list(paragraphs or [])

    # only this document's pictures may be joined to its paragraphs
    assets: List[ImageAsset] = list(owned)
    extraction: Optional[MediaExtraction] = None
    kind = container_kind(content, name)
    if probe is True or (probe == "auto" and not assets):
        extraction = scan_media(
            content, name,
            max_images=max_images, max_image_bytes=max_image_bytes,
            include_vector=include_vector, include_external=include_external,
            min_side=min_side,
        )
        held = {item.sha256 for item in assets if item.content}
        extra = [item for item in extraction.assets if not item.content or item.sha256 not in held]
        if collector is not None:
            _produced, kept = collector.collect(extra)
            _own(kept)
        else:
            _own(extra)
        assets = list(owned)

    result = attach_images(
        paragraphs, assets,
        heading_fallback=heading_fallback, anchor_fallback=anchor_fallback,
    )
    result.name = name
    result.extraction = extraction
    result.collector = collector
    if extraction is not None and kind == "unknown":
        _log.debug(f"no media container recognised for {name}")
    return result


def slice_path_multimodal(path: Any, **kwargs: Any) -> MultimodalResult:
    """``slice_multimodal`` for a file on disk (mirrors :func:`smart_slice.slice_path`)."""
    kwargs.pop("content", None)
    return slice_multimodal(path=path, **kwargs)