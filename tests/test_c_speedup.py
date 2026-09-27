# coding=utf-8
"""Accelerator tests: the prefilter must never change slicing results.

The single-pass heading scan is an optimisation, and an optimisation that quietly
drops headings would be far worse than a slow parser.  Three properties are pinned:

1. **Scan equivalence** - the C extension (when built) and the pure-Python
   reference return identical candidates on systematic and fuzz corpora.
2. **Superset premise** - if the regex for heading level L matches anything, the
   scan must report a candidate with exactly L hashes.  This is what makes
   skipping a level safe: "scan says level L absent" implies "regex for L is empty".
3. **End-to-end equivalence** - slicing with the prefilter active produces output
   byte-identical to slicing with it disabled, across structured, adversarial and
   fuzz corpora.

Property 3 is the one that matters for users; 1 and 2 localise a regression.
"""
import random
import re
import sys
import unittest

import smart_slice.chunker as chunker
from smart_slice._accel import ACCELERATOR, heading_level_of, scan_heading_candidates
from smart_slice._speedup_py import scan_heading_candidates as python_scan
from smart_slice.chunker import mask_code_blocks
from smart_slice.patterns import BLANK_LINE, DEFAULT_PATTERNS, MARKDOWN_HEADINGS

HASH_ALPHABET = "#` \n\tabc-*|>12"


def systematic_corpora():
    """Every combination of hash count, indent, separator, tail and fence state."""
    out = []
    for hashes in range(0, 9):
        for indent in ("", " ", "\t", "   "):
            for sep in (" ", "\t", "", "  "):
                for tail in ("Title", "", "#", "x", "# more", "-*- coding: utf-8 -*-"):
                    for fence in (False, True):
                        body = indent + "#" * hashes + sep + tail
                        if fence:
                            body = "```\n" + body + "\n```"
                        out.append("# top\n\ntext\n\n" + body + "\n\nmore\n")
    return out


def fuzz_corpora(count=2500, seed=23):
    rnd = random.Random(seed)
    return [
        "".join(rnd.choice(HASH_ALPHABET) for _ in range(rnd.randint(0, 400)))
        for _ in range(count)
    ]


def regex_hits_per_level(text):
    """What parse_level would find for each canonical heading level."""
    masked = mask_code_blocks(text)
    hits = {}
    for level, pattern in enumerate(MARKDOWN_HEADINGS, 1):
        found = [h for h in re.findall(pattern, masked) if h is not None]
        # mirror parse_level's filtering of empty / hash-only matches
        found = [h for h in found if h.strip(" ") and h.replace("#", "").strip(" ")]
        hits[level] = found
    return hits


