"""Exact-target managed installation for tablet-hosted QR document reception."""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from functools import lru_cache
from pathlib import Path

import _package_download
import _plugin_packages as packages
import _rmkit_cn
import _tap_page_turn as tap
import _xovi_standalone as shared

FEATURE_ID = "qr-upload"
PACKAGE_REVISION = 1
REPO_URL = "https://github.com/pretenderlu/rmtool"
ASSET_RELEASE_URL = f"{REPO_URL}/releases/download/qr-upload-assets"
COS_URL = "https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/qr-upload"
REMOTE_BASE_URLS = (ASSET_RELEASE_URL, COS_URL)
MANIFEST_URLS = tuple(f"{base}/manifest.json" for base in REMOTE_BASE_URLS)
BUNDLED_MANIFEST = Path(__file__).with_name("qr-upload") / "manifest.json"
QMD_PAYLOAD_PATH = "exthome/qt-resource-rebuilder/qr-upload.qmd"
BRIDGE_PAYLOAD_PATH = "extensions.d/rmtool-qr-upload.so"
RECEIVER_PAYLOAD_PATH = "helpers/rmtool-qr-upload"
REQUIRED_PATHS = {*shared._COMMON_ARCHIVE_PATHS, QMD_PAYLOAD_PATH, BRIDGE_PAYLOAD_PATH, RECEIVER_PAYLOAD_PATH}
MAX_PACKAGE_BYTES = tap.MAX_PACKAGE_BYTES
MAX_UNPACKED_BYTES = tap.MAX_UNPACKED_BYTES
ALLOWED_TARGETS = {
    ("ferrari", "20261009073202", "aarch64", "9d6d844d793b6242345c7c25667451620c48b2cc28986a21049fa8d534042dd4"):
        ("3.29.0.153", "beta", True, False),
    ("tatsu", "20261009073202", "aarch64", "3735105b7e6b8a1d8ea42c9eabe6079c0bd1c99a546ab1a9fc7870263909ce39"):
        ("3.29.0.153", "beta", True, False),
    ("rm1", "20261009073202", "armv7l", "0c68cf7b049fb87929b5978494a1205bc558f239f77758821c67524133a3f54b"):
        ("3.29.0.153", "beta", True, False),
    ("rm2", "20261009073202", "armv7l", "53547bc30584b9057d5b05ada6c5fb2df3561c3a097c1c2b1aae527d5ddd2e26"):
        ("3.29.0.153", "beta", True, False),
    ("chiappa", "20260827113527", "aarch64", "5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4"):
        ("3.28.0.172", "stable", True, False),
    ("chiappa", "20261009073202", "aarch64", "0d4bfba6daf8f61138996af95b494d70da11654d4861a98bffe894a425b8caba"):
        ("3.29.0.153", "beta", True, False),
}


class QrUploadState(Enum):
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
class QrUploadPackage:
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
        return f"qr-upload:{self.platform}:{self.firmware}:{self.xochitl_sha256}"

    @property
    def download_urls(self):
        return self.urls

    @property
    def download_url(self):
        return self.urls[0]

    def file(self, path):
        return next(item for item in self.files if item.path == path)


@dataclass(frozen=True)
class QrUploadStatus:
    state: QrUploadState
    identity: tap.DeviceIdentity
    package: QrUploadPackage | None = None
    available_packages: tuple[QrUploadPackage, ...] = ()
    detail: str = ""
    recovery_available: bool = False
    cleanup_available: bool = False


def _expected_asset_name(platform, firmware, release):
    return f"rmtool-qr-upload-{platform}-{firmware}-{release}.tar.gz"


