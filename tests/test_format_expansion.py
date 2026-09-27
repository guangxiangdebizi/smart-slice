# coding=utf-8
"""Phase 3（2026-09-27）格式扩展单测：新增 handler + 可选依赖降级 + 保真度。

全离线构造（内存/临时目录），不依赖 DB/网络/凭据。覆盖：
  1. 新格式产出：svg/ipynb/srt/vtt/ass/fb2(+fb2.zip)/mbox/ics/vcf/扩展位图；
  2. 可选依赖缺失时 import 不崩、格式降级 400（裸环境语义）；
  3. 保真度：源码扩展名（.java 等）里的 `#` 行不再被误抽成 title；
  4. OOXML 变体（.ppsx/.potx/.pptm/.dotx/.xltx）声明与认领一致；
  5. 注册表完整性：新 handler 在 SPLIT_HANDLERS 内且 TextSplitHandle 末位兜底。

运行：python -m pytest tests/test_format_expansion.py -v
"""
import base64
import io
import sys
import unittest
import zipfile

import smart_slice as ss
from smart_slice.exceptions import SliceError
from smart_slice.handlers import SPLIT_HANDLERS
from smart_slice.handlers.text import LITERAL_EXTENSIONS, TEXT_EXTENSIONS
from smart_slice.service import split_document


def _noop_save_image(image_list):
    return None


def _split(name, content, **kwargs):
    return split_document(name, content, limit=1000, pattern_list=None, with_filter=True,
                          normalize=True, save_image=_noop_save_image, **kwargs)


def _all_text(result):
    groups = result if isinstance(result, list) else [result]
    out = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        out.append(str(g.get("title") or ""))
        items = g.get("content") if isinstance(g.get("content"), list) else [g.get("content")]
        for it in items:
            if isinstance(it, dict):
                out.append(str(it.get("title") or ""))
                out.append(str(it.get("content") or ""))
            elif it is not None:
                # normalize=True 时每组即段落 dict，content 是字符串
                out.append(str(it))
    return "\n".join(out)


FB2 = ('<?xml version="1.0"?><FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
       '<body><section><title><p>Chapter One</p></title><p>fb2 body paragraph</p></section>'
       '</body></FictionBook>').encode()


def _zip(member_name, payload):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(member_name, payload)
    return b.getvalue()


class SvgTests(unittest.TestCase):
    SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><text>Hello SVG</text><tspan>tail</tspan></svg>'

    def test_svg_text_extracted(self):
        rows = _split("note.svg", self.SVG)
        self.assertTrue(rows)
        text = _all_text(rows)
        self.assertIn("Hello SVG", text)
        self.assertIn("tail", text)

    def test_svg_drops_script_and_style(self):
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script>'
               b'<style>.a{}</style><text>visible</text></svg>')
        text = _all_text(_split("n.svg", svg))
        self.assertIn("visible", text)
        self.assertNotIn("alert", text)

    def test_svg_detected(self):
        self.assertEqual(ss.detect_handler(name="n.svg", content=self.SVG), "SvgSplitHandle")


class IpynbTests(unittest.TestCase):
    NB = (b'{"cells":[{"cell_type":"markdown","source":["# Title\\n\\nintro text"]},'
          b'{"cell_type":"code","source":["x = 1\\n# a comment"]}],'
          b'"metadata":{"title":"My Notebook"}}')

    def test_ipynb_markdown_and_code(self):
        text = _all_text(_split("nb.ipynb", self.NB))
        self.assertIn("intro text", text)
        self.assertIn("x = 1", text)

    def test_ipynb_nonstandard_source_list_not_glued(self):
        # 手写 notebook：source 是无行尾换行的 list，规范拼接会粘成 "# Hintro"，
        # 启发式兜底应改用换行连接，保证标题与正文不粘连
        import json
        nb = json.dumps({"cells": [
            {"cell_type": "markdown", "source": ["# Heading", "body line"]},
        ]}).encode()
        text = _all_text(_split("nb2.ipynb", nb))
        self.assertIn("Heading", text)
        self.assertIn("body line", text)
        self.assertNotIn("Headingbody", text)

    def test_ipynb_code_comment_not_a_title(self):
        # 代码 cell 包进围栏，# 注释不被当标题
        rows = _split("nb.ipynb", self.NB)
        titles = " ".join(r.get("title") or "" for r in rows)
        self.assertNotIn("a comment", titles)


class SubtitleTests(unittest.TestCase):
    SRT = b"1\n00:00:01,000 --> 00:00:02,000\nfirst cue\n\n2\n00:00:03,000 --> 00:00:04,000\nsecond cue\n"
    VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nvtt cue one\n"
    ASS = (b"[Script Info]\nTitle: meta\n\n[Events]\n"
           b"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
           b"Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,ass dialogue line\n")

    def test_srt_drops_timecodes(self):
        text = _all_text(_split("s.srt", self.SRT))
        self.assertIn("first cue", text)
        self.assertIn("second cue", text)
        self.assertNotIn("-->", text)
        self.assertNotIn("00:00:01", text)

    def test_vtt_drops_header(self):
        text = _all_text(_split("s.vtt", self.VTT))
        self.assertIn("vtt cue one", text)
        self.assertNotIn("WEBVTT", text)

    def test_ass_only_dialogue(self):
        text = _all_text(_split("s.ass", self.ASS))
        self.assertIn("ass dialogue line", text)
        self.assertNotIn("Script Info", text)
        self.assertNotIn("Format:", text)
        self.assertNotIn("meta", text)


