# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("files")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import ResourceLimitError

import hashlib
import os
import shutil
import tempfile
from contextlib import contextmanager




class DownloadedImportFile:
    chunk_size = 1024 * 1024

    def __init__(
        self,
        name="smart-slice-download",
        directory=None,
        max_bytes=1024 * 1024 * 1024,
        min_free_bytes=0,
        progress_hook=None,
    ):
        self.name = name
        self.size = 0
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.progress_hook = progress_hook
        self.sha256 = ""
        self._digest = hashlib.sha256()
        self._file = tempfile.TemporaryFile(mode="w+b", dir=directory)
        self._directory = directory or tempfile.gettempdir()
        self._complete = False

    @property
    def closed(self):
        return self._file.closed

    def write(self, chunk):
        if self._complete:
            raise ValueError("Download is already complete")
        if self.size + len(chunk) > self.max_bytes:
            raise ResourceLimitError(f"附件超过本次允许下载的大小（{self.max_bytes // (1024 * 1024)} MiB）")
        if shutil.disk_usage(self._directory).free - len(chunk) < self.min_free_bytes:
            raise ResourceLimitError("导入临时磁盘可用空间不足，请释放空间后重试")
        written = self._file.write(chunk)
        if written != len(chunk):
            raise OSError("Incomplete temporary file write")
        self.size += written
        self._digest.update(chunk)
        if self.progress_hook is not None:
            self.progress_hook()
        return written

    def finish(self, expected_size=None):
        if expected_size is not None and self.size != expected_size:
            raise OSError(f"附件下载不完整：期望 {expected_size} 字节，实际收到 {self.size} 字节")
        self._file.flush()
        self.sha256 = self._digest.hexdigest()
        self._complete = True
        self._file.seek(0)
        return self

    def read(self, size=-1):
        return self._file.read(size)

    def seek(self, offset, whence=os.SEEK_SET):
        return self._file.seek(offset, whence)

    def tell(self):
        return self._file.tell()

    def chunks(self, chunk_size=None):
        position = self.tell()
        self.seek(0)
        try:
            while True:
                chunk = self.read(chunk_size or self.chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            self.seek(position)

    def close(self):
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class ImportSplitFile:
    def __init__(self, name, source, max_buffer_bytes=128 * 1024 * 1024):
        self.name = name
        self.source = source
        self.size = source.size
        self.max_buffer_bytes = max_buffer_bytes

    def read(self):
        if self.size > self.max_buffer_bytes:
            raise ResourceLimitError("该格式需要分段解析，文件超过当前解析缓冲额度")
        with self.open_reader() as reader:
            content = reader.read(self.max_buffer_bytes + 1)
            if len(content) > self.max_buffer_bytes:
                raise ResourceLimitError("文件超过当前解析缓冲额度")
            return content

    def chunks(self, chunk_size=None):
        yield from self.source.chunks(chunk_size)

    @contextmanager
    def open_reader(self):
        position = self.source.tell()
        self.source.seek(0)
        try:
            yield self.source
        finally:
            self.source.seek(position)
