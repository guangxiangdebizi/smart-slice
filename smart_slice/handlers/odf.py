"""OdfSplitHandle（.odt/.ods/.odp）：zipfile + xml.etree 解析 content.xml。

Phase 2-A 全格式适配（2026-09-17）：ODF 为 zip 容器 + XML，标准库即可解析，
无需 odfpy。转换规则：
- text:h → Markdown 标题（text:h 的 outline-level 映射 # 数量）；
- text:p → 段落文本；
- table:table → Markdown 管道表格（table:table-row / table:table-cell，
  covered-table-cell 补空列）。
三类文档（文字/表格/演示）统一按上述规则抽取正文；段落间空行拼接后交给
SplitModel（标题树 + 空行分段 + limit 二次切分）。
"""
# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("odf")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError, ResourceLimitError

import os
import traceback
from typing import List


from smart_slice.handlers.base import BaseSplitHandle
from smart_slice._validation import (
    ParserLimits, document_package, parse_xml, read_package_xml,
)
from smart_slice.handlers._utils import build_split_model, md_table

ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
OFFICE_NAMESPACE = '{urn:oasis:names:tc:opendocument:xmlns:office:1.0}'
TABLE_NAMESPACE = '{urn:oasis:names:tc:opendocument:xmlns:table:1.0}'
MANIFEST_NAMESPACE = '{urn:oasis:names:tc:opendocument:xmlns:manifest:1.0}'
ODF_KINDS = {
    '.odt': 'text', '.ods': 'spreadsheet', '.odp': 'presentation',
    '.ott': 'text', '.ots': 'spreadsheet', '.otp': 'presentation',
    '.fodt': 'text', '.fods': 'spreadsheet', '.fodp': 'presentation',
}
TEMPLATE_EXTENSIONS = ('.ott', '.ots', '.otp')
FLAT_EXTENSIONS = ('.fodt', '.fods', '.fodp')
ODF_EXTENSIONS = tuple(ODF_KINDS)


def _local_tag(tag: str) -> str:
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _cell_text(cell) -> str:
    text = "\n".join(value.strip() for value in cell.itertext() if value and value.strip())
    if text:
        return text
    for attribute in ('string-value', 'value', 'date-value', 'boolean-value', 'time-value'):
        if cell.get(OFFICE_NAMESPACE + attribute) is not None:
            return cell.get(OFFICE_NAMESPACE + attribute)
    return ''


def _repeat_count(element, attribute, limits):
    try:
        count = int(element.get(TABLE_NAMESPACE + attribute, '1'))
    except ValueError as error:
        raise SliceError(400, 'Invalid ODF repeated row or column count') from error
    if count < 1:
        raise SliceError(400, 'Invalid ODF repeated row or column count')
    if count > limits.max_table_cells:
        raise ResourceLimitError('ODF repeated cells exceed the cell count limit')
    return count


def _table_to_md(table, limits) -> str:
    rows = []
    cell_count = 0
    text_bytes = 0
    width = 0
    for row in table.iter():
        if _local_tag(row.tag) != 'table-row':
            continue
        cells = []
        for cell in row:
            tag = _local_tag(cell.tag)
            if tag == 'table-cell':
                text = _cell_text(cell)
            elif tag == 'covered-table-cell':
                text = ''
            else:
                continue
            repeated = _repeat_count(cell, 'number-columns-repeated', limits)
            if len(cells) + repeated > limits.max_table_cells:
                raise ResourceLimitError('ODF table exceeds the cell count limit')
            text_bytes += (len(text.encode('utf-8')) + 8) * repeated
            if text_bytes > limits.max_odf_text_bytes:
                raise ResourceLimitError('ODF table exceeds the expanded text byte limit')
            cells.extend([text] * repeated)
        repeated_rows = _repeat_count(row, 'number-rows-repeated', limits)
        cell_count += max(1, len(cells)) * repeated_rows
        width = max(width, len(cells))
        text_bytes += sum(len(text.encode('utf-8')) + 8 for text in cells) * (repeated_rows - 1)
        if (cell_count > limits.max_table_cells or text_bytes > limits.max_odf_text_bytes
                or width * (len(rows) + repeated_rows) > limits.max_table_cells):
            raise ResourceLimitError('ODF table exceeds the expanded cell or text limit')
        rows.extend([cells] * repeated_rows)
    return md_table(rows)


class _MarkdownParts(list):
    def __init__(self, limits):
        super().__init__()
        self.limits = limits
        self.text_bytes = 0

    def append(self, content):
        self.text_bytes += len(content.encode('utf-8')) + 2
        if self.text_bytes > self.limits.max_odf_text_bytes:
            raise ResourceLimitError('ODF exceeds the extracted text byte limit')
        super().append(content)


