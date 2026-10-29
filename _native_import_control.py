"""Exact-target control component for desktop native document import."""
from __future__ import annotations

import json
import shlex
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from functools import lru_cache
from pathlib import Path

import _package_download
import _plugin_packages as packages
import _tap_page_turn as tap
import _xovi_standalone as shared

FEATURE_ID = "native-import"
PACKAGE_REVISION = 1
REMOTE_BASE_URLS = (
    "https://github.com/pretenderlu/rmtool/releases/download/native-import-assets",
    "https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/native-import",
)
MANIFEST_URLS = tuple(f"{base}/manifest.json" for base in REMOTE_BASE_URLS)
BUNDLED_MANIFEST = Path(__file__).with_name("native-import") / "manifest.json"
QMD_PAYLOAD_PATH = "exthome/qt-resource-rebuilder/native-import.qmd"
BRIDGE_PAYLOAD_PATH = "extensions.d/rmtool-native-import.so"
HELPER_PAYLOAD_PATH = "helpers/rmtool-native-import"
REQUIRED_PATHS = {*shared._COMMON_ARCHIVE_PATHS, QMD_PAYLOAD_PATH, BRIDGE_PAYLOAD_PATH, HELPER_PAYLOAD_PATH}
ALLOWED_TARGETS = {
    ("ferrari", "20261028104944", "aarch64", "35e940d8acb3de0dfaf61f1a421d450e61dbaa915f885c818a2a3cc5c94c8ecd"): ("3.29.1.0", "stable", True, False),
    ("chiappa", "20261028104944", "aarch64", "9a1391c4a2d26c1c364de078d37773f0464da296039097e88e475ecf807bb6b4"): ("3.29.1.0", "stable", True, False),
    ("ferrari", "20260827113527", "aarch64", "b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021"): ("3.28.0.172", "stable", True, False),
    ("chiappa", "20260827113527", "aarch64", "5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4"): ("3.28.0.172", "stable", True, False),
    ("tatsu", "20260827113527", "aarch64", "fa674d2ca3d8002602ce4b1b92280b96bcdf2cf32b6a42ec62ae178a1fad1fe3"): ("3.28.0.172", "stable", True, False),
    ("rm1", "20260827113527", "armv7l", "1f4fbb6e14650704b5b036e482da9948e73553178f6116ad464ab072f7b90117"): ("3.28.0.172", "stable", True, False),
    ("rm2", "20260827113527", "armv7l", "071d85beef3ef2d4cc0e11002140b27b82a2cc04a2ed740a5669f591069b77df"): ("3.28.0.172", "stable", True, False),
    ("chiappa", "20261009073202", "aarch64", "0d4bfba6daf8f61138996af95b494d70da11654d4861a98bffe894a425b8caba"): ("3.29.0.153", "beta", True, False),
    ("ferrari", "20261009073202", "aarch64", "9d6d844d793b6242345c7c25667451620c48b2cc28986a21049fa8d534042dd4"): ("3.29.0.153", "beta", True, False),
    ("tatsu", "20261009073202", "aarch64", "3735105b7e6b8a1d8ea42c9eabe6079c0bd1c99a546ab1a9fc7870263909ce39"): ("3.29.0.153", "beta", True, False),
    ("rm1", "20261009073202", "armv7l", "0c68cf7b049fb87929b5978494a1205bc558f239f77758821c67524133a3f54b"): ("3.29.0.153", "beta", True, False),
    ("rm2", "20261009073202", "armv7l", "53547bc30584b9057d5b05ada6c5fb2df3561c3a097c1c2b1aae527d5ddd2e26"): ("3.29.0.153", "beta", True, False),
}


class NativeImportState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    REPAIR_AVAILABLE = "repair_available"
    INSTALLED_DISABLED = "installed_disabled"
    ENABLE_PENDING_REBOOT = "enable_pending_reboot"
    ENABLED = "enabled"
    DISABLE_PENDING_REBOOT = "disable_pending_reboot"
    BROKEN = "broken"