class ScanEquivalenceTests(unittest.TestCase):
    """C scan (if built) must agree with the Python reference exactly."""

    def test_accelerator_resolved(self):
        self.assertIn(ACCELERATOR, ("c", "python"))

    def test_systematic_agreement(self):
        if ACCELERATOR != "c":
            self.skipTest("C extension not built; python path is the reference")
        for text in systematic_corpora():
            self.assertEqual(
                scan_heading_candidates(text), python_scan(text), f"mismatch on {text!r}"
            )

    def test_fuzz_agreement(self):
        if ACCELERATOR != "c":
            self.skipTest("C extension not built; python path is the reference")
        for text in fuzz_corpora():
            self.assertEqual(
                scan_heading_candidates(text), python_scan(text), f"mismatch on {text!r}"
            )

    def test_scan_finds_real_headings(self):
        doc = "# A\n\nbody\n\n## B\n\nbody\n\n### C\n\nbody"
        levels = sorted(hashes for (_s, _e, hashes) in python_scan(doc))
        self.assertEqual(levels, [1, 2, 3])

    def test_scan_reports_line_bounds(self):
        doc = "# Alpha\n\ntext"
        start, end, hashes = python_scan(doc)[0]
        self.assertEqual(doc[start:end], "# Alpha")
        self.assertEqual(hashes, 1)

    def test_scan_ignores_indented_hashes(self):
        # the canonical patterns anchor on ^ or \n immediately before '#', so an
        # indented '#' is not a heading
        doc = "# Real\n\n  # indented\n\t## tabbed\n"
        levels = [hashes for (_s, _e, hashes) in python_scan(doc)]
        self.assertEqual(levels, [1])

    def test_scan_requires_space_after_hashes(self):
        doc = "#A\n\n## B\n\n###\n"
        levels = [hashes for (_s, _e, hashes) in python_scan(doc)]
        self.assertEqual(levels, [2])

    def test_scan_is_a_superset_for_coding_declarations(self):
        """The scan may over-report; the regex still rejects the declaration.

        A prefilter is only safe when it never *under*-reports, so the coding
        declaration stays a candidate at scan level and is filtered out later by
        the level-1 pattern's ``(?!--*- coding:)`` guard.
        """
        doc = "# -*- coding: utf-8 -*-\n\n# Real\n"
        levels = [hashes for (_s, _e, hashes) in python_scan(doc)]
        self.assertEqual(levels, [1, 1], "superset: both lines are scan candidates")
        hits = regex_hits_per_level(doc)
        self.assertEqual(hits[1], ["# Real"], "regex must reject the coding declaration")
        # and the end-to-end result must not treat it as a heading
        from smart_slice import slice_text

        rows = slice_text(doc, limit=1000)
        self.assertTrue(all("*-* coding" not in r["title"] for r in rows), rows)


class CanonicalPatternRecognitionTests(unittest.TestCase):
    def test_all_markdown_heading_levels_recognised(self):
        self.assertEqual(
            [heading_level_of(p) for p in MARKDOWN_HEADINGS], [1, 2, 3, 4, 5, 6]
        )

    def test_blank_line_pattern_not_recognised(self):
        self.assertIsNone(heading_level_of(BLANK_LINE[0]))

    def test_custom_pattern_not_recognised(self):
        self.assertIsNone(heading_level_of(re.compile(r"^SECTION \d+:.*", re.M)))
        self.assertIsNone(heading_level_of(re.compile(r"^(=+)\s*$")))

    def test_raw_string_accepted(self):
        level1 = MARKDOWN_HEADINGS[0].pattern
        self.assertEqual(heading_level_of(level1), 1)

    def test_default_patterns_recognised(self):
        levels = [heading_level_of(p) for p in DEFAULT_PATTERNS]
        self.assertEqual(levels, [1, 2, 3, 4, 5, 6, None])


class SupersetPremiseTests(unittest.TestCase):
    """If the regex matches level L, the scan must have reported a level-L candidate."""

    def _check(self, corpora, label):
        violations = 0
        for text in corpora:
            masked = mask_code_blocks(text)
            hits = regex_hits_per_level(text)
            scan_levels = {hashes for (_s, _e, hashes) in python_scan(masked)}
            for level, matched in hits.items():
                if matched and level not in scan_levels:
                    violations += 1
                    if violations <= 3:
                        self.fail(f"{label}: regex matched level {level} but scan missed it: {text[:90]!r}")
        self.assertEqual(violations, 0, f"{label}: prefilter would drop headings")

    def test_systematic(self):
        self._check(systematic_corpora(), "systematic")

    def test_fuzz(self):
        self._check(fuzz_corpora(), "fuzz")


