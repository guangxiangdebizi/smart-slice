# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("qa.csv")


from smart_slice._i18n import gettext as _

import csv
import io
import traceback

from charset_normalizer import detect

from smart_slice.qa._base import BaseParseQAHandle, get_title_row_index_dict, get_row_value


def read_csv_standard(file_path):
    data = []
    with open(file_path, 'r', encoding='utf-8') as file:
        reader = csv.reader(file)
        for row in reader:
            data.append(row)
    return data


class CsvParseQAHandle(BaseParseQAHandle):
    def support(self, file, get_buffer):
        file_name: str = file.name.lower()
        if file_name.endswith(".csv"):
            return True
        return False

    def handle(self, file, get_buffer, save_image):
        buffer = get_buffer(file)
        try:
            reader = csv.reader(io.TextIOWrapper(io.BytesIO(buffer), encoding=detect(buffer)['encoding']))
            try:
                title_row_list = reader.__next__()
            except Exception as e:
                return [{'name': file.name, 'paragraphs': []}]
            if len(title_row_list) == 0:
                return [{'name': file.name, 'paragraphs': []}]
            title_row_index_dict = get_title_row_index_dict(title_row_list)
            paragraph_list = []
            for row in reader:
                content = get_row_value(row, title_row_index_dict, 'content')
                if content is None:
                    continue
                problem = get_row_value(row, title_row_index_dict, 'problem_list')
                problem = str(problem) if problem is not None else ''
                problem_list = [{'content': p[0:255]} for p in problem.split('\n') if len(p.strip()) > 0]
                title = get_row_value(row, title_row_index_dict, 'title')
                title = str(title) if title is not None else ''
                paragraph_list.append({'title': title[0:255],
                                       'content': content[0:102400],
                                       'problem_list': problem_list})
            return [{'name': file.name, 'paragraphs': paragraph_list}]
        except Exception as e:
            _log.error(f"Error processing CSV file {file.name}: {e}, {traceback.format_exc()}")
            return [{'name': file.name, 'paragraphs': []}]
