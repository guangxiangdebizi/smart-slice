"""MobiSplitHandle（.mobi/.azw/.azw3）：mobi 库（内置 KindleUnpack，纯 Python）提取。

Phase 2-A 全格式适配（2026-09-17）：mobi 0.4.1 为纯 Python wheel（依赖 loguru、
standard-imghdr，无系统级二进制），可解 MOBI6/KF8 及 PalmDOC。提取产物为 HTML，
经 BeautifulSoup + markdownify 转 Markdown 后交给 SplitModel。

mobi.extract 只接受文件路径：落临时文件解包，读取产物后清理。解包产物为
HTML 时段落组织随章节标题（HTML heading）走 SplitModel 标题树。
"""
# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("mobi")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError

import os
import shutil
import tempfile
import traceback
from typing import List

try:
    from bs4 import BeautifulSoup
    from markdownify import markdownify
    MARKUP_AVAILABLE = True
except ImportError:  # optional extra: smart-slice[markup]
    BeautifulSoup = markdownify = None
    MARKUP_AVAILABLE = False

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers._utils import build_split_model
from smart_slice import _optional


class MobiSplitHandle(BaseSplitHandle):
    MOBI_EXTENSIONS = (".mobi", ".azw", ".azw1", ".azw3", ".azw4", ".prc")

    def support(self, file, get_buffer):
        # mobi 库 + markdownify/bs4 均为可选 extra（ebook / markup）：缺失时不认领
        if not (MARKUP_AVAILABLE and _optional.installed("mobi")):
            return False
        return file.name.lower().endswith(self.MOBI_EXTENSIONS)

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            buffer = get_buffer(file)
            content = self._extract(buffer)
            split_model = build_split_model(pattern_list, with_filter, limit)
        except SliceError:
            raise
        except BaseException as e:
            _log.error(f"Error processing MOBI file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(
                500,
                _("Failed to parse document file {name}: {reason}").format(name=file.name, reason=e),
            )
        return {'name': file.name, 'content': split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            return self._extract(file.read())
        except BaseException as e:
            _log.error(f'Error getting mobi content: {e}')
            return f'{e}'

    @staticmethod
    def _extract(buffer: bytes) -> str:
        import mobi
        tempdir = None
        tmp_path = None
        fd, tmp_path = tempfile.mkstemp(suffix=".mobi")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(buffer)
            tempdir, filepath = mobi.extract(tmp_path)
            with open(filepath, 'rb') as f:
                html_bytes = f.read()
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
            if tempdir:
                shutil.rmtree(tempdir, ignore_errors=True)
        soup = BeautifulSoup(html_bytes, 'html.parser')
        for tag in soup(['script', 'style']):
            tag.decompose()
        body = soup.body if soup.body else soup
        return markdownify(str(body), heading_style='ATX').strip()
