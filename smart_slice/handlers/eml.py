"""EmlSplitHandle（.eml）：标准库 email 解析，主题 + 正文。

Phase 2-A 全格式适配（2026-09-17）：邮件头取主题（缺省"(无主题)"），正文取
text/plain 优先、无纯文本部分时 text/html 经 BeautifulSoup + markdownify 转
Markdown。内嵌附件本期不递归解析（遗留项，记录于执行记录）。
"""
# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("eml")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError

import email
import email.policy
import io
import traceback
from typing import List

from bs4 import BeautifulSoup
from markdownify import markdownify

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers._utils import build_split_model


def _html_to_text(html_bytes: bytes) -> str:
    soup = BeautifulSoup(html_bytes, 'html.parser')
    for tag in soup(['script', 'style']):
        tag.decompose()
    return markdownify(str(soup), heading_style='ATX').strip()


def _pick_body(msg) -> str:
    """text/plain 优先；无纯文本时回退 text/html。多 part 只取第一个命中。"""
    plain = None
    html = None
    for part in msg.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        content_type = part.get_content_type()
        try:
            payload = part.get_content()
        except Exception:
            continue
        if not isinstance(payload, str):
            continue
        if content_type == 'text/plain' and plain is None:
            plain = payload
        elif content_type == 'text/html' and html is None:
            html = payload
    if plain and plain.strip():
        return plain.strip()
    if html and html.strip():
        return _html_to_text(html.encode('utf-8', 'ignore'))
    return ''


class EmlSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        return file.name.lower().endswith(".eml")

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            buffer = get_buffer(file)
            msg = email.message_from_bytes(buffer, policy=email.policy.default)
            subject = str(msg.get('Subject') or '').strip() or "(无主题)"
            body = _pick_body(msg)
            content = f"# {subject}\n\n{body}" if body else f"# {subject}"
            split_model = build_split_model(pattern_list, with_filter, limit)
        except SliceError:
            raise
        except BaseException as e:
            _log.error(f"Error processing EML file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(
                500,
                _("Failed to parse document file {name}: {reason}").format(name=file.name, reason=e),
            )
        return {'name': file.name, 'content': split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            msg = email.message_from_bytes(file.read(), policy=email.policy.default)
            subject = str(msg.get('Subject') or '').strip() or "(无主题)"
            body = _pick_body(msg)
            return f"# {subject}\n\n{body}" if body else f"# {subject}"
        except BaseException as e:
            _log.error(f'Error getting eml content: {e}')
            return f'{e}'
