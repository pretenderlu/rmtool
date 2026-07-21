"""Persistent, firmware-gated tap-to-turn support for reMarkable devices."""

from __future__ import annotations

import hashlib
import gzip
import io
import json
import logging
import os
import posixpath
import re
import shlex
import tarfile
import tempfile
import urllib.request
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional


REPO_URL = "https://github.com/pretenderlu/rmtool"
ASSET_RELEASE_URL = f"{REPO_URL}/releases/download/tap-page-turn-assets"
MANIFEST_URL = f"{ASSET_RELEASE_URL}/manifest.json"

REMOTE_BASE = "/home/root/.local/share/rmtool/tap-page-turn"
DROPIN_NAME = "90-rmtool-tap-page-turn.conf"
DROPIN_PATH = f"/etc/systemd/system/xochitl.service.d/{DROPIN_NAME}"
MARKER_PATH = f"{REMOTE_BASE}/package.json"
LAUNCHER_PATH = f"{REMOTE_BASE}/launcher.sh"

SHARED_XOVI_BASE = "/home/root/xovi"
SHARED_XOVI_DROPIN = "/etc/systemd/system/xochitl.service.d/00-xovi.conf"
SHARED_XOVI_LIBRARY = f"{SHARED_XOVI_BASE}/xovi.so"
SHARED_QRR_LIBRARY = f"{SHARED_XOVI_BASE}/extensions.d/qt-resource-rebuilder.so"
SHARED_QRR_HOME = f"{SHARED_XOVI_BASE}/exthome/qt-resource-rebuilder"
SHARED_HASHTAB = f"{SHARED_QRR_HOME}/hashtab"
SHARED_QMD = f"{SHARED_QRR_HOME}/rmtool-tap-page-turn.qmd"
SHARED_APPLOAD_LIBRARY = f"{SHARED_XOVI_BASE}/extensions.d/appload.so"

VELLUM_PACKAGE_NAME = "rmtool-tap-page-turn"
VELLUM_ROOT = "/home/root/.vellum"
VELLUM_BIN = f"{VELLUM_ROOT}/bin/vellum"
VELLUM_LICENSE_DIR = f"{VELLUM_ROOT}/licenses/{VELLUM_PACKAGE_NAME}"
VELLUM_LICENSE_PATH = f"{VELLUM_LICENSE_DIR}/LICENSE"
VELLUM_SOURCES_PATH = f"{VELLUM_LICENSE_DIR}/SOURCES"
VELLUM_CONFLICTS = ("gesture-tap-to-page", "tap-to-change-view-or-page")
VELLUM_RUNTIME_PACKAGES = ("xovi", "qt-resource-rebuilder", "appload")
_VELLUM_DEVICE_PACKAGE = {
    "ferrari": "rmpp",
    "chiappa": "rmppmove",
    "tatsu": "rmppure",
    "rm1": "rm1",
    "rm2": "rm2",
}

MAX_MANIFEST_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 40 * 1024 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FIRMWARE_RE = re.compile(r"[0-9]{14}")
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2,3}")
_PLATFORM_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_ARCH_RE = re.compile(r"[a-z0-9_][a-z0-9_-]{0,31}")
_ASSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz")
_PROCESS_TOKEN_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9]+:[0-9]+"
)
_APK_PACKAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]*")
_APK_VERSION_RE = re.compile(r"[0-9][A-Za-z0-9._+~-]*-r[0-9]+")

_RUNTIME_PATHS = {
    "xovi.so",
    "extensions.d/qt-resource-rebuilder.so",
    "exthome/qt-resource-rebuilder/tap-page-turn.qmd",
    "exthome/qt-resource-rebuilder/hashtab",
}
_REQUIRED_PATHS = _RUNTIME_PATHS | {"qmd-tool"}


class TapPageTurnState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    INSTALLED_DISABLED = "installed_disabled"
    ENABLE_PENDING_REBOOT = "enable_pending_reboot"
    WAITING_FOR_XOVI = "waiting_for_xovi"
    ENABLED = "enabled"
    DISABLE_PENDING_REBOOT = "disable_pending_reboot"
    BROKEN = "broken"


@dataclass(frozen=True)
class PayloadFile:
    path: str
    sha256: str
    size: int
    mode: int


@dataclass(frozen=True)
class TapPageTurnPackage:
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

    @property
    def package_id(self) -> str:
        return f"{self.platform}-{self.firmware}-{self.xochitl_sha256[:12]}"

    @property
    def download_url(self) -> str:
        return f"{ASSET_RELEASE_URL}/{self.asset}"

    def file(self, path: str) -> PayloadFile:
        for item in self.files:
            if item.path == path:
                return item
        raise KeyError(path)


@dataclass(frozen=True)
class DeviceIdentity:
    firmware: str
    platform: str
    architecture: str
    xochitl_sha256: str


@dataclass(frozen=True)
class TapPageTurnStatus:
    state: TapPageTurnState
    identity: DeviceIdentity
    package: Optional[TapPageTurnPackage] = None
    available_packages: tuple[TapPageTurnPackage, ...] = ()
    detail: str = ""
    dropin_present: bool = False


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RuntimeError("点击翻页资源路径无效。")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise RuntimeError("点击翻页资源路径不安全。")
    return value


def _required_string(entry: dict, key: str, pattern: re.Pattern[str]) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RuntimeError(f"点击翻页清单字段 {key} 无效。")
    return value


def _parse_payload_file(entry: object) -> PayloadFile:
    if not isinstance(entry, dict):
        raise RuntimeError("点击翻页资源文件格式无效。")
    path = _safe_relative_path(entry.get("path"))
    digest = _required_string(entry, "sha256", _SHA256_RE)
    size = entry.get("size")
    mode = entry.get("mode")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise RuntimeError(f"点击翻页资源 {path} 的大小无效。")
    if mode not in (0o644, 0o755):
        raise RuntimeError(f"点击翻页资源 {path} 的权限无效。")
    return PayloadFile(path, digest, size, mode)


def parse_manifest(data: bytes) -> tuple[TapPageTurnPackage, ...]:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("点击翻页云端清单不是有效 JSON。") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise RuntimeError("点击翻页云端清单版本不受支持。")
    entries = document.get("packages")
    if not isinstance(entries, list):
        raise RuntimeError("点击翻页云端清单缺少 packages。")

    packages: list[TapPageTurnPackage] = []
    identities: set[tuple[str, str, str]] = set()
    assets: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("点击翻页清单包格式无效。")
        firmware = _required_string(entry, "firmware", _FIRMWARE_RE)
        release_version = _required_string(entry, "release_version", _VERSION_RE)
        platform = _required_string(entry, "platform", _PLATFORM_RE)
        architecture = _required_string(entry, "architecture", _ARCH_RE)
        xochitl_sha = _required_string(entry, "xochitl_sha256", _SHA256_RE)
        asset = _required_string(entry, "asset", _ASSET_RE)
        digest = _required_string(entry, "sha256", _SHA256_RE)
        channel = entry.get("channel")
        size = entry.get("size")
        if channel not in ("stable", "beta"):
            raise RuntimeError("点击翻页清单发布类型无效。")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or size > MAX_PACKAGE_BYTES
        ):
            raise RuntimeError("点击翻页资源包大小无效。")
        file_entries = entry.get("files")
        if not isinstance(file_entries, list) or not file_entries:
            raise RuntimeError("点击翻页资源包缺少文件清单。")
        files = tuple(_parse_payload_file(item) for item in file_entries)
        paths = [item.path for item in files]
        if len(paths) != len(set(paths)):
            raise RuntimeError("点击翻页资源包包含重复路径。")
        if not _REQUIRED_PATHS.issubset(paths):
            raise RuntimeError("点击翻页资源包缺少必要文件。")
        if sum(item.size for item in files) > MAX_UNPACKED_BYTES:
            raise RuntimeError("点击翻页资源包解压后过大。")

        identity = (platform, firmware, xochitl_sha)
        if identity in identities or asset in assets:
            raise RuntimeError("点击翻页清单包含重复包。")
        identities.add(identity)
        assets.add(asset)
        packages.append(
            TapPageTurnPackage(
                firmware=firmware,
                release_version=release_version,
                channel=channel,
                platform=platform,
                architecture=architecture,
                xochitl_sha256=xochitl_sha,
                asset=asset,
                sha256=digest,
                size=size,
                files=files,
            )
        )
    return tuple(packages)