def parse_manifest(data: bytes, *, require_local_match=True):
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("扫码传输清单不是有效 JSON。") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1 or not isinstance(document.get("packages"), list):
        raise RuntimeError("扫码传输清单格式无效。")
    result, seen = [], set()
    for entry in document["packages"]:
        if not isinstance(entry, dict) or set(entry) != set(QrUploadPackage.__dataclass_fields__):
            raise RuntimeError("扫码传输包字段无效。")
        for key, pattern in (
            ("firmware", packages.FIRMWARE_RE), ("release_version", packages.VERSION_RE),
            ("platform", packages.PLATFORM_RE), ("architecture", packages.ARCH_RE),
            ("xochitl_sha256", packages.SHA256_RE), ("sha256", packages.SHA256_RE), ("asset", packages.ASSET_RE),
        ):
            packages.required_string(entry, key, pattern, "扫码传输")
        identity = tuple(entry[key] for key in ("platform", "firmware", "architecture", "xochitl_sha256"))
        validation = tuple(entry[key] for key in ("release_version", "channel", "offline_verified", "device_verified"))
        if (identity in seen or ALLOWED_TARGETS.get(identity) != validation
                or type(entry["offline_verified"]) is not bool or type(entry["device_verified"]) is not bool
                or type(entry["package_revision"]) is not int or entry["package_revision"] != PACKAGE_REVISION):
            raise RuntimeError("扫码传输设备或固件不在精确信任清单中。")
        if type(entry["size"]) is not int or not 0 < entry["size"] <= MAX_PACKAGE_BYTES:
            raise RuntimeError("扫码传输包大小无效。")
        if not isinstance(entry["files"], list):
            raise RuntimeError("扫码传输文件清单无效。")
        files = tuple(packages.parse_payload_file(item, "扫码传输") for item in entry["files"])
        if len(files) != len(REQUIRED_PATHS) or {item.path for item in files} != REQUIRED_PATHS or sum(item.size for item in files) > MAX_UNPACKED_BYTES:
            raise RuntimeError("扫码传输包不符合固定文件清单。")
        for item in files:
            if item.mode != (0o755 if item.path in ("qmd-tool", RECEIVER_PAYLOAD_PATH) else 0o644):
                raise RuntimeError("扫码传输文件权限无效。")
        asset = _expected_asset_name(entry["platform"], entry["firmware"], entry["release_version"])
        if entry["asset"] != asset or entry["urls"] != [f"{base}/{asset}" for base in REMOTE_BASE_URLS]:
            raise RuntimeError("扫码传输下载来源无效。")
        result.append(QrUploadPackage(**{**entry, "files": files, "urls": tuple(entry["urls"])}))
        seen.add(identity)
    if seen != set(ALLOWED_TARGETS):
        raise RuntimeError("扫码传输清单缺少已支持的目标。")
    result = tuple(result)
    if require_local_match and result != _trusted_catalog():
        raise RuntimeError("扫码传输资源与内置信任清单不一致。")
    return result


@lru_cache(maxsize=1)
def _trusted_catalog():
    return parse_manifest(BUNDLED_MANIFEST.read_bytes(), require_local_match=False)


def load_catalog(state_dir, *, refresh=True):
    return packages.load_catalog(
        manifest_urls=MANIFEST_URLS, cache_path=packages.cache_dir(state_dir, FEATURE_ID) / "manifest.json",
        bundled_path=BUNDLED_MANIFEST, parse=parse_manifest, max_manifest_bytes=tap.MAX_MANIFEST_BYTES,
        refresh=refresh, log_label="QR upload", error_message="没有可用的扫码传输清单。",
    )


def select_package(catalog, identity):
    return packages.select_by_identity(catalog, identity)


def load_local_package(package, source_path, state_dir):
    return packages.load_local_package(package, source_path,
        packages.package_cache_path(state_dir, FEATURE_ID, package), "扫码传输")


def download_package(package, state_dir):
    return _package_download.download_verified_package(
        package, packages.package_cache_path(state_dir, FEATURE_ID, package), MAX_PACKAGE_BYTES,
        feature_label="扫码传输", mismatch_message="扫码传输包校验失败。", log_label=FEATURE_ID,
        store=lambda source: load_local_package(package, source, state_dir),
    )


def extract_verified_package(archive_path, package, destination):
    return packages.extract_verified_package(archive_path, package, destination, "扫码传输")


def _shared_specs(package):
    runtime, feature = shared.specs_from_package(package, FEATURE_ID, QMD_PAYLOAD_PATH,
        (BRIDGE_PAYLOAD_PATH, RECEIVER_PAYLOAD_PATH))
    return runtime, replace(feature, strict_metadata_paths=(BRIDGE_PAYLOAD_PATH, RECEIVER_PAYLOAD_PATH))


def _context(identity, package):
    runtime, trusted, legacies = tap._trusted_shared_context(identity)
    own_runtime, feature = _shared_specs(package)
    if runtime != own_runtime or trusted.get(FEATURE_ID) != feature:
        raise RuntimeError("扫码传输与共享运行资源不一致。")
    return runtime, trusted, legacies, feature


def _inspect(ssh, identity, runtime, trusted, *, check_lower=False):
    return shared.inspect_shared_revisions(ssh, runtime, trusted,
        tap._reading_enhancement_revisions(identity, trusted), check_lower=check_lower)


def _assert_stopped(ssh):
    for path in ("/run/rmtool-qr-upload-ui", "/home/root/.rmtool-qr-upload-web-state",
                 "/run/rmtool-native-import-control/active", "/home/root/.rmtool-native-import-web-state"):
        if shared._remote_entry_exists(ssh, path):
            raise RuntimeError("请先停止接收和电脑文件上传后再重试；异常退出后请重新打开扫码传输页面或重启设备恢复设置。")


