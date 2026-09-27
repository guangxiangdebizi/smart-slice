"""压缩包族 handler：TarSplitHandle（.tar/.tar.gz/.tgz/.tar.bz2）/ SevenZipSplitHandle（.7z）。

Phase 2-A 全格式适配（2026-09-17）：解包后逐文件复用 ZipSplitHandle 的内层子清单
处理逻辑（zip_split_handle.file_to_paragraph），保持"内层单文件解析失败跳过不阻断
整包"的既有语义。注意：tar/7z 内层扩展名支持面与 zip 内层一致（含 Phase 2-A 新增
格式），但内层 markdown 图片引用落库与 .rar 支持不在本期范围（rarfile 依赖 unrar
系统二进制，已评估归入不支持清单，详见执行记录）。

.7z 经 py7zr（纯 wheel，内置压缩算法实现）解包；tar 族经标准库 tarfile。
"""
# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("archive")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError

import io
import tarfile
import traceback
from typing import List

try:
    import py7zr
    PY7ZR_AVAILABLE = True
except ImportError:  # optional extra: smart-slice[archive]
    py7zr = None
    PY7ZR_AVAILABLE = False

from smart_slice.handlers.base import BaseSplitHandle


def _inner_file_to_paragraph(name, content, pattern_list, with_filter, limit, save_image):
    # 延迟导入：zip_split_handle 在内层子清单中注册本模块 handler，模块级互相
    # import 会形成循环，故在首次调用时解析
    from smart_slice.handlers.zip_handler import file_to_paragraph
    return file_to_paragraph(_InnerBytesFile(name, content), pattern_list, with_filter, limit, save_image)


class _InnerBytesFile:
    """解包内层文件的最小 file 适配（file_to_paragraph 消费 .name / .read()）。"""

    def __init__(self, name: str, content: bytes):
        self.name = name
        self.size = len(content)
        self._content = content

    def read(self) -> bytes:
        return self._content


def _safe_member_name(name: str) -> str:
    """tar 成员路径防御：拒绝绝对路径与 .. 逃逸成员（防止解包越界读写）。"""
    normalized = name.replace("\\", "/")
    return not normalized.startswith("/") and ".." not in normalized.split("/")


class TarSplitHandle(BaseSplitHandle):
    TAR_EXTENSIONS = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".txz")

    def support(self, file, get_buffer):
        return file.name.lower().endswith(self.TAR_EXTENSIONS)

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        result = []
        try:
            buffer = get_buffer(file)
            with tarfile.open(fileobj=io.BytesIO(buffer), mode="r:*") as tar_ref:
                for member in tar_ref.getmembers():
                    if not member.isfile() or not _safe_member_name(member.name):
                        continue
                    try:
                        inner = tar_ref.extractfile(member)
                        if inner is None:
                            continue
                        content = inner.read()
                        value = _inner_file_to_paragraph(
                            member.name, content, pattern_list, with_filter, limit, save_image)
                        if isinstance(value, list):
                            result = [*result, *value]
                        else:
                            result.append(value)
                    except Exception as e:
                        # 内层单文件解析失败保持跳过语义（与 zip 内层一致），但必须留痕
                        _log.error(
                            f"Skip file {member.name} in tar {file.name}: {e}, {traceback.format_exc()}")
        except SliceError:
            raise
        except BaseException as e:
            _log.error(f"Error processing TAR file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(
                500,
                _("Failed to parse archive file {name}: {reason}").format(name=file.name, reason=e),
            )
        return result

    def get_content(self, file, save_image):
        # 文档抽取节点路径不接入 tar 内层（与 zip 内层 get_content 的差异面保持最小）
        raise SliceError(500, _("Unsupported file format"))


class SevenZipSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        # py7zr 为可选 extra（smart-slice[archive]）：缺失时不认领 .7z，
        # 由公共层统一报 400 不支持，而不是在 import 阶段炸掉整个包
        return PY7ZR_AVAILABLE and file.name.lower().endswith(".7z")

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        import os
        import tempfile
        result = []
        try:
            buffer = get_buffer(file)
            # py7zr 1.1.3 仅支持落盘 extractall（无内存 readall 接口），解包到
            # 临时目录逐文件读取后清理；py7zr 自带成员路径穿越防护
            with tempfile.TemporaryDirectory() as tmp_dir:
                with py7zr.SevenZipFile(io.BytesIO(buffer), mode="r") as archive:
                    archive.extractall(path=tmp_dir)
                for root, _dirs, files in os.walk(tmp_dir):
                    for file_name in files:
                        disk_path = os.path.join(root, file_name)
                        member_name = os.path.relpath(disk_path, tmp_dir).replace("\\", "/")
                        try:
                            with open(disk_path, "rb") as f:
                                content = f.read()
                            value = _inner_file_to_paragraph(
                                member_name, content, pattern_list, with_filter, limit, save_image)
                            if isinstance(value, list):
                                result = [*result, *value]
                            else:
                                result.append(value)
                        except Exception as e:
                            # 内层单文件解析失败保持跳过语义（与 zip 内层一致），但必须留痕
                            _log.error(
                                f"Skip file {member_name} in 7z {file.name}: {e}, {traceback.format_exc()}")
        except SliceError:
            raise
        except BaseException as e:
            _log.error(f"Error processing 7Z file {file.name}: {e}, {traceback.format_exc()}")
            raise SliceError(
                500,
                _("Failed to parse archive file {name}: {reason}").format(name=file.name, reason=e),
            )
        return result

    def get_content(self, file, save_image):
        raise SliceError(500, _("Unsupported file format"))
