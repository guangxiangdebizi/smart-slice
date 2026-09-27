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
    # Phase 3（2026-09-27）格式扩展：补齐常见源码/配置/数据扩展名。此前这些后缀仅靠
    # "编码探测通过即按文本硬解码"的兜底路径命中，且因不在 TEXT_EXTENSIONS 里而落到
    # default_pattern_list（markdown 标题），源码里的 `# 注释` 会被误判为标题并抽成
    # title（保真度缺陷）。补进后自动进入 LITERAL_EXTENSIONS（仅空行分段、不解释 `#`），
    # 源码原样保留、按空行/limit 切分。注意 .ts 已在上方（与 MPEG-TS 的区分见
    # _decode_file_text 的 0x47 同步字节嗅探）。
    '.java', '.kt', '.kts', '.scala', '.groovy', '.gradle',
    '.c', '.h', '.cpp', '.cc', '.cxx', '.hpp', '.hh', '.m', '.mm',
    '.cs', '.go', '.rs', '.rb', '.php', '.swift', '.dart', '.lua', '.r', '.jl',
    '.pl', '.pm', '.ex', '.exs', '.erl', '.hs', '.clj', '.elm', '.nim', '.zig',
    '.vue', '.svelte', '.astro',
    '.bat', '.cmd', '.make', '.mak', '.cmake', '.dockerfile',
    '.proto', '.graphql', '.gql', '.thrift', '.avsc',
    '.properties', '.env', '.editorconfig', '.gitignore', '.gitattributes',
    '.diff', '.patch', '.po', '.pot', '.nfo', '.lock', '.map',
    '.adoc', '.asciidoc', '.org', '.textile', '.mediawiki',
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
