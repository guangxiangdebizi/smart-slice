# coding=utf-8
"""切片保真第一批（智能切片升级方案 2026-09-16，1-1/1-2/1-3/1-4）单测。

覆盖：
① with_filter 行首锚定：正文中间 # 保留、行首标题标记清除（方案 1-1）；
② markdown 表格超 limit 切断后后续分块增补表头、原表数据行一行不少（方案 1-2）；
③ 无标题多自然段 txt/docx 按空行分段、不再整篇成段（方案 1-3）；
④ xlsx/xls 超限行二次切分且内容拼接后与原文一致（方案 1-4）；
⑤ 格式兼容冒烟：每种 SplitHandle 至少一个"段落 content 总字符数不减"类保真断言。
⑥ 多元数据格式矩阵（2026-09-16 矩阵验证）：多 sheet/合并单元格/公式缓存/空行空列/
   超长单元格/xls 合并/docx 标题树与合并表格与图片/代码块围栏/RFC4180 CSV/嵌套
   HTML 表格/空文件边界。

全部离线运行，不依赖外部服务与数据库写入。
"""
import io
import json
import re
import unittest
import zipfile

from smart_slice.handlers.csv_handler import CsvSplitHandle
from smart_slice.handlers.doc import DocSplitHandle
from smart_slice.handlers.html import HTMLSplitHandle
from smart_slice.handlers.image import ImageSplitHandle
from smart_slice.handlers.text import TextSplitHandle, default_pattern_list as text_default_patterns
from smart_slice.handlers.xls import XlsSplitHandle, handle_sheet as xls_handle_sheet
from smart_slice.handlers.xlsx import XlsxSplitHandle, handle_sheet as xlsx_handle_sheet
from smart_slice.handlers.xmind import XmindSplitHandle
from smart_slice.handlers.zip_handler import ZipSplitHandle
from smart_slice.chunker import SplitModel, filter_special_char, smart_split_paragraph


def get_buffer(file):
    return file.read()


def save_image(items):
    return None


class FakeFile:
    def __init__(self, name, data):
        self.name = name
        self._data = data
        self.size = len(data)

    def read(self):
        return self._data

    def chunks(self):
        yield self._data


def build_table_md(row_count, row_template):
    """构造 markdown 表格文本：表头 + 分隔行 + 数据行。"""
    lines = ['| 名称 | 说明 | 参数 |', '| --- | --- | --- |']
    for i in range(1, row_count + 1):
        lines.append(row_template.format(i=i))
    return '\n'.join(lines)


class WithFilterHashPreservationTests(unittest.TestCase):
    """方案 1-1（P3）：with_filter 仅清除行首标题标记，正文中间 # 一律保留。"""

    def test_mid_line_hash_preserved(self):
        self.assertEqual(filter_special_char('code = "#123ABC"  # 行尾注释'), 'code = "#123ABC" # 行尾注释')

    def test_line_start_heading_marker_removed(self):
        self.assertEqual(filter_special_char('## 标题行\n下一行'), '标题行\n下一行')

    def test_parse_keeps_color_codes_and_comments(self):
        text = '# 文档标题\n\n正文含色值 #4A90D9 与赋值 color = "#123ABC"。\n\n## 二级标题\n\n结尾 # 保留。'
        result = SplitModel(text_default_patterns, with_filter=True, limit=4096).parse(text)
        full = ''.join(row['content'] for row in result)
        self.assertIn('#4A90D9', full)
        self.assertIn('"#123ABC"', full)
        self.assertIn('结尾 # 保留', full)
        # 行首标题标记不进入任何段落正文行首
        for row in result:
            self.assertFalse(row['content'].lstrip().startswith('#'),
                             f"段落正文不应以行首 # 开头: {row['content'][:30]!r}")


