# coding=utf-8
"""SvgSplitHandle（.svg）：矢量图按 XML 文本抽取。

SVG 是 XML 文本，但既有 TextSplitHandle 的排除表（end）把 .svg 当二进制拦掉，
导致矢量图里的可读文字（<text>/<tspan>）无法进入检索。本 handler 用标准库
xml.etree 解析（经 _validation.parse_xml 限深限量），剥除 <script>/<style>/<defs>
等非可视节点后收集文本，交给 SplitModel。纯标准库实现，裸环境（无可选 extra）
同样可用。
"""
from smart_slice._logging import get_logger

_log = get_logger("svg")

import gzip
import io
import traceback
import zlib
from typing import List

from smart_slice._i18n import gettext as _
from smart_slice._validation import ParserLimits, parse_xml
from smart_slice.exceptions import ResourceLimitError, SliceError
from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers._utils import build_split_model

SVG_EXTENSIONS = (".svg", ".svgz")
#: 非可视 / 噪声节点（含命名空间前缀形式），文本抽取时整体跳过
_SKIP_TAGS = {"script", "style", "defs", "metadata", "title", "desc"}


def _decompress(buffer: bytes, name: str) -> bytes:
    """`.svgz` 是 gzip 压缩的 SVG：解压后按普通 SVG 解析。

    上限沿用 ParserLimits.max_input_bytes，避免解压炸弹（gzip 高压缩比）撑爆内存。
    """
    if not str(name).lower().endswith(".svgz") and buffer[:2] != b"\x1f\x8b":
        return buffer
    limits = ParserLimits.load()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(buffer)) as gz:
            return gz.read(limits.max_input_bytes + 1)
    except (OSError, EOFError, zlib.error) as error:
        raise SliceError(400, "Invalid or damaged gzipped SVG") from error


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _svg_to_text(root) -> str:
    """遍历 SVG，按文档顺序收集可视文本；<text>/<tspan> 之间补空格，块级元素换行。"""
    parts: List[str] = []

    def walk(element):
        tag = _local(element.tag)
        if tag in _SKIP_TAGS:
            return
        text = (element.text or "").strip()
        if text:
            parts.append(text)
        for child in element:
            walk(child)
            tail = (child.tail or "").strip()
            if tail:
                parts.append(tail)

    walk(root)
    # SVG 文本无天然段落结构：同一 <text> 内 token 以空格相连，整体作为单段交给 limit 切分
    return " ".join(parts)


class SvgSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        if not file.name.lower().endswith(SVG_EXTENSIONS):
            return False
        try:
            head = _decompress(get_buffer(file), file.name)[:512]
        except Exception:  # noqa: BLE001 - 嗅探失败不阻断分发
            return False
        return head.lstrip()[:5] == b"<?xml" or b"<svg" in head

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            root = parse_xml(_decompress(get_buffer(file), file.name), ParserLimits.load())
            content = _svg_to_text(root)
            split_model = build_split_model(pattern_list, with_filter, limit)
        except (SliceError, ResourceLimitError):
            raise
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error processing SVG file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(500, _("Failed to parse document file {name}: {reason}").format(
                name=file.name, reason=e))
        return {"name": file.name, "content": split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            return _svg_to_text(parse_xml(_decompress(file.read(), file.name), ParserLimits.load()))
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error getting svg content: {e}")
            return f"{e}"
