"""Exact-firmware backend for the device-side WeRead launcher."""

from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

import _package_download
import _tap_page_turn as tap
import _weread_app
import _xovi_standalone as shared


REPO_URL = "https://github.com/pretenderlu/rmtool"
ASSET_RELEASE_URL = f"{REPO_URL}/releases/download/weread-launcher-assets"
COS_URL = (
    "https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/"
    "weread-launcher"
)
REMOTE_BASE_URLS = (ASSET_RELEASE_URL, COS_URL)
MANIFEST_URLS = tuple(f"{base}/manifest.json" for base in REMOTE_BASE_URLS)
BUNDLED_MANIFEST = Path(__file__).with_name("weread-launcher") / "manifest.json"

FEATURE_ID = "weread-launcher"
PACKAGE_REVISION = 5
QMD_PAYLOAD_PATH = "exthome/qt-resource-rebuilder/weread-launcher.qmd"
BRIDGE_PAYLOAD_PATH = "extensions.d/rmtool-weread-launcher.so"
SHIM_PAYLOAD_PATH = "helpers/rmtool-weread-fast.so"
_PAYLOAD_PATHS = {
    *shared._COMMON_ARCHIVE_PATHS,
    QMD_PAYLOAD_PATH,
    BRIDGE_PAYLOAD_PATH,
    SHIM_PAYLOAD_PATH,
}
MAX_MANIFEST_BYTES = tap.MAX_MANIFEST_BYTES
MAX_PACKAGE_BYTES = tap.MAX_PACKAGE_BYTES
MAX_UNPACKED_BYTES = tap.MAX_UNPACKED_BYTES
_ASSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz")

OFFICIAL_FILES = _weread_app.OFFICIAL_FILES

ALLOWED_TARGETS = {
    ("ferrari", "20260827113527", "aarch64", "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021"):
        ("3.28.0.172", "stable", True, False),
    ("chiappa", "20260827113527", "aarch64", "5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4"):
        ("3.28.0.172", "stable", True, True),
}

_REVISION_2_FEATURES = {
    "chiappa": {
        "package_id": (
            "weread-launcher:chiappa:20260827113527:"
            "fa22877280a497336e0bbaf655dfb6daa07fc50ecc9718d93b049a445fa6e3ec"
        ),
        "qmd_sha256": "140e6c4758991c34ba9adc1d6db4fe410cdcd66cb349fcfae4bb9a99295ae875",
        "qmd_size": 12184,
    },
    "ferrari": {
        "package_id": (
            "weread-launcher:ferrari:20260827113527:"
            "58fd8f60b1c69181d65b0e23c9858f90ec87fd0ca7396561639d71d97a818b7b"
        ),
        "qmd_sha256": "140e6c4758991c34ba9adc1d6db4fe410cdcd66cb349fcfae4bb9a99295ae875",
        "qmd_size": 12184,
    },
}
_REVISION_2_EXTRA_FILES = {
    BRIDGE_PAYLOAD_PATH: (
        "4c9a14047002b66634e2587c2472fe06f7d5cb341cefb2104ccd21c013b6a564",
        1924384,
    ),
    SHIM_PAYLOAD_PATH: (
        "c6341d7d73dfd20a896485c74be36f246307ed4affaa7a32c946db737c503a80",
        39968,
    ),
}
_REVISION_3_FEATURES = {
    platform: {
        "package_id": record["package_id"],
        "qmd_sha256": "9bc9f761a2a15c10547912c48b7175c19ed17ab00463e0c6e926633c550dccc7",
        "qmd_size": 19360,
    }
    for platform, record in _REVISION_2_FEATURES.items()
}
_REVISION_3_EXTRA_FILES = {
    BRIDGE_PAYLOAD_PATH: (
        "05a2db2647bbcdff6d2729cfcf68835528cc698407b90dc56672129c770f147b",
        1925400,
    ),
    SHIM_PAYLOAD_PATH: (
        "5973c96c3f3663a458100198ce7998ad2c08a57cdc17b5e2a85962765d380bb1",
        209952,
    ),
}
_REVISION_4_FEATURES = {
    platform: {
        "package_id": record["package_id"],
        "qmd_sha256": "f0f045f21466aadaa79cd044a50f43d16eea0b63d8ccaed99c8ee4e0b019b59d",
        "qmd_size": 19498,
    }
    for platform, record in _REVISION_2_FEATURES.items()
}
_REVISION_4_EXTRA_FILES = {
    BRIDGE_PAYLOAD_PATH: (
        "0d0a834f359eed7aad758670024391606e5613f5e42ab2f758bb7bbc058efe70",
        1925400,
    ),
    SHIM_PAYLOAD_PATH: (
        "a01fdfc9fabcc89602861d012592addcb98571874484cf62de9b4f1af2d66044",
        209888,
    ),
}


