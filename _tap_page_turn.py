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
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

import _xovi_standalone


REPO_URL = "https://github.com/pretenderlu/rmtool"
ASSET_RELEASE_URL = f"{REPO_URL}/releases/download/tap-page-turn-assets"
COS_URL = (
    "https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/"
    "tap-page-turn"
)
REMOTE_BASE_URLS = (COS_URL, ASSET_RELEASE_URL)
MANIFEST_URLS = tuple(f"{base_url}/manifest.json" for base_url in REMOTE_BASE_URLS)
MANIFEST_URL = MANIFEST_URLS[0]
BUNDLED_MANIFEST = Path(__file__).resolve().parent / "tap-page-turn" / "manifest.json"

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
VELLUM_UNINSTALL_URL = "https://github.com/vellum-dev/vellum-cli#usage"
VELLUM_UNINSTALL_COMMAND = "vellum self uninstall"
RMTOOL_VELLUM_PACKAGE_NAMES = (
    "rmtool-tap-page-turn",
    "rmtool-fast-mono-reading",
)
VELLUM_LICENSE_DIR = f"{VELLUM_ROOT}/licenses/{VELLUM_PACKAGE_NAME}"
VELLUM_LICENSE_PATH = f"{VELLUM_LICENSE_DIR}/LICENSE"
VELLUM_SOURCES_PATH = f"{VELLUM_LICENSE_DIR}/SOURCES"

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

_STANDALONE_LAYOUT = _xovi_standalone.StandaloneLayout(
    remote_base=REMOTE_BASE,
    dropin_name=DROPIN_NAME,
    log_tag="rmtool-tap-page-turn",
    mount_tag="rmtool-tap",
)


class TapPageTurnState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    INSTALLED_DISABLED = "installed_disabled"
    ENABLE_PENDING_REBOOT = "enable_pending_reboot"
    ENABLED = "enabled"
    DISABLE_PENDING_REBOOT = "disable_pending_reboot"
    OUTDATED = "outdated"
    LEGACY_VELLUM = "legacy_vellum"
    VELLUM_RUNTIME = "vellum_runtime"
    FIRMWARE_RESIDUE = "firmware_residue"
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


@lru_cache(maxsize=1)
def _trusted_catalog() -> tuple[TapPageTurnPackage, ...]:
    if not BUNDLED_MANIFEST.is_file():
        raise RuntimeError("缺少内置点击翻页信任清单。")
    return parse_manifest(BUNDLED_MANIFEST.read_bytes())


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


def _cache_dir(state_dir: str) -> Path:
    return Path(state_dir) / "cache" / "tap-page-turn"


def load_catalog(
    state_dir: str, *, refresh: bool = True
) -> tuple[TapPageTurnPackage, ...]:
    manifest_path = _cache_dir(state_dir) / "manifest.json"
    if refresh:
        for manifest_url in MANIFEST_URLS:
            try:
                data = _download_limited(manifest_url, MAX_MANIFEST_BYTES)
                catalog = parse_manifest(data)
                _write_atomic(manifest_path, data)
                return catalog
            except Exception as exc:
                logging.warning(
                    "Could not load tap-to-turn manifest from %s: %s",
                    manifest_url,
                    exc,
                )
    for candidate in (manifest_path, BUNDLED_MANIFEST):
        if candidate.is_file():
            try:
                return parse_manifest(candidate.read_bytes())
            except Exception as exc:
                logging.warning(
                    "Tap-to-turn manifest is invalid (%s): %s", candidate, exc
                )
    raise RuntimeError("无法获取点击翻页云端清单，且没有可用缓存或内置清单。")


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
    last_error: Optional[Exception] = None
    for download_url in package.download_urls:
        try:
            data = _download_limited(download_url, MAX_PACKAGE_BYTES)
            if len(data) != package.size:
                raise RuntimeError("点击翻页资源包大小与云端清单不匹配。")
            if hashlib.sha256(data).hexdigest() != package.sha256:
                raise RuntimeError("点击翻页资源包 SHA-256 校验失败。")
            _write_atomic(destination, data)
            return destination
        except Exception as exc:
            last_error = exc
            logging.warning(
                "Could not download tap-to-turn package from %s: %s",
                download_url,
                exc,
            )
    raise RuntimeError("无法从可用镜像下载并校验点击翻页资源包。") from last_error


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
    include_directories: bool = True,
) -> bytes:
    output = io.BytesIO()
    directories: set[str] = set()
    if include_directories:
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
    return _xovi_standalone.launcher(
        package, package.files, _RUNTIME_PATHS, _STANDALONE_LAYOUT
    )


