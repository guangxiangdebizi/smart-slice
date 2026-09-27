# coding=utf-8
"""Fb2SplitHandle（.fb2/.fb2.zip）：FictionBook 电子书 XML 抽取。

FictionBook 是单一 XML 文档（或 zip 容器内一个 .fb2）。既有 OdfSplitHandle 的
命名空间与本格式无关，故单独实现：取 <body> 内 <section>/<title>/<p> 文本，
<section><title> 映射为 Markdown 标题（按嵌套深度，封顶六级），<p> 为段落，
交给 SplitModel（标题树 + 空行 + limit）。经 _validation.parse_xml 限深限量。
纯标准库（zipfile + xml.etree），裸环境可用。
"""
from smart_slice._logging import get_logger

_log = get_logger("fb2")

import traceback
from typing import List

from smart_slice._i18n import gettext as _
from smart_slice._validation import ParserLimits, document_package, parse_xml, read_package_xml
from smart_slice.exceptions import ResourceLimitError, SliceError
from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers._utils import build_split_model

FB2_EXTENSIONS = (".fb2", ".fb2.zip")
ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text_of(element) -> str:
    return " ".join(t for t in element.itertext() if t and t.strip()).strip()


def _emit(element, out: List[str], depth: int, limits):
    tag = _local(element.tag)
    if tag == "section":
        for child in element:
            _emit(child, out, depth + 1, limits)
        return
    if tag == "title":
        text = _text_of(element)
        if text:
            level = max(1, min(6, depth))
            out.append("#" * level + " " + text)
        return
    if tag == "p":
        text = _text_of(element)
        if text:
            out.append(text)
        return
    if tag in ("epigraph", "cite", "poem", "stanza", "subtitle", "text-author"):
        for child in element:
            _emit(child, out, depth, limits)
        return
    # 其余容器递归（body/section 之外的结构）
    for child in element:
        _emit(child, out, depth, limits)


def _fb2_xml_to_markdown(root, limits) -> str:
    out: List[str] = []
    for child in root:
        if _local(child.tag) == "body":
            _emit(child, out, 0, limits)
            break
    text = "\n\n".join(out)
    if len(text.encode("utf-8")) > limits.max_odf_text_bytes:
        raise ResourceLimitError("FictionBook exceeds the extracted text byte limit")
    return text


def _extract_root(buffer: bytes, name: str, limits):
    """`.fb2.zip`（或 zip 容器内的 .fb2）取内部 XML；纯 .fb2 直接解析。"""
    if buffer[:4] in ZIP_MAGICS or name.lower().endswith(".zip"):
        with document_package(buffer, limits) as archive:
            member = next((n for n in archive.namelist() if n.lower().endswith(".fb2")), None)
            if member is None:
                raise SliceError(400, "No .fb2 member found in the FictionBook zip container")
            return read_package_xml(archive, member, limits)
    return parse_xml(buffer, limits)


class Fb2SplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        return file.name.lower().endswith(FB2_EXTENSIONS)

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            limits = ParserLimits.load()
            root = _extract_root(get_buffer(file), file.name, limits)
            content = _fb2_xml_to_markdown(root, limits)
            split_model = build_split_model(pattern_list, with_filter, limit)
        except (SliceError, ResourceLimitError):
            raise
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error processing FB2 file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(500, _("Failed to parse document file {name}: {reason}").format(
                name=file.name, reason=e))
        return {"name": file.name, "content": split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            limits = ParserLimits.load()
            return _fb2_xml_to_markdown(_extract_root(file.read(), file.name, limits), limits)
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error getting fb2 content: {e}")
            return f"{e}"