class WeReadLauncherState(Enum):
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
class WeReadLauncherPackage:
    firmware: str
    release_version: str
    channel: str
    platform: str
    architecture: str
    xochitl_sha256: str
    asset: str
    sha256: str
    size: int
    files: tuple[tap.PayloadFile, ...]
    urls: tuple[str, ...]
    package_revision: int
    offline_verified: bool
    device_verified: bool

    @property
    def package_id(self) -> str:
        return _REVISION_2_FEATURES[self.platform]["package_id"]

    @property
    def download_url(self) -> str:
        return self.urls[0]

    @property
    def download_urls(self) -> tuple[str, ...]:
        return self.urls

    def file(self, path: str) -> tap.PayloadFile:
        for item in self.files:
            if item.path == path:
                return item
        raise KeyError(path)


@dataclass(frozen=True)
class WeReadLauncherStatus:
    state: WeReadLauncherState
    identity: tap.DeviceIdentity
    package: Optional[WeReadLauncherPackage] = None
    available_packages: tuple[WeReadLauncherPackage, ...] = ()
    detail: str = ""
    recovery_available: bool = False
    cleanup_available: bool = False
    prerequisite_available: bool = True


def _expected_asset_name(platform: str, firmware: str, release: str) -> str:
    return f"rmtool-weread-launcher-{platform}-{firmware}-{release}.tar.gz"


def parse_manifest(data: bytes, *, require_local_match: bool = True):
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("微信读书启动器清单不是有效 JSON。") from exc
    if not isinstance(document, dict):
        raise RuntimeError("微信读书启动器清单版本不受支持。")
    entries = document.get("packages")
    if document.get("schema_version") != 1 or not isinstance(entries, list):
        raise RuntimeError("微信读书启动器清单版本不受支持。")
    packages = []
    identities = set()
    assets = set()
    required_keys = {
        "firmware", "release_version", "channel", "platform", "architecture",
        "xochitl_sha256", "offline_verified", "device_verified",
        "package_revision", "asset", "sha256", "size", "urls", "files",
    }
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != required_keys:
            raise RuntimeError("微信读书启动器清单包格式无效。")
        firmware = entry.get("firmware")
        release = entry.get("release_version")
        platform = entry.get("platform")
        architecture = entry.get("architecture")
        xochitl_sha = entry.get("xochitl_sha256")
        asset = entry.get("asset")
        digest = entry.get("sha256")
        for value, pattern in (
            (firmware, tap._FIRMWARE_RE), (release, tap._VERSION_RE),
            (platform, tap._PLATFORM_RE), (architecture, tap._ARCH_RE),
            (xochitl_sha, tap._SHA256_RE), (digest, tap._SHA256_RE),
            (asset, _ASSET_RE),
        ):
            if not isinstance(value, str) or not pattern.fullmatch(value):
                raise RuntimeError("微信读书启动器清单字段无效。")
        expected = ALLOWED_TARGETS.get((platform, firmware, architecture, xochitl_sha))
        offline_verified = entry.get("offline_verified")
        device_verified = entry.get("device_verified")
        if type(offline_verified) is not bool or type(device_verified) is not bool:
            raise RuntimeError("微信读书启动器验证字段无效。")
        observed = (release, entry.get("channel"), offline_verified, device_verified)
        if expected != observed or entry.get("package_revision") != PACKAGE_REVISION:
            raise RuntimeError("微信读书启动器清单身份不在本地信任清单中。")
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_PACKAGE_BYTES:
            raise RuntimeError("微信读书启动器资源包大小无效。")
        file_entries = entry.get("files")
        if not isinstance(file_entries, list):
            raise RuntimeError("微信读书启动器文件清单格式无效。")
        files = tuple(tap._parse_payload_file(item) for item in file_entries)
        if {item.path for item in files} != _PAYLOAD_PATHS or len(files) != len(_PAYLOAD_PATHS):
            raise RuntimeError("微信读书启动器文件清单与固定白名单不匹配。")
        if sum(item.size for item in files) > MAX_UNPACKED_BYTES:
            raise RuntimeError("微信读书启动器资源包解压后过大。")
        if asset != _expected_asset_name(platform, firmware, release):
            raise RuntimeError("微信读书启动器资源包文件名无效。")
        urls = entry.get("urls")
        if not isinstance(urls, list) or set(urls) != {
            f"{base}/{asset}" for base in REMOTE_BASE_URLS
        } or len(urls) != 2:
            raise RuntimeError("微信读书启动器下载源无效。")
        identity = (platform, firmware, xochitl_sha)
        if identity in identities or asset in assets:
            raise RuntimeError("微信读书启动器清单包含重复包。")
        identities.add(identity)
        assets.add(asset)
        packages.append(WeReadLauncherPackage(
            firmware, release, entry["channel"], platform, architecture,
            xochitl_sha, asset, digest, size, files, tuple(urls), PACKAGE_REVISION,
            entry["offline_verified"], entry["device_verified"],
        ))
    result = tuple(packages)
    if require_local_match and result != _trusted_catalog():
        raise RuntimeError("微信读书启动器清单与本地信任清单不一致。")
    return result


