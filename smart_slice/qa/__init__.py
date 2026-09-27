# coding=utf-8
"""QA-pair and table parsers.

Two extra extraction modes used by knowledge bases on top of plain slicing:

- *QA*: a spreadsheet / CSV / markdown table whose columns are the question and
  the answer.  Each row becomes one paragraph, and the question column feeds
  retrieval so a query matches the phrasing a reader would use.
- *table*: a spreadsheet whose first row is a header.  Every data row is emitted
  as ``header: value`` pairs so a single row stays interpretable once it is
  detached from its sheet.

These handlers have a different interface from the slicing handlers
(``handle(file, get_buffer, save_image)``), so they live in their own registry.
"""
from ._base import BaseParseQAHandle, get_row_value, get_title_row_index_dict
from ._table_base import BaseParseTableHandle
from .csv_qa import CsvParseQAHandle
from .csv_table import CsvParseTableHandle
from .md_qa import MarkdownParseQAHandle
from .xls_qa import XlsParseQAHandle
from .xls_table import XlsParseTableHandle
from .xlsx_qa import XlsxParseQAHandle
from .xlsx_table import XlsxParseTableHandle
from .zip_qa import ZipParseQAHandle

__all__ = [
    "BaseParseQAHandle",
    "BaseParseTableHandle",
    "get_row_value",
    "get_title_row_index_dict",
    "QA_HANDLERS",
    "TABLE_HANDLERS",
    "CsvParseQAHandle",
    "MarkdownParseQAHandle",
    "XlsParseQAHandle",
    "XlsxParseQAHandle",
    "ZipParseQAHandle",
    "CsvParseTableHandle",
    "XlsParseTableHandle",
    "XlsxParseTableHandle",
]

QA_HANDLERS = (
    MarkdownParseQAHandle(),
    CsvParseQAHandle(),
    XlsxParseQAHandle(),
    XlsParseQAHandle(),
    ZipParseQAHandle(),
)

TABLE_HANDLERS = (
    CsvParseTableHandle(),
    XlsxParseTableHandle(),
    XlsParseTableHandle(),
)