class Fb2Tests(unittest.TestCase):
    def test_fb2_plain(self):
        text = _all_text(_split("book.fb2", FB2))
        self.assertIn("fb2 body paragraph", text)
        self.assertIn("Chapter One", text)

    def test_fb2_zip_container(self):
        rows = _split("book.fb2.zip", _zip("book.fb2", FB2))
        self.assertIn("fb2 body paragraph", _all_text(rows))
        self.assertEqual(ss.detect_handler(name="b.fb2.zip", content=_zip("book.fb2", FB2)),
                         "Fb2SplitHandle")


class MboxTests(unittest.TestCase):
    MBOX = (b"From a@b Mon Jan  1 00:00:00 2024\nSubject: First mail\n\nbody one\n\n"
            b"From c@d Mon Jan  1 00:00:00 2024\nSubject: Second mail\n\nbody two\n")

    def test_mbox_two_messages(self):
        rows = _split("mail.mbox", self.MBOX)
        text = _all_text(rows)
        self.assertIn("body one", text)
        self.assertIn("body two", text)
        self.assertGreaterEqual(len(rows), 2)

    def test_mbox_requires_from_line(self):
        # 无 From 起始行 -> 不认领，落 TextSplitHandle 兜底
        self.assertNotEqual(ss.detect_handler(name="x.mbox", content=b"just text\n"),
                            "MboxSplitHandle")


class VcalVcardTests(unittest.TestCase):
    ICS = (b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Team Sync\nDTSTART:20260101T100000\n"
           b"LOCATION:Room 3\nEND:VEVENT\nEND:VCALENDAR\n")
    VCF = b"BEGIN:VCARD\nVERSION:3.0\nFN:John Doe\nTEL;TYPE=CELL:123456\nEMAIL:j@x.com\nEND:VCARD\n"

    def test_ics_summary_is_title(self):
        rows = _split("cal.ics", self.ICS)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["title"], "Team Sync")
        self.assertIn("Room 3", rows[0]["content"])

    def test_vcf_name_and_fields(self):
        rows = _split("card.vcf", self.VCF)
        text = _all_text(rows)
        self.assertIn("John Doe", text)
        self.assertIn("123456", text)


class ExtendedImageTests(unittest.TestCase):
    def test_extra_raster_claimed(self):
        for ext in (".ico", ".tga", ".pcx", ".ppm", ".psd", ".icns"):
            self.assertEqual(
                ss.detect_handler(name="p" + ext, content=b"\0" * 60),
                "ExtendedImageSplitHandle", ext)

    def test_svg_not_claimed_as_image(self):
        self.assertNotEqual(
            ss.detect_handler(name="p.svg", content=b'<svg xmlns="x"><text>t</text></svg>'),
            "ExtendedImageSplitHandle")

    def test_raster_extends_image_handler(self):
        from smart_slice.handlers.image import ImageSplitHandle
        from smart_slice.handlers.raster_image import ExtendedImageSplitHandle
        self.assertTrue(issubclass(ExtendedImageSplitHandle, ImageSplitHandle))


class SourceFidelityTests(unittest.TestCase):
    def test_source_extensions_are_literal(self):
        for e in (".java", ".go", ".c", ".cpp", ".rs", ".rb", ".php", ".swift",
                  ".properties", ".adoc", ".org", ".diff", ".proto", ".graphql"):
            self.assertIn(e, TEXT_EXTENSIONS, e)
            self.assertIn(e, LITERAL_EXTENSIONS, e)

    def test_sharp_comment_not_a_title(self):
        java = "// header\n\n# a sharp line\n\nint x = 1;\n"
        rows = _split("a.java", java.encode())
        titles = " ".join(r.get("title") or "" for r in rows)
        self.assertNotIn("a sharp line", titles)
        self.assertIn("# a sharp line", _all_text(rows))


class OoxmlVariantTests(unittest.TestCase):
    def test_pptx_variants_declared(self):
        exts = set(ss.HANDLER_EXTENSIONS["PptxSplitHandle"])
        for e in (".pptx", ".pptm", ".ppsx", ".ppsm", ".potx", ".potm"):
            self.assertIn(e, exts)

    def test_doc_variants_declared(self):
        exts = set(ss.HANDLER_EXTENSIONS["DocSplitHandle"])
        for e in (".docx", ".docm", ".dotx", ".dotm"):
            self.assertIn(e, exts)

    def test_xlsx_variants_declared(self):
        exts = set(ss.HANDLER_EXTENSIONS["XlsxSplitHandle"])
        for e in (".xlsx", ".xlsm", ".xltx", ".xltm"):
            self.assertIn(e, exts)

    def test_tar_xz_declared(self):
        self.assertIn(".tar.xz", ss.HANDLER_EXTENSIONS["TarSplitHandle"])
        self.assertIn(".txz", ss.HANDLER_EXTENSIONS["TarSplitHandle"])


