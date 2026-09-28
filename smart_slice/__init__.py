# coding=utf-8
"""smart-slice - fidelity-first document slicing for RAG pipelines.

Turn a document into retrieval-ready paragraphs: extract the text of ~30 file
formats, cut it along its own structure (heading tree, then blank lines, then a
length budget), and keep every original character reachable.

Quick start::

    from smart_slice import slice_bytes, slice_text

    paragraphs = slice_text("# Chapter\\n\\nbody text", limit=1000)
    # [{'title': 'Chapter', 'content': 'body text'}]

    paragraphs = slice_bytes(open("report.pdf", "rb").read(), "report.pdf", limit=1000)

Design notes
------------
*Fidelity before cleverness.*  No cleaning step deletes source text: heading
markers are stripped only at line starts and only outside code fences, table
headers are *appended* to continuation chunks rather than rewritten into them,
oversized rows are re-split instead of truncated.

*Structure before length.*  ``limit`` is a budget, not a grid: the slicer walks
the heading tree first, then falls back to sentence boundaries, then to a hard
character cut.

*No framework.*  Pure library: no Django, no ORM, no network, no logging
configuration.  Errors are :class:`~smart_slice.exceptions.SliceError` with an
HTTP-style ``code``; images extracted from documents are handed to a callback
instead of being persisted.
"""
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

from ._validation import ParserLimits
from .exceptions import (
    ParseError,
    ResourceLimitError,
    SliceError,
    UnsupportedFormatError,
)
from .handlers import (
    HANDLER_EXTENSIONS,
    SPLIT_HANDLERS,
    BaseSplitHandle,
    FileBufferHandle,
    missing_dependencies,
)
from .options import (
    DEFAULT_LIMIT,
    DEFAULT_OVERLAP,
    ChunkingOptions,
    current_options,
    resolve_options,
    use_options,
)

#: Default embedding-chunk window (characters) for :func:`chunk_paragraphs`.
DEFAULT_CHUNK_SIZE = 256
from .patterns import (
    BLANK_LINE,
    DEFAULT_PATTERNS,
    LITERAL_PATTERNS,
    MARKDOWN_HEADINGS,
    patterns_for,
)
from .scheduler import (
    BACKEND_PROCESS,
    BACKEND_SERIAL,
    BACKEND_THREAD,
    DEFAULT_MAX_CONCURRENCY,
    ERROR_COLLECT,
    ERROR_RAISE_FIRST,
    POLICY_AUTO,
    POLICY_CORES,
    POLICY_SERIAL,
    BatchReport,
    SchedulerPolicy,
    SliceJob,
    TaskOutcome,
    as_slice_jobs,
    auto_concurrency,
    available_cores,
    current_policy,
    default_policy,
    resolve_concurrency,
    resolve_policy,
    run_parallel,
    slice_many,
    slice_paths,
    use_policy,
)
from .service import (
    BytesSplitFile,
    LazyImageTextExtractor,
    build_image_text_extractor,
    normalize_split_rows,
    replace_image_file_ids,
    split_document,
)
from .types import ImageAsset, Paragraph, SplitResult

__version__ = "0.4.0"

