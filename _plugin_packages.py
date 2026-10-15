"""Shared building blocks for firmware-specific plugin packages.

Every rmtool device feature (tap-to-turn, reading/note enhancements, fast
mono reading, pinyin input, native Chinese, WeRead launcher ...) ships the
same kind of artefact: a schema-1 JSON manifest that pins exact device
identities, and per-firmware ``.tar.gz`` payloads whose files are listed
with size, SHA-256 and mode.  This module holds the parts that are identical
across features so the security checks live in one place:

* strict field validation (:func:`required_string`, :func:`parse_payload_file`)
* size-capped HTTPS download (:func:`download_limited`)
* atomic cache writes (:func:`write_atomic`)
* remote → cached → bundled manifest fallback (:func:`load_catalog`)
* manual package import (:func:`load_local_package`)
* allow-listed tar extraction with per-file hashing
  (:func:`extract_verified_package`)

Feature modules keep their own manifest *schemas* (allowed keys, revision and
allow-list rules) and only delegate these mechanics.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Optional, Sequence, TypeVar

import _https
import _package_download

USER_AGENT = "rmtool-tap-page-turn/1"

SHA256_RE = re.compile(r"[0-9a-f]{64}")
FIRMWARE_RE = re.compile(r"[0-9]{14}")
VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2,3}")
PLATFORM_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
ARCH_RE = re.compile(r"[a-z0-9_][a-z0-9_-]{0,31}")
ASSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz")

DEFAULT_LABEL = "点击翻页"

T = TypeVar("T")


@dataclass(frozen=True)
class PayloadFile:
    path: str
    sha256: str
    size: int
    mode: int


# -- Field validation ---------------------------------------------------------
def safe_relative_path(value: object, label: str = DEFAULT_LABEL) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeError(f"{label}资源路径无效。")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise RuntimeError(f"{label}资源路径不安全。")
    return value


def required_string(
    entry: dict, key: str, pattern: re.Pattern[str], label: str = DEFAULT_LABEL
) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RuntimeError(f"{label}清单字段 {key} 无效。")
    return value


def parse_payload_file(entry: object, label: str = DEFAULT_LABEL) -> PayloadFile:
    if not isinstance(entry, dict):
        raise RuntimeError(f"{label}资源文件格式无效。")
    path = safe_relative_path(entry.get("path"), label)
    digest = required_string(entry, "sha256", SHA256_RE, label)
    size = entry.get("size")
    mode = entry.get("mode")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise RuntimeError(f"{label}资源 {path} 的大小无效。")
    if mode not in (0o644, 0o755):
        raise RuntimeError(f"{label}资源 {path} 的权限无效。")
    return PayloadFile(path, digest, size, mode)


# -- I/O ------------------------------------------------------------------------
def download_limited(url: str, maximum: int) -> bytes:
    """Fetch ``url`` over verified HTTPS, refusing bodies over ``maximum``."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with _https.urlopen(request, timeout=30) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > maximum:
            raise RuntimeError("云端文件超过允许大小。")
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise RuntimeError("云端文件超过允许大小。")
    return data


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: Optional[int] = None
    temporary: Optional[Path] = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        output = os.fdopen(fd, "wb")
        fd = None
        with output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _cache_writer() -> Callable[[Path, bytes], None]:
    """Return the patchable cache writer.

    ``_tap_page_turn._write_atomic`` is the historical seam that tests and the
    download audit observe, and it aliases :func:`write_atomic`.  Resolving it
    at call time keeps every cache write going through that single name.
    """
    import _tap_page_turn

    return _tap_page_turn._write_atomic


def cache_dir(state_dir: str, feature_id: str) -> Path:
    return Path(state_dir) / "cache" / feature_id


def package_cache_path(state_dir: str, feature_id: str, package) -> Path:
    return cache_dir(state_dir, feature_id) / package.firmware / package.asset


# -- Catalogs -------------------------------------------------------------------
def load_catalog(
    *,
    manifest_urls: Sequence[str],
    cache_path: Path,
    bundled_path: Path,
    parse: Callable[[bytes], T],
    max_manifest_bytes: int,
    refresh: bool,
    log_label: str,
    error_message: str,
    download: Optional[Callable[[str, int], bytes]] = None,
) -> T:
    """Return the first valid manifest: remote mirrors, then cache, then bundled.

    A remote manifest only replaces the cache after it parses successfully,
    so an invalid response can never overwrite a verified cache.
    ``download`` lets a feature module route requests through its own
    (patchable) downloader; it defaults to :func:`download_limited`.
    """
    fetch = download or download_limited
    if refresh:
        for url in manifest_urls:
            try:
                data = fetch(url, max_manifest_bytes)
                catalog = parse(data)
                _cache_writer()(cache_path, data)
                return catalog
            except Exception as exc:
                logging.warning("Could not load %s manifest from %s: %s", log_label, url, exc)
    for candidate in (cache_path, bundled_path):
        if candidate.is_file():
            try:
                return parse(candidate.read_bytes())
            except Exception as exc:
                logging.warning("%s manifest is invalid (%s): %s", log_label, candidate, exc)
    raise RuntimeError(error_message)


def select_by_identity(catalog: Iterable[T], identity) -> Optional[T]:
    """Return the package matching the exact device identity, if any."""
    wanted = (
        identity.firmware,
        identity.platform,
        identity.architecture,
        identity.xochitl_sha256,
    )
    for package in catalog:
        if (
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
        ) == wanted:
            return package
    return None


# -- Packages -------------------------------------------------------------------
def load_local_package(
    package, source_path: str | Path, destination: Path, feature_label: str
) -> Path:
    """Verify a manually downloaded archive and store it in the package cache."""
    data = Path(source_path).read_bytes()
    _package_download.verify_local_package(data, package.size, package.sha256, feature_label)
    _cache_writer()(destination, data)
    return destination


def cached_package_is_valid(destination: Path, package) -> bool:
    if not destination.is_file():
        return False
    data = destination.read_bytes()
    return len(data) == package.size and hashlib.sha256(data).hexdigest() == package.sha256


def extract_verified_package(
    archive_path: str | Path,
    package,
    destination: str | Path,
    label: str = DEFAULT_LABEL,
) -> Path:
    """Extract only manifest-listed regular files, checking size and SHA-256."""
    archive = Path(archive_path)
    output = Path(destination)
    output.mkdir(parents=True, exist_ok=True)
    expected = {item.path: item for item in package.files}
    seen: set[str] = set()
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                name = safe_relative_path(member.name, label)
                if name not in expected or name in seen or not member.isfile():
                    raise RuntimeError(f"{label}资源包包含未授权文件。")
                spec = expected[name]
                if member.size != spec.size:
                    raise RuntimeError(f"{label}资源 {name} 大小不匹配。")
                source = bundle.extractfile(member)
                if source is None:
                    raise RuntimeError(f"无法读取{label}资源 {name}。")
                target = output.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with source, target.open("wb") as destination_file:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        destination_file.write(chunk)
                if digest.hexdigest() != spec.sha256:
                    raise RuntimeError(f"{label}资源 {name} SHA-256 校验失败。")
                os.chmod(target, spec.mode)
                seen.add(name)
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError(f"无法解压{label}资源包。") from exc
    if seen != set(expected):
        raise RuntimeError(f"{label}资源包缺少清单文件。")
    return output