class EndToEndEquivalenceTests(unittest.TestCase):
    """Slicing with and without the prefilter must produce identical output."""

    CORPORA = [
        # deep nesting at every level
        "\n\n".join(f"{'#' * lvl} Title {lvl}\n\nbody for level {lvl}." for lvl in range(1, 9)),
        # headings out of order (level jumps)
        "# A\n\ntext\n\n##### deep\n\ntext\n\n## back\n\ntext\n\n#### more\n\ntext\n",
        # code fences containing hashes
        (
            "# Real Heading\n\nintro\n\n```python\n# fake heading\n## also fake\n```\n\n"
            "## Real Sub\n\nbody\n\n```\n### fake\n```\n\n### Real SubSub\n\ntail\n"
        ),
        # coding declaration
        "# -*- coding: utf-8 -*-\n\n# Real Title\n\nbody\n\n## Sub\n\ntext\n",
        # tables and an oversized row
        (
            "# Report\n\n| a | b |\n| --- | --- |\n"
            + "\n".join(f"| {i} | {'x' * 900} |" for i in range(4))
            + "\n\n## Notes\n\n" + "sentence. " * 300
        ),
        # no headings at all
        "\n\n".join(f"paragraph {i} with some text." for i in range(50)),
        # heading with no body (the has_block_descendant guard)
        "# A\n\n# B\n\n## C\n\nbody\n",
        # indented hashes must not be headings
        "# A\n\n  # not a heading\n\t## also not\n\n## Real\n\ntext\n",
        # single huge block with no structure
        "y" * 9000,
        # empty and whitespace-only documents
        "",
        "\n\n\n",
    ]

    def setUp(self):
        self._original = chunker._heading_levels_present

    def tearDown(self):
        chunker._heading_levels_present = self._original
        chunker._SCAN_MEMO[0] = None
        chunker._SCAN_MEMO[1] = None

    def _slice_both_ways(self, doc, limit):
        from smart_slice import slice_text

        chunker._SCAN_MEMO[0] = None
        fast = slice_text(doc, limit=limit)

        # disable the prefilter: report "unknown" so no level is ever skipped
        chunker._heading_levels_present = lambda text: None
        chunker._SCAN_MEMO[0] = None
        slow = slice_text(doc, limit=limit)

        chunker._heading_levels_present = self._original
        return fast, slow

    def test_structured_corpora_identical(self):
        for i, doc in enumerate(self.CORPORA):
            for limit in (50, 200, 1000):
                fast, slow = self._slice_both_ways(doc, limit)
                self.assertEqual(fast, slow, f"corpus #{i} limit={limit} diverged")

    def test_fuzz_corpora_identical(self):
        for i, doc in enumerate(fuzz_corpora(600, seed=77)):
            fast, slow = self._slice_both_ways(doc, 200)
            self.assertEqual(fast, slow, f"fuzz corpus #{i} diverged: {doc[:60]!r}")

    def test_random_structured_docs_identical(self):
        rnd = random.Random(99)
        for trial in range(60):
            parts = []
            for _ in range(rnd.randint(2, 12)):
                lvl = rnd.randint(1, 6)
                parts.append("#" * lvl + " T%d" % rnd.randint(0, 99))
                if rnd.random() < 0.3:
                    parts.append("```c\n# x\n```")
                parts.append(
                    " ".join("w%d" % rnd.randint(0, 99) for _ in range(rnd.randint(5, 300))) + "."
                )
            doc = "\n\n".join(parts)
            fast, slow = self._slice_both_ways(doc, 400)
            self.assertEqual(fast, slow, f"structured doc #{trial} diverged")

    def test_custom_patterns_still_work(self):
        """A non-canonical pattern must take the regex path and behave normally."""
        from smart_slice import slice_bytes

        custom = [re.compile(r"(?m)^SECTION \d+:.*")] + list(BLANK_LINE)
        doc = b"SECTION 1: Intro\n\nbody one\n\nSECTION 2: More\n\nbody two\n"
        rows = slice_bytes(doc, "spec.txt", limit=1000, patterns=custom)
        titles = [r["title"] for r in rows]
        self.assertTrue(any("SECTION 1" in t for t in titles), titles)
        self.assertTrue(any("SECTION 2" in t for t in titles), titles)


if __name__ == "__main__":
    unittest.main()