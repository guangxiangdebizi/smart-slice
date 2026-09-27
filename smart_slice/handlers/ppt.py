"""PptSplitHandle（.ppt 老 OLE 二进制格式）：纯 Python 提取文本记录。

可行性评估结论（2026-09-17 Phase 2-A，详见 执行记录.md）：
1. 部分 .ppt 实为改名 docx/pptx（zip 容器）：先 sniff PK zip magic，命中则转交
   PptxSplitHandle / DocSplitHandle 按完整 OOXML 语义解析；
2. 真 OLE 二进制 .ppt：olefile（纯 Python）读 "PowerPoint Document" 流，递归遍历
   记录树提取 TextCharsAtom(0x0FA0, UTF-16LE) / TextBytesAtom(0x0FA8, ANSI) /
   CString(0x0FBA, UTF-16LE) 文本原子。文本顺序近似幻灯片顺序，但拿不到页边界，
   退化为单语义单元整篇文本（无逐页标题），属可接受的降级提取。

两条路都不可行的输入（损坏文件 / 缺流）抛清晰 SliceError，由链路容错。
"""
# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("ppt")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError

import io
import struct
import traceback
from typing import List

import olefile

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers.doc import DocSplitHandle
from smart_slice.handlers.pptx import PptxSplitHandle
from smart_slice.handlers._utils import build_split_model

ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

# 文本原子记录类型
_TEXT_CHARS_ATOM = 0x0FA0   # UTF-16LE
_TEXT_BYTES_ATOM = 0x0FA8   # ANSI（cp1252）
_CSTRING_ATOM = 0x0FBA      # UTF-16LE


def _walk_text_atoms(data: bytes, offset: int, end: int, out: List[str]):
    """递归遍历 PPT 记录树，收集文本原子。container 记录 ver nibble = 0xF。"""
    while offset + 8 <= end:
        ver_inst, rec_type, rec_len = struct.unpack_from('<HHI', data, offset)
        ver = ver_inst & 0x0F
        body_start = offset + 8
        body_end = min(body_start + rec_len, end)
        if ver == 0x0F and body_end >= body_start:
            _walk_text_atoms(data, body_start, body_end, out)
        elif rec_type == _TEXT_CHARS_ATOM or rec_type == _CSTRING_ATOM:
            out.append(data[body_start:body_end].decode('utf-16-le', 'ignore'))
        elif rec_type == _TEXT_BYTES_ATOM:
            out.append(data[body_start:body_end].decode('cp1252', 'ignore'))
        if body_end <= offset:  # 防御：异常长度导致死循环
            break
        offset = body_end


def _extract_ppt_text(buffer: bytes) -> str:
    """从 OLE 二进制 .ppt 提取文本原子并拼接；失败抛异常由 handle 统一包装。"""
    if not olefile.isOleFile(io.BytesIO(buffer)):
        raise ValueError("not an OLE compound file")
    ole = olefile.OleFileIO(io.BytesIO(buffer))
    try:
        if not ole.exists('PowerPoint Document'):
            raise ValueError("PowerPoint Document stream not found")
        data = ole.openstream('PowerPoint Document').read()
    finally:
        ole.close()
    atoms: List[str] = []
    _walk_text_atoms(data, 0, len(data), atoms)
    # 去重连续重复原子（同一文本可能以 TextChars + TextBytes 双记录出现）与空片段
    pieces = [a.strip() for a in atoms if a and a.strip()]
    deduped = []
    for piece in pieces:
        if not deduped or deduped[-1] != piece:
            deduped.append(piece)
    return "\n\n".join(deduped)


class PptSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        return file.name.lower().endswith(".ppt")

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            buffer = get_buffer(file)
            if buffer[:4] in ZIP_MAGICS:
                # 改名 OOXML：优先按 pptx 解析，失败回退 docx
                try:
                    return PptxSplitHandle().handle(file, pattern_list, with_filter, limit, get_buffer, save_image)
                except Exception:
                    return DocSplitHandle().handle(file, pattern_list, with_filter, limit, get_buffer, save_image)
            content = _extract_ppt_text(buffer)
            if not content.strip():
                raise ValueError("no text atom found in PowerPoint Document stream")
            split_model = build_split_model(pattern_list, with_filter, limit)
        except SliceError:
            raise
        except BaseException as e:
            _log.error(f"Error processing PPT file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(
                500,
                _("Failed to parse document file {name}: {reason}").format(name=file.name, reason=e),
            )
        return {'name': file.name, 'content': split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            buffer = file.read()
            if buffer[:4] in ZIP_MAGICS:
                try:
                    return PptxSplitHandle().get_content(file, save_image)
                except Exception:
                    return DocSplitHandle().get_content(file, save_image)
            return _extract_ppt_text(buffer)
        except BaseException as e:
            _log.error(f'Error getting ppt content: {e}', exc_info=True)
            return f'{e}'
