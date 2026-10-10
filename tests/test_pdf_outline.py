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


# ---------------------------------------------------------------------------
# Post-review regression coverage (2026-10-10, second round).
#
# An adversarial review of the fixes above found that three of them were either
# harmful as written or untested in a way that could not detect a regression:
#
#   F1  Chapter location ran an *unanchored* text.find(stripped_short_title) on
#       every page of the interval.  Once numbering is stripped the key becomes an
#       ordinary body word ("4 Requirements" -> "Requirements"), so a hit inside a
#       running header, a footer or plain prose silently deleted the whole page
#       (measured: a chapter kept 4.1% of its body; another lost its "Warning: must
#       be earthed" line).  Now: anchored hits only, head trim on the chapter's
#       first page only, tail trim on its last page only, located *before* the
#       newline erasure, and the fallback key confined to a window at the top of
#       the page.  The tail cut uses the numbered original only.
#   M3  Numbering stripping was too wide (optional trailing separator *and*
#       optional whitespace), rewriting "1.5 m clearance" / "5G network" /
#       "3M tape" / "2024 annual report" / "Chapter 2 3D printing" into crippled
#       fragments.  Now only unambiguous numbering is stripped.
#   M1  A page image was attached to the *first* outline entry covering the page.
#       Bookmarks are parent-first and a chapter opener shares its page with the
#       first subsection, so images landed in the parent chunk - which after
#       trimming often holds nothing but its title - while the subsection that
#       discusses the figure got none.  Now ownership goes to the last (deepest,
#       most specific) entry covering the page.
#   M2  Image markdown references entered smart_split_paragraph for the first time
#       and were cut in half by '.' / '!' (both are sentence boundaries), which
#       silently breaks downstream id rewriting and OCR injection.
#   M7  An out-of-range /Dest page number raised IndexError and lost the whole file.
#
# The previous image assertions counted occurrences of source text and checked
# co_varnames; a mutation moving imaged_pages from function scope into the loop
# (which disables the per-page dedupe) passed all of them.  Everything below runs
# the real code against real fixtures instead.
# ---------------------------------------------------------------------------
import re as _re  # noqa: E402

from smart_slice.chunker import smart_split_paragraph  # noqa: E402

_IMAGE_REF = _re.compile(r"!\[[^\]]*\]\(\./oss/file/[^)]*\)")


def _escape_pdf_string(raw):
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _page(lines, jpeg=None):
    """One hand-rolled page: each line becomes its own text run (pypdf joins them
    with newlines), plus an optional real DCTDecode image XObject."""
    parts = []
    y = 350
    for line in lines:
        parts.append(b"BT /F1 12 Tf 40 " + str(y).encode() + b" Td ("
                     + _escape_pdf_string(line) + b") Tj ET\n")
        y -= 20
    if jpeg is not None:
        parts.append(b"q 60 0 0 60 40 40 cm /Im1 Do Q\n")
    content = b"".join(parts)
    resources = b"<< /Font << /F1 5 0 R >> "
    if jpeg is not None:
        resources += b"/XObject << /Im1 6 0 R >> "
    resources += b"/ProcSet [/PDF /Text /ImageC] >>"
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 400] /Resources "
        + resources + b" /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    if jpeg is not None:
        bodies.append(
            b"<< /Type /XObject /Subtype /Image /Width 8 /Height 8 /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % len(jpeg)
            + jpeg + b"\nendstream")
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