def _download_limited(url: str, maximum: int) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "rmtool-tap-page-turn/1"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        declared = response.headers.get("Content-Length")
        if declared and int(declared) > maximum:
            raise RuntimeError("云端文件超过允许大小。")
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise RuntimeError("云端文件超过允许大小。")
    return data


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _cache_dir(state_dir: str) -> Path:
    return Path(state_dir) / "cache" / "tap-page-turn"


def load_catalog(
    state_dir: str, *, refresh: bool = True
) -> tuple[TapPageTurnPackage, ...]:
    manifest_path = _cache_dir(state_dir) / "manifest.json"
    if refresh:
        try:
            data = _download_limited(MANIFEST_URL, MAX_MANIFEST_BYTES)
            catalog = parse_manifest(data)
            _write_atomic(manifest_path, data)
            return catalog
        except Exception as exc:
            logging.warning("Could not refresh tap-to-turn manifest: %s", exc)
    if manifest_path.is_file():
        try:
            return parse_manifest(manifest_path.read_bytes())
        except Exception as exc:
            logging.warning("Cached tap-to-turn manifest is invalid: %s", exc)
    raise RuntimeError("无法获取点击翻页云端清单，且没有可用缓存。")


def download_package(
    package: TapPageTurnPackage, state_dir: str
) -> Path:
    destination = _cache_dir(state_dir) / package.firmware / package.asset
    if destination.is_file():
        data = destination.read_bytes()
        if (
            len(data) == package.size
            and hashlib.sha256(data).hexdigest() == package.sha256
        ):
            return destination
    data = _download_limited(package.download_url, MAX_PACKAGE_BYTES)
    if len(data) != package.size:
        raise RuntimeError("点击翻页资源包大小与云端清单不匹配。")
    if hashlib.sha256(data).hexdigest() != package.sha256:
        raise RuntimeError("点击翻页资源包 SHA-256 校验失败。")
    _write_atomic(destination, data)
    return destination


def extract_verified_package(
    archive_path: str | Path,
    package: TapPageTurnPackage,
    destination: str | Path,
) -> Path:
    archive = Path(archive_path)
    output = Path(destination)
    output.mkdir(parents=True, exist_ok=True)
    expected = {item.path: item for item in package.files}
    seen: set[str] = set()
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            for member in bundle.getmembers():
                name = _safe_relative_path(member.name)
                if name not in expected or name in seen or not member.isfile():
                    raise RuntimeError("点击翻页资源包包含未授权文件。")
                spec = expected[name]
                if member.size != spec.size:
                    raise RuntimeError(f"点击翻页资源 {name} 大小不匹配。")
                source = bundle.extractfile(member)
                if source is None:
                    raise RuntimeError(f"无法读取点击翻页资源 {name}。")
                target = output.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                with source, target.open("wb") as destination_file:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        destination_file.write(chunk)
                if digest.hexdigest() != spec.sha256:
                    raise RuntimeError(f"点击翻页资源 {name} SHA-256 校验失败。")
                os.chmod(target, spec.mode)
                seen.add(name)
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError("无法解压点击翻页资源包。") from exc
    if seen != set(expected):
        raise RuntimeError("点击翻页资源包缺少清单文件。")
    return output


def _gzip_member(data: bytes) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=output,
        mtime=0,
    ) as compressed:
        compressed.write(data)
    return output.getvalue()


def _pax_record(key: str, value: str) -> bytes:
    body = f" {key}={value}\n".encode("utf-8")
    length = len(body) + 1
    while True:
        record = str(length).encode("ascii") + body
        if len(record) == length:
            return record
        length = len(record)


def _ustar_header(path: str, *, mode: int, size: int, entry_type: bytes) -> bytes:
    encoded_path = path.encode("utf-8")
    if len(encoded_path) > 100:
        raise RuntimeError(f"Vellum APK 路径过长：{path}")
    header = bytearray(512)

    def field(offset: int, length: int, value: bytes) -> None:
        if len(value) > length:
            raise RuntimeError("Vellum APK tar 字段过长。")
        header[offset : offset + len(value)] = value

    def octal(offset: int, length: int, value: int) -> None:
        field(offset, length, f"{value:0{length - 1}o}\0".encode("ascii"))

    field(0, 100, encoded_path)
    octal(100, 8, mode)
    octal(108, 8, 0)
    octal(116, 8, 0)
    octal(124, 12, size)
    octal(136, 12, 0)
    header[148:156] = b"        "
    field(156, 1, entry_type)
    field(257, 6, b"ustar\0")
    field(263, 2, b"00")
    checksum = sum(header)
    header[148:156] = f"{checksum:06o}\0 ".encode("ascii")
    return bytes(header)


def _tar_padding(size: int) -> bytes:
    return b"\0" * ((-size) % 512)


def _tar_member(
    files: dict[str, tuple[bytes, int]],
    *,
    apk_checksums: bool = True,
    end_archive: bool = True,
) -> bytes:
    output = io.BytesIO()
    directories: set[str] = set()
    for path in files:
        parent = PurePosixPath(path).parent
        while str(parent) not in ("", "."):
            directories.add(str(parent))
            parent = parent.parent
    entries = [
        (path, b"", 0o755, b"5")
        for path in sorted(directories, key=lambda item: (item.count("/"), item))
    ]
    entries.extend(
        (path, data, mode, b"0")
        for path, (data, mode) in sorted(files.items())
    )
    for path, data, mode, entry_type in entries:
        pax = _pax_record("ctime", "0") + _pax_record("atime", "0")
        if apk_checksums and entry_type == b"0":
            pax += _pax_record(
                "APK-TOOLS.checksum.SHA1",
                hashlib.sha1(data).hexdigest(),
            )
        parent = str(PurePosixPath(path).parent)
        pax_prefix = "./" if parent == "." else f"{parent}/"
        pax_name = f"{pax_prefix}PaxHeaders/{PurePosixPath(path).name}"
        output.write(
            _ustar_header(pax_name, mode=0o644, size=len(pax), entry_type=b"x")
        )
        output.write(pax)
        output.write(_tar_padding(len(pax)))
        entry_path = f"{path}/" if entry_type == b"5" else path
        output.write(
            _ustar_header(
                entry_path,
                mode=mode,
                size=len(data),
                entry_type=entry_type,
            )
        )
        output.write(data)
        output.write(_tar_padding(len(data)))
    if end_archive:
        output.write(b"\0" * 1024)
        output.write(b"\0" * ((-output.tell()) % (20 * 512)))
    return output.getvalue()


def _vellum_package_version(package: TapPageTurnPackage) -> str:
    return f"{package.release_version}-r0"


