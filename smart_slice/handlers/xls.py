# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("xls")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError, ResourceLimitError

import traceback
import zipfile
from typing import List

try:
    import xlrd
    XLRD_AVAILABLE = True
except ImportError:  # optional extra: smart-slice[office]
    xlrd = None
    XLRD_AVAILABLE = False

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice._validation import ParserLimits, validate_input
from smart_slice.chunker import smart_split_paragraph


def post_cell(cell_value):
    return cell_value.replace('\r\n', '<br>').replace('\n', '<br>').replace('|', '&#124;')


def row_to_md(row):
    return '| ' + ' | '.join(
        [post_cell(str(cell)) if cell is not None else '' for cell in row]) + ' |\n'


def handle_sheet(file_name, sheet, limit: int):
    rows = iter([sheet.row_values(i) for i in range(sheet.nrows)])
    paragraphs = []
    result = {'name': file_name, 'content': paragraphs}
    try:
        title_row_list = next(rows)
        title_md_content = row_to_md(title_row_list)
        title_md_content += '| ' + ' | '.join(
            ['---' if cell is not None else '' for cell in title_row_list]) + ' |\n'
    except Exception as e:
        return result
    if len(title_row_list) == 0:
        return result
    result_item_content = ''
    for row in rows:
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
        # 智能切片升级方案 1-4（2026-09-16，P7）：单行数据超 limit 时二次切分，行内容不丢失
        # （与 xlsx_split_handle.handle_sheet 同构；累计路径已有 < limit 校验不会超限）
        if len(result_item_content) > limit:
            piece_limit = max(limit - len(title_md_content), 50)
            for piece in smart_split_paragraph(next_md_content, piece_limit):
                paragraphs.append({'content': title_md_content + piece, 'title': ''})
            result_item_content = ''
    if len(result_item_content) > 0:
        paragraphs.append({'content': result_item_content, 'title': ''})
    return result


class XlsSplitHandle(BaseSplitHandle):
    @staticmethod
    def _workbook(buffer, **kwargs):
        limits = ParserLimits.load()
        validate_input(buffer, limits)
        if xlrd.inspect_format(content=buffer) != 'xls':
            raise SliceError(400, 'Invalid or unsupported binary Excel workbook')
        workbook = xlrd.open_workbook(file_contents=buffer, **kwargs)
        if sum(sheet.nrows * sheet.ncols for sheet in workbook.sheets()) > limits.max_table_cells:
            raise ResourceLimitError('Workbook exceeds the cell count limit')
        return workbook

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        buffer = get_buffer(file)
        try:
            if type(limit) is str:
                limit = int(limit)
            workbook = self._workbook(buffer)
            worksheets = workbook.sheets()
            worksheets_size = len(worksheets)
            return [row for row in
                    [handle_sheet(file.name,
                                  sheet, limit) if worksheets_size == 1 and sheet.name == 'Sheet1' else handle_sheet(
                        sheet.name, sheet, limit) for sheet
                     in worksheets] if row is not None]
        except (SliceError, ResourceLimitError):
            raise
        except Exception as e:
            _log.error(f"Error processing XLS file {file.name}: {e}, {traceback.format_exc()}")
            # 解析失败显式报错，避免静默返回空段落
            raise SliceError(
                500,
                _("Failed to parse Excel file {name}: {reason}").format(name=file.name, reason=e),
            )

    def get_content(self, file, save_image):
        # 打开 .xls 文件
        try:
            workbook = self._workbook(file.read(), formatting_info=True)
            sheets = workbook.sheets()
            md_tables = ''
            for sheet in sheets:
                # 过滤空白的sheet
                if sheet.nrows == 0 or sheet.ncols == 0:
                    continue

                # 获取表头和内容
                headers = sheet.row_values(0)
                data = [sheet.row_values(row_idx) for row_idx in range(1, sheet.nrows)]

                # 构建 Markdown 表格
                md_table = '| ' + ' | '.join(headers) + ' |\n'
                md_table += '| ' + ' | '.join(['---'] * len(headers)) + ' |\n'
                for row in data:
                    # 将每个单元格中的内容替换换行符为 <br> 以保留原始格式
                    md_table += '| ' + ' | '.join(
                        [str(cell)
                         .replace('\r\n', '<br>')
                         .replace('\n', '<br>')
                         if cell else '' for cell in row]) + ' |\n'
                md_tables += md_table + '\n\n'

            return md_tables
        except (SliceError, ResourceLimitError):
            raise
        except Exception as e:
            _log.error(f'excel split handle error: {e}')
            raise SliceError(400, 'Invalid, encrypted or damaged binary Excel workbook') from e

    def support(self, file, get_buffer):
        # xlrd 为可选 extra（smart-slice[office]）：缺失时不认领
        if not XLRD_AVAILABLE:
            return False
        file_name: str = file.name.lower()
        buffer = get_buffer(file)
        if file_name.endswith((".xls", ".xlt")):
            try:
                if not buffer or xlrd.inspect_format(content=buffer) != 'xls':
                    raise SliceError(400, 'Invalid or unsupported binary Excel workbook')
            except (OSError, ValueError, zipfile.BadZipFile) as error:
                raise SliceError(400, 'Invalid or damaged binary Excel workbook') from error
            return True
        return False
