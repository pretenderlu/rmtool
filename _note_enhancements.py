"""Exact-firmware backend for the independent note-enhancements feature."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

import _package_download
import _reading_enhancements as reading
import _tap_page_turn as tap
import _xovi_standalone as shared


REPO_URL = "https://github.com/pretenderlu/rmtool"
ASSET_RELEASE_URL = f"{REPO_URL}/releases/download/note-enhancements-assets"
COS_URL = (
    "https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/"
    "note-enhancements"
)
REMOTE_BASE_URLS = (ASSET_RELEASE_URL, COS_URL)
MANIFEST_URLS = tuple(f"{base}/manifest.json" for base in REMOTE_BASE_URLS)
BUNDLED_MANIFEST = Path(__file__).with_name("note-enhancements") / "manifest.json"

QMD_PAYLOAD_PATH = "exthome/qt-resource-rebuilder/note-enhancements.qmd"
FEATURE_ID = "note-enhancements"
PACKAGE_REVISION = 12
MAX_MANIFEST_BYTES = tap.MAX_MANIFEST_BYTES
MAX_PACKAGE_BYTES = tap.MAX_PACKAGE_BYTES
MAX_UNPACKED_BYTES = tap.MAX_UNPACKED_BYTES
_ASSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz")
_REQUIRED_PATHS = {*shared._COMMON_ARCHIVE_PATHS, QMD_PAYLOAD_PATH}
_PAYLOAD_PATHS = _REQUIRED_PATHS | {
    "LICENSE.qmd-tool",
    "LICENSE.rm-xovi-extensions",
    "LICENSE.xovi",
}

# Note enhancements intentionally follows the exact color-device matrix already
# verified for reading enhancements. A reading target change therefore forces a
# matching note manifest update instead of silently widening device support.
SUPPORTED_PLATFORMS = frozenset(("ferrari", "chiappa"))
ALLOWED_TARGETS = {
    identity: (release, channel, True, False)
    for identity, (release, channel, _offline, _device) in reading.ALLOWED_TARGETS.items()
    if identity[0] in SUPPORTED_PLATFORMS
}

_UPSTREAM_QMD_PATHS = (
    "/home/root/xovi/exthome/qt-resource-rebuilder/delayStrokeScreenRefresh.qmd",
    "/home/root/.local/share/xovi/exthome/qt-resource-rebuilder/delayStrokeScreenRefresh.qmd",
)
_UPSTREAM_QMD_SHA256 = (
    "e62171129de6b26b5f3b76d32be889344f98df33832d65ec3ba5c4d45ee6f2e6"
)

PayloadFile = tap.PayloadFile
DeviceIdentity = tap.DeviceIdentity


class NoteEnhancementsState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    MIGRATION_AVAILABLE = "migration_available"
    REPAIR_AVAILABLE = "repair_available"
    INSTALLED_DISABLED = "installed_disabled"
    ENABLE_PENDING_REBOOT = "enable_pending_reboot"
    ENABLED = "enabled"
    DISABLE_PENDING_REBOOT = "disable_pending_reboot"
    BROKEN = "broken"


@dataclass(frozen=True)
class NoteEnhancementsPackage:
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
    urls: tuple[str, str]
    package_revision: int
    offline_verified: bool
    device_verified: bool

    @property
    def package_id(self) -> str:
        return f"{self.platform}-{self.firmware}-{self.xochitl_sha256[:12]}"

    @property
    def download_urls(self) -> tuple[str, ...]:
        return (f"{ASSET_RELEASE_URL}/{self.asset}", f"{COS_URL}/{self.asset}")

    @property
    def download_url(self) -> str:
        return self.download_urls[0]

    def file(self, path: str) -> PayloadFile:
        for item in self.files:
            if item.path == path:
                return item
        raise KeyError(path)


@dataclass(frozen=True)
class NoteEnhancementsStatus:
    state: NoteEnhancementsState
    identity: DeviceIdentity
    package: Optional[NoteEnhancementsPackage] = None
    available_packages: tuple[NoteEnhancementsPackage, ...] = ()
    detail: str = ""
    recovery_available: bool = False
    cleanup_available: bool = False


def _required(entry: dict, key: str, pattern: re.Pattern[str]) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RuntimeError(f"笔记增强清单字段 {key} 无效。")
    return value


def _expected_asset_name(platform: str, firmware: str, release_version: str) -> str:
    return f"rmtool-note-enhancements-{platform}-{firmware}-{release_version}.tar.gz"


def parse_manifest(
    data: bytes, *, require_local_match: bool = True
) -> tuple[NoteEnhancementsPackage, ...]:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("笔记增强清单不是有效 JSON。") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise RuntimeError("笔记增强清单版本不受支持。")
    entries = document.get("packages")
    if not isinstance(entries, list):
        raise RuntimeError("笔记增强清单缺少 packages。")

    packages: list[NoteEnhancementsPackage] = []
    identities: set[tuple[str, str, str]] = set()
    assets: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "firmware",
            "release_version",
            "channel",
            "platform",
            "architecture",
            "xochitl_sha256",
            "offline_verified",
            "device_verified",
            "package_revision",
            "asset",
            "sha256",
            "size",
            "urls",
            "files",
        }:
            raise RuntimeError("笔记增强清单包格式无效。")
        firmware = _required(entry, "firmware", tap._FIRMWARE_RE)
        release_version = _required(entry, "release_version", tap._VERSION_RE)
        channel = entry.get("channel")
        if channel not in ("stable", "beta"):
            raise RuntimeError("笔记增强清单发布类型无效。")
        platform = _required(entry, "platform", tap._PLATFORM_RE)
        architecture = _required(entry, "architecture", tap._ARCH_RE)
        xochitl_sha = _required(entry, "xochitl_sha256", tap._SHA256_RE)
        asset = _required(entry, "asset", _ASSET_RE)
        digest = _required(entry, "sha256", tap._SHA256_RE)
        size = entry.get("size")
        revision = entry.get("package_revision")
        offline_verified = entry.get("offline_verified")
        device_verified = entry.get("device_verified")
        if revision != PACKAGE_REVISION:
            raise RuntimeError("笔记增强包修订版本不受支持。")
        if type(offline_verified) is not bool or type(device_verified) is not bool:
            raise RuntimeError("笔记增强包验证级别必须是布尔值。")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_PACKAGE_BYTES:
            raise RuntimeError("笔记增强资源包大小无效。")
        raw_files = entry.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise RuntimeError("笔记增强资源包缺少文件清单。")
        files = tuple(tap._parse_payload_file(item) for item in raw_files)
        paths = {item.path for item in files}
        if len(paths) != len(files) or paths != _PAYLOAD_PATHS:
            raise RuntimeError("笔记增强资源包文件清单与固定白名单不匹配。")
        if sum(item.size for item in files) > MAX_UNPACKED_BYTES:
            raise RuntimeError("笔记增强资源包解压后过大。")

        identity = (platform, firmware, xochitl_sha)
        if identity in identities or asset in assets:
            raise RuntimeError("笔记增强清单包含重复包。")
        expected = ALLOWED_TARGETS.get((platform, firmware, architecture, xochitl_sha))
        if expected != (release_version, channel, offline_verified, device_verified):
            raise RuntimeError("笔记增强清单身份或验证级别不在本地信任清单中。")
        if asset != _expected_asset_name(platform, firmware, release_version):
            raise RuntimeError("笔记增强资源包文件名与本地信任清单不一致。")
        urls = entry.get("urls")
        expected_urls = {f"{base}/{asset}" for base in REMOTE_BASE_URLS}
        if not isinstance(urls, list) or len(urls) != 2 or set(urls) != expected_urls:
            raise RuntimeError("笔记增强资源包下载源无效。")
        identities.add(identity)
        assets.add(asset)
        packages.append(
            NoteEnhancementsPackage(
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
                tuple(urls),
                revision,
                offline_verified,
                device_verified,
            )
        )
    result = tuple(packages)
    if require_local_match and result != _trusted_catalog():
        raise RuntimeError("笔记增强清单与本地完整目标信任清单不一致。")
    return result


@lru_cache(maxsize=1)
def _trusted_catalog() -> tuple[NoteEnhancementsPackage, ...]:
    if not BUNDLED_MANIFEST.is_file():
        raise RuntimeError("缺少内置笔记增强信任清单。")
    return parse_manifest(BUNDLED_MANIFEST.read_bytes(), require_local_match=False)


def _cache_dir(state_dir: str) -> Path:
    return Path(state_dir) / "cache" / FEATURE_ID


def load_catalog(
    state_dir: str, *, refresh: bool = True
) -> tuple[NoteEnhancementsPackage, ...]:
    manifest_path = _cache_dir(state_dir) / "manifest.json"
    if refresh:
        for url in MANIFEST_URLS:
            try:
                data = tap._download_limited(url, MAX_MANIFEST_BYTES)
                catalog = parse_manifest(data)
                tap._write_atomic(manifest_path, data)
                return catalog
            except Exception as exc:
                logging.warning(
                    "Could not load note-enhancements manifest from %s: %s", url, exc
                )
    for candidate in (manifest_path, BUNDLED_MANIFEST):
        if candidate.is_file():
            try:
                return parse_manifest(candidate.read_bytes())
            except Exception as exc:
                logging.warning(
                    "Note-enhancements manifest is invalid (%s): %s", candidate, exc
                )
    raise RuntimeError("无法获取笔记增强清单，且没有可用缓存或内置清单。")


def download_package(package: NoteEnhancementsPackage, state_dir: str) -> Path:
    destination = _cache_dir(state_dir) / package.firmware / package.asset
    if destination.is_file():
        data = destination.read_bytes()
        if len(data) == package.size and hashlib.sha256(data).hexdigest() == package.sha256:
            return destination
    last_error: Optional[Exception] = None
    for url in package.download_urls:
        try:
            data = tap._download_limited(url, MAX_PACKAGE_BYTES)
            if len(data) != package.size or hashlib.sha256(data).hexdigest() != package.sha256:
                raise RuntimeError("笔记增强资源包与清单校验不匹配。")
            tap._write_atomic(destination, data)
            return destination
        except Exception as exc:
            last_error = exc
            logging.warning(
                "Could not download note-enhancements package from %s: %s", url, exc
            )
    raise _package_download.PackageDownloadError(
        "笔记增强",
        package.asset,
        package.download_urls,
        package.size,
        package.sha256,
        store=lambda source_path: load_local_package(package, source_path, state_dir),
    ) from last_error


def load_local_package(
    package: NoteEnhancementsPackage, source_path: str | Path, state_dir: str
) -> Path:
    data = Path(source_path).read_bytes()
    _package_download.verify_local_package(data, package.size, package.sha256, "笔记增强")
    destination = _cache_dir(state_dir) / package.firmware / package.asset
    tap._write_atomic(destination, data)
    return destination


def extract_verified_package(
    archive_path: str | Path,
    package: NoteEnhancementsPackage,
    destination: str | Path,
) -> Path:
    return tap.extract_verified_package(archive_path, package, destination)


def select_package(
    catalog: Iterable[NoteEnhancementsPackage], identity: DeviceIdentity
) -> Optional[NoteEnhancementsPackage]:
    return next(
        (
            item
            for item in catalog
            if (item.firmware, item.platform, item.architecture, item.xochitl_sha256)
            == (
                identity.firmware,
                identity.platform,
                identity.architecture,
                identity.xochitl_sha256,
            )
        ),
        None,
    )


def _shared_specs(package: NoteEnhancementsPackage):
    return shared.specs_from_package(package, FEATURE_ID, QMD_PAYLOAD_PATH)


def _known_shared_predecessor_specs(package, current):
    return ()


def _trusted_context(identity: DeviceIdentity, package: NoteEnhancementsPackage):
    runtime, trusted, legacies = tap._trusted_shared_context(identity)
    feature = trusted.get(FEATURE_ID)
    if feature is None:
        peer_runtime, feature = _shared_specs(package)
        if peer_runtime != runtime:
            raise RuntimeError("笔记增强与现有共享 Xovi 运行资源不一致。")
        trusted = dict(trusted)
        trusted[FEATURE_ID] = feature
    shared.assert_feature_layout(runtime, trusted.values())
    return runtime, dict(trusted), tuple(legacies), feature


def _peer_revisions(identity: DeviceIdentity, trusted):
    revisions = dict(tap._reading_enhancement_revisions(identity, trusted))
    package = select_package(_trusted_catalog(), identity)
    if package is not None and FEATURE_ID in trusted:
        items = _known_shared_predecessor_specs(package, trusted[FEATURE_ID])
        if items:
            revisions[FEATURE_ID] = items
    for module_name in ("_native_chinese", "_pinyin_input"):
        try:
            module = __import__(module_name)
            package = module.select_package(module._trusted_catalog(), identity)
            if package is not None and module.FEATURE_ID in trusted:
                items = tuple(
                    (item.reason, item.feature)
                    for item in module._known_shared_predecessor_specs(package)
                )
                if items:
                    revisions[module.FEATURE_ID] = items
        except (ImportError, AttributeError):
            pass
    try:
        import _fast_mono_reading as fast

        package = fast.select_package(fast._trusted_catalog(), identity)
        if package is not None and fast.FEATURE_ID in trusted:
            items = tuple(
                (f"package-revision-{revision}", feature)
                for revision, feature in fast._known_shared_predecessor_specs(package)
            )
            if items:
                revisions[fast.FEATURE_ID] = items
    except (ImportError, AttributeError):
        pass
    return revisions


def _inspect_shared(ssh_client, runtime, trusted, identity, *, check_lower=True):
    return shared.inspect_shared_revisions(
        ssh_client,
        runtime,
        trusted,
        _peer_revisions(identity, trusted),
        check_lower=check_lower,
    )


def _known_upstream_qmd(ssh_client) -> Optional[str]:
    for path in _UPSTREAM_QMD_PATHS:
        if not ssh_client.file_exists(path):
            continue
        digest = shared._remote_sha256(ssh_client, path)
        if digest == _UPSTREAM_QMD_SHA256:
            return path
        raise RuntimeError(f"检测到被修改或未知的彩色刷新补丁：{path}。")
    return None


def get_status(
    ssh_client,
    catalog: Iterable[NoteEnhancementsPackage],
) -> NoteEnhancementsStatus:
    packages = tuple(catalog)
    identity = tap.get_device_identity(ssh_client)
    available = tuple(item for item in packages if item.platform == identity.platform)
    package = select_package(packages, identity)
    if package is None:
        return NoteEnhancementsStatus(
            NoteEnhancementsState.INCOMPATIBLE,
            identity,
            available_packages=available,
            detail="当前设备身份不在笔记增强的精确信任清单中。",
        )
    try:
        upstream = _known_upstream_qmd(ssh_client)
        if tap._vellum_runtime_present(ssh_client):
            detail = (
                "检测到已验证的社区延迟刷新补丁；它由 Vellum/外部 Xovi 管理，"
                "rmtool 不会跨所有权自动修改。请先用原管理器卸载。"
                if upstream
                else "检测到 Vellum/AppLoader Xovi 运行环境；为避免所有权冲突，"
                "已阻止笔记增强操作。请先按 Vellum 官方说明移除运行环境。"
            )
            return NoteEnhancementsStatus(
                NoteEnhancementsState.BROKEN,
                identity,
                package,
                available,
                detail,
            )
        if upstream:
            raise RuntimeError("检测到社区延迟刷新补丁，请先移除后再安装笔记增强。")
        runtime, trusted, _legacies, _feature = _trusted_context(identity, package)
        if not shared.has_shared_artifacts(ssh_client):
            return NoteEnhancementsStatus(
                NoteEnhancementsState.NOT_INSTALLED, identity, package, available
            )
        inspection, _installed_trusted, selected = _inspect_shared(
            ssh_client, runtime, trusted, identity
        )
        record = inspection.states.get(FEATURE_ID)
        if FEATURE_ID in selected:
            raise RuntimeError("笔记增强旧版识别结果无效。")
        if record is None:
            return NoteEnhancementsStatus(
                NoteEnhancementsState.NOT_INSTALLED,
                identity,
                package,
                available,
                "共享 Xovi 正由其他已验证功能使用。",
                True,
            )
        current = tap._xochitl_process_token(ssh_client)
        if record.enabled:
            if current == record.process_token:
                return NoteEnhancementsStatus(
                    NoteEnhancementsState.ENABLE_PENDING_REBOOT,
                    identity,
                    package,
                    available,
                    "等待手动重启后载入笔记增强。",
                    True,
                )
            if not inspection.active:
                raise RuntimeError("笔记增强未在当前 xochitl 进程中载入。")
            return NoteEnhancementsStatus(
                NoteEnhancementsState.ENABLED,
                identity,
                package,
                available,
                recovery_available=True,
            )
        if current == record.process_token:
            return NoteEnhancementsStatus(
                NoteEnhancementsState.DISABLE_PENDING_REBOOT,
                identity,
                package,
                available,
                "等待手动重启后停用笔记增强。",
                True,
            )
        return NoteEnhancementsStatus(
            NoteEnhancementsState.INSTALLED_DISABLED,
            identity,
            package,
            available,
            recovery_available=True,
        )
    except Exception as exc:
        return NoteEnhancementsStatus(
            NoteEnhancementsState.BROKEN,
            identity,
            package,
            available,
            str(exc),
            True,
        )


def install(
    ssh_client,
    package: NoteEnhancementsPackage,
    archive_path: str | Path,
) -> NoteEnhancementsStatus:
    identity = tap.get_device_identity(ssh_client)
    if select_package((package,), identity) is None:
        raise RuntimeError("当前设备与笔记增强包不精确匹配，未执行修改。")
    if select_package(_trusted_catalog(), identity) != package:
        raise RuntimeError("笔记增强包与内置信任清单不一致，拒绝部署。")
    if tap._vellum_runtime_present(ssh_client) or _known_upstream_qmd(ssh_client):
        raise RuntimeError("检测到外部 Xovi/延迟刷新补丁，请先由原管理器卸载。")
    tap._preflight_device(ssh_client)
    runtime, trusted, legacies, feature = _trusted_context(identity, package)
    if any(shared.validate_legacy(ssh_client, item) for item in legacies):
        raise RuntimeError("检测到旧版独立 Xovi，请先完成阅读插件迁移。")
    installed_trusted = trusted
    if shared.has_shared_artifacts(ssh_client):
        inspection, installed_trusted, _selected = _inspect_shared(
            ssh_client, runtime, trusted, identity
        )
        record = inspection.states.get(FEATURE_ID)
        if record is not None and record.enabled and record.spec == feature:
            return get_status(ssh_client, (package,))
    with tempfile.TemporaryDirectory() as temporary:
        extracted = extract_verified_package(archive_path, package, temporary)
        shared.enable_shared(
            ssh_client,
            runtime,
            feature,
            extracted,
            installed_trusted,
            (),
        )
    return get_status(ssh_client, (package,))


def disable(
    ssh_client, catalog: Iterable[NoteEnhancementsPackage]
) -> NoteEnhancementsStatus:
    packages = tuple(catalog)
    status = get_status(ssh_client, packages)
    if status.state in (
        NoteEnhancementsState.NOT_INSTALLED,
        NoteEnhancementsState.INCOMPATIBLE,
    ):
        return status
    if status.state == NoteEnhancementsState.BROKEN:
        raise RuntimeError(status.detail or "笔记增强状态无法验证。")
    if status.package is None:
        raise RuntimeError("当前设备没有精确匹配的笔记增强包。")
    runtime, trusted, _legacies, feature = _trusted_context(
        status.identity, status.package
    )
    inspection, installed_trusted, _selected = _inspect_shared(
        ssh_client, runtime, trusted, status.identity
    )
    installed = inspection.states.get(FEATURE_ID)
    if installed is None:
        return get_status(ssh_client, packages)
    shared.disable_shared(
        ssh_client,
        runtime,
        FEATURE_ID,
        installed_trusted,
        replacement_spec=feature if installed.spec != feature else None,
    )
    return get_status(ssh_client, packages)


def cleanup_legacy(
    ssh_client, catalog: Iterable[NoteEnhancementsPackage]
) -> NoteEnhancementsStatus:
    status = get_status(ssh_client, tuple(catalog))
    if status.state != NoteEnhancementsState.REPAIR_AVAILABLE:
        raise RuntimeError("当前没有可验证的旧版笔记增强可清理。")
    raise RuntimeError("当前笔记增强版本不需要旧版清理。")


def migrate(
    ssh_client,
    package: NoteEnhancementsPackage,
    archive_path: str | Path,
) -> NoteEnhancementsStatus:
    return install(ssh_client, package, archive_path)
