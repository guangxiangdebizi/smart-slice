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
from .fb2 import Fb2SplitHandle
from .ipynb import IpynbSplitHandle
from .mbox import MboxSplitHandle
from .raster_image import EXTRA_IMAGE_EXTENSIONS, ExtendedImageSplitHandle
from .subtitle import SUBTITLE_EXTENSIONS, SubtitleSplitHandle
from .svg import SVG_EXTENSIONS, SvgSplitHandle
from .vcalendar import ICS_EXTENSIONS, VCF_EXTENSIONS, VcalendarSplitHandle, VcardSplitHandle
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
    "SvgSplitHandle",
    "IpynbSplitHandle",
    "SubtitleSplitHandle",
    "Fb2SplitHandle",
    "MboxSplitHandle",
    "VcalendarSplitHandle",
    "VcardSplitHandle",
    "ExtendedImageSplitHandle",
    "SVG_EXTENSIONS",
    "SUBTITLE_EXTENSIONS",
    "EXTRA_IMAGE_EXTENSIONS",
    "ICS_EXTENSIONS",
    "VCF_EXTENSIONS",
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
    # .fb2.zip 必须在通用 zip 之前认领，否则被 ZipSplitHandle 当普通压缩包解
    Fb2SplitHandle(),
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
    # 结构化文本格式（标准库解析）：必须在 TextSplitHandle 兜底之前认领，
    # 否则 .svg/.ipynb/.ics/.vcf 等会被排除表拦成 400 或按纯文本硬解
    SvgSplitHandle(),
    IpynbSplitHandle(),
    SubtitleSplitHandle(),
    MboxSplitHandle(),
    VcalendarSplitHandle(),
    VcardSplitHandle(),
    ImageSplitHandle(),
    # 扩展位图（.ico/.tga/.pcx/...）：继承 ImageSplitHandle，认领父类未覆盖的容器
    ExtendedImageSplitHandle(),
    TextSplitHandle(),
)

#: Extension -> handler class, for introspection and docs only (dispatch itself
#: goes through ``support`` which also content-sniffs).
HANDLER_EXTENSIONS: Dict[str, Tuple[str, ...]] = {
    "HTMLSplitHandle": (".html", ".htm", ".xhtml", ".shtml"),
    "MhtmlSplitHandle": (".mhtml", ".mht"),
    "DocSplitHandle": (".docx", ".docm", ".doc", ".dotx", ".dotm"),
    "PdfSplitHandle": (".pdf",),
    "XlsxSplitHandle": (".xlsx", ".xlsm", ".xltx", ".xltm"),
    "XlsSplitHandle": (".xls",),
    "CsvSplitHandle": (".csv", ".tsv", ".tab"),
    "ZipSplitHandle": (".zip",),
    "XmindSplitHandle": (".xmind",),
    "PptxSplitHandle": (".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"),
    "PptSplitHandle": (".ppt", ".dps"),
    "WpsSplitHandle": (".wps", ".et"),
    "RtfSplitHandle": (".rtf",),
    "OdfSplitHandle": (".odt", ".ods", ".odp", ".fodt", ".fods", ".fodp"),
    "EpubSplitHandle": (".epub",),
    "EmlSplitHandle": (".eml",),
    "MsgSplitHandle": (".msg",),
    "MobiSplitHandle": (".mobi", ".azw", ".azw1", ".azw3", ".azw4", ".prc"),
    "TarSplitHandle": (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".txz"),
    "SevenZipSplitHandle": (".7z",),
    "ImageSplitHandle": image_extensions + ((".heic", ".heif") if HEIF_SUPPORTED else ()),
    "Fb2SplitHandle": (".fb2", ".fb2.zip"),
    "SvgSplitHandle": SVG_EXTENSIONS,
    "IpynbSplitHandle": (".ipynb",),
    "SubtitleSplitHandle": SUBTITLE_EXTENSIONS,
    "MboxSplitHandle": (".mbox",),
    "VcalendarSplitHandle": ICS_EXTENSIONS,
    "VcardSplitHandle": VCF_EXTENSIONS,
    "ExtendedImageSplitHandle": EXTRA_IMAGE_EXTENSIONS,
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