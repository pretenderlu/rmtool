"""Exact-firmware fast monochrome reading support for color reMarkable devices."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import shlex
import tempfile
import urllib.request
import uuid
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

import _tap_page_turn as tap
import _xovi_standalone


REPO_URL = "https://github.com/pretenderlu/rmtool"
ASSET_RELEASE_URL = f"{REPO_URL}/releases/download/fast-mono-reading-assets"
COS_URL = (
    "https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/"
    "fast-mono-reading"
)
REMOTE_BASE_URLS = (COS_URL, ASSET_RELEASE_URL)
MANIFEST_URLS = tuple(f"{base_url}/manifest.json" for base_url in REMOTE_BASE_URLS)
MANIFEST_URL = MANIFEST_URLS[0]
BUNDLED_MANIFEST = Path(__file__).with_name("fast-mono-reading") / "manifest.json"

ALLOWED_TARGETS = {
    (
        "ferrari",
        "20260506100933",
        "aarch64",
        "29b9896b07f59636d910d8a740f6562c502f676a1a70f8814459229d25cc5288",
    ): ("3.27.1.0", "stable", True, False),
    (
        "chiappa",
        "20260506100933",
        "aarch64",
        "4646e0aef1cef2b3417889073ad5faba9259ae6b41f68326e75ef9a5c520c322",
    ): ("3.27.1.0", "stable", True, False),
    (
        "ferrari",
        "20260612085811",
        "aarch64",
        "9749880daa2f10844e77b560ec0ecddd1634d43eb328af637c7026edf3ef120e",
    ): ("3.27.3.0", "stable", True, False),
    (
        "chiappa",
        "20260612085811",
        "aarch64",
        "227a9bfe928ef5d164359e490d97648ffca40a5de13f07a9eb57a618a403f084",
    ): ("3.27.3.0", "stable", True, False),
    (
        "ferrari",
        "20260629074044",
        "aarch64",
        "10082aeb857c69c3f404ab189d7403318ba97d0c169e756ae9a5b3532b248a4a",
    ): ("3.28.0.162", "beta", True, False),
    (
        "chiappa",
        "20260629074044",
        "aarch64",
        "9e3e0372a15da25b148ac17667feb566014440e079c3e3ee504112d556ad2e10",
    ): ("3.28.0.162", "beta", True, False),
    (
        "ferrari",
        "20260702125656",
        "aarch64",
        "49f60572e830f6c4f20d800a56d644cdf53cd65a8e240b2b27106cce55040f89",
    ): ("3.28.0.163", "beta", True, False),
    (
        "chiappa",
        "20260702125656",
        "aarch64",
        "08171df6296b99d04b3694b337bd0ce911e6a93356955961a37de9dd93a0394d",
    ): ("3.28.0.163", "beta", True, False),
    (
        "ferrari",
        "20260702125656",
        "aarch64",
        "113bf7ea62ad171ea03c77c1f90e0666bcff163242a22ebca84372533b270c1c",
    ): ("3.28.0.164", "beta", True, False),
    (
        "chiappa",
        "20260702125656",
        "aarch64",
        "3a9e18483b73f43016fb25b451e3ece0efba7aa1cc92e080771e138ce6bbca98",
    ): ("3.28.0.164", "beta", True, False),
}

REMOTE_BASE = "/home/root/.local/share/rmtool/fast-mono-reading"
DROPIN_NAME = "91-rmtool-fast-mono-reading.conf"
DROPIN_PATH = f"/etc/systemd/system/xochitl.service.d/{DROPIN_NAME}"
MARKER_PATH = f"{REMOTE_BASE}/package.json"
LAUNCHER_PATH = f"{REMOTE_BASE}/launcher.sh"

SHARED_QMD = f"{tap.SHARED_QRR_HOME}/rmtool-fast-mono-reading.qmd"
VELLUM_PACKAGE_NAME = "rmtool-fast-mono-reading"
VELLUM_CONFLICTS = ("rmtool-fast-mono-reading-canary",)
VELLUM_LICENSE_DIR = f"{tap.VELLUM_ROOT}/licenses/{VELLUM_PACKAGE_NAME}"
VELLUM_LICENSE_PATH = f"{VELLUM_LICENSE_DIR}/LICENSE"
VELLUM_SOURCES_PATH = f"{VELLUM_LICENSE_DIR}/SOURCES"

MAX_MANIFEST_BYTES = tap.MAX_MANIFEST_BYTES
MAX_PACKAGE_BYTES = tap.MAX_PACKAGE_BYTES
MAX_UNPACKED_BYTES = tap.MAX_UNPACKED_BYTES
QMD_PAYLOAD_PATH = "exthome/qt-resource-rebuilder/fast-mono-reading.qmd"
_KNOWN_SHARED_PREDECESSOR_QMDS = {
    (3, "20260506100933"): (
        (
            1,
            "0fa777c1278318d1f98d18e7bbdbbb5dfadbd5baf463e4d7a8df0107c36a0f9d",
            3990,
        ),
        (
            2,
            "4d8f829d81d83f84d37e16668a3366468758c04b4247b809f8f843d6d0abcc8d",
            9327,
        ),
    ),
    (3, "20260612085811"): (
        (
            1,
            "0fa777c1278318d1f98d18e7bbdbbb5dfadbd5baf463e4d7a8df0107c36a0f9d",
            3990,
        ),
        (
            2,
            "4d8f829d81d83f84d37e16668a3366468758c04b4247b809f8f843d6d0abcc8d",
            9327,
        ),
    ),
    (3, "20260629074044"): (
        (
            1,
            "587844a02383b70b1851b78b1d0bb3a5a2ff6c38559d6d3c78ac673bd964f18f",
            3106,
        ),
        (
            2,
            "7fec635a5939b1929959e84464bccfe0788d905e91d1e1704f1d0ec980237a4a",
            8448,
        ),
    ),
    (3, "20260702125656"): (
        (
            1,
            "587844a02383b70b1851b78b1d0bb3a5a2ff6c38559d6d3c78ac673bd964f18f",
            3106,
        ),
        (
            2,
            "7fec635a5939b1929959e84464bccfe0788d905e91d1e1704f1d0ec980237a4a",
            8448,
        ),
    ),
}
_RUNTIME_PATHS = {
    "xovi.so",
    "extensions.d/qt-resource-rebuilder.so",
    QMD_PAYLOAD_PATH,
    "exthome/qt-resource-rebuilder/hashtab",
}
_PAYLOAD_PATHS = _RUNTIME_PATHS | {
    "qmd-tool",
    "LICENSE.qmd-tool",
    "LICENSE.rm-xovi-extensions",
    "LICENSE.xovi",
}
_ASSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz")
_STANDALONE_LAYOUT = _xovi_standalone.StandaloneLayout(
    remote_base=REMOTE_BASE,
    dropin_name=DROPIN_NAME,
    log_tag="rmtool-fast-mono-reading",
    mount_tag="rmtool-fast-mono",
)

PayloadFile = tap.PayloadFile
DeviceIdentity = tap.DeviceIdentity


class FastMonoReadingState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    INSTALLED_DISABLED = "installed_disabled"
    ENABLE_PENDING_REBOOT = "enable_pending_reboot"
    WAITING_FOR_XOVI = "waiting_for_xovi"
    ENABLED = "enabled"
    DISABLE_PENDING_REBOOT = "disable_pending_reboot"
    OUTDATED = "outdated"
    FIRMWARE_RESIDUE = "firmware_residue"
    BROKEN = "broken"


@dataclass(frozen=True)
class FastMonoReadingPackage:
    firmware: str
    release_version: str
    channel: str
    platform: str
    architecture: str
    xochitl_sha256: str
    asset: str
    sha256: str
    size: int
    files: tuple[PayloadFile, ...]
    package_revision: int
    offline_verified: bool
    device_verified: bool

    @property
    def package_id(self) -> str:
        return f"{self.platform}-{self.firmware}-{self.xochitl_sha256[:12]}"

    @property
    def download_url(self) -> str:
        return self.download_urls[0]

    @property
    def download_urls(self) -> tuple[str, ...]:
        return tuple(f"{base_url}/{self.asset}" for base_url in REMOTE_BASE_URLS)

    def file(self, path: str) -> PayloadFile:
        for item in self.files:
            if item.path == path:
                return item
        raise KeyError(path)


@dataclass(frozen=True)
class FastMonoReadingStatus:
    state: FastMonoReadingState
    identity: DeviceIdentity
    package: Optional[FastMonoReadingPackage] = None
    available_packages: tuple[FastMonoReadingPackage, ...] = ()
    detail: str = ""
    recovery_available: bool = False


def _expected_asset_name(
    platform: str,
    firmware: str,
    release_version: str,
) -> str:
    default = f"rmtool-fast-mono-reading-{platform}-{firmware}.tar.gz"
    releases = sorted(
        policy[0]
        for identity, policy in ALLOWED_TARGETS.items()
        if identity[0] == platform and identity[1] == firmware
    )
    if len(releases) > 1 and release_version != releases[0]:
        return default.removesuffix(".tar.gz") + f"-{release_version}.tar.gz"
    return default


def parse_manifest(
    data: bytes, *, require_local_match: bool = True
) -> tuple[FastMonoReadingPackage, ...]:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("快速黑白清单不是有效 JSON。") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise RuntimeError("快速黑白清单版本不受支持。")
    entries = document.get("packages")
    if not isinstance(entries, list):
        raise RuntimeError("快速黑白清单缺少 packages。")

    packages: list[FastMonoReadingPackage] = []
    identities: set[tuple[str, str, str]] = set()
    assets: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("快速黑白清单包格式无效。")
        firmware = tap._required_string(entry, "firmware", tap._FIRMWARE_RE)
        release_version = tap._required_string(
            entry, "release_version", tap._VERSION_RE
        )
        platform = tap._required_string(entry, "platform", tap._PLATFORM_RE)
        architecture = tap._required_string(entry, "architecture", tap._ARCH_RE)
        xochitl_sha = tap._required_string(entry, "xochitl_sha256", tap._SHA256_RE)
        asset = tap._required_string(entry, "asset", _ASSET_RE)
        digest = tap._required_string(entry, "sha256", tap._SHA256_RE)
        channel = entry.get("channel")
        size = entry.get("size")
        revision = entry.get("package_revision")
        offline_verified = entry.get("offline_verified")
        device_verified = entry.get("device_verified")
        if type(offline_verified) is not bool or type(device_verified) is not bool:
            raise RuntimeError("快速黑白包验证级别必须是布尔值。")
        if revision != 3:
            raise RuntimeError("快速黑白包修订版本必须是 r3。")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or size > MAX_PACKAGE_BYTES
        ):
            raise RuntimeError("快速黑白资源包大小无效。")
        file_entries = entry.get("files")
        if not isinstance(file_entries, list) or not file_entries:
            raise RuntimeError("快速黑白资源包缺少文件清单。")
        files = tuple(tap._parse_payload_file(item) for item in file_entries)
        paths = [item.path for item in files]
        if len(paths) != len(set(paths)) or set(paths) != _PAYLOAD_PATHS:
            raise RuntimeError("快速黑白资源包文件清单与固定白名单不匹配。")
        if sum(item.size for item in files) > MAX_UNPACKED_BYTES:
            raise RuntimeError("快速黑白资源包解压后过大。")

        identity = (platform, firmware, xochitl_sha)
        if identity in identities or asset in assets:
            raise RuntimeError("快速黑白清单包含重复包。")
        expected = ALLOWED_TARGETS.get(
            (platform, firmware, architecture, xochitl_sha)
        )
        if expected is None:
            raise RuntimeError("快速黑白清单包含未列入本地白名单的设备身份。")
        if expected != (
            release_version,
            channel,
            offline_verified,
            device_verified,
        ):
            raise RuntimeError("快速黑白清单的版本、渠道或验证级别与本地白名单不一致。")
        expected_asset = _expected_asset_name(
            platform, firmware, release_version
        )
        if asset != expected_asset:
            raise RuntimeError("快速黑白资源包文件名与本地白名单不一致。")
        identities.add(identity)
        assets.add(asset)
        packages.append(
            FastMonoReadingPackage(
                firmware,
                release_version,
                channel,
                platform,
                architecture,
                xochitl_sha,
                asset,
                digest,
                size,
                files,
                revision,
                offline_verified,
                device_verified,
            )
        )
    result = tuple(packages)
    if require_local_match and result != _trusted_catalog():
        raise RuntimeError("快速黑白清单与本地完整目标信任清单不一致。")
    return result


@lru_cache(maxsize=1)
def _trusted_catalog() -> tuple[FastMonoReadingPackage, ...]:
    if not BUNDLED_MANIFEST.is_file():
        raise RuntimeError("缺少内置快速黑白信任清单。")
    return parse_manifest(
        BUNDLED_MANIFEST.read_bytes(), require_local_match=False
    )


def _cache_dir(state_dir: str) -> Path:
    return Path(state_dir) / "cache" / "fast-mono-reading"


def load_catalog(
    state_dir: str, *, refresh: bool = True
) -> tuple[FastMonoReadingPackage, ...]:
    manifest_path = _cache_dir(state_dir) / "manifest.json"
    if refresh:
        for manifest_url in MANIFEST_URLS:
            try:
                data = tap._download_limited(manifest_url, MAX_MANIFEST_BYTES)
                catalog = parse_manifest(data)
                tap._write_atomic(manifest_path, data)
                return catalog
            except Exception as exc:
                logging.warning(
                    "Could not load fast-mono manifest from %s: %s",
                    manifest_url,
                    exc,
                )
    for candidate in (manifest_path, BUNDLED_MANIFEST):
        if candidate.is_file():
            try:
                return parse_manifest(candidate.read_bytes())
            except Exception as exc:
                logging.warning("Fast-mono manifest is invalid (%s): %s", candidate, exc)
    raise RuntimeError("无法获取快速黑白清单，且没有可用的内置清单。")


def download_package(package: FastMonoReadingPackage, state_dir: str) -> Path:
    destination = _cache_dir(state_dir) / package.firmware / package.asset
    if destination.is_file():
        data = destination.read_bytes()
        if len(data) == package.size and hashlib.sha256(data).hexdigest() == package.sha256:
            return destination
    last_error: Optional[Exception] = None
    for download_url in package.download_urls:
        try:
            data = tap._download_limited(download_url, MAX_PACKAGE_BYTES)
            if (
                len(data) != package.size
                or hashlib.sha256(data).hexdigest() != package.sha256
            ):
                raise RuntimeError("快速黑白资源包与清单校验不匹配。")
            tap._write_atomic(destination, data)
            return destination
        except Exception as exc:
            last_error = exc
            logging.warning(
                "Could not download fast-mono package from %s: %s",
                download_url,
                exc,
            )
    raise RuntimeError("无法从可用镜像下载并校验快速黑白资源包。") from last_error


def extract_verified_package(
    archive_path: str | Path,
    package: FastMonoReadingPackage,
    destination: str | Path,
) -> Path:
    return tap.extract_verified_package(archive_path, package, destination)


def select_package(
    catalog: Iterable[FastMonoReadingPackage], identity: DeviceIdentity
) -> Optional[FastMonoReadingPackage]:
    trusted = _trusted_catalog()
    for package in catalog:
        expected = ALLOWED_TARGETS.get(
            (
                package.platform,
                package.firmware,
                package.architecture,
                package.xochitl_sha256,
            )
        )
        if (
            package in trusted
            and expected == (
                package.release_version,
                package.channel,
                package.offline_verified,
                package.device_verified,
            )
            and package.firmware == identity.firmware
            and package.platform == identity.platform
            and package.architecture == identity.architecture
            and package.xochitl_sha256 == identity.xochitl_sha256
        ):
            return package
    return None


def _vellum_package_version(package: FastMonoReadingPackage) -> str:
    return f"{package.release_version}-r{package.package_revision}"


def _build_vellum_apk(
    package: FastMonoReadingPackage, qmd: bytes, license_text: bytes
) -> bytes:
    qmd_spec = package.file(QMD_PAYLOAD_PATH)
    if hashlib.sha256(qmd).hexdigest() != qmd_spec.sha256:
        raise RuntimeError("Vellum APK 的 QMD 与快速黑白清单不匹配。")
    qmd_source = (
        "fast-mono-reading/qmd-src/fast-mono-reading-3.28.qmd"
        if package.release_version.startswith("3.28.")
        else "fast-mono-reading/qmd-src/fast-mono-reading-3.27.qmd"
    )
    sources = (
        f"repository = {REPO_URL}\n"
        f"source = {qmd_source}\n"
        f"firmware = {package.release_version} ({package.firmware})\n"
    ).encode("ascii")
    data_files = {
        SHARED_QMD.removeprefix("/"): (qmd, 0o644),
        f"{VELLUM_LICENSE_DIR.removeprefix('/')}/LICENSE": (license_text, 0o644),
        f"{VELLUM_LICENSE_DIR.removeprefix('/')}/SOURCES": (sources, 0o644),
    }
    data_member = tap._gzip_member(tap._tar_member(data_files))
    device_package = tap._VELLUM_DEVICE_PACKAGE.get(package.platform)
    if device_package not in ("rmpp", "rmppmove"):
        raise RuntimeError("快速黑白包没有对应的 Vellum 彩色设备依赖。")
    dependencies = (
        "qt-resource-rebuilder>=19.0.0",
        "qt-resource-rebuilder<20.0.0",
        "appload>=0.5.3",
        f"remarkable-os={package.release_version}-r0",
        f"{device_package}=1.0.0-r0",
        *(f"!{name}" for name in VELLUM_CONFLICTS),
    )
    pkginfo = "\n".join(
        (
            "# Generated by rmtool",
            f"pkgname = {VELLUM_PACKAGE_NAME}",
            f"pkgver = {_vellum_package_version(package)}",
            "pkgdesc = Session-scoped fast monochrome PDF and EPUB reading toggle",
            f"url = {REPO_URL}",
            "builddate = 0",
            "packager = rmtool",
            f"size = {sum(len(data) for data, _mode in data_files.values())}",
            "arch = noarch",
            f"origin = {VELLUM_PACKAGE_NAME}",
            "license = GPL-3.0-only",
            *(f"depend = {dependency}" for dependency in dependencies),
            f"datahash = {hashlib.sha256(data_member).hexdigest()}",
            "",
        )
    ).encode("utf-8")
    control = tap._gzip_member(
        tap._tar_member(
            {".PKGINFO": (pkginfo, 0o644)},
            apk_checksums=False,
            end_archive=False,
        )
    )
    return control + data_member


def _launcher(package: FastMonoReadingPackage) -> str:
    return _xovi_standalone.launcher(
        package, package.files, _RUNTIME_PATHS, _STANDALONE_LAYOUT
    )


def _dropin(package: FastMonoReadingPackage) -> str:
    del package
    return _xovi_standalone.dropin(_RUNTIME_PATHS, _STANDALONE_LAYOUT)


def _standalone_marker(
    package: FastMonoReadingPackage, launcher_sha: str, dropin_sha: str
) -> bytes:
    document = {
        "schema_version": 1,
        "deployment_mode": "standalone",
        "package_id": package.package_id,
        "firmware": package.firmware,
        "platform": package.platform,
        "xochitl_sha256": package.xochitl_sha256,
        "launcher_sha256": launcher_sha,
        "dropin_sha256": dropin_sha,
    }
    return (json.dumps(document, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")


def _shared_specs(package: FastMonoReadingPackage):
    return _xovi_standalone.specs_from_package(
        package, "fast-mono-reading", QMD_PAYLOAD_PATH
    )


def _known_shared_predecessor_specs(
    package: FastMonoReadingPackage,
) -> tuple[tuple[int, _xovi_standalone.SharedFeatureSpec], ...]:
    predecessors = _KNOWN_SHARED_PREDECESSOR_QMDS.get(
        (package.package_revision, package.firmware)
    )
    if predecessors is None:
        return ()
    _runtime, current = _shared_specs(package)
    return tuple(
        (
            revision,
            _xovi_standalone.SharedFeatureSpec(
                current.feature_id,
                current.package_id,
                current.archive_path,
                current.runtime_path,
                sha256,
                size,
                current.mode,
            ),
        )
        for revision, sha256, size in predecessors
    )


def _inspect_shared_revision(
    ssh_client,
    runtime: _xovi_standalone.SharedRuntimeSpec,
    trusted: dict[str, _xovi_standalone.SharedFeatureSpec],
    package: FastMonoReadingPackage,
    *,
    check_lower: bool = False,
    firmware_residue_identity: Optional[tuple[str, str, str, str]] = None,
):
    def inspect(candidate):
        if firmware_residue_identity is not None:
            return _xovi_standalone.inspect_shared_firmware_residue(
                ssh_client,
                runtime,
                candidate,
                firmware_residue_identity,
            )
        return _xovi_standalone.inspect_shared(
            ssh_client,
            runtime,
            candidate,
            check_lower=check_lower,
        )

    try:
        inspection = inspect(trusted)
        return inspection, trusted, False
    except RuntimeError as current_error:
        predecessors = _known_shared_predecessor_specs(package)
        if not predecessors:
            raise
        for _revision, predecessor in predecessors:
            predecessor_trusted = dict(trusted)
            predecessor_trusted[predecessor.feature_id] = predecessor
            try:
                inspection = inspect(predecessor_trusted)
            except RuntimeError:
                continue
            return inspection, predecessor_trusted, True
        raise current_error


def _legacy_spec(package: FastMonoReadingPackage):
    runtime, feature = _shared_specs(package)
    launcher_sha = hashlib.sha256(_launcher(package).encode()).hexdigest()
    dropin_sha = hashlib.sha256(_dropin(package).encode()).hexdigest()
    return _xovi_standalone.LegacyStandaloneSpec(
        feature,
        runtime,
        _STANDALONE_LAYOUT,
        json.loads(_standalone_marker(package, launcher_sha, dropin_sha)),
        tuple(
            _xovi_standalone.SharedFileSpec(
                item.path, item.sha256, item.size, item.mode
            )
            for item in package.files
        ),
    )


def _trusted_shared_context(identity: DeviceIdentity):
    package = select_package(_trusted_catalog(), identity)
    if package is None:
        raise RuntimeError("内置快速黑白清单没有当前设备的精确包。")
    runtime, feature = _shared_specs(package)
    trusted = {feature.feature_id: feature}
    tap_package = tap.select_package(tap._trusted_catalog(), identity)
    if tap_package is None:
        raise RuntimeError("内置点击翻页清单没有当前设备的精确包。")
    tap_runtime, tap_feature = tap._shared_specs(tap_package)
    if tap_runtime != runtime:
        raise RuntimeError("快速黑白与点击翻页的内置运行资源不一致。")
    trusted[tap_feature.feature_id] = tap_feature
    return runtime, trusted, (_legacy_spec(package), tap._legacy_spec(tap_package))


def _trusted_shared_context_from_marker(ssh_client):
    identity = tap.DeviceIdentity(*_xovi_standalone.read_shared_identity(ssh_client))
    return _trusted_shared_context(identity)


def _vellum_marker(
    package: FastMonoReadingPackage, *, enabled: bool, process_token: str
) -> bytes:
    document = {
        "schema_version": 2,
        "deployment_mode": "vellum",
        "package_id": package.package_id,
        "firmware": package.firmware,
        "platform": package.platform,
        "xochitl_sha256": package.xochitl_sha256,
        "enabled": enabled,
        "process_token": process_token,
        "vellum_package": VELLUM_PACKAGE_NAME,
        "vellum_version": _vellum_package_version(package),
        "qmd_path": SHARED_QMD,
        "qmd_sha256": package.file(QMD_PAYLOAD_PATH).sha256,
    }
    return (json.dumps(document, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")


def _read_marker(ssh_client) -> dict:
    marker = json.loads(tap._remote_text(ssh_client, MARKER_PATH))
    if not isinstance(marker, dict):
        raise RuntimeError("设备快速黑白安装标记格式无效。")
    return marker


def _active_with_standalone(ssh_client) -> bool:
    command = (
        "pid=$(systemctl show xochitl -p MainPID --value 2>/dev/null || true); "
        '[ -n "$pid" ] && [ "$pid" != 0 ] && '
        f"grep -Fq '{REMOTE_BASE}/xovi.so' /proc/$pid/maps 2>/dev/null"
    )
    _stdout, _stderr, code = ssh_client.exec_command(command)
    return code == 0


def _vellum_payload_paths_valid(ssh_client) -> bool:
    expected_files = {
        SHARED_QMD.lstrip("/"),
        VELLUM_LICENSE_PATH.lstrip("/"),
        VELLUM_SOURCES_PATH.lstrip("/"),
    }
    allowed = set(expected_files)
    for path in expected_files:
        parent = PurePosixPath(path).parent
        while str(parent) not in ("", "."):
            allowed.add(str(parent))
            parent = parent.parent
    paths = tap._vellum_package_paths(ssh_client, VELLUM_PACKAGE_NAME)
    return expected_files <= paths and paths <= allowed


def _standalone_payload_valid(
    ssh_client, package: FastMonoReadingPackage
) -> tuple[bool, str]:
    try:
        marker = _read_marker(ssh_client)
        if marker.get("schema_version") != 1 or marker.get("deployment_mode") != "standalone":
            return False, "standalone 安装标记版本无效"
        launcher_sha = hashlib.sha256(_launcher(package).encode("utf-8")).hexdigest()
        dropin_sha = hashlib.sha256(_dropin(package).encode("utf-8")).hexdigest()
        if marker.get("package_id") != package.package_id:
            return False, "设备安装标记与当前包不匹配"
        if marker.get("launcher_sha256") != launcher_sha:
            return False, "启动包装器标记不匹配"
        if marker.get("dropin_sha256") != dropin_sha:
            return False, "systemd 配置标记不匹配"
        if tap._remote_sha256(ssh_client, DROPIN_PATH) != dropin_sha:
            return False, "systemd 配置已被修改"
        if tap._remote_sha256(ssh_client, LAUNCHER_PATH) != launcher_sha:
            return False, "启动包装器已被修改"
        for item in package.files:
            if item.path in _RUNTIME_PATHS:
                remote = posixpath.join(REMOTE_BASE, item.path)
                if tap._remote_sha256(ssh_client, remote) != item.sha256:
                    return False, f"运行资源 {item.path} 已被修改"
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _vellum_payload_valid(
    ssh_client,
    package: FastMonoReadingPackage,
    marker: dict,
    *,
    package_revision: Optional[int] = None,
    qmd_sha256: Optional[str] = None,
) -> tuple[bool, str]:
    try:
        if marker.get("schema_version") != 2 or marker.get("deployment_mode") != "vellum":
            return False, "Vellum 安装标记版本无效"
        if marker.get("package_id") != package.package_id:
            return False, "Vellum 安装标记与当前包不匹配"
        if not isinstance(marker.get("enabled"), bool):
            return False, "Vellum 安装标记的启用状态无效"
        process_token = str(marker.get("process_token", ""))
        if not tap._PROCESS_TOKEN_RE.fullmatch(process_token):
            return False, "Vellum 安装标记的进程身份无效"
        expected = json.loads(
            _vellum_marker(
                package,
                enabled=marker["enabled"],
                process_token=process_token,
            )
        )
        if package_revision is not None:
            expected["vellum_version"] = (
                f"{package.release_version}-r{package_revision}"
            )
        if qmd_sha256 is not None:
            expected["qmd_sha256"] = qmd_sha256
        if set(marker) != set(expected):
            return False, "Vellum 安装标记字段集合不匹配"
        for key, value in expected.items():
            if marker.get(key) != value:
                return False, f"Vellum 安装标记字段 {key} 不匹配"
        installed = tap._vellum_installed_version(ssh_client, VELLUM_PACKAGE_NAME)
        if marker["enabled"]:
            if installed != expected["vellum_version"]:
                return False, "Vellum 快速黑白包未安装或版本不匹配"
            tap._assert_vellum_runtime(ssh_client, package)
            if not tap._vellum_package_owns_path(
                ssh_client, VELLUM_PACKAGE_NAME, SHARED_QMD
            ):
                return False, "Vellum 未确认快速黑白 QMD 所有权"
            if not _vellum_payload_paths_valid(ssh_client):
                return False, "Vellum 快速黑白包拥有预期范围外的文件"
            if tap._remote_sha256(ssh_client, SHARED_QMD) != expected["qmd_sha256"]:
                return False, "快速黑白 QMD 已变化"
        elif installed is not None or ssh_client.file_exists(SHARED_QMD):
            return False, "停用状态下快速黑白 Vellum 包仍存在"
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _vellum_payload_revision(
    ssh_client,
    package: FastMonoReadingPackage,
    marker: dict,
) -> tuple[Optional[int], str]:
    valid, detail = _vellum_payload_valid(ssh_client, package, marker)
    if valid:
        return package.package_revision, ""
    for revision, predecessor in _known_shared_predecessor_specs(package):
        predecessor_valid, _predecessor_detail = _vellum_payload_valid(
            ssh_client,
            package,
            marker,
            package_revision=revision,
            qmd_sha256=predecessor.sha256,
        )
        if predecessor_valid:
            return revision, ""
    return None, detail


def _marker_dir_has_only_marker(ssh_client) -> bool:
    output = ssh_client.exec_checked(
        f"find {shlex.quote(REMOTE_BASE)} -mindepth 1 -maxdepth 1 -print 2>/dev/null"
    )
    return tuple(sorted(line.strip() for line in output.splitlines() if line.strip())) == (
        MARKER_PATH,
    )


def get_status(
    ssh_client, catalog: Iterable[FastMonoReadingPackage]
) -> FastMonoReadingStatus:
    packages = tuple(catalog)
    identity = tap.get_device_identity(ssh_client)
    available = tuple(item for item in packages if item.platform == identity.platform)
    package = select_package(packages, identity)
    marker_exists = ssh_client.file_exists(MARKER_PATH)
    dropin_exists = ssh_client.file_exists(DROPIN_PATH)
    shared_exists = _xovi_standalone.has_shared_artifacts(ssh_client)
    vellum_version = None
    vellum_error = ""
    if ssh_client.file_exists(tap.VELLUM_BIN):
        try:
            vellum_version = tap._vellum_installed_version(
                ssh_client, VELLUM_PACKAGE_NAME
            )
        except Exception as exc:
            vellum_error = str(exc)
    recovery = marker_exists or dropin_exists or vellum_version is not None or shared_exists
    if shared_exists and package is not None:
        try:
            shared_identity = tap.DeviceIdentity(
                *_xovi_standalone.read_shared_identity(ssh_client)
            )
        except Exception:
            shared_identity = identity
        if shared_identity != identity:
            try:
                runtime, trusted, legacies = _trusted_shared_context(
                    shared_identity
                )
                marker_package = select_package(
                    _trusted_catalog(), shared_identity
                )
                if marker_package is None:
                    raise RuntimeError(
                        "内置快速黑白清单无法验证该共享安装。"
                    )
                if any(
                    ssh_client.file_exists(path)
                    for legacy in legacies
                    for path in (
                        legacy.layout.remote_base,
                        legacy.marker_path,
                        legacy.layout.dropin_path,
                    )
                ):
                    raise RuntimeError("检测到共享与旧版/Vellum Xovi 混合布局。")
                inspection, _installed_trusted, _outdated = (
                    _inspect_shared_revision(
                        ssh_client,
                        runtime,
                        trusted,
                        marker_package,
                        firmware_residue_identity=(
                            identity.firmware,
                            identity.platform,
                            identity.architecture,
                            identity.xochitl_sha256,
                        ),
                    )
                )
                return FastMonoReadingStatus(
                    FastMonoReadingState.FIRMWARE_RESIDUE,
                    identity,
                    package,
                    available,
                    "旧共享目录与内置旧包完全一致，且上下层 drop-in 均已由固件升级移除；"
                    "旧功能当前未载入。清理会一并移除点击翻页和快速黑白的旧共享状态，"
                    "随后两项功能均可安装当前固件版本。",
                    True,
                )
            except Exception as exc:
                return FastMonoReadingStatus(
                    FastMonoReadingState.BROKEN,
                    identity,
                    package,
                    available,
                    str(exc),
                    True,
                )
    if package is None and shared_exists:
        try:
            runtime, trusted, legacies = _trusted_shared_context_from_marker(
                ssh_client
            )
            marker_identity = tap.DeviceIdentity(
                runtime.firmware,
                runtime.platform,
                runtime.architecture,
                runtime.xochitl_sha256,
            )
            marker_package = select_package(_trusted_catalog(), marker_identity)
            if marker_package is None:
                raise RuntimeError("内置快速黑白清单无法验证该共享安装。")
            if any(
                ssh_client.file_exists(path)
                for legacy in legacies
                for path in (
                    legacy.layout.remote_base,
                    legacy.marker_path,
                    legacy.layout.dropin_path,
                )
            ):
                raise RuntimeError("检测到共享与旧版/Vellum Xovi 混合布局。")
            _inspection, _installed_trusted, outdated = _inspect_shared_revision(
                ssh_client,
                runtime,
                trusted,
                marker_package,
            )
        except Exception as exc:
            return FastMonoReadingStatus(
                FastMonoReadingState.BROKEN,
                identity,
                available_packages=available,
                detail=str(exc),
                recovery_available=True,
            )
        return FastMonoReadingStatus(
            FastMonoReadingState.INCOMPATIBLE,
            identity,
            available_packages=available,
            detail="检测到属于其他固件的有效共享安装；可安全停用，不能在当前固件载入",
            recovery_available=True,
        )
    if package is None:
        return FastMonoReadingStatus(
            FastMonoReadingState.INCOMPATIBLE,
            identity,
            available_packages=available,
            detail="当前硬件、固件、架构或 xochitl 哈希不在八项目标白名单中",
            recovery_available=recovery,
        )

    if shared_exists:
        try:
            runtime, trusted, legacies = _trusted_shared_context(identity)
            if any(
                ssh_client.file_exists(path)
                for legacy in legacies
                for path in (
                    legacy.layout.remote_base,
                    legacy.marker_path,
                    legacy.layout.dropin_path,
                )
            ):
                raise RuntimeError("检测到共享与旧版/Vellum Xovi 混合布局。")
            inspection, _installed_trusted, outdated = _inspect_shared_revision(
                ssh_client,
                runtime,
                trusted,
                package,
            )
            state_record = inspection.states.get("fast-mono-reading")
            if outdated:
                return FastMonoReadingStatus(
                    FastMonoReadingState.OUTDATED,
                    identity,
                    package,
                    available,
                    "已精确验证为 rmtool 安装的旧版快速黑白。"
                    "请先卸载旧版并手动重启，再重新检测并安装新版；"
                    "点击翻页不会被卸载。",
                    True,
                )
            if state_record is None:
                state = FastMonoReadingState.NOT_INSTALLED
                detail = "共享 Xovi 正由另一项 rmtool 功能使用"
            else:
                current = tap._xochitl_process_token(ssh_client)
                process_changed = current != state_record.process_token
                if state_record.enabled:
                    if process_changed and inspection.active:
                        state = FastMonoReadingState.ENABLED
                        detail = ""
                    elif not process_changed:
                        state = FastMonoReadingState.ENABLE_PENDING_REBOOT
                        detail = "等待手动重启后载入共享 Xovi"
                    else:
                        raise RuntimeError("共享 Xovi 未在当前 xochitl 进程中载入。")
                elif process_changed:
                    state = FastMonoReadingState.INSTALLED_DISABLED
                    detail = ""
                else:
                    state = FastMonoReadingState.DISABLE_PENDING_REBOOT
                    detail = "等待手动重启后停用快速黑白"
            return FastMonoReadingStatus(
                state, identity, package, available, detail, True
            )
        except Exception as exc:
            return FastMonoReadingStatus(
                FastMonoReadingState.BROKEN,
                identity,
                package,
                available,
                str(exc),
                True,
            )

    marker = None
    if marker_exists:
        try:
            marker = _read_marker(ssh_client)
        except Exception as exc:
            return FastMonoReadingStatus(
                FastMonoReadingState.BROKEN,
                identity,
                package,
                available,
                str(exc),
                recovery,
            )
    if marker and marker.get("deployment_mode") == "vellum":
        marker_package = tap._package_from_marker(
            (package, *_trusted_catalog()), marker
        )
        if marker_package is None:
            state = FastMonoReadingState.BROKEN
            detail = "Vellum 快速黑白标记不属于任何内置信任包"
        else:
            revision, detail = _vellum_payload_revision(
                ssh_client, marker_package, marker
            )
        if marker_package is not None and revision is None:
            state = FastMonoReadingState.BROKEN
        elif marker_package is not None and (
            marker_package != package
            or revision != package.package_revision
        ):
            state = FastMonoReadingState.OUTDATED
            detail = (
                "已精确验证为 rmtool 安装的旧版快速黑白。"
                "请先卸载旧版并手动重启，再重新检测并安装新版；"
                "点击翻页不会被卸载。"
            )
        elif marker_package is not None:
            try:
                current = tap._xochitl_process_token(ssh_client)
            except Exception as exc:
                state, detail = FastMonoReadingState.BROKEN, str(exc)
            else:
                process_changed = current != marker["process_token"]
                if marker["enabled"]:
                    if not tap._active_with_shared_xovi(ssh_client):
                        state = FastMonoReadingState.WAITING_FOR_XOVI
                        detail = "请按 AppLoader 的正常流程手动激活 Xovi"
                    elif process_changed:
                        state = FastMonoReadingState.ENABLED
                    else:
                        state = FastMonoReadingState.ENABLE_PENDING_REBOOT
                elif process_changed:
                    state = FastMonoReadingState.INSTALLED_DISABLED
                else:
                    state = FastMonoReadingState.DISABLE_PENDING_REBOOT
        return FastMonoReadingStatus(
            state, identity, package, available, detail, recovery
        )
    if vellum_error and (marker or vellum_version is not None):
        return FastMonoReadingStatus(
            FastMonoReadingState.BROKEN,
            identity,
            package,
            available,
            vellum_error,
            recovery,
        )
    if vellum_version is not None:
        return FastMonoReadingStatus(
            FastMonoReadingState.BROKEN,
            identity,
            package,
            available,
            "Vellum 快速黑白包存在，但 rmtool 状态标记缺失或无效",
            True,
        )

    if marker and marker.get("deployment_mode") == "standalone":
        marker_package = tap._package_from_marker(
            (package, *_trusted_catalog()), marker
        )
        if marker_package is None:
            return FastMonoReadingStatus(
                FastMonoReadingState.BROKEN,
                identity,
                package,
                available,
                "旧版快速黑白标记不属于任何内置信任包",
                True,
            )
        if marker_package != package:
            try:
                _xovi_standalone.validate_legacy(
                    ssh_client,
                    _legacy_spec(marker_package),
                    check_lower=True,
                )
            except Exception as exc:
                return FastMonoReadingStatus(
                    FastMonoReadingState.BROKEN,
                    identity,
                    package,
                    available,
                    str(exc),
                    True,
                )
            return FastMonoReadingStatus(
                FastMonoReadingState.OUTDATED,
                identity,
                package,
                available,
                "已精确验证为旧固件的独立快速黑白；"
                "请先卸载旧版，再安装当前固件版本。",
                True,
            )

    active = _active_with_standalone(ssh_client)
    if dropin_exists:
        valid, detail = _standalone_payload_valid(ssh_client, package)
        state = (
            FastMonoReadingState.ENABLED
            if valid and active
            else FastMonoReadingState.ENABLE_PENDING_REBOOT
            if valid
            else FastMonoReadingState.BROKEN
        )
        return FastMonoReadingStatus(
            state, identity, package, available, detail, True
        )
    if active:
        state = FastMonoReadingState.DISABLE_PENDING_REBOOT
    elif ssh_client.file_exists(REMOTE_BASE):
        state = FastMonoReadingState.INSTALLED_DISABLED
    else:
        state = FastMonoReadingState.NOT_INSTALLED
    return FastMonoReadingStatus(
        state, identity, package, available, recovery_available=recovery
    )


def get_cloud_status(ssh_client, state_dir: str) -> FastMonoReadingStatus:
    return get_status(ssh_client, load_catalog(state_dir))


def _tap_state_is_vellum(
    ssh_client, package: FastMonoReadingPackage
) -> bool:
    if not ssh_client.file_exists(tap.REMOTE_BASE):
        return False
    if not ssh_client.file_exists(tap.MARKER_PATH):
        raise RuntimeError("点击翻页目录缺少所有权标记，拒绝部署快速黑白。")
    try:
        marker = tap._read_marker(ssh_client)
    except Exception as exc:
        raise RuntimeError("点击翻页所有权标记无效，拒绝部署快速黑白。") from exc
    mode = marker.get("deployment_mode")
    if mode in (None, "shared_xovi"):
        return False
    if mode != "vellum":
        raise RuntimeError("点击翻页所有权标记的部署模式无效，拒绝部署快速黑白。")
    identity = tap.DeviceIdentity(
        package.firmware,
        package.platform,
        package.architecture,
        package.xochitl_sha256,
    )
    trusted = tap.select_package(tap._trusted_catalog(), identity)
    if trusted is None:
        raise RuntimeError("内置点击翻页清单没有当前设备的精确包。")
    valid, detail = tap._vellum_payload_valid(ssh_client, trusted, marker)
    if not valid:
        raise RuntimeError(detail or "Vellum 点击翻页载荷无法精确验证。")
    if not ssh_client.file_exists(tap.VELLUM_BIN):
        raise RuntimeError("Vellum 不可用，无法与现有点击翻页安装共存。")
    return True


def _deployment_mode(ssh_client, package: FastMonoReadingPackage) -> str:
    disabled_current_vellum = False
    if ssh_client.file_exists(MARKER_PATH):
        marker = _read_marker(ssh_client)
        if marker.get("deployment_mode") == "vellum":
            revision, _detail = _vellum_payload_revision(
                ssh_client, package, marker
            )
            if marker.get("enabled") is not False or revision is None:
                raise RuntimeError(
                    _detail or "现有 Vellum 快速黑白标记无法精确验证，拒绝覆盖。"
                )
            if revision != package.package_revision:
                raise RuntimeError("检测到旧版快速黑白停用标记，请先清除旧版状态。")
            if not _marker_dir_has_only_marker(ssh_client):
                raise RuntimeError("快速黑白标记目录包含未知文件，拒绝覆盖。")
            if not ssh_client.file_exists(tap.VELLUM_BIN):
                raise RuntimeError("Vellum 不可用，无法重新启用现有快速黑白安装。")
            disabled_current_vellum = True
    tap_vellum = _tap_state_is_vellum(ssh_client, package)
    rmtool_standalone = _xovi_standalone.has_shared_artifacts(ssh_client) or any(
        ssh_client.file_exists(path)
        for path in (
            DROPIN_PATH,
            tap.DROPIN_PATH,
        )
    ) or (
        ssh_client.file_exists(tap.REMOTE_BASE) and not tap_vellum
    ) or (ssh_client.file_exists(REMOTE_BASE) and not disabled_current_vellum)
    vellum_feature = None
    if ssh_client.file_exists(tap.VELLUM_BIN):
        vellum_feature = tap._vellum_installed_version(
            ssh_client, VELLUM_PACKAGE_NAME
        )
    if rmtool_standalone and vellum_feature is None:
        if any(
            ssh_client.file_exists(path)
            for path in (
                tap.SHARED_XOVI_LIBRARY,
                tap.SHARED_QRR_LIBRARY,
                tap.SHARED_APPLOAD_LIBRARY,
            )
        ):
            raise RuntimeError("检测到 Vellum 与 rmtool 独立 Xovi 混合布局，拒绝修改。")
        return "standalone"

    if ssh_client.file_exists(DROPIN_PATH):
        raise RuntimeError(
            "检测到已有快速黑白 standalone 配置；请先检测状态并停用旧配置，拒绝直接覆盖。"
        )
    if ssh_client.file_exists(REMOTE_BASE):
        if not ssh_client.file_exists(MARKER_PATH):
            raise RuntimeError("rmtool 快速黑白目录缺少所有权标记，拒绝覆盖。")
        marker = _read_marker(ssh_client)
        if (
            marker.get("package_id") != package.package_id
            or marker.get("deployment_mode") not in ("standalone", "vellum")
        ):
            raise RuntimeError("现有快速黑白安装标记与当前生产包不匹配，拒绝覆盖。")

    tap_base_exists = ssh_client.file_exists(tap.REMOTE_BASE)
    if ssh_client.file_exists(tap.DROPIN_PATH):
        raise RuntimeError(
            "检测到点击翻页正在使用 rmtool 独立 Xovi。本版本暂不支持两项独立运行时共存；请先停用点击翻页，或改用标准 Vellum。"
        )
    if tap_base_exists and not tap_vellum:
        raise RuntimeError(
            "检测到点击翻页的独立或旧版 Xovi 资源。本版本不支持两项 standalone 功能共存。"
        )

    command = f"""
