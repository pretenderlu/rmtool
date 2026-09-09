"""Strict HTTPS access using the CA bundle shipped with rmtool."""

from __future__ import annotations

import ssl
import urllib.request
from functools import lru_cache

import certifi


SMOKE_URLS = (
    "https://github.com/pretenderlu/rmtool/releases/download/"
    "tap-page-turn-assets/manifest.json",
    "https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/"
    "manifest.json",
    "https://remarkable-software.s3.us-east-2.amazonaws.com/"
    "?list-type=2&max-keys=1",
)


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """Return the shared, verifying TLS context backed by bundled certifi CAs."""
    return ssl.create_default_context(cafile=certifi.where())


def urlopen(url, *, timeout: float, handlers=()):
    """Open an HTTPS URL with rmtool's CA bundle and optional urllib handlers."""
    context = ssl_context()
    if handlers:
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), *handlers
        )
        return opener.open(url, timeout=timeout)
    return urllib.request.urlopen(url, timeout=timeout, context=context)


def smoke_test() -> None:
    """Verify public HTTPS trust from the packaged application runtime."""
    for url in SMOKE_URLS:
        request = urllib.request.Request(
            url, headers={"User-Agent": "rmtool-packaged-https-smoke/1"}
        )
        with urlopen(request, timeout=30) as response:
            response.read(1)
