# coding=utf-8
"""Environment-backed settings helpers.

There is no framework settings object: every tunable is read from the process
environment.  The defensive parser caps live on
:class:`smart_slice._validation.ParserLimits`, which resolves each field through
:func:`_load_limits_from_env` below - the canonical variable name is
``SMART_SLICE_PARSER_<FIELD>`` (e.g. ``SMART_SLICE_PARSER_MAX_XML_BYTES``).
"""
import os
from dataclasses import fields
from typing import Optional

__all__ = ["integer_setting", "setting", "_env_flag", "_load_limits_from_env"]


def _env_flag(name: str, default: bool = False) -> bool:
    """Boolean environment switch; anything but a truthy literal means ``default``."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def setting(name: str, default=None, config: Optional[dict] = None):
    """Read a setting: environment first, then the optional ``config`` mapping."""
    value = os.environ.get(name)
    if value is not None:
        return value
    if config:
        value = config.get(name)
        if value is not None:
            return value
    return default


def integer_setting(name: str, default: int, minimum: int = 1, config: Optional[dict] = None) -> int:
    """``setting`` coerced to a positive int, falling back to ``default`` on any problem."""
    try:
        raw = setting(name, default, config)
        if isinstance(raw, bool):
            return default
        value = int(raw)
        return value if value >= minimum else default
    except (TypeError, ValueError, OverflowError):
        return default


def _load_limits_from_env(klass, config: Optional[dict] = None):
    """Build a frozen dataclass instance of ``klass`` from ``SMART_SLICE_PARSER_<FIELD>``.

    Shared by ``ParserLimits.load`` so the environment-variable naming convention
    is defined in exactly one place.
    """
    defaults = {field.name: field.default for field in fields(klass)}
    resolved = {
        name: integer_setting("SMART_SLICE_PARSER_" + name.upper(), default, config=config)
        for name, default in defaults.items()
    }
    return klass(**resolved)