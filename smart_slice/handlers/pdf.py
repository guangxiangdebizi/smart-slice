# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("pdf")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError, ResourceLimitError
from smart_slice.types import ImageAsset
from smart_slice import _uuid as uuid

import io
import os
import re
import tempfile
import time
import traceback
from contextlib import contextmanager
from typing import List

try:
    from PIL import Image
    from pypdf import PdfReader
    from pypdf.generic import Destination
    PDF_AVAILABLE = True
except ImportError:  # optional extra: smart-slice[pdf]
    Image = PdfReader = Destination = None
    PDF_AVAILABLE = False

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.chunker import SplitModel, smart_split_paragraph

default_pattern_list = [
    re.compile("(?<=^)# .*|(?<=\\n)# .*"),
    re.compile("(?<=\\n)(?<!#)## (?!#).*|(?<=^)(?<!#)## (?!#).*"),
    re.compile("(?<=\\n)(?<!#)### (?!#).*|(?<=^)(?<!#)### (?!#).*"),
    re.compile("(?<=\\n)(?<!#)#### (?!#).*|(?<=^)(?<!#)#### (?!#).*"),
    re.compile("(?<=\\n)(?<!#)##### (?!#).*|(?<=^)(?<!#)##### (?!#).*"),
    re.compile("(?<=\\n)(?<!#)###### (?!#).*|(?<=^)(?<!#)###### (?!#).*"),
    re.compile("(?<!\n)\n\n+"),
]


# Search window (in characters) for the fallback heading key.
# A numbered heading is specific enough to be located anywhere on the page; the
# numbering-stripped fallback key is usually a high-frequency body word, so it is
# only allowed to match near the top of the page. Letting it match deep in the
# body would hand the cut position over to ordinary prose.
HEADING_FALLBACK_WINDOW = 200


def check_links_in_pdf(doc):
    for page in doc.pages:
        if PdfSplitHandle.get_internal_links(doc, page):
            return True
    return False


def get_pdf_object(value):
    if hasattr(value, "get_object"):
        return value.get_object()
    return value


@contextmanager
def pdf_file_stream(file):
    open_reader = getattr(file, "open_reader", None)
    if callable(open_reader):
        with open_reader() as reader:
            yield reader
        return
    with tempfile.TemporaryFile(mode="w+b") as temporary_file:
        for chunk in file.chunks():
            temporary_file.write(chunk)
        temporary_file.seek(0)
        yield temporary_file


