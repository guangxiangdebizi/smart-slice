# coding=utf-8
"""Multimodal support: image primitives, collection, recovery and attachment.

Everything here is built offline from stdlib fixtures (a valid PNG is synthesised
with ``zlib``), so the suite does not depend on Pillow, python-docx or a network.
Cases that need a specific optional parser are guarded with ``skipUnless``.

Run:
    python -m pytest tests/test_multimodal.py -v
"""
import base64
import email.message
import io
import struct
import threading
import unittest
import zipfile
import zlib

from smart_slice import _images
from smart_slice._images import image_dimensions, sniff_mime
from smart_slice.multimodal import (
    ImageCollector,
    attach_images,
    container_kind,
    extract_media,
    scan_media,
    slice_multimodal,
)
from smart_slice.types import ImageAsset


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #


def make_png(width=8, height=5, rgb=(200, 30, 90)):
    """A real, decodable PNG built from the specification (no Pillow needed)."""

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(bytearray(rgb)) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def make_gif(width=7, height=3):
    return (b"GIF89a" + struct.pack("<HH", width, height) + b"\xf7\x00\x00"
            + b"\x00" * 6 + b"\x3b")


def make_bmp(width=6, height=4):
    row = (b"\xff\x00\x00" * width + b"\x00" * ((4 - width * 3 % 4) % 4))
    payload = row * height
    return (b"BM" + struct.pack("<IHHI", 54 + len(payload), 0, 0, 54)
            + struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(payload), 0, 0, 0, 0)
            + payload)


def make_webp_extended(width=11, height=9):
    return (b"RIFF" + struct.pack("<I", 30) + b"WEBPVP8X" + struct.pack("<I", 10)
            + b"\x00" * 4 + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))


def make_tiff(width=13, height=6):
    entries = struct.pack("<H", 2)
    entries += struct.pack("<HHIH", 256, 3, 1, width) + b"\x00\x00"
    entries += struct.pack("<HHIH", 257, 3, 1, height) + b"\x00\x00"
    body = struct.pack("<H", 8) + entries
    return b"II*\x00" + struct.pack("<I", 8) + body[2:]


def make_ico(*sizes):
    directory = struct.pack("<HHH", 0, 1, len(sizes))
    for w, h in sizes:
        directory += struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, 40, 22)
    return directory + b"\x00" * 40