@dataclass(frozen=True)
class NativeImportPackage:
    firmware: str
    release_version: str
    channel: str
    platform: str
    architecture: str
    xochitl_sha256: str
    asset: str
    sha256: str
    size: int
    files: tuple[packages.PayloadFile, ...]
    urls: tuple[str, ...]
    package_revision: int
    offline_verified: bool
    device_verified: bool

    @property
    def package_id(self):
        return f"native-import:{self.platform}:{self.firmware}:{self.xochitl_sha256}"

    @property
    def download_urls(self):
        return self.urls

    @property
    def download_url(self):
        return self.urls[0]

    def file(self, path):
        return next(item for item in self.files if item.path == path)


@dataclass(frozen=True)
class NativeImportStatus:
    state: NativeImportState
    identity: tap.DeviceIdentity
    package: NativeImportPackage | None = None
    available_packages: tuple[NativeImportPackage, ...] = ()
    detail: str = ""


def _expected_asset_name(platform, firmware, release):
    return f"rmtool-native-import-{platform}-{firmware}-{release}.tar.gz"


def parse_manifest(data, *, require_local_match=True):
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("原生传输清单不是有效 JSON。") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1 or not isinstance(document.get("packages"), list):
        raise RuntimeError("原生传输清单格式无效。")
    result, seen = [], set()
    for entry in document["packages"]:
        if not isinstance(entry, dict) or set(entry) != set(NativeImportPackage.__dataclass_fields__):
            raise RuntimeError("原生传输包字段无效。")
        for key, pattern in (
            ("firmware", packages.FIRMWARE_RE), ("release_version", packages.VERSION_RE),
            ("platform", packages.PLATFORM_RE), ("architecture", packages.ARCH_RE),
            ("xochitl_sha256", packages.SHA256_RE), ("sha256", packages.SHA256_RE), ("asset", packages.ASSET_RE),
        ):
            packages.required_string(entry, key, pattern, "原生传输")
        identity = tuple(entry[key] for key in ("platform", "firmware", "architecture", "xochitl_sha256"))
        verification = tuple(entry[key] for key in ("release_version", "channel", "offline_verified", "device_verified"))
        if (identity in seen or ALLOWED_TARGETS.get(identity) != verification
                or type(entry["offline_verified"]) is not bool or type(entry["device_verified"]) is not bool
                or type(entry["package_revision"]) is not int or entry["package_revision"] != PACKAGE_REVISION):
            raise RuntimeError("原生传输设备或固件不在精确信任清单中。")
        if type(entry["size"]) is not int or not 0 < entry["size"] <= tap.MAX_PACKAGE_BYTES:
            raise RuntimeError("原生传输包大小无效。")
        if not isinstance(entry["files"], list):
            raise RuntimeError("原生传输文件清单无效。")
        files = tuple(packages.parse_payload_file(item, "原生传输") for item in entry["files"])
        if (len(files) != len(REQUIRED_PATHS) or {item.path for item in files} != REQUIRED_PATHS
                or sum(item.size for item in files) > tap.MAX_UNPACKED_BYTES):
            raise RuntimeError("原生传输包不符合固定文件清单。")
        for item in files:
            if item.mode != (0o755 if item.path in ("qmd-tool", HELPER_PAYLOAD_PATH) else 0o644):
                raise RuntimeError("原生传输文件权限无效。")
        asset = _expected_asset_name(entry["platform"], entry["firmware"], entry["release_version"])
        if entry["asset"] != asset or entry["urls"] != [f"{base}/{asset}" for base in REMOTE_BASE_URLS]:
            raise RuntimeError("原生传输下载来源无效。")
        result.append(NativeImportPackage(**{**entry, "files": files, "urls": tuple(entry["urls"])}))
        seen.add(identity)
    if seen != set(ALLOWED_TARGETS):
        raise RuntimeError("原生传输清单缺少已支持的目标。")
    result = tuple(result)
    if require_local_match and result != _trusted_catalog():
        raise RuntimeError("原生传输资源与内置信任清单不一致。")
    return result


@lru_cache(maxsize=1)
def _trusted_catalog():
    return parse_manifest(BUNDLED_MANIFEST.read_bytes(), require_local_match=False)


def select_package(catalog, identity):
    return packages.select_by_identity(catalog, identity)


