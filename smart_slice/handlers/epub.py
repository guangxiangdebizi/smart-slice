"""EpubSplitHandle（.epub）：zipfile 解包 + HTML 章节解析。

Phase 2-A 全格式适配（2026-09-17）：EPUB 为 zip 容器；解析 META-INF/container.xml
定位 OPF 包描述，按 spine 顺序提取各章节 xhtml/html，章节 HTML 经 BeautifulSoup +
markdownify 转 Markdown（与 HTMLSplitHandle 同款转换语义），按章节顺序空行拼接后
交给 SplitModel（标题树 + limit 二次切分）。非 spine 内的资源（图片/样式）本期不
提取落库（遗留项，记录于执行记录）。
"""
# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("epub")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError

import io
import traceback
import zipfile
from typing import List
from xml.etree import ElementTree

try:
    from bs4 import BeautifulSoup
    from markdownify import markdownify
    MARKUP_AVAILABLE = True
except ImportError:  # optional extra: smart-slice[markup]
    BeautifulSoup = markdownify = None
    MARKUP_AVAILABLE = False

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers._utils import build_split_model

ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
# 缺陷 #34 修复（2026-09-18）：container.xml 命名空间不再做精确匹配——标准 OCF
# 命名空间为 urn:oasis:names:tc:opendocument:xmlns:container（无 ":1.0" 后缀），
# 部分制作用 ":1.0" 变体或无命名空间；与 _spine_items 一致改按 local-name 匹配。
_OPF_MEDIA_TYPES = ('application/xhtml+xml', 'text/html')


def _container_rootfile(zip_ref: zipfile.ZipFile) -> str:
    container = zip_ref.read('META-INF/container.xml')
    root = ElementTree.fromstring(container)
    for rootfile in root.iter():
        if rootfile.tag.rsplit('}', 1)[-1] == 'rootfile':
            path = rootfile.get('full-path')
            if path:
                return path
    raise ValueError('rootfile element not found in container.xml')


def _spine_items(zip_ref: zipfile.ZipFile, opf_path: str) -> List[str]:
    """按 spine 顺序返回章节文件在 zip 内的路径列表。"""
    opf_root = ElementTree.fromstring(zip_ref.read(opf_path))
    base_dir = opf_path.rsplit('/', 1)[0] + '/' if '/' in opf_path else ''
    manifest = {}
    for item in opf_root.iter():
        if item.tag.rsplit('}', 1)[-1] == 'item':
            item_id = item.get('id')
            if item_id and item.get('media-type') in _OPF_MEDIA_TYPES:
                manifest[item_id] = item.get('href')
    items = []
    for itemref in opf_root.iter():
        if itemref.tag.rsplit('}', 1)[-1] == 'itemref':
            item_id = itemref.get('idref')
            href = manifest.get(item_id)
            if href:
                from urllib.parse import unquote, urljoin
                zip_path = urljoin(base_dir, unquote(href))
                if zip_path in zip_ref.namelist():
                    items.append(zip_path)
    if not items:
        raise ValueError('no spine html/xhtml items found in OPF')
    return items


def _html_to_markdown(html_bytes: bytes) -> str:
    soup = BeautifulSoup(html_bytes, 'html.parser')
    for a in soup.find_all('a'):
        if a.get('href', '').startswith('#'):
            a.unwrap()
    for tag in soup(['script', 'style']):
        tag.decompose()
    return markdownify(str(soup.body if soup.body else soup), heading_style='ATX').strip()


class EpubSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        # markdownify/bs4 为可选 extra（smart-slice[markup]）：缺失时不认领
        if not MARKUP_AVAILABLE:
            return False
        if not file.name.lower().endswith('.epub'):
            return False
        try:
            return get_buffer(file)[:4] in ZIP_MAGICS
        except Exception:
            return False

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            buffer = get_buffer(file)
            chapters = []
            with zipfile.ZipFile(io.BytesIO(buffer), 'r') as zip_ref:
                opf_path = _container_rootfile(zip_ref)
                for zip_path in _spine_items(zip_ref, opf_path):
                    markdown = _html_to_markdown(zip_ref.read(zip_path))
                    if markdown:
                        chapters.append(markdown)
            content = '\n\n'.join(chapters)
            split_model = build_split_model(pattern_list, with_filter, limit)
        except SliceError:
            raise
        except BaseException as e:
            _log.error(f"Error processing EPUB file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(
                500,
                _("Failed to parse document file {name}: {reason}").format(name=file.name, reason=e),
            )
        return {'name': file.name, 'content': split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            buffer = file.read()
            chapters = []
            with zipfile.ZipFile(io.BytesIO(buffer), 'r') as zip_ref:
                opf_path = _container_rootfile(zip_ref)
                for zip_path in _spine_items(zip_ref, opf_path):
                    markdown = _html_to_markdown(zip_ref.read(zip_path))
                    if markdown:
                        chapters.append(markdown)
            return '\n\n'.join(chapters)
        except BaseException as e:
            _log.error(f'Error getting epub content: {e}')
            return f'{e}'