for file in /etc/systemd/system/xochitl.service.d/*.conf; do
    [ -f "$file" ] || continue
    [ "$file" = "{DROPIN_PATH}" ] && continue
    if grep -Eq 'LD_PRELOAD|XOVI_ROOT|ExecStart=.*xovi' "$file"; then
        echo "$file"
    fi
done
""".strip()
    output = ssh_client.exec_checked(command).strip()
    conflicts = output.splitlines() if output else []
    if conflicts and conflicts != [tap.SHARED_XOVI_DROPIN]:
        raise RuntimeError("检测到其他 xochitl/Xovi 持久化配置，拒绝自动合并：" + output)

    vellum_available = ssh_client.file_exists(tap.VELLUM_BIN)
    xovi_installed = False
    if vellum_available:
        package_conflicts = sorted(
            set(VELLUM_CONFLICTS) & tap._vellum_installed_packages(ssh_client)
        )
        if package_conflicts:
            raise RuntimeError(
                "检测到快速黑白测试包，请先运行 `vellum del "
                f"{package_conflicts[0]}`，手动重启并确认测试包已移除后，再安装生产版。"
            )
        xovi_installed = tap._vellum_installed_version(ssh_client, "xovi") is not None
    shared_files = any(
        ssh_client.file_exists(path)
        for path in (
            tap.SHARED_XOVI_LIBRARY,
            tap.SHARED_QRR_LIBRARY,
            tap.SHARED_APPLOAD_LIBRARY,
        )
    )
    if not conflicts and not xovi_installed and not shared_files:
        if disabled_current_vellum or tap_vellum:
            raise RuntimeError(
                "检测到由 Vellum 管理的 rmtool 功能状态，但标准 Vellum/Xovi "
                "运行环境不完整，拒绝降级为独立部署。"
            )
        return "standalone"
    if not xovi_installed:
        raise RuntimeError("检测到非 Vellum 管理的 Xovi 文件或启动配置，拒绝自动合并。")
    expected_dropin = "\n".join(
        (
            "[Service]",
            f'Environment="LD_PRELOAD={tap.SHARED_XOVI_LIBRARY}"',
            'Environment="XOVI_ROOT=/home/root/xovi/services/xochitl.service/"',
        )
    )
    if conflicts and tap._remote_text(ssh_client, tap.SHARED_XOVI_DROPIN).strip() != expected_dropin:
        raise RuntimeError("AppLoader Xovi 启动配置不是 rmtool 支持的标准布局。")
    links = ssh_client.exec_checked(
        "readlink -f /home/root/xovi/services/xochitl.service/extensions.d; "
        "readlink -f /home/root/xovi/services/xochitl.service/exthome"
    ).splitlines()
    if links != [f"{tap.SHARED_XOVI_BASE}/extensions.d", f"{tap.SHARED_XOVI_BASE}/exthome"]:
        raise RuntimeError("AppLoader Xovi 服务目录链接不是 rmtool 支持的标准布局。")
    tap._assert_vellum_runtime(ssh_client, package)
    if ssh_client.file_exists(SHARED_QMD):
        installed = tap._vellum_installed_version(ssh_client, VELLUM_PACKAGE_NAME)
        if installed is None or not tap._vellum_package_owns_path(
            ssh_client, VELLUM_PACKAGE_NAME, SHARED_QMD
        ):
            raise RuntimeError("快速黑白 QMD 已存在但不归 Vellum 所有。")
    return "vellum"


def _qmd_check_command(stage: str, *, vellum: bool) -> str:
    check_root = f"{stage}/check"
    hashtab = tap.SHARED_HASHTAB if vellum else f"{stage}/exthome/qt-resource-rebuilder/hashtab"
    qmd = f"{stage}/fast-mono-reading.qmd" if vellum else f"{stage}/{QMD_PAYLOAD_PATH}"
    return (
        f"mkdir -p {check_root}/hashtabs {check_root}/qmd && "
        f"cp {hashtab} {check_root}/hashtabs/hashtab-device && "
        f"cp {qmd} {check_root}/qmd/fast-mono-reading.qmd && "
        f"{stage}/qmd-tool check -hashtabs {check_root}/hashtabs -qmd {check_root}/qmd"
    )


def _write_marker(ssh_client, marker: bytes, token: str) -> None:
    staged = f"/tmp/rmtool-fast-mono-marker-{token}.json"
    backup = f"{MARKER_PATH}.backup-{token}"
    digest = hashlib.sha256(marker).hexdigest()
    tap._upload_text(ssh_client, marker, staged, 0o644)
    command = f"""set -eu
MARKER={shlex.quote(MARKER_PATH)}
STAGED={shlex.quote(staged)}
BACKUP={shlex.quote(backup)}
HAD_MARKER=0
rollback() {{
    rc=$?
    trap - EXIT INT TERM
    rm -f "$MARKER.tmp"
    if [ "$rc" -ne 0 ] && [ "$HAD_MARKER" -eq 1 ] && [ -f "$BACKUP" ]; then
        mv -f "$BACKUP" "$MARKER"
    fi
    rm -f "$BACKUP" "$STAGED"
    exit "$rc"
}}
trap rollback EXIT INT TERM
mkdir -p "$(dirname "$MARKER")"
if [ -f "$MARKER" ]; then
    HAD_MARKER=1
    cp "$MARKER" "$BACKUP"
fi
cp "$STAGED" "$MARKER.tmp"
chmod 0644 "$MARKER.tmp"
chown root:root "$MARKER.tmp"
mv -f "$MARKER.tmp" "$MARKER"
printf '%s  %s\n' {shlex.quote(digest)} "$MARKER" | sha256sum -c -
rm -f "$BACKUP" "$STAGED"
trap - EXIT INT TERM
"""
    try:
        ssh_client.exec_checked(f"/bin/sh -c {shlex.quote(command)}")
    finally:
        try:
            ssh_client.exec_checked(f"rm -f {shlex.quote(staged)}")
        except Exception:
            logging.exception("Could not remove temporary fast-mono marker")


def _enable_vellum(
    ssh_client,
    package: FastMonoReadingPackage,
    archive_path: str | Path,
) -> FastMonoReadingStatus:
    token = uuid.uuid4().hex
    stage = f"/tmp/rmtool-fast-mono-vellum-{token}"
    remote_apk = f"{stage}/{VELLUM_PACKAGE_NAME}.apk"
    process_token = tap._xochitl_process_token(ssh_client)
    marker = _vellum_marker(package, enabled=True, process_token=process_token)
    expected_version = _vellum_package_version(package)
    installed_before = tap._vellum_installed_version(ssh_client, VELLUM_PACKAGE_NAME)
    marker_before = (
        tap._remote_text(ssh_client, MARKER_PATH).encode("utf-8")
        if ssh_client.file_exists(MARKER_PATH)
        else None
    )
    if installed_before is not None and installed_before != expected_version:
        raise RuntimeError(
            f"设备已有其他版本的 {VELLUM_PACKAGE_NAME}（{installed_before}），请先停用。"
        )
    if installed_before is not None:
        if not tap._vellum_package_owns_path(ssh_client, VELLUM_PACKAGE_NAME, SHARED_QMD):
            raise RuntimeError("Vellum 未确认现有快速黑白 QMD 的所有权。")
        if tap._remote_sha256(ssh_client, SHARED_QMD) != package.file(QMD_PAYLOAD_PATH).sha256:
            raise RuntimeError("现有快速黑白 QMD 与精确包不匹配。")

    with tempfile.TemporaryDirectory() as temporary_dir:
        extracted = extract_verified_package(archive_path, package, temporary_dir)
        qmd_local = extracted.joinpath(*PurePosixPath(QMD_PAYLOAD_PATH).parts)
        tool_local = extracted / "qmd-tool"
        license_local = extracted / "LICENSE.qmd-tool"
        apk_data = _build_vellum_apk(
            package, qmd_local.read_bytes(), license_local.read_bytes()
        )
        local_apk = Path(temporary_dir) / f"{VELLUM_PACKAGE_NAME}.apk"
        local_apk.write_bytes(apk_data)
        apk_sha = hashlib.sha256(apk_data).hexdigest()
        ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)}")
        try:
            ssh_client.exec_checked(f"mkdir -p {shlex.quote(stage)}")
            for local, remote, mode in (
                (qmd_local, f"{stage}/fast-mono-reading.qmd", 0o644),
                (tool_local, f"{stage}/qmd-tool", 0o755),
                (local_apk, remote_apk, 0o644),
            ):
                ssh_client.transfer_file(str(local), remote)
                ssh_client.exec_checked(f"chmod {mode:o} {shlex.quote(remote)}")
            ssh_client.exec_checked(f"chown -R root:root {shlex.quote(stage)}")
            for remote, expected in (
                (f"{stage}/fast-mono-reading.qmd", package.file(QMD_PAYLOAD_PATH).sha256),
                (f"{stage}/qmd-tool", package.file("qmd-tool").sha256),
                (remote_apk, apk_sha),
            ):
                if tap._remote_sha256(ssh_client, remote) != expected:
                    raise RuntimeError(f"设备端 Vellum 资源 {remote} 上传校验失败。")
            ssh_client.exec_checked(_qmd_check_command(stage, vellum=True))
            ssh_client.exec_checked(f"rm -rf {shlex.quote(stage + '/check')}")
            if installed_before is None:
                command = (
                    f"{shlex.quote(tap.VELLUM_BIN)} add --allow-untrusted "
                    f"{{simulate}}{shlex.quote(remote_apk)}"
                )
                ssh_client.exec_checked(command.format(simulate="--simulate "))
                ssh_client.exec_checked(command.format(simulate=""))
            if tap._vellum_installed_version(ssh_client, VELLUM_PACKAGE_NAME) != expected_version:
                raise RuntimeError("Vellum 快速黑白包安装后版本校验失败。")
            if not tap._vellum_package_owns_path(ssh_client, VELLUM_PACKAGE_NAME, SHARED_QMD):
                raise RuntimeError("Vellum 未登记快速黑白 QMD 的所有权。")
            if not _vellum_payload_paths_valid(ssh_client):
                raise RuntimeError("Vellum 快速黑白包拥有预期范围外的文件。")
            if tap._remote_sha256(ssh_client, SHARED_QMD) != package.file(QMD_PAYLOAD_PATH).sha256:
                raise RuntimeError("Vellum 快速黑白 QMD 安装后哈希不匹配。")
            _write_marker(ssh_client, marker, token)
        except Exception:
            package_absent = installed_before is None
            if installed_before is None:
                try:
                    if tap._vellum_installed_version(ssh_client, VELLUM_PACKAGE_NAME) is not None:
                        ssh_client.exec_checked(
                            f"{shlex.quote(tap.VELLUM_BIN)} del {shlex.quote(VELLUM_PACKAGE_NAME)}"
                        )
                    package_absent = tap._vellum_installed_version(
                        ssh_client, VELLUM_PACKAGE_NAME
                    ) is None
                except Exception:
                    package_absent = False
                    logging.exception("Could not roll back Vellum fast-mono package")
            if installed_before is not None or package_absent:
                try:
                    if marker_before is None:
                        ssh_client.exec_checked(f"rm -f {shlex.quote(MARKER_PATH)}")
                    else:
                        _write_marker(ssh_client, marker_before, f"rollback-{token}")
                except Exception:
                    logging.exception("Could not restore fast-mono marker")
            raise
        finally:
            try:
                ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)}")
            except Exception:
                logging.exception("Could not remove fast-mono Vellum staging files")
    return get_status(ssh_client, (package,))


def enable(
    ssh_client,
    package: FastMonoReadingPackage,
    archive_path: str | Path,
) -> FastMonoReadingStatus:
    identity = tap.get_device_identity(ssh_client)
    if select_package((package,), identity) is None:
        raise RuntimeError("当前设备与快速黑白包不精确匹配，未执行修改。")
    tap._preflight_device(ssh_client)
    deployment_mode = _deployment_mode(ssh_client, package)
    if deployment_mode == "vellum":
        return _enable_vellum(ssh_client, package, archive_path)

    trusted_package = select_package(_trusted_catalog(), identity)
    if trusted_package is None or trusted_package != package:
        raise RuntimeError("快速黑白包与内置信任清单不一致，拒绝部署。")
    runtime, trusted, legacies = _trusted_shared_context(identity)
    _runtime, feature = _shared_specs(trusted_package)
    with tempfile.TemporaryDirectory() as temporary_dir:
        extracted = extract_verified_package(archive_path, package, temporary_dir)
        _xovi_standalone.enable_shared(
            ssh_client, runtime, feature, extracted, trusted, legacies
        )
    return get_status(ssh_client, (package,))


def enable_cloud(
    ssh_client, package: FastMonoReadingPackage, state_dir: str
) -> FastMonoReadingStatus:
    return enable(ssh_client, package, download_package(package, state_dir))


def _disable_vellum(
    ssh_client, catalog: Iterable[FastMonoReadingPackage]
) -> FastMonoReadingStatus:
    packages = tuple(catalog)
    identity = tap.get_device_identity(ssh_client)
    marker = _read_marker(ssh_client)
    trusted_package = tap._package_from_marker(_trusted_catalog(), marker)
    if trusted_package is None:
        raise RuntimeError("内置快速黑白清单无法验证该 Vellum 安装。")
    revision, detail = _vellum_payload_revision(
        ssh_client, trusted_package, marker
    )
    if revision is None or marker.get("enabled") is not True:
        raise RuntimeError(
            detail or "Vellum 快速黑白安装无法精确验证，拒绝自动卸载。"
        )
    process_token = tap._xochitl_process_token(ssh_client)
    ssh_client.exec_checked(
        f"{shlex.quote(tap.VELLUM_BIN)} del {shlex.quote(VELLUM_PACKAGE_NAME)}"
    )
    if tap._vellum_installed_version(ssh_client, VELLUM_PACKAGE_NAME) is not None:
        raise RuntimeError("Vellum 快速黑白包删除后仍在包数据库中。")
    if ssh_client.file_exists(SHARED_QMD):
        raise RuntimeError("Vellum 快速黑白包删除后 QMD 仍然存在。")
    package = select_package(packages, identity)
    if package is not None:
        _write_marker(
            ssh_client,
            _vellum_marker(package, enabled=False, process_token=process_token),
            uuid.uuid4().hex,
        )
    else:
        ssh_client.exec_checked(f"rm -f {shlex.quote(MARKER_PATH)}")
    return get_status(ssh_client, packages)


def _clear_disabled_vellum_marker(
    ssh_client, catalog: Iterable[FastMonoReadingPackage]
) -> FastMonoReadingStatus:
    packages = tuple(catalog)
    identity = tap.get_device_identity(ssh_client)
    marker = _read_marker(ssh_client)
    package = tap._package_from_marker(_trusted_catalog(), marker)
    if package is None:
        raise RuntimeError("内置快速黑白清单无法验证该 Vellum 安装标记。")
    revision, detail = _vellum_payload_revision(ssh_client, package, marker)
    if revision is None or marker.get("enabled") is not False:
        raise RuntimeError(detail or "Vellum 快速黑白停用标记无法精确验证。")
    if not _marker_dir_has_only_marker(ssh_client):
        raise RuntimeError("快速黑白标记目录包含未知文件，拒绝自动清理。")
    ssh_client.exec_checked(
        f"rm -f {shlex.quote(MARKER_PATH)}; "
        f"rmdir {shlex.quote(REMOTE_BASE)} 2>/dev/null || true"
    )
    if ssh_client.file_exists(MARKER_PATH):
        raise RuntimeError("快速黑白停用标记删除后仍然存在。")
    return get_status(ssh_client, packages)


def disable(
    ssh_client, catalog: Iterable[FastMonoReadingPackage] = ()
) -> FastMonoReadingStatus:
    if ssh_client.file_exists(tap.VELLUM_BIN):
        if tap._vellum_installed_version(ssh_client, VELLUM_PACKAGE_NAME) is not None:
            return _disable_vellum(ssh_client, catalog)
        if ssh_client.file_exists(MARKER_PATH):
            marker = _read_marker(ssh_client)
            if marker.get("deployment_mode") == "vellum":
                return _clear_disabled_vellum_marker(ssh_client, catalog)
    if _xovi_standalone.has_shared_artifacts(ssh_client):
        current_identity = tap.get_device_identity(ssh_client)
        identity = tap.DeviceIdentity(
            *_xovi_standalone.read_shared_identity(ssh_client)
        )
        runtime, trusted, _legacies = _trusted_shared_context(identity)
        package = select_package(_trusted_catalog(), identity)
        if package is None:
            raise RuntimeError("内置快速黑白清单无法验证该共享安装。")
        _inspection, installed_trusted, outdated = _inspect_shared_revision(
            ssh_client,
            runtime,
            trusted,
            package,
            check_lower=True,
            firmware_residue_identity=(
                current_identity.firmware,
                current_identity.platform,
                current_identity.architecture,
                current_identity.xochitl_sha256,
            ) if identity != current_identity else None,
        )
        if identity != current_identity:
            _xovi_standalone.remove_shared_firmware_residue(
                ssh_client,
                runtime,
                installed_trusted,
                (
                    current_identity.firmware,
                    current_identity.platform,
                    current_identity.architecture,
                    current_identity.xochitl_sha256,
                ),
            )
        else:
            _xovi_standalone.disable_shared(
                ssh_client,
                runtime,
                "fast-mono-reading",
                installed_trusted,
                trusted["fast-mono-reading"] if outdated else None,
            )
        return get_status(ssh_client, catalog)
    marker_exists = ssh_client.file_exists(MARKER_PATH)
    if not marker_exists and (
        ssh_client.file_exists(DROPIN_PATH) or ssh_client.file_exists(REMOTE_BASE)
    ):
        raise RuntimeError("快速黑白资源缺少 rmtool 所有权标记，拒绝自动删除。")
    if marker_exists:
        marker = _read_marker(ssh_client)
        if marker.get("deployment_mode") == "vellum":
            raise RuntimeError("Vellum 快速黑白包未安装，拒绝直接删除共享 Xovi 文件。")
        if (
            marker.get("schema_version") != 1
            or marker.get("deployment_mode") != "standalone"
        ):
            raise RuntimeError("快速黑白安装标记的部署模式无效，拒绝自动停用。")
        marker_package = tap._package_from_marker(_trusted_catalog(), marker)
        if marker_package is None:
            raise RuntimeError("快速黑白安装标记不属于任何内置信任包。")
        current_package = select_package(
            _trusted_catalog(), tap.get_device_identity(ssh_client)
        )
        if current_package != marker_package:
            _xovi_standalone.remove_verified_legacy(
                ssh_client, _legacy_spec(marker_package)
            )
            return get_status(ssh_client, catalog)
    token = uuid.uuid4().hex
    remote_script = f"/tmp/rmtool-fast-mono-disable-{token}.sh"
    try:
        tap._upload_text(
            ssh_client,
            _xovi_standalone.disable_script(token, _STANDALONE_LAYOUT),
            remote_script,
            0o755,
        )
        ssh_client.exec_checked(f"/bin/sh {shlex.quote(remote_script)}")
    finally:
        try:
            ssh_client.exec_checked(f"rm -f {shlex.quote(remote_script)}")
        except Exception:
            logging.exception("Could not remove fast-mono disable script")
    return get_status(ssh_client, catalog)
