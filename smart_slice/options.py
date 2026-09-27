# coding=utf-8
"""Chunking configuration: defaults plus per-call overrides.

Two knobs matter for retrieval quality - how big a chunk may get, and how much
context neighbouring chunks share.  Both are exposed the same way at every layer:

* **default mode** - pass nothing and you get :data:`DEFAULT_OPTIONS`
  (``limit=1000`` chars, ``overlap=0``);
* **custom mode** - pass ``limit``/``overlap`` (or ``overlap_ratio``) to any entry
  point, or build a :class:`ChunkingOptions` and reuse it.

Threading options through the call stack
---------------------------------------
The format handlers are generated code with a fixed
``handle(file, pattern_list, with_filter, limit, get_buffer, save_image)``
signature; adding a parameter to 24 of them would be invasive and would break the
guarantee that generated files stay byte-identical to their source.  Instead the
public entry points publish the active options on a :mod:`contextvars` slot for the
duration of the call, and :class:`~smart_slice.chunker.SplitModel` reads it when no
explicit value is given.

Context variables are per-task, so this is safe under threads and ``asyncio`` -
concurrent slices with different options cannot see each other's settings.  Direct
use of the low-level API (``SplitModel(..., overlap=80)``,
``smart_split_paragraph(text, 1000, overlap=80)``) always wins over the context.
"""
import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Callable, Iterator, Optional

__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_OVERLAP",
    "DEFAULT_LOOKBACK",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "ChunkingOptions",
    "current_options",
    "use_options",
    "resolve_options",
]

#: Default paragraph/chunk budget in characters.  Small enough that a paragraph
#: maps to a few hundred tokens - the range where embedding models are most
#: reliable - and large enough that a normal prose paragraph is not cut at all.
DEFAULT_LIMIT = 1000

#: Default overlap between consecutive chunks, in characters.  Zero keeps the
#: historical behaviour: chunks tile the source exactly once, so concatenating
#: them reproduces the original text.  Any positive value trades that strict
#: tiling property for cross-boundary retrieval recall.
DEFAULT_OVERLAP = 0

#: How far back from the budget edge to look for a sentence boundary, as a
#: fraction of ``limit``.  0.5 reproduces the original "keep at least half" rule.
DEFAULT_LOOKBACK = 0.5

#: Hard clamps inherited from the source implementation.
MAX_LIMIT = 100000
MIN_LIMIT = 50


def _clamp_limit(limit) -> int:
    """Coerce ``limit`` into ``[MIN_LIMIT, MAX_LIMIT]`` (source-platform semantics)."""
    if limit is None:
        return MAX_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return MAX_LIMIT
    if value > MAX_LIMIT:
        return MAX_LIMIT
    if value < MIN_LIMIT:
        return MIN_LIMIT
    return value