def _build_vellum_apk(
    package: TapPageTurnPackage,
    qmd: bytes,
    license_text: bytes,
) -> bytes:
    device_package = _VELLUM_DEVICE_PACKAGE.get(package.platform)
    if not device_package:
        raise RuntimeError("点击翻页包没有对应的 Vellum 设备依赖。")
    qmd_sha = package.file(
        "exthome/qt-resource-rebuilder/tap-page-turn.qmd"
    ).sha256
    if hashlib.sha256(qmd).hexdigest() != qmd_sha:
        raise RuntimeError("Vellum APK 的 QMD 与点击翻页清单不匹配。")

    sources = (
        f"{REPO_URL}\n"
        "https://github.com/asivery/xovi\n"
        "https://github.com/asivery/rm-xovi-extensions\n"
    ).encode("ascii")
    data_files = {
        SHARED_QMD.removeprefix("/"): (qmd, 0o644),
        f"{VELLUM_LICENSE_DIR.removeprefix('/')}/LICENSE": (
            license_text,
            0o644,
        ),
        f"{VELLUM_LICENSE_DIR.removeprefix('/')}/SOURCES": (sources, 0o644),
    }
    data_member = _gzip_member(_tar_member(data_files))
    data_hash = hashlib.sha256(data_member).hexdigest()
    dependencies = (
        "qt-resource-rebuilder>=19.0.0",
        "qt-resource-rebuilder<20.0.0",
        "appload>=0.5.3",
        f"remarkable-os={package.release_version}-r0",
        f"{device_package}=1.0.0-r0",
        *(f"!{name}" for name in VELLUM_CONFLICTS),
    )
    pkginfo_lines = (
        "# Generated by rmtool",
        f"pkgname = {VELLUM_PACKAGE_NAME}",
        f"pkgver = {_vellum_package_version(package)}",
        "pkgdesc = Persistent tap regions for PDF and EPUB page navigation",
        f"url = {REPO_URL}",
        "builddate = 0",
        "packager = rmtool",
        f"size = {sum(len(data) for data, _mode in data_files.values())}",
        "arch = noarch",
        f"origin = {VELLUM_PACKAGE_NAME}",
        "license = GPL-3.0-only",
        *(f"depend = {dependency}" for dependency in dependencies),
        f"datahash = {data_hash}",
        "",
    )
    control_files = {
        ".PKGINFO": ("\n".join(pkginfo_lines).encode("utf-8"), 0o644),
    }
    control_member = _gzip_member(
        _tar_member(control_files, apk_checksums=False, end_archive=False)
    )
    return control_member + data_member


def _platform_from_machine(machine: str) -> str:
    normalized = machine.casefold()
    for platform in ("ferrari", "chiappa", "tatsu"):
        if platform in normalized:
            return platform
    if "remarkable 1" in normalized:
        return "rm1"
    if "remarkable 2" in normalized:
        return "rm2"
    return ""


def get_device_identity(ssh_client) -> DeviceIdentity:
    firmware = ssh_client.exec_checked("tr -cd '0-9' < /etc/version").strip()
    architecture = ssh_client.exec_checked("uname -m").strip()
    machine = ssh_client.exec_checked(
        "cat /sys/devices/soc0/machine 2>/dev/null || "
        "tr -d '\\0' < /proc/device-tree/model 2>/dev/null || true"
    ).strip()
    digest_output = ssh_client.exec_checked("sha256sum /usr/bin/xochitl").strip()
    digest = digest_output.split()[0] if digest_output else ""
    return DeviceIdentity(
        firmware=firmware,
        platform=_platform_from_machine(machine),
        architecture=architecture,
        xochitl_sha256=digest,
    )


def select_package(
    catalog: Iterable[TapPageTurnPackage], identity: DeviceIdentity
) -> Optional[TapPageTurnPackage]:
    for package in catalog:
        if (
            package.firmware == identity.firmware
            and package.platform == identity.platform
            and package.architecture == identity.architecture
            and package.xochitl_sha256 == identity.xochitl_sha256
        ):
            return package
    return None


def _remote_text(ssh_client, path: str) -> str:
    with ssh_client.open_remote(path, "r") as remote:
        data = remote.read()
    return data.decode("utf-8") if isinstance(data, bytes) else data


def _remote_sha256(ssh_client, path: str) -> str:
    output = ssh_client.exec_checked(f"sha256sum {shlex.quote(path)}").strip()
    digest = output.split()[0] if output else ""
    if not _SHA256_RE.fullmatch(digest):
        raise RuntimeError(f"设备未返回 {path} 的有效 SHA-256。")
    return digest


def _launcher(package: TapPageTurnPackage) -> str:
    checks = []
    for item in package.files:
        if item.path in _RUNTIME_PATHS:
            remote = posixpath.join(REMOTE_BASE, item.path)
            checks.append(
                f'[ "$(file_sha {shlex.quote(remote)})" = "{item.sha256}" ] || stock'
            )
    checks_text = "\n".join(checks)
    return f"""#!/bin/sh
BASE={shlex.quote(REMOTE_BASE)}

stock() {{
    logger -t rmtool-tap-page-turn "preflight failed; starting stock xochitl" 2>/dev/null || true
    unset LD_PRELOAD XOVI_ROOT QML_DISABLE_DISK_CACHE QML_XHR_ALLOW_FILE_WRITE QML_XHR_ALLOW_FILE_READ
    exec /usr/bin/xochitl --system
}}

file_sha() {{
    [ -f "$1" ] || return 1
    sha256sum "$1" | awk '{{print $1}}'
}}

[ "$(uname -m)" = "{package.architecture}" ] || stock
machine=$(cat /sys/devices/soc0/machine 2>/dev/null || true)
case "$machine" in
    *Ferrari*) platform=ferrari ;;
    *Chiappa*) platform=chiappa ;;
    *Tatsu*) platform=tatsu ;;
    *"reMarkable 1"*) platform=rm1 ;;
    *"reMarkable 2"*) platform=rm2 ;;
    *) platform=unknown ;;
esac
[ "$platform" = "{package.platform}" ] || stock
version=$(tr -cd '0-9' < /etc/version)
[ "$version" = "{package.firmware}" ] || stock
[ "$(file_sha /usr/bin/xochitl)" = "{package.xochitl_sha256}" ] || stock
{checks_text}

export XOVI_ROOT="$BASE"
export QML_DISABLE_DISK_CACHE=1
export QML_XHR_ALLOW_FILE_WRITE=1
export QML_XHR_ALLOW_FILE_READ=1
export LD_PRELOAD="$BASE/xovi.so"
exec /usr/bin/xochitl --system
"""


def _dropin(package: TapPageTurnPackage) -> str:
    del package
    conditions = [LAUNCHER_PATH]
    conditions.extend(
        posixpath.join(REMOTE_BASE, path) for path in sorted(_RUNTIME_PATHS)
    )
    condition_lines = "\n".join(
        f"ConditionPathExists={path}" for path in conditions
    )
    return f"""[Unit]
After=home.mount
{condition_lines}

[Service]
ExecStart=
ExecStart={LAUNCHER_PATH}
WatchdogSec=0
"""