def make_jpeg_header(width=17, height=12):
    """Segment structure a JPEG parser walks; not decodable, but sized correctly."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x01\x11\x00" * 3
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


SVG = (b'<?xml version="1.0" encoding="UTF-8"?>\n'
       b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="45" viewBox="0 0 120 45">'
       b'<rect width="120" height="45"/></svg>')

PNG_A = make_png(8, 5)
PNG_B = make_png(4, 4, (1, 2, 3))
PNG_A_B64 = base64.b64encode(PNG_A).decode()


def zip_bytes(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def epub_bytes(image=PNG_A, src="../OEBPS/images/cover.png"):
    return zip_bytes({
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": (
            b'<?xml version="1.0"?><container version="1.0" '
            b'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            b'<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            b'</rootfiles></container>'),
        "OEBPS/content.opf": (
            b'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            b'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            b'<dc:title>Book</dc:title><dc:identifier id="id">x</dc:identifier>'
            b'<dc:language>en</dc:language></metadata><manifest>'
            b'<item id="c1" href="chap1.xhtml" media-type="application/xhtml+xml"/>'
            b'<item id="i1" href="images/cover.png" media-type="image/png"/></manifest>'
            b'<spine><itemref idref="c1"/></spine></package>'),
        "OEBPS/chap1.xhtml": (
            b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
            b'<title>Ch 1</title></head><body><h1>Chapter One</h1>'
            b'<p>Opening prose of the chapter with plenty of words to match on.</p>'
            b'<img src="' + src.encode() + b'" alt="cover"/><p>Closing prose.</p></body></html>'),
        "OEBPS/images/cover.png": image,
    })


def mime_bytes(image=PNG_A, cid="shot@x"):
    message = email.message.Message()
    message["MIME-Version"] = "1.0"
    message["Content-Type"] = 'multipart/related; boundary="BB"; type="text/html"'
    message.set_payload(
        '--BB\r\nContent-Type: text/html; charset="utf-8"\r\n\r\n'
        '<html><body><h1>Mail Heading</h1>'
        '<p>Prose inside the mail body that is long enough to slice on.</p>'
        '<img src="cid:%s"></body></html>\r\n'
        '--BB\r\nContent-Type: image/png\r\nContent-ID: <%s>\r\n'
        'Content-Transfer-Encoding: base64\r\n\r\n%s\r\n--BB--\r\n'
        % (cid, cid, base64.b64encode(image).decode()))
    return message.as_bytes()


def has_module(name):
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001 - an absent optional extra skips the case
        return False


# --------------------------------------------------------------------------- #


class ImagePrimitiveTests(unittest.TestCase):
    def test_sniff_by_magic_bytes(self):
        cases = {
            "image/png": PNG_A, "image/gif": make_gif(), "image/bmp": make_bmp(),
            "image/webp": make_webp_extended(), "image/tiff": make_tiff(),
            "image/jpeg": make_jpeg_header(), "image/x-icon": make_ico((16, 16)),
            "image/svg+xml": SVG,
        }
        for expected, payload in cases.items():
            self.assertEqual(sniff_mime(payload), expected, expected)

    def test_sniff_rejects_non_images(self):
        for payload in (b"", b"hello world", b"PK\x03\x04not-an-image", b"%PDF-1.4\n",
                        struct.pack("<I", 1) + b"\x00" * 200):
            self.assertIsNone(sniff_mime(payload), payload[:16])

    def test_svg_root_element_must_be_svg(self):
        xhtml = (b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
                 b'<svg width="10" height="10"></svg></body></html>')
        self.assertIsNone(sniff_mime(xhtml))
        self.assertEqual(sniff_mime(b'<!-- c -->\n<!DOCTYPE svg>\n<svg width="3" height="4"/>'), "image/svg+xml")

    def test_dimensions(self):
        self.assertEqual(image_dimensions(PNG_A), (8, 5))
        self.assertEqual(image_dimensions(make_png(31, 17)), (31, 17))
        self.assertEqual(image_dimensions(make_gif()), (7, 3))
        self.assertEqual(image_dimensions(make_bmp()), (6, 4))
        self.assertEqual(image_dimensions(make_webp_extended()), (11, 9))
        self.assertEqual(image_dimensions(make_tiff()), (13, 6))
        self.assertEqual(image_dimensions(make_jpeg_header()), (17, 12))
        self.assertEqual(image_dimensions(make_ico((16, 16), (32, 32))), (32, 32))
        self.assertEqual(image_dimensions(SVG), (120, 45))

    def test_svg_size_falls_back_to_viewbox_and_units(self):
        self.assertEqual(image_dimensions(b'<svg viewBox="0 0 200 100"></svg>'), (200, 100))
        self.assertEqual(image_dimensions(b'<svg width="2.5in" height="1in"></svg>'), (240, 96))
        self.assertIsNone(image_dimensions(b'<svg width="100%" height="50%"></svg>'))

    def test_dimensions_never_raise(self):
        self.assertIsNone(image_dimensions(b""))
        self.assertIsNone(image_dimensions(b"\x89PNG\r\n\x1a\ntruncated"))
        self.assertIsNone(image_dimensions(b"not an image at all"))

    def test_emf_needs_a_plausible_header(self):
        self.assertEqual(sniff_mime(b"\x01\x00\x00\x00" + struct.pack("<I", 88) + b"\x00" * 88), "image/x-emf")
        self.assertIsNone(sniff_mime(b"\x01\x00\x00\x00" + struct.pack("<I", 4) + b"\x00" * 8))
        self.assertEqual(sniff_mime(b"\xd7\xcd\xc6\x9a" + b"\x00" * 20), "image/x-wmf")

    def test_vector_mimes_are_segregated(self):
        self.assertIn("image/svg+xml", _images.VECTOR_MIMES)
        self.assertNotIn("image/png", _images.VECTOR_MIMES)
        self.assertIn("image/png", _images.RASTER_MIMES)
        self.assertTrue(_images.is_image_bytes(PNG_A))
        self.assertFalse(_images.is_image_bytes(SVG, include_vector=False))
        self.assertTrue(_images.is_image_bytes(SVG, include_vector=True))


class ImageAssetViewTests(unittest.TestCase):
    def asset(self, payload=PNG_A, **meta):
        meta.setdefault("content", payload)
        return ImageAsset(id="id-1", file_name="pic.png", meta=meta)

    def test_computed_views(self):
        asset = self.asset()
        self.assertEqual(asset.mime_type, "image/png")
        self.assertEqual(asset.suffix, ".png")
        self.assertEqual(asset.size, len(PNG_A))
        self.assertEqual(asset.dimensions, (8, 5))
        self.assertEqual((asset.width, asset.height), (8, 5))
        self.assertEqual(len(asset.sha256), 64)
        self.assertTrue(asset.data_uri().startswith("data:image/png;base64,"))

    def test_declared_mime_wins_over_sniffing(self):
        self.assertEqual(self.asset(mime="image/x-custom").mime_type, "image/x-custom")

    def test_mime_falls_back_to_the_file_name(self):
        asset = ImageAsset(id=1, file_name="chart.WEBP", meta={})
        self.assertEqual(asset.mime_type, "image/webp")
        self.assertEqual(asset.size, 0)
        self.assertIsNone(asset.dimensions)
        self.assertEqual(asset.data_uri(), "")

    def test_external_reference_data_uri_is_the_source(self):
        asset = ImageAsset(id=1, file_name="a.png", meta={"src": "https://x/a.png", "external": True})
        self.assertEqual(asset.data_uri(), "https://x/a.png")

    def test_dimensions_are_cached(self):
        asset = self.asset()
        self.assertEqual(asset.dimensions, (8, 5))
        asset.__dict__["_dimensions"] = False
        self.assertIsNone(asset.dimensions)

    def test_to_dict_and_repr_hide_bytes(self):
        asset = self.asset(source="zip", member="word/media/image1.png", page=None)
        row = asset.to_dict()
        self.assertEqual(row["mime_type"], "image/png")
        self.assertEqual(row["member"], "word/media/image1.png")
        self.assertNotIn("page", row)
        self.assertNotIn("data_uri", row)
        self.assertNotIn("content", row)
        self.assertIn("data_uri", asset.to_dict(with_data=True))
        self.assertIn(f"bytes={len(PNG_A)}", repr(asset))
        self.assertNotIn(base64.b64encode(PNG_A).decode()[:24], repr(asset))

    def test_constructor_signature_is_unchanged(self):
        # generated handlers build assets positionally and by keyword
        self.assertEqual(ImageAsset("a", "b.png", {"content": PNG_A}).content, PNG_A)
        self.assertEqual(ImageAsset(id="a").content, b"")


class ImageCollectorTests(unittest.TestCase):
    def asset(self, payload, ident=None):
        return ImageAsset(id=ident or f"id-{len(payload)}-{payload[16]}", file_name="p.png",
                          meta={"content": payload})

    def test_collects_and_returns_no_mapping_without_duplicates(self):
        collector = ImageCollector()
        self.assertEqual(collector([self.asset(PNG_A)]), {})
        self.assertEqual(collector([self.asset(PNG_B)]), {})
        self.assertEqual(len(collector.assets), 2)
        self.assertEqual(collector.duplicates, 0)

    def test_dedupe_returns_the_remap_contract(self):
        collector = ImageCollector()
        first = self.asset(PNG_A, "keep-me")
        second = self.asset(PNG_A, "drop-me")
        self.assertEqual(collector([first]), {})
        mapping = collector([second])
        self.assertEqual(mapping, {"drop-me": "keep-me"})
        self.assertEqual(len(collector.assets), 1)
        self.assertEqual(collector.duplicates, 1)
        self.assertEqual(collector.remapped, {"drop-me": "keep-me"})
        self.assertEqual(collector.assets[0].meta["also_referenced_as"], ["drop-me"])

    def test_assets_without_bytes_are_never_deduped(self):
        collector = ImageCollector()
        blank = [ImageAsset(id=n, file_name="x", meta={}) for n in ("a", "b")]
        collector(blank)
        self.assertEqual(len(collector.assets), 2)

    def test_dedupe_can_be_switched_off(self):
        collector = ImageCollector(dedupe=False)
        collector([self.asset(PNG_A, "1"), self.asset(PNG_A, "2")])
        self.assertEqual(len(collector.assets), 2)
        self.assertEqual(collector.duplicates, 0)

    def test_max_images_drops_the_overflow(self):
        collector = ImageCollector(max_images=1)
        collector([self.asset(PNG_A, "1"), self.asset(PNG_B, "2")])
        self.assertEqual(len(collector.assets), 1)
        self.assertEqual(collector.overflow, 1)

    def test_keep_content_false_frees_the_bytes(self):
        collector = ImageCollector(keep_content=False)
        collector([self.asset(PNG_A, "1")])
        self.assertEqual(collector.assets[0].content, b"")
        self.assertNotIn("content", collector.assets[0].meta)

    def test_handler_supplied_shapes_are_accepted(self):
        collector = ImageCollector()
        collector(None)
        collector([])
        collector(iter([self.asset(PNG_A, "1")]))
        collector({"k": self.asset(PNG_B, "2")}.values())   # xlsx passes dict_values
        collector(["not-an-asset"])                          # defensive
        self.assertEqual(len(collector.assets), 2)

    def test_is_thread_safe(self):
        collector = ImageCollector(dedupe=False)
        payloads = [make_png(3 + i, 3) for i in range(8)]

        def worker():
            for payload in payloads:
                collector([self.asset(payload)])

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(collector.assets), 48)
        self.assertEqual(len(collector.known_sha()), 8)

    def test_repr_reports_sizes_without_bytes(self):
        collector = ImageCollector()
        collector([self.asset(PNG_A, "1")])
        self.assertIn("images=1", repr(collector))

class ContainerKindTests(unittest.TestCase):
    def test_magic_bytes_win_over_the_name(self):
        self.assertEqual(container_kind(zip_bytes({"a.txt": b"x"}), "not-an-archive.bin"), "zip")
        self.assertEqual(container_kind(b"%PDF-1.7\n", "notes.txt"), "pdf")
        self.assertEqual(container_kind(PNG_A, "mislabelled.txt"), "image")

    def test_by_name_when_bytes_are_ambiguous(self):
        self.assertEqual(container_kind(b"# Title\n\nbody", "doc.md"), "markup")
        self.assertEqual(container_kind(b"<html><body>x</body></html>", "page.html"), "markup")
        self.assertEqual(container_kind(b"From: a@b\nMIME-Version: 1.0\n", "mail.eml"), "mime")
        self.assertEqual(container_kind(SVG, "logo.svg"), "image")

    def test_content_sniffing_finds_markup_without_an_extension(self):
        self.assertEqual(container_kind(b'<html><body><img src="a.png"></body></html>', "blob"), "markup")
        self.assertEqual(container_kind(b'text ![f](data:image/png;base64,AA==) more', "blob"), "markup")

    def test_unknown(self):
        self.assertEqual(container_kind(b"\x00\x01\x02\x03binary", "thing.bin"), "unknown")
        self.assertEqual(container_kind(b"", ""), "unknown")


class ExtractMediaTests(unittest.TestCase):
    def test_standalone_picture(self):
        assets = extract_media(PNG_A, "photo.png")
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].content, PNG_A)
        self.assertEqual(assets[0].mime_type, "image/png")
        self.assertEqual(assets[0].dimensions, (8, 5))
        self.assertEqual(assets[0].meta["role"], "document")

    def test_html_data_uri_and_external_references(self):
        html = ('<html><body><h1>Intro</h1><p>Leading prose about widgets.</p>'
                f'<img src="data:image/png;base64,{PNG_A_B64}" alt="c"/>'
                '<h2>Remote</h2><p>Trailing prose after the second heading.</p>'
                '<img src="https://cdn.example.com/pic.png">'
                '<img src=\'images/local.png\'>'
                '<IMG SRC="DATA:IMAGE/GIF;BASE64,%s">'
                '</body></html>' % base64.b64encode(make_gif()).decode())
        report = scan_media(html.encode(), "page.html")
        self.assertEqual(report.kind, "markup")
        inline = [a for a in report.assets if a.content]
        external = report.external
        self.assertEqual(len(inline), 2)
        self.assertEqual({a.mime_type for a in inline}, {"image/png", "image/gif"})
        self.assertEqual(len(external), 2)
        self.assertEqual({a.meta["src"] for a in external},
                         {"https://cdn.example.com/pic.png", "images/local.png"})
        self.assertTrue(all(a.meta["external"] for a in external))
        self.assertEqual(external[0].meta["heading"], "Remote")
        self.assertTrue(all(a.meta["anchor"] for a in report.assets))

    def test_markdown_image_syntax(self):
        md = ("# Title\n\nBody paragraph with enough words to anchor on here.\n\n"
              f"![fig](data:image/png;base64,{PNG_A_B64})\n\n## Sub\n\nTrailing body text.\n")
        assets = extract_media(md.encode(), "doc.md")
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].content, PNG_A)
        self.assertEqual(assets[0].meta["heading"], "Title")

    def test_broken_data_uri_is_reported_not_raised(self):
        html = b'<img src="data:image/png;base64,!!!!not-base64!!!!">'
        report = scan_media(html, "page.html")
        self.assertEqual(report.assets, [])
        self.assertEqual(report.skipped, 1)
        self.assertTrue(report.errors)

    def test_external_references_can_be_dropped(self):
        html = b'<img src="https://cdn.example.com/a.png">'
        self.assertEqual(len(extract_media(html, "p.html", include_external=False)), 0)
        self.assertEqual(len(extract_media(html, "p.html", include_external=True)), 1)

    def test_min_side_filters_decorative_images(self):
        html = (f'<img src="data:image/png;base64,{base64.b64encode(make_png(3, 3)).decode()}">'
                f'<img src="data:image/png;base64,{base64.b64encode(make_png(40, 30)).decode()}">')
        report = scan_media(html.encode(), "p.html", min_side=20)
        self.assertEqual(len(report.assets), 1)
        self.assertEqual(report.assets[0].dimensions, (40, 30))
        self.assertEqual(report.skipped, 1)

    def test_vector_formats_are_opt_in(self):
        payload = zip_bytes({"a.svg": SVG, "b.png": PNG_A})
        self.assertEqual({a.mime_type for a in extract_media(payload, "p.zip")}, {"image/png"})
        self.assertEqual({a.mime_type for a in extract_media(payload, "p.zip", include_vector=True)},
                         {"image/png", "image/svg+xml"})

    def test_ooxml_media_members(self):
        payload = zip_bytes({
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b"<w:document/>",
            "word/media/image1.png": PNG_A,
            "word/media/image2.emf": b"\x01\x00\x00\x00" + struct.pack("<I", 88) + b"\x00" * 88,
        })
        assets = extract_media(payload, "doc.docx")
        self.assertEqual([a.file_name for a in assets], ["image1.png"])
        self.assertEqual(assets[0].meta["member"], "word/media/image1.png")
        self.assertEqual(assets[0].meta["source"], "zip")

    def test_epub_relative_reference_resolves_to_the_member(self):
        report = scan_media(epub_bytes(), "book.epub")
        self.assertEqual(len(report.assets), 1, report.summary())
        asset = report.assets[0]
        self.assertEqual(asset.content, PNG_A)
        self.assertFalse(asset.meta.get("external"))
        self.assertEqual(asset.meta["heading"], "Chapter One")
        self.assertEqual(asset.meta["member"], "OEBPS/images/cover.png")
        self.assertEqual(asset.meta["referenced_in"], ["OEBPS/chap1.xhtml"])

    def test_epub_unresolvable_reference_stays_external(self):
        report = scan_media(epub_bytes(src="https://cdn.example.com/missing.png"), "book.epub")
        self.assertEqual(len(report.assets), 2)
        self.assertTrue(any(a.meta.get("external") for a in report.assets))

    def test_duplicate_pictures_collapse(self):
        payload = zip_bytes({"a.png": PNG_A, "b.png": PNG_A, "c.png": PNG_B})
        self.assertEqual(len(extract_media(payload, "p.zip")), 2)

    def test_mime_cid_reference_resolves(self):
        report = scan_media(mime_bytes(), "mail.mhtml")
        self.assertEqual(len(report.assets), 1, report.summary())
        asset = report.assets[0]
        self.assertEqual(asset.content, PNG_A)
        self.assertFalse(asset.meta.get("external"))
        self.assertEqual(asset.meta["heading"], "Mail Heading")

    def test_corrupt_container_is_reported_not_raised(self):
        report = scan_media(b"PK\x03\x04" + b"garbage" * 20, "broken.zip")
        self.assertEqual(report.assets, [])
        self.assertTrue(report.errors)

    def test_caps_are_enforced(self):
        payload = zip_bytes({f"i{n}.png": make_png(4 + n, 4) for n in range(6)})
        self.assertEqual(len(extract_media(payload, "p.zip", max_images=3)), 3)
        self.assertEqual(len(extract_media(payload, "p.zip", max_image_bytes=10)), 0)

    def test_unknown_container_extracts_nothing(self):
        report = scan_media(b"\x00\x01\x02binary", "thing.bin")
        self.assertEqual(report.assets, [])
        self.assertEqual(report.kind, "unknown")
        self.assertTrue(report.errors)

    def test_scan_media_summary_and_iteration(self):
        report = scan_media(PNG_A, "a.png")
        self.assertEqual(len(report), 1)
        self.assertEqual([a.mime_type for a in report], ["image/png"])
        self.assertIn("1 images", report.summary())


def real_jpeg(width=17, height=12):
    """A genuinely decodable JPEG when Pillow is installed, else a header-only stub."""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - Pillow is optional
        return make_jpeg_header(width, height)
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 40, 40)).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def make_pdf_bytes(jpeg=None, width=17, height=12):
    """A minimal one-page PDF carrying a DCTDecode image XObject and a text run."""
    jpeg = jpeg if jpeg is not None else real_jpeg(width, height)
    content = (b"BT /F1 12 Tf 40 350 Td (A PDF paragraph with enough words to slice.) Tj ET\n"
               b"q %d 0 0 %d 40 40 cm /Im1 Do Q\n" % (width, height))
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 400] /Resources << /XObject << /Im1 4 0 R >> "
        b"/Font << /F1 6 0 R >> /ProcSet [/PDF /Text /ImageC] >> /Contents 5 0 R >>",
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (width, height, len(jpeg))
        + jpeg + b"\nendstream",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(bodies) + 1, xref))
    return out.getvalue()


class ExtractPdfTests(unittest.TestCase):
    @unittest.skipUnless(has_module("pypdf"), "smart-slice[pdf] not installed")
    def test_pdf_image_xobject(self):
        report = scan_media(make_pdf_bytes(), "doc.pdf")
        self.assertEqual(report.kind, "pdf")
        self.assertEqual(len(report.assets), 1, report.summary())
        self.assertEqual(report.assets[0].meta["source"], "pdf")
        self.assertEqual(report.assets[0].meta["page"], 1)

    @unittest.skipUnless(has_module("pypdf"), "smart-slice[pdf] not installed")
    def test_broken_pdf_is_reported(self):
        report = scan_media(b"%PDF-1.4\nthis is not a real pdf body", "bad.pdf")
        self.assertEqual(report.assets, [])
        self.assertTrue(report.errors)

def asset(ident, payload=PNG_A, **meta):
    meta.setdefault("content", payload)
    return ImageAsset(id=ident, file_name=f"{ident}.png", meta=meta)


class AttachImagesTests(unittest.TestCase):
    paragraphs = [
        {"title": "Intro", "content": "Leading prose about widgets and their behaviour."},
        {"title": "Middle", "content": "Body text with a picture reference ![p](./oss/file/ref-1) inside."},
        {"title": "Tail", "content": "Closing prose that mentions nothing at all."},
    ]

    def test_reference_pass_is_exact(self):
        held = asset("ref-1")
        result = attach_images(self.paragraphs, [held])
        self.assertEqual([g.index for g in result.groups if g.images], [1])
        self.assertEqual(result.groups[1].images[0].meta["match"], "reference")
        self.assertEqual(result.unattached, [])
        self.assertEqual(result.coverage, 1.0)

    def test_a_reference_per_paragraph_and_no_duplicates(self):
        rows = [
            {"title": "A", "content": "x ./oss/file/r1 y ./oss/file/r1 z"},
            {"title": "B", "content": "q ./oss/file/r1"},
        ]
        result = attach_images(rows, [asset("r1")])
        self.assertEqual(len(result.groups[0].images), 1)
        self.assertEqual(len(result.groups[1].images), 1)
        self.assertEqual(len(result.attached), 2)
        self.assertEqual(len(result.images), 1)

    def test_unknown_reference_is_ignored(self):
        result = attach_images(self.paragraphs, [asset("nobody-references-me")])
        self.assertEqual(result.attached, [])
        self.assertEqual(len(result.unattached), 1)
        self.assertEqual(result.coverage, 0.0)

    def test_heading_pass(self):
        held = asset("h1", heading="Tail")
        result = attach_images(self.paragraphs, [held])
        self.assertEqual([g.index for g in result.groups if g.images], [2])
        self.assertEqual(held.meta["match"], "heading")

    def test_heading_pass_tolerates_a_chained_title(self):
        rows = [{"title": "Intro  Remote", "content": "some words here"}]
        result = attach_images(rows, [asset("h", heading="Remote")])
        self.assertEqual(len(result.attached), 1)

    def test_anchor_pass(self):
        held = asset("a1", anchor="prose about widgets and their")
        result = attach_images(self.paragraphs, [held], heading_fallback=False)
        self.assertEqual([g.index for g in result.groups if g.images], [0])
        self.assertEqual(held.meta["match"], "anchor")

    def test_anchor_pass_is_switchable(self):
        held = asset("a1", anchor="prose about widgets and their")
        result = attach_images(self.paragraphs, [held], heading_fallback=False, anchor_fallback=False)
        self.assertEqual(result.attached, [])
        self.assertEqual(len(result.unattached), 1)

    def test_short_anchors_are_not_trusted(self):
        result = attach_images(self.paragraphs, [asset("s", anchor="too short")])
        self.assertEqual(result.attached, [])

    def test_document_role_pins_to_the_first_paragraph(self):
        rows = [{"title": "photo.png", "content": "photo.png"}]
        result = attach_images(rows, [asset("d", role="document")])
        self.assertEqual(len(result.groups[0].images), 1)
        self.assertEqual(result.groups[0].images[0].meta["match"], "document")

    def test_paragraph_text_is_never_modified(self):
        before = [dict(row) for row in self.paragraphs]
        attach_images(self.paragraphs, [asset("ref-1"), asset("h", heading="Intro")])
        self.assertEqual(self.paragraphs, before)

    def test_non_dict_rows_and_empty_input(self):
        result = attach_images(["plain string row"], [asset("d", role="document")])
        self.assertEqual(result.groups[0].content, "plain string row")
        empty = attach_images([], [])
        self.assertEqual((len(empty.groups), empty.images, empty.coverage), (0, [], 1.0))

    def test_records_and_paired_shapes(self):
        result = attach_images(self.paragraphs, [asset("ref-1"), asset("orphan")])
        rows = result.records()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(rows[1]), ["content", "images", "index", "title"])
        self.assertEqual(len(rows[1]["images"]), 1)
        self.assertEqual(rows[1]["images"][0]["id"], "ref-1")
        self.assertNotIn("data_uri", rows[1]["images"][0])
        paired = result.paired(with_data=False)
        self.assertEqual([row["index"] for row in paired], [1])
        self.assertIn("data_uri", result.paired()[0]["images"][0])

    def test_stats_and_summary(self):
        result = attach_images(self.paragraphs, [asset("ref-1"), asset("orphan")])
        info = result.stats()
        self.assertEqual(info["images"], 2)
        self.assertEqual(info["attached"], 1)
        self.assertEqual(info["unattached"], 1)
        self.assertEqual(info["by_mime"], {"image/png": 2})
        self.assertEqual(info["by_match"], {"reference": 1})
        self.assertEqual(info["bytes"], 2 * len(PNG_A))
        self.assertIn("2 images", result.summary())
        self.assertEqual(len(result), 3)
        self.assertIs(result[0], result.groups[0])


class SliceMultimodalTests(unittest.TestCase):
    def test_html_end_to_end(self):
        html = ('<html><body><h1>Intro</h1><p>Leading prose about widgets in production.</p>'
                f'<img src="data:image/png;base64,{PNG_A_B64}"/>'
                '<h2>Remote</h2><p>Trailing prose after the second heading here.</p>'
                '<img src="https://cdn.example.com/pic.png"></body></html>')
        result = slice_multimodal(html.encode(), "page.html", limit=1000)
        self.assertEqual(result.name, "page.html")
        self.assertGreaterEqual(len(result.paragraphs), 1)
        self.assertEqual(len(result.images), 2)
        self.assertEqual(result.unattached, [])
        self.assertEqual(result.collector.duplicates, 0)
        self.assertEqual(result.extraction.kind, "markup")
        inline = [a for a in result.attached if a.content]
        self.assertEqual(len(inline), 1)
        self.assertEqual(inline[0].content, PNG_A)

    def test_standalone_picture_pins_to_its_paragraph(self):
        result = slice_multimodal(PNG_A, "photo.png")
        self.assertEqual(len(result.paragraphs), 1)
        self.assertEqual(len(result.groups[0].images), 1)
        self.assertEqual(result.groups[0].images[0].content, PNG_A)

    def test_markdown_data_uri(self):
        md = ("# Title\n\nBody paragraph with enough words to anchor on here.\n\n"
              f"![fig](data:image/png;base64,{PNG_A_B64})\n\n## Sub\n\nTrailing body text.\n")
        result = slice_multimodal(md.encode(), "doc.md")
        self.assertEqual(len(result.images), 1)
        self.assertEqual(len(result.attached), 1)

    @unittest.skipUnless(has_module("markdownify") and has_module("bs4"), "smart-slice[markup] not installed")
    def test_epub_end_to_end(self):
        result = slice_multimodal(epub_bytes(), "book.epub")
        self.assertGreaterEqual(len(result.paragraphs), 1)
        self.assertEqual(len(result.images), 1, result.extraction.summary())
        self.assertEqual(result.images[0].content, PNG_A)

    def test_mhtml_end_to_end(self):
        result = slice_multimodal(mime_bytes(), "mail.mhtml")
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.images[0].content, PNG_A)

    def test_probe_switches(self):
        html = f'<img src="data:image/png;base64,{PNG_A_B64}"/>'
        self.assertEqual(len(slice_multimodal(html.encode(), "p.html", probe=True).images), 1)
        self.assertEqual(len(slice_multimodal(html.encode(), "p.html", probe="auto").images), 1)
        self.assertEqual(len(slice_multimodal(html.encode(), "p.html", probe=False).images), 0)

    def test_collect_switch(self):
        html = f'<img src="data:image/png;base64,{PNG_A_B64}"/>'
        result = slice_multimodal(html.encode(), "p.html", collect=False)
        self.assertIsNone(result.collector)
        self.assertEqual(len(result.images), 1)

    def test_path_entry_point(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            target = os.path.join(folder, "pic.png")
            with open(target, "wb") as handle:
                handle.write(PNG_A)
            from smart_slice import slice_path_multimodal

            result = slice_path_multimodal(target)
            self.assertEqual(result.name, "pic.png")
            self.assertEqual(len(result.images), 1)
            self.assertEqual(result.groups[0].images[0].content, PNG_A)

    def test_needs_content_or_path(self):
        with self.assertRaises(TypeError):
            slice_multimodal()

    def test_dedupe_across_a_document(self):
        html = (f'<img src="data:image/png;base64,{PNG_A_B64}"/>'
                f'<img src="data:image/png;base64,{PNG_A_B64}"/>')
        result = slice_multimodal(html.encode(), "p.html")
        self.assertEqual(len(result.images), 1)

    def test_min_side_reaches_the_probe(self):
        html = f'<img src="data:image/png;base64,{base64.b64encode(make_png(2, 2)).decode()}"/>'
        self.assertEqual(len(slice_multimodal(html.encode(), "p.html", min_side=20).images), 0)
        self.assertEqual(len(slice_multimodal(html.encode(), "p.html").images), 1)

    def test_limit_and_overlap_still_reach_the_slicer(self):
        text = "# A\n\n" + ("word " * 400) + "\n\n# B\n\n" + ("term " * 400) + "\n"
        plain = slice_multimodal(text.encode(), "d.md", limit=200)
        wide = slice_multimodal(text.encode(), "d.md", limit=5000)
        self.assertGreater(len(plain.paragraphs), len(wide.paragraphs))
        overlapped = slice_multimodal(text.encode(), "d.md", limit=200, overlap=50)
        self.assertEqual(len(overlapped.paragraphs), len(plain.paragraphs))

    def test_progress_hook_and_ocr_hook_are_passed_through(self):
        beats = []
        seen = []

        def extractor(payload, label):
            seen.append(label)
            return "ocr text for the picture"

        result = slice_multimodal(PNG_A, "photo.png", progress_hook=lambda: beats.append(1),
                                  image_text_extractor=extractor)
        self.assertTrue(beats)
        self.assertTrue(seen)
        self.assertEqual(len(result.images), 1)


class MultimodalFidelityTests(unittest.TestCase):
    """Multimodal must not change a single character of the sliced text."""

    def cases(self):
        return [
            ("photo.png", PNG_A),
            ("page.html", ('<html><body><h1>Intro</h1><p>Prose about widgets.</p>'
                           f'<img src="data:image/png;base64,{PNG_A_B64}"/></body></html>').encode()),
            ("doc.md", ("# Title\n\nBody paragraph with words to slice on here.\n\n"
                        f"![f](data:image/png;base64,{PNG_A_B64})\n").encode()),
            ("book.epub", epub_bytes()),
            ("mail.mhtml", mime_bytes()),
            ("pack.zip", zip_bytes({"doc.md": b"# Pack\n\nSome markdown body text here.\n",
                                    "images/a.png": PNG_A, "images/b.png": PNG_B})),
        ]

    def test_paragraphs_are_identical_to_slice_bytes(self):
        from smart_slice import slice_bytes

        for name, payload in self.cases():
            with self.subTest(name=name):
                baseline = slice_bytes(payload, name, limit=1000, normalize=True)
                result = slice_multimodal(payload, name, limit=1000)
                self.assertEqual(result.paragraphs, baseline)

    def test_probe_true_does_not_change_the_text_either(self):
        from smart_slice import slice_bytes

        for name, payload in self.cases():
            with self.subTest(name=name):
                baseline = slice_bytes(payload, name, limit=1000, normalize=True)
                result = slice_multimodal(payload, name, limit=1000, probe=True)
                self.assertEqual(result.paragraphs, baseline)

    def test_images_are_recovered_for_every_case(self):
        for name, payload in self.cases():
            with self.subTest(name=name):
                self.assertGreaterEqual(len(slice_multimodal(payload, name).images), 1)


class BatchCollectImagesTests(unittest.TestCase):
    inputs = [
        ("a.png", PNG_A),
        ("b.png", PNG_B),
        ("page.html", (f'<img src="data:image/png;base64,{PNG_A_B64}"/>').encode()),
    ]

    def test_collect_images_off_by_default(self):
        from smart_slice import slice_many

        report = slice_many(self.inputs, backend="serial")
        self.assertEqual(report.images, [])
        self.assertTrue(report.ok)

    def test_collect_images_gathers_the_batch(self):
        from smart_slice import slice_many

        report = slice_many(self.inputs, backend="serial", collect_images=True)
        self.assertTrue(report.ok)
        self.assertEqual(len(report.images), 2)          # a.png and the html data URI dedupe
        self.assertEqual({i.mime_type for i in report.images}, {"image/png"})
        self.assertIn("2 images", report.summary())

    @unittest.skipUnless(has_module("pypdf"), "smart-slice[pdf] not installed")
    def test_explicit_save_image_still_wins(self):
        from smart_slice import slice_many

        seen = []
        report = slice_many([("doc.pdf", make_pdf_bytes())], backend="serial",
                            collect_images=True, save_image=lambda batch: seen.extend(batch))
        self.assertTrue(report.ok, report.failures)
        self.assertEqual(report.images, [])      # no collector was installed
        self.assertTrue(seen)                    # the caller's own sink ran

    @unittest.skipUnless(has_module("pypdf"), "smart-slice[pdf] not installed")
    def test_a_chained_sink_keeps_both_channels(self):
        seen = []
        result = slice_multimodal(make_pdf_bytes(), "doc.pdf",
                                  save_image=lambda batch: seen.extend(batch))
        self.assertTrue(seen)                    # the caller's sink still fires
        self.assertGreaterEqual(len(result.images), 1)   # and so does the collector

    def test_a_shared_collector_does_not_cross_documents(self):
        collector = ImageCollector()
        first = slice_multimodal(PNG_A, "a.png", collector=collector)
        second = slice_multimodal(PNG_B, "b.png", collector=collector)
        self.assertEqual(len(collector.assets), 2)
        self.assertEqual([a.sha256 for a in first.images], [ImageAsset(id=0, file_name="",
                                                                       meta={"content": PNG_A}).sha256])
        self.assertEqual(len(second.images), 1)
        self.assertEqual(len(first.groups[0].images), 1)
        self.assertEqual(len(second.groups[0].images), 1)
        self.assertNotEqual(first.images[0].sha256, second.images[0].sha256)

    def test_thread_backend_shares_one_collector(self):
        from smart_slice import slice_many

        report = slice_many(self.inputs, backend="thread", concurrency=3, collect_images=True)
        self.assertTrue(report.ok)
        self.assertEqual(len(report.images), 2)

    def test_process_backend_is_rejected(self):
        from smart_slice import slice_many

        with self.assertRaises(ValueError):
            slice_many(self.inputs, backend="process", collect_images=True)

    def test_empty_batch(self):
        from smart_slice import slice_many

        report = slice_many([], collect_images=True)
        self.assertEqual(report.images, [])
        self.assertEqual(len(report), 0)