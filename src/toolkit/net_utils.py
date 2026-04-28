"""Network utilities for the Toolkit installer.

All HTTPS downloads go through :func:`open_url` which uses the ``certifi``
CA bundle so they work on a stock macOS Python install where the system
CA store is not available to urllib.
"""
from __future__ import annotations

import logging
import ssl
import urllib.request
from pathlib import Path
from typing import IO

LOGGER = logging.getLogger(__name__)


def _ssl_context() -> ssl.SSLContext:
    """Return an SSL context that uses the certifi CA bundle.

    Falls back to the default context if certifi is not installed (it is
    listed in requirements so this should not happen in production).
    """
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        LOGGER.debug("SSL context using certifi: %s", certifi.where())
        return ctx
    except ImportError:
        LOGGER.warning(
            "certifi not installed — falling back to default SSL context. "
            "Downloads may fail on macOS with stock Python."
        )
        return ssl.create_default_context()


def open_url(url: str) -> urllib.request.Request:
    """Return a :class:`urllib.request.Request` for *url* with a proper
    User-Agent header. Pass the result to :func:`urlopen`."""
    return urllib.request.Request(
        url,
        headers={"User-Agent": "Toolkit-Installer/1.0"},
    )


def urlopen(url: str):
    """Open *url* using the certifi SSL context.

    Returns the same file-like object as ``urllib.request.urlopen``.
    """
    return urllib.request.urlopen(open_url(url), context=_ssl_context())