def _marker(package: TapPageTurnPackage, launcher_sha: str, dropin_sha: str) -> bytes:
    document = {
        "schema_version": 1,
        "package_id": package.package_id,
        "firmware": package.firmware,
        "platform": package.platform,
        "xochitl_sha256": package.xochitl_sha256,
        "launcher_sha256": launcher_sha,
        "dropin_sha256": dropin_sha,
    }
    return (json.dumps(document, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")


def _vellum_marker(
    package: TapPageTurnPackage,
    *,
    enabled: bool,
    process_token: str,
) -> bytes:
    document = {
        "schema_version": 3,
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
        "qmd_sha256": package.file(
            "exthome/qt-resource-rebuilder/tap-page-turn.qmd"
        ).sha256,
    }
    return (json.dumps(document, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")


def _read_marker(ssh_client) -> dict:
    marker = json.loads(_remote_text(ssh_client, MARKER_PATH))
    if not isinstance(marker, dict):
        raise RuntimeError("设备点击翻页安装标记格式无效。")
    return marker


def _xochitl_process_token(ssh_client) -> str:
    command = (
        "pid=$(systemctl show xochitl -p MainPID --value 2>/dev/null || true); "
        '[ -n "$pid" ] && [ "$pid" != 0 ] || exit 1; '
        "boot=$(cat /proc/sys/kernel/random/boot_id); "
        "start=$(awk '{print $22}' /proc/$pid/stat 2>/dev/null); "
        'printf "%s:%s:%s\\n" "$boot" "$pid" "$start"'
    )
    token = ssh_client.exec_checked(command).strip()
    if not _PROCESS_TOKEN_RE.fullmatch(token):
        raise RuntimeError("设备未返回有效的 xochitl 进程身份。")
    return token


def _active_with_rmtool_payload(ssh_client) -> bool:
    command = (
        "pid=$(systemctl show xochitl -p MainPID --value 2>/dev/null || true); "
        "[ -n \"$pid\" ] && [ \"$pid\" != 0 ] && "
        f"grep -Fq '{REMOTE_BASE}/xovi.so' /proc/$pid/maps 2>/dev/null"
    )
    _stdout, _stderr, code = ssh_client.exec_command(command)
    return code == 0


def _active_with_shared_xovi(ssh_client) -> bool:
    command = (
        "pid=$(systemctl show xochitl -p MainPID --value 2>/dev/null || true); "
        '[ -n "$pid" ] && [ "$pid" != 0 ] && '
        f"grep -Fq '{SHARED_XOVI_LIBRARY}' /proc/$pid/maps 2>/dev/null && "
        f"grep -Fq '{SHARED_QRR_LIBRARY}' /proc/$pid/maps 2>/dev/null"
    )
    _stdout, _stderr, code = ssh_client.exec_command(command)
    return code == 0


def _vellum_installed_version(ssh_client, package_name: str) -> Optional[str]:
    if package_name not in _vellum_installed_packages(ssh_client):
        return None
    output = ssh_client.exec_checked(
        f"{shlex.quote(VELLUM_BIN)} list -I {shlex.quote(package_name)}"
    )
    prefix = f"{package_name}-"
    versions = []
    for line in output.splitlines():
        first_field = line.split(maxsplit=1)[0] if line.split() else ""
        if not first_field.startswith(prefix):
            continue
        version = first_field[len(prefix) :]
        if _APK_VERSION_RE.fullmatch(version):
            versions.append(version)
    if len(versions) > 1:
        raise RuntimeError(f"Vellum 返回了多个 {package_name} 已安装版本。")
    return versions[0] if versions else None


def _vellum_package_paths(ssh_client, package_name: str) -> set[str]:
    output = ssh_client.exec_checked(
        f"{shlex.quote(VELLUM_BIN)} info -L {shlex.quote(package_name)}"
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    header_prefix = f"{package_name}-"
    header_indexes = []
    for index, line in enumerate(lines):
        if not (line.endswith(" contains:") and line.startswith(header_prefix)):
            continue
        version = line[len(header_prefix) : -len(" contains:")]
        if _APK_VERSION_RE.fullmatch(version):
            header_indexes.append(index)
    if len(header_indexes) != 1:
        raise RuntimeError(f"无法确认 Vellum 包 {package_name} 的文件清单。")
    if any(line.endswith(" contains:") for line in lines if line != lines[header_indexes[0]]):
        raise RuntimeError(f"Vellum 包 {package_name} 的文件清单包含其他包。")
    return {
        line.lstrip("/").rstrip("/")
        for line in lines[header_indexes[0] + 1 :]
        if not line.endswith(" contains:")
    }


def _vellum_package_owns_path(
    ssh_client,
    package_name: str,
    path: str,
) -> bool:
    expected = path.lstrip("/").rstrip("/")
    return expected in _vellum_package_paths(ssh_client, package_name)


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
    paths = _vellum_package_paths(ssh_client, VELLUM_PACKAGE_NAME)
    return expected_files <= paths and paths <= allowed


def _vellum_installed_packages(ssh_client) -> set[str]:
    output = ssh_client.exec_checked(f"{shlex.quote(VELLUM_BIN)} info -q")
    packages = {line.strip() for line in output.splitlines() if line.strip()}
    invalid = sorted(name for name in packages if not _APK_PACKAGE_RE.fullmatch(name))
    if invalid:
        raise RuntimeError("Vellum 返回了无效的已安装包列表。")
    return packages


def _assert_vellum_runtime(
    ssh_client,
    package: TapPageTurnPackage,
) -> None:
    if not ssh_client.file_exists(VELLUM_BIN):
        raise RuntimeError("检测到 Xovi，但未找到 Vellum 管理器。")
    missing = [
        name
        for name in VELLUM_RUNTIME_PACKAGES
        if _vellum_installed_version(ssh_client, name) is None
    ]
    if missing:
        raise RuntimeError(
            "Vellum 缺少点击翻页所需包：" + ", ".join(missing)
        )
    owned_paths = (
        ("xovi", SHARED_XOVI_LIBRARY),
        ("qt-resource-rebuilder", SHARED_QRR_LIBRARY),
        ("appload", SHARED_APPLOAD_LIBRARY),
    )
    for owner, path in owned_paths:
        if not _vellum_package_owns_path(ssh_client, owner, path):
            raise RuntimeError(f"Vellum 数据库未确认 {path} 由 {owner} 所有。")
    if _remote_sha256(ssh_client, SHARED_XOVI_LIBRARY) != package.file(
        "xovi.so"
    ).sha256:
        raise RuntimeError("Vellum Xovi 版本与点击翻页包不匹配。")
    if _remote_sha256(ssh_client, SHARED_QRR_LIBRARY) != package.file(
        "extensions.d/qt-resource-rebuilder.so"
    ).sha256:
        raise RuntimeError("Vellum QML 资源扩展版本与点击翻页包不匹配。")
    if not ssh_client.file_exists(SHARED_HASHTAB):
        raise RuntimeError("Vellum QML hashtab 不存在。")


def _vellum_payload_valid(
    ssh_client,
    package: TapPageTurnPackage,
    marker: dict,
) -> tuple[bool, str]:
    try:
        expected = json.loads(
            _vellum_marker(
                package,
                enabled=bool(marker.get("enabled")),
                process_token=str(marker.get("process_token", "")),
            )
        )
        for key in (
            "schema_version",
            "deployment_mode",
            "package_id",
            "firmware",
            "platform",
            "xochitl_sha256",
            "vellum_package",
            "vellum_version",
            "qmd_path",
            "qmd_sha256",
        ):
            if marker.get(key) != expected[key]:
                return False, f"Vellum 安装标记字段 {key} 不匹配"
        if not isinstance(marker.get("enabled"), bool):
            return False, "Vellum 安装标记的启用状态无效"
        if not _PROCESS_TOKEN_RE.fullmatch(str(marker.get("process_token", ""))):
            return False, "Vellum 安装标记的进程身份无效"
        installed_version = _vellum_installed_version(
            ssh_client, VELLUM_PACKAGE_NAME
        )
        if marker["enabled"]:
            if installed_version != expected["vellum_version"]:
                return False, "Vellum 点击翻页包未安装或版本不匹配"
            _assert_vellum_runtime(ssh_client, package)
            if not _vellum_package_owns_path(
                ssh_client, VELLUM_PACKAGE_NAME, SHARED_QMD
            ):
                return False, "Vellum 数据库未确认点击翻页 QMD 所有权"
            if not _vellum_payload_paths_valid(ssh_client):
                return False, "Vellum 点击翻页包拥有预期范围外的文件"
            if _remote_sha256(ssh_client, SHARED_QMD) != expected["qmd_sha256"]:
                return False, "rmtool 点击翻页 QMD 已变化"
        else:
            if installed_version is not None:
                return False, "停用状态下 Vellum 点击翻页包仍已安装"
            if ssh_client.file_exists(SHARED_QMD):
                return False, "停用状态下仍存在 rmtool 点击翻页 QMD"
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _payload_valid(ssh_client, package: TapPageTurnPackage) -> tuple[bool, str]:
    try:
        marker = _read_marker(ssh_client)
        if marker.get("package_id") != package.package_id:
            return False, "设备安装标记与当前包不匹配"
        expected_launcher = hashlib.sha256(
            _launcher(package).encode("utf-8")
        ).hexdigest()
        expected_dropin = hashlib.sha256(_dropin(package).encode("utf-8")).hexdigest()
        if marker.get("launcher_sha256") != expected_launcher:
            return False, "启动包装器标记不匹配"
        if marker.get("dropin_sha256") != expected_dropin:
            return False, "systemd 配置标记不匹配"
        if _remote_sha256(ssh_client, DROPIN_PATH) != expected_dropin:
            return False, "systemd 配置已被修改"
        if _remote_sha256(ssh_client, LAUNCHER_PATH) != expected_launcher:
            return False, "启动包装器已被修改"
        for item in package.files:
            if item.path in _RUNTIME_PATHS:
                remote = posixpath.join(REMOTE_BASE, item.path)
                if _remote_sha256(ssh_client, remote) != item.sha256:
                    return False, f"运行资源 {item.path} 已被修改"
        return True, ""
    except Exception as exc:
        return False, str(exc)


def get_status(
    ssh_client,
    catalog: Iterable[TapPageTurnPackage],
) -> TapPageTurnStatus:
    packages = tuple(catalog)
    identity = get_device_identity(ssh_client)
    available = tuple(item for item in packages if item.platform == identity.platform)
    dropin_exists = ssh_client.file_exists(DROPIN_PATH)
    marker_exists = ssh_client.file_exists(MARKER_PATH)
    vellum_version = None
    vellum_error = ""
    if ssh_client.file_exists(VELLUM_BIN):
        try:
            vellum_version = _vellum_installed_version(
                ssh_client, VELLUM_PACKAGE_NAME
            )
        except Exception as exc:
            vellum_error = str(exc)
    recovery_available = dropin_exists or marker_exists or vellum_version is not None
    package = select_package(packages, identity)
    if package is None:
        return TapPageTurnStatus(
            TapPageTurnState.INCOMPATIBLE,
            identity,
            available_packages=available,
            detail="没有与设备身份和 xochitl 哈希精确匹配的包",
            dropin_present=recovery_available,
        )

    marker = None
    if marker_exists:
        try:
            marker = _read_marker(ssh_client)
        except Exception as exc:
            return TapPageTurnStatus(
                TapPageTurnState.BROKEN,
                identity,
                package,
                available,
                str(exc),
                recovery_available,
            )
    if marker and marker.get("deployment_mode") == "vellum":
        valid, detail = _vellum_payload_valid(ssh_client, package, marker)
        if not valid:
            state = TapPageTurnState.BROKEN
        else:
            try:
                current_process = _xochitl_process_token(ssh_client)
            except Exception as exc:
                state = TapPageTurnState.BROKEN
                detail = str(exc)
            else:
                process_changed = current_process != marker["process_token"]
                if marker["enabled"]:
                    if not _active_with_shared_xovi(ssh_client):
                        state = TapPageTurnState.WAITING_FOR_XOVI
                        detail = "请按 AppLoader 的正常流程手动激活 Xovi"
                    elif process_changed:
                        state = TapPageTurnState.ENABLED
                    else:
                        state = TapPageTurnState.ENABLE_PENDING_REBOOT
                elif process_changed:
                    state = TapPageTurnState.INSTALLED_DISABLED
                else:
                    state = TapPageTurnState.DISABLE_PENDING_REBOOT
        return TapPageTurnStatus(
            state,
            identity,
            package,
            available,
            detail,
            recovery_available,
        )

    if marker and marker.get("deployment_mode") == "shared_xovi":
        if vellum_error:
            return TapPageTurnStatus(
                TapPageTurnState.BROKEN,
                identity,
                package,
                available,
                vellum_error,
                True,
            )
        if vellum_version is not None:
            return TapPageTurnStatus(
                TapPageTurnState.BROKEN,
                identity,
                package,
                available,
                "Vellum 点击翻页包已存在，但设备仍保留旧版共享部署标记",
                True,
            )
        return TapPageTurnStatus(
            TapPageTurnState.INSTALLED_DISABLED,
            identity,
            package,
            available,
            "检测到旧版共享 Xovi 部署；点击启用可迁移为 Vellum 包",
            False,
        )

    if vellum_error and marker:
        return TapPageTurnStatus(
            TapPageTurnState.BROKEN,
            identity,
            package,
            available,
            vellum_error,
            True,
        )
    if vellum_version is not None:
        return TapPageTurnStatus(
            TapPageTurnState.BROKEN,
            identity,
            package,
            available,
            "Vellum 点击翻页包存在，但 rmtool 状态标记缺失或无效",
            True,
        )

    base_exists = ssh_client.file_exists(REMOTE_BASE)
    active = _active_with_rmtool_payload(ssh_client)
    if dropin_exists:
        valid, detail = _payload_valid(ssh_client, package)
        if not valid:
            state = TapPageTurnState.BROKEN
        elif active:
            state = TapPageTurnState.ENABLED
        else:
            state = TapPageTurnState.ENABLE_PENDING_REBOOT
        return TapPageTurnStatus(
            state, identity, package, available, detail, dropin_exists
        )
    if active:
        state = TapPageTurnState.DISABLE_PENDING_REBOOT
    elif base_exists:
        state = TapPageTurnState.INSTALLED_DISABLED
    else:
        state = TapPageTurnState.NOT_INSTALLED
    return TapPageTurnStatus(
        state, identity, package, available, dropin_present=dropin_exists
    )


def get_cloud_status(ssh_client, state_dir: str) -> TapPageTurnStatus:
    return get_status(ssh_client, load_catalog(state_dir))


def _deployment_mode(
    ssh_client,
    package: TapPageTurnPackage,
) -> str:
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
    if conflicts and conflicts != [SHARED_XOVI_DROPIN]:
        raise RuntimeError(
            "检测到其他 xochitl/Xovi 持久化配置，拒绝自动合并：" + output
        )

    vellum_available = ssh_client.file_exists(VELLUM_BIN)
    xovi_installed = False
    if vellum_available:
        xovi_installed = _vellum_installed_version(ssh_client, "xovi") is not None
    shared_files_present = any(
        ssh_client.file_exists(path)
        for path in (SHARED_XOVI_LIBRARY, SHARED_QRR_LIBRARY, SHARED_APPLOAD_LIBRARY)
    )
    if not conflicts and not xovi_installed and not shared_files_present:
        return "standalone"
    if not xovi_installed:
        raise RuntimeError("检测到非 Vellum 管理的 Xovi 文件或启动配置，拒绝自动合并。")
    if ssh_client.file_exists(DROPIN_PATH):
        raise RuntimeError("设备同时存在 rmtool 独立配置和 Vellum Xovi，请先停用旧配置。")

    expected_dropin = "\n".join(
        (
            "[Service]",
            f'Environment="LD_PRELOAD={SHARED_XOVI_LIBRARY}"',
            'Environment="XOVI_ROOT=/home/root/xovi/services/xochitl.service/"',
        )
    )
    if conflicts and _remote_text(ssh_client, SHARED_XOVI_DROPIN).strip() != expected_dropin:
        raise RuntimeError("AppLoader Xovi 启动配置不是 rmtool 支持的标准布局。")
    links = ssh_client.exec_checked(
        "readlink -f /home/root/xovi/services/xochitl.service/extensions.d; "
        "readlink -f /home/root/xovi/services/xochitl.service/exthome"
    ).splitlines()
    if links != [f"{SHARED_XOVI_BASE}/extensions.d", f"{SHARED_XOVI_BASE}/exthome"]:
        raise RuntimeError("AppLoader Xovi 服务目录链接不是 rmtool 支持的标准布局。")
    _assert_vellum_runtime(ssh_client, package)
    if ssh_client.file_exists(REMOTE_BASE) and not ssh_client.file_exists(MARKER_PATH):
        raise RuntimeError("rmtool 点击翻页目录缺少所有权标记，拒绝覆盖。")
    if ssh_client.file_exists(SHARED_QMD):
        vellum_owner = _vellum_package_owns_path(
            ssh_client, VELLUM_PACKAGE_NAME, SHARED_QMD
        ) if _vellum_installed_version(
            ssh_client, VELLUM_PACKAGE_NAME
        ) is not None else False
        legacy_owner = _legacy_shared_qmd_owned(ssh_client, package)
        if not vellum_owner and not legacy_owner:
            raise RuntimeError("点击翻页 QMD 已存在但不归 Vellum 或旧版 rmtool 所有。")
    return "vellum"


def _legacy_shared_qmd_owned(
    ssh_client,
    package: TapPageTurnPackage,
) -> bool:
    if not (
        ssh_client.file_exists(MARKER_PATH)
        and ssh_client.file_exists(SHARED_QMD)
    ):
        return False
    marker = _read_marker(ssh_client)
    expected_sha = package.file(
        "exthome/qt-resource-rebuilder/tap-page-turn.qmd"
    ).sha256
    return (
        marker.get("schema_version") == 2
        and marker.get("deployment_mode") == "shared_xovi"
        and marker.get("qmd_path") == SHARED_QMD
        and marker.get("qmd_sha256") == expected_sha
        and _remote_sha256(ssh_client, SHARED_QMD) == expected_sha
    )


def _preflight_device(ssh_client) -> None:
    required_commands = (
        "awk",
        "cat",
        "chmod",
        "chown",
        "cmp",
        "cp",
        "dirname",
        "grep",
        "mount",
        "mv",
        "sha256sum",
        "systemctl",
        "umount",
    )
    command_list = " ".join(required_commands)
    ssh_client.exec_checked(
        "for cmd in "
        + command_list
        + '; do command -v "$cmd" >/dev/null 2>&1 || { '
        + 'echo "missing:$cmd" >&2; exit 1; }; done'
    )
    service_state = ssh_client.exec_checked(
        "systemctl is-active xochitl"
    ).strip()
    if service_state != "active":
        raise RuntimeError(
            f"原生 xochitl 当前不是 active（{service_state or '未知'}），拒绝部署。"
        )
    available_output = ssh_client.exec_checked(
        "df -Pk /home | awk 'NR==2 {print $4}'"
    ).strip()
    try:
        available_kib = int(available_output)
    except ValueError as exc:
        raise RuntimeError("无法确认设备 /home 剩余空间。") from exc
    if available_kib < 64 * 1024:
        raise RuntimeError("设备 /home 剩余空间不足 64 MiB，拒绝部署。")

    counters = ssh_client.exec_checked(
        "for file in /sys/devices/platform/lpgpr/root*_errcnt; do "
        '[ -e "$file" ] || continue; cat "$file"; done'
    ).split()
    try:
        values = [int(value) for value in counters]
    except ValueError as exc:
        raise RuntimeError("设备返回了无效的 A/B 错误计数。") from exc
    if any(value != 0 for value in values):
        raise RuntimeError(
            "设备 A/B 错误计数不为 0，拒绝在不稳定状态下部署。"
        )


def _activation_script(stage: str, backup: str, token: str) -> str:
    mount_dir = f"/tmp/rmtool-tap-rootfs-{token}"
    source_dropin = f"{REMOTE_BASE}/systemd/{DROPIN_NAME}"
    return f"""#!/bin/sh
set -eu
BASE={shlex.quote(REMOTE_BASE)}
STAGE={shlex.quote(stage)}
BACKUP={shlex.quote(backup)}
DROPIN={shlex.quote(DROPIN_PATH)}
MOUNT_DIR={shlex.quote(mount_dir)}
MOVED=0
HAD_BASE=0
MOUNTED=0

unmount_root() {{
    if [ "$MOUNTED" -eq 1 ]; then
        sync
        mount -o remount,ro "$MOUNT_DIR" 2>/dev/null || true
        umount "$MOUNT_DIR" 2>/dev/null || umount -l "$MOUNT_DIR" 2>/dev/null || true
        MOUNTED=0
    fi
    rmdir "$MOUNT_DIR" 2>/dev/null || true
}}

remove_lower_dropin() {{
    mkdir -p "$MOUNT_DIR"
    mount --bind / "$MOUNT_DIR"
    MOUNTED=1
    mount -o remount,rw "$MOUNT_DIR"
    rm -f "$MOUNT_DIR$DROPIN"
    unmount_root
}}

rollback() {{
    rc=$?
    trap - EXIT INT TERM
    unmount_root
    if [ "$rc" -ne 0 ]; then
        rm -f "$DROPIN"
        remove_lower_dropin 2>/dev/null || true
        if [ "$MOVED" -eq 1 ]; then
            rm -rf "$BASE"
            if [ "$HAD_BASE" -eq 1 ] && [ -d "$BACKUP" ]; then
                mv "$BACKUP" "$BASE"
            fi
        fi
        systemctl daemon-reload 2>/dev/null || true
    fi
    exit "$rc"
}}
trap rollback EXIT INT TERM

if [ -e "$BASE" ]; then
    HAD_BASE=1
    mv "$BASE" "$BACKUP"
fi
mv "$STAGE" "$BASE"
MOVED=1

mkdir -p "$(dirname "$DROPIN")" "$MOUNT_DIR"
mount --bind / "$MOUNT_DIR"
MOUNTED=1
mount -o remount,rw "$MOUNT_DIR"
mkdir -p "$MOUNT_DIR$(dirname "$DROPIN")"
cp {shlex.quote(source_dropin)} "$DROPIN.tmp"
chmod 0644 "$DROPIN.tmp"
mv -f "$DROPIN.tmp" "$DROPIN"
cp {shlex.quote(source_dropin)} "$MOUNT_DIR$DROPIN.tmp"
chmod 0644 "$MOUNT_DIR$DROPIN.tmp"
mv -f "$MOUNT_DIR$DROPIN.tmp" "$MOUNT_DIR$DROPIN"
cmp -s {shlex.quote(source_dropin)} "$DROPIN"
cmp -s {shlex.quote(source_dropin)} "$MOUNT_DIR$DROPIN"
unmount_root
systemctl daemon-reload
rm -rf "$BACKUP"
trap - EXIT INT TERM
"""


def _disable_script(token: str) -> str:
    mount_dir = f"/tmp/rmtool-tap-rootfs-{token}"
    return f"""#!/bin/sh
set -eu
DROPIN={shlex.quote(DROPIN_PATH)}
MOUNT_DIR={shlex.quote(mount_dir)}
MOUNTED=0
cleanup() {{
    if [ "$MOUNTED" -eq 1 ]; then
        sync
        mount -o remount,ro "$MOUNT_DIR" 2>/dev/null || true
        umount "$MOUNT_DIR" 2>/dev/null || umount -l "$MOUNT_DIR" 2>/dev/null || true
    fi
    rmdir "$MOUNT_DIR" 2>/dev/null || true
}}
trap cleanup EXIT INT TERM
rm -f "$DROPIN"
mkdir -p "$MOUNT_DIR"
mount --bind / "$MOUNT_DIR"
MOUNTED=1
mount -o remount,rw "$MOUNT_DIR"
rm -f "$MOUNT_DIR$DROPIN"
cleanup
MOUNTED=0
trap - EXIT INT TERM
systemctl daemon-reload
"""


def _qmd_check_command(stage: str) -> str:
    check_root = f"{stage}/check"
    return (
        f"mkdir -p {check_root}/hashtabs {check_root}/qmd && "
        f"cp {stage}/exthome/qt-resource-rebuilder/hashtab "
        f"{check_root}/hashtabs/hashtab-device && "
        f"cp {stage}/exthome/qt-resource-rebuilder/tap-page-turn.qmd "
        f"{check_root}/qmd/tap-page-turn.qmd && "
        f"{stage}/qmd-tool check -hashtabs {check_root}/hashtabs "
        f"-qmd {check_root}/qmd"
    )


def _vellum_qmd_check_command(stage: str) -> str:
    check_root = f"{stage}/check"
    return (
        f"mkdir -p {check_root}/hashtabs {check_root}/qmd && "
        f"cp {SHARED_HASHTAB} {check_root}/hashtabs/hashtab-device && "
        f"cp {stage}/tap-page-turn.qmd {check_root}/qmd/tap-page-turn.qmd && "
        f"{stage}/qmd-tool check -hashtabs {check_root}/hashtabs "
        f"-qmd {check_root}/qmd"
    )


def _upload_text(ssh_client, content: str | bytes, remote_path: str, mode: int) -> None:
    data = content.encode("utf-8") if isinstance(content, str) else content
    with tempfile.NamedTemporaryFile(delete=False) as temporary:
        temporary.write(data)
        local_path = temporary.name
    try:
        ssh_client.transfer_file(local_path, remote_path)
        ssh_client.exec_checked(f"chmod {mode:o} {shlex.quote(remote_path)}")
    finally:
        Path(local_path).unlink(missing_ok=True)


def _write_vellum_marker(
    ssh_client,
    marker: bytes,
    token: str,
) -> None:
    staged_marker = f"/tmp/rmtool-tap-marker-{token}.json"
    backup = f"{MARKER_PATH}.backup-{token}"
    marker_sha = hashlib.sha256(marker).hexdigest()
    _upload_text(ssh_client, marker, staged_marker, 0o644)
    command = f"""set -eu
MARKER={shlex.quote(MARKER_PATH)}
STAGED={shlex.quote(staged_marker)}
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
printf '%s  %s\n' {shlex.quote(marker_sha)} "$MARKER" | sha256sum -c -
rm -f "$BACKUP" "$STAGED"
trap - EXIT INT TERM
"""
    try:
        ssh_client.exec_checked(f"/bin/sh -c {shlex.quote(command)}")
    finally:
        try:
            ssh_client.exec_checked(f"rm -f {shlex.quote(staged_marker)}")
        except Exception:
            logging.exception("Could not remove temporary Vellum marker")


def _enable_vellum(
    ssh_client,
    package: TapPageTurnPackage,
    archive_path: str | Path,
) -> TapPageTurnStatus:
    token = uuid.uuid4().hex
    stage = f"/tmp/rmtool-tap-vellum-{token}"
    remote_apk = f"{stage}/{VELLUM_PACKAGE_NAME}.apk"
    process_token = _xochitl_process_token(ssh_client)
    marker = _vellum_marker(package, enabled=True, process_token=process_token)
    qmd_spec = package.file("exthome/qt-resource-rebuilder/tap-page-turn.qmd")
    tool_spec = package.file("qmd-tool")
    expected_version = _vellum_package_version(package)
    legacy_qmd = _legacy_shared_qmd_owned(ssh_client, package)
    marker_before = (
        _remote_text(ssh_client, MARKER_PATH).encode("utf-8")
        if ssh_client.file_exists(MARKER_PATH)
        else None
    )
    installed_before = _vellum_installed_version(ssh_client, VELLUM_PACKAGE_NAME)
    if installed_before is not None and installed_before != expected_version:
        raise RuntimeError(
            f"设备已有其他版本的 {VELLUM_PACKAGE_NAME}（{installed_before}），请先停用。"
        )
    conflicts = sorted(
        set(VELLUM_CONFLICTS) & _vellum_installed_packages(ssh_client)
    )
    if conflicts:
        raise RuntimeError(
            "Vellum 已安装冲突的点击翻页包，请先移除：" + ", ".join(conflicts)
        )

    with tempfile.TemporaryDirectory() as temporary_dir:
        extracted = extract_verified_package(archive_path, package, temporary_dir)
        qmd_local = extracted / "exthome" / "qt-resource-rebuilder" / "tap-page-turn.qmd"
        tool_local = extracted / "qmd-tool"
        license_local = extracted / "LICENSE.qmd-tool"
        apk_data = _build_vellum_apk(
            package,
            qmd_local.read_bytes(),
            license_local.read_bytes(),
        )
        local_apk = Path(temporary_dir) / f"{VELLUM_PACKAGE_NAME}.apk"
        local_apk.write_bytes(apk_data)
        apk_sha = hashlib.sha256(apk_data).hexdigest()
        legacy_backup = f"{stage}/legacy-shared-qmd.backup"
        ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)}")
        try:
            ssh_client.exec_checked(f"mkdir -p {shlex.quote(stage)}")
            if legacy_qmd:
                ssh_client.exec_checked(
                    f"cp {shlex.quote(SHARED_QMD)} {shlex.quote(legacy_backup)} && "
                    f"cmp -s {shlex.quote(SHARED_QMD)} {shlex.quote(legacy_backup)}"
                )
            for local, remote, mode in (
                (qmd_local, f"{stage}/tap-page-turn.qmd", qmd_spec.mode),
                (tool_local, f"{stage}/qmd-tool", tool_spec.mode),
                (local_apk, remote_apk, 0o644),
            ):
                ssh_client.transfer_file(str(local), remote)
                ssh_client.exec_checked(f"chmod {mode:o} {shlex.quote(remote)}")
            ssh_client.exec_checked(f"chown -R root:root {shlex.quote(stage)}")
            for remote, expected in (
                (f"{stage}/tap-page-turn.qmd", qmd_spec.sha256),
                (f"{stage}/qmd-tool", tool_spec.sha256),
                (remote_apk, apk_sha),
            ):
                if _remote_sha256(ssh_client, remote) != expected:
                    raise RuntimeError(f"设备端 Vellum 资源 {remote} 上传校验失败。")

            ssh_client.exec_checked(_vellum_qmd_check_command(stage))
            ssh_client.exec_checked(f"rm -rf {shlex.quote(stage + '/check')}")
            if installed_before is None:
                ssh_client.exec_checked(
                    f"{shlex.quote(VELLUM_BIN)} add --allow-untrusted "
                    f"--simulate {shlex.quote(remote_apk)}"
                )
                ssh_client.exec_checked(
                    f"{shlex.quote(VELLUM_BIN)} add --allow-untrusted "
                    f"{shlex.quote(remote_apk)}"
                )
            if _vellum_installed_version(
                ssh_client, VELLUM_PACKAGE_NAME
            ) != expected_version:
                raise RuntimeError("Vellum 点击翻页包安装后版本校验失败。")
            if not _vellum_package_owns_path(
                ssh_client, VELLUM_PACKAGE_NAME, SHARED_QMD
            ):
                raise RuntimeError("Vellum 未登记点击翻页 QMD 的所有权。")
            if not _vellum_payload_paths_valid(ssh_client):
                raise RuntimeError("Vellum 点击翻页包拥有预期范围外的文件。")
            if _remote_sha256(ssh_client, SHARED_QMD) != qmd_spec.sha256:
                raise RuntimeError("Vellum 点击翻页 QMD 安装后哈希不匹配。")
            _write_vellum_marker(ssh_client, marker, token)
        except Exception:
            package_absent = installed_before is None
            if installed_before is None:
                try:
                    if _vellum_installed_version(
                        ssh_client, VELLUM_PACKAGE_NAME
                    ) is not None:
                        ssh_client.exec_checked(
                            f"{shlex.quote(VELLUM_BIN)} del "
                            f"{shlex.quote(VELLUM_PACKAGE_NAME)}"
                        )
                    package_absent = (
                        _vellum_installed_version(
                            ssh_client, VELLUM_PACKAGE_NAME
                        )
                        is None
                    )
                except Exception:
                    package_absent = False
                    logging.exception("Could not roll back Vellum tap-to-turn package")
            if package_absent and legacy_qmd:
                try:
                    restore_command = f"""set -eu
cp {shlex.quote(legacy_backup)} {shlex.quote(SHARED_QMD)}
chmod 0644 {shlex.quote(SHARED_QMD)}
chown root:root {shlex.quote(SHARED_QMD)}
printf '%s  %s\n' {shlex.quote(qmd_spec.sha256)} {shlex.quote(SHARED_QMD)} | sha256sum -c -
"""
                    ssh_client.exec_checked(
                        f"/bin/sh -c {shlex.quote(restore_command)}"
                    )
                except Exception:
                    package_absent = False
                    logging.exception("Could not restore legacy shared-Xovi QMD")
            if installed_before is not None or package_absent:
                try:
                    if marker_before is None:
                        ssh_client.exec_checked(f"rm -f {shlex.quote(MARKER_PATH)}")
                    else:
                        _write_vellum_marker(
                            ssh_client,
                            marker_before,
                            f"rollback-{token}",
                        )
                except Exception:
                    logging.exception("Could not restore tap-to-turn marker")
            try:
                ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)}")
            except Exception:
                logging.exception("Could not clean Vellum staging files")
            raise
        finally:
            try:
                ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)}")
            except Exception:
                logging.exception("Could not remove Vellum temporary files")
    return get_status(ssh_client, (package,))