@dataclass(frozen=True)
class ChunkingOptions:
    """Immutable chunking configuration.

    :param limit:          maximum chunk size, measured with ``length_fn``
    :param overlap:        characters of context shared by consecutive chunks.
                           Mutually exclusive with ``overlap_ratio`` (if both are
                           given, ``overlap`` wins).
    :param overlap_ratio:  convenience form of ``overlap`` as a fraction of
                           ``limit`` (0.15 on a 1000-char limit means 150 chars).
                           Used only when ``overlap`` is None.  Both are clamped to
                           ``limit // 2`` so progress is guaranteed.
    :param boundary:       snap cut points to sentence boundaries (``。 . ! ！ ? ？``)
                           instead of cutting mid-sentence.  Default True.
    :param lookback:       fraction of ``limit`` searched backwards for a boundary.
                           Lower values produce chunks closer to ``limit`` but cut
                           more arbitrarily.
    :param overlap_boundary: when taking overlap, move the overlap start forward to
                           the next sentence/whitespace boundary so carried context
                           never begins mid-word.  Keeps ``overlap`` an upper bound.
    :param min_chunk:      chunks shorter than this are merged into the previous
                           chunk rather than emitted alone (avoids 3-char fragments
                           that pollute an index).  0 disables merging.
    :param carry_title:    prepend the heading chain to each chunk.  Chunking a
                           long paragraph otherwise strips section context from
                           every chunk but the first, which measurably hurts
                           retrieval.
    :param overlap_within_section: when True, paragraph overlap only carries context
                           between paragraphs that share the same heading chain
                           (default False: carry across the whole document in order).
    :param length_fn:      how "size" is measured - ``len`` (characters) by default.
                           Pass a tokenizer (e.g. ``lambda s: len(enc.encode(s))``)
                           to budget in tokens.  ``overlap`` and ``lookback`` stay
                           in characters so the math remains predictable.
    """

    limit: int = DEFAULT_LIMIT
    overlap: Optional[int] = None
    overlap_ratio: Optional[float] = None
    boundary: bool = True
    lookback: float = DEFAULT_LOOKBACK
    overlap_boundary: bool = True
    min_chunk: int = 0
    carry_title: bool = False
    overlap_within_section: bool = False
    length_fn: Callable[[str], int] = len

    def __post_init__(self) -> None:
        object.__setattr__(self, "limit", _clamp_limit(self.limit))
        # Validate both knobs but keep ``overlap``'s None sentinel: materialising it
        # to 0 here would make ``with_(overlap_ratio=...)`` a no-op on any derived
        # options object, because an explicit 0 outranks the ratio.
        if self.overlap is not None:
            if int(self.overlap) < 0:
                raise ValueError("overlap must be >= 0")
        if self.overlap_ratio is not None:
            ratio = float(self.overlap_ratio)
            if ratio < 0:
                raise ValueError("overlap_ratio must be >= 0")
            if ratio > 0.5:
                raise ValueError("overlap_ratio must be <= 0.5 (more than half the chunk would be repeated)")
        if not 0.0 < self.lookback <= 1.0:
            raise ValueError("lookback must be in (0, 1]")
        if self.min_chunk < 0:
            raise ValueError("min_chunk must be >= 0")

    @property
    def effective_overlap(self) -> int:
        """Resolved overlap in characters.

        An explicit ``overlap`` wins; otherwise ``overlap_ratio * limit`` is used;
        otherwise zero.  The result is clamped to ``limit // 2`` so the splitter's
        cursor always advances (no infinite loop, no chunk that is all repeat).
        """
        if self.overlap is not None:
            value = int(self.overlap)
        elif self.overlap_ratio is not None:
            value = int(self.limit * float(self.overlap_ratio))
        else:
            value = 0
        return max(0, min(value, self.limit // 2))

    def size(self, text: str) -> int:
        """Measure ``text`` with the configured ``length_fn``."""
        return self.length_fn(text)

    def with_(self, **changes) -> "ChunkingOptions":
        """Return a copy with the given fields replaced (frozen dataclass helper)."""
        return replace(self, **changes)


#: The defaults used when a caller specifies nothing.
DEFAULT_OPTIONS = ChunkingOptions()

_CURRENT: contextvars.ContextVar[Optional[ChunkingOptions]] = contextvars.ContextVar(
    "smart_slice_chunking_options", default=None)


def current_options() -> ChunkingOptions:
    """Options in effect for the current call, or :data:`DEFAULT_OPTIONS`."""
    return _CURRENT.get() or DEFAULT_OPTIONS


@contextmanager
def use_options(options: Optional[ChunkingOptions]) -> Iterator[ChunkingOptions]:
    """Publish ``options`` for the duration of the block.

    Used by the public entry points so that generated handlers - which cannot take
    an extra parameter - still honour the caller's chunking configuration.
    """
    resolved = options or DEFAULT_OPTIONS
    token = _CURRENT.set(resolved)
    try:
        yield resolved
    finally:
        _CURRENT.reset(token)


def resolve_options(
    options: Optional[ChunkingOptions] = None,
    *,
    limit: Optional[int] = None,
    overlap: Optional[int] = None,
    overlap_ratio: Optional[float] = None,
    **changes,
) -> ChunkingOptions:
    """Merge explicit keyword arguments over ``options`` (or the ambient default).

    Precedence: explicit kwargs > the passed ``options`` object > the contextvar >
    :data:`DEFAULT_OPTIONS`.  This is what lets
    ``slice_bytes(data, name, limit=800, overlap=80)`` and
    ``slice_bytes(data, name, options=my_opts)`` both work.
    """
    base = options or current_options()
    if limit is None and overlap is None and overlap_ratio is None and not changes:
        return base
    fields = {}
    if limit is not None:
        fields["limit"] = limit
    if overlap is not None:
        fields["overlap"] = overlap
    if overlap_ratio is not None:
        fields["overlap_ratio"] = overlap_ratio
    fields.update(changes)
    return base.with_(**fields)