__all__ = [
    "__version__",
    # primary API
    "slice_bytes",
    "slice_text",
    "slice_path",
    "slice_many",
    "slice_paths",
    "split_document",
    "chunk",
    "chunk_paragraphs",
    # extraction only (no slicing)
    "extract_text",
    # text-level building blocks
    "SplitModel",
    "smart_split_paragraph",
    "filter_special_char",
    "MarkChunkHandle",
    # batch scheduling ("how many documents at once, on which cores")
    "SchedulerPolicy",
    "BatchReport",
    "SliceJob",
    "TaskOutcome",
    "as_slice_jobs",
    "run_parallel",
    "resolve_policy",
    "resolve_concurrency",
    "auto_concurrency",
    "available_cores",
    "current_policy",
    "default_policy",
    "use_policy",
    "POLICY_AUTO",
    "POLICY_CORES",
    "POLICY_SERIAL",
    "BACKEND_THREAD",
    "BACKEND_PROCESS",
    "BACKEND_SERIAL",
    "ERROR_RAISE_FIRST",
    "ERROR_COLLECT",
    "DEFAULT_MAX_CONCURRENCY",
    # configuration / introspection
    "ChunkingOptions",
    "DEFAULT_LIMIT",
    "DEFAULT_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "resolve_options",
    "current_options",
    "use_options",
    "ParserLimits",
    "SPLIT_HANDLERS",
    "HANDLER_EXTENSIONS",
    "missing_dependencies",
    "supported_extensions",
    "detect_handler",
    "patterns_for",
    "MARKDOWN_HEADINGS",
    "DEFAULT_PATTERNS",
    "BLANK_LINE",
    "LITERAL_PATTERNS",
    # types
    "ImageAsset",
    "Paragraph",
    "SplitResult",
    "BaseSplitHandle",
    "FileBufferHandle",
    "BytesSplitFile",
    "LazyImageTextExtractor",
    "build_image_text_extractor",
    "normalize_split_rows",
    "replace_image_file_ids",
    # errors
    "SliceError",
    "ParseError",
    "UnsupportedFormatError",
    "ResourceLimitError",
]



def slice_text(
    text: str,
    *,
    limit: Optional[int] = None,
    overlap: Optional[int] = None,
    overlap_ratio: Optional[float] = None,
    options: Optional[ChunkingOptions] = None,
    patterns: Optional[Sequence[Any]] = None,
    with_filter: bool = False,
    name: str = "document.md",
) -> List[Paragraph]:
    """Slice an in-memory string into ``[{title, content}]`` paragraphs.

    :param text:          source text (markdown headings are recognised)
    :param limit:         maximum characters per paragraph; ``None`` = default (1000)
    :param overlap:       characters of context shared by consecutive paragraphs.
                          ``None`` = default (0, no overlap)
    :param overlap_ratio: alternative form of ``overlap`` as a fraction of ``limit``
                          (``0.15`` means 15%).  Ignored when ``overlap`` is given.
    :param options:       a :class:`ChunkingOptions` to reuse across calls; keyword
                          arguments above override the matching field
    :param patterns:      heading/paragraph regexes; ``None`` = the markdown default
    :param with_filter:   strip line-initial heading markers and collapse blank runs
    :param name:          only used to pick the default pattern family

    ``with_filter`` defaults to False here: slicing your own string is usually a
    deliberate act and the caller keeps the raw text.  The file entry points keep
    the platform default of True.
    """
    opts = resolve_options(options, limit=limit, overlap=overlap, overlap_ratio=overlap_ratio)
    with use_options(opts):
        return split_document(
            name,
            text.encode("utf-8"),
            limit=opts.limit,
            pattern_list=list(patterns) if patterns is not None else None,
            with_filter=with_filter,
            normalize=True,
        )


