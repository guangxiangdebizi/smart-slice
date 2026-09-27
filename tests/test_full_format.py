# coding=utf-8
"""全格式适配（Phase 2-A）单测：新增格式 handler + 兜底拦截语义（全离线构造，不依赖 DB/网络/凭据）。

覆盖（2026-09-17 Phase 2-A，分支 feat/split-full-format-support）：
  1. 新支持格式：pptx/ppt(改名 OOXML+OLE 文本原子)/wps(zip 容器)/rtf/odt/ods/odp/
     epub/eml/tar/7z/mobi 按双链路参数（normalize=False/True）调 split_document，
     断言产出非空段落且内容含样例文字；
  2. 明确拒绝格式：老二进制 wps/rar/key/pages/numbers/媒体扩展名 断言抛 400；
  3. 兜底拦截：上传链路（normalize=False）无 handler 命中时抛 400 而非乱码段落；
     .log/.json 纯文本探测放行不受影响；
  4. 图片别名：.tif 段落生成；.heic 依 pillow-heif 可用性条件断言。

运行：
  python -m pytest tests/test_full_format.py -v
"""
import email.message
import io
import os
import struct
import tarfile
import unittest
import zipfile


from smart_slice.exceptions import SliceError
from smart_slice.service import split_document

PPTX_SVC_TITLE = "季度产品汇报标题"
PPTX_SVC_BODY = "第一页要点内容文本"
RTF_SAMPLE_TEXT = "Hello RTF World Second line"
ODT_HEADING = "ODF一级标题"
ODT_PARA = "ODF段落内容文本"
EPUB_CH1 = "第一章 电子书标题"
EPUB_CH1_BODY = "第一章正文内容文本"
EML_SUBJECT = "季度会议纪要"
EML_BODY = "这是邮件正文内容文本"
MOBI_TEXT = "mobi 正文内容用于验证解析"
TAR_INNER = "tar 内层正文内容"
SEVENZ_INNER = "7z 内层正文内容"


def _noop_save_image(image_list):
    return None


def _split_upload(name, content, **kwargs):
    return split_document(name, content, limit=4096, pattern_list=None, with_filter=None,
                          normalize=False, save_image=_noop_save_image, **kwargs)


def _split_bulk_import(name, content, **kwargs):
    return split_document(name, content, limit=1000, pattern_list=None, with_filter=True,
                          normalize=True, **kwargs)


def _all_text(result):
    """提取 handler 返回结构中的全部文本（dict/list 两类结构）。

    组级 title 必须收集：normalize=True 展平结构的组即段落 dict（{title, content}），
    段落标题挂在组的 title 键上（SplitModel 标题树经 parent_chain 装配）；该层漏收
    会让"标题/主题/章节名是否进入产出"的断言在批量导入链路全部失真（2026-09-18 修复，
    修复前 pptx/eml/epub/odt 的批量导入链路误报标题丢失——标题实际一直在产出中，
    仅断言收集面缺角，handler 实现无缺陷）。"""
    groups = result if isinstance(result, list) else [result]
    texts = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        texts.append(str(group.get("title") or ""))
        content = group.get("content")
        items = content if isinstance(content, list) else [content]
        for item in items:
            if isinstance(item, dict):
                texts.append(str(item.get("title") or ""))
                texts.append(str(item.get("content") or ""))
            elif item is not None:
                texts.append(str(item))
    return "\n".join(texts)


def pptx_bytes():
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = PPTX_SVC_TITLE
    slide.placeholders[1].text = PPTX_SVC_BODY
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def rtf_bytes():
    # 最小合法 RTF：{\rtf 头 + 纯文本段落
    return (r"{\rtf1\ansi " + RTF_SAMPLE_TEXT + r"\par second line\par}").encode("latin-1")


def odf_bytes(ext="odt"):
    content_xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">
