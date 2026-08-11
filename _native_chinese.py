"""Exact-build native Simplified Chinese support for reMarkable firmware."""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

import _rmkit_cn
import _tap_page_turn as tap
import _xovi_standalone


REPO_URL = "https://github.com/pretenderlu/rmtool"
COS_URL = (
    "https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/"
    "native-chinese"
)
GITHUB_URL = f"{REPO_URL}/releases/download/native-chinese-assets"
BUNDLED_MANIFEST = Path(__file__).resolve().parent / "native-chinese" / "manifest.json"
FEATURE_ID = "native-chinese"
QMD_PATH = "exthome/qt-resource-rebuilder/native-chinese.qmd"
EXTENSION_PATH = "extensions.d/native-chinese-translator.so"
CATALOG_PATH = "native-chinese/reMarkable_zh_CN.qm"
PAYLOAD_PATHS = {
    "xovi.so",
    "extensions.d/qt-resource-rebuilder.so",
    "exthome/qt-resource-rebuilder/hashtab",
    "qmd-tool",
    QMD_PATH,
    EXTENSION_PATH,
    CATALOG_PATH,
}
FERRARI_166_IDENTITY = (
    "20260806095513",
    "ferrari",
    "aarch64",
    "8726b4fce55a9154a5014956e5204401ce881d752c1ff3813adb622a68aac2f9",
)
CHIAPPA_3273_IDENTITY = (
    "20260612085811",
    "chiappa",
    "aarch64",
    "227a9bfe928ef5d164359e490d97648ffca40a5de13f07a9eb57a618a403f084",
)
# Kept for callers/tests that use the first device-verified target as a fixture.
SUPPORTED_IDENTITY = FERRARI_166_IDENTITY
ALLOWED_TARGETS = {
    FERRARI_166_IDENTITY: ("3.28.0.166", "beta", True, True),
    CHIAPPA_3273_IDENTITY: ("3.27.3.0", "stable", True, True),
    (
        "20260506100933",
        "chiappa",
        "aarch64",
        "4646e0aef1cef2b3417889073ad5faba9259ae6b41f68326e75ef9a5c520c322",
    ): ("3.27.1.0", "stable", True, False),
    (
        "20260506100933",
        "ferrari",
        "aarch64",
        "29b9896b07f59636d910d8a740f6562c502f676a1a70f8814459229d25cc5288",
    ): ("3.27.1.0", "stable", True, False),
    (
        "20260612085811",
        "ferrari",
        "aarch64",
        "9749880daa2f10844e77b560ec0ecddd1634d43eb328af637c7026edf3ef120e",
    ): ("3.27.3.0", "stable", True, False),
    (
        "20260629074044",
        "chiappa",
        "aarch64",
        "9e3e0372a15da25b148ac17667feb566014440e079c3e3ee504112d556ad2e10",
    ): ("3.28.0.162", "beta", True, False),
    (
        "20260629074044",
        "ferrari",
        "aarch64",
        "10082aeb857c69c3f404ab189d7403318ba97d0c169e756ae9a5b3532b248a4a",
    ): ("3.28.0.162", "beta", True, False),
    (
        "20260702125656",
        "chiappa",
        "aarch64",
        "08171df6296b99d04b3694b337bd0ce911e6a93356955961a37de9dd93a0394d",
    ): ("3.28.0.163", "beta", True, False),
    (
        "20260702125656",
        "ferrari",
        "aarch64",
        "49f60572e830f6c4f20d800a56d644cdf53cd65a8e240b2b27106cce55040f89",
    ): ("3.28.0.163", "beta", True, False),
    (
        "20260702125656",
        "chiappa",
        "aarch64",
        "3a9e18483b73f43016fb25b451e3ece0efba7aa1cc92e080771e138ce6bbca98",
    ): ("3.28.0.164", "beta", True, False),
    (
        "20260702125656",
        "ferrari",
        "aarch64",
        "113bf7ea62ad171ea03c77c1f90e0666bcff163242a22ebca84372533b270c1c",
    ): ("3.28.0.164", "beta", True, False),
}
EXPECTED_ASSETS = {
    identity: (
        f"rmtool-native-chinese-{identity[1]}-{identity[0]}-{policy[0]}.tar.gz"
    )
    for identity, policy in ALLOWED_TARGETS.items()
}
MAX_PACKAGE_BYTES = 32 * 1024 * 1024