@lru_cache(maxsize=1)
def _trusted_catalog():
    if not BUNDLED_MANIFEST.is_file():
        raise RuntimeError("缺少内置微信读书启动器信任清单。")
    return parse_manifest(BUNDLED_MANIFEST.read_bytes(), require_local_match=False)


def _cache_dir(state_dir: str) -> Path:
    return Path(state_dir) / "cache" / FEATURE_ID


def load_catalog(state_dir: str, *, refresh: bool = True):
    cache = _cache_dir(state_dir) / "manifest.json"
    if refresh:
        for url in MANIFEST_URLS:
            try:
                data = tap._download_limited(url, MAX_MANIFEST_BYTES)
                result = parse_manifest(data)
                tap._write_atomic(cache, data)
                return result
            except Exception as exc:
                logging.warning("Could not load WeRead launcher manifest from %s: %s", url, exc)
    for candidate in (cache, BUNDLED_MANIFEST):
        if candidate.is_file():
            try:
                return parse_manifest(candidate.read_bytes())
            except Exception as exc:
                logging.warning("WeRead launcher manifest is invalid (%s): %s", candidate, exc)
    raise RuntimeError("无法获取微信读书启动器清单，且没有可用内置清单。")


def download_package(package, state_dir: str) -> Path:
    return _package_download.download_verified_package(
        package, _cache_dir(state_dir) / package.firmware / package.asset,
        MAX_PACKAGE_BYTES, feature_label="微信读书启动器",
        mismatch_message="微信读书启动器资源包与清单校验不匹配。",
        log_label=FEATURE_ID,
        store=lambda source: load_local_package(package, source, state_dir),
    )


def load_local_package(package, source_path, state_dir: str) -> Path:
    data = Path(source_path).read_bytes()
    _package_download.verify_local_package(data, package.size, package.sha256, "微信读书启动器")
    destination = _cache_dir(state_dir) / package.firmware / package.asset
    tap._write_atomic(destination, data)
    return destination


def select_package(catalog: Iterable[WeReadLauncherPackage], identity):
    return next((item for item in catalog if (
        item.firmware, item.platform, item.architecture, item.xochitl_sha256
    ) == (
        identity.firmware, identity.platform, identity.architecture, identity.xochitl_sha256
    )), None)


def extract_verified_package(archive_path, package, destination):
    return tap.extract_verified_package(archive_path, package, destination)


