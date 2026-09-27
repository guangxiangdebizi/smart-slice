# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("csv")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError, ResourceLimitError

import csv
import io
import os
import traceback
from typing import List

from charset_normalizer import detect

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice._validation import ParserLimits, decode_text


def post_cell(cell_value):
    return cell_value.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '<br>').replace('|', '&#124;')


def row_to_md(row):
    return '| ' + ' | '.join(
        [post_cell(cell) if cell is not None else '' for cell in row]) + ' |\n'


class CsvSplitHandle(BaseSplitHandle):
    @staticmethod
    def _rows(buffer, name):
        limits = ParserLimits.load()
        text = decode_text(buffer, detector=detect)
        delimiter = '\t' if str(name).lower().endswith(('.tsv', '.tab')) else ','
        if not name and text:
            try:
                delimiter = csv.Sniffer().sniff(text[:65536], delimiters=',\t').delimiter
            except csv.Error:
                pass
        cells = 0
        for row in csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True):
            cells += len(row)
            if cells > limits.max_table_cells:
                raise ResourceLimitError('Delimited table exceeds the cell count limit')
            yield row

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        buffer = get_buffer(file)
        paragraphs = []
        file_name = os.path.basename(file.name)
        result = {'name': file_name, 'content': paragraphs}
        try:
            if type(limit) is str:
                limit = int(limit)
            reader = self._rows(buffer, file.name)
            try:
                title_row_list = reader.__next__()
            except StopIteration:
                # 空文件（无任何行）：返回空段落属正常语义，不视为解析失败
                return result
            title_md_content = row_to_md(title_row_list)
            title_md_content += '| ' + ' | '.join(
                ['---' if cell is not None else '' for cell in title_row_list]) + ' |\n'
            if len(title_row_list) == 0:
                return result
            result_item_content = ''
            for row in reader:
                next_md_content = row_to_md(row)
                next_md_content_len = len(next_md_content)
                result_item_content_len = len(result_item_content)
                if len(result_item_content) == 0:
                    result_item_content += title_md_content
                    result_item_content += next_md_content
                else:
                    if result_item_content_len + next_md_content_len < limit:
                        result_item_content += next_md_content
                    else:
                        paragraphs.append({'content': result_item_content, 'title': ''})
                        result_item_content = title_md_content + next_md_content
            paragraphs.append({'content': result_item_content or title_md_content, 'title': ''})
            return result
        except (SliceError, ResourceLimitError):
            raise
        except Exception as e:
            _log.error(f"Error processing CSV file {file.name}: {e}, {traceback.format_exc()}")
            # 解析失败显式报错，避免静默返回空段落
            raise SliceError(
                500,
                _("Failed to parse CSV file {name}: {reason}").format(name=file.name, reason=e),
            )

    def get_content(self, file, save_image):
        buffer = file.read()
        try:
            reader = self._rows(buffer, getattr(file, 'name', ''))
            rows = list(reader)

            if not rows:
                return ""

            # 构建 Markdown 表格
            md_lines = []

            # 添加表头
            width = max(len(row) for row in rows)
            header = [post_cell(cell) for cell in rows[0]] + [''] * (width - len(rows[0]))
            md_lines.append('| ' + ' | '.join(header) + ' |')

            # 添加分隔线
            md_lines.append('| ' + ' | '.join(['---'] * len(header)) + ' |')

            # 添加数据行
            for row in rows[1:]:
                if row:  # 跳过空行
                    # 确保行长度与表头一致,并将换行符转换为 <br>
                    padded_row = [post_cell(cell) for cell in row] + [''] * (width - len(row))
                    md_lines.append('| ' + ' | '.join(padded_row) + ' |')

            return '\n'.join(md_lines)

        except (SliceError, ResourceLimitError):
            raise
        except Exception as e:
            _log.error(f"Error processing CSV file {getattr(file, 'name', '')}: {e}, {traceback.format_exc()}")
            raise SliceError(400, 'Invalid or damaged delimited table') from e

    def support(self, file, get_buffer):
        file_name: str = file.name.lower()
        if file_name.endswith((".csv", ".tsv", ".tab")):
            return True
        return False
