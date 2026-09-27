# coding=utf-8
"""Public API surface tests (the package's own additions, not ported).

Covers the convenience entry points layered on top of the ported service core:
slice_text / slice_bytes / slice_path / extract_text / detect_handler /
supported_extensions / chunk / chunk_paragraphs, plus the documented fidelity
guarantees (title chain, no source-text loss) at the public boundary.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

import smart_slice as ss
from smart_slice import (
    ImageAsset,
    SliceError,
    UnsupportedFormatError,
    slice_bytes,
    slice_text,
)


class SliceTextTests(unittest.TestCase):
    def test_heading_tree_produces_title_chain(self):
        rows = slice_text("# A\n\nbody a\n\n## B\n\nbody b", limit=1000)
        titles = [r["title"] for r in rows]
        self.assertIn("A", titles[0])
        # nested heading carries the full parent chain
        self.assertTrue(any("A" in t and "B" in t for t in titles), titles)

    def test_blank_line_splitting_without_headings(self):
        rows = slice_text("first para.\n\nsecond para.\n\nthird para.", limit=1000)
        joined = "\n\n".join(r["content"] for r in rows)
        for needle in ("first para.", "second para.", "third para."):
            self.assertIn(needle, joined)

    def test_limit_breaks_long_body(self):
        rows = slice_text("x" * 5000, limit=200, name="t.txt")
        self.assertTrue(all(len(r["content"]) <= 200 for r in rows))
        self.assertGreater(len(rows), 1)

    def test_with_filter_strips_heading_marker_only_at_line_start(self):
        rows = slice_text("keep #tag inline", limit=1000, with_filter=True, name="t.txt")
        self.assertIn("#tag", rows[0]["content"])


class SliceBytesTests(unittest.TestCase):
    def test_unsupported_format_raises(self):
        with self.assertRaises(SliceError):
            slice_bytes(b"\x00\x01\x02binary", "movie.mp4")

    def test_normalize_false_keeps_handler_structure(self):
        raw = slice_bytes("# Hi\n\nbody".encode("utf-8"), "doc.md", normalize=False)
        self.assertIsInstance(raw, dict)
        self.assertIn("content", raw)

    def test_save_image_callback_receives_assets(self):
        # a docx with an inline image is built in test_fidelity; here we only assert
        # the callback contract using a text file (no images) -> callback untouched
        seen = []
        slice_bytes(b"plain text body", "f.txt", save_image=lambda imgs: seen.extend(imgs))
        self.assertEqual(seen, [])

    def test_markdown_round_trip_via_bytes(self):
        rows = slice_bytes(b"# T\n\nhello world", "x.md")
        self.assertTrue(any("hello world" in r["content"] for r in rows))


class ExtractAndDetectTests(unittest.TestCase):
    def test_extract_text_returns_string(self):
        text = ss.extract_text(b"# Title\n\nsome body", "a.md")
        self.assertIn("some body", text)

    def test_detect_handler_by_extension_and_content(self):
        self.assertEqual(ss.detect_handler("a.pdf"), "PdfSplitHandle")
        self.assertEqual(ss.detect_handler("a.docx"), "DocSplitHandle")
        # unknown extension but decodable text -> TextSplitHandle fallback
        self.assertEqual(ss.detect_handler("notes.weird", b"just text"), "TextSplitHandle")
        self.assertIsNone(ss.detect_handler("movie.mp4", b"\x00\x00\x00\x18ftypmp42"))

    def test_supported_extensions_shape(self):
        ext = ss.supported_extensions()
        self.assertIn("PdfSplitHandle", ext)
        self.assertIn(".pdf", ext["PdfSplitHandle"])


class ChunkTests(unittest.TestCase):
    def test_chunk_splits_long_string(self):
        pieces = ss.chunk("abcdefghij" * 50, chunk_size=100)
        self.assertTrue(all(len(p) <= 100 for p in pieces))

    def test_chunk_paragraphs_skips_empty(self):
        out = ss.chunk_paragraphs(
            [{"title": "a", "content": "word " * 100}, {"title": "", "content": ""}],
            chunk_size=50,
        )
        self.assertTrue(out)


class CLITests(unittest.TestCase):
    def _run(self, *args):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run(
            [sys.executable, "-m", "smart_slice", *args],
            capture_output=True, text=True, encoding="utf-8", env=env, cwd=repo,
        )

    def test_slice_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write("# H\n\nbody text here")
            path = fh.name
        try:
            proc = self._run("slice", path, "--format", "json")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = json.loads(proc.stdout)
            self.assertTrue(any("body text here" in r["content"] for r in rows))
        finally:
            os.unlink(path)

    def test_slice_text_and_stats(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
            fh.write("alpha\n\nbeta\n\ngamma")
            path = fh.name
        try:
            proc = self._run("slice", path, "--format", "text", "--stats", "--limit", "1000")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("alpha", proc.stdout)
            self.assertIn("paragraphs", proc.stderr)
        finally:
            os.unlink(path)

    def test_detect_command(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write("# x\n\ny")
            path = fh.name
        try:
            proc = self._run("detect", path)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("SplitHandle", proc.stdout)
        finally:
            os.unlink(path)

    def test_formats_command_json(self):
        proc = self._run("formats", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("handlers", payload)
        self.assertIn("missing_optional", payload)


if __name__ == "__main__":
    unittest.main()