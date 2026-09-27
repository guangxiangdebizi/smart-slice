# coding=utf-8
"""Overlap-aware embedding chunker.

:func:`smart_slice.slice_bytes` already honours ``overlap`` when it cuts long
paragraphs, but a knowledge base usually embeds *chunks* that are smaller than the
paragraphs they came from.  This handler provides the same two knobs at that second
stage, so a caller can size chunks for an embedding window **and** give neighbouring
chunks shared context.

The cut points reuse :func:`~smart_slice.chunker.smart_split_paragraph`, which means
sentence-boundary snapping, markdown-table protection, and token-budget sizing all
behave identically to the first stage.
"""
from typing import Any, List, Optional

from ._base import IChunkHandle

__all__ = ["OverlapChunkHandle"]


class OverlapChunkHandle(IChunkHandle):
    """Chunk each paragraph to ``chunk_size`` with ``overlap`` characters of context.

    Chunks are produced per paragraph and never span two paragraphs: a paragraph is
    the semantic unit, and blending two sections into one vector usually hurts
    retrieval more than a little lost context helps.

    ``handle`` accepts either a :class:`~smart_slice.options.ChunkingOptions` or a
    plain ``chunk_size`` int, so the handler stays usable through the simple
    :class:`IChunkHandle` interface as well.
    """

    def handle(self, chunk_list: List[str], chunk_size: Any = 256) -> List[str]:
        from smart_slice.chunker import smart_split_paragraph
        from smart_slice.options import ChunkingOptions

        if isinstance(chunk_size, ChunkingOptions):
            opts = chunk_size
            size = opts.limit
            kwargs: Optional[dict] = {
                "overlap": opts.effective_overlap,
                "boundary": opts.boundary,
                "lookback": opts.lookback,
                "overlap_boundary": opts.overlap_boundary,
                "min_chunk": opts.min_chunk,
                "length_fn": opts.length_fn,
            }
        else:
            size = int(chunk_size)
            kwargs = None

        result: List[str] = []
        for text in chunk_list:
            if not isinstance(text, str) or not text.strip():
                continue
            if kwargs is None:
                pieces = smart_split_paragraph(text, size)
            else:
                pieces = smart_split_paragraph(text, size, **kwargs)
            for piece in pieces:
                stripped = piece.strip()
                if stripped:
                    result.append(stripped)
        return result