def slice_bytes(
    content: bytes,
    name: str,
    *,
    limit: Optional[int] = None,
    overlap: Optional[int] = None,
    overlap_ratio: Optional[float] = None,
    options: Optional[ChunkingOptions] = None,
    patterns: Optional[Sequence[Any]] = None,
    with_filter: bool = True,
    normalize: bool = True,
    save_image: Optional[Callable[[List[ImageAsset]], Any]] = None,
    image_text_extractor: Optional[Callable[[bytes, str], str]] = None,
    fallback_title: Optional[str] = None,
    progress_hook: Optional[Callable[[], None]] = None,
) -> Union[List[Paragraph], SplitResult]:
    """Slice a document from its raw bytes.

    :param content:       complete file bytes
    :param name:          file name - decides which handler runs
    :param limit:         maximum characters per paragraph; ``None`` = default (1000)
    :param overlap:       characters of context shared by consecutive paragraphs.
                          ``None`` = default (0).  A positive value trades the
                          "chunks tile the source exactly once" property for
                          cross-boundary retrieval recall.
    :param overlap_ratio: alternative form of ``overlap`` as a fraction of ``limit``
    :param options:       a :class:`ChunkingOptions` to reuse; keyword arguments
                          above override the matching field
    :param patterns:      custom heading regexes; ``None`` = per-format default
    :param with_filter:   pass through to the slicer's cleaning stage
    :param normalize:     True -> flat ``[{title, content}]``;
                          False -> the handler's raw structure (nested groups for
                          multi-sheet workbooks and archives, which previews want)
    :param save_image:    callback receiving :class:`ImageAsset` objects extracted
                          from the document; may return ``{new_id: existing_id}``
                          to remap references after deduplication
    :param image_text_extractor: ``(image_bytes, image_name) -> str`` OCR hook.
                          Not installed by default; see
                          :func:`build_image_text_extractor`.
    :param fallback_title: title used for paragraphs that have none (only with
                          ``normalize=True``)
    :param progress_hook: zero-argument callback fired before each handler, each
                          archive member and each OCR call - use it as a heartbeat
                          while parsing large files
    :raises SliceError:   nothing supports the format, or parsing failed

    This is the entry point every other layer in the package funnels through.

    ``overlap`` reaches the slicer through a contextvar (see
    :mod:`smart_slice.options`), so the generated format handlers keep their
    original signature while still honouring the caller's chunking configuration.
    """
    opts = resolve_options(options, limit=limit, overlap=overlap, overlap_ratio=overlap_ratio)
    with use_options(opts):
        return split_document(
            name,
            content,
            limit=opts.limit,
            pattern_list=list(patterns) if patterns is not None else None,
            with_filter=with_filter,
            save_image=save_image,
            image_text_extractor=image_text_extractor,
            normalize=normalize,
            fallback_title=fallback_title,
            progress_hook=progress_hook,
        )


def slice_path(
    path: Union[str, "os.PathLike[str]"],
    *,
    limit: Optional[int] = None,
    overlap: Optional[int] = None,
    overlap_ratio: Optional[float] = None,
    options: Optional[ChunkingOptions] = None,
    name: Optional[str] = None,
    **kwargs: Any,
) -> Union[List[Paragraph], SplitResult]:
    """Read a file from disk and slice it.  See :func:`slice_bytes` for keywords.

    :param name: override the file name used for handler dispatch (defaults to the
                 path's basename)
    """
    resolved = os.fspath(path)
    with open(resolved, "rb") as handle:
        content = handle.read()
    return slice_bytes(
        content,
        name or os.path.basename(resolved),
        limit=limit,
        overlap=overlap,
        overlap_ratio=overlap_ratio,
        options=options,
        **kwargs,
    )


def extract_text(content: bytes, name: str, *, save_image: Optional[Callable] = None) -> str:
    """Extraction only: return the document's text without slicing it.

    Mirrors ``get_content`` on the handlers - useful for previews, for feeding an
    LLM a whole (small) document, or for bundling archive members into one file.
    """
    file = BytesSplitFile(name, content)
    buf = FileBufferHandle()
    buf.buffer = content
    get_buffer = buf.get_buffer
    for handler in SPLIT_HANDLERS:
        if not handler.support(file, get_buffer):
            continue
        sink = save_image or (lambda images: None)
        from .handlers.image import ImageSplitHandle

        if isinstance(handler, ImageSplitHandle):
            # extended signature: OCR extractor + buffer access are keyword args
            return handler.get_content(file, sink, get_buffer=get_buffer)
        return handler.get_content(file, sink)
    extension = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
    raise UnsupportedFormatError(f"Unsupported file format{f': {extension}' if extension else ''}")


def detect_handler(name: str, content: Optional[bytes] = None) -> Optional[str]:
    """Return the handler class name that would claim this input, or ``None``.

    Introspection helper for CLIs, tests and "why did my file parse like that"
    debugging.  Passing ``content`` enables the content-sniffing handlers.
    """
    file = BytesSplitFile(name, content or b"")
    buf = FileBufferHandle()
    if content is not None:
        buf.buffer = content
    get_buffer = buf.get_buffer
    for handler in SPLIT_HANDLERS:
        try:
            if handler.support(file, get_buffer):
                return type(handler).__name__
        except Exception:  # noqa: BLE001 - a failed sniff must not break detection
            continue
    return None


def supported_extensions() -> Dict[str, List[str]]:
    """Map handler name -> extensions it is documented for."""
    return {name: list(exts) for name, exts in HANDLER_EXTENSIONS.items()}


