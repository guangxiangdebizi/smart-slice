# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("qa.xlsx")


import io
import traceback

try:
    import openpyxl
except ImportError:  # optional extra: smart-slice[office]
    openpyxl = None

from smart_slice.qa._base import BaseParseQAHandle, get_title_row_index_dict, get_row_value
from smart_slice.handlers._xlsx_images import xlsx_embed_cells_images


def handle_sheet(file_name, sheet, image_dict):
    rows = sheet.rows
    try:
        title_row_list = next(rows)
        title_row_list = [row.value for row in title_row_list]
    except Exception as e:
        return {'name': file_name, 'paragraphs': []}
    if len(title_row_list) == 0:
        return {'name': file_name, 'paragraphs': []}
    title_row_index_dict = get_title_row_index_dict(title_row_list)
    paragraph_list = []
    for row in rows:
        content = get_row_value(row, title_row_index_dict, 'content')
        if content is None or content.value is None:
            continue
        problem = get_row_value(row, title_row_index_dict, 'problem_list')
        problem = str(problem.value) if problem is not None and problem.value is not None else ''
        problem_list = [{'content': p[0:255]} for p in problem.split('\n') if len(p.strip()) > 0]
        title = get_row_value(row, title_row_index_dict, 'title')
        title = str(title.value) if title is not None and title.value is not None else ''
        content = str(content.value)
        image = image_dict.get(content, None)
        if image is not None:
            content = f'![](./oss/file/{image.id})'
        paragraph_list.append({'title': title[0:255],
                               'content': content[0:102400],
                               'problem_list': problem_list})
    return {'name': file_name, 'paragraphs': paragraph_list}


class XlsxParseQAHandle(BaseParseQAHandle):
    def support(self, file, get_buffer):
        file_name: str = file.name.lower()
        if file_name.endswith(".xlsx"):
            return True
        return False

    def handle(self, file, get_buffer, save_image):
        buffer = get_buffer(file)
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(buffer))
            try:
                image_dict: dict = xlsx_embed_cells_images(io.BytesIO(buffer))
                save_image([item for item in image_dict.values()])
            except Exception as e:
                image_dict = {}
            worksheets = workbook.worksheets
            worksheets_size = len(worksheets)
            return [row for row in
                    [handle_sheet(file.name,
                                  sheet,
                                  image_dict) if worksheets_size == 1 and sheet.title == 'Sheet1' else handle_sheet(
                        sheet.title, sheet, image_dict) for sheet
                     in worksheets] if row is not None]
        except Exception as e:
            _log.error(f"Error processing XLSX file {file.name}: {e}, {traceback.format_exc()}")
            return [{'name': file.name, 'paragraphs': []}]