class TableHeaderRestoreTests(unittest.TestCase):
    """方案 1-2（P4）：表格超 limit 切断后，后续分块开头增补表头两行，数据行一行不少。"""

    ROW_TEMPLATE = '| 设备{i:03d} | 第{i}号设备的详细说明文字，用于撑大表格体积。 | 阈值{i}.5 |'

    def test_smart_split_keeps_table_rows_intact(self):
        table_md = build_table_md(120, self.ROW_TEMPLATE)
        chunks = smart_split_paragraph(table_md, 500)
        self.assertGreater(len(chunks), 1)
        # 原始数据行在分块拼接后一行不少
        joined = ''.join(chunks)
        for i in range(1, 121):
            self.assertIn(self.ROW_TEMPLATE.format(i=i), joined)
        # 不应从表格行中间切断：每个分块的首行要么是完整行首（| 开头）
        for chunk in chunks:
            first_line = chunk.split('\n', 1)[0]
            self.assertTrue(first_line.lstrip().startswith('|') or first_line.strip() == '',
                            f'分块首行不是完整表格行: {first_line[:40]!r}')

    def test_parse_prepends_header_to_continuation_chunks(self):
        text = ('# 设备清单\n\n' + build_table_md(120, self.ROW_TEMPLATE)
                + '\n\n表格之后的普通结尾段落。')
        result = SplitModel(text_default_patterns, with_filter=False, limit=500).parse(text)
        full = ''.join(row['content'] for row in result)
        header_line = '| 名称 | 说明 | 参数 |'
        separator_line = '| --- | --- | --- |'
        # 原表头仅出现一次，其余表头均为增补
        self.assertGreaterEqual(full.count(header_line), 2, '被切断的后续分块应增补表头')
        self.assertIn(self.ROW_TEMPLATE.format(i=120), full)
        # 所有数据行一行不少
        for i in range(1, 121):
            self.assertIn(self.ROW_TEMPLATE.format(i=i), full)
        # 逐分块校验：以数据行开头（非完整表头开头）的分块，其前一块必须含表头
        table_paras = [row['content'] for row in result if row['content'].lstrip().startswith('|')]
        for idx, para in enumerate(table_paras):
            if para.startswith(header_line):
                continue
            # 数据行开头的分块要么自身带增补表头（前两行含分隔行），要么其前一块含表头
            prev = table_paras[idx - 1] if idx > 0 else ''
            self.assertTrue(separator_line in para or separator_line in prev,
                            f'数据行开头分块缺少表头上下文: {para[:40]!r}')

    def test_docx_huge_table_end_to_end(self):
        from docx import Document
        doc = Document()
        doc.add_heading('设备清单', level=1)
        table = doc.add_table(rows=1, cols=3)
        header_cells = table.rows[0].cells
        header_cells[0].text, header_cells[1].text, header_cells[2].text = '名称', '说明', '参数'
        for i in range(1, 61):
            cells = table.add_row().cells
            cells[0].text = f'设备{i:03d}'
            cells[1].text = f'第{i}号设备的详细说明文字，用于撑大表格体积。'
            cells[2].text = f'阈值{i}.5'
        doc.add_paragraph('表格之后的普通结尾段落。')
        buf = io.BytesIO()
        doc.save(buf)
        result = DocSplitHandle().handle(FakeFile('big-table.docx', buf.getvalue()),
                                         None, False, 500, get_buffer, save_image)
        paras = result['content']
        full = ''.join(row['content'] for row in paras)
        # 60 行数据一行不少，且表头随切断增补
        for i in range(1, 61):
            self.assertIn(f'设备{i:03d}', full)
        self.assertGreaterEqual(full.count('| 名称 | 说明 | 参数 |'), 2)


class BlankLineSplitTests(unittest.TestCase):
    """方案 1-3（P6）：无标题多自然段 txt/docx 按空行分段。"""

    PARAGRAPHS = [f'第{i}段。这是无标题自然段的正文内容，用于验证空行分段模式。第{i}段结束。' for i in range(1, 7)]

    def test_txt_no_heading_split_by_blank_line(self):
        text = '\n\n'.join(self.PARAGRAPHS)
        result = SplitModel(text_default_patterns, with_filter=False, limit=4096).parse(text)
        self.assertEqual(len(result), 6, f'应按空行分为 6 段，实际 {len(result)} 段')
        joined = ''.join(row['content'] for row in result)
        for i in range(1, 7):
            self.assertIn(f'第{i}段。', joined)

    def test_txt_with_heading_keeps_title_chain(self):
        text = '# 标题\n\n正文第一段。\n\n正文第二段。'
        result = SplitModel(text_default_patterns, with_filter=False, limit=4096).parse(text)
        joined_titles = ' '.join(row['title'] for row in result)
        self.assertIn('标题', joined_titles)
        full = ''.join(row['content'] for row in result)
        self.assertIn('正文第一段。', full)
        self.assertIn('正文第二段。', full)

    def test_docx_to_md_uses_blank_line_join(self):
        from docx import Document
        doc = Document()
        for text in self.PARAGRAPHS:
            doc.add_paragraph(text)
        handle = DocSplitHandle()
        md = handle.to_md(doc, [], None)
        self.assertIn('\n\n', md)
        # 表格行内部保持单换行，不被双换行破坏
        doc2 = Document()
        table = doc2.add_table(rows=2, cols=2)
        table.rows[0].cells[0].text = '表头甲'
        table.rows[0].cells[1].text = '表头乙'
        table.rows[1].cells[0].text = '数据甲'
        table.rows[1].cells[1].text = '数据乙'
        md2 = handle.to_md(doc2, [], None)
        table_lines = [line for line in md2.split('\n') if line.strip().startswith('|')]
        self.assertEqual(len(table_lines), 3, '表头行 + 分隔行 + 数据行 3 行应完整保留')
        self.assertIn('| 表头甲 | 表头乙 |', md2)
        self.assertIn('| 数据甲 | 数据乙 |', md2)
        self.assertIn('| --- | --- |', md2)

    def test_docx_no_heading_split_by_blank_line(self):
        from docx import Document
        doc = Document()
        for text in self.PARAGRAPHS:
            doc.add_paragraph(text)
        buf = io.BytesIO()
        doc.save(buf)
        result = DocSplitHandle().handle(FakeFile('no-heading.docx', buf.getvalue()),
                                         None, False, 4096, get_buffer, save_image)
        paras = result['content']
        self.assertEqual(len(paras), 6, f'docx 无标题自然段应分为 6 段，实际 {len(paras)} 段')


