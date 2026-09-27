# coding=utf-8
"""Embedding chunking of already-sliced paragraphs.

Slicing produces semantic paragraphs; embedding models still have an input
window, so paragraphs get cut once more into fixed-size chunks.  This is a
separate stage on purpose - a knowledge base usually wants the paragraph as the
retrieval/display unit and the chunk only as the vectorisation unit.

Two handlers ship here:

``MarkChunkHandle``
    The historical implementation: punctuation/newline oriented splitting to
    ``chunk_size``, no overlap.  Kept byte-for-byte so existing pipelines and
    stored vectors stay reproducible.
``OverlapChunkHandle``
    Overlap-aware, and the one :func:`smart_slice.chunk_paragraphs` picks as soon
    as an overlap is requested.  Reuses the first-stage splitter so sentence
    boundaries, markdown-table protection and token budgeting behave the same.
"""
from ._base import IChunkHandle
from .mark import MarkChunkHandle
from .overlap import OverlapChunkHandle

__all__ = ["IChunkHandle", "MarkChunkHandle", "OverlapChunkHandle"]


def default_chunk_handle(overlap: int = 0) -> IChunkHandle:
    """Return the handler matching ``overlap``: legacy one at zero, overlap-aware above."""
    return MarkChunkHandle() if overlap <= 0 else OverlapChunkHandle()