"""全格式适配新增 handler 的公共小工具（2026-09-17 Phase 2-A）。

仅服务新增的 office/ebook/mail/archive 族 handler；既有 10 个 handler 的行为保持
原样，不做回改。统一两件事：

- build_split_model：按既有各 handler 的惯例构造 SplitModel
  （limit/with_filter 兼容字符串入参；pattern_list 非空优先，否则用各格式缺省模式）；
- md_table：二维数组转 Markdown 管道表格（pptx/odf 表格提取复用）。
"""
# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.


import re
from typing import List

from smart_slice.chunker import SplitModel

# 缺省分段模式与 text/docx 族一致：六级标题 + 空行分段（智能切片升级方案 1-3 语义）
default_text_pattern_list = [
    re.compile('(?<=^)# (?!-\\*- coding:).*|(?<=\\n)# (?!-\\*- coding:).*'),
    re.compile('(?<=\\n)(?<!#)## (?!#).*|(?<=^)(?<!#)## (?!#).*'),
    re.compile("(?<=\\n)(?<!#)### (?!#).*|(?<=^)(?<!#)### (?!#).*"),
    re.compile("(?<=\\n)(?<!#)#### (?!#).*|(?<=^)(?<!#)#### (?!#).*"),
    re.compile("(?<=\\n)(?<!#)##### (?!#).*|(?<=^)(?<!#)##### (?!#).*"),
    re.compile("(?<=\\n)(?<!#)###### (?!#).*|(?<=^)(?<!#)###### (?!#).*"),
    re.compile("(?<!\n)\n\n+")
]


def build_split_model(pattern_list: List, with_filter, limit, default_patterns=None) -> SplitModel:
    """按既有 handler 惯例构造 SplitModel（docx/html/pdf 等实现的同款逻辑收口）。"""
    if type(limit) is str:
        limit = int(limit)
    if type(with_filter) is str:
        with_filter = with_filter.lower() == 'true'
    if pattern_list is not None and len(pattern_list) > 0:
        return SplitModel(pattern_list, with_filter, limit)
    return SplitModel(default_patterns if default_patterns is not None else default_text_pattern_list,
                      with_filter=with_filter, limit=limit)


def md_table(rows: List[List[str]]) -> str:
    """二维文本数组 → Markdown 管道表格（首行为表头）。空表返回空串。"""
    if not rows or not rows[0]:
        return ''
    width = max(len(row) for row in rows)
    normalized = [row + [''] * (width - len(row)) for row in rows]
    out = ['| ' + ' | '.join(cell.replace('|', '\\|').replace('\n', '</br>') for cell in normalized[0]) + ' |']
    out.append('| ' + ' | '.join(['---'] * width) + ' |')
    for row in normalized[1:]:
        out.append('| ' + ' | '.join(cell.replace('|', '\\|').replace('\n', '</br>') for cell in row) + ' |')
    return '\n'.join(out) + '\n'