def _doc(pages, outline):
    """pages: [(lines, jpeg|None)]; outline: [(title, page_index, parent_index|None)]."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for lines, jpeg in pages:
        writer.add_page(PdfReader(io.BytesIO(_page(lines, jpeg))).pages[0])
    handles = []
    for title, page_number, parent_index in outline:
        parent = handles[parent_index] if parent_index is not None else None
        handles.append(writer.add_outline_item(title, page_number, parent=parent))
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _run(data, limit=100000, image_list=None, progress_hook=None):
    from pypdf import PdfReader

    return PdfSplitHandle.handle_toc(PdfReader(io.BytesIO(data)), limit,
                                     progress_hook=progress_hook, image_list=image_list)


def _jpeg_of(shade):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), shade).save(buf, format="JPEG")
    return buf.getvalue()


@unittest.skipUnless(HAS_PYPDF and HAS_PIL, "pypdf and Pillow are required")
class HeadingLocationTests(unittest.TestCase):
    """F1: the cut must be anchored, page-scoped, and happen before newline erasure."""

    def test_a_heading_word_in_a_later_page_body_is_not_a_heading(self):
        data = _doc(
            [([b"3.1 Data", b"This section lists the recorded values."], None),
             ([b"Metadata is listed below.", b"See annex A for details."], None)],
            [("3.1 Data", 0, None)])
        content = _run(data)[0]["content"]
        for fragment in ("This section lists the recorded values.",
                         "Metadata is listed below.", "See annex A for details."):
            self.assertIn(fragment, content)

    def test_the_chapter_heading_itself_is_still_trimmed(self):
        data = _doc([([b"3.1 Data", b"This section lists the recorded values."], None)],
                    [("3.1 Data", 0, None)])
        content = _run(data)[0]["content"]
        self.assertNotIn("3.1 Data", content)
        self.assertIn("This section lists the recorded values.", content)

    def test_a_mention_of_the_next_section_does_not_truncate_the_current_one(self):
        data = _doc(
            [([b"2 Install", b"Read the Commissioning section first.", b"Step one: power off.",
               b"Warning: must be earthed."], None),
             ([b"3.1 Commissioning", b"Power on test."], None)],
            [("2 Install", 0, None), ("3.1 Commissioning", 1, None)])
        content = _run(data)[0]["content"]
        for sentence in ("Read the Commissioning section first.", "Step one: power off.",
                         "Warning: must be earthed."):
            self.assertIn(sentence, content)

    def test_a_sibling_heading_on_the_same_page_still_bounds_the_parent(self):
        data = _doc(
            [([b"1 Safety", b"1.1 Pre-installation check", b"Earth the terminal before wiring."],
              None)],
            [("1 Safety", 0, None), ("1.1 Pre-installation check", 0, 0)])
        chapters = _run(data)
        self.assertNotIn("Earth the terminal", chapters[0]["content"])
        self.assertIn("Earth the terminal before wiring.", chapters[1]["content"])

    def test_head_trim_does_not_apply_to_later_pages(self):
        """A running footer repeating the heading must not eat that page's body."""
        data = _doc([([b"3.1 Data", b"alpha body"], None), ([b"beta body", b"3.1 Data"], None)],
                    [("3.1 Data", 0, None)])
        content = _run(data)[0]["content"]
        self.assertIn("alpha body", content)
        self.assertIn("beta body", content)

    def test_tail_trim_does_not_apply_to_earlier_pages(self):
        data = _doc(
            [([b"2 Install", b"3.1 Commissioning", b"alpha body"], None),
             ([b"gamma body"], None),
             ([b"3.1 Commissioning", b"delta body"], None)],
            [("2 Install", 0, None), ("3.1 Commissioning", 2, None)])
        content = _run(data)[0]["content"]
        self.assertIn("alpha body", content)
        self.assertIn("gamma body", content)

    def test_an_unanchored_heading_word_on_the_first_page_is_not_trimmed(self):
        data = _doc([([b"Introduction", b"Pre-Check the valve before wiring."], None)],
                    [("3.1 Check", 0, None)])
        self.assertIn("Pre-Check the valve before wiring.", _run(data)[0]["content"])

    def test_an_anchored_heading_word_on_the_first_page_is_trimmed(self):
        data = _doc([([b"Introduction", b"Check the valve before wiring."], None)],
                    [("3.1 Check", 0, None)])
        content = _run(data)[0]["content"]
        self.assertIn("the valve before wiring.", content)
        self.assertNotIn("Check the valve", content)

    def test_the_fallback_key_is_confined_to_the_top_of_the_page(self):
        filler = [b"Introduction paragraph that goes on for a while."] * 6
        data = _doc([(filler + [b"Data logging is optional."], None)], [("3.1 Data", 0, None)])
        content = _run(data)[0]["content"]
        self.assertIn("Introduction paragraph that goes on for a while.", content)
        self.assertIn("Data logging is optional.", content)

    def test_an_empty_search_key_is_skipped_by_the_locator(self):
        self.assertEqual(PdfSplitHandle._locate_heading("body text", ("",)), (-1, -1))
        self.assertEqual(PdfSplitHandle._locate_heading("Alpha then body", ("", "Alpha")), (0, 5))


