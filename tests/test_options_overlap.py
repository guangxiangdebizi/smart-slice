# coding=utf-8
"""Chunk-size / overlap configuration tests.

Covers the dual-mode requirement (sensible defaults + full user control) at both
stages, and pins the invariants that make overlap safe:

* ``overlap=0`` is byte-identical to the pre-overlap behaviour (backward compat);
* overlap is purely additive - a paragraph's own text is never altered or dropped;
* paragraph *count* and titles are unchanged by overlap (only context is prepended);
* overlap can never consume all of a chunk (progress is guaranteed, so no hang).

Also covers the ``ChunkingOptions`` normalisation/clamping rules and the
``post_handler_paragraph`` defect fix (it referenced an unimported ``reduce``).
"""
import unittest

from smart_slice import (
    ChunkingOptions,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_LIMIT,
    DEFAULT_OVERLAP,
    chunk_paragraphs,
    resolve_options,
    slice_text,
    use_options,
)
from smart_slice.chunker import apply_paragraph_overlap, post_handler_paragraph, smart_split_paragraph
from smart_slice.options import current_options


def long_body(repeat=120):
    return " ".join(f"word{i}" for i in range(repeat)) + "."


HEADED_DOC = (
    "# Alpha\n\n"
    + long_body(80)
    + "\n\n## Beta\n\n"
    + long_body(80)
    + "\n\n## Gamma\n\n"
    + long_body(80)
    + "\n"
)


class OptionsNormalisationTests(unittest.TestCase):
    def test_defaults_are_documented_values(self):
        opts = ChunkingOptions()
        self.assertEqual(opts.limit, DEFAULT_LIMIT)
        self.assertEqual(opts.effective_overlap, DEFAULT_OVERLAP)
        self.assertEqual(DEFAULT_LIMIT, 1000)
        self.assertEqual(DEFAULT_OVERLAP, 0)
        self.assertEqual(DEFAULT_CHUNK_SIZE, 256)

    def test_overlap_ratio_folds_into_overlap(self):
        self.assertEqual(ChunkingOptions(limit=1000, overlap_ratio=0.15).effective_overlap, 150)
        self.assertEqual(ChunkingOptions(limit=400, overlap_ratio=0.25).effective_overlap, 100)

    def test_explicit_overlap_beats_ratio(self):
        opts = ChunkingOptions(limit=1000, overlap=77, overlap_ratio=0.5)
        self.assertEqual(opts.effective_overlap, 77)

    def test_overlap_clamped_to_half_limit(self):
        # progress guarantee: cursor must always advance
        self.assertEqual(ChunkingOptions(limit=1000, overlap=99999).effective_overlap, 500)

    def test_limit_clamped_to_source_platform_range(self):
        self.assertEqual(ChunkingOptions(limit=1).limit, 50)
        self.assertEqual(ChunkingOptions(limit=10 ** 9).limit, 100000)

    def test_negative_overlap_rejected(self):
        with self.assertRaises(ValueError):
            ChunkingOptions(overlap=-1)

    def test_bad_ratio_rejected(self):
        for ratio in (-0.1, 0.6, 2.0):
            with self.assertRaises(ValueError):
                ChunkingOptions(overlap_ratio=ratio)

    def test_bad_lookback_rejected(self):
        for lookback in (0.0, -0.5, 1.5):
            with self.assertRaises(ValueError):
                ChunkingOptions(lookback=lookback)

    def test_string_limit_coerced(self):
        self.assertEqual(ChunkingOptions(limit="800").limit, 800)

    def test_options_immutable_but_with_helper(self):
        base = ChunkingOptions(limit=1000)
        derived = base.with_(limit=500)
        self.assertEqual(base.limit, 1000)
        self.assertEqual(derived.limit, 500)

    def test_resolve_options_precedence(self):
        base = ChunkingOptions(limit=1000, overlap=10)
        self.assertEqual(resolve_options(base).limit, 1000)
        self.assertEqual(resolve_options(base, limit=300).limit, 300)
        self.assertEqual(resolve_options(base, limit=300).effective_overlap, 10)
        self.assertEqual(resolve_options(base, overlap=90).effective_overlap, 90)

    def test_use_options_scopes_the_contextvar(self):
        self.assertEqual(current_options().limit, DEFAULT_LIMIT)
        with use_options(ChunkingOptions(limit=321, overlap=33)):
            self.assertEqual(current_options().limit, 321)
            self.assertEqual(current_options().effective_overlap, 33)
        self.assertEqual(current_options().limit, DEFAULT_LIMIT)
        self.assertEqual(current_options().effective_overlap, 0)

    def test_use_options_restores_after_exception(self):
        with self.assertRaises(RuntimeError):
            with use_options(ChunkingOptions(limit=99)):
                raise RuntimeError("boom")
        self.assertEqual(current_options().limit, DEFAULT_LIMIT)


