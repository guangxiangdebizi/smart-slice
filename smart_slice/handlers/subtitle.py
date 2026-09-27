# coding=utf-8
"""SubtitleSplitHandle（.srt/.vtt/.ass/.ssa/.sub）：字幕按可读文本抽取。

字幕直接当文本切片会把序号、时间码（``00:00:01,000 --> 00:00:02,000``）、ASS
样式声明行（``[Script Info]``/``[Events]``/``Dialogue:`` 的头部字段）一并入库，
既污染向量也浪费段落预算。本 handler 按容器格式剥除非台词行，只留可读文本，
每条字幕一行、相邻空行分段，交给 SplitModel。纯标准库实现，裸环境可用。
"""
from smart_slice._logging import get_logger

_log = get_logger("subtitle")

import re
import traceback
from typing import List

from smart_slice._i18n import gettext as _
from smart_slice._validation import decode_text
from smart_slice.exceptions import ResourceLimitError, SliceError
from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers._utils import build_split_model

SUBTITLE_EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa", ".sub")

#: SRT/VTT 时间码行；ASS Dialogue 的时间码字段
_TIMECODE = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}\s*-->")
_INDEX = re.compile(r"^\s*\d+\s*$")
#: ASS Dialogue 行：取最后一个逗号后的台词文本（前 9 个字段为样式/时间等）
_ASS_DIALOGUE = re.compile(r"^Dialogue:\s*", re.IGNORECASE)
#: 字幕内联标记（HTML 标签、ASS override {\...}、音乐符号）
_INLINE_TAG = re.compile(r"<[^>]+>|\{\\[^}]*\}")


def _split_ass(text: str) -> List[str]:
    """ASS/SSA 只取 Dialogue 台词字段。

    [Script Info]（Title/ScriptType 等）与 [Events] 的 Format 声明行都是元数据，
    混入段落会污染向量；故仅匹配 Dialogue: 行，丢弃其前 9 个逗号分隔字段
    （Layer/Start/End/Style/Name/MarginL/MarginR/MarginV/Effect），只留 Text 字段。
    """
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not _ASS_DIALOGUE.match(line):
            continue
        line = _ASS_DIALOGUE.sub("", line)
        fields = line.split(",", 9)
        line = fields[9] if len(fields) >= 10 else ""
        line = _INLINE_TAG.sub("", line).strip()
        # ASS 用 \N 表示换行
        line = line.replace("\\N", " ").replace("\\n", " ").strip()
        if line:
            lines.append(line)
    return lines


def _split_srt_vtt(text: str) -> List[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _INDEX.match(line) or _TIMECODE.match(line):
            continue
        if line.upper().startswith("WEBVTT") or line.upper().startswith("NOTE"):
            continue
        line = _INLINE_TAG.sub("", line).strip()
        if line:
            lines.append(line)
    return lines


def subtitle_to_text(text: str, name: str) -> str:
    """按扩展名选择字幕方言，返回每条一行的可读台词文本。"""
    lower = name.lower()
    if lower.endswith((".ass", ".ssa")):
        cues = _split_ass(text)
    else:
        cues = _split_srt_vtt(text)
    # 相邻字幕多为连续对话：整体作为段落流，交 SplitModel 按空行/limit 切分
    return "\n".join(cues)


class SubtitleSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        return file.name.lower().endswith(SUBTITLE_EXTENSIONS)

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        try:
            content = subtitle_to_text(decode_text(get_buffer(file)), file.name)
            split_model = build_split_model(pattern_list, with_filter, limit)
        except (SliceError, ResourceLimitError):
            raise
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error processing subtitle {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(500, _("Failed to parse document file {name}: {reason}").format(
                name=file.name, reason=e))
        return {"name": file.name, "content": split_model.parse(content)}

    def get_content(self, file, save_image):
        try:
            return subtitle_to_text(decode_text(file.read()), file.name)
        except BaseException as e:  # noqa: BLE001
            _log.error(f"Error getting subtitle content: {e}")
            return f"{e}"
