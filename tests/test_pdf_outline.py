# coding=utf-8
"""Regression tests for the PDF bookmark (outline) path.

Before 2026-10-10 this path had no test coverage at all (``outline`` appeared
once in the whole suite, and every PDF fixture was a synthetic page without
bookmarks).  Three defects lived there and are now fixed:

1. ``collect_toc`` resolved each bookmark's ``level`` and ``handle_toc`` unpacked
   it, but never used it - every entry collapsed to one weight, so a chunk title
   kept only its leaf heading and the ancestor chain was lost.
2. ``handle_toc`` / ``handle_links`` never called ``extract_page_images``, so a
   PDF carrying an outline lost every illustration.
3. ``handle_chapter_title`` stripped only CJK-style numbering, so Arabic
   multi-level numbering ("3.2.1 ...") survived and ``text.find`` missed.
"""
import io
import unittest
from importlib.util import find_spec

from smart_slice import slice_bytes
from smart_slice.handlers.pdf import PdfSplitHandle

HAS_PYPDF = find_spec("pypdf") is not None
HAS_PIL = find_spec("PIL") is not None


def _real_jpeg(width=17, height=12):
    """A genuine (tiny) baseline JPEG, so the DCTDecode XObject is decodable."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _one_page_pdf(jpeg):
    """Hand-rolled single page: one Helvetica text run plus one image XObject."""
    content = (b"BT /F1 12 Tf 40 350 Td (A PDF paragraph with enough words to slice.) Tj ET\n"
               b"q 17 0 0 12 40 40 cm /Im1 Do Q\n")
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 400] /Resources << /XObject << /Im1 4 0 R >> "
        b"/Font << /F1 6 0 R >> /ProcSet [/PDF /Text /ImageC] >> /Contents 5 0 R >>",
        b"<< /Type /XObject /Subtype /Image /Width 17 /Height 12 /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % len(jpeg) + jpeg + b"\nendstream",
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
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(bodies) + 1, xref))
    return out.getvalue()


def make_bookmarked_pdf(nested=True, extra_pages=2):
    """A multi-page PDF with an outline; ``nested`` gives it two heading levels."""
    from pypdf import PdfReader, PdfWriter

    src = PdfReader(io.BytesIO(_one_page_pdf(_real_jpeg())))
    w = PdfWriter()
    w.add_page(src.pages[0])
    for _ in range(extra_pages):
        w.add_blank_page(width=400, height=400)
    if nested:
        c1 = w.add_outline_item("Chapter 1", 0)
        w.add_outline_item("1.1 Installing", 0, parent=c1)
        w.add_outline_item("1.2 Configuring", 1, parent=c1)
        c2 = w.add_outline_item("Chapter 2", 2)
        w.add_outline_item("2.1 Running", 2, parent=c2)
    else:
        w.add_outline_item("Alpha", 0)
        w.add_outline_item("Beta", 1)
        w.add_outline_item("Gamma", 2)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _titles(data):
    return [p.get("title") or "" for p in slice_bytes(data, "manual.pdf", limit=100000)]


@unittest.skipUnless(HAS_PYPDF and HAS_PIL, "smart-slice[pdf] not installed")
class OutlineLevelTests(unittest.TestCase):
    def test_nested_outline_keeps_the_ancestor_chain(self):
        titles = _titles(make_bookmarked_pdf(nested=True))
        self.assertIn("Chapter 1 1.1 Installing", titles)
        self.assertIn("Chapter 1 1.2 Configuring", titles)
        self.assertIn("Chapter 2 2.1 Running", titles)

    def test_top_level_entries_carry_no_stale_ancestor(self):
        titles = _titles(make_bookmarked_pdf(nested=True))
        self.assertIn("Chapter 1", titles)
        self.assertIn("Chapter 2", titles)
        self.assertNotIn("Chapter 1 Chapter 2", titles)

    def test_flat_outline_titles_are_unchanged(self):
        """A single-level outline must behave exactly as before the fix."""
        self.assertEqual(_titles(make_bookmarked_pdf(nested=False)), ["Alpha", "Beta", "Gamma"])

    def test_chain_is_joined_with_a_single_space(self):
        """Matches chunker.result_tree_to_paragraph's " ".join(parent_chain)."""
        for t in _titles(make_bookmarked_pdf(nested=True)):
            self.assertNotIn("  ", t)


@unittest.skipUnless(HAS_PYPDF and HAS_PIL, "smart-slice[pdf] not installed")
class OutlineImageTests(unittest.TestCase):
    def test_bookmarked_pdf_no_longer_loses_its_illustrations(self):
        from smart_slice import slice_multimodal

        result = slice_multimodal(make_bookmarked_pdf(nested=True), "manual.pdf", limit=100000)
        self.assertGreaterEqual(len(result.images), 1, result.summary() if hasattr(result, "summary") else "")

    def test_the_same_image_is_not_registered_twice(self):
        """Parent and first child cover page 0; per-page dedupe must collapse them."""
        from smart_slice import slice_multimodal

        result = slice_multimodal(make_bookmarked_pdf(nested=True), "manual.pdf", limit=100000)
        digests = [a.sha256 for a in result.images]
        self.assertEqual(len(digests), len(set(digests)))


@unittest.skipUnless(HAS_PYPDF, "smart-slice[pdf] not installed")
class ChapterTitleTests(unittest.TestCase):
    def test_arabic_multilevel_numbering_is_stripped(self):
        self.assertEqual(PdfSplitHandle.handle_chapter_title("3.2.1 Creating a datum plane"),
                         "Creating a datum plane")

    def test_cjk_numbering_is_still_stripped(self):
        self.assertEqual(PdfSplitHandle.handle_chapter_title(u"\u7b2c\u4e09\u7ae0 \u5efa\u6a21"), u"\u5efa\u6a21")

    def test_a_purely_numeric_title_never_becomes_an_empty_search_key(self):
        """text.find("") returns 0, which would slice the chapter body away entirely."""
        self.assertEqual(PdfSplitHandle.handle_chapter_title("2024"), "2024")

    def test_unnumbered_title_is_untouched(self):
        self.assertEqual(PdfSplitHandle.handle_chapter_title("Overview"), "Overview")


if __name__ == "__main__":
    unittest.main()