@unittest.skipUnless(HAS_PYPDF and HAS_PIL, "pypdf and Pillow are required")
class NumberingStripTests(unittest.TestCase):
    """M3: strip numbering, never a title that merely starts with a digit."""

    STRIPPED = [("3.2.1 Creating a datum plane", "Creating a datum plane"),
                ("3.2 Datum plane", "Datum plane"),
                ("2-1 Assembly drawing", "Assembly drawing"),
                ("2026.10.10 Change log", "Change log"),
                ("10. Precautions", "Precautions"),
                ("1)Installation steps", "Installation steps"),
                ("3、维护", "维护"),
                ("1.Overview", "Overview"),
                ("3.2.1", "")]
    PRESERVED = ["4 Requirements", "1 Scope", "01 Overview", "8 号工位", "12 inch wafer",
                 "100 mL flask", "1.5米以内", "5G network", "3M tape", "2024年度报告",
                 "3.14圆周率", "2.4G模组", "ISO 9001 requirements", "第3章 建模", "2D drawing",
                 "2024", "A-3 Widget", "V2.0 release notes"]

    def test_unambiguous_numbering_is_stripped(self):
        for source, expected in self.STRIPPED:
            with self.subTest(title=source):
                self.assertEqual(PdfSplitHandle.handle_chapter_title(source), expected)

    def test_digit_led_titles_are_preserved(self):
        for source in self.PRESERVED:
            with self.subTest(title=source):
                self.assertEqual(PdfSplitHandle.handle_chapter_title(source), source)

    def test_stripping_rules_do_not_stack_into_a_broken_word(self):
        self.assertEqual(PdfSplitHandle.handle_chapter_title("第二章 3D 打印"), "3D 打印")


@unittest.skipUnless(HAS_PYPDF and HAS_PIL, "pypdf and Pillow are required")
class ImageOwnershipTests(unittest.TestCase):
    """M1: an illustration belongs to the most specific bookmark covering its page."""

    def test_an_illustration_goes_to_the_deepest_entry_on_its_page(self):
        data = _doc(
            [([b"1 Safety", b"1.1 Pre-installation check", b"Earth the terminal before wiring."],
              _jpeg_of((30, 120, 200))),
             ([b"1.2 Running", b"Do not open the guard."], None)],
            [("1 Safety", 0, None), ("1.1 Pre-installation check", 0, 0), ("1.2 Running", 1, 0)])
        assets = []
        chapters = _run(data, image_list=assets)
        by_title = {c["title"]: c["content"] for c in chapters}
        self.assertNotIn("./oss/file/", by_title["1 Safety"])
        self.assertIn("./oss/file/", by_title["1 Safety 1.1 Pre-installation check"])
        self.assertIn("Earth the terminal before wiring.",
                      by_title["1 Safety 1.1 Pre-installation check"])

    def test_a_page_is_decoded_only_once(self):
        data = _doc([([b"1 Safety", b"1.1 Check", b"body"], _jpeg_of((10, 200, 90)))],
                    [("1 Safety", 0, None), ("1.1 Check", 0, 0)])
        assets = []
        _run(data, image_list=assets)
        self.assertEqual(len(assets), 1)

    def test_image_extraction_reports_progress(self):
        data = _doc([([b"1 Safety", b"body"], _jpeg_of((10, 20, 210))),
                     ([b"1.2 Running", b"body"], None)],
                    [("1 Safety", 0, None), ("1.2 Running", 1, 0)])
        calls = []
        _run(data, image_list=[], progress_hook=lambda: calls.append(1))
        self.assertGreaterEqual(len(calls), 3)


