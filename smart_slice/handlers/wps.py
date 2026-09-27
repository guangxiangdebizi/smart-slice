"""WpsSplitHandle（.wps/.et）：金山 WPS 文件 sniff 转交处理。

可行性评估结论（2026-09-17 Phase 2-A，详见 执行记录.md）：
- WPS 2005+ 新格式（.wps 文字 / .et 表格）为 OOXML 兼容 zip 容器，sniff PK zip
  magic 后分别转交 DocSplitHandle（.wps）/ XlsxSplitHandle（.et）按完整语义解析；
- 老版本 WPS（DOS/Win9x 时代）私有二进制格式无纯 Python 解析方案：support() 对
  非 zip 容器返回 False（归入不支持清单，由公共层统一抛 400），并在
  TextSplitHandle 排除表加入 .wps/.et 防止二进制被硬解码乱码入库。
"""
# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("wps")


from typing import List

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers.doc import DocSplitHandle
from smart_slice.handlers.xlsx import XlsxSplitHandle

ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


class WpsSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        file_name: str = file.name.lower()
        if not (file_name.endswith(".wps") or file_name.endswith(".et")):
            return False
        # 仅接受 OOXML 兼容 zip 容器；老二进制格式不命中（公共层报 400 不支持）
        try:
            return get_buffer(file)[:4] in ZIP_MAGICS
        except Exception:
            return False

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        if file.name.lower().endswith(".et"):
            return XlsxSplitHandle().handle(file, pattern_list, with_filter, limit, get_buffer, save_image)
        return DocSplitHandle().handle(file, pattern_list, with_filter, limit, get_buffer, save_image)

    def get_content(self, file, save_image):
        if file.name.lower().endswith(".et"):
            return XlsxSplitHandle().get_content(file, save_image)
        return DocSplitHandle().get_content(file, save_image)