def enable(
    ssh_client,
    package: TapPageTurnPackage,
    archive_path: str | Path,
) -> TapPageTurnStatus:
    identity = get_device_identity(ssh_client)
    if select_package((package,), identity) is None:
        raise RuntimeError("当前设备与点击翻页包不精确匹配，未执行修改。")
    _preflight_device(ssh_client)
    deployment_mode = _deployment_mode(ssh_client, package)
    if deployment_mode == "vellum":
        return _enable_vellum(ssh_client, package, archive_path)

    token = uuid.uuid4().hex
    stage = f"{REMOTE_BASE}.staging-{token}"
    backup = f"{REMOTE_BASE}.backup-{token}"
    remote_script = f"/tmp/rmtool-tap-activate-{token}.sh"
    with tempfile.TemporaryDirectory() as temporary_dir:
        extracted = extract_verified_package(archive_path, package, temporary_dir)
        launcher = _launcher(package)
        dropin = _dropin(package)
        launcher_sha = hashlib.sha256(launcher.encode("utf-8")).hexdigest()
        dropin_sha = hashlib.sha256(dropin.encode("utf-8")).hexdigest()
        (extracted / "launcher.sh").write_text(launcher, encoding="utf-8", newline="\n")
        systemd_dir = extracted / "systemd"
        systemd_dir.mkdir()
        (systemd_dir / DROPIN_NAME).write_text(dropin, encoding="utf-8", newline="\n")
        (extracted / "package.json").write_bytes(_marker(package, launcher_sha, dropin_sha))

        ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)} {shlex.quote(backup)}")
        try:
            directories = {stage}
            files: list[tuple[Path, str, int]] = []
            for spec in package.files:
                remote = posixpath.join(stage, spec.path)
                directories.add(posixpath.dirname(remote))
                files.append(
                    (
                        extracted.joinpath(*PurePosixPath(spec.path).parts),
                        remote,
                        spec.mode,
                    )
                )
            extra_files = (
                (extracted / "launcher.sh", f"{stage}/launcher.sh", 0o755),
                (systemd_dir / DROPIN_NAME, f"{stage}/systemd/{DROPIN_NAME}", 0o644),
                (extracted / "package.json", f"{stage}/package.json", 0o644),
            )
            directories.update(
                posixpath.dirname(remote) for _local, remote, _mode in extra_files
            )
            ssh_client.exec_checked(
                "mkdir -p " + " ".join(shlex.quote(item) for item in sorted(directories))
            )
            for local, remote, mode in (*files, *extra_files):
                ssh_client.transfer_file(str(local), remote)
                ssh_client.exec_checked(f"chmod {mode:o} {shlex.quote(remote)}")
            ssh_client.exec_checked(f"chown -R root:root {shlex.quote(stage)}")

            for spec in package.files:
                remote = posixpath.join(stage, spec.path)
                if _remote_sha256(ssh_client, remote) != spec.sha256:
                    raise RuntimeError(f"设备端资源 {spec.path} 上传校验失败。")
            if _remote_sha256(ssh_client, f"{stage}/launcher.sh") != launcher_sha:
                raise RuntimeError("设备端启动包装器上传校验失败。")
            if (
                _remote_sha256(ssh_client, f"{stage}/systemd/{DROPIN_NAME}")
                != dropin_sha
            ):
                raise RuntimeError("设备端 systemd 配置上传校验失败。")

            check_root = f"{stage}/check"
            ssh_client.exec_checked(_qmd_check_command(stage))
            ssh_client.exec_checked(f"rm -rf {shlex.quote(check_root)}")

            _upload_text(
                ssh_client,
                _activation_script(stage, backup, token),
                remote_script,
                0o755,
            )
            ssh_client.exec_checked(f"/bin/sh {shlex.quote(remote_script)}")
        except Exception:
            try:
                ssh_client.exec_checked(
                    f"rm -rf {shlex.quote(stage)}; "
                    f"rm -f {shlex.quote(remote_script)}"
                )
            except Exception:
                logging.exception("Could not clean tap-to-turn staging files")
            raise
        finally:
            try:
                ssh_client.exec_checked(f"rm -f {shlex.quote(remote_script)}")
            except Exception:
                logging.exception("Could not remove tap-to-turn activation script")
    return get_status(ssh_client, (package,))


