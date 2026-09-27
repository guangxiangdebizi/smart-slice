# coding=utf-8
"""Logging setup.

The library never configures handlers or levels on the root logger; it only emits
records through ``logging.getLogger("smart_slice")`` so that the embedding
application stays in charge of log routing.  Per-module child loggers are used
(``smart_slice.handlers.pdf`` ...) which makes selective silencing easy.
"""
import logging

__all__ = ["logger", "get_logger"]

ROOT_LOGGER_NAME = "smart_slice"

logger = logging.getLogger(ROOT_LOGGER_NAME)


def get_logger(name: str = "") -> logging.Logger:
    """Return a child logger under the ``smart_slice`` namespace."""
    if not name:
        return logger
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")