def _dropin(package: TapPageTurnPackage) -> str:
    del package
    return _xovi_standalone.dropin(_RUNTIME_PATHS, _STANDALONE_LAYOUT)


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


def _shared_specs(package: TapPageTurnPackage):
    return _xovi_standalone.specs_from_package(
        package,
        "tap-page-turn",
        "exthome/qt-resource-rebuilder/tap-page-turn.qmd",
    )


def _legacy_spec(package: TapPageTurnPackage):
    runtime, feature = _shared_specs(package)
    launcher_sha = hashlib.sha256(_launcher(package).encode()).hexdigest()
    dropin_sha = hashlib.sha256(_dropin(package).encode()).hexdigest()
    return _xovi_standalone.LegacyStandaloneSpec(
        feature,
        runtime,
        _STANDALONE_LAYOUT,
        json.loads(_marker(package, launcher_sha, dropin_sha)),
        tuple(
            _xovi_standalone.SharedFileSpec(
                item.path, item.sha256, item.size, item.mode
            )
            for item in package.files
        ),
    )


def _trusted_shared_context(identity: DeviceIdentity):
    package = select_package(_trusted_catalog(), identity)
    runtime = None
    trusted = {}
    legacies = []
    if package is not None:
        runtime, feature = _shared_specs(package)
        trusted[feature.feature_id] = feature
        legacies.append(_legacy_spec(package))
    try:
        import _fast_mono_reading as fast

        peer = next(
            (
                item
                for item in fast._trusted_catalog()
                if item.firmware == identity.firmware
                and item.platform == identity.platform
                and item.architecture == identity.architecture
                and item.xochitl_sha256 == identity.xochitl_sha256
            ),
            None,
        )
        if peer is not None:
            peer_runtime, peer_feature = fast._shared_specs(peer)
            if runtime is not None and peer_runtime != runtime:
                raise RuntimeError("点击翻页与快速黑白的内置运行资源不一致。")
            runtime = runtime or peer_runtime
            trusted[peer_feature.feature_id] = peer_feature
            legacies.append(fast._legacy_spec(peer))
    except ImportError:
        pass
    try:
        import _native_chinese as native

        peer = native.select_package(native._trusted_catalog(), identity)
        if peer is not None:
            peer_runtime, peer_feature = native._shared_specs(peer)
            if runtime is not None and peer_runtime != runtime:
                raise RuntimeError("点击翻页与原生中文的内置运行资源不一致。")
            runtime = runtime or peer_runtime
            trusted[peer_feature.feature_id] = peer_feature
    except ImportError:
        pass
    try:
        import _pinyin_input as pinyin

        peer = pinyin.select_package(pinyin._trusted_catalog(), identity)
        if peer is not None:
            peer_runtime, peer_feature = pinyin._shared_specs(peer)
            if runtime is not None and peer_runtime != runtime:
                raise RuntimeError("点击翻页与拼音输入法的内置运行资源不一致。")
            runtime = runtime or peer_runtime
            trusted[peer_feature.feature_id] = peer_feature
    except ImportError:
        pass
    if runtime is None:
        raise RuntimeError("内置点击翻页清单没有当前设备的精确包。")
    _xovi_standalone.assert_feature_layout(runtime, trusted.values())
    return runtime, trusted, tuple(legacies)


def _trusted_shared_context_from_marker(ssh_client):
    identity = DeviceIdentity(*_xovi_standalone.read_shared_identity(ssh_client))
    return _trusted_shared_context(identity)


def _inspect_shared_firmware_residue(
    ssh_client,
    runtime: _xovi_standalone.SharedRuntimeSpec,
    trusted: dict[str, _xovi_standalone.SharedFeatureSpec],
    current_identity: tuple[str, str, str, str],
):
    import _fast_mono_reading as fast

    installed_identity = DeviceIdentity(
        runtime.firmware,
        runtime.platform,
        runtime.architecture,
        runtime.xochitl_sha256,
    )
    fast_package = fast.select_package(fast._trusted_catalog(), installed_identity)
    if fast_package is not None:
        inspection, installed_trusted, _outdated = fast._inspect_shared_revision(
            ssh_client,
            runtime,
            trusted,
            fast_package,
            firmware_residue_identity=current_identity,
        )
        return inspection, installed_trusted
    return (
        _xovi_standalone.inspect_shared_firmware_residue(
            ssh_client,
            runtime,
            trusted,
            current_identity,
        ),
        trusted,
    )


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