class NativeChineseState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    INSTALLED_DISABLED = "installed_disabled"
    ENABLE_PENDING_REBOOT = "enable_pending_reboot"
    ENABLED = "enabled"
    DISABLE_PENDING_REBOOT = "disable_pending_reboot"
    EMERGENCY_DISABLED = "emergency_disabled"
    FIRMWARE_RESIDUE = "firmware_residue"
    BROKEN = "broken"


@dataclass(frozen=True)
class NativeChinesePackage:
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
    urls: tuple[str, str]
    offline_verified: bool
    device_verified: bool

    @property
    def package_id(self) -> str:
        return f"{self.platform}-{self.firmware}-{self.xochitl_sha256[:12]}"

    def file(self, path: str) -> tap.PayloadFile:
        for item in self.files:
            if item.path == path:
                return item
        raise KeyError(path)


@dataclass(frozen=True)
class NativeChineseStatus:
    state: NativeChineseState
    identity: tap.DeviceIdentity
    package: Optional[NativeChinesePackage] = None
    detail: str = ""
    installed: bool = False
    emergency_disabled: bool = False


def parse_manifest(data: bytes) -> tuple[NativeChinesePackage, ...]:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("原生中文清单不是有效 JSON。") from exc
    entries = document.get("packages") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "packages"}
        or document.get("schema_version") != 1
        or not isinstance(entries, list)
    ):
        raise RuntimeError("原生中文清单结构无效。")

    result = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "firmware",
            "release_version",
            "channel",
            "platform",
            "architecture",
            "xochitl_sha256",
            "asset",
            "sha256",
            "size",
            "urls",
            "files",
            "offline_verified",
            "device_verified",
        }:
            raise RuntimeError("原生中文包格式无效。")
        firmware = tap._required_string(entry, "firmware", tap._FIRMWARE_RE)
        release_version = tap._required_string(
            entry, "release_version", tap._VERSION_RE
        )
        platform = tap._required_string(entry, "platform", tap._PLATFORM_RE)
        architecture = tap._required_string(entry, "architecture", tap._ARCH_RE)
        xochitl_sha256 = tap._required_string(
            entry, "xochitl_sha256", tap._SHA256_RE
        )
        asset = tap._required_string(entry, "asset", tap._ASSET_RE)
        digest = tap._required_string(entry, "sha256", tap._SHA256_RE)
        channel = entry.get("channel")
        offline_verified = entry.get("offline_verified")
        device_verified = entry.get("device_verified")
        size = entry.get("size")
        raw_files = entry.get("files")
        if (
            channel not in {"stable", "beta"}
            or type(offline_verified) is not bool
            or type(device_verified) is not bool
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 < size <= MAX_PACKAGE_BYTES
        ):
            raise RuntimeError("原生中文包的发布类型或大小无效。")
        if not isinstance(raw_files, list):
            raise RuntimeError("原生中文包缺少文件清单。")
        files = tuple(tap._parse_payload_file(item) for item in raw_files)
        if len({item.path for item in files}) != len(files):
            raise RuntimeError("原生中文包包含重复路径。")
        if set(item.path for item in files) != PAYLOAD_PATHS:
            raise RuntimeError("原生中文包的文件白名单无效。")
        identity = (
            firmware,
            platform,
            architecture,
            xochitl_sha256,
        )
        expected = ALLOWED_TARGETS.get(identity)
        if expected != (
            release_version,
            channel,
            offline_verified,
            device_verified,
        ) or asset != EXPECTED_ASSETS.get(identity):
            raise RuntimeError("原生中文包不在本地精确身份白名单中。")
        urls = entry.get("urls")
        expected_urls = [f"{COS_URL}/{asset}", f"{GITHUB_URL}/{asset}"]
        if urls != expected_urls:
            raise RuntimeError("原生中文包的下载镜像字段无效。")
        result.append(
            NativeChinesePackage(
                firmware,
                release_version,
                channel,
                platform,
                architecture,
                xochitl_sha256,
                asset,
                digest,
                size,
                files,
                tuple(urls),
                offline_verified,
                device_verified,
            )
        )
    identities = {
        (item.firmware, item.platform, item.architecture, item.xochitl_sha256)
        for item in result
    }
    if (
        len(result) != len(identities)
        or len({item.asset for item in result}) != len(result)
        or identities != set(ALLOWED_TARGETS)
    ):
        raise RuntimeError("原生中文清单必须完整且唯一地包含本地精确信任包。")
    return tuple(result)


