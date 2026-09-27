# coding=utf-8
"""Format handlers.

Every handler implements :class:`~smart_slice.handlers.base.BaseSplitHandle`:

``support(file, get_buffer) -> bool``
    Claim the input by file name and, where cheap, by content sniffing.
``handle(file, pattern_list, with_filter, limit, get_buffer, save_image) -> SplitResult``
    Extract text, then slice it into paragraphs.
``get_content(file, save_image) -> str``
    Extraction only (no slicing) - used for document previews and bundling.

``SPLIT_HANDLERS`` is the dispatch order.  Order matters: the first handler whose
``support`` returns True wins, so specific formats must precede the generic text
fallback that accepts almost anything decodable.

Optional dependencies are imported lazily inside the modules that need them, or
guarded by ``try/except ImportError`` at module level - importing this package
never fails because one format's parser is missing.  Use
:func:`missing_dependencies` to see which extras would unlock more formats.
"""
from typing import Dict, List, Tuple

from .archive import SevenZipSplitHandle, TarSplitHandle
from .base import BaseSplitHandle
from .csv_handler import CsvSplitHandle
from .doc import DocSplitHandle
from .eml import EmlSplitHandle
from .epub import EpubSplitHandle
from .html import HTMLSplitHandle
from .image import HEIF_SUPPORTED, ImageSplitHandle, image_extensions
from .mhtml import MhtmlSplitHandle
from .mobi import MobiSplitHandle
from .msg import MsgSplitHandle
from .odf import OdfSplitHandle
from .pdf import PdfSplitHandle
from .ppt import PptSplitHandle
from .pptx import PptxSplitHandle
from .rtf import RtfSplitHandle
from .text import TEXT_EXTENSIONS, TextSplitHandle
from .wps import WpsSplitHandle
from .xls import XlsSplitHandle
from .xlsx import XlsxSplitHandle
from .xmind import XmindSplitHandle
from .zip_handler import FileBufferHandle, ZipSplitHandle

__all__ = [
    "BaseSplitHandle",
    "SPLIT_HANDLERS",
    "FileBufferHandle",
    "HANDLER_EXTENSIONS",
    "missing_dependencies",
    "HTMLSplitHandle",
    "MhtmlSplitHandle",
    "DocSplitHandle",
    "PdfSplitHandle",
    "XlsxSplitHandle",
    "XlsSplitHandle",
    "CsvSplitHandle",
    "ZipSplitHandle",
    "XmindSplitHandle",
    "PptxSplitHandle",
    "PptSplitHandle",
    "WpsSplitHandle",
    "RtfSplitHandle",
    "OdfSplitHandle",
    "EpubSplitHandle",
    "EmlSplitHandle",
    "MsgSplitHandle",
    "MobiSplitHandle",
    "TarSplitHandle",
    "SevenZipSplitHandle",
    "ImageSplitHandle",
    "TextSplitHandle",
    "TEXT_EXTENSIONS",
    "image_extensions",
]

#: Dispatch order == priority.  Keep ``TextSplitHandle`` last: it is the fallback.
SPLIT_HANDLERS: Tuple[BaseSplitHandle, ...] = (
    HTMLSplitHandle(),
    MhtmlSplitHandle(),
    DocSplitHandle(),
    PdfSplitHandle(),
    XlsxSplitHandle(),
    XlsSplitHandle(),
    CsvSplitHandle(),
    ZipSplitHandle(),
    XmindSplitHandle(),
    PptxSplitHandle(),
    PptSplitHandle(),
    WpsSplitHandle(),
    RtfSplitHandle(),
    OdfSplitHandle(),
    EpubSplitHandle(),
    EmlSplitHandle(),
    MsgSplitHandle(),
    MobiSplitHandle(),
    TarSplitHandle(),
    SevenZipSplitHandle(),
    ImageSplitHandle(),
    TextSplitHandle(),
)

#: Extension -> handler class, for introspection and docs only (dispatch itself
#: goes through ``support`` which also content-sniffs).
HANDLER_EXTENSIONS: Dict[str, Tuple[str, ...]] = {
    "HTMLSplitHandle": (".html", ".htm"),
    "MhtmlSplitHandle": (".mhtml", ".mht"),
    "DocSplitHandle": (".docx", ".docm"),
    "PdfSplitHandle": (".pdf",),
    "XlsxSplitHandle": (".xlsx", ".xlsm"),
    "XlsSplitHandle": (".xls",),
    "CsvSplitHandle": (".csv", ".tsv"),
    "ZipSplitHandle": (".zip",),
    "XmindSplitHandle": (".xmind",),
    "PptxSplitHandle": (".pptx", ".ppsx", ".potx", ".ppsm", ".potm"),
    "PptSplitHandle": (".ppt", ".dps"),
    "WpsSplitHandle": (".wps", ".et"),
    "RtfSplitHandle": (".rtf",),
    "OdfSplitHandle": (".odt", ".ods", ".odp", ".fodt", ".fods", ".fodp"),
    "EpubSplitHandle": (".epub",),
    "EmlSplitHandle": (".eml",),
    "MsgSplitHandle": (".msg",),
    "MobiSplitHandle": (".mobi", ".azw", ".azw3"),
    "TarSplitHandle": (".tar", ".tar.gz", ".tgz", ".tar.bz2"),
    "SevenZipSplitHandle": (".7z",),
    "ImageSplitHandle": image_extensions + ((".heic", ".heif") if HEIF_SUPPORTED else ()),
    "TextSplitHandle": TEXT_EXTENSIONS,
}

#: import name -> pip extra that provides it
_OPTIONAL_IMPORTS: Tuple[Tuple[str, str, str], ...] = (
    ("markdownify", "all", "markdownify>=0.13"),
    ("bs4", "all", "beautifulsoup4>=4.12"),
    ("pptx", "office", "python-pptx>=1.0"),
    ("olefile", "office", "olefile>=0.46"),
    ("striprtf", "office", "striprtf>=0.0.26"),
    ("extract_msg", "mail", "extract-msg>=0.48"),
    ("py7zr", "archive", "py7zr>=0.20"),
    ("mobi", "ebook", "mobi>=0.3.3"),
    ("pillow_heif", "image", "pillow-heif>=0.13"),
    ("jieba", "keywords", "jieba>=0.42"),
    ("rapidocr_onnxruntime", "ocr", "rapidocr-onnxruntime>=1.3"),
    ("uuid_utils", "all", "uuid-utils>=0.6"),
)


def missing_dependencies() -> List[Dict[str, str]]:
    """Report optional dependencies that are not importable right now.

    Useful for a startup banner ("install smart-slice[ocr] to enable image text
    extraction"); the core API works regardless.
    """
    import importlib.util

    missing = []
    for module_name, extra, requirement in _OPTIONAL_IMPORTS:
        try:
            available = importlib.util.find_spec(module_name) is not None
        except (ImportError, ValueError):  # pragma: no cover - broken install
            available = False
        if not available:
            missing.append({"module": module_name, "extra": extra, "requirement": requirement})
    return missing