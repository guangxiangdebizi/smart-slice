# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("table.csv")


import csv
import io
import traceback

from charset_normalizer import detect
from smart_slice.qa._base import get_title_row_index_dict, get_row_value
from smart_slice.qa._table_base import BaseParseTableHandle


class CsvParseTableHandle(BaseParseTableHandle):
    def support(self, file, get_buffer):
        file_name: str = file.name.lower()
        if file_name.endswith(".csv"):
            return True
        return False

    def handle(self, file, get_buffer, save_image):
        buffer = get_buffer(file)
        try:
            content = buffer.decode(detect(buffer)['encoding'])
        except BaseException as e:
            _log.error(f"Error processing CSV file {file.name}: {e}, {traceback.format_exc()}")
            return [{'name': file.name, 'paragraphs': []}]

        csv_model = content.split('\n')
        paragraphs = []
        # 第一行为标题
        title = csv_model[0].split(',')
        for row in csv_model[1:]:
            if not row:
                continue
            line = '; '.join([f'{key}:{value}' for key, value in zip(title, row.split(','))])
            paragraphs.append({'title': '', 'content': line})

        return [{'name': file.name, 'paragraphs': paragraphs}]

    def get_content(self, file, save_image):
        buffer = file.read()
        try:
            reader = csv.reader(io.TextIOWrapper(io.BytesIO(buffer), encoding=detect(buffer)['encoding']))
            rows = list(reader)

            if not rows:
                return ""

            # 构建 Markdown 表格
            md_lines = []

            # 添加表头
            header = [cell.replace('\n', '<br>').replace('\r', '') for cell in rows[0]]
            md_lines.append('| ' + ' | '.join(header) + ' |')

            # 添加分隔线
            md_lines.append('| ' + ' | '.join(['---'] * len(header)) + ' |')

            # 添加数据行
            for row in rows[1:]:
                if row:  # 跳过空行
                    # 确保行长度与表头一致,并将换行符转换为 <br>
                    padded_row = [
                                     cell.replace('\n', '<br>').replace('\r', '') for cell in row
                                 ] + [''] * (len(header) - len(row))
                    md_lines.append('| ' + ' | '.join(padded_row[:len(header)]) + ' |')

            return '\n'.join(md_lines)

        except Exception as e:
            _log.error(f"Error processing CSV file {file.name}: {e}, {traceback.format_exc()}")
            return ""
