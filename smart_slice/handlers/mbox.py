# coding=utf-8
"""MboxSplitHandle（.mbox/.mbox.txt）：mbox 邮箱归档按邮件逐封抽取。

.mbox 是多封邮件拼接的文本归档（``From `` 分隔行）。既有 EmlSplitHandle 只处理
单封 RFC822 邮件，对 mbox 会把整档当一封信解析、只取到首个主题。本 handler 用
标准库 mailbox.mbox 逐封解析，每封产出 ``# 主题`` + 正文（复用 EmlSplitHandle
的 text/plain 优先、html 回退 markdown 的取正文逻辑），封与封之间空行分隔。
纯标准库 + 既有 markup 能力（html 正文走 markdownify，缺失时降级纯文本）。
"""
from smart_slice._logging import get_logger

_log = get_logger("mbox")

import email
import email.policy
import mailbox
import os
import tempfile
import traceback
from typing import List

from smart_slice._i18n import gettext as _
from smart_slice.exceptions import ResourceLimitError, SliceError
from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers._utils import build_split_model

MBOX_EXTENSIONS = (".mbox",)


def _pick_body(msg) -> str:
    """text/plain 优先；无纯文本时回退 text/html（能转 markdown 则转，否则剥标签）。

    与 EmlSplitHandle._pick_body 同语义，但 markup extra 缺失时降级为不转
    markdown 的纯文本，保证裸环境仍可用。
    """
    plain = None
    html = None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        try:
            payload = part.get_content()
        except Exception:  # noqa: BLE001 - 单个 part 解码失败跳过
            continue
        if not isinstance(payload, str):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = payload
        elif ctype == "text/html" and html is None:
            html = payload
    if plain and plain.strip():
        return plain.strip()
    if html and html.strip():
        try:
            from bs4 import BeautifulSoup
            from markdownify import markdownify
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            return markdownify(str(soup), heading_style="ATX").strip()
        except ImportError:
            import re
            return re.sub(r"<[^>]+>", "", html).strip()
    return ""


def _message_to_markdown(msg) -> str:
    subject = str(msg.get("Subject") or "").strip() or "(无主题)"
    body = _pick_body(msg)
    return f"# {subject}\n\n{body}" if body else f"# {subject}"


def mbox_to_markdown(buffer: bytes, limits) -> str:
    """落临时文件交给 mailbox.mbox（其接口基于路径），逐封抽取后拼接。"""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".mbox")
        with os.fdopen(fd, "wb") as fh:
            fh.write(buffer)
        # factory 用 policy=default：mailbox 默认 Compat32 的 Message 没有 get_content()，
        # _pick_body 会因 AttributeError 静默跳过每个 part，正文全丢（与 EmlSplitHandle
        # 的 email.message_from_bytes(..., policy=default) 对齐）
        box = mailbox.mbox(
            tmp,
            factory=lambda fh: email.message_from_binary_file(fh, policy=email.policy.default),
        )
        messages: List[str] = []
        count = 0
        for message in box:
            count += 1
            if count > limits.max_mime_parts:
                raise ResourceLimitError("mbox exceeds the message count limit")
            text = _message_to_markdown(message).strip()
            if text:
                messages.append(text)
        box.close()
        return "\n\n".join(messages)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:  # pragma: no cover - 清理失败不影响结果
                pass


class MboxSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        if not file.name.lower().endswith(MBOX_EXTENSIONS):
            return False
        # mbox 以 "From " 行起始；宽松嗅探避免误认普通 .txt
        try:
            head = get_buffer(file)[:1024]
        except Exception:  # noqa: BLE001
            return False
        return head.lstrip().startswith(b"From ") or b"\nFrom " in head

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            from smart_slice._validation import ParserLimits
            content = mbox_to_markdown(get_buffer(file), ParserLimits.load())
            split_model = build_split_model(pattern_list, with_filter, limit)
        except (SliceError, ResourceLimitError):
            raise
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error processing mbox {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(500, _("Failed to parse document file {name}: {reason}").format(
                name=file.name, reason=e))
        return {"name": file.name, "content": split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            from smart_slice._validation import ParserLimits
            return mbox_to_markdown(file.read(), ParserLimits.load())
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error getting mbox content: {e}")
            return f"{e}"