def enable_cloud(
    ssh_client,
    package: TapPageTurnPackage,
    state_dir: str,
) -> TapPageTurnStatus:
    archive = download_package(package, state_dir)
    return enable(ssh_client, package, archive)


def _disable_vellum(
    ssh_client,
    catalog: Iterable[TapPageTurnPackage],
) -> TapPageTurnStatus:
    installed_version = _vellum_installed_version(
        ssh_client, VELLUM_PACKAGE_NAME
    )
    if installed_version is None:
        raise RuntimeError("Vellum 点击翻页包未安装，未执行删除。")
    if not _vellum_payload_paths_valid(ssh_client):
        raise RuntimeError(
            "Vellum 点击翻页包拥有预期范围外的文件，拒绝自动卸载。"
        )
    process_token = _xochitl_process_token(ssh_client)
    token = uuid.uuid4().hex
    ssh_client.exec_checked(
        f"{shlex.quote(VELLUM_BIN)} del {shlex.quote(VELLUM_PACKAGE_NAME)}"
    )
    if _vellum_installed_version(ssh_client, VELLUM_PACKAGE_NAME) is not None:
        raise RuntimeError("Vellum 点击翻页包删除后仍在包数据库中。")
    if ssh_client.file_exists(SHARED_QMD):
        raise RuntimeError("Vellum 删除完成后点击翻页 QMD 仍然存在。")

    packages = tuple(catalog)
    package = select_package(packages, get_device_identity(ssh_client))
    if package is not None:
        marker = _vellum_marker(
            package,
            enabled=False,
            process_token=process_token,
        )
        _write_vellum_marker(ssh_client, marker, token)
    else:
        ssh_client.exec_checked(f"rm -f {shlex.quote(MARKER_PATH)}")
    return get_status(ssh_client, catalog)


