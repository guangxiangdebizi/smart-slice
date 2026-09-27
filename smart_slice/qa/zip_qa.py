# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("qa.zip")


from smart_slice._i18n import gettext as _
from smart_slice.types import ImageAsset
from smart_slice import _uuid as uuid
from smart_slice._markdown import parse_md_file_link, parse_md_image

import io
import os
import re
import zipfile
from typing import List
from urllib.parse import urljoin


from smart_slice.qa._base import BaseParseQAHandle
from smart_slice.qa.csv_qa import CsvParseQAHandle
from smart_slice.qa.xls_qa import XlsParseQAHandle
from smart_slice.qa.xlsx_qa import XlsxParseQAHandle


class FileBufferHandle:
    buffer = None

    def get_buffer(self, file):
        if self.buffer is None:
            self.buffer = file.read()
        return self.buffer


split_handles = [
    XlsParseQAHandle(),
    XlsxParseQAHandle(),
    CsvParseQAHandle()
]


def file_to_paragraph(file, save_inner_image):
    """
    文件转换为段落列表
    @param file: 文件
    @return: {
      name:文件名
      paragraphs:段落列表
    }
    """
    get_buffer = FileBufferHandle().get_buffer
    for split_handle in split_handles:
        if split_handle.support(file, get_buffer):
            return split_handle.handle(file, get_buffer, save_inner_image)
    raise Exception(_("Unsupported file format"))


def is_valid_uuid(uuid_str: str):
    """
    校验字符串是否是uuid
    @param uuid_str: 需要校验的字符串
    @return: bool
    """
    try:
        uuid.UUID(uuid_str)
    except ValueError:
        return False
    return True


def get_image_list(result_list: list, zip_files: List[str]):
    image_file_list = []
    for result in result_list:
        for p in result.get('paragraphs', []):
            content: str = p.get('content', '')
            tokens = parse_md_image(content) + parse_md_file_link(content)
            for token in tokens:
                src_match = re.search(r'\bsrc=["\']([^"\']+)["\']', token)
                paren_match = re.search(r'\(([^)]*)\)', token)
                if src_match:
                    source_path = src_match.group(1).strip()
                elif paren_match:
                    source_path = paren_match.group(1).strip().split(" ")[0]
                else:
                    continue
                new_image_id = str(uuid.uuid7())
                image_path = urljoin(result.get('name'), '.' + source_path if source_path.startswith(
                    '/') else source_path)
                if image_path not in zip_files:
                    continue
                if image_path.startswith('oss/file/') or image_path.startswith('oss/image/'):
                    image_id = image_path.replace('oss/file/', '').replace('oss/image/', '')
                    if is_valid_uuid(image_id):
                        image_file_list.append({'source_file': image_path, 'image_id': image_id})
                    else:
                        image_file_list.append({'source_file': image_path, 'image_id': new_image_id})
                        content = content.replace(source_path, f'./oss/file/{new_image_id}')
                        p['content'] = content
                else:
                    image_file_list.append({'source_file': image_path, 'image_id': new_image_id})
                    content = content.replace(source_path, f'./oss/file/{new_image_id}')
                    p['content'] = content
    return image_file_list


def filter_image_file(result_list: list, image_list):
    image_source_file_list = [image.get('source_file') for image in image_list]
    return [r for r in result_list if not image_source_file_list.__contains__(r.get('name', ''))]


class ZipParseQAHandle(BaseParseQAHandle):

    def handle(self, file, get_buffer, save_image):
        buffer = get_buffer(file)
        bytes_io = io.BytesIO(buffer)
        result = []
        # 打开zip文件
        with zipfile.ZipFile(bytes_io, 'r') as zip_ref:
            # 获取压缩包中的文件名列表
            files = zip_ref.namelist()
            # 读取压缩包中的文件内容
            for file in files:
                # 跳过 macOS 特有的元数据目录和文件
                if file.endswith('/') or file.startswith('__MACOSX'):
                    continue
                with zip_ref.open(file) as f:
                    # 对文件内容进行处理
                    try:
                        value = file_to_paragraph(f, save_image)
                        if isinstance(value, list):
                            result = [*result, *value]
                        else:
                            result.append(value)
                    except Exception:
                        pass
            image_list = get_image_list(result, files)
            result = filter_image_file(result, image_list)
            image_mode_list = []
            for image in image_list:
                with zip_ref.open(image.get('source_file')) as f:
                    i = ImageAsset(
                        id=image.get('image_id'),
                        file_name=os.path.basename(image.get('source_file')),
                        meta={'debug': False, 'content': f.read()}
                    )
                    image_mode_list.append(i)
            save_image(image_mode_list)
        return result

    def support(self, file, get_buffer):
        file_name: str = file.name.lower()
        if file_name.endswith(".zip") or file_name.endswith(".ZIP"):
            return True
        return False
