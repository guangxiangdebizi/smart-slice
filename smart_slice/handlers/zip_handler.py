# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("zip")


from smart_slice._i18n import gettext as _
from smart_slice.types import ImageAsset
from smart_slice import _uuid as uuid
from smart_slice._markdown import parse_md_file_link, parse_md_image

import io
import os
import re
import traceback
import zipfile
from typing import List
from urllib.parse import urljoin

from charset_normalizer import detect

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.handlers.archive import SevenZipSplitHandle, TarSplitHandle
from smart_slice.handlers.csv_handler import CsvSplitHandle
from smart_slice.handlers.doc import DocSplitHandle
from smart_slice.handlers.eml import EmlSplitHandle
from smart_slice.handlers.epub import EpubSplitHandle
from smart_slice.handlers.html import HTMLSplitHandle
from smart_slice.handlers.mobi import MobiSplitHandle
from smart_slice.handlers.mhtml import MhtmlSplitHandle
from smart_slice.handlers.msg import MsgSplitHandle
from smart_slice.handlers.odf import OdfSplitHandle
from smart_slice.handlers.pdf import PdfSplitHandle
from smart_slice.handlers.ppt import PptSplitHandle
from smart_slice.handlers.pptx import PptxSplitHandle
from smart_slice.handlers.rtf import RtfSplitHandle
from smart_slice.handlers.text import TextSplitHandle
from smart_slice.handlers.wps import WpsSplitHandle
from smart_slice.handlers.xls import XlsSplitHandle
from smart_slice.handlers.xlsx import XlsxSplitHandle


class FileBufferHandle:
    buffer = None

    def get_buffer(self, file):
        if self.buffer is None:
            self.buffer = file.read()
        return self.buffer


default_split_handle = TextSplitHandle()
# 内层子清单：Phase 2-A（2026-09-17）补齐全格式适配新 handler（与公共层
# SPLIT_HANDLERS 的既有 10 个 handler + 新格式一致），zip/tar/7z 内层文件
# 均经 file_to_paragraph 消费此清单；TextSplitHandle 仍为末位兜底。
split_handles = [
    HTMLSplitHandle(),
    MhtmlSplitHandle(),
    DocSplitHandle(),
    PdfSplitHandle(),
    XlsxSplitHandle(),
    XlsSplitHandle(),
    CsvSplitHandle(),
    PptxSplitHandle(),
    PptSplitHandle(),
    WpsSplitHandle(),
    RtfSplitHandle(),
    OdfSplitHandle(),
    EpubSplitHandle(),
    EmlSplitHandle(),
    MsgSplitHandle(),
    MobiSplitHandle(),
    TarSplitHandle(),
    SevenZipSplitHandle(),
    default_split_handle,
]


def file_to_paragraph(file, pattern_list: List, with_filter: bool, limit: int, save_inner_image):
    get_buffer = FileBufferHandle().get_buffer
    for split_handle in split_handles:
        if split_handle.support(file, get_buffer):
            return split_handle.handle(file, pattern_list, with_filter, limit, get_buffer, save_inner_image)
    raise Exception(_("Unsupported file format"))


def is_valid_uuid(uuid_str: str):
    try:
        uuid.UUID(uuid_str)
    except ValueError:
        return False
    return True


def _collect_file_refs(tokens: list, base_name: str, zip_files: List[str], content: str, update_content):
    """
    Process a list of markdown/HTML tokens (image or file-link syntax), resolve paths against
    zip_files, and return (file_list, updated_content).  update_content is a callable(old, new)
    used to patch paths in the paragraph text.
    """
    file_list = []
    for token in tokens:
        # For HTML src tags extract the src value; for markdown extract the (...) part
        src_match = re.search(r'\bsrc=["\']([^"\']+)["\']', token)
        paren_match = re.search(r"\(([^)]*)\)", token)
        if src_match:
            source_path = src_match.group(1).strip()
        elif paren_match:
            source_path = paren_match.group(1).strip().split(" ")[0]
        else:
            continue
        new_id = str(uuid.uuid7())
        file_path = urljoin(base_name, "." + source_path if source_path.startswith("/") else source_path)
        if file_path not in zip_files:
            continue
        if file_path.startswith("oss/file/") or file_path.startswith("oss/image/"):
            file_id = file_path.replace("oss/file/", "").replace("oss/image/", "")
            if is_valid_uuid(file_id):
                file_list.append({"source_file": file_path, "image_id": file_id})
            else:
                file_list.append({"source_file": file_path, "image_id": new_id})
                content = update_content(content, source_path, f"./oss/file/{new_id}")
        else:
            file_list.append({"source_file": file_path, "image_id": new_id})
            content = update_content(content, source_path, f"./oss/file/{new_id}")
    return file_list, content


def get_image_list(result_list: list, zip_files: List[str]):
    image_file_list = []
    for result in result_list:
        for p in result.get("content", []):
            content: str = p.get("content", "")
            tokens = parse_md_image(content) + parse_md_file_link(content)

            def _update(c, old, new):
                return c.replace(old, new)

            refs, content = _collect_file_refs(tokens, result.get("name"), zip_files, content, _update)
            image_file_list.extend(refs)
            p["content"] = content

    return image_file_list


