# coding=utf-8
"""Embedding chunking of already-sliced paragraphs.

Slicing produces semantic paragraphs; embedding models still have an input
window, so paragraphs get cut once more into fixed-size chunks.  This is a
separate stage on purpose - a knowledge base usually wants the paragraph as the
retrieval/display unit and the chunk only as the vectorisation unit.
"""
from ._base import IChunkHandle
from .mark import MarkChunkHandle

__all__ = ["IChunkHandle", "MarkChunkHandle"]