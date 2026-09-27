# coding=utf-8
"""UUID helpers - drop-in replacement for ``uuid_utils.compat``.

Image assets extracted from documents get a fresh id which is then embedded in
the markdown reference (``./oss/file/{id}``).  UUIDv7 is preferred because ids
stay time-sortable; ``uuid_utils`` provides it as a compiled extension.  When
that optional dependency is absent, a small pure-python v7 generator is used so
the package keeps working with a stdlib-only install.
"""
import os
import time
import uuid as _stdlib_uuid

__all__ = ["UUID", "uuid7", "uuid4"]

UUID = _stdlib_uuid.UUID


def uuid4() -> UUID:
    return _stdlib_uuid.uuid4()


def _uuid7_pure_python() -> UUID:
    """RFC 9562 style v7 UUID: 48-bit unix-ms timestamp + random tail."""
    timestamp_ms = int(time.time() * 1000)
    rand = bytearray(os.urandom(10))
    rand[0] = (rand[0] & 0x0F) | 0x70  # version 7
    rand[2] = (rand[2] & 0x3F) | 0x80  # RFC 4122 variant
    body = timestamp_ms.to_bytes(6, "big") + bytes(rand)
    return UUID(bytes=body)


try:  # prefer the compiled implementation when installed
    from uuid_utils import uuid7 as _uuid7_impl
except Exception:  # noqa: BLE001 - optional dependency
    _uuid7_impl = _uuid7_pure_python


def uuid7() -> UUID:
    return _uuid7_impl()