# coding=utf-8
"""Minimal gettext shim.

The slicing core ships user facing error strings.  Wrapping them in ``gettext``
keeps them translatable without depending on a web framework: applications that
want translations can install their own catalogues for the ``smart_slice``
domain, everyone else gets the source strings unchanged.
"""
import gettext as _gettext
import os
from typing import Any

__all__ = ["gettext", "install_translations"]

_DOMAIN = "smart_slice"
_translation = _gettext.NullTranslations()


def install_translations(localedir: str = None, languages=None) -> None:
    """Load a catalogue for the ``smart_slice`` domain.

    Missing catalogues are not an error: the null translation (identity) stays
    installed so callers never have to guard.
    """
    global _translation
    localedir = localedir or os.path.join(os.path.dirname(os.path.dirname(__file__)), "locale")
    try:
        _translation = _gettext.translation(_DOMAIN, localedir=localedir, languages=languages, fallback=True)
    except Exception:  # noqa: BLE001 - translation is an enhancement, never fatal
        _translation = _gettext.NullTranslations()


def gettext(message: Any) -> str:
    """Translate ``message``; returns it unchanged when no catalogue is installed."""
    if not isinstance(message, str):
        return str(message)
    return _translation.gettext(message)