def chunk_paragraphs(
    paragraphs: Iterable[Paragraph],
    *,
    chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None,
    chunk_overlap_ratio: Optional[float] = None,
    options: Optional[ChunkingOptions] = None,
    carry_title: Optional[bool] = None,
    handler: Optional[Any] = None,
) -> List[str]:
    """Chunk paragraphs down to an embedding window, optionally with overlap.

    Slicing yields semantic paragraphs; embedding models still need fixed-size
    input.  The paragraph stays the retrieval unit, the chunk the vector unit.

    Defaults mirror the historical behaviour (``chunk_size=256``, no overlap, no
    title prefix); every one of them is overridable per call or through an
    :class:`~smart_slice.options.ChunkingOptions`.

    :param paragraphs:        ``[{title, content}]`` from :func:`slice_bytes`
    :param chunk_size:        target chunk length in characters (default 256)
    :param chunk_overlap:     characters of context carried from the previous chunk
    :param chunk_overlap_ratio: ``chunk_overlap`` as a fraction of ``chunk_size``
    :param options:           a :class:`ChunkingOptions` to reuse
    :param carry_title:       prefix **every** chunk with its paragraph's heading
                              chain (default False).  Without this, chunking a long
                              paragraph leaves only its first chunk carrying section
                              context, which measurably hurts retrieval for the rest.
                              The prefix is additive: ``chunk_size`` still governs
                              the body length, so budget for the title in your
                              embedding window.
    :param handler:           any :class:`~smart_slice.chunking.IChunkHandle`
    """
    from .chunking import MarkChunkHandle, OverlapChunkHandle

    opts = resolve_options(
        options,
        limit=chunk_size if chunk_size is not None else DEFAULT_CHUNK_SIZE,
        overlap=chunk_overlap,
        overlap_ratio=chunk_overlap_ratio,
    )
    if carry_title is not None:
        opts = opts.with_(carry_title=carry_title)

    handle = handler or (MarkChunkHandle() if opts.effective_overlap == 0 else OverlapChunkHandle())

    # Pair each paragraph's text with its heading chain so the chain can be
    # re-applied to *every* chunk produced from that paragraph (see below).
    pairs = []
    for row in paragraphs:
        if isinstance(row, dict):
            content = row.get("content")
            title = str(row.get("title") or "").strip()
        else:
            content, title = row, ""
        if isinstance(content, str) and content.strip():
            pairs.append((content, title))

    # Chunk each paragraph on its own: a paragraph is the semantic unit, and
    # blending two sections into one vector hurts retrieval more than a little
    # lost context helps.
    chunks: List[str] = []
    for text, title in pairs:
        if isinstance(handle, OverlapChunkHandle):
            pieces = handle.handle([text], opts)
        else:
            pieces = handle.handle([text], opts.limit)
        if opts.carry_title and title:
            prefix = f"# {title}\n"
            chunks.extend(prefix + piece for piece in pieces)
        else:
            chunks.extend(pieces)
    return chunks


def chunk(
    text: str,
    *,
    chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None,
    chunk_overlap_ratio: Optional[float] = None,
    options: Optional[ChunkingOptions] = None,
    handler: Optional[Any] = None,
) -> List[str]:
    """Chunk a single string (convenience wrapper over :func:`chunk_paragraphs`)."""
    return chunk_paragraphs(
        [{"title": "", "content": text}],
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        chunk_overlap_ratio=chunk_overlap_ratio,
        options=options,
        handler=handler,
    )


# re-exported lazily so ``import smart_slice`` stays cheap (jieba / parsers are
# only imported when the caller actually reaches for them)
def __getattr__(name: str) -> Any:
    if name == "SplitModel":
        from .chunker import SplitModel

        return SplitModel
    if name == "smart_split_paragraph":
        from .chunker import smart_split_paragraph

        return smart_split_paragraph
    if name == "filter_special_char":
        from .chunker import filter_special_char

        return filter_special_char
    if name == "MarkChunkHandle":
        from .chunking import MarkChunkHandle

        return MarkChunkHandle
    if name == "OverlapChunkHandle":
        from .chunking import OverlapChunkHandle

        return OverlapChunkHandle
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")