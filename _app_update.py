"""Best-effort public release check for the rmtool desktop application."""

from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.request import Request

import _https


LATEST_RELEASE_URL = "https://api.github.com/repos/pretenderlu/rmtool/releases/latest"
_VERSION_RE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def _version_tuple(value: object) -> Optional[tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.fullmatch(value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def check_for_update(current_version: str) -> Optional[str]:
    """Return a newer public release tag, or None without affecting startup."""
    current = _version_tuple(current_version)
    if current is None:
        logging.warning("Could not check rmtool updates: invalid local version %r", current_version)
        return None

    request = Request(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "rmtool-update-check/1",
        },
    )
    try:
        with _https.urlopen(request, timeout=8) as response:
            payload = json.load(response)
    except Exception as exc:
        logging.warning("Could not check rmtool updates: %s", exc)
        return None

    if (
        not isinstance(payload, dict)
        or type(payload.get("draft")) is not bool
        or type(payload.get("prerelease")) is not bool
    ):
        logging.warning("Ignored malformed rmtool latest release response")
        return None
    if payload["draft"] or payload["prerelease"]:
        return None

    tag = payload.get("tag_name")
    latest = _version_tuple(tag)
    if latest is None:
        logging.warning("Ignored malformed rmtool release tag %r", tag)
        return None
    if latest <= current:
        return None
    return "v{}.{}.{}".format(*latest)