def _shared_specs(package):
    runtime, feature = shared.specs_from_package(
        package,
        FEATURE_ID,
        QMD_PAYLOAD_PATH,
        (BRIDGE_PAYLOAD_PATH, SHIM_PAYLOAD_PATH),
    )
    return runtime, replace(
        feature,
        strict_metadata_paths=(BRIDGE_PAYLOAD_PATH, SHIM_PAYLOAD_PATH),
    )


def _trusted_context(identity, package):
    runtime, trusted, legacies = tap._trusted_shared_context(identity)
    peer_runtime, feature = _shared_specs(package)
    if peer_runtime != runtime:
        raise RuntimeError("微信读书启动器与共享 Xovi 运行资源不一致。")
    trusted = dict(trusted)
    trusted[FEATURE_ID] = feature
    shared.assert_feature_layout(runtime, trusted.values())
    return runtime, trusted, tuple(legacies), feature


def _known_shared_predecessor_specs(package, current):
    predecessors = []
    for revision, features, files in (
        (2, _REVISION_2_FEATURES, _REVISION_2_EXTRA_FILES),
        (3, _REVISION_3_FEATURES, _REVISION_3_EXTRA_FILES),
        (4, _REVISION_4_FEATURES, _REVISION_4_EXTRA_FILES),
    ):
        record = features.get(package.platform)
        if record is None:
            continue
        extra_files = tuple(
            replace(
                item,
                sha256=files[item.archive_path][0],
                size=files[item.archive_path][1],
            )
            for item in current.extra_files
        )
        predecessors.append((
            f"package-revision-{revision}",
            replace(
                current,
                sha256=record["qmd_sha256"],
                size=record["qmd_size"],
                extra_files=extra_files,
            ),
        ))
    return tuple(predecessors)


def _peer_revisions(identity, trusted):
    revisions = dict(tap._reading_enhancement_revisions(identity, trusted))
    package = select_package(_trusted_catalog(), identity)
    if package is not None and FEATURE_ID in trusted:
        revisions[FEATURE_ID] = _known_shared_predecessor_specs(
            package, trusted[FEATURE_ID]
        )
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


def _inspect_shared(ssh_client, runtime, trusted, identity, *, check_lower=False):
    return shared.inspect_shared_revisions(
        ssh_client,
        runtime,
        trusted,
        _peer_revisions(identity, trusted),
        check_lower=check_lower,
    )


def _official_install_error(ssh_client) -> str:
    for path, digest in OFFICIAL_FILES.items():
        if not ssh_client.file_exists(path):
            return "请先安装官方微信读书 v1.0.0"
        if shared._remote_sha256(ssh_client, path) != digest:
            return "已安装的微信读书版本与启动器不兼容"
    return ""


def get_status(ssh_client, catalog) -> WeReadLauncherStatus:
    packages = tuple(catalog)
    identity = tap.get_device_identity(ssh_client)
    available = tuple(item for item in packages if item.platform == identity.platform)
    package = select_package(packages, identity)
    if package is None:
        return WeReadLauncherStatus(
            WeReadLauncherState.INCOMPATIBLE, identity, available_packages=available,
            detail="当前设备身份不在微信读书启动器精确信任清单中。",
        )
    try:
        official_error = _official_install_error(ssh_client)
        runtime, trusted, _legacies, feature = _trusted_context(identity, package)
        if not shared.has_shared_artifacts(ssh_client):
            return WeReadLauncherStatus(
                WeReadLauncherState.NOT_INSTALLED, identity, package, available,
                official_error or "尚未安装设备端启动入口。",
                prerequisite_available=not official_error,
            )
        inspection, _installed_trusted, selected = _inspect_shared(
            ssh_client, runtime, trusted, identity
        )
        record = inspection.states.get(FEATURE_ID)
        if record is None:
            return WeReadLauncherStatus(
                WeReadLauncherState.NOT_INSTALLED, identity, package, available,
                official_error or "共享 Xovi 正由其他已验证功能使用。",
                recovery_available=True, prerequisite_available=not official_error,
            )
        if record.spec != feature:
            reason = selected.get(FEATURE_ID)
            predecessors = dict(_known_shared_predecessor_specs(package, feature))
            if reason == shared.MANAGED_RECEIPT_REASON:
                detail = "已验证为 rmtool 完成安装的旧版启动器，可安全更新。"
            elif reason in predecessors and record.spec == predecessors[reason]:
                detail = "旧版文件身份已精确验证，可安全更新。"
            else:
                raise RuntimeError("微信读书启动器状态与当前内置信任清单不一致。")
            return WeReadLauncherStatus(
                WeReadLauncherState.MIGRATION_AVAILABLE,
                identity,
                package,
                available,
                official_error or detail,
                recovery_available=True,
                prerequisite_available=not official_error,
            )
        current = tap._xochitl_process_token(ssh_client)
        if record.enabled:
            state = (WeReadLauncherState.ENABLE_PENDING_REBOOT
                     if current == record.process_token else WeReadLauncherState.ENABLED)
            if state is WeReadLauncherState.ENABLED and not inspection.active:
                raise RuntimeError("微信读书启动器未在当前 xochitl 进程中载入。")
        else:
            state = (WeReadLauncherState.DISABLE_PENDING_REBOOT
                     if current == record.process_token else WeReadLauncherState.INSTALLED_DISABLED)
        return WeReadLauncherStatus(
            state, identity, package, available, official_error,
            recovery_available=True, prerequisite_available=not official_error,
        )
    except Exception as exc:
        return WeReadLauncherStatus(
            WeReadLauncherState.BROKEN, identity, package, available, str(exc), True
        )


