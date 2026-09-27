"""RtfSplitHandle（.rtf）：striprtf 提取纯文本。

Phase 2-A 全格式适配（2026-09-17）：RTF 中文内容通常以 \\uN 转义承载，文件本体
编码影响小；striprtf 为纯 Python 实现。提取出的纯文本按缺省模式（标题 + 空行）
交给 SplitModel 切分。support() 额外校验 {\\rtf magic，非 RTF 内容的 .rtf 扩展名
不命中（由 TextSplitHandle 排除表兜底为 400，避免乱码入库）。
"""
# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("rtf")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError

from typing import List

try:
    from striprtf.striprtf import rtf_to_text
    RTF_AVAILABLE = True
except ImportError:  # optional extra: smart-slice[office]
    rtf_to_text = None
    RTF_AVAILABLE = False

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers._utils import build_split_model

RTF_MAGIC = b"{\\rtf"


class RtfSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        # striprtf 为可选 extra（smart-slice[office]）：缺失时不认领
        if not RTF_AVAILABLE:
            return False
        if not file.name.lower().endswith(".rtf"):
            return False
        try:
            return get_buffer(file).lstrip()[:5] == RTF_MAGIC
        except Exception:
            return False

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            buffer = get_buffer(file)
            # striprtf 接受 str/bytes；显式按 latin-1 解码保字节语义，\uN 转义由库内处理
            content = rtf_to_text(buffer.decode('latin-1', 'ignore'))
            if type(limit) is str:
                limit = int(limit)
            split_model = build_split_model(pattern_list, with_filter, limit)
        except SliceError:
            raise
        except BaseException as e:
            _log.error(f"Error processing RTF file {file.name}: {e}")
            raise SliceError(
                500,
                _("Failed to parse document file {name}: {reason}").format(name=file.name, reason=e),
            )
        return {'name': file.name, 'content': split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            buffer = file.read()
            return rtf_to_text(buffer.decode('latin-1', 'ignore'))
        except BaseException as e:
            _log.error(f'Error getting rtf content: {e}')
            return f'{e}'