<office:body><office:text>
<text:h text:outline-level="1">{ODT_HEADING}</text:h>
<text:p>{ODT_PARA}</text:p>
<table:table><table:table-row><table:table-cell><text:p>A1</text:p></table:table-cell><table:table-cell><text:p>B1</text:p></table:table-cell></table:table-row></table:table>
</office:text></office:body></office:document-content>'''
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml", content_xml)
    return buf.getvalue()


def epub_bytes(container_ns="urn:oasis:names:tc:opendocument:xmlns:container:1.0"):
    # 缺口覆盖（2026-09-18）：既有用例用的是非标 ":1.0" 命名空间变体，未覆盖真实
    # 电子书普遍使用的标准 OCF 命名空间（无 ":1.0" 后缀）；参数化两版均验证。
    container = ('<?xml version="1.0"?><container version="1.0" '
                 f'xmlns="{container_ns}"><rootfiles>'
                 '<rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/></rootfiles></container>')
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
           '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
           '<spine><itemref idref="c1"/></spine></package>')
    ch1 = f'<html><body><h1>{EPUB_CH1}</h1><p>{EPUB_CH1_BODY}</p></body></html>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/ch1.xhtml", ch1)
    return buf.getvalue()


def eml_bytes():
    msg = email.message.EmailMessage()
    msg["Subject"] = EML_SUBJECT
    msg["From"] = "sender@example.com"
    msg["To"] = "receiver@example.com"
    msg.set_content(EML_BODY)
    return msg.as_bytes()


def tar_gz_bytes():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = f"# 内层标题\n{TAR_INNER}".encode("utf-8")
        info = tarfile.TarInfo("docs/inner.md")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def sevenz_bytes():
    import tempfile

    import py7zr

    path = tempfile.mktemp(suffix=".7z")
    try:
        with py7zr.SevenZipFile(path, "w") as archive:
            archive.writestr(f"# 内层标题\n{SEVENZ_INNER}".encode("utf-8"), "inner.md")
        with open(path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(path):
            os.remove(path)


def mobi_bytes():
    """构造最小合法 MOBI6（MOBI header 0xE4 + EXTH Title/Author + 无压缩文本记录）。

    布局要点：PalmDOC 头 16 字节 → 'MOBI'（记录内偏移 16）→ header length 228
    （0xE4，避免 MobiHeader 读取 0xF4 处 ncxidx）→ EXTH（Title=100/Author=101，
    exth_flag 0x40）→ 全名追加在记录尾部（0x54/0x58 指向）。

    正文记录须以 ≥12 字节的 ASCII HTML 标记开头（KindleUnpack 兼容结构，
    2026-09-18 修复）：mobi 库 mobi_html.insertHREFS 无条件把 charset meta 标签
    拼接在 srctext[0:12] 与 srctext[12:] 之间，真实 MOBI 书的正文记录以 <html>
    标记开头故该偏移安全；若正文直接以 UTF-8 汉字开头，12 字节截点会切在多字节
    字符中间，产生 REPLACEMENT CHARACTER 乱码。样例以 <html><body>（恰好 12 字节）
    作为正文记录开头，文本置于其后。
    """
    title, author = "测试书名", "测试作者"
    text_bytes = f"<html><body>{MOBI_TEXT}</body></html>".encode("utf-8")
    MOBI_HL = 228

    def exth_record(rtype, data):
        rec = struct.pack(">II", rtype, 8 + len(data)) + data
        return rec + bytes((4 - len(rec) % 4) % 4)

    exth_body = exth_record(100, title.encode("utf-8")) + exth_record(101, author.encode("utf-8"))
    exth = b"EXTH" + struct.pack(">II", 12 + len(exth_body), 2) + exth_body

    mobi = b"MOBI" + struct.pack(">IIIII", MOBI_HL, 2, 65001, 0, 0)
    mobi += struct.pack(">III", 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF)
    mobi += bytes(28)
    mobi += struct.pack(">III", 0xFFFFFFFF, 0, 0)      # 0x50 firstnontext / 0x54 toff / 0x58 tlen
    mobi += bytes(16)
    mobi += struct.pack(">I", 0xFFFFFFFF)              # 0x6C firstresource
    mobi += bytes(16)
    mobi += struct.pack(">I", 0x40)                    # 0x80 exth flag
    mobi += bytes(MOBI_HL - len(mobi))
    assert len(mobi) == MOBI_HL

    header = struct.pack(">HHIHHHH", 1, 0, len(text_bytes), 1, 4096, 0, 0) + mobi + exth
    rec0 = bytearray(header)
    struct.pack_into(">I", rec0, 0x54, len(header))
    struct.pack_into(">I", rec0, 0x58, len(title.encode("utf-8")))
    rec0 += title.encode("utf-8")

    recs = [bytes(rec0), text_bytes]
    n = len(recs)
    hdr = b"TestBook".ljust(32, bytes(1)) + struct.pack(">HH", 0, 0)
    hdr += struct.pack(">IIII", 0, 0, 0, 0)
    hdr += struct.pack(">II", 0, 0)
    hdr += b"BOOK" + b"MOBI"
    hdr += struct.pack(">II", n, 0)
    hdr += struct.pack(">H", n)                        # numrecords @76
    offset = 78 + 8 * n + 6
    entries = b""
    for r in recs:
        entries += struct.pack(">I", offset) + bytes(4)
        offset += len(r)
    return hdr + entries + bytes(2) + struct.pack(">I", n) + b"".join(recs)


def et_bytes():
    import openpyxl

    wb = openpyxl.Workbook()
    # 表样须含数据行：既有 XlsxSplitHandle.handle_sheet 把首行固定当表头、仅在有
    # 数据行时才产出段落（单行表头的空表产出为空，属既有 handler 行为、不在本
    # 分支改动范围），故 .et 转交链路的验证样例至少需要一行数据（2026-09-18 修复）。
    wb.active["A1"] = "WPS表格内容"
    wb.active["A2"] = "数据行内容"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class NewFormatDualLinkTests(unittest.TestCase):
    """新支持格式：双链路参数均产出非空段落且含样例文字。"""

    def test_pptx(self):
        content = pptx_bytes()
        for split in (_split_upload, _split_bulk_import):
            result = split("演示.pptx", content)
            text = _all_text(result)
            self.assertIn(PPTX_SVC_TITLE, text)
            self.assertIn(PPTX_SVC_BODY, text)

    def test_pptx_renamed_as_ppt_sniffs_zip(self):
        # 改名 OOXML 的 .ppt：zip magic sniff 转交 pptx 语义
        result = _split_upload("老演示.ppt", pptx_bytes())
        self.assertIn(PPTX_SVC_TITLE, _all_text(result))

    def test_ppt_ole_text_atoms(self):
        # 真 OLE 二进制 .ppt 的文本原子提取（记录树遍历，单元级验证）
        from smart_slice.handlers.ppt import _walk_text_atoms

        data = struct.pack("<HHI", 0x000F, 0x03E8, 500)
        t1 = "幻灯片一文本".encode("utf-16-le")
        data += struct.pack("<HHI", 0x0020, 0x0FA0, len(t1)) + t1
        t2 = b"ANSI body text"
        data += struct.pack("<HHI", 0x0020, 0x0FA8, len(t2)) + t2
        atoms = []
        _walk_text_atoms(data, 0, len(data), atoms)
        self.assertIn("幻灯片一文本", atoms)
        self.assertIn("ANSI body text", atoms)

    def test_rtf(self):
        for split in (_split_upload, _split_bulk_import):
            result = split("说明.rtf", rtf_bytes())
            self.assertIn(RTF_SAMPLE_TEXT, _all_text(result))

    def test_odt(self):
        for split in (_split_upload, _split_bulk_import):
            result = split("文档.odt", odf_bytes("odt"))
            text = _all_text(result)
            self.assertIn(ODT_HEADING, text)
            self.assertIn(ODT_PARA, text)
            self.assertIn("A1", text)

    def test_epub(self):
        for split in (_split_upload, _split_bulk_import):
            result = split("电子书.epub", epub_bytes())
            text = _all_text(result)
            self.assertIn(EPUB_CH1, text)
            self.assertIn(EPUB_CH1_BODY, text)

    def test_epub_standard_ocf_namespace(self):
        # 缺陷 #34 回归（2026-09-18）：标准 OCF 命名空间（无 ":1.0" 后缀）的
        # container.xml 此前因命名空间硬编码解析必然失败 500
        for split in (_split_upload, _split_bulk_import):
            result = split("电子书.epub", epub_bytes(
                container_ns="urn:oasis:names:tc:opendocument:xmlns:container"))
            text = _all_text(result)
            self.assertIn(EPUB_CH1, text)
            self.assertIn(EPUB_CH1_BODY, text)

    def test_epub_no_namespace_container(self):
        # 同类边界：container.xml 无命名空间时同样按 local-name 命中 rootfile
        for split in (_split_upload, _split_bulk_import):
            result = split("电子书.epub", epub_bytes(container_ns=""))
            text = _all_text(result)
            self.assertIn(EPUB_CH1, text)

    def test_eml(self):
        for split in (_split_upload, _split_bulk_import):
            result = split("邮件.eml", eml_bytes())
            text = _all_text(result)
            self.assertIn(EML_SUBJECT, text)
            self.assertIn(EML_BODY, text)

    def test_tar_gz(self):
        for split in (_split_upload, _split_bulk_import):
            result = split("打包.tar.gz", tar_gz_bytes())
            self.assertIn(TAR_INNER, _all_text(result))

    def test_sevenz(self):
        for split in (_split_upload, _split_bulk_import):
            result = split("打包.7z", sevenz_bytes())
            self.assertIn(SEVENZ_INNER, _all_text(result))

    def test_mobi(self):
        for split in (_split_upload, _split_bulk_import):
            result = split("电子书.mobi", mobi_bytes())
            self.assertIn(MOBI_TEXT, _all_text(result))

    def test_et_zip_container(self):
        # .et 新格式（OOXML 兼容 zip）转交 xlsx 语义
        for split in (_split_upload, _split_bulk_import):
            result = split("表格.et", et_bytes())
            text = _all_text(result)
            self.assertIn("WPS表格内容", text)
            self.assertIn("数据行内容", text)

    def test_new_format_inside_zip(self):
        # zip 内嵌新格式（.pptx）：内层子清单补齐后自动生效
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("内层.pptx", pptx_bytes())
        result = _split_upload("打包.zip", buf.getvalue())
        self.assertIn(PPTX_SVC_TITLE, _all_text(result))


class RejectedFormatTests(unittest.TestCase):
    """明确拒绝格式：两条链路均抛 400（SliceError）。"""

    def _assert_rejected(self, name, content):
        for split in (_split_upload, _split_bulk_import):
            with self.assertRaises(SliceError) as ctx:
                split(name, content)
            # assert the business code via .code: SliceError.__init__ sets only
            # code/message, and split_document raises SliceError(400, ...) with 400 on
            # .code (SliceError has no .status_code attribute).
            self.assertEqual(ctx.exception.code, 400)

    def test_old_binary_wps_rejected(self):
        # 老版本 WPS 私有二进制（OLE 魔数，非 zip 容器）——评估结论：无纯 Python 解析方案
        self._assert_rejected("老文档.wps", b"\xd0\xcf\x11\xe0wps-binary")

    def test_old_binary_et_rejected(self):
        self._assert_rejected("老表格.et", b"\xd0\xcf\x11\xe0et-binary")

    def test_rar_rejected(self):
        # rarfile 依赖 unrar/unar 系统二进制，违反纯 wheel 约束，归不支持
        self._assert_rejected("压缩包.rar", b"Rar!\x1a\x07\x01\x0fake-rar")

    def test_iwork_rejected(self):
        # Apple iWork：.key/.pages 为 IWA/Snappy+protobuf 无纯 Python 解析方案；
        # .numbers 的 numbers-parser 依赖 python-snappy C 扩展，同判不支持
        for name in ("演示.key", "文档.pages", "表格.numbers"):
            self._assert_rejected(name, b"PK\x03\x04fake-iwa-zip-bytes")

    def test_media_rejected_on_upload_link(self):
        # 兜底拦截：上传链路（normalize=False）对媒体扩展名同样抛 400（不再乱码兜底）
        self._assert_rejected("视频.mp4", b"\x00\x00\x00\x18ftypmp42")
        self._assert_rejected("音频.mp3", b"ID3\x03fake-mp3")


class FallbackInterceptionTests(unittest.TestCase):
    """兜底拦截语义：无 handler 命中 → 400；纯文本探测放行不受影响。"""

    def test_unknown_extension_of_binary_raises_400(self):
        # .pptx 字节改名 .xyz：无 handler 命中，抛 400 而非乱码段落
        with self.assertRaises(SliceError) as ctx:
            _split_upload("附件.xyz", pptx_bytes())
        # business code is asserted via .code (see _assert_rejected)
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn(".xyz", ctx.exception.message)

    def test_rejection_message_contains_extension(self):
        with self.assertRaises(SliceError) as ctx:
            _split_upload("视频.mkv", b"\x1a\x45\xdf\xa3fake-mkv")
        self.assertIn(".mkv", ctx.exception.message)

    def test_log_still_supported(self):
        content = "2026-09-17 10:00:00 INFO 服务启动完成\n2026-09-17 10:00:01 ERROR 发生错误".encode("utf-8")
        result = _split_upload("运行日志.log", content)
        self.assertIn("服务启动完成", _all_text(result))

    def test_json_still_supported(self):
        content = '{"名称": "打印机", "数量": 10}'.encode("utf-8")
        result = _split_upload("配置.json", content)
        self.assertIn("打印机", _all_text(result))

    def test_plain_txt_both_links(self):
        content = "纯文本段落一。\n\n纯文本段落二。".encode("utf-8")
        self.assertIn("纯文本段落一", _all_text(_split_upload("说明.txt", content)))
        self.assertIn("纯文本段落二", _all_text(_split_bulk_import("说明.txt", content)))


class ImageAliasTests(unittest.TestCase):
    """图片别名：.tif 始终支持；.heic/.heif 依 pillow-heif 可用性条件断言。"""

    def test_tif_supported(self):
        import PIL.Image

        buf = io.BytesIO()
        PIL.Image.new("RGB", (4, 4), color=(255, 0, 0)).save(buf, format="TIFF")
        result = _split_upload("扫描件.tif", buf.getvalue())
        rows = result["content"] if isinstance(result, dict) else result[0]["content"]
        self.assertEqual(len(rows), 1)  # 无 OCR 时降级文件名占位，段落仍非空

    def test_heic_conditional(self):
        from smart_slice.handlers.image import HEIF_SUPPORTED

        if not HEIF_SUPPORTED:
            self.skipTest("pillow-heif 不可用，.heic 降级为不支持")
        import PIL.Image

        buf = io.BytesIO()
        PIL.Image.new("RGB", (4, 4), color=(0, 255, 0)).save(buf, format="HEIF")
        result = _split_upload("照片.heic", buf.getvalue())
        rows = result["content"] if isinstance(result, dict) else result[0]["content"]
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