@lru_cache(maxsize=1)
def _trusted_catalog() -> tuple[NativeChinesePackage, ...]:
    return parse_manifest(BUNDLED_MANIFEST.read_bytes())


def load_catalog(_state_dir: str) -> tuple[NativeChinesePackage, ...]:
    return _trusted_catalog()


def select_package(
    catalog: Iterable[NativeChinesePackage], identity: tap.DeviceIdentity
) -> Optional[NativeChinesePackage]:
    for package in catalog:
        if (
            package.firmware == identity.firmware
            and package.platform == identity.platform
            and package.architecture == identity.architecture
            and package.xochitl_sha256 == identity.xochitl_sha256
        ):
            return package
    return None


def _cache_path(state_dir: str, package: NativeChinesePackage) -> Path:
    return Path(state_dir) / "cache" / FEATURE_ID / package.firmware / package.asset


def download_package(package: NativeChinesePackage, state_dir: str) -> Path:
    destination = _cache_path(state_dir, package)
    if destination.is_file():
        data = destination.read_bytes()
        if len(data) == package.size and hashlib.sha256(data).hexdigest() == package.sha256:
            return destination
    last_error: Optional[Exception] = None
    for url in package.urls:
        try:
            data = tap._download_limited(url, MAX_PACKAGE_BYTES)
            if len(data) != package.size or hashlib.sha256(data).hexdigest() != package.sha256:
                raise RuntimeError("原生中文包与内置清单不匹配。")
            tap._write_atomic(destination, data)
            return destination
        except Exception as exc:
            last_error = exc
            logging.warning("Could not download native Chinese package from %s: %s", url, exc)
    raise RuntimeError("无法从腾讯云 COS 或 GitHub 下载并验证原生中文包。") from last_error


def _shared_specs(package: NativeChinesePackage):
    return _xovi_standalone.specs_from_package(
        package,
        FEATURE_ID,
        QMD_PATH,
        (EXTENSION_PATH, CATALOG_PATH),
    )


def _trusted_shared_context(identity: tap.DeviceIdentity):
    return tap._trusted_shared_context(identity)


def _state_from_inspection(
    ssh_client,
    inspection: _xovi_standalone.SharedInspection,
    emergency: bool,
) -> tuple[NativeChineseState, str, bool]:
    record = inspection.states.get(FEATURE_ID)
    if record is None:
        return NativeChineseState.NOT_INSTALLED, "共享 Xovi 正由其他 rmtool 功能使用", False
    if emergency:
        return NativeChineseState.EMERGENCY_DISABLED, "紧急停用标记存在，下次启动将使用原生 xochitl", True
    _xovi_standalone.assert_startup_guard_not_latched(inspection)
    current = tap._xochitl_process_token(ssh_client)
    process_changed = current != record.process_token
    if record.enabled:
        if process_changed and inspection.active:
            return NativeChineseState.ENABLED, "", True
        if not process_changed:
            return NativeChineseState.ENABLE_PENDING_REBOOT, "等待手动重启后载入", True
        raise RuntimeError("共享 Xovi 未在当前 xochitl 进程中载入。")
    if process_changed:
        return NativeChineseState.INSTALLED_DISABLED, "", True
    return NativeChineseState.DISABLE_PENDING_REBOOT, "等待手动重启后停用", True


