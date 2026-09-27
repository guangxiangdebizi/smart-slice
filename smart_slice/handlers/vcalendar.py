# coding=utf-8
"""VcalendarSplitHandle（.ics）/ VcardSplitHandle（.vcf）：结构化日程与名片抽取。

.ics（iCalendar）与 .vcf（vCard）都是 ``KEY;PARAM=VALUE:内容`` 的行式文本，带
折行（续行以空白开头）。直接当文本切片会把 DTSTAMP、UID、PRODID 等机器字段一并
入库。本模块用标准库解析：解折行、按组件（VEVENT/VTODO/VJOURNAL、VCARD）分组，
只保留有阅读价值的字段并映射为 Markdown（事件标题→标题，时间/地点/描述→键值
行；名片姓名→标题，电话/邮箱/地址→键值行），交给 SplitModel。裸环境可用。
"""
from smart_slice._logging import get_logger

_log = get_logger("vcal")

import traceback
from typing import Dict, List, Optional, Tuple

from smart_slice._i18n import gettext as _
from smart_slice._validation import decode_text
from smart_slice.exceptions import ResourceLimitError, SliceError
from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers._utils import build_split_model

ICS_EXTENSIONS = (".ics", ".ifb")
VCF_EXTENSIONS = (".vcf",)


def unfold(text: str, limits) -> List[str]:
    """RFC 5545/6350 折行还原：以单空白开头的续行接回上一行。"""
    lines: List[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
        if len(lines) > limits.max_mime_parts:
            raise ResourceLimitError("Calendar/contact file exceeds the line limit")
    return lines


def parse_line(line: str) -> Optional[Tuple[str, Dict[str, str], str]]:
    """拆 ``NAME;PARAM=V;P2=V2:VALUE`` -> (name, params, value)；非法行返回 None。"""
    if ":" not in line:
        return None
    head, _, value = line.partition(":")
    name, _, param_blob = head.partition(";")
    params: Dict[str, str] = {}
    for param in param_blob.split(";"):
        if "=" in param:
            key, _, val = param.partition("=")
            params[key.strip().upper()] = val.strip()
    return name.strip().upper(), params, value


def _clean(value: str) -> str:
    """转义还原：\\n -> 换行，去掉尾部空白。"""
    return value.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").strip()


def _ics_to_markdown(text: str, limits) -> str:
    blocks: List[str] = []
    stack: List[Tuple[str, List[str]]] = []
    for line in unfold(text, limits):
        parsed = parse_line(line)
        if parsed is None:
            continue
        name, params, value = parsed
        if name == "BEGIN":
            stack.append((value.upper(), []))
        elif name == "END":
            if not stack:
                continue
            kind, fields = stack.pop()
            rendered = _render_ics_component(kind, fields)
            # 子组件渲染结果直接进 blocks：父组件（VCALENDAR）只当容器，不吸收子
            # 组件文本，避免把渲染串当父组件字段处理（曾导致 .ics 产出 0 段）
            if rendered:
                blocks.append(rendered)
        elif stack:
            stack[-1][1].append((name, params, value))
    return "\n\n".join(block for block in blocks if block.strip())


_ICS_FIELD_LABELS = {
    "DTSTART": "开始", "DTEND": "结束", "DUE": "截止", "LOCATION": "地点",
    "DESCRIPTION": "描述", "SUMMARY": "摘要", "ORGANIZER": "组织者", "STATUS": "状态",
    "CATEGORIES": "分类", "URL": "链接", "COMMENT": "备注",
}


def _render_ics_component(kind: str, fields) -> str:
    if kind not in ("VEVENT", "VTODO", "VJOURNAL"):
        return ""
    title = ""
    rows: List[str] = []
    for name, params, value in fields:
        text = _clean(value)
        if not text:
            continue
        if name == "SUMMARY" and not title:
            title = text
            continue
        label = _ICS_FIELD_LABELS.get(name)
        if label:
            rows.append(f"- {label}: {text}" if "\n" not in text else f"- {label}:\n{text}")
    heading = title or kind
    body = "\n".join(rows)
    return f"# {heading}\n\n{body}" if body else f"# {heading}"


def _vcf_to_markdown(text: str, limits) -> str:
    cards: List[str] = []
    current: List = []
    for line in unfold(text, limits):
        parsed = parse_line(line)
        if parsed is None:
            continue
        name, params, value = parsed
        if name == "BEGIN" and value.upper() == "VCARD":
            current = []
        elif name == "END" and value.upper() == "VCARD":
            rendered = _render_vcard(current)
            if rendered:
                cards.append(rendered)
            current = []
        elif current is not None:
            current.append((name, params, value))
    return "\n\n".join(cards)


_VCF_FIELD_LABELS = {
    "TEL": "电话", "EMAIL": "邮箱", "ADR": "地址", "ORG": "组织", "TITLE": "职位",
    "URL": "链接", "NOTE": "备注", "BDAY": "生日", "ROLE": "角色", "NICKNAME": "昵称",
}


def _render_vcard(fields) -> str:
    name = ""
    rows: List[str] = []
    for field_name, params, value in fields:
        text = _clean(value)
        if not text:
            continue
        if field_name == "FN" and not name:
            name = text
            continue
        if field_name == "N" and not name:
            parts = [p.strip() for p in text.split(";") if p.strip()]
            name = " ".join(parts)
            continue
        if field_name == "ADR":
            text = ", ".join(p.strip() for p in text.split(";") if p.strip())
        label = _VCF_FIELD_LABELS.get(field_name)
        if label and text:
            rows.append(f"- {label}: {text}")
    if not name and not rows:
        return ""
    heading = name or "名片"
    body = "\n".join(rows)
    return f"# {heading}\n\n{body}" if body else f"# {heading}"


class VcalendarSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        if not file.name.lower().endswith(ICS_EXTENSIONS):
            return False
        try:
            return b"BEGIN:VCALENDAR" in get_buffer(file)[:512].upper()
        except Exception:  # noqa: BLE001
            return False

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            from smart_slice._validation import ParserLimits
            content = _ics_to_markdown(decode_text(get_buffer(file)), ParserLimits.load())
            split_model = build_split_model(pattern_list, with_filter, limit)
        except (SliceError, ResourceLimitError):
            raise
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error processing ICS file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(500, _("Failed to parse document file {name}: {reason}").format(
                name=file.name, reason=e))
        return {"name": file.name, "content": split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            from smart_slice._validation import ParserLimits
            return _ics_to_markdown(decode_text(file.read()), ParserLimits.load())
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error getting ics content: {e}")
            return f"{e}"


class VcardSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        if not file.name.lower().endswith(VCF_EXTENSIONS):
            return False
        try:
            return b"BEGIN:VCARD" in get_buffer(file)[:512].upper()
        except Exception:  # noqa: BLE001
            return False

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            from smart_slice._validation import ParserLimits
            content = _vcf_to_markdown(decode_text(get_buffer(file)), ParserLimits.load())
            split_model = build_split_model(pattern_list, with_filter, limit)
        except (SliceError, ResourceLimitError):
            raise
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error processing VCF file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(500, _("Failed to parse document file {name}: {reason}").format(
                name=file.name, reason=e))
        return {"name": file.name, "content": split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            from smart_slice._validation import ParserLimits
            return _vcf_to_markdown(decode_text(file.read()), ParserLimits.load())
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error getting vcf content: {e}")
            return f"{e}"