class PdfSplitHandle(BaseSplitHandle):
    def handle(
        self,
        file,
        pattern_list: List,
        with_filter: bool,
        limit: int,
        get_buffer,
        save_image,
        progress_hook=None,
    ):
        """progress_hook（2026-09-18 导入心跳接线）：可选无参回调，按页/按图调用，
        供导入侧刷新心跳——超大 PDF 逐页提取文本与图片的耗时可能达分钟级，
        仅靠"解析前后各一次心跳"余量不足。传 None 时行为与接线前完全一致。"""
        try:
            with pdf_file_stream(file) as pdf_file:
                pdf_document = PdfReader(pdf_file)
                if type(limit) is str:
                    limit = int(limit)
                if type(with_filter) is str:
                    with_filter = with_filter.lower() == "true"
                # 处理有目录的pdf
                toc_image_list = []
                result = self.handle_toc(pdf_document, limit, progress_hook=progress_hook,
                                         image_list=toc_image_list)
                if result is not None:
                    if len(toc_image_list) > 0:
                        save_image(toc_image_list)
                    return {"name": file.name, "content": result}

                # 没目录但是有链接的pdf
                link_image_list = []
                result = self.handle_links(pdf_document, pattern_list, with_filter, limit,
                                           progress_hook=progress_hook, image_list=link_image_list)
                if result is not None and len(result) > 0:
                    if len(link_image_list) > 0:
                        save_image(link_image_list)
                    return {"name": file.name, "content": result}

                # 没有目录的pdf
                image_list = []
                content = self.handle_pdf_content(file, pdf_document, image_list, progress_hook=progress_hook)
                if len(image_list) > 0:
                    save_image(image_list)

                # 无任何可提取内容（无文本且无图片）时明确报错，避免静默返回空内容导致前端"无预览"
                if not content.strip():
                    raise SliceError(
                        500,
                        _("No text or image content extracted from PDF file {name}, "
                          "the file may be encrypted or an image-only/scanned PDF").format(name=file.name),
                    )

                if pattern_list is not None and len(pattern_list) > 0:
                    split_model = SplitModel(pattern_list, with_filter, limit)
                else:
                    split_model = SplitModel(default_pattern_list, with_filter=with_filter, limit=limit)
        except (SliceError, ResourceLimitError):
            raise
        except BaseException as e:
            _log.error(f"File: {file.name}, error: {e}, {traceback.format_exc()}")
            raise SliceError(
                500,
                _("Failed to parse PDF file {name}: {reason}").format(name=file.name, reason=e),
            )
        return {"name": file.name, "content": split_model.parse(content)}

    BROWSER_SAFE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}

    @staticmethod
    def _to_browser_safe_image(data, file_name):
        """Convert image to browser-compatible format if needed.
        Returns (bytes, file_name) tuple."""
        ext = os.path.splitext(file_name)[1].lower()
        if ext in PdfSplitHandle.BROWSER_SAFE_EXTENSIONS:
            return data, file_name
        try:
            img = Image.open(io.BytesIO(data))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            new_name = os.path.splitext(file_name)[0] + ".png"
            return buf.getvalue(), new_name
        except BaseException as e:
            _log.error(f"Failed to convert image {file_name}: {e}")
            return data, file_name

    @staticmethod
    def extract_page_images(page, image_list):
        """Extract images from a PDF page, append File objects to image_list,
        and return markdown references."""
        md_parts = []
        try:
            images = page.images
        except BaseException:
            return ""
        for img in images:
            try:
                image_uuid = uuid.uuid7()
                raw_name = img.name or f"image_{image_uuid}"
                raw_name = raw_name.replace("[", "").replace("]", "")
                img_data, file_name = PdfSplitHandle._to_browser_safe_image(img.data, raw_name)
                if not any(f.id == image_uuid for f in image_list):
                    file_obj = ImageAsset(
                        id=image_uuid,
                        file_name=file_name,
                        meta={"debug": False, "content": img_data},
                    )
                    image_list.append(file_obj)
                md_parts.append(f"![{file_name}](./oss/file/{image_uuid})")
            except BaseException as e:
                _log.error(f"Error extracting PDF image: {e}")
        return "\n\n".join(md_parts)

    @staticmethod
    def handle_pdf_content(file, pdf_document, image_list=None, progress_hook=None):
        # 第一步:收集所有字体大小
        font_sizes = []
        page_lines = []
        for page in pdf_document.pages:
            if progress_hook is not None:
                progress_hook()  # 逐页提取文本行，按页上报进展
            lines = PdfSplitHandle.extract_page_lines(page)
            page_lines.append(lines)
            for line_text, font_size in lines:
                if line_text and font_size > 0:
                    font_sizes.append(font_size)

        # 计算正文字体大小(众数)
        if not font_sizes:
            body_font_size = 12
        else:
            from collections import Counter

            body_font_size = Counter(font_sizes).most_common(1)[0][0]

        # 第二步:提取内容
        content = ""
        for page_num, page in enumerate(pdf_document.pages):
            if progress_hook is not None:
                progress_hook()  # 逐页组装正文与页内图片，按页上报进展
            start_time = time.time()

            for text, font_size in page_lines[page_num]:
                if not text:
                    continue

                # 根据与正文字体的差值判断
                size_diff = font_size - body_font_size

                if size_diff > 2:  # 明显大于正文
                    content += f"## {text}\n\n"
                elif size_diff > 0.5:  # 略大于正文
                    content += f"### {text}\n\n"
                else:  # 正文
                    content += f"{text}\n"

            if image_list is not None:
                images_md = PdfSplitHandle.extract_page_images(page, image_list)
                if images_md:
                    content += images_md + "\n\n"
            else:
                for image_index in range(PdfSplitHandle.get_page_image_count(page)):
                    content += f"![image](image_{page_num}_{image_index})\n\n"

            content = content.replace("\0", "")

            elapsed_time = time.time() - start_time
            _log.debug(f"File: {file.name}, Page: {page_num + 1}, Time: {elapsed_time:.3f}s")

        return content

    @staticmethod
    def extract_page_lines(page):
        lines = []
        current_text = []
        current_sizes = []

        def flush_line():
            text = "".join(current_text).strip()
            if text:
                font_size = current_sizes[0] if current_sizes else 0
                lines.append((text, font_size))
            current_text.clear()
            current_sizes.clear()

        def visitor_text(text, cm, tm, font_dict, font_size):
            if text is None:
                return
            parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            for index, part in enumerate(parts):
                current_text.append(part)
                if part.strip() and font_size:
                    current_sizes.append(float(font_size))
                if index < len(parts) - 1:
                    flush_line()

        try:
            page.extract_text(visitor_text=visitor_text)
        except BaseException:
            text = PdfSplitHandle.extract_page_text(page)
            return [(line.strip(), 0) for line in text.splitlines() if line.strip()]
        flush_line()
        if lines:
            return lines

        text = page.extract_text() or ""
        return [(line.strip(), 0) for line in text.splitlines() if line.strip()]

    @staticmethod
    def get_page_image_count(page):
        try:
            return len(page.images)
        except BaseException:
            return 0

    @staticmethod
    def extract_page_text(page):
        return (page.extract_text() or "").replace("\0", "")

    @staticmethod
    def get_toc(doc):
        try:
            outline = doc.outline
        except BaseException:
            return []

        toc = []
        PdfSplitHandle.collect_toc(doc, outline, 1, toc)
        return toc

    @staticmethod
    def collect_toc(doc, outline, level, toc):
        for item in outline:
            if isinstance(item, list):
                PdfSplitHandle.collect_toc(doc, item, level + 1, toc)
                continue

            page_number = PdfSplitHandle.get_destination_page_number(doc, item)
            # BUGFIX 2026-10-10: a /Dest page number can be out of range (merged PDFs and
            # OCR-generated outlines both do this). Only None used to be skipped, so an
            # out-of-range number made doc.pages[page_number] raise IndexError, which the
            # caller turned into "failed to parse" - one bad bookmark lost the whole file.
            if page_number is None or not 0 <= page_number < len(doc.pages):
                continue

            title = getattr(item, "title", None)
            if title is None and hasattr(item, "get"):
                title = item.get("/Title")
            if title is None:
                title = str(item)
            toc.append((level, str(title).replace("\0", ""), page_number))

    @staticmethod
    def _locate_heading(text, candidates):
        """Locate a chapter heading in one page of text.

        @param candidates: search keys ordered by specificity (numbered heading first,
                           numbering-stripped heading as fallback); empty keys are skipped.
        @param text:       the page text **before** newline erasure.
        @return: (start, end) offsets of the heading, or (-1, -1) when not located.

        A hit is only accepted when it is anchored: at offset 0, or preceded by whitespace
        (space / tab / CR / LF / ideographic space). A heading occupies its own line in PDF
        text extraction, so anchoring holds for real headings, while the same word inside
        prose, a running header or a footer is normally glued to other characters and is
        rejected. Duplicating a heading line into the body is visible and harmless; cutting
        a whole page of body text is silent and unrecoverable, so the trade is deliberate.
        """
        for candidate in candidates:
            if not candidate:
                continue
            search_from = 0
            while True:
                idx = text.find(candidate, search_from)
                if idx < 0:
                    break
                if idx == 0 or text[idx - 1] in " \t\r\n\u3000":
                    return idx, idx + len(candidate)
                search_from = idx + 1
        return -1, -1

    @staticmethod
    def _toc_page_owners(toc, page_count):
        """Map each page to the outline entry that owns it: the last one covering it.

        A page is routinely covered by both a parent bookmark and its first child (a chapter
        opener carries the chapter heading and the first subsection heading on the same page).
        Uses the same interval rule as the handle_toc loop (end_page = next entry start - 1,
        last entry runs to the end of the document, clamped to start_page) so ownership and
        traversal cannot disagree.
        """
        owners = {}
        for i, (_level, _title, start_page) in enumerate(toc):
            end_page = toc[i + 1][2] - 1 if i + 1 < len(toc) else page_count - 1
            end_page = max(start_page, min(end_page, page_count - 1))
            for page_num in range(start_page, end_page + 1):
                owners[page_num] = i
        return owners

    @staticmethod
    def handle_toc(doc, limit, progress_hook=None, image_list=None):
        # 找到目录
        toc = PdfSplitHandle.get_toc(doc)
        if toc is None or len(toc) == 0:
            return None

        # 创建存储章节内容的数组
        chapters = []
        # BUGFIX 2026-10-10: collect_toc already resolves each bookmark level, but the
        # unpacked level was never used, so every outline entry collapsed to one weight and
        # a chunk title kept only its leaf heading (the ancestor chain was lost). Keep an
        # ancestor stack keyed on level and emit " ".join(chain), which matches the
        # " ".join(parent_chain) convention used by chunker.result_tree_to_paragraph.
        ancestors = []  # [(level, title)]
        # BUGFIX 2026-10-10: the bookmark path never extracted images, so any PDF carrying
        # an outline lost every illustration. imaged_pages makes sure a page is decoded once.
        imaged_pages = set()
        # BUGFIX 2026-10-10 (post-review): attaching a page image to the *first* entry that
        # covers the page put it in the parent bookmark's chunk, which after trimming often
        # holds nothing but its title, while the subsection that actually discusses the figure
        # got no image at all - and inject_image_ocr_text then wrote the OCR text into that
        # empty shell. Ownership now goes to the *last* entry covering the page (the deepest,
        # most specific one). Duplicate File rows are already collapsed downstream by sha256,
        # so this dedupe exists only to avoid decoding a page twice.
        page_owner = PdfSplitHandle._toc_page_owners(toc, len(doc.pages))

        # 遍历目录并按章节提取文本
        for i, entry in enumerate(toc):
            if progress_hook is not None:
                progress_hook()  # 逐章提取文本，按章上报进展（防超大 PDF 逐章解析被误判零进展）
            level, raw_title, start_page = entry
            while ancestors and ancestors[-1][0] >= level:
                ancestors.pop()
            ancestors.append((level, raw_title))
            chapter_title = " ".join(t for _lv, t in ancestors if t and t.strip())
            # 确定结束页码，如果是最后一个章节则到文档末尾
            if i + 1 < len(toc):
                end_page = toc[i + 1][2] - 1
            else:
                end_page = len(doc.pages) - 1
            end_page = max(start_page, end_page)

            # Heading search keys: the numbered original first (specific), the
            # numbering-stripped form only as a fallback.
            stripped_title = PdfSplitHandle.handle_chapter_title(raw_title)
            # BUGFIX 2026-10-10 (post-review): the tail cut uses the numbered original only.
            # The next heading is essentially always rendered with its number, whereas the
            # stripped short key also matches prose that merely mentions the next section
            # ("read the Commissioning section first"); a false tail hit discards the rest of
            # the page and no other chunk ever recovers it. Under-trimming only duplicates
            # the next section's text, which is visible and harmless.
            next_keys = (toc[i + 1][1],) if i + 1 < len(toc) else ()

            # 提取该章节的文本内容
            chapter_text = ""
            for page_num in range(start_page, end_page + 1):
                text = PdfSplitHandle.extract_page_text(doc.pages[page_num])

                # BUGFIX 2026-10-10 (post-review): trim the head on the chapter's first page
                # only, the tail on its last page only, require an anchored hit, and locate
                # before the newline erasure below. The previous code ran an unanchored
                # text.find(stripped_short_title) on *every* page of the interval, and once
                # numbering was stripped the key became an ordinary body word ("4 Requirements"
                # -> "Requirements"), so a hit inside a running header, a footer or plain prose
                # silently deleted the whole page (measured: a chapter kept 4.1% of its body).
                # The deleted text appeared in no chunk at all, with no log and no exception.
                if page_num == start_page:
                    _head_start, head_end = PdfSplitHandle._locate_heading(text, (raw_title,))
                    if head_end < 0 and stripped_title and stripped_title != raw_title:
                        _head_start, head_end = PdfSplitHandle._locate_heading(
                            text[:HEADING_FALLBACK_WINDOW], (stripped_title,)
                        )
                    if head_end > -1:
                        text = text[head_end:]
                if next_keys and page_num == end_page:
                    tail_start, _tail_end = PdfSplitHandle._locate_heading(text, next_keys)
                    if tail_start > -1:
                        text = text[:tail_start]

                text = re.sub(r"(?<!。)\n+", "", text)
                text = re.sub(r"(?<!.)\n+", "", text)

                chapter_text += text  # 提取文本

                # BUGFIX 2026-10-10: append this page's illustrations as markdown references.
                # Coverage is limited to the bookmarked page range - pages before the first
                # bookmark (cover, foreword) are not included; that loss pre-dates this change
                # and is tracked separately.
                if image_list is not None and page_owner.get(page_num) == i and page_num not in imaged_pages:
                    imaged_pages.add(page_num)
                    if progress_hook is not None:
                        progress_hook()  # image decode + PNG re-encode cost seconds per page
                    images_md = PdfSplitHandle.extract_page_images(doc.pages[page_num], image_list)
                    if images_md:
                        chapter_text += images_md + "\n\n"

            # Null characters are not allowed.
            chapter_text = chapter_text.replace("\0", "")
            # 限制标题长度
            real_chapter_title = chapter_title[:256]
            # 限制章节内容长度
            if 0 < limit < len(chapter_text):
                split_text = smart_split_paragraph(chapter_text, limit)
                for text in split_text:
                    chapters.append(
                        {"title": real_chapter_title, "content": text.encode("utf-8", "ignore").decode("utf-8")}
                    )
            else:
                chapters.append(
                    {
                        "title": real_chapter_title,
                        "content": (chapter_text if chapter_text else real_chapter_title)
                        .encode("utf-8", "ignore")
                        .decode("utf-8"),
                    }
                )
            # 保存章节内容和章节标题
        return chapters

    @staticmethod
    def handle_links(doc, pattern_list, with_filter, limit, progress_hook=None, image_list=None):
        # 检查文档是否包含内部链接
        if not check_links_in_pdf(doc):
            return
        # 创建存储章节内容的数组
        chapters = []
        # BUGFIX 2026-10-10: the internal-link path never extracted images either; see the
        # matching note in handle_toc. One set per call, keyed on the absolute page index.
        imaged_pages = set()
        toc_start_page = -1
        page_content = ""
        handle_pre_toc = True
        # 遍历 PDF 的每一页，查找带有目录链接的页
        for page_num, page in enumerate(doc.pages):
            if progress_hook is not None:
                progress_hook()  # 逐页扫描内部链接，按页上报进展
            links = PdfSplitHandle.get_internal_links(doc, page)
            # 如果目录开始页码未设置，则设置为当前页码
            if len(links) > 0 and toc_start_page < 0:
                toc_start_page = page_num
            if toc_start_page < 0:
                page_content += PdfSplitHandle.extract_page_text(page)
            # 检查该页是否包含内部链接（即指向文档内部的页面）
            for num in range(len(links)):
                link = links[num]
                # 获取链接目标的页面
                dest_page = link["page"]
                rect = link["from"]  # 获取链接的矩形区域
                # 如果目录开始页码包括前言部分，则不处理前言部分
                if dest_page < toc_start_page:
                    handle_pre_toc = False

                # 提取链接区域的文本作为标题
                link_title = PdfSplitHandle.extract_link_title(page, rect)
                if not link_title:
                    link_title = PdfSplitHandle.extract_first_line(doc.pages[dest_page])
                # 提取目标页面内容作为章节开始
                start_page = dest_page
                end_page = dest_page
                # 下一个link
                next_link = links[num + 1] if num + 1 < len(links) else None
                next_link_title = None
                if next_link is not None:
                    next_link_title = PdfSplitHandle.extract_link_title(page, next_link["from"])
                    if not next_link_title:
                        next_link_title = PdfSplitHandle.extract_first_line(doc.pages[next_link["page"]])
                    end_page = next_link["page"]

                # 提取章节内容
                chapter_text = ""
                last_page = min(end_page, len(doc.pages) - 1)
                for p_num in range(start_page, last_page + 1):
                    text = PdfSplitHandle.extract_page_text(doc.pages[p_num])

                    # BUGFIX 2026-10-10 (post-review): same discipline as handle_toc - head on
                    # the first page, tail on the last page, anchored, before newline erasure.
                    # Titles on this path come from extract_link_title / extract_first_line and
                    # their value is unchanged here (known defect: both delete every period, so
                    # "3.2 Debugging" becomes "32 Debugging"; tracked separately).
                    if p_num == start_page:
                        _head_start, head_end = PdfSplitHandle._locate_heading(text, (link_title,))
                        if head_end > -1:
                            text = text[head_end:]
                    if next_link_title is not None and p_num == last_page:
                        tail_start, _tail_end = PdfSplitHandle._locate_heading(text, (next_link_title,))
                        if tail_start > -1:
                            text = text[:tail_start]

                    text = re.sub(r"(?<!。)\n+", "", text)
                    text = re.sub(r"(?<!.)\n+", "", text)
                    chapter_text += text

                    # BUGFIX 2026-10-10: recover illustrations on the internal-link path.
                    if image_list is not None and p_num not in imaged_pages:
                        imaged_pages.add(p_num)
                        if progress_hook is not None:
                            progress_hook()  # image decode costs seconds per page
                        images_md = PdfSplitHandle.extract_page_images(doc.pages[p_num], image_list)
                        if images_md:
                            chapter_text += images_md + "\n\n"

                # Null characters are not allowed.
                chapter_text = chapter_text.replace("\0", "")

                # 限制章节内容长度
                if 0 < limit < len(chapter_text):
                    split_text = smart_split_paragraph(chapter_text, limit)
                    for text in split_text:
                        chapters.append({"title": link_title, "content": text})
                else:
                    # 保存章节信息
                    chapters.append({"title": link_title, "content": chapter_text})

        # 目录中没有前言部分，手动处理
        if handle_pre_toc:
            pre_toc = []
            lines = page_content.strip().split("\n")
            try:
                for line in lines:
                    if re.match(r"^前\s*言", line):
                        pre_toc.append({"title": line, "content": ""})
                    else:
                        pre_toc[-1]["content"] += line
                for i in range(len(pre_toc)):
                    pre_toc[i]["content"] = re.sub(r"(?<!。)\n+", "", pre_toc[i]["content"])
                    pre_toc[i]["content"] = re.sub(r"(?<!.)\n+", "", pre_toc[i]["content"])
            except BaseException as e:
                _log.error(_("This document has no preface and is treated as ordinary text: {e}").format(e=e))
                if pattern_list is not None and len(pattern_list) > 0:
                    split_model = SplitModel(pattern_list, with_filter, limit)
                else:
                    split_model = SplitModel(default_pattern_list, with_filter=with_filter, limit=limit)
                # 插入目录前的部分
                page_content = re.sub(r"(?<!。)\n+", "", page_content)
                page_content = re.sub(r"(?<!.)\n+", "", page_content)
                page_content = page_content.strip()
                pre_toc = split_model.parse(page_content)
            chapters = pre_toc + chapters
        return chapters

    @staticmethod
    def get_internal_links(doc, page):
        links = []
        annotations = getattr(page, "annotations", None) or []
        for annotation in annotations:
            annotation = get_pdf_object(annotation)
            if not hasattr(annotation, "get"):
                continue
            if annotation.get("/Subtype") != "/Link":
                continue
            dest_page = PdfSplitHandle.get_annotation_destination_page_number(doc, annotation)
            if dest_page is None or dest_page < 0 or dest_page >= len(doc.pages):
                continue
            rect = annotation.get("/Rect")
            links.append({"page": dest_page, "from": PdfSplitHandle.normalize_rect(rect)})
        return links

    @staticmethod
    def get_annotation_destination_page_number(doc, annotation):
        destination = annotation.get("/Dest")
        if destination is None:
            action = get_pdf_object(annotation.get("/A"))
            if hasattr(action, "get") and action.get("/S") == "/GoTo":
                destination = action.get("/D")
        return PdfSplitHandle.get_destination_page_number(doc, destination)

    @staticmethod
    def get_destination_page_number(doc, destination):
        if destination is None:
            return None

        destination = get_pdf_object(destination)

        if isinstance(destination, bytes):
            destination = destination.decode(errors="ignore")

        if isinstance(destination, str):
            destination = doc.named_destinations.get(destination)
            if destination is None:
                return None

        if isinstance(destination, Destination):
            try:
                page_number = doc.get_destination_page_number(destination)
                return page_number if page_number >= 0 else None
            except BaseException:
                return None

        if isinstance(destination, (list, tuple)) and len(destination) > 0:
            return PdfSplitHandle.get_page_number_by_reference(doc, destination[0])

        if hasattr(destination, "get") and destination.get("/D") is not None:
            return PdfSplitHandle.get_destination_page_number(doc, destination.get("/D"))

        return None

    @staticmethod
    def get_page_number_by_reference(doc, page_reference):
        try:
            page_number = int(page_reference)
            if 0 <= page_number < len(doc.pages):
                return page_number
        except BaseException:
            pass

        try:
            page = get_pdf_object(page_reference)
            page_number = doc.get_page_number(page)
            return page_number if page_number >= 0 else None
        except BaseException:
            return None

    @staticmethod
    def normalize_rect(rect):
        if rect is None or len(rect) < 4:
            return None
        left, bottom, right, top = [float(value) for value in rect[:4]]
        return min(left, right), min(bottom, top), max(left, right), max(bottom, top)

    @staticmethod
    def extract_link_title(page, rect):
        if rect is None:
            return ""

        left, bottom, right, top = rect
        tolerance = 2
        text_parts = []

        def visitor_text(text, cm, tm, font_dict, font_size):
            if not text:
                return
            x = tm[4] if len(tm) > 4 else 0
            y = tm[5] if len(tm) > 5 else 0
            text_top = y + (float(font_size) if font_size else 0)
            in_horizontal_range = left - tolerance <= x <= right + tolerance
            in_vertical_range = (
                bottom - tolerance <= y <= top + tolerance or bottom - tolerance <= text_top <= top + tolerance
            )
            if in_horizontal_range and in_vertical_range:
                text_parts.append(text)

        try:
            page.extract_text(visitor_text=visitor_text)
        except BaseException:
            return ""

        return "".join(text_parts).replace("\0", "").strip().split("\n")[0].replace(".", "").strip()

    @staticmethod
    def extract_first_line(page):
        text = PdfSplitHandle.extract_page_text(page).strip()
        return text.split("\n")[0].replace(".", "").strip() if text else ""

    @staticmethod
    def handle_chapter_title(title):
        title = title.replace("\0", "")
        title = re.sub(r"[一二三四五六七八九十\s*]、\s*", "", title)
        title = re.sub(r"第[一二三四五六七八九十]章\s*", "", title)
        # BUGFIX 2026-10-10: only CJK-style numbering was stripped; Arabic multi-level
        # numbering ("3.2.1 Creating a datum plane") survived and the fallback search key
        # missed the page text.
        # TIGHTENED 2026-10-10 (post-review): strip *numbering*, never "a title that happens
        # to start with a digit". The first version made the trailing separator and the
        # whitespace optional, which rewrote "1.5 m clearance" / "5G network" / "3M tape" /
        # "2024 annual report" / "Chapter 2 3D printing" into "m clearance" / "G network" /
        # "M tape" / "annual report" / "D printing" - keys that locate nothing and are
        # themselves crippled. See _strip_arabic_numbering for the rule.
        return PdfSplitHandle._strip_arabic_numbering(title)

    @staticmethod
    def _strip_arabic_numbering(title):
        """Strip Arabic numbering; return the title unchanged when the shape is ambiguous.

        Stripped: multi-level numbering followed by whitespace or end of string
                  ("3.2.1 Creating a datum plane", "2-1 Assembly drawing",
                  "2026.10.10 Change log"), and single-level numbering carrying an explicit
                  trailing separator ("10. Precautions", "1)Installation", "3、Maintenance",
                  "1.Overview").
        Kept:     bare integer + whitespace ("4 Requirements", "01 Overview") - indistinguishable
                  from quantity phrases ("Station 8", "12 inch wafer", "100 mL flask"); and
                  "1.5 m clearance", "3.14 pi", "5G network", "3M tape", "2024 annual report",
                  "ISO 9001 requirements".

        Rationale: the result is only ever a *fallback* search key (the numbered original is
        tried first, see _locate_heading). Not stripping merely leaves the heading text inside
        the body, which is visible and harmless; over-stripping can degrade the fallback key
        into a high-frequency body word, which - if the anchoring constraint ever fails -
        silently deletes a whole page.
        """
        matched = re.match(r"\s*\d+(?:[.\-．]\d+)*[.\-．、)）]?", title)
        if not matched:
            return title
        numbering = matched.group(0).strip()
        rest = title[matched.end() :]
        if numbering[-1] in ".-．、)）":
            return rest.strip()
        if re.search(r"[.\-．]\d", numbering) and (not rest or rest[0].isspace()):
            return rest.strip()
        return title

    def support(self, file, get_buffer):
        # pypdf/Pillow 为可选 extra（smart-slice[pdf]）：缺失时不认领，
        # 由公共层报 400 不支持，而不是在 import 阶段炸掉整个包
        if not PDF_AVAILABLE:
            return False
        file_name: str = file.name.lower()
        if file_name.endswith(".pdf") or file_name.endswith(".PDF"):
            return True
        return False

    def get_content(self, file, save_image):
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            # 将上传的文件保存到临时文件中
            temp_file.write(file.read())
            # 获取临时文件的路径
            temp_file_path = temp_file.name

        try:
            with open(temp_file_path, "rb") as pdf_file:
                pdf_document = PdfReader(pdf_file)
                image_list = []
                content = self.handle_pdf_content(file, pdf_document, image_list)
                if len(image_list) > 0:
                    save_image(image_list)
                return content
        except BaseException as e:
            traceback.print_exception(e)
            return f"{e}"
        finally:
            os.remove(temp_file_path)
