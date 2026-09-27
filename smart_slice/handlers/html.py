# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("html")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError, ResourceLimitError

import re
import traceback
from typing import List

from charset_normalizer import detect
try:
    from bs4 import BeautifulSoup
    from markdownify import markdownify
    MARKUP_AVAILABLE = True
except ImportError:  # optional extra: smart-slice[markup]
    BeautifulSoup = markdownify = None
    MARKUP_AVAILABLE = False

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice._validation import decode_text, validate_input
from smart_slice.chunker import SplitModel

#: HTML 族扩展名（.svg 单独走 SvgSplitHandle，不在此清单）
HTML_EXTENSIONS = ('.html', '.htm', '.xhtml', '.shtml')

default_pattern_list = [re.compile('(?<=^)# .*|(?<=\\n)# .*'),
                        re.compile('(?<=\\n)(?<!#)## (?!#).*|(?<=^)(?<!#)## (?!#).*'),
                        re.compile("(?<=\\n)(?<!#)### (?!#).*|(?<=^)(?<!#)### (?!#).*"),
                        re.compile("(?<=\\n)(?<!#)#### (?!#).*|(?<=^)(?<!#)#### (?!#).*"),
                        re.compile("(?<=\\n)(?<!#)##### (?!#).*|(?<=^)(?<!#)##### (?!#).*"),
                        re.compile("(?<=\\n)(?<!#)###### (?!#).*|(?<=^)(?<!#)###### (?!#).*")]


def get_encoding(buffer):
    validate_input(buffer)
    beautiful_soup = BeautifulSoup(buffer, "html.parser")
    meta_list = beautiful_soup.find_all('meta')
    charset_list = [meta.attrs.get('charset') for meta in meta_list if
                    meta.attrs is not None and 'charset' in meta.attrs]
    if len(charset_list) > 0:
        charset = charset_list[0]
        return charset
    return detect(buffer)['encoding']


def html_to_markdown(buffer):
    encoding = get_encoding(buffer)
    content = decode_text(buffer, encoding=encoding)
    content = HTMLSplitHandle()._remove_anchor_links(content)
    return markdownify(content, heading_style='ATX')


class HTMLSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        # markdownify/bs4 为可选 extra（smart-slice[markup]）：缺失时不认领
        if not MARKUP_AVAILABLE:
            return False
        file_name: str = file.name.lower()
        if file_name.endswith(HTML_EXTENSIONS):
            return True
        return False

    def _remove_anchor_links(self, html: str) -> str:
        soup = BeautifulSoup(html, 'html.parser')
        for element in soup(['script', 'style', 'iframe', 'object', 'embed']):
            element.decompose()
        for a in soup.find_all('a', href=re.compile('^#')):
            a.unwrap()
        return str(soup)

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        buffer = get_buffer(file)
        if type(limit) is str:
            limit = int(limit)
        if type(with_filter) is str:
            with_filter = with_filter.lower() == 'true'
        if pattern_list is not None and len(pattern_list) > 0:
            split_model = SplitModel(pattern_list, with_filter, limit)
        else:
            split_model = SplitModel(default_pattern_list, with_filter=with_filter, limit=limit)
        try:
            content = html_to_markdown(buffer)
        except (SliceError, ResourceLimitError):
            raise
        except Exception as e:
            _log.error(f"Error processing HTML file {file.name}: {e}, {traceback.format_exc()}")
            # 解析失败显式报错，避免静默返回空段落
            raise SliceError(
                500,
                _("Failed to parse HTML file {name}: {reason}").format(name=file.name, reason=e),
            )
        return {
            'name': file.name,
            'content': split_model.parse(content)
        }

    def get_content(self, file, save_image):
        buffer = file.read()

        try:
            return html_to_markdown(buffer)
        except (SliceError, ResourceLimitError):
            raise
        except Exception as e:
            _log.error(f'Exception: {e}', exc_info=True)
            raise SliceError(400, 'Invalid or damaged HTML document') from e