def install(ssh_client, package, archive_path) -> WeReadLauncherStatus:
    identity = tap.get_device_identity(ssh_client)
    if select_package((package,), identity) is None or select_package(_trusted_catalog(), identity) != package:
        raise RuntimeError("当前设备与微信读书启动器包不精确匹配，未执行修改。")
    error = _official_install_error(ssh_client)
    if error:
        raise RuntimeError(error + "。")
    if tap._vellum_runtime_present(ssh_client):
        raise RuntimeError("检测到 Vellum/AppLoader Xovi，请先由原管理器卸载。")
    tap._preflight_device(ssh_client)
    runtime, trusted, legacies, feature = _trusted_context(identity, package)
    if any(shared.validate_legacy(ssh_client, item) for item in legacies):
        raise RuntimeError("检测到旧版独立 Xovi，请先完成插件迁移。")
    installed_trusted = trusted
    if shared.has_shared_artifacts(ssh_client):
        inspection, installed_trusted, _selected = _inspect_shared(
            ssh_client, runtime, trusted, identity, check_lower=True
        )
        record = inspection.states.get(FEATURE_ID)
        if record is not None and record.enabled and record.spec == feature:
            return get_status(ssh_client, (package,))
    with tempfile.TemporaryDirectory() as temporary:
        extracted = extract_verified_package(archive_path, package, temporary)
        shared.enable_shared(ssh_client, runtime, feature, extracted, installed_trusted, ())
    return get_status(ssh_client, (package,))


def disable(ssh_client, catalog) -> WeReadLauncherStatus:
    packages = tuple(catalog)
    status = get_status(ssh_client, packages)
    if status.state in (WeReadLauncherState.NOT_INSTALLED, WeReadLauncherState.INCOMPATIBLE):
        return status
    if status.state is WeReadLauncherState.BROKEN or status.package is None:
        raise RuntimeError(status.detail or "微信读书启动器状态无法验证。")
    runtime, trusted, _legacies, feature = _trusted_context(status.identity, status.package)
    inspection, installed_trusted, _selected = _inspect_shared(
        ssh_client, runtime, trusted, status.identity, check_lower=True
    )
    if FEATURE_ID not in inspection.states:
        return get_status(ssh_client, packages)
    shared.disable_shared(ssh_client, runtime, FEATURE_ID, installed_trusted,
                          replacement_spec=feature if inspection.states[FEATURE_ID].spec != feature else None)
    return get_status(ssh_client, packages)


def cleanup_legacy(ssh_client, catalog):
    del ssh_client, catalog
    raise RuntimeError("微信读书启动器没有可清理的旧版。")


def migrate(ssh_client, package, archive_path):
    return install(ssh_client, package, archive_path)
