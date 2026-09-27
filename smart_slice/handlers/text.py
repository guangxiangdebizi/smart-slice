# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("text")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError, ResourceLimitError

import re
import traceback
from typing import List

from charset_normalizer import detect

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice._validation import decode_text
from smart_slice.chunker import SplitModel

default_pattern_list = [
    re.compile('(?<=^)# (?!-\\*- coding:).*|(?<=\\n)# (?!-\\*- coding:).*'),
    re.compile('(?<=\\n)(?<!#)## (?!#).*|(?<=^)(?<!#)## (?!#).*'),
    re.compile("(?<=\\n)(?<!#)### (?!#).*|(?<=^)(?<!#)### (?!#).*"),
    re.compile("(?<=\\n)(?<!#)#### (?!#).*|(?<=^)(?<!#)#### (?!#).*"),
    re.compile("(?<=\\n)(?<!#)##### (?!#).*|(?<=^)(?<!#)##### (?!#).*"),
    re.compile("(?<=\\n)(?<!#)###### (?!#).*|(?<=^)(?<!#)###### (?!#).*"),
    # 智能切片升级方案 1-3（2026-09-16，P6）：补空行分段模式（置于标题模式之后、
    # 长度兜底之前），无标题的 txt/md 按空行分段，不再整篇成段；写法对齐 pdf_split_handle
    re.compile("(?<!\n)\n\n+")
]

TEXT_EXTENSIONS = (
    '.txt', '.md', '.markdown', '.log', '.json', '.jsonl', '.ndjson', '.yaml', '.yml',
    '.toml', '.ini', '.cfg', '.conf', '.rst', '.tex', '.sql', '.py', '.js', '.ts',
    '.tsx', '.jsx', '.sh', '.bash', '.ps1', '.css', '.scss', '.xml',
)
LITERAL_EXTENSIONS = tuple(extension for extension in TEXT_EXTENSIONS
                           if extension not in ('.txt', '.md', '.markdown', '.rst'))

end = [".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".webm", ".mpeg", ".mpg", ".3gp", ".rmvb",
       ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus", ".alac", ".aiff", ".amr",
       ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp", ".heif", ".raw", ".ico", ".svg", ".pdf",
       # Phase 2-A（2026-09-17）排除表补齐：以下扩展名由专属 handler 处理或明确不支持，
       # 一律不进入"编码探测通过即按文本硬解码"的兜底路径（防二进制乱码入库）
       ".tif", ".heic",   # 图片（.tif 对齐 .tiff；.heic 在 pillow-heif 可用时由 ImageSplitHandle 处理）
       ".rtf",            # RtfSplitHandle（magic 校验未通过时同样拒绝）
       ".ppt", ".wps", ".et",  # 老 OLE/私有二进制防误判（新格式 sniff 未命中也在此兜住）
       ".rar",            # 无纯 Python 解析方案（rarfile 依赖 unrar 系统件），明确不支持
       ".key", ".pages", ".numbers", ".exe", ".dll", ".msi", ".dmg", ".apk", ".iso"
       ]


def _decode_file_text(file, buffer):
    if str(getattr(file, 'name', '')).lower().endswith('.ts'):
        for stride, offset in ((188, 0), (192, 4), (204, 0)):
            if len(buffer) >= offset + stride * 3 and all(
                    buffer[offset + index * stride] == 0x47 for index in range(3)):
                raise SliceError(400, 'MPEG transport streams are not supported as TypeScript text')
    return decode_text(buffer, detector=detect)


class TextSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        file_name: str = file.name.lower()
        if file_name.endswith(TEXT_EXTENSIONS):
            return True
        lower_name = file_name.lower()
        if any([True for item in end if lower_name.endswith(item)]):
            return False
        try:
            return bool(decode_text(get_buffer(file), detector=detect).strip())
        except SliceError:
            return False

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        buffer = get_buffer(file)
        if type(limit) is str:
            limit = int(limit)
        if type(with_filter) is str:
            with_filter = with_filter.lower() == 'true'
        if file.name.lower().endswith(LITERAL_EXTENSIONS) and not pattern_list:
            split_model = SplitModel([], with_filter=False, limit=limit)
        elif pattern_list is not None and len(pattern_list) > 0:
            split_model = SplitModel(pattern_list, with_filter, limit)
        else:
            split_model = SplitModel(default_pattern_list, with_filter=with_filter, limit=limit)
        try:
            content = _decode_file_text(file, buffer)
        except (SliceError, ResourceLimitError):
            raise
        except BaseException as e:
            _log.error(f"Error processing TEXT file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(
                500,
                _("Failed to parse text file {name}: {reason}").format(name=file.name, reason=e),
            )
        return {'name': file.name, 'content': split_model.parse(content)}

    def get_content(self, file, save_image):
        return _decode_file_text(file, file.read())
