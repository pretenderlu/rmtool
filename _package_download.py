"""Shared download-failure reporting for firmware plugin packages.

Every feature module downloads its exact-firmware packages from the same two
mirrors: GitHub Releases first and the Tencent COS mirror as the fallback.
When both mirrors fail, the raised :class:`PackageDownloadError` keeps the
exact URLs so the UI can show them and offer loading a manually downloaded
copy that still passes the manifest size and SHA-256 verification.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Callable, Optional


def _mirror_label(url: str) -> str:
    if "github.com" in url:
        return "GitHub（默认）"
    if "myqcloud.com" in url:
        return "腾讯云 COS（备用）"
    return url.split("//", 1)[-1].split("/", 1)[0]


class PackageDownloadError(RuntimeError):
    """Both package mirrors failed; carries URLs and a manual-load hook."""

    def __init__(
        self,
        feature_label: str,
        asset: str,
        urls,
        size: int,
        sha256: str,
        store: Optional[Callable[[str], object]] = None,
    ):
        self.feature_label = feature_label
        self.asset = asset
        self.urls = tuple(urls)
        self.size = size
        self.sha256 = sha256
        # Provided by the raising module: stores a verified local file into
        # the feature cache so the next install attempt reuses it.
        self.store = store
        mirror_lines = "\n".join(
            f"{index}. {_mirror_label(url)}：{url}"
            for index, url in enumerate(self.urls, start=1)
        )
        super().__init__(
            f"无法自动下载并校验{feature_label}资源包（{asset}），"
            "已依次尝试以下镜像均未成功：\n"
            f"{mirror_lines}\n"
            "可手动下载任一地址的文件，然后选择“手动加载资源包”完成校验和安装。"
        )


def download_verified_package(
    package, destination: Path, maximum: int, *, feature_label: str,
    mismatch_message: str, log_label: str, store: Callable[[str], object],
) -> Path:
    """Reuse a verified cache or try each mirror before offering manual loading."""
    # tap imports this module; resolve its helpers at call time to avoid a cycle.
    import _tap_page_turn as tap

    if not 0 < package.size <= maximum:
        raise RuntimeError(f"{feature_label}资源包大小超过允许范围。")
    if destination.is_file() and destination.stat().st_size == package.size:
        data = destination.read_bytes()
        if len(data) == package.size and hashlib.sha256(data).hexdigest() == package.sha256:
            return destination
    last_error: Optional[Exception] = None
    for url in package.download_urls:
        try:
            data = tap._download_limited(url, maximum)
            if len(data) != package.size or hashlib.sha256(data).hexdigest() != package.sha256:
                raise RuntimeError(mismatch_message)
            tap._write_atomic(destination, data)
            return destination
        except Exception as exc:
            last_error = exc
            logging.warning("Could not download %s package from %s: %s", log_label, url, exc)
    raise PackageDownloadError(
        feature_label, package.asset, package.download_urls,
        package.size, package.sha256, store=store,
    ) from last_error


def verify_local_package(data: bytes, size: int, sha256: str, feature_label: str) -> None:
    """Verify a manually provided package against its manifest entry."""
    if len(data) != size:
        raise RuntimeError(
            f"所选文件大小与清单不符（期望 {size} 字节，实际 {len(data)} 字节），"
            f"不是可用的{feature_label}资源包。"
        )
    if hashlib.sha256(data).hexdigest() != sha256:
        raise RuntimeError(
            f"所选文件 SHA-256 与清单不符，不是可用的{feature_label}资源包。"
        )