def _package_from_marker(catalog, marker: dict):
    matches = {
        package
        for package in catalog
        if marker.get("package_id") == package.package_id
        and marker.get("firmware") == package.firmware
        and marker.get("platform") == package.platform
        and marker.get("xochitl_sha256") == package.xochitl_sha256
    }
    return next(iter(matches)) if len(matches) == 1 else None


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


def _vellum_runtime_present(ssh_client) -> bool:
    return any(
        ssh_client.file_exists(path)
        for path in (
            VELLUM_BIN,
            SHARED_XOVI_DROPIN,
            SHARED_XOVI_LIBRARY,
            SHARED_QRR_LIBRARY,
            SHARED_APPLOAD_LIBRARY,
        )
    )


def _assert_shared_xovi_installable(ssh_client) -> None:
    legacy_packages = set()
    if ssh_client.file_exists(VELLUM_BIN):
        legacy_packages = (
            _vellum_installed_packages(ssh_client)
            & set(RMTOOL_VELLUM_PACKAGE_NAMES)
        )
    if legacy_packages:
        raise RuntimeError(
            "检测到 rmtool 安装的旧版 Vellum 功能包，请先在对应功能区域卸载："
            + ", ".join(sorted(legacy_packages))
        )
    if _vellum_runtime_present(ssh_client):
        raise RuntimeError(
            "检测到 Vellum/AppLoader Xovi 运行环境。请按 Vellum 官方说明执行 "
            f"`{VELLUM_UNINSTALL_COMMAND}`，确认运行环境已移除后再安装 rmtool 插件："
            f"{VELLUM_UNINSTALL_URL}"
        )
    allowed_dropins = {
        DROPIN_PATH,
        "/etc/systemd/system/xochitl.service.d/91-rmtool-fast-mono-reading.conf",
        _xovi_standalone.SHARED_LAYOUT.dropin_path,
    }
    output = ssh_client.exec_checked(
        "for file in /etc/systemd/system/xochitl.service.d/*.conf; do "
        "[ -f \"$file\" ] || continue; "
        "if grep -Eq 'LD_PRELOAD|XOVI_ROOT|ExecStart=.*xovi' \"$file\"; then "
        "printf '%s\\n' \"$file\"; fi; done"
    ).strip()
    conflicts = sorted(
        path for path in output.splitlines() if path and path not in allowed_dropins
    )
    if conflicts:
        raise RuntimeError(
            "检测到其他 xochitl/Xovi 持久化配置，拒绝自动合并："
            + ", ".join(conflicts)
        )


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
        if set(marker) != set(expected):
            return False, "Vellum 安装标记字段集合不匹配"
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
        installed_version = (
            _vellum_installed_version(ssh_client, VELLUM_PACKAGE_NAME)
            if ssh_client.file_exists(VELLUM_BIN)
            else None
        )
        if marker["enabled"]:
            if installed_version != expected["vellum_version"]:
                return False, "Vellum 点击翻页包未安装或版本不匹配"
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
    shared_exists = _xovi_standalone.has_shared_artifacts(ssh_client)
    vellum_version = None
    vellum_error = ""
    if ssh_client.file_exists(VELLUM_BIN):
        try:
            vellum_version = _vellum_installed_version(
                ssh_client, VELLUM_PACKAGE_NAME
            )
        except Exception as exc:
            vellum_error = str(exc)
    recovery_available = (
        dropin_exists or marker_exists or vellum_version is not None or shared_exists
    )
    package = select_package(packages, identity)

    if marker_exists:
        try:
            legacy_marker = _read_marker(ssh_client)
        except Exception:
            legacy_marker = None
        if legacy_marker and legacy_marker.get("deployment_mode") == "vellum":
            legacy_package = _package_from_marker(
                _trusted_catalog(), legacy_marker
            )
            if legacy_package is None:
                return TapPageTurnStatus(
                    TapPageTurnState.BROKEN,
                    identity,
                    package,
                    available,
                    "Vellum 点击翻页标记不属于任何内置信任包",
                    True,
                )
            valid, detail = _vellum_payload_valid(
                ssh_client, legacy_package, legacy_marker
            )
            return TapPageTurnStatus(
                TapPageTurnState.LEGACY_VELLUM if valid else TapPageTurnState.BROKEN,
                identity,
                package,
                available,
                (
                    "已精确验证为 rmtool 安装的旧版 Vellum 点击翻页包。"
                    "请先卸载该包；rmtool 不会修改 Vellum 或任何第三方包。"
                    if valid
                    else detail
                ),
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
    if _vellum_runtime_present(ssh_client):
        return TapPageTurnStatus(
            TapPageTurnState.VELLUM_RUNTIME,
            identity,
            package,
            available,
            "请按 Vellum 官方说明执行 "
            f"`{VELLUM_UNINSTALL_COMMAND}`，确认 Vellum/AppLoader Xovi 已移除后，"
            "再使用 rmtool 共享 Xovi 安装。",
            False,
        )

    if shared_exists and package is not None:
        try:
            shared_identity = DeviceIdentity(
                *_xovi_standalone.read_shared_identity(ssh_client)
            )
        except Exception:
            shared_identity = identity
        if shared_identity != identity:
            try:
                runtime, trusted, legacies = _trusted_shared_context(shared_identity)
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
                inspection, _installed_trusted = _inspect_shared_firmware_residue(
                    ssh_client,
                    runtime,
                    trusted,
                    (
                        identity.firmware,
                        identity.platform,
                        identity.architecture,
                        identity.xochitl_sha256,
                    ),
                )
                return TapPageTurnStatus(
                    TapPageTurnState.FIRMWARE_RESIDUE,
                    identity,
                    package,
                    available,
                    "旧共享目录与内置旧包完全一致，且上下层 drop-in 均已由固件升级移除；"
                    "旧功能当前未载入。清理会一并移除点击翻页和快速黑白的旧共享状态，"
                    "随后两项功能均可安装当前固件版本。",
                    True,
                )
            except Exception as exc:
                return TapPageTurnStatus(
                    TapPageTurnState.BROKEN,
                    identity,
                    package,
                    available,
                    str(exc),
                    True,
                )
    if package is None and shared_exists:
        try:
            runtime, trusted, legacies = _trusted_shared_context_from_marker(ssh_client)
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
            inspection = _xovi_standalone.inspect_shared(
                ssh_client, runtime, trusted
            )
        except Exception as exc:
            return TapPageTurnStatus(
                TapPageTurnState.BROKEN,
                identity,
                available_packages=available,
                detail=str(exc),
                dropin_present=True,
            )
        if "tap-page-turn" not in inspection.states:
            return TapPageTurnStatus(
                TapPageTurnState.INCOMPATIBLE,
                identity,
                available_packages=available,
                detail="共享 Xovi 正由其他 rmtool 功能使用；点击翻页未安装",
                dropin_present=False,
            )
        return TapPageTurnStatus(
            TapPageTurnState.INCOMPATIBLE,
            identity,
            available_packages=available,
            detail="检测到属于其他固件的有效共享安装；可安全停用，不能在当前固件载入",
            dropin_present=True,
        )
    if package is None:
        return TapPageTurnStatus(
            TapPageTurnState.INCOMPATIBLE,
            identity,
            available_packages=available,
            detail="没有与设备身份和 xochitl 哈希精确匹配的包",
            dropin_present=recovery_available,
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
            inspection = _xovi_standalone.inspect_shared(
                ssh_client, runtime, trusted
            )
            _xovi_standalone.assert_startup_guard_not_latched(inspection)
            state_record = inspection.states.get("tap-page-turn")
            if state_record is None:
                state = TapPageTurnState.NOT_INSTALLED
                detail = "共享 Xovi 正由另一项 rmtool 功能使用"
            else:
                current = _xochitl_process_token(ssh_client)
                process_changed = current != state_record.process_token
                if state_record.enabled:
                    if process_changed and inspection.active:
                        state = TapPageTurnState.ENABLED
                        detail = ""
                    elif not process_changed:
                        state = TapPageTurnState.ENABLE_PENDING_REBOOT
                        detail = "等待手动重启后载入共享 Xovi"
                    else:
                        raise RuntimeError("共享 Xovi 未在当前 xochitl 进程中载入。")
                elif process_changed:
                    state = TapPageTurnState.INSTALLED_DISABLED
                    detail = ""
                else:
                    state = TapPageTurnState.DISABLE_PENDING_REBOOT
                    detail = "等待手动重启后停用点击翻页"
            return TapPageTurnStatus(
                state, identity, package, available, detail, True
            )
        except Exception as exc:
            return TapPageTurnStatus(
                TapPageTurnState.BROKEN,
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
            return TapPageTurnStatus(
                TapPageTurnState.BROKEN,
                identity,
                package,
                available,
                str(exc),
                recovery_available,
            )
    if marker and marker.get("deployment_mode") == "vellum":
        marker_package = _package_from_marker(
            (package, *_trusted_catalog()), marker
        )
        if marker_package is None:
            state = TapPageTurnState.BROKEN
            detail = "Vellum 点击翻页标记不属于任何内置信任包"
        else:
            valid, detail = _vellum_payload_valid(
                ssh_client, marker_package, marker
            )
            if not valid:
                state = TapPageTurnState.BROKEN
            else:
                state = TapPageTurnState.LEGACY_VELLUM
                detail = (
                    "已精确验证为 rmtool 安装的旧版 Vellum 点击翻页包。"
                    "请先卸载该包；rmtool 不会修改 Vellum 或任何第三方包。"
                )
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

    if _vellum_runtime_present(ssh_client):
        return TapPageTurnStatus(
            TapPageTurnState.VELLUM_RUNTIME,
            identity,
            package,
            available,
            "请按 Vellum 官方说明执行 "
            f"`{VELLUM_UNINSTALL_COMMAND}`，确认 Vellum/AppLoader Xovi 已移除后，"
            "再使用 rmtool 共享 Xovi 安装。",
            False,
        )

    if marker and marker.get("deployment_mode") is None:
        marker_package = _package_from_marker(
            (package, *_trusted_catalog()), marker
        )
        if marker_package is None:
            return TapPageTurnStatus(
                TapPageTurnState.BROKEN,
                identity,
                package,
                available,
                "旧版点击翻页标记不属于任何内置信任包",
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
                return TapPageTurnStatus(
                    TapPageTurnState.BROKEN,
                    identity,
                    package,
                    available,
                    str(exc),
                    True,
                )
            return TapPageTurnStatus(
                TapPageTurnState.OUTDATED,
                identity,
                package,
                available,
                "已精确验证为旧固件的独立点击翻页；"
                "请先卸载旧版，再安装当前固件版本。",
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
    _assert_shared_xovi_installable(ssh_client)
    return "standalone"


def _preflight_device(ssh_client) -> None:
    required_commands = (
        "awk",
        "cat",
        "chmod",
        "chown",
        "cmp",
        "cp",
        "dirname",
        "find",
        "grep",
        "mount",
        "mv",
        "sha256sum",
        "stat",
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
    return _xovi_standalone.activation_script(
        stage, backup, token, _STANDALONE_LAYOUT
    )


def _disable_script(token: str) -> str:
    return _xovi_standalone.disable_script(token, _STANDALONE_LAYOUT)


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


def enable(
    ssh_client,
    package: TapPageTurnPackage,
    archive_path: str | Path,
) -> TapPageTurnStatus:
    identity = get_device_identity(ssh_client)
    if select_package((package,), identity) is None:
        raise RuntimeError("当前设备与点击翻页包不精确匹配，未执行修改。")
    trusted_package = select_package(_trusted_catalog(), identity)
    if trusted_package is None or trusted_package != package:
        raise RuntimeError("点击翻页包与内置信任清单不一致，拒绝部署。")
    _preflight_device(ssh_client)
    _deployment_mode(ssh_client, package)

    runtime, trusted, legacies = _trusted_shared_context(identity)
    _runtime, feature = _shared_specs(trusted_package)
    with tempfile.TemporaryDirectory() as temporary_dir:
        extracted = extract_verified_package(archive_path, package, temporary_dir)
        _xovi_standalone.enable_shared(
            ssh_client, runtime, feature, extracted, trusted, legacies
        )
    return get_status(ssh_client, (package,))


def enable_cloud(
    ssh_client,
    package: TapPageTurnPackage,
    state_dir: str,
) -> TapPageTurnStatus:
    archive = download_package(package, state_dir)
    return enable(ssh_client, package, archive)


def _validate_vellum_removal(ssh_client) -> TapPageTurnPackage:
    marker = _read_marker(ssh_client)
    installed_package = _package_from_marker(_trusted_catalog(), marker)
    if installed_package is None:
        raise RuntimeError("内置点击翻页清单无法验证该 Vellum 安装。")
    valid, detail = _vellum_payload_valid(
        ssh_client, installed_package, marker
    )
    if not valid or marker.get("enabled") is not True:
        raise RuntimeError(
            detail or "Vellum 点击翻页安装无法精确验证，拒绝自动卸载。"
        )
    installed_version = _vellum_installed_version(
        ssh_client, VELLUM_PACKAGE_NAME
    )
    if installed_version is None:
        raise RuntimeError("Vellum 点击翻页包未安装，未执行删除。")
    if not _vellum_payload_paths_valid(ssh_client):
        raise RuntimeError(
            "Vellum 点击翻页包拥有预期范围外的文件，拒绝自动卸载。"
        )
    if not _marker_dir_has_only_marker(ssh_client):
        raise RuntimeError("点击翻页标记目录包含未知文件，拒绝自动卸载。")
    return installed_package


def _remove_validated_vellum(ssh_client) -> None:
    ssh_client.exec_checked(
        f"{shlex.quote(VELLUM_BIN)} del {shlex.quote(VELLUM_PACKAGE_NAME)}"
    )
    if _vellum_installed_version(ssh_client, VELLUM_PACKAGE_NAME) is not None:
        raise RuntimeError("Vellum 点击翻页包删除后仍在包数据库中。")
    if ssh_client.file_exists(SHARED_QMD):
        raise RuntimeError("Vellum 删除完成后点击翻页 QMD 仍然存在。")

    ssh_client.exec_checked(
        f"rm -f {shlex.quote(MARKER_PATH)}; "
        f"rmdir {shlex.quote(REMOTE_BASE)} 2>/dev/null || true"
    )


def _disable_vellum(
    ssh_client,
    catalog: Iterable[TapPageTurnPackage],
) -> TapPageTurnStatus:
    _validate_vellum_removal(ssh_client)
    _remove_validated_vellum(ssh_client)
    return get_status(ssh_client, catalog)


def _marker_dir_has_only_marker(ssh_client) -> bool:
    output = ssh_client.exec_checked(
        f"find {shlex.quote(REMOTE_BASE)} -mindepth 1 -maxdepth 1 -print 2>/dev/null"
    )
    return tuple(sorted(line.strip() for line in output.splitlines() if line.strip())) == (
        MARKER_PATH,
    )


def _clear_disabled_vellum_marker(
    ssh_client, catalog: Iterable[TapPageTurnPackage]
) -> TapPageTurnStatus:
    marker = _read_marker(ssh_client)
    package = _package_from_marker(_trusted_catalog(), marker)
    if package is None:
        raise RuntimeError("内置点击翻页清单无法验证该 Vellum 安装标记。")
    valid, detail = _vellum_payload_valid(ssh_client, package, marker)
    if not valid or marker.get("enabled") is not False:
        raise RuntimeError(detail or "Vellum 点击翻页停用标记无法精确验证。")
    if not _marker_dir_has_only_marker(ssh_client):
        raise RuntimeError("点击翻页标记目录包含未知文件，拒绝自动清理。")
    ssh_client.exec_checked(
        f"rm -f {shlex.quote(MARKER_PATH)}; "
        f"rmdir {shlex.quote(REMOTE_BASE)} 2>/dev/null || true"
    )
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
        if marker.get("deployment_mode") == "vellum":
            return _clear_disabled_vellum_marker(ssh_client, catalog)
    if _xovi_standalone.has_shared_artifacts(ssh_client):
        current_identity = get_device_identity(ssh_client)
        marker_identity = DeviceIdentity(
            *_xovi_standalone.read_shared_identity(ssh_client)
        )
        runtime, trusted, _legacies = _trusted_shared_context(marker_identity)
        if marker_identity != current_identity:
            _inspection, installed_trusted = _inspect_shared_firmware_residue(
                ssh_client,
                runtime,
                trusted,
                (
                    current_identity.firmware,
                    current_identity.platform,
                    current_identity.architecture,
                    current_identity.xochitl_sha256,
                ),
            )
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
                "tap-page-turn",
                trusted,
            )
        return get_status(ssh_client, catalog)
    if ssh_client.file_exists(MARKER_PATH):
        marker = _read_marker(ssh_client)
        if marker.get("deployment_mode") in ("shared_xovi", "vellum"):
            raise RuntimeError(
                "Vellum 点击翻页包未安装；拒绝直接删除共享 Xovi 目录中的文件。"
            )
        if marker.get("schema_version") in (2, 3):
            raise RuntimeError("点击翻页安装标记的部署模式无效，拒绝自动停用。")
        marker_package = _package_from_marker(_trusted_catalog(), marker)
        if marker_package is None:
            raise RuntimeError("点击翻页安装标记不属于任何内置信任包。")
        current_package = select_package(
            _trusted_catalog(), get_device_identity(ssh_client)
        )
        if current_package != marker_package:
            _xovi_standalone.remove_verified_legacy(
                ssh_client, _legacy_spec(marker_package)
            )
            return get_status(ssh_client, catalog)
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