def _element_to_md(element, out: List[str]):
    tag = _local_tag(element.tag)
    if tag == 'h':
        level = 1
        try:
            level = max(1, min(6, int(element.get(
                '{urn:oasis:names:tc:opendocument:xmlns:text:1.0}outline-level', '1'))))
        except Exception:
            pass
        text = _cell_text(element)
        if text:
            out.append('#' * level + ' ' + text)
        return
    if tag == 'p':
        text = _cell_text(element)
        if text:
            out.append(text)
        return
    if tag == 'table':
        md = _table_to_md(element, out.limits)
        if md:
            out.append(md)
        return
    for child in element:
        _element_to_md(child, out)


def _content_xml_to_md(content_bytes: bytes) -> str:
    return _root_to_md(parse_xml(content_bytes))


def _root_to_md(root, limits=None) -> str:
    out = _MarkdownParts(limits or ParserLimits.load())
    for element in root.iter():
        if _local_tag(element.tag) == 'body':
            for child in element:
                _element_to_md(child, out)
            break
    return "\n\n".join(out)


def _read_odf(buffer, name):
    limits = ParserLimits.load()
    extension = os.path.splitext(str(name))[1].lower()
    kind = ODF_KINDS.get(extension)
    expected_mime = ('application/vnd.oasis.opendocument.' + kind
                     + ('-template' if extension in TEMPLATE_EXTENSIONS else '')) if kind else None
    flat = extension in FLAT_EXTENSIONS or (not extension and buffer[:4] not in ZIP_MAGICS)
    if flat:
        root = parse_xml(buffer, limits)
        if root.tag != OFFICE_NAMESPACE + 'document':
            raise SliceError(400, 'Invalid Flat ODF document root')
        mime_type = root.get(OFFICE_NAMESPACE + 'mimetype')
    else:
        with document_package(buffer, limits) as archive:
            if archive.getinfo('mimetype').file_size > 256:
                raise SliceError(400, 'Invalid ODF MIME type')
            mime_type = archive.read('mimetype').decode('ascii').strip()
            manifest_name = 'META-INF/manifest.xml'
            if manifest_name in archive.namelist():
                manifest = read_package_xml(archive, manifest_name, limits)
                if manifest.tag != MANIFEST_NAMESPACE + 'manifest':
                    raise SliceError(400, 'Invalid ODF manifest')
                if any(element.tag == MANIFEST_NAMESPACE + 'encryption-data' for element in manifest.iter()):
                    raise SliceError(400, 'Encrypted ODF documents are not supported')
                root_entries = [element for element in manifest
                                if element.get(MANIFEST_NAMESPACE + 'full-path') == '/']
                if len(root_entries) != 1 or root_entries[0].get(MANIFEST_NAMESPACE + 'media-type') != mime_type:
                    raise SliceError(400, 'ODF manifest does not match the document MIME type')
            elif extension in TEMPLATE_EXTENSIONS:
                raise SliceError(400, 'ODF template manifest is missing')
            root = read_package_xml(archive, 'content.xml', limits)
        if root.tag != OFFICE_NAMESPACE + 'document-content':
            raise SliceError(400, 'Invalid ODF content root')
    if expected_mime and mime_type != expected_mime:
        raise SliceError(400, 'ODF MIME type does not match the supported format')
    if not kind:
        kind = next((candidate for candidate in ('text', 'spreadsheet', 'presentation')
                     if mime_type in ('application/vnd.oasis.opendocument.' + candidate,
                                      'application/vnd.oasis.opendocument.' + candidate + '-template')), None)
    body = root.find(OFFICE_NAMESPACE + 'body')
    if kind is None or body is None or body.find(OFFICE_NAMESPACE + kind) is None:
        raise SliceError(400, 'ODF document body does not match the supported format')
    content = _root_to_md(root, limits)
    if len(content.encode('utf-8')) > limits.max_odf_text_bytes:
        raise ResourceLimitError('ODF exceeds the extracted text byte limit')
    return content


class OdfSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        file_name: str = file.name.lower()
        return file_name.endswith(ODF_EXTENSIONS)

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            buffer = get_buffer(file)
            content = _read_odf(buffer, file.name)
            split_model = build_split_model(pattern_list, with_filter, limit)
        except (SliceError, ResourceLimitError):
            raise
        except BaseException as e:
            _log.error(f"Error processing ODF file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(
                500,
                _("Failed to parse document file {name}: {reason}").format(name=file.name, reason=e),
            )
        return {'name': file.name, 'content': split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            return _read_odf(file.read(), getattr(file, 'name', ''))
        except (SliceError, ResourceLimitError):
            raise
        except Exception as e:
            _log.error(f'Error getting odf content: {e}')
            raise SliceError(400, 'Invalid or damaged ODF document') from e
