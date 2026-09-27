# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("xlsx_images")


from smart_slice.types import ImageAsset
from smart_slice import _uuid as uuid

import io
import traceback
from functools import reduce
from io import BytesIO
from xml.etree.ElementTree import fromstring
from zipfile import ZipFile

from PIL import Image as PILImage
from openpyxl.drawing.image import Image as openpyxl_Image
from openpyxl.packaging.relationship import get_rels_path, get_dependents
from openpyxl.xml.constants import SHEET_DRAWING_NS, REL_NS, SHEET_MAIN_NS


from contextlib import contextmanager

from PIL import ImageFile


@contextmanager
def _permissive_pil():
    """Lift the Pillow truncated-image and pixel-cap guards for one parse, then restore.

    Mutating these globals at import time would silently disable the embedding
    application decompression-bomb protection, so the relaxation is scoped here.
    """
    prev_truncated = ImageFile.LOAD_TRUNCATED_IMAGES
    prev_max_pixels = PILImage.MAX_IMAGE_PIXELS
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    PILImage.MAX_IMAGE_PIXELS = None
    try:
        yield
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = prev_truncated
        PILImage.MAX_IMAGE_PIXELS = prev_max_pixels

def parse_element(element) -> {}:
    data = {}
    xdr_namespace = "{%s}" % SHEET_DRAWING_NS
    targets = level_order_traversal(element, xdr_namespace + "nvPicPr")
    for target in targets:
        cNvPr = embed = ""
        for child in target:
            if child.tag == xdr_namespace + "nvPicPr":
                cNvPr = child[0].attrib["name"]
            elif child.tag == xdr_namespace + "blipFill":
                _rel_embed = "{%s}embed" % REL_NS
                embed = child[0].attrib[_rel_embed]
        if cNvPr:
            data[cNvPr] = embed
    return data


def parse_element_sheet_xml(element) -> []:
    data = []
    xdr_namespace = "{%s}" % SHEET_MAIN_NS
    targets = level_order_traversal(element, xdr_namespace + "f")
    for target in targets:
        for child in target:
            if child.tag == xdr_namespace + "f":
                data.append(child.text)
    return data


def level_order_traversal(root, flag: str) -> []:
    queue = [root]
    targets = []
    while queue:
        node = queue.pop(0)
        children = [child.tag for child in node]
        if flag in children:
            targets.append(node)
            continue
        for child in node:
            queue.append(child)
    return targets


def handle_images(deps, archive: ZipFile) -> []:
    images = []
    if not PILImage:  # Pillow not installed, drop images
        return images
    for dep in deps:
        try:
            image_io = archive.read(dep.target)
            image = openpyxl_Image(BytesIO(image_io))
        except Exception as e:
            _log.error(f"Error reading image {dep.target}: {e}, {traceback.format_exc()}")
            continue
        image.embed = dep.id  # 文件rId
        image.target = dep.target  # 文件地址
        images.append(image)
    return images


def xlsx_embed_cells_images(buffer) -> {}:
    with _permissive_pil():
        return _xlsx_embed_cells_images_impl(buffer)


def _xlsx_embed_cells_images_impl(buffer) -> {}:
    archive = ZipFile(buffer)
    # 解析cellImage.xml文件
    deps = get_dependents(archive, get_rels_path("xl/cellimages.xml"))
    image_rel = handle_images(deps=deps, archive=archive)
    # 工作表及其中图片ID
    sheet_list = {}
    for item in archive.namelist():
        if not item.startswith('xl/worksheets/sheet'):
            continue
        key = item.split('/')[-1].split('.')[0].split('sheet')[-1]
        sheet_list[key] = parse_element_sheet_xml(fromstring(archive.read(item)))
    cell_images_xml = parse_element(fromstring(archive.read("xl/cellimages.xml")))
    cell_images_rel = {}
    for image in image_rel:
        cell_images_rel[image.embed] = image
    for cnv, embed in cell_images_xml.items():
        cell_images_xml[cnv] = cell_images_rel.get(embed)
    result = {}
    for key, img in cell_images_xml.items():
        all_cells = [
            cell
            for _sheet_id, sheet in sheet_list.items()
            if sheet is not None
            for cell in sheet or []
        ]

        image_excel_id_list = [
            cell for cell in all_cells
            if isinstance(cell, str) and key in cell
        ]
        if img is None:
            continue
        if len(image_excel_id_list) > 0:
            image_excel_id = image_excel_id_list[-1]
            f = archive.open(img.target)
            img_byte = io.BytesIO()
            im = PILImage.open(f).convert('RGB')
            im.save(img_byte, format='JPEG')
            image = ImageAsset(id=uuid.uuid7(), file_name=img.path, meta={'debug': False, 'content': img_byte.getvalue()})
            result['=' + image_excel_id] = image
    archive.close()
    return result