def get_image_list_by_content(name: str, content: str, zip_files: List[str]):
    tokens = parse_md_image(content) + parse_md_file_link(content)

    def _update(c, old, new):
        return c.replace(old, new)

    file_list, content = _collect_file_refs(tokens, name, zip_files, content, _update)
    return file_list, content


def get_file_name(file_name):
    try:
        file_name_code = file_name.encode("cp437")
        charset = detect(file_name_code)["encoding"]
        return file_name_code.decode(charset)
    except Exception as e:
        return file_name


def filter_image_file(result_list: list, image_list):
    image_source_file_list = [image.get("source_file") for image in image_list]
    return [r for r in result_list if not image_source_file_list.__contains__(r.get("name", ""))]


class ZipSplitHandle(BaseSplitHandle):
    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        if type(limit) is str:
            limit = int(limit)
        if type(with_filter) is str:
            with_filter = with_filter.lower() == "true"
        buffer = get_buffer(file)
        bytes_io = io.BytesIO(buffer)
        result = []
        # 打开zip文件
        with zipfile.ZipFile(bytes_io, "r") as zip_ref:
            # 获取压缩包中的文件名列表
            files = zip_ref.namelist()
            # 读取压缩包中的文件内容
            for file in files:
                if file.endswith("/") or file.startswith("__MACOSX"):
                    continue
                with zip_ref.open(file) as f:
                    # 对文件内容进行处理
                    try:
                        # 处理一下文件名
                        f.name = get_file_name(f.name)
                        value = file_to_paragraph(f, pattern_list, with_filter, limit, save_image)
                        if isinstance(value, list):
                            result = [*result, *value]
                        else:
                            result.append(value)
                    except Exception as e:
                        # zip 内单个文件解析失败保持跳过语义（不因一个坏文件阻断整包），
                        # 但必须留痕，否则"部分文件被静默丢弃"无法排查（缺陷修复：补日志）。
                        # Phase 2-B 最小适配（2026-09-18）：原日志 f"Skip file {f.name} in zip
                        # {file.name}" 中外层循环变量 file（str）遮蔽了函数入参 file（zip 文件
                        # 对象），file.name 抛 AttributeError 且发生在 except 块内直接炸掉整包
                        # 解析——zip 内含任何图片/不支持文件即触发（Phase 2-B 内嵌图 OCR 的
                        # zip 场景被此缺陷阻断，故做最小修复：去掉 file.name 引用）。
                        _log.error(
                            f"Skip file {f.name} in zip: {e}, {traceback.format_exc()}")
            image_list = get_image_list(result, files)
            result = filter_image_file(result, image_list)
            image_mode_list = []
            for image in image_list:
                with zip_ref.open(image.get("source_file")) as f:
                    i = ImageAsset(
                        id=image.get("image_id"),
                        file_name=os.path.basename(image.get("source_file")),
                        meta={"debug": False, "content": f.read()},  # 这里的content是二进制数据
                    )
                    image_mode_list.append(i)
            save_image(image_mode_list)
        return result

    def support(self, file, get_buffer):
        file_name: str = file.name.lower()
        if file_name.endswith(".zip") or file_name.endswith(".ZIP"):
            return True
        return False

    def get_content(self, file, save_image):
        """
        从 zip 中提取并返回拼接的 md 文本，同时收集并保存内嵌图片（通过 save_image 回调）。
        使用 posixpath 来正确处理 zip 内部的路径拼接与规范化。
        """
        buffer = file.read() if hasattr(file, "read") else None
        bytes_io = io.BytesIO(buffer) if buffer is not None else io.BytesIO(file)
        image_list = []
        content_parts = []

        with zipfile.ZipFile(bytes_io, "r") as zip_ref:
            files = zip_ref.namelist()
            file_content_list = []
            for inner_name in files:
                if inner_name.endswith("/") or inner_name.startswith("__MACOSX"):
                    continue
                with zip_ref.open(inner_name) as zf:
                    try:
                        real_name = get_file_name(zf.name)
                    except Exception:
                        real_name = zf.name
                    # 为 split_handle 提供可重复读取的 file-like 对象
                    zf.name = real_name
                    get_buffer = FileBufferHandle().get_buffer
                    for split_handle in split_handles:
                        if split_handle.support(zf, get_buffer):
                            row = get_buffer(zf)
                            inner_file = io.BytesIO(row)
                            inner_file.name = real_name
                            md_text = split_handle.get_content(inner_file, save_image)
                            file_content_list.append({"content": md_text, "name": real_name})
                            break
            for file_content in file_content_list:
                _image_list, content = get_image_list_by_content(
                    file_content.get("name"), file_content.get("content"), files
                )
                content_parts.append(content)
                for image in _image_list:
                    image_list.append(image)

            # 将收集到的图片通过回调保存（一次性）
            if image_list:
                image_mode_list = []
                for image in image_list:
                    with zip_ref.open(image.get("source_file")) as f:
                        i = ImageAsset(
                            id=image.get("image_id"),
                            file_name=os.path.basename(image.get("source_file")),
                            meta={"debug": False, "content": f.read()},  # 这里的content是二进制数据
                        )
                        image_mode_list.append(i)
                save_image(image_mode_list)

        return "\n\n".join(content_parts)