def load_catalog(state_dir, *, refresh=True):
    return packages.load_catalog(
        manifest_urls=MANIFEST_URLS, cache_path=packages.cache_dir(state_dir, FEATURE_ID) / "manifest.json",
        bundled_path=BUNDLED_MANIFEST, parse=parse_manifest, max_manifest_bytes=tap.MAX_MANIFEST_BYTES,
        refresh=refresh, log_label="Native import", error_message="没有可用的原生传输清单。",
    )


def load_local_package(package, source_path, state_dir):
    return packages.load_local_package(package, source_path,
        packages.package_cache_path(state_dir, FEATURE_ID, package), "原生传输")


def download_package(package, state_dir):
    return _package_download.download_verified_package(
        package, packages.package_cache_path(state_dir, FEATURE_ID, package), tap.MAX_PACKAGE_BYTES,
        feature_label="原生传输", mismatch_message="原生传输包校验失败。", log_label=FEATURE_ID,
        store=lambda source: load_local_package(package, source, state_dir),
    )


def extract_verified_package(archive_path, package, destination):
    return packages.extract_verified_package(archive_path, package, destination, "原生传输")


def _shared_specs(package):
    runtime, feature = shared.specs_from_package(package, FEATURE_ID, QMD_PAYLOAD_PATH,
        (BRIDGE_PAYLOAD_PATH, HELPER_PAYLOAD_PATH))
    return runtime, replace(feature, strict_metadata_paths=(BRIDGE_PAYLOAD_PATH, HELPER_PAYLOAD_PATH))


def _context(identity, package):
    runtime, trusted, legacies = tap._trusted_shared_context(identity)
    own_runtime, feature = _shared_specs(package)
    if runtime != own_runtime or trusted.get(FEATURE_ID) != feature:
        raise RuntimeError("原生传输与共享运行资源不一致。")
    return runtime, trusted, legacies, feature


def _inspect(ssh, identity, runtime, trusted, *, check_lower=False):
    return shared.inspect_shared_revisions(ssh, runtime, trusted,
        tap._reading_enhancement_revisions(identity, trusted), check_lower=check_lower)


def get_status(ssh_client, catalog):
    catalog = tuple(catalog)
    identity = tap.get_device_identity(ssh_client)
    package = select_package(catalog, identity)
    if package is None:
        return NativeImportStatus(NativeImportState.INCOMPATIBLE, identity,
            detail="自动传输支持五款设备的 3.28.0.172 正式版和 3.29.0.153 测试版；其他固件可手动开启原生 USB 网页界面。")
    try:
        runtime, trusted, _legacies, feature = _context(identity, package)
        if not shared.has_shared_artifacts(ssh_client):
            return NativeImportStatus(NativeImportState.NOT_INSTALLED, identity, package, catalog)
        inspection, _installed, _selected = _inspect(ssh_client, identity, runtime, trusted)
        record = inspection.states.get(FEATURE_ID)
        if record is None:
            return NativeImportStatus(NativeImportState.NOT_INSTALLED, identity, package, catalog)
        if record.spec != feature or inspection.launcher_update_available:
            return NativeImportStatus(NativeImportState.REPAIR_AVAILABLE, identity, package, catalog)
        pending = tap._xochitl_process_token(ssh_client) == record.process_token
        if record.enabled:
            shared.assert_startup_guard_not_latched(inspection)
            state = NativeImportState.ENABLE_PENDING_REBOOT if pending else NativeImportState.ENABLED
            if not pending and not inspection.active:
                raise RuntimeError("原生传输组件未在当前阅读进程中载入。")
        else:
            state = NativeImportState.DISABLE_PENDING_REBOOT if pending else NativeImportState.INSTALLED_DISABLED
        return NativeImportStatus(state, identity, package, catalog)
    except Exception as exc:
        return NativeImportStatus(NativeImportState.BROKEN, identity, package, catalog, str(exc))


def loaded_helper(ssh_client):
    """Reuse only an authenticated, loaded component; never trust a socket alone."""
    identity = tap.get_device_identity(ssh_client)
    if (identity.platform, identity.firmware, identity.architecture, identity.xochitl_sha256) not in ALLOWED_TARGETS:
        return None
    runtime, trusted, _legacies = tap._trusted_shared_context(identity)
    if not shared.has_shared_artifacts(ssh_client):
        return None
    inspection, _installed, _selected = _inspect(ssh_client, identity, runtime, trusted)
    shared.assert_startup_guard_not_latched(inspection)
    if not inspection.active:
        return None
    process = tap._xochitl_process_token(ssh_client)
    for feature_id, helper in ((FEATURE_ID, HELPER_PAYLOAD_PATH), ("qr-upload", "helpers/rmtool-qr-upload")):
        record = inspection.states.get(feature_id)
        if record is not None and record.enabled and process != record.process_token:
            if any(item.runtime_path == helper for item in record.spec.files):
                return f"{inspection.layout.remote_base}/{helper}"
    return None