@unittest.skipUnless(HAS_PYPDF and HAS_PIL, "pypdf and Pillow are required")
class BookmarkPageRangeTests(unittest.TestCase):
    """M7: an out-of-range /Dest must not cost the whole document."""

    def test_collect_toc_skips_an_out_of_range_destination(self):
        from pypdf import PdfReader

        data = _doc([([b"only page"], None)], [("Alpha", 0, None)])
        reader = PdfReader(io.BytesIO(data))
        collected = []
        PdfSplitHandle.collect_toc(reader, reader.outline, 1, collected)
        self.assertEqual([entry[2] for entry in collected], [0])

        class _OutOfRange:
            title = "Beta"

        real = PdfSplitHandle.get_destination_page_number
        PdfSplitHandle.get_destination_page_number = staticmethod(lambda doc, item: 99)
        try:
            leaked = []
            PdfSplitHandle.collect_toc(reader, [_OutOfRange()], 1, leaked)
        finally:
            PdfSplitHandle.get_destination_page_number = real
        self.assertEqual(leaked, [])


class ImageReferenceSplitTests(unittest.TestCase):
    """M2: a markdown image reference must never be cut in half."""

    REF = "![p0.png](./oss/file/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee)"
    BODY = "sentence. " * 600  # longer than every limit under test, so splitting really happens

    def _scan(self, limit):
        broken = lost = unsplit = 0
        for position in range(0, len(self.BODY), 7):
            content = self.BODY[:position] + self.REF + self.BODY[position:]
            parts = smart_split_paragraph(content, limit)
            if len(parts) < 2:
                unsplit += 1
            if "".join(parts) != content:
                lost += 1
            if sum(len(_IMAGE_REF.findall(p)) for p in parts) != 1:
                broken += 1
        return broken, lost, unsplit

    def test_no_reference_is_split_at_limit_1000(self):
        broken, lost, unsplit = self._scan(1000)
        self.assertEqual(unsplit, 0, "corpus never reached the limit; the assertion would be vacuous")
        self.assertEqual((broken, lost), (0, 0))

    def test_no_reference_is_split_at_limit_4096(self):
        broken, lost, unsplit = self._scan(4096)
        self.assertEqual(unsplit, 0, "corpus never reached the limit; the assertion would be vacuous")
        self.assertEqual((broken, lost), (0, 0))

    def test_an_oversized_reference_does_not_hang(self):
        content = "sentence. " * 10 + "![x](./oss/file/" + "f" * 3000 + ")" + "sentence. " * 10
        self.assertEqual("".join(smart_split_paragraph(content, 200)), content)

    def test_an_unterminated_bang_bracket_does_not_move_cut_points(self):
        """'![' is common in shell (``![ -f x ]``), C (``a![i]``) and regex lookaheads
        (``(?![0-9])``). Only well-formed references may influence the split point."""
        for text in ("if ![ -f /tmp/x ]; then echo ok; fi\n" * 300,
                     "int v = a![i]; printf(\"%d\\n\", v);\n" * 300,
                     "pattern = r\"(?![0-9])\\w+\"\n" * 300,
                     "note: ![not an image reference, no closing paren\n" * 300):
            for limit in (200, 500, 1000, 4096):
                parts = smart_split_paragraph(text, limit)
                self.assertEqual("".join(parts), text)

    def test_the_markdown_table_protection_is_unaffected(self):
        content = ("| a | b |\n| --- | --- |\n"
                   + "".join("| r%d | v%d |\n" % (i, i) for i in range(400)))
        self.assertEqual("".join(smart_split_paragraph(content, 300)), content)