class LegacyBinaryAndSvgzTests(unittest.TestCase):
    """老二进制 .doc 报 400（不再被兜成 500）；.svgz 解压后正常解析。"""

    OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 200

    def test_legacy_binary_doc_is_400(self):
        with self.assertRaises(SliceError) as ctx:
            _split("legacy.doc", self.OLE)
        self.assertEqual(ctx.exception.code, 400)

    def test_real_docx_still_parses(self):
        docx = pytest_import_docx()
        if docx is None:
            self.skipTest("python-docx not installed")
        import io
        buf = io.BytesIO()
        doc = docx.Document()
        doc.add_paragraph("hello docx body")
        doc.save(buf)
        rows = _split("real.docx", buf.getvalue())
        self.assertIn("hello docx body", _all_text(rows))

    def test_svgz_decompressed(self):
        import gzip
        import io
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><text>gzipped svg</text></svg>'
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(svg)
        rows = _split("n.svgz", buf.getvalue())
        self.assertIn("gzipped svg", _all_text(rows))


def pytest_import_docx():
    try:
        import docx
        return docx
    except ImportError:
        return None


class ArchiveInnerFormatTests(unittest.TestCase):
    """压缩包内层文件经与公共层一致的清单分发（Phase 3 修复内层漏图片 handler）。"""

    PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+p7RLAAAAAElFTkSuQmCC")

    def test_zip_inner_image_is_skipped_not_claimed(self):
        """Phase 2-B 既有决策：内层清单刻意不含图片 handler。

        独立图片文件在压缩包内层被跳过（不产段落、不阻断整包），内嵌图片由
        ZipSplitHandle 自身的 markdown 引用收集 + save_image 路径处理。该行为由
        tests/test_service.py::OcrInjectionBranchTests 固化，此处断言不被格式扩展
        改动破坏。
        """
        rows = _split("pack.zip", _zip("pic.png", self.PNG))
        self.assertEqual(rows, [])

    def test_zip_inner_new_formats(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("n.svg", b'<svg xmlns="x"><text>inner svg text</text></svg>')
            z.writestr("a.md", b"# Head\n\ninner markdown body")
            z.writestr("b.java", b"// c\n\n# sharp not a title\n")
        rows = _split("pack.zip", buf.getvalue())
        text = _all_text(rows)
        self.assertIn("inner svg text", text)
        self.assertIn("inner markdown body", text)
        self.assertIn("sharp not a title", text)
        # 源码里的 # 行不得成为段落标题
        self.assertNotIn("sharp not a title", " ".join(r.get("title") or "" for r in rows))

    def test_tar_inner_new_format(self):
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b'<svg xmlns="x"><text>inner tar svg</text></svg>'
            info = tarfile.TarInfo("n.svg")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        rows = _split("pack.tar.gz", buf.getvalue())
        self.assertIn("inner tar svg", _all_text(rows))


class RegistryTests(unittest.TestCase):
    def test_new_handlers_registered(self):
        names = [type(h).__name__ for h in SPLIT_HANDLERS]
        for h in ("SvgSplitHandle", "IpynbSplitHandle", "SubtitleSplitHandle",
                  "MboxSplitHandle", "VcalendarSplitHandle", "VcardSplitHandle",
                  "Fb2SplitHandle", "ExtendedImageSplitHandle"):
            self.assertIn(h, names)

    def test_text_fallback_last(self):
        names = [type(h).__name__ for h in SPLIT_HANDLERS]
        self.assertEqual(names[-1], "TextSplitHandle")

    def test_fb2_before_zip(self):
        names = [type(h).__name__ for h in SPLIT_HANDLERS]
        self.assertLess(names.index("Fb2SplitHandle"), names.index("ZipSplitHandle"))


class UnsupportedStillRejectedTests(unittest.TestCase):
    def test_rar_rejected(self):
        with self.assertRaises(SliceError) as ctx:
            _split("a.rar", b"Rar!\x1a\x07\x01\x00fake")
        self.assertEqual(ctx.exception.code, 400)

    def test_media_rejected(self):
        with self.assertRaises(SliceError) as ctx:
            _split("v.mp4", b"\x00\x00\x00\x18ftypmp42")
        self.assertEqual(ctx.exception.code, 400)

    def test_svg_still_not_text_fallback(self):
        # .svg 由 SvgSplitHandle 认领，不再落 TextSplitHandle 排除表 -> 400
        self.assertEqual(
            ss.detect_handler(name="n.svg", content=b'<svg xmlns="x"><text>t</text></svg>'),
            "SvgSplitHandle")


if __name__ == "__main__":
    unittest.main()
