"""MsgSplitHandle（.msg）：extract-msg 解析 Outlook 邮件，主题 + 正文。

Phase 2-A 全格式适配（2026-09-17）：.msg 为 OLE 复合文档，extract-msg 为纯 Python
实现（olefile 底座）。正文取 msg.body 纯文本，缺失时回退 htmlBody 转 Markdown。
内嵌附件/内嵌图片本期不递归解析（遗留项，记录于执行记录）。
"""
# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("msg")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError

import io
import traceback
from typing import List

from bs4 import BeautifulSoup
from markdownify import markdownify

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers._utils import build_split_model

OLE_MAGIC = b"\xd0\xcf\x11\xe0"


class MsgSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        if not file.name.lower().endswith(".msg"):
            return False
        try:
            return get_buffer(file)[:4] == OLE_MAGIC
        except Exception:
            return False

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            buffer = get_buffer(file)
            subject, body = _parse_msg_bytes(buffer)
            content = f"# {subject}\n\n{body}" if body else f"# {subject}"
            split_model = build_split_model(pattern_list, with_filter, limit)
        except SliceError:
            raise
        except BaseException as e:
            _log.error(f"Error processing MSG file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(
                500,
                _("Failed to parse document file {name}: {reason}").format(name=file.name, reason=e),
            )
        return {'name': file.name, 'content': split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            subject, body = _parse_msg_bytes(file.read())
            return f"# {subject}\n\n{body}" if body else f"# {subject}"
        except BaseException as e:
            _log.error(f'Error getting msg content: {e}')
            return f'{e}'


def _parse_msg_bytes(buffer: bytes):
    import extract_msg
    msg = extract_msg.openMsg(io.BytesIO(buffer))
    try:
        subject = str(msg.subject or '').strip() or "(无主题)"
        body = (msg.body or '').strip()
        if not body and msg.htmlBody:
            soup = BeautifulSoup(msg.htmlBody, 'html.parser')
            for tag in soup(['script', 'style']):
                tag.decompose()
            body = markdownify(str(soup), heading_style='ATX').strip()
        return subject, body
    finally:
        msg.close()