def get_status(
    ssh_client,
    catalog: Iterable[NativeChinesePackage] = (),
) -> NativeChineseStatus:
    packages = tuple(catalog) or _trusted_catalog()
    identity = tap.get_device_identity(ssh_client)
    package = select_package(packages, identity)
    emergency = _xovi_standalone.recovery_sentinel_present(ssh_client)
    shared_exists = _xovi_standalone.has_shared_artifacts(ssh_client)
    if not shared_exists:
        if package is None:
            return NativeChineseStatus(
                NativeChineseState.INCOMPATIBLE,
                identity,
                detail="当前固件、硬件、架构或 xochitl 哈希不在精确支持清单中",
                emergency_disabled=emergency,
            )
        return NativeChineseStatus(
            NativeChineseState.NOT_INSTALLED,
            identity,
            package,
            emergency_disabled=emergency,
        )
    try:
        marker_identity = tap.DeviceIdentity(*_xovi_standalone.read_shared_identity(ssh_client))
        if marker_identity != identity:
            runtime, trusted, _legacies = _trusted_shared_context(marker_identity)
            inspection = _xovi_standalone.inspect_shared_firmware_residue(
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
            if FEATURE_ID not in inspection.states:
                raise RuntimeError("旧共享 Xovi 不包含原生简体中文；请在对应功能中清理。")
            return NativeChineseStatus(
                NativeChineseState.FIRMWARE_RESIDUE,
                identity,
                package,
                "检测到固件升级后遗留的原生中文共享 Xovi 状态，可安全清理",
                True,
                emergency,
            )
        runtime, trusted, _legacies = _trusted_shared_context(identity)
        inspection = _xovi_standalone.inspect_shared(ssh_client, runtime, trusted)
        if package is None and FEATURE_ID not in inspection.states:
            return NativeChineseStatus(
                NativeChineseState.INCOMPATIBLE,
                identity,
                detail="当前设备没有精确匹配的原生中文包；其他共享功能不受影响",
                emergency_disabled=emergency,
            )
        if package is None:
            raise RuntimeError("当前固件没有精确匹配的原生中文包。")
        state, detail, installed = _state_from_inspection(ssh_client, inspection, emergency)
        return NativeChineseStatus(state, identity, package, detail, installed, emergency)
    except Exception as exc:
        return NativeChineseStatus(
            NativeChineseState.BROKEN,
            identity,
            package,
            str(exc),
            True,
            emergency,
        )


def get_cloud_status(ssh_client, state_dir: str) -> NativeChineseStatus:
    return get_status(ssh_client, load_catalog(state_dir))


def _bundled_french_slot_package(
    identity: tap.DeviceIdentity,
) -> _rmkit_cn.TranslationPackage:
    catalog = _rmkit_cn.parse_translation_manifest(
        _rmkit_cn.BUNDLED_TRANSLATION_MANIFEST_PATH.read_bytes()
    )
    root = catalog.get(identity.firmware)
    candidates = (root, *root.variants) if root is not None else ()
    matches = tuple(
        package
        for package in candidates
        if package.platform.casefold() == identity.platform.casefold()
        and (
            not package.xochitl_sha256
            or package.xochitl_sha256 == identity.xochitl_sha256
        )
    )
    if len(matches) != 1:
        raise RuntimeError(
            "内置汉化清单无法唯一验证当前设备的法语槽位，拒绝部署原生中文。"
        )
    return matches[0]


def _reject_active_french_slot(
    ssh_client, identity: tap.DeviceIdentity
) -> None:
    status = _rmkit_cn.get_localization_status(
        ssh_client, _bundled_french_slot_package(identity)
    )
    if status.state is not _rmkit_cn.LocalizationState.NOT_INSTALLED:
        raise RuntimeError("请先在系统汉化中还原法语槽位汉化，再启用原生简体中文。")


def enable(
    ssh_client,
    package: NativeChinesePackage,
    archive_path: str | Path,
    state_dir: str,
) -> NativeChineseStatus:
    identity = tap.get_device_identity(ssh_client)
    trusted = select_package(_trusted_catalog(), identity)
    if trusted is None or trusted != package:
        raise RuntimeError("设备与原生中文包不精确匹配，未执行修改。")
    if not _rmkit_cn.has_cjk_font(ssh_client):
        raise RuntimeError(
            "当前 sans-serif 字体不支持简体中文。请先在字体管理中上传并设为"
            "系统字体，确认字体状态正常后再启用原生简体中文。"
        )
    tap._preflight_device(ssh_client)
    _reject_active_french_slot(ssh_client, identity)
    runtime, feature_trust, legacies = _trusted_shared_context(identity)
    _runtime, feature = _shared_specs(package)
    with tempfile.TemporaryDirectory() as temporary:
        extracted = tap.extract_verified_package(archive_path, package, temporary)
        _xovi_standalone.enable_shared(
            ssh_client,
            runtime,
            feature,
            extracted,
            feature_trust,
            legacies,
        )
    return get_status(ssh_client, (package,))


def enable_cloud(
    ssh_client,
    package: NativeChinesePackage,
    state_dir: str,
) -> NativeChineseStatus:
    return enable(ssh_client, package, download_package(package, state_dir), state_dir)


def _switch_selected_chinese_to_english(ssh_client) -> None:
    data = _rmkit_cn._read_bytes(ssh_client, _rmkit_cn.CONFIG_PATH)
    text = data.decode("utf-8", "surrogateescape")
    if _rmkit_cn._general_language(text) != "zh_CN":
        return
    updated = _rmkit_cn.set_language_config(text, "en")
    _rmkit_cn._write_remote_bytes(
        ssh_client,
        _rmkit_cn.CONFIG_PATH,
        updated.encode("utf-8", "surrogateescape"),
    )
    _rmkit_cn._flush_remote_writes(ssh_client)


def disable(
    ssh_client,
    catalog: Iterable[NativeChinesePackage] = (),
) -> NativeChineseStatus:
    identity = tap.get_device_identity(ssh_client)
    marker_identity = tap.DeviceIdentity(
        *_xovi_standalone.read_shared_identity(ssh_client)
    )
    runtime, trusted, _legacies = _trusted_shared_context(marker_identity)
    if marker_identity != identity:
        inspection = _xovi_standalone.inspect_shared_firmware_residue(
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
        if FEATURE_ID not in inspection.states:
            raise RuntimeError("旧共享 Xovi 不包含原生简体中文。")
        _switch_selected_chinese_to_english(ssh_client)
        _xovi_standalone.remove_shared_firmware_residue(
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
        return get_status(ssh_client, tuple(catalog) or _trusted_catalog())
    with _xovi_standalone._operation_lock(ssh_client):
        inspection = _xovi_standalone.inspect_shared(ssh_client, runtime, trusted)
        if FEATURE_ID not in inspection.states:
            raise RuntimeError("原生简体中文尚未安装。")
        _switch_selected_chinese_to_english(ssh_client)
        _xovi_standalone._disable_shared_locked(
            ssh_client,
            runtime,
            FEATURE_ID,
            trusted,
        )
    return get_status(ssh_client, tuple(catalog) or _trusted_catalog())


def set_emergency_disable(
    ssh_client,
    catalog: Iterable[NativeChinesePackage] = (),
) -> NativeChineseStatus:
    _xovi_standalone.set_recovery_sentinel(ssh_client)
    return get_status(ssh_client, tuple(catalog) or _trusted_catalog())


def clear_emergency_disable(
    ssh_client,
    catalog: Iterable[NativeChinesePackage] = (),
) -> NativeChineseStatus:
    _xovi_standalone.clear_recovery_sentinel(ssh_client)
    return get_status(ssh_client, tuple(catalog) or _trusted_catalog())