def disable(
    ssh_client,
    catalog: Iterable[TapPageTurnPackage] = (),
) -> TapPageTurnStatus:
    if ssh_client.file_exists(VELLUM_BIN):
        installed_version = _vellum_installed_version(
            ssh_client, VELLUM_PACKAGE_NAME
        )
        if installed_version is not None:
            return _disable_vellum(ssh_client, catalog)
    if ssh_client.file_exists(MARKER_PATH):
        marker = _read_marker(ssh_client)
        if marker.get("deployment_mode") in ("shared_xovi", "vellum"):
            raise RuntimeError(
                "Vellum 点击翻页包未安装；拒绝直接删除共享 Xovi 目录中的文件。"
            )
        if marker.get("schema_version") in (2, 3):
            raise RuntimeError("点击翻页安装标记的部署模式无效，拒绝自动停用。")
    token = uuid.uuid4().hex
    remote_script = f"/tmp/rmtool-tap-disable-{token}.sh"
    try:
        _upload_text(ssh_client, _disable_script(token), remote_script, 0o755)
        ssh_client.exec_checked(f"/bin/sh {shlex.quote(remote_script)}")
    finally:
        try:
            ssh_client.exec_checked(f"rm -f {shlex.quote(remote_script)}")
        except Exception:
            logging.exception("Could not remove tap-to-turn disable script")
    return get_status(ssh_client, catalog)
