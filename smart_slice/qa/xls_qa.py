# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("qa.xls")


import traceback

try:
    import xlrd
except ImportError:  # optional extra: smart-slice[office]
    xlrd = None

from smart_slice.qa._base import BaseParseQAHandle, get_title_row_index_dict, get_row_value


def handle_sheet(file_name, sheet):
    rows = iter([sheet.row_values(i) for i in range(sheet.nrows)])
    try:
        title_row_list = next(rows)
    except Exception as e:
        return {'name': file_name, 'paragraphs': []}
    if len(title_row_list) == 0:
        return {'name': file_name, 'paragraphs': []}
    title_row_index_dict = get_title_row_index_dict(title_row_list)
    paragraph_list = []
    for row in rows:
        content = get_row_value(row, title_row_index_dict, 'content')
        if content is None:
            continue
        problem = get_row_value(row, title_row_index_dict, 'problem_list')
        problem = str(problem) if problem is not None else ''
        problem_list = [{'content': p[0:255]} for p in problem.split('\n') if len(p.strip()) > 0]
        title = get_row_value(row, title_row_index_dict, 'title')
        title = str(title) if title is not None else ''
        content = str(content)
        paragraph_list.append({'title': title[0:255],
                               'content': content[0:102400],
                               'problem_list': problem_list})
    return {'name': file_name, 'paragraphs': paragraph_list}


class XlsParseQAHandle(BaseParseQAHandle):
    def support(self, file, get_buffer):
        file_name: str = file.name.lower()
        buffer = get_buffer(file)
        if file_name.endswith(".xls") and xlrd.inspect_format(content=buffer):
            return True
        return False

    def handle(self, file, get_buffer, save_image):
        buffer = get_buffer(file)
        try:
            workbook = xlrd.open_workbook(file_contents=buffer)
            worksheets = workbook.sheets()
            worksheets_size = len(worksheets)
            return [row for row in
                    [handle_sheet(file.name,
                                  sheet) if worksheets_size == 1 and sheet.name == 'Sheet1' else handle_sheet(
                        sheet.name, sheet) for sheet
                     in worksheets] if row is not None]
        except Exception as e:
            _log.error(f"Error processing XLS file {file.name}: {e}, {traceback.format_exc()}")
            return [{'name': file.name, 'paragraphs': []}]
