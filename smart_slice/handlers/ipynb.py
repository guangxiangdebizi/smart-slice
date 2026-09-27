# coding=utf-8
"""IpynbSplitHandle（.ipynb）：Jupyter Notebook 按 cell 抽取正文。

.ipynb 是 JSON，直接当文本切片会把 base64 图片输出、执行计数、cell metadata
等噪声一并入库，检索命中率与段落质量都很差。本 handler 只取每个 cell 的
``source``：markdown cell 原样保留（标题树由此生效），code cell 包进围栏代码块
（SplitModel 的 mask_code_blocks 因此不会把其中的 ``#`` 注释误判为标题），
cell 之间以空行分隔。``outputs`` 里的纯文本（stream/text 类型）也收集，base64
图片输出跳过。纯标准库实现。
"""
from smart_slice._logging import get_logger

_log = get_logger("ipynb")

import json
import traceback
from typing import List

from smart_slice._i18n import gettext as _
from smart_slice._validation import ParserLimits
from smart_slice.exceptions import ResourceLimitError, SliceError
from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers._utils import build_split_model

IPYNB_EXTENSIONS = (".ipynb",)


def _source_text(source) -> str:
    """notebook JSON 的 source/text 字段可为 str 或 list[str]，统一拼成字符串。

    nbformat 规范：list 形式下每个元素是一行且**自带行尾换行**，故按 ``""`` 拼接。
    但手写/非规范的 notebook 可能给出无行尾换行的 list（如 ``["# Title", "intro"]``），
    直接拼接会粘成 ``# Titleintro``。启发式兜底：多元素且无一元素以换行结尾时，改用
    ``"\n"`` 连接，避免标题与正文粘连。规范输入不受影响（元素带换行 -> 仍按 "" 拼）。
    """
    if isinstance(source, str):
        return source
    if isinstance(source, list):
        items = [str(item) for item in source]
        if len(items) > 1 and not any(item.endswith("\n") for item in items):
            return "\n".join(items)
        return "".join(items)
    return ""


def _output_text(outputs) -> str:
    """收集 cell 输出中的纯文本（stream 与 execute_result/display_data 的 text/plain）。"""
    chunks = []
    for output in outputs if isinstance(outputs, list) else []:
        if not isinstance(output, dict):
            continue
        if output.get("output_type") == "stream":
            text = _source_text(output.get("text"))
        else:
            data = output.get("data") or {}
            text = _source_text(data.get("text/plain")) if isinstance(data, dict) else ""
        text = text.strip()
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def notebook_to_markdown(notebook, limits=None) -> str:
    limits = limits or ParserLimits.load()
    cells = notebook.get("cells")
    if not isinstance(cells, list):
        raise SliceError(400, "Notebook has no cell list")
    if len(cells) > limits.max_xml_elements:
        raise ResourceLimitError("Notebook exceeds the cell count limit")
    parts: List[str] = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        source = _source_text(cell.get("source")).strip()
        kind = str(cell.get("cell_type") or "").lower()
        if kind == "markdown":
            if source:
                parts.append(source)
        elif source:
            parts.append("```python\n" + source + "\n```")
        output = _output_text(cell.get("outputs"))
        if output:
            parts.append(output)
    # notebook 级 metadata.title 作为文档标题，保证无 markdown 标题时仍有 title 回退
    metadata = notebook.get("metadata") or {}
    title = ""
    if isinstance(metadata, dict):
        title = str(metadata.get("title") or "").strip()
    if title:
        parts.insert(0, f"# {title}")
    return "\n\n".join(parts)


def _load_notebook(buffer: bytes):
    text = buffer.decode("utf-8-sig", "replace")
    try:
        notebook = json.loads(text)
    except ValueError as error:
        raise SliceError(400, f"Invalid notebook JSON: {error}") from error
    if not isinstance(notebook, dict):
        raise SliceError(400, "Notebook JSON must be an object")
    return notebook


class IpynbSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        return file.name.lower().endswith(IPYNB_EXTENSIONS)

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            content = notebook_to_markdown(_load_notebook(get_buffer(file)))
            split_model = build_split_model(pattern_list, with_filter, limit)
        except (SliceError, ResourceLimitError):
            raise
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error processing notebook {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(500, _("Failed to parse document file {name}: {reason}").format(
                name=file.name, reason=e))
        return {"name": file.name, "content": split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            return notebook_to_markdown(_load_notebook(file.read()))
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error getting notebook content: {e}")
            return f"{e}"