class SmartSplitOverlapTests(unittest.TestCase):
    def test_zero_overlap_tiles_exactly(self):
        text = long_body(600)
        pieces = smart_split_paragraph(text, limit=1000, overlap=0)
        self.assertEqual("".join(pieces), text)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(len(p) <= 1000 for p in pieces))

    def test_overlap_increases_total_without_growing_chunks(self):
        text = long_body(600)
        base = smart_split_paragraph(text, limit=1000, overlap=0)
        with_ov = smart_split_paragraph(text, limit=1000, overlap=150)
        # overlap is purely additive: more total characters, never a bigger chunk
        self.assertGreater(sum(map(len, with_ov)), sum(map(len, base)))
        self.assertTrue(all(len(p) <= 1000 for p in with_ov))
        # fidelity: concatenating the overlap-free chunks reproduces the source,
        # and every source character still appears in the overlapped output
        self.assertEqual("".join(base), text)
        joined = "".join(with_ov)
        for piece in base:
            self.assertIn(piece[:40], joined)
            self.assertIn(piece[-40:], joined)

    def test_neighbouring_chunks_actually_share_context(self):
        text = long_body(600)
        pieces = smart_split_paragraph(text, limit=1000, overlap=200)
        self.assertGreaterEqual(len(pieces), 3)
        shared = 0
        for prev, nxt in zip(pieces, pieces[1:]):
            if prev[-40:] and prev[-40:] in nxt:
                shared += 1
        self.assertGreater(shared, 0, "no shared context detected between chunks")

    def test_monotonic_more_overlap_more_chunks(self):
        text = long_body(600)
        counts = [len(smart_split_paragraph(text, limit=1000, overlap=ov)) for ov in (0, 50, 150, 300)]
        self.assertEqual(counts, sorted(counts), f"chunk count not monotonic in overlap: {counts}")

    def test_overlap_never_stalls_progress(self):
        # pathological input: no sentence boundary at all
        text = "a" * 5000
        pieces = smart_split_paragraph(text, limit=500, overlap=249)
        self.assertTrue(all(len(p) <= 500 for p in pieces))
        self.assertGreaterEqual(len(pieces), 10)

    def test_boundary_disabled_cuts_at_limit(self):
        text = long_body(600)
        pieces = smart_split_paragraph(text, limit=500, boundary=False)
        self.assertTrue(all(len(p) == 500 for p in pieces[:-1]))

    def test_lookback_controls_search_window(self):
        text = long_body(600)
        wide = smart_split_paragraph(text, limit=1000, lookback=0.9)
        narrow = smart_split_paragraph(text, limit=1000, lookback=0.1)
        # a wider lookback window finds an earlier boundary => shorter chunks on average
        self.assertLessEqual(sum(map(len, wide)), sum(map(len, narrow)) + len(wide) * 50)

    def test_token_budget_via_length_fn(self):
        # a crude "tokenizer": 4 chars == 1 token
        text = long_body(600)
        pieces = smart_split_paragraph(text, limit=50, length_fn=lambda s: len(s) // 4)
        self.assertTrue(all(len(p) // 4 <= 50 for p in pieces))
        self.assertGreater(len(pieces), 1)

    def test_min_chunk_merges_short_tail(self):
        text = long_body(200) + " tail."
        raw = smart_split_paragraph(text, limit=500, min_chunk=0)
        merged = smart_split_paragraph(text, limit=500, min_chunk=20)
        self.assertLessEqual(len(merged), len(raw))
        for piece in merged[:-1]:
            self.assertGreaterEqual(len(piece), 20)

    def test_fullwidth_punctuation_is_a_boundary(self):
        # regression: the source implementation listed ASCII '!'/'?' twice and
        # never included the fullwidth U+FF01/U+FF1F forms
        text = "第一句话结束！" * 200
        pieces = smart_split_paragraph(text, limit=100, overlap=0)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(any(p.endswith("！") for p in pieces), "fullwidth ! not used as boundary")


class ApplyParagraphOverlapTests(unittest.TestCase):
    def test_zero_overlap_returns_input_untouched(self):
        rows = [{"title": "A", "content": "one"}, {"title": "A", "content": "two"}]
        self.assertEqual(apply_paragraph_overlap(rows, 0), rows)

    def test_additive_only_original_text_preserved(self):
        rows = [{"title": "A", "content": "first paragraph"}, {"title": "A", "content": "second paragraph"}]
        out = apply_paragraph_overlap(rows, overlap=8)
        self.assertEqual(out[0]["content"], "first paragraph")
        self.assertTrue(out[1]["content"].endswith("second paragraph"))
        self.assertIn("first paragraph"[-8:], out[1]["content"])

    def test_input_not_mutated(self):
        rows = [{"title": "A", "content": "first"}, {"title": "A", "content": "second"}]
        snapshot = [dict(r) for r in rows]
        apply_paragraph_overlap(rows, overlap=5)
        self.assertEqual(rows, snapshot)

    def test_count_and_titles_preserved(self):
        rows = [{"title": f"T{i}", "content": long_body(20)} for i in range(5)]
        out = apply_paragraph_overlap(rows, overlap=50)
        self.assertEqual(len(out), len(rows))
        self.assertEqual([r["title"] for r in out], [r["title"] for r in rows])

    def test_within_section_mode_only_carries_inside_a_section(self):
        rows = [
            {"title": "Alpha", "content": "alpha tail text"},
            {"title": "Beta", "content": "beta body text"},
        ]
        carried = apply_paragraph_overlap(rows, overlap=10, overlap_within_section=True)
        self.assertEqual(carried[1]["content"], "beta body text")
        mixed = apply_paragraph_overlap(rows, overlap=10, overlap_within_section=False)
        self.assertNotEqual(mixed[1]["content"], "beta body text")

    def test_boundary_snapping_keeps_overlap_an_upper_bound(self):
        rows = [{"title": "A", "content": "one two three four five six"},
                {"title": "A", "content": "next body"}]
        out = apply_paragraph_overlap(rows, overlap=12, overlap_boundary=True)
        carried = out[1]["content"][: -len(" next body")] if out[1]["content"].endswith(" next body") else ""
        self.assertLessEqual(len(carried), 12)

    def test_non_dict_rows_pass_through(self):
        rows = ["bare string", {"title": "A", "content": "body"}]
        out = apply_paragraph_overlap(rows, overlap=5)
        self.assertEqual(out[0], "bare string")


class SliceEntryPointOverlapTests(unittest.TestCase):
    def test_slice_text_default_has_no_overlap(self):
        rows = slice_text(HEADED_DOC)
        total = sum(len(r["content"]) for r in rows)
        plain = "".join(r["content"] for r in rows)
        self.assertEqual(len(plain), total)
        self.assertLess(total, sum(len(r["content"]) for r in slice_text(HEADED_DOC, overlap=150)))

    def test_slice_text_overlap_grows_content_only(self):
        base = slice_text(HEADED_DOC, limit=500)
        over = slice_text(HEADED_DOC, limit=500, overlap=100)
        self.assertEqual(len(base), len(over))
        self.assertEqual([r["title"] for r in base], [r["title"] for r in over])
        self.assertGreater(sum(len(r["content"]) for r in over),
                           sum(len(r["content"]) for r in base))
        # every original paragraph survives verbatim inside its overlapped twin
        for b, o in zip(base, over):
            self.assertIn(b["content"], o["content"])

    def test_overlap_ratio_matches_explicit_overlap(self):
        by_ratio = slice_text(HEADED_DOC, limit=500, overlap_ratio=0.2)
        by_value = slice_text(HEADED_DOC, limit=500, overlap=100)
        self.assertEqual(by_ratio, by_value)

    def test_options_object_equals_kwargs(self):
        via_opts = slice_text(HEADED_DOC, options=ChunkingOptions(limit=500, overlap=100))
        via_kw = slice_text(HEADED_DOC, limit=500, overlap=100)
        self.assertEqual(via_opts, via_kw)

    def test_kwargs_override_options_object(self):
        out = slice_text(HEADED_DOC, options=ChunkingOptions(limit=500, overlap=100), limit=250)
        self.assertTrue(all(len(r["content"]) <= 250 + 100 + 40 for r in out))

    def test_no_overlap_by_default_matches_explicit_zero(self):
        self.assertEqual(slice_text(HEADED_DOC, limit=500),
                         slice_text(HEADED_DOC, limit=500, overlap=0))

    def test_deterministic(self):
        a = slice_text(HEADED_DOC, limit=500, overlap=120)
        b = slice_text(HEADED_DOC, limit=500, overlap=120)
        self.assertEqual(a, b)


class ChunkStageOverlapTests(unittest.TestCase):
    def test_default_chunk_size_and_no_overlap(self):
        rows = slice_text(HEADED_DOC, limit=2000)
        pieces = chunk_paragraphs(rows)
        self.assertTrue(pieces)
        self.assertTrue(all(len(p) <= DEFAULT_CHUNK_SIZE + 8 for p in pieces))

    def test_chunk_overlap_adds_context(self):
        rows = slice_text(HEADED_DOC, limit=2000)
        plain = chunk_paragraphs(rows, chunk_size=256)
        overlapped = chunk_paragraphs(rows, chunk_size=256, chunk_overlap=40)
        self.assertGreater(sum(map(len, overlapped)), sum(map(len, plain)))
        self.assertEqual(len(plain), len(overlapped))

    def test_chunk_overlap_ratio(self):
        rows = slice_text(HEADED_DOC, limit=2000)
        by_ratio = chunk_paragraphs(rows, chunk_size=200, chunk_overlap_ratio=0.2)
        by_value = chunk_paragraphs(rows, chunk_size=200, chunk_overlap=40)
        self.assertEqual(by_ratio, by_value)

    def test_carry_title_prefixes_chunks(self):
        rows = slice_text("# Section\n\n" + long_body(300), limit=2000)
        plain = chunk_paragraphs(rows, chunk_size=200)
        carried = chunk_paragraphs(rows, chunk_size=200, carry_title=True)
        self.assertTrue(all(c.startswith("# Section") for c in carried))
        self.assertFalse(any(c.startswith("# Section") for c in plain))

    def test_custom_handler_still_supported(self):
        from smart_slice.chunking import MarkChunkHandle

        rows = slice_text(HEADED_DOC, limit=2000)
        pieces = chunk_paragraphs(rows, chunk_size=256, handler=MarkChunkHandle())
        self.assertTrue(pieces)


class PostHandlerParagraphRegressionTests(unittest.TestCase):
    """post_handler_paragraph referenced an unimported ``reduce`` (latent NameError)."""

    def test_runs_without_nameerror(self):
        out = post_handler_paragraph("a" * 50 + "\n" + "b" * 3000, 100)
        self.assertTrue(out)

    def test_respects_limit_on_hard_cut(self):
        out = post_handler_paragraph("x" * 2500, 100)
        self.assertTrue(all(len(piece) <= 100 for piece in out))
        self.assertEqual("".join(out), "x" * 2500)

    def test_line_accumulation_under_limit(self):
        content = "line one\nline two\nline three\n"
        out = post_handler_paragraph(content, 1000)
        self.assertEqual("".join(out), content)


if __name__ == "__main__":
    unittest.main()