class OversizeRowSplitTests(unittest.TestCase):
    """方案 1-4（P7）：xlsx/xls 单行超 limit 二次切分，行内容不丢失。"""

    GIANT_CELL = '超' + '长段落内容，包含句号。' * 500  # 约 4500 字符

    def test_xlsx_oversize_row_split(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = 'Sheet1'
        ws.append(['名称', '说明'])
        ws.append(['巨段行', self.GIANT_CELL])
        ws.append(['普通行', '这是一行普通数据。'])
        result = xlsx_handle_sheet('test.xlsx', ws, {}, 4096)
        paras = result['content']
        self.assertGreater(len(paras), 2, '超限行应被二次切分为多个分块')
        for para in paras:
            self.assertLessEqual(len(para['content']), 4096,
                                 f'分块超 limit: {len(para["content"])}')
        joined = ''.join(para['content'] for para in paras)
        self.assertIn(self.GIANT_CELL[:100], joined)
        self.assertIn(self.GIANT_CELL[-100:], joined)
        self.assertIn('普通行', joined)

    def test_xls_oversize_row_split(self):
        class FakeXlsSheet:
            def __init__(self, rows):
                self._rows = rows
                self.nrows = len(rows)

            def row_values(self, i):
                return self._rows[i]

        sheet = FakeXlsSheet([
            ['名称', '说明'],
            ['巨段行', self.GIANT_CELL],
            ['普通行', '这是一行普通数据。'],
        ])
        result = xls_handle_sheet('test.xls', sheet, 4096)
        paras = result['content']
        self.assertGreater(len(paras), 2, '超限行应被二次切分为多个分块')
        for para in paras:
            self.assertLessEqual(len(para['content']), 4096,
                                 f'分块超 limit: {len(para["content"])}')
        joined = ''.join(para['content'] for para in paras)
        self.assertIn(self.GIANT_CELL[:100], joined)
        self.assertIn(self.GIANT_CELL[-100:], joined)

    def test_xls_normal_rows_unchanged(self):
        class FakeXlsSheet:
            def __init__(self, rows):
                self._rows = rows
                self.nrows = len(rows)

            def row_values(self, i):
                return self._rows[i]

        sheet = FakeXlsSheet([['甲', '乙'], ['数据一', '数据二']])
        result = xls_handle_sheet('test.xls', sheet, 4096)
        self.assertEqual(len(result['content']), 1, '未超限的正常行不应被切分')


class FormatCompatSmokeTests(unittest.TestCase):
    """⑤ 格式兼容冒烟：每种 handle 解析最小样本不报错、内容不丢失。"""

    def _run_handle(self, handle, name, data, **kwargs):
        result = handle.handle(FakeFile(name, data), None, False, 4096, get_buffer, save_image, **kwargs)
        paras = []
        if isinstance(result, list):
            for item in result:
                content = item.get('content')
                paras.extend(content if isinstance(content, list) else [{'content': str(content)}])
        else:
            content = result.get('content')
            paras = content if isinstance(content, list) else [{'content': str(content)}]
        return ''.join(str(p.get('content', '')) for p in paras)

    def test_text_handle_content_not_lost(self):
        text = '# 标题\n\n正文内容锚点XYZ。颜色 #AABBCC 保留。\n\n第二段。'
        full = self._run_handle(TextSplitHandle(), 'sample.md', text.encode('utf-8'))
        self.assertIn('正文内容锚点XYZ', full)
        self.assertIn('#AABBCC', full)
        self.assertIn('第二段。', full)

    def test_docx_handle_content_not_lost(self):
        from docx import Document
        doc = Document()
        doc.add_paragraph('文档正文锚点DOCXYZ。')
        buf = io.BytesIO()
        doc.save(buf)
        full = self._run_handle(DocSplitHandle(), 'sample.docx', buf.getvalue())
        self.assertIn('文档正文锚点DOCXYZ', full)

    def test_xlsx_handle_content_not_lost(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(['表头甲', '表头乙'])
        ws.append(['数据甲', '数据乙'])
        buf = io.BytesIO()
        wb.save(buf)
        full = self._run_handle(XlsxSplitHandle(), 'sample.xlsx', buf.getvalue())
        self.assertIn('数据甲', full)
        self.assertIn('数据乙', full)

    def test_xls_handle_content_not_lost(self):
        class FakeXlsSheet:
            def __init__(self, rows):
                self._rows = rows
                self.nrows = len(rows)

            def row_values(self, i):
                return self._rows[i]

        result = xls_handle_sheet('sample.xls', FakeXlsSheet([['表头甲'], ['数据甲']]), 4096)
        full = ''.join(p['content'] for p in result['content'])
        self.assertIn('数据甲', full)

    def test_csv_handle_content_not_lost(self):
        full = self._run_handle(CsvSplitHandle(), 'sample.csv', '姓名,部门\n张三,研发部\n'.encode('utf-8'))
        self.assertIn('张三', full)

    def test_html_handle_content_not_lost(self):
        html = '<html><body><h1>标题锚点</h1><p>正文锚点HTMLXYZ。</p></body></html>'
        full = self._run_handle(HTMLSplitHandle(), 'sample.html', html.encode('utf-8'))
        self.assertIn('正文锚点HTMLXYZ', full)

    def test_zip_handle_content_not_lost(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('inner.txt', '压缩包内正文锚点ZIPXYZ。')
        full = self._run_handle(ZipSplitHandle(), 'sample.zip', buf.getvalue())
        self.assertIn('压缩包内正文锚点ZIPXYZ', full)

    def test_xmind_handle_content_not_lost(self):
        content = [{
            'id': 'sheet1', 'title': '画布 1',
            'rootTopic': {'id': 'root', 'title': '导图根锚点XMIND',
                          'children': {'attached': [{'id': 'b1', 'title': '分支甲'}]}},
        }]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            zf.writestr('content.json', json.dumps(content, ensure_ascii=False))
        full = self._run_handle(XmindSplitHandle(), 'sample.xmind', buf.getvalue())
        self.assertIn('导图根锚点XMIND', full)

    def test_image_handle_content_not_lost(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (4, 4), color=(255, 255, 255)).save(buf, format='PNG')
        full = self._run_handle(ImageSplitHandle(), 'sample.png', buf.getvalue(),
                                image_text_extractor=None)
        # 无 OCR 时降级为文件名占位，不报错且不丢内容
        self.assertIn('sample.png', full)

    def test_pdf_handle_no_error(self):
        try:
            from pypdf import PdfWriter
        except ImportError:
            self.skipTest('pypdf PdfWriter 不可用')
        from smart_slice.exceptions import SliceError
        from smart_slice.handlers.pdf import PdfSplitHandle
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        buf = io.BytesIO()
        writer.write(buf)
        # 空白 PDF（无文本无图片）触发既有显式报错路径（SliceError），
        # 不允许出现其他未包装异常
        with self.assertRaises(SliceError):
            PdfSplitHandle().handle(FakeFile('blank.pdf', buf.getvalue()), None, False, 4096,
                                    get_buffer, save_image)

    def test_smart_split_preserves_total_content(self):
        """切段兜底：任意文本切段后内容拼接不减（去首尾空白后）。"""
        text = '句子一。' * 2000
        chunks = smart_split_paragraph(text, 4096)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(''.join(chunks), text, '切段前后内容拼接必须完全一致')


def run_handle_groups(handle, name, data, limit=4096, with_filter=False, image_sink=None):
    """运行 handle 并归一化为 [(分组名, [段落])]；兼容单组 dict 与多组 list 出参。"""
    sink_items = []
    sink = image_sink if image_sink is not None else (lambda items: sink_items.extend(items))
    result = handle.handle(FakeFile(name, data), None, with_filter, limit, get_buffer, sink)
    groups = []
    if isinstance(result, list):
        for item in result:
            if isinstance(item, dict) and isinstance(item.get('content'), list):
                groups.append((item.get('name', ''), item['content']))
            elif isinstance(item, dict):
                groups.append(('', [item]))
    else:
        content = result.get('content')
        groups.append((result.get('name', ''), content if isinstance(content, list) else [{'content': str(content)}]))
    return groups, sink_items


def joined_group_text(groups):
    return '\n'.join(str(p.get('content', '')) for _, paras in groups for p in paras)


def build_xlsx_with_cached_formula():
    """手工构造含公式缓存值的 xlsx（openpyxl 无法同时写入公式与缓存结果）：
    B2 = SUM(B3:B4)，缓存值 42。"""
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="公式缓存" sheetId="1" r:id="rId1"/></sheets></workbook>')
    wbrels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
              '</Relationships>')
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
             '<row r="1"><c r="A1" t="inlineStr"><is><t>项目</t></is></c><c r="B1" t="inlineStr"><is><t>数值</t></is></c></row>'
             '<row r="2"><c r="A2" t="inlineStr"><is><t>合计</t></is></c><c r="B2"><f>SUM(B3:B4)</f><v>42</v></c></row>'
             '<row r="3"><c r="A3" t="inlineStr"><is><t>甲</t></is></c><c r="B3"><v>20</v></c></row>'
             '<row r="4"><c r="A4" t="inlineStr"><is><t>乙</t></is></c><c r="B4"><v>22</v></c></row>'
             '</sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('[Content_Types].xml', ct)
        z.writestr('_rels/.rels', rels)
        z.writestr('xl/workbook.xml', wb)
        z.writestr('xl/_rels/workbook.xml.rels', wbrels)
        z.writestr('xl/worksheets/sheet1.xml', sheet)
    return buf.getvalue()


class MatrixMultiSheetTests(unittest.TestCase):
    """矩阵 A1/B1：多 sheet 独立成组，sheet 名进组名，各组带表头。"""

    def test_xlsx_multi_sheet_groups(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws1 = wb.active
        ws1.title = '销售明细'
        ws1.append(['姓名', '产品'])
        ws1.append(['张三', '打印机'])
        ws2 = wb.create_sheet('宽表二十列')
        ws2.append([f'列{i:02d}' for i in range(1, 21)])
        ws2.append([f'W1-{c}' for c in range(1, 21)])
        ws3 = wb.create_sheet('长表五百行')
        ws3.append(['序号', '内容'])
        for r in range(1, 500):
            ws3.append([r, f'长表行{r}数据'])
        buf = io.BytesIO()
        wb.save(buf)
        groups, _ = run_handle_groups(XlsxSplitHandle(), 'multi.xlsx', buf.getvalue())
        names = [n for n, _ in groups]
        for sheet_name in ['销售明细', '宽表二十列', '长表五百行']:
            self.assertTrue(any(sheet_name in n for n in names), f'组名缺少 sheet 名 {sheet_name}: {names}')
        full = joined_group_text(groups)
        self.assertIn('张三', full)
        self.assertIn('列20', full)
        self.assertIn('长表行499数据', full)
        # 各组均带表头（含分隔行）
        for n, paras in groups:
            self.assertTrue(any('| --- ' in p['content'] for p in paras), f'sheet {n} 组缺表头分隔行')

    def test_xls_multi_sheet_and_merge(self):
        xlwt = __import__('xlwt')
        wb = xlwt.Workbook()
        ws = wb.add_sheet('统计表')
        ws.write_merge(0, 0, 0, 1, '合并表头甲')
        ws.write(0, 2, '数值列')
        ws.write(1, 0, '行甲')
        ws.write(1, 2, 5)
        ws.write_merge(2, 3, 0, 0, '跨行合并格')
        ws.write(3, 1, '行丙数据')
        ws2 = wb.add_sheet('明细表')
        ws2.write(0, 0, '项目')
        ws2.write(1, 0, '螺丝')
        buf = io.BytesIO()
        wb.save(buf)
        groups, _ = run_handle_groups(XlsSplitHandle(), 'multi.xls', buf.getvalue())
        names = [n for n, _ in groups]
        self.assertTrue(any('统计表' in n for n in names) and any('明细表' in n for n in names),
                        f'xls 多 sheet 组名缺失: {names}')
        full = joined_group_text(groups)
        self.assertIn('合并表头甲', full)
        self.assertIn('跨行合并格', full)
        self.assertIn('行丙数据', full)
        self.assertIn('螺丝', full)
        self.assertNotRegex(full, r'(?<![A-Za-z])None(?![A-Za-z])', 'xls 合并区不应出现 None 字面量')


class MatrixMergedCellTests(unittest.TestCase):
    """矩阵 A2/C2：xlsx/docx 合并单元格内容不丢失、不出现 None 垃圾字面量。"""

    def test_xlsx_merged_cells_no_none_garbage(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = '合并演示'
        ws['A1'] = '基本信息'
        ws['C1'] = '数值'
        ws.merge_cells('A1:B1')
        ws['A2'] = '普通格甲'
        ws['B2'] = '合并区甲'
        ws['C2'] = 11
        ws['C3'] = 22
        ws.merge_cells('B2:C3')
        ws['A5'] = '跨行标题'
        ws['B5'] = '行五数据'
        ws.merge_cells('A5:A6')
        ws['B6'] = '行六数据'
        buf = io.BytesIO()
        wb.save(buf)
        groups, _ = run_handle_groups(XlsxSplitHandle(), 'merged.xlsx', buf.getvalue())
        full = joined_group_text(groups)
        self.assertIn('基本信息', full)
        self.assertIn('合并区甲', full)
        self.assertIn('跨行标题', full)
        self.assertIn('行六数据', full)
        self.assertNotRegex(full, r'(?<![A-Za-z])None(?![A-Za-z])', '合并区不应出现 None 字面量')

    def test_docx_merged_table_content_kept(self):
        from docx import Document
        doc = Document()
        doc.add_paragraph('表格前正文段落。')
        t = doc.add_table(rows=3, cols=3)
        t.style = 'Table Grid'
        t.cell(0, 0).text = '合并表头'
        t.cell(0, 0).merge(t.cell(0, 1))
        t.cell(0, 2).text = '数值'
        t.cell(1, 0).text = '甲类'
        t.cell(1, 1).text = '子项一'
        t.cell(1, 2).text = '31'
        t.cell(2, 1).text = '子项二'
        t.cell(2, 2).text = '42'
        t.cell(1, 0).merge(t.cell(2, 0))
        doc.add_paragraph('表格后正文段落。')
        buf = io.BytesIO()
        doc.save(buf)
        groups, _ = run_handle_groups(DocSplitHandle(), 'merged.docx', buf.getvalue())
        full = joined_group_text(groups)
        for kw in ['表格前正文段落', '合并表头', '子项一', '子项二', '31', '42', '表格后正文段落']:
            self.assertIn(kw, full, f'docx 合并表格缺内容: {kw}')
        # 表格不被空行分段模式切断：数据行与分隔行保持在同段
        table_para = next(p for _, ps in groups for p in ps if '| --- |' in p['content'])
        self.assertIn('| --- |', table_para['content'])
        self.assertIn('子项二', table_para['content'], '表格数据行应与表头同段')


class MatrixFormulaAndLayoutTests(unittest.TestCase):
    """矩阵 A3/A4/A5：公式缓存值、标题行+空行空列报表、单格超长文本二次切分。"""

    def test_xlsx_formula_cached_value_preferred(self):
        """矩阵验证修复 2026-09-16：有缓存值时取值（42），不出现公式串。"""
        data = build_xlsx_with_cached_formula()
        groups, _ = run_handle_groups(XlsxSplitHandle(), 'formula.xlsx', data)
        full = joined_group_text(groups)
        self.assertIn('42', full, '应采用 Excel 缓存计算值 42')
        self.assertNotIn('=SUM', full, '有缓存值时不应出现公式串')

    def test_xlsx_formula_cache_missing_keeps_placeholder(self):
        """缓存缺失（openpyxl 生成、未经 Excel 计算）时保留公式串占位，不产出 None/乱码。"""
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = '公式'
        ws.append(['项目', '数值'])
        ws.append(['合计', '=SUM(B3:B4)'])
        ws.append(['甲', 12.5])
        buf = io.BytesIO()
        wb.save(buf)
        groups, _ = run_handle_groups(XlsxSplitHandle(), 'formula2.xlsx', buf.getvalue())
        full = joined_group_text(groups)
        self.assertIn('=SUM(B3:B4)', full)
        self.assertIn('12.5', full)
        self.assertNotRegex(full, r'(?<![A-Za-z])None(?![A-Za-z])')

    def test_xlsx_title_row_and_blank_rows_no_garbage(self):
        from openpyxl import Workbook
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = '报表'
        ws['A1'] = '季度销售统计报表'
        ws['A3'] = '区域'
        ws['B3'] = '销售额'
        ws['A4'] = '华北'
        ws['B4'] = 100
        ws['A6'] = '西南'
        ws['B6'] = 400
        buf = io.BytesIO()
        wb.save(buf)
        groups, _ = run_handle_groups(XlsxSplitHandle(), 'layout.xlsx', buf.getvalue())
        full = joined_group_text(groups)
        self.assertIn('季度销售统计报表', full, '表头前的大标题行不应丢失')
        self.assertIn('华北', full)
        self.assertIn('西南', full)
        # 空行不产生空白垃圾段
        for _, paras in groups:
            for p in paras:
                self.assertTrue(str(p['content']).strip(), '不应产生空白垃圾段')

    def test_xlsx_oversize_cell_limit1000_all_content_kept(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = '长文本'
        ws.append(['编号', '正文', '尾注'])
        long_text = ('保真起点哨兵。' + '密集填充文本。' * 300 + '保真中段哨兵。'
                     + '继续填充内容。' * 200 + '保真终点哨兵。')
        self.assertGreater(len(long_text), 3000)
        ws.append(['X-001', long_text, '尾部注记甲'])
        ws.append(['X-002', '短行内容', '尾部注记乙'])
        buf = io.BytesIO()
        wb.save(buf)
        groups, _ = run_handle_groups(XlsxSplitHandle(), 'longcell.xlsx', buf.getvalue(), limit=1000)
        full = joined_group_text(groups)
        for kw in ['保真起点哨兵', '保真中段哨兵', '保真终点哨兵', '尾部注记甲', 'X-002']:
            self.assertIn(kw, full, f'超限行二次切分丢内容: {kw}')
        # 粒度：每段 ≤ limit + 表头开销（二次切分每片带完整表头）
        limit = 1000
        header_len = len('| 编号 | 正文 | 尾注 |\n| --- | --- | --- |\n')
        for _, paras in groups:
            for p in paras:
                self.assertLessEqual(len(p['content']), limit + header_len + 50,
                                     f'分块超限: {len(p["content"])}')

    def test_xlsx_drawing_image_no_crash(self):
        """矩阵 A6（记录现状）：标准 OOXML 内嵌图片（xl/drawings）当前不被提取，
        链路不得崩溃，文字内容保留；WPS cellimages 路径之外的增强属后续项。"""
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image as XImage
        import base64
        tiny_png = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
        wb = Workbook()
        ws = wb.active
        ws.title = '含图'
        ws['A1'] = '图注标题'
        ws['A2'] = '图下说明文字'
        ws.add_image(XImage(io.BytesIO(tiny_png)), 'B2')
        buf = io.BytesIO()
        wb.save(buf)
        sink_items = []
        groups, _ = run_handle_groups(XlsxSplitHandle(), 'img.xlsx', buf.getvalue(), image_sink=sink_items.extend)
        full = joined_group_text(groups)
        self.assertIn('图注标题', full)
        self.assertIn('图下说明文字', full)


class MatrixDocxStructureTests(unittest.TestCase):
    """矩阵 C1/C3：docx 多级标题树、列表保留、内嵌图片引用与 image_sink。"""

    def test_docx_heading_tree_and_lists(self):
        from docx import Document
        doc = Document()
        doc.add_heading('项目总体说明', level=1)
        doc.add_paragraph('本章介绍项目背景。')
        doc.add_heading('建设目标', level=2)
        doc.add_heading('实施细节', level=3)
        doc.add_paragraph('细节正文。')
        doc.add_paragraph('要点甲：先做主链路', style='List Bullet')
        doc.add_paragraph('步骤一：环境准备', style='List Number')
        doc.add_heading('风险与对策', level=2)
        doc.add_paragraph('风险正文。')
        buf = io.BytesIO()
        doc.save(buf)
        groups, _ = run_handle_groups(DocSplitHandle(), 'headings.docx', buf.getvalue())
        titles = ' '.join(str(p.get('title', '')) for _, ps in groups for p in ps)
        full = joined_group_text(groups)
        for heading in ['项目总体说明', '建设目标', '实施细节', '风险与对策']:
            self.assertIn(heading, titles, f'标题树缺层级标题: {heading}')
        for kw in ['细节正文', '要点甲', '步骤一', '风险正文']:
            self.assertIn(kw, full, f'列表/正文内容丢失: {kw}')
        # 标题层级：H3 标题的 parent_chain 应包含 H1 与 H2
        h3 = next(p for _, ps in groups for p in ps if '实施细节' in str(p.get('title', '')))
        self.assertIn('项目总体说明', h3['title'])
        self.assertIn('建设目标', h3['title'])

    def test_docx_inline_image_ref_and_sink(self):
        from docx import Document
        import base64
        tiny_png = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
        doc = Document()
        doc.add_paragraph('图片前文字。')
        doc.add_picture(io.BytesIO(tiny_png))
        doc.add_paragraph('图片后文字。')
        buf = io.BytesIO()
        doc.save(buf)
        groups, sink_items = run_handle_groups(DocSplitHandle(), 'img.docx', buf.getvalue())
        full = joined_group_text(groups)
        self.assertIn('图片前文字', full)
        self.assertIn('图片后文字', full)
        self.assertIn('./oss/file/', full, '内嵌图片应转为 ./oss/file/ 引用')
        self.assertGreaterEqual(len(sink_items), 1, '图片应落 image_sink')


class MatrixTextBoundaryTests(unittest.TestCase):
    """矩阵 D1-D4：代码块围栏、RFC4180 CSV、嵌套 HTML 表格、空文件边界。"""

    def test_code_fence_line_start_hash_preserved(self):
        """矩阵验证修复 2026-09-16：``` 代码块内行首 #（注释）保留，围栏外标题标记仍清除。"""
        text = ('# 混排文档\n\n```python\n# 行首注释应保留\ndef f(x):\n'
                '    color = "#FFFFFF"  # 行内注释\n    return color\n```\n\n## 数据表\n\n正文段落。')
        result = SplitModel(text_default_patterns, with_filter=True, limit=4096).parse(text)
        full = ''.join(row['content'] for row in result)
        self.assertIn('# 行首注释应保留', full, '代码块内行首注释 # 不应被剥离')
        self.assertIn('"#FFFFFF"', full)
        self.assertIn('def f(x):', full)
        self.assertEqual(full.count('```') % 2, 0, '代码围栏应成对保留')
        for row in result:
            lines = row['content'].split('\n')
            in_fence = False
            for line in lines:
                if line.lstrip().startswith('```'):
                    in_fence = not in_fence
                    continue
                if not in_fence:
                    self.assertFalse(line.startswith('#'), f'围栏外行首 # 仍应清除: {line!r}')

    def test_strip_heading_marker_outside_code_direct(self):
        from smart_slice.chunker import strip_heading_marker_outside_code
        inside = '```py\n# keep me\n```'
        self.assertIn('# keep me', strip_heading_marker_outside_code(inside))
        outside = '## 标题\n正文'
        self.assertEqual(strip_heading_marker_outside_code(outside), '标题\n正文')
        unbalanced = '```\n# still kept'
        self.assertIn('# still kept', strip_heading_marker_outside_code(unbalanced))

    def test_csv_rfc4180_quoted_fields(self):
        csv_bytes = ('名称,描述,备注\n'
                     '"含,逗号","第一行\n第二行","普通"\n').encode('utf-8')
        groups, _ = run_handle_groups(CsvSplitHandle(), 'rfc.csv', csv_bytes)
        full = joined_group_text(groups)
        self.assertIn('含,逗号', full, '引号包裹的逗号字段应保持单格')
        self.assertIn('第一行<br>第二行', full, '引号内换行应转 <br> 保持单格')

    def test_html_nested_table_colspan_rowspan(self):
        html = ('<html><head><meta charset="utf-8"></head><body><h1>外层标题</h1>'
                '<table border="1">'
                '<tr><th colspan="2">跨列表头</th><th>普通列</th></tr>'
                '<tr><td rowspan="2">跨行格甲</td><td>格乙</td><td>格丙</td></tr>'
                '<tr><td>格丁</td><td>格戊</td></tr>'
                '</table><p>表后段落收尾。</p></body></html>').encode('utf-8')
        groups, _ = run_handle_groups(HTMLSplitHandle(), 'nested.html', html)
        full = joined_group_text(groups)
        for kw in ['跨列表头', '跨行格甲', '格乙', '格丁', '格戊', '表后段落收尾']:
            self.assertIn(kw, full, f'嵌套表内容丢失: {kw}')

    def test_empty_and_degenerate_text_files(self):
        handle = TextSplitHandle()
        # 空文件：不崩溃、不产生段落
        groups, _ = run_handle_groups(handle, 'empty.txt', b'')
        self.assertEqual(sum(len(ps) for _, ps in groups), 0)
        # 只有空行：不崩溃、不产生段落
        groups, _ = run_handle_groups(handle, 'blank.txt', '\n\n\n'.encode('utf-8'))
        self.assertEqual(sum(len(ps) for _, ps in groups), 0)
        # 单字符：1 段且字符保留
        groups, _ = run_handle_groups(handle, 'one.txt', 'A'.encode('utf-8'))
        self.assertEqual(sum(len(ps) for _, ps in groups), 1)
        self.assertIn('A', joined_group_text(groups))


if __name__ == '__main__':
    unittest.main()