def _assert_stopped(ssh):
    for path in ("/run/rmtool-qr-upload-ui", "/home/root/.rmtool-qr-upload-web-state",
                 "/run/rmtool-native-import-control/active", "/home/root/.rmtool-native-import-web-state"):
        if shared._remote_entry_exists(ssh, path):
            raise RuntimeError("请先停止扫码接收和电脑上传；异常退出后请重启设备恢复设置。")


def install(ssh_client, package, archive_path):
    identity = tap.get_device_identity(ssh_client)
    if select_package(_trusted_catalog(), identity) != package:
        raise RuntimeError("设备与原生传输包不精确匹配，未执行修改。")
    _assert_stopped(ssh_client)
    if tap._vellum_runtime_present(ssh_client):
        raise RuntimeError(tap._vellum_conflict_detail(ssh_client))
    tap._preflight_device(ssh_client)
    runtime, trusted, legacies, feature = _context(identity, package)
    if any(shared.validate_legacy(ssh_client, item) for item in legacies):
        raise RuntimeError("请先通过设备维护完成旧插件迁移。")
    if shared.has_shared_artifacts(ssh_client):
        inspection, _installed, _selected = _inspect(ssh_client, identity, runtime, trusted, check_lower=True)
        qr = inspection.states.get("qr-upload")
        if qr is not None and qr.enabled:
            bridge = f"{inspection.layout.remote_base}/extensions.d/rmtool-qr-upload.so"
            try:
                ssh_client.exec_checked("LC_ALL=C grep -a -F -q '_ZN13ImportControl16staticMetaObjectE' " + shlex.quote(bridge))
            except RuntimeError as exc:
                raise RuntimeError("已有扫码传输组件缺少共享控制能力或无法验证，请先更新扫码传输后再初始化原生传输。") from exc
    with tempfile.TemporaryDirectory() as temporary:
        extracted = extract_verified_package(archive_path, package, temporary)
        shared.enable_shared(ssh_client, runtime, feature, extracted, trusted, ())
    status = get_status(ssh_client, (package,))
    if status.state not in (NativeImportState.ENABLED, NativeImportState.ENABLE_PENDING_REBOOT):
        raise RuntimeError(status.detail or "原生传输部署后的校验未通过，请保留诊断信息，不要直接重启设备。")
    return status


def initialize(ssh_client, state_dir):
    status = get_status(ssh_client, _trusted_catalog())
    if status.state in (NativeImportState.INCOMPATIBLE, NativeImportState.BROKEN):
        raise RuntimeError(status.detail)
    if status.state in (NativeImportState.ENABLED, NativeImportState.ENABLE_PENDING_REBOOT):
        return status
    archive = download_package(status.package, state_dir)
    return install(ssh_client, status.package, archive)


def uninstall(ssh_client, catalog):
    status = get_status(ssh_client, catalog)
    if status.state in (NativeImportState.NOT_INSTALLED, NativeImportState.INCOMPATIBLE):
        return replace(status, detail=status.detail or "设备未安装独立的原生传输组件，无需移除。")
    if status.state is NativeImportState.BROKEN:
        raise RuntimeError(status.detail)
    _assert_stopped(ssh_client)
    runtime, trusted, _legacies, _feature = _context(status.identity, status.package)
    _inspect(ssh_client, status.identity, runtime, trusted, check_lower=True)
    shared.remove_shared_features(ssh_client, runtime, trusted, (FEATURE_ID,))
    status = get_status(ssh_client, catalog)
    if status.state is not NativeImportState.NOT_INSTALLED:
        raise RuntimeError(status.detail or "原生传输组件移除后的状态未确认，请导出诊断日志。")
    return status


def migrate(ssh_client, package, archive_path):
    return install(ssh_client, package, archive_path)