def get_status(ssh_client, catalog):
    catalog = tuple(catalog)
    identity = tap.get_device_identity(ssh_client)
    package = select_package(catalog, identity)
    if package is None:
        return QrUploadStatus(QrUploadState.INCOMPATIBLE, identity,
            detail="扫码传输支持五款设备的 3.29.0.153，以及 Paper Pro Move 3.28.0.172。")
    try:
        runtime, trusted, _legacies, feature = _context(identity, package)
        if not shared.has_shared_artifacts(ssh_client):
            return QrUploadStatus(QrUploadState.NOT_INSTALLED, identity, package, catalog)
        inspection, _installed, _selected = _inspect(ssh_client, identity, runtime, trusted)
        record = inspection.states.get(FEATURE_ID)
        if record is None:
            return QrUploadStatus(QrUploadState.NOT_INSTALLED, identity, package, catalog)
        if record.spec != feature or inspection.launcher_update_available:
            return QrUploadStatus(QrUploadState.REPAIR_AVAILABLE, identity, package, catalog,
                detail="已验证的扫码传输安装可更新。", recovery_available=True)
        process = tap._xochitl_process_token(ssh_client)
        if record.enabled:
            shared.assert_startup_guard_not_latched(inspection)
            state = QrUploadState.ENABLE_PENDING_REBOOT if process == record.process_token else QrUploadState.ENABLED
            if state is QrUploadState.ENABLED and not inspection.active:
                raise RuntimeError("扫码传输尚未在当前阅读进程中载入。")
        else:
            state = QrUploadState.DISABLE_PENDING_REBOOT if process == record.process_token else QrUploadState.INSTALLED_DISABLED
        return QrUploadStatus(state, identity, package, catalog, recovery_available=True)
    except Exception as exc:
        return QrUploadStatus(QrUploadState.BROKEN, identity, package, catalog, detail=str(exc), recovery_available=True)


def preflight(ssh_client, package, *, with_plan=False):
    identity = tap.get_device_identity(ssh_client)
    if select_package(_trusted_catalog(), identity) != package:
        raise RuntimeError("设备与扫码传输包不精确匹配，未执行修改。")
    _assert_stopped(ssh_client)
    managed = shared.has_shared_artifacts(ssh_client)
    if not managed and tap._vellum_runtime_present(ssh_client):
        raise RuntimeError(tap._vellum_conflict_detail(ssh_client))
    tap._preflight_device(ssh_client)
    if not _rmkit_cn.has_localization_cjk_font(ssh_client):
        raise RuntimeError(
            "扫码传输页面需要简体中文字体，设备当前没有可用字体，未安装插件。"
            "请先通过“原生简体中文”准备中文字体（RM1 / RM2 可使用内置兜底字体），"
            "或在“字体管理”中设置支持中文的系统字体，再重试。"
            "不使用密码锁屏时可选择仅保存在 home，不占用 data 字体空间。"
        )
    runtime, trusted, legacies, feature = _context(identity, package)
    if any(shared.validate_legacy(ssh_client, item, check_lower=False) for item in legacies):
        raise RuntimeError("请先通过设备维护完成旧插件迁移。")
    inspection = None
    if managed:
        inspection, trusted, _selected = _inspect(
            ssh_client, identity, runtime, trusted, check_lower=False
        )
        # Inactive external files stay untouched; active competing loaders do not.
        import _plugin_recovery
        _plugin_recovery._external_loaders(ssh_client, inspection.layout)
    enable_plan = None
    if runtime.platform in {"rm1", "rm2"}:
        enable_plan = shared.check_shared_enable_capacity(
            ssh_client, runtime, feature, trusted, (), inspection=inspection
        )
    if with_plan:
        return runtime, trusted, feature, enable_plan
    return runtime, trusted, feature


def install(ssh_client, package, archive_path):
    runtime, trusted, feature = preflight(ssh_client, package)
    with tempfile.TemporaryDirectory() as temporary:
        extracted = extract_verified_package(archive_path, package, temporary)
        shared.enable_shared(ssh_client, runtime, feature, extracted, trusted, ())
    return get_status(ssh_client, (package,))


def install_cloud(ssh_client, package, state_dir):
    preflight(ssh_client, package)
    archive = download_package(package, state_dir)
    return install(ssh_client, package, archive)


def _change(ssh_client, catalog, *, remove):
    catalog = tuple(catalog)
    status = get_status(ssh_client, catalog)
    if status.state in (QrUploadState.NOT_INSTALLED, QrUploadState.INCOMPATIBLE):
        return status
    if status.state is QrUploadState.BROKEN or status.package is None:
        raise RuntimeError(status.detail or "扫码传输安装无法验证，未执行修改。")
    _assert_stopped(ssh_client)
    runtime, trusted, _legacies, feature = _context(status.identity, status.package)
    inspection, trusted, _selected = _inspect(ssh_client, status.identity, runtime, trusted, check_lower=True)
    if FEATURE_ID in inspection.states:
        if remove:
            shared.remove_shared_features(ssh_client, runtime, trusted, (FEATURE_ID,))
        else:
            shared.disable_shared(ssh_client, runtime, FEATURE_ID, trusted,
                replacement_spec=feature if inspection.states[FEATURE_ID].spec != feature else None)
    return get_status(ssh_client, catalog)


def disable(ssh_client, catalog):
    return _change(ssh_client, catalog, remove=False)


def uninstall(ssh_client, catalog):
    return _change(ssh_client, catalog, remove=True)


def cleanup_legacy(ssh_client, catalog):
    raise RuntimeError("扫码传输没有需要清理的历史发布版本。")


def migrate(ssh_client, package, archive_path):
    return install(ssh_client, package, archive_path)
