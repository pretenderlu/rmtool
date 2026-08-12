"""Exact-build offline Pinyin input support for reMarkable firmware."""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import shlex
import tempfile
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

import _tap_page_turn as tap
import _xovi_standalone


REPO_URL = "https://github.com/pretenderlu/rmtool"
COS_URL = (
    "https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/"
    "pinyin-input"
)
GITHUB_URL = f"{REPO_URL}/releases/download/pinyin-input-assets"
BUNDLED_MANIFEST = Path(__file__).resolve().parent / "pinyin-input" / "manifest.json"
FEATURE_ID = "pinyin-input"
QMD_PATH = "exthome/qt-resource-rebuilder/pinyin-input.qmd"
HOOK_PATH = "pinyin-input/ime_hook.so"
RCC_PATH = "exthome/qt-resource-rebuilder/zh_CN.rcc"
V2_RCC_PATH = "pinyin-input/zh_CN.rcc"
SERVER_PATH = "pinyin-input/ime-server"
UNIT_PATH = "pinyin-input/rmtool-pinyin-input.service"
UNIT_NAME = "rmtool-pinyin-input.service"
NOTICE_PATH = "pinyin-input/NOTICE-rmkit.md"
LICENSE_PATH = "pinyin-input/LICENSE-rmkit"
REMOTE_BASE = "/home/root/.local/share/rmtool/pinyin-input"
REMOTE_SERVER = f"{REMOTE_BASE}/ime-server"
REMOTE_MARKER = f"{REMOTE_BASE}/package.json"
REMOTE_FILE_MAP = {
    SERVER_PATH: "ime-server",
    NOTICE_PATH: "NOTICE-rmkit.md",
    LICENSE_PATH: "LICENSE-rmkit",
}
PAYLOAD_PATHS = {
    "xovi.so",
    "extensions.d/qt-resource-rebuilder.so",
    "exthome/qt-resource-rebuilder/hashtab",
    "qmd-tool",
    QMD_PATH,
    HOOK_PATH,
    RCC_PATH,
    SERVER_PATH,
    UNIT_PATH,
    NOTICE_PATH,
    LICENSE_PATH,
}
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 40 * 1024 * 1024
SUPPORTED_IDENTITY = tap.DeviceIdentity(
    "20260806095513",
    "ferrari",
    "aarch64",
    "8726b4fce55a9154a5014956e5204401ce881d752c1ff3813adb622a68aac2f9",
)
ALLOWED_TARGETS = {
    ("20260506100933", "chiappa", "aarch64", "4646e0aef1cef2b3417889073ad5faba9259ae6b41f68326e75ef9a5c520c322"): ("3.27.1.0", "stable", True, False),
    ("20260506100933", "ferrari", "aarch64", "29b9896b07f59636d910d8a740f6562c502f676a1a70f8814459229d25cc5288"): ("3.27.1.0", "stable", True, False),
    ("20260612085811", "chiappa", "aarch64", "227a9bfe928ef5d164359e490d97648ffca40a5de13f07a9eb57a618a403f084"): ("3.27.3.0", "stable", True, False),
    ("20260612085811", "ferrari", "aarch64", "9749880daa2f10844e77b560ec0ecddd1634d43eb328af637c7026edf3ef120e"): ("3.27.3.0", "stable", True, False),
    ("20260629074044", "chiappa", "aarch64", "9e3e0372a15da25b148ac17667feb566014440e079c3e3ee504112d556ad2e10"): ("3.28.0.162", "beta", True, False),
    ("20260629074044", "ferrari", "aarch64", "10082aeb857c69c3f404ab189d7403318ba97d0c169e756ae9a5b3532b248a4a"): ("3.28.0.162", "beta", True, False),
    ("20260702125656", "chiappa", "aarch64", "08171df6296b99d04b3694b337bd0ce911e6a93356955961a37de9dd93a0394d"): ("3.28.0.163", "beta", True, False),
    ("20260702125656", "ferrari", "aarch64", "49f60572e830f6c4f20d800a56d644cdf53cd65a8e240b2b27106cce55040f89"): ("3.28.0.163", "beta", True, False),
    ("20260702125656", "chiappa", "aarch64", "3a9e18483b73f43016fb25b451e3ece0efba7aa1cc92e080771e138ce6bbca98"): ("3.28.0.164", "beta", True, False),
    ("20260702125656", "ferrari", "aarch64", "113bf7ea62ad171ea03c77c1f90e0666bcff163242a22ebca84372533b270c1c"): ("3.28.0.164", "beta", True, False),
    (SUPPORTED_IDENTITY.firmware, SUPPORTED_IDENTITY.platform, SUPPORTED_IDENTITY.architecture, SUPPORTED_IDENTITY.xochitl_sha256): ("3.28.0.166", "beta", True, True),
}
EXPECTED_ASSETS = {
    identity: f"rmtool-pinyin-input-{identity[1]}-{identity[0]}-{policy[0]}.tar.gz"
    for identity, policy in ALLOWED_TARGETS.items()
}
RMKIT_IME_PATHS = (
    "/home/root/rmkit-cn/bin/ime-server",
    "/home/root/rmkit-cn/bin/ime_hook.so",
    "/etc/systemd/system/rmkit-cn-ime.service",
    "/etc/systemd/system/rmkit-cn-ime-http.service",
    "/etc/systemd/system/xochitl.service.d/zz-rmkit-cn.conf",
)
V1_ARCHIVE_SHA256 = "a44df633180253d489eb34b9accc1f00b8adcb82f1bf7d6c1d452bda764f48d1"
V2_ARCHIVE_SHA256 = "275380fda80304102fd0aba9356f1b9e1cb0f473ffffee9811e57ecffe372638"
V3_ARCHIVE_SHA256 = "03863c0fa904aa710e6e37e6c786c7afe286a35a47689c36e64d4c9af629d7ee"
V3_QMD_SHA256 = "3c5b8c3545225e6d05d1c28cfd4558844b401773a4aef8a9ebe55eefec056b50"
V3_QMD_SIZE = 51642
V4_ARCHIVE_SHA256 = "d2014789e4e201a63f4745fb47d61e4378a238b7f5e32c169023969791822f27"
V4_QMD_SHA256 = "a80b669e09eb292d5bd5f6e71d64b21aacc133cc5ec287a75c4698d92b96f2c3"
V4_QMD_SIZE = 52384
V5_ARCHIVE_SHA256 = "2c01890467e7dbd82ae7c96f6d33aa95fdb9ba75412839b31845832fefc0d107"


class PinyinInputState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    INSTALLED_DISABLED = "installed_disabled"
    ENABLE_PENDING_REBOOT = "enable_pending_reboot"
    ENABLED = "enabled"
    DISABLE_PENDING_REBOOT = "disable_pending_reboot"
    EMERGENCY_DISABLED = "emergency_disabled"
    OUTDATED = "outdated"
    BROKEN = "broken"


@dataclass(frozen=True)
class PinyinInputPackage:
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
class PinyinInputStatus:
    state: PinyinInputState
    identity: tap.DeviceIdentity
    package: Optional[PinyinInputPackage] = None
    detail: str = ""
    installed: bool = False
    emergency_disabled: bool = False


@dataclass(frozen=True)
class _SharedPredecessor:
    reason: str
    archive_sha256: str
    feature: _xovi_standalone.SharedFeatureSpec


def parse_manifest(data: bytes) -> tuple[PinyinInputPackage, ...]:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("拼音输入法清单不是有效 JSON。") from exc
    entries = document.get("packages") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "packages"}
        or document.get("schema_version") != 1
        or not isinstance(entries, list)
    ):
        raise RuntimeError("拼音输入法清单结构无效。")
    packages = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "firmware", "release_version", "channel", "platform",
            "architecture", "xochitl_sha256", "asset", "sha256", "size",
            "urls", "files", "offline_verified", "device_verified",
        }:
            raise RuntimeError("拼音输入法包格式无效。")
        if not isinstance(entry["files"], list):
            raise RuntimeError("拼音输入法包缺少文件清单。")
        files = tuple(tap._parse_payload_file(item) for item in entry["files"])
        urls = entry["urls"]
        if (
            entry["channel"] not in {"stable", "beta"}
            or type(entry["offline_verified"]) is not bool
            or type(entry["device_verified"]) is not bool
            or not isinstance(entry["size"], int)
            or isinstance(entry["size"], bool)
            or not 0 < entry["size"] <= MAX_PACKAGE_BYTES
            or not isinstance(urls, list)
            or len(urls) != 2
            or len({item.path for item in files}) != len(files)
            or set(item.path for item in files) != PAYLOAD_PATHS
            or sum(item.size for item in files) > MAX_UNPACKED_BYTES
        ):
            raise RuntimeError("拼音输入法包字段或文件白名单无效。")
        identity = tap.DeviceIdentity(
            tap._required_string(entry, "firmware", tap._FIRMWARE_RE),
            tap._required_string(entry, "platform", tap._PLATFORM_RE),
            tap._required_string(entry, "architecture", tap._ARCH_RE),
            tap._required_string(entry, "xochitl_sha256", tap._SHA256_RE),
        )
        identity_key = (
            identity.firmware,
            identity.platform,
            identity.architecture,
            identity.xochitl_sha256,
        )
        release_version = tap._required_string(
            entry, "release_version", tap._VERSION_RE
        )
        expected = ALLOWED_TARGETS.get(identity_key)
        if expected != (
            release_version,
            entry["channel"],
            entry["offline_verified"],
            entry["device_verified"],
        ):
            raise RuntimeError("拼音输入法清单包含未审核的设备目标。")
        asset = tap._required_string(entry, "asset", tap._ASSET_RE)
        if asset != EXPECTED_ASSETS[identity_key]:
            raise RuntimeError("拼音输入法包名称不在精确信任白名单中。")
        expected_urls = (f"{COS_URL}/{asset}", f"{GITHUB_URL}/{asset}")
        if tuple(urls) != expected_urls:
            raise RuntimeError("拼音输入法包下载源无效。")
        packages.append(PinyinInputPackage(
            identity.firmware,
            release_version,
            entry["channel"],
            identity.platform,
            identity.architecture,
            identity.xochitl_sha256,
            asset,
            tap._required_string(entry, "sha256", tap._SHA256_RE),
            entry["size"],
            files,
            expected_urls,
            entry["offline_verified"],
            entry["device_verified"],
        ))
    identities = {
        (item.firmware, item.platform, item.architecture, item.xochitl_sha256)
        for item in packages
    }
    if (
        len(packages) != len(identities)
        or len({item.asset for item in packages}) != len(packages)
        or identities != set(ALLOWED_TARGETS)
    ):
        raise RuntimeError("拼音输入法清单必须完整且唯一地包含 11 个精确目标。")
    return tuple(packages)


@lru_cache(maxsize=1)
def _trusted_catalog() -> tuple[PinyinInputPackage, ...]:
    return parse_manifest(BUNDLED_MANIFEST.read_bytes())


def select_package(
    catalog: Iterable[PinyinInputPackage], identity: tap.DeviceIdentity
) -> Optional[PinyinInputPackage]:
    return next(
        (
            package
            for package in catalog
            if (
                package.firmware,
                package.platform,
                package.architecture,
                package.xochitl_sha256,
            )
            == (
                identity.firmware,
                identity.platform,
                identity.architecture,
                identity.xochitl_sha256,
            )
        ),
        None,
    )


def _cache_path(state_dir: str, package: PinyinInputPackage) -> Path:
    return Path(state_dir) / "cache" / FEATURE_ID / package.firmware / package.asset


def download_package(package: PinyinInputPackage, state_dir: str) -> Path:
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
                raise RuntimeError("拼音输入法包与内置信任清单不匹配。")
            tap._write_atomic(destination, data)
            return destination
        except Exception as exc:
            last_error = exc
            logging.warning("Could not download Pinyin package from %s: %s", url, exc)
    raise RuntimeError("无法从腾讯云 COS 或 GitHub 下载并验证拼音输入法包。") from last_error


def _shared_specs(package: PinyinInputPackage):
    runtime, feature = _xovi_standalone.specs_from_package(
        package, FEATURE_ID, QMD_PATH, (HOOK_PATH, RCC_PATH, UNIT_PATH)
    )
    server = package.file(SERVER_PATH)
    return runtime, replace(
        feature,
        preload_paths=(HOOK_PATH,),
        strict_metadata_paths=(HOOK_PATH, RCC_PATH),
        sidecars=(
            _xovi_standalone.SharedSidecarSpec(
                REMOTE_SERVER,
                server.sha256,
                server.size,
                server.mode,
                UNIT_NAME,
                UNIT_PATH,
            ),
        ),
    )


def _known_shared_predecessor_specs(
    package: PinyinInputPackage,
) -> tuple[_SharedPredecessor, ...]:
    """Describe exact Pinyin packages superseded by the current package."""
    if (
        package.firmware,
        package.platform,
        package.architecture,
        package.xochitl_sha256,
    ) != (
        SUPPORTED_IDENTITY.firmware,
        SUPPORTED_IDENTITY.platform,
        SUPPORTED_IDENTITY.architecture,
        SUPPORTED_IDENTITY.xochitl_sha256,
    ):
        return ()
    _runtime, current = _shared_specs(package)
    rcc = package.file(RCC_PATH)
    legacy_files = tuple(
        item for item in current.extra_files if item.runtime_path != UNIT_PATH
    )
    legacy_sidecar = replace(
        current.sidecars[0], unit_name="", unit_runtime_path=""
    )
    v5 = replace(
        current,
        extra_files=legacy_files,
        sidecars=(legacy_sidecar,),
    )
    v4 = replace(
        v5,
        sha256=V4_QMD_SHA256,
        size=V4_QMD_SIZE,
        strict_metadata_paths=(),
    )
    v3 = replace(v5, strict_metadata_paths=())
    v2 = replace(
        v3,
        extra_files=tuple(
            replace(item, archive_path=V2_RCC_PATH, runtime_path=V2_RCC_PATH)
            if item.runtime_path == RCC_PATH else item
            for item in v3.extra_files
        ),
        legacy_resource_path=V2_RCC_PATH,
    )
    v1 = replace(
        v3,
        extra_files=tuple(
            item for item in v3.extra_files if item.runtime_path != RCC_PATH
        ),
    )
    if not any(
        item.runtime_path == V2_RCC_PATH
        and item.sha256 == rcc.sha256
        and item.size == rcc.size
        for item in v2.extra_files
    ):
        raise RuntimeError("无法构造受信的拼音输入法 v2 迁移描述。")
    return (
        _SharedPredecessor("inline_home_sidecar", V5_ARCHIVE_SHA256, v5),
        _SharedPredecessor("keyboard_label_owned_by_pinyin", V4_ARCHIVE_SHA256, v4),
        _SharedPredecessor("preload_metadata_unchecked", V3_ARCHIVE_SHA256, v3),
        _SharedPredecessor("rcc_subdirectory", V2_ARCHIVE_SHA256, v2),
        _SharedPredecessor("missing_rcc", V1_ARCHIVE_SHA256, v1),
    )


def _inspect_shared_revision(
    ssh_client,
    runtime: _xovi_standalone.SharedRuntimeSpec,
    trusted: dict[str, _xovi_standalone.SharedFeatureSpec],
    package: PinyinInputPackage,
    *,
    check_lower: bool = False,
):
    revisions = {
        FEATURE_ID: tuple(
            (item.reason, item.feature)
            for item in _known_shared_predecessor_specs(package)
        )
    }
    try:
        import _native_chinese as native

        peer = native.select_package(
            native._trusted_catalog(),
            tap.DeviceIdentity(
                package.firmware,
                package.platform,
                package.architecture,
                package.xochitl_sha256,
            ),
        )
        if peer is not None and native.FEATURE_ID in trusted:
            revisions[native.FEATURE_ID] = tuple(
                (item.reason, item.feature)
                for item in native._known_shared_predecessor_specs(peer)
            )
    except ImportError:
        pass
    inspection, installed_trusted, selected = (
        _xovi_standalone.inspect_shared_revisions(
            ssh_client,
            runtime,
            trusted,
            {feature_id: items for feature_id, items in revisions.items() if items},
            check_lower=check_lower,
        )
    )
    return inspection, installed_trusted, selected.get(FEATURE_ID)


def _trusted_shared_context(identity: tap.DeviceIdentity):
    return tap._trusted_shared_context(identity)


def _external_marker(package: PinyinInputPackage) -> bytes:
    document = {
        "schema_version": 1,
        "feature": FEATURE_ID,
        "package_id": package.package_id,
        "files": {
            REMOTE_FILE_MAP[path]: package.file(path).sha256
            for path in REMOTE_FILE_MAP
        },
    }
    return (json.dumps(document, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")


def _external_specs(package: PinyinInputPackage) -> dict[str, _xovi_standalone.SharedFileSpec]:
    result = {
        remote: _xovi_standalone.SharedFileSpec(
            remote, package.file(archive).sha256, package.file(archive).size,
            package.file(archive).mode,
        )
        for archive, remote in REMOTE_FILE_MAP.items()
    }
    marker = _external_marker(package)
    result["package.json"] = _xovi_standalone.SharedFileSpec(
        "package.json", hashlib.sha256(marker).hexdigest(), len(marker), 0o644
    )
    return result


def _has_external_payload(ssh_client) -> bool:
    return ssh_client.file_exists(REMOTE_BASE) or ssh_client.file_exists(REMOTE_MARKER)


def _validate_external_payload(ssh_client, package: PinyinInputPackage) -> None:
    _xovi_standalone._validate_owned_tree(
        ssh_client, REMOTE_BASE, _external_specs(package), "拼音输入法服务"
    )


def _assert_no_rmkit_ime(ssh_client) -> None:
    command = "\n".join(
        f"[ ! -e {shlex.quote(path)} ] && [ ! -L {shlex.quote(path)} ] || echo {shlex.quote(path)}"
        for path in RMKIT_IME_PATHS
    )
    found = tuple(line for line in ssh_client.exec_checked(command).splitlines() if line)
    if found:
        raise RuntimeError(
            "检测到 rmkit 管理的拼音输入法，拒绝混合安装：" + ", ".join(found)
        )


def _server_running(ssh_client) -> bool:
    command = f"""
for proc in /proc/[0-9]*; do
    [ "$(readlink "$proc/exe" 2>/dev/null || true)" = {shlex.quote(REMOTE_SERVER)} ] && exit 0
done
exit 1
""".strip()
    result = ssh_client.exec_command(command)
    return isinstance(result, tuple) and len(result) == 3 and result[2] == 0


def _state_from_inspection(
    ssh_client,
    inspection: _xovi_standalone.SharedInspection,
    emergency: bool,
) -> tuple[PinyinInputState, str, bool]:
    record = inspection.states.get(FEATURE_ID)
    if record is None:
        return PinyinInputState.NOT_INSTALLED, "共享 Xovi 正由其他 rmtool 功能使用", False
    if emergency:
        return PinyinInputState.EMERGENCY_DISABLED, "紧急停用标记存在", True
    _xovi_standalone.assert_startup_guard_not_latched(inspection)
    current = tap._xochitl_process_token(ssh_client)
    changed = current != record.process_token
    if record.enabled:
        if changed and inspection.active:
            if not _server_running(ssh_client):
                raise RuntimeError("拼音词库服务未在当前 xochitl 会话中运行。")
            return PinyinInputState.ENABLED, "", True
        if not changed:
            return PinyinInputState.ENABLE_PENDING_REBOOT, "等待手动重启后载入", True
        raise RuntimeError("共享 Xovi 未在当前 xochitl 进程中载入。")
    if changed:
        return PinyinInputState.INSTALLED_DISABLED, "", True
    return PinyinInputState.DISABLE_PENDING_REBOOT, "等待手动重启后停用", True


def get_status(
    ssh_client, catalog: Iterable[PinyinInputPackage] = ()
) -> PinyinInputStatus:
    packages = tuple(catalog) or _trusted_catalog()
    identity = tap.get_device_identity(ssh_client)
    package = select_package(packages, identity)
    emergency = _xovi_standalone.recovery_sentinel_present(ssh_client)
    shared_exists = _xovi_standalone.has_shared_artifacts(ssh_client)
    external_exists = _has_external_payload(ssh_client)
    if not shared_exists:
        if external_exists:
            return PinyinInputStatus(
                PinyinInputState.BROKEN, identity, package,
                "存在未被共享 Xovi 引用的拼音服务残留", True, emergency,
            )
        state = PinyinInputState.NOT_INSTALLED if package else PinyinInputState.INCOMPATIBLE
        detail = "" if package else "当前固件没有精确匹配的拼音输入法包"
        return PinyinInputStatus(state, identity, package, detail, False, emergency)
    try:
        marker_identity = tap.DeviceIdentity(*_xovi_standalone.read_shared_identity(ssh_client))
        if marker_identity != identity:
            raise RuntimeError("检测到固件升级后的旧共享 Xovi 状态，请先清理旧插件。")
        runtime, trusted, _legacies = _trusted_shared_context(identity)
        if package is None:
            inspection = _xovi_standalone.inspect_shared(
                ssh_client, runtime, trusted
            )
            outdated = None
        else:
            inspection, _installed_trusted, outdated = _inspect_shared_revision(
                ssh_client, runtime, trusted, package
            )
        record = inspection.states.get(FEATURE_ID)
        if record is None:
            if external_exists:
                raise RuntimeError("拼音服务存在，但共享 Xovi 中没有对应功能记录。")
            state = PinyinInputState.NOT_INSTALLED if package else PinyinInputState.INCOMPATIBLE
            return PinyinInputStatus(state, identity, package, installed=False, emergency_disabled=emergency)
        if package is None:
            raise RuntimeError("当前固件没有精确匹配的拼音输入法包。")
        if record.enabled:
            if not external_exists:
                raise RuntimeError("拼音服务文件缺失。")
            _validate_external_payload(ssh_client, package)
        elif external_exists:
            raise RuntimeError("拼音功能已停用，但服务目录仍有残留。")
        if outdated:
            detail = {
                "keyboard_label_owned_by_pinyin": (
                    "已精确验证为仍由拼音功能修改键盘名称的旧版拼音包，"
                    "需与原生中文补丁一并更新，可直接修复更新"
                ),
                "inline_home_sidecar": (
                    "已精确验证为会让 xochitl 等待加密主目录的旧版拼音包，"
                    "可迁移为锁屏安全的独立词库服务，可直接修复更新"
                ),
                "preload_metadata_unchecked": (
                    "已精确验证为尚未在启动时检查 hook 与键盘资源所有权的旧版拼音包，"
                    "可直接修复更新"
                ),
                "rcc_subdirectory": (
                    "已精确验证为中文键盘布局资源放在无效子目录的第二版拼音包；"
                    "QRR 不会扫描该位置，可直接修复更新"
                ),
                "missing_rcc": (
                    "已精确验证为缺少中文键盘布局资源的首版拼音包，可直接修复更新"
                ),
            }[outdated]
            return PinyinInputStatus(
                PinyinInputState.OUTDATED,
                identity,
                package,
                detail,
                True,
                emergency,
            )
        state, detail, installed = _state_from_inspection(ssh_client, inspection, emergency)
        return PinyinInputStatus(state, identity, package, detail, installed, emergency)
    except Exception as exc:
        return PinyinInputStatus(
            PinyinInputState.BROKEN, identity, package, str(exc), True, emergency
        )


def _stage_external(ssh_client, package: PinyinInputPackage, extracted: Path, stage: str) -> None:
    ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)} && mkdir -p {shlex.quote(stage)}")
    for archive_path, remote_name in REMOTE_FILE_MAP.items():
        item = package.file(archive_path)
        local = extracted.joinpath(*PurePosixPath(archive_path).parts)
        _xovi_standalone._upload_path(
            ssh_client, local, f"{stage}/{remote_name}", item.mode
        )
    _xovi_standalone._upload_bytes(
        ssh_client, _external_marker(package), f"{stage}/package.json", 0o644
    )
    ssh_client.exec_checked(f"chown -R root:root {shlex.quote(stage)}")
    _xovi_standalone._validate_owned_tree(
        ssh_client, stage, _external_specs(package), "拼音输入法暂存服务"
    )


def enable(
    ssh_client,
    package: PinyinInputPackage,
    archive_path: str | Path,
    state_dir: str,
) -> PinyinInputStatus:
    identity = tap.get_device_identity(ssh_client)
    if select_package(_trusted_catalog(), identity) != package:
        raise RuntimeError("设备与拼音输入法包不精确匹配，未执行修改。")
    tap._preflight_device(ssh_client)
    _assert_no_rmkit_ime(ssh_client)
    runtime, trusted, legacies = _trusted_shared_context(identity)
    _runtime, feature = _shared_specs(package)
    token = uuid.uuid4().hex
    stage = f"{REMOTE_BASE}.staging-{token}"
    backup = f"{REMOTE_BASE}.backup-{token}"
    with tempfile.TemporaryDirectory() as temporary:
        extracted = tap.extract_verified_package(archive_path, package, temporary)
        with _xovi_standalone._operation_lock(ssh_client):
            installed_trusted = trusted
            inspection = None
            outdated = False
            if _xovi_standalone.has_shared_artifacts(ssh_client):
                inspection, installed_trusted, outdated = _inspect_shared_revision(
                    ssh_client, runtime, trusted, package, check_lower=True
                )
            had_previous = _has_external_payload(ssh_client)
            if had_previous:
                _validate_external_payload(ssh_client, package)
            installed = inspection.states.get(FEATURE_ID) if inspection else None
            if (
                had_previous
                and not outdated
                and installed is not None
                and installed.enabled
                and installed.spec == feature
                and inspection.layout == _xovi_standalone.SHARED_LAYOUT
            ):
                return get_status(ssh_client, (package,))
            previous_moved = False
            new_moved = False
            try:
                _stage_external(ssh_client, package, extracted, stage)
                ssh_client.exec_checked(f"rm -rf {shlex.quote(backup)}")
                if had_previous:
                    ssh_client.exec_checked(
                        f"mv {shlex.quote(REMOTE_BASE)} {shlex.quote(backup)}"
                    )
                    previous_moved = True
                ssh_client.exec_checked(f"mv {shlex.quote(stage)} {shlex.quote(REMOTE_BASE)}")
                new_moved = True
                _xovi_standalone._enable_shared_locked(
                    ssh_client,
                    runtime,
                    feature,
                    extracted,
                    installed_trusted,
                    legacies,
                )
            except Exception:
                try:
                    if new_moved:
                        ssh_client.exec_checked(f"rm -rf {shlex.quote(REMOTE_BASE)}")
                    if previous_moved:
                        ssh_client.exec_checked(
                            f"mv {shlex.quote(backup)} {shlex.quote(REMOTE_BASE)}"
                        )
                    ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)}")
                except Exception:
                    logging.exception("Could not roll back Pinyin service payload")
                raise
            ssh_client.exec_checked(f"rm -rf {shlex.quote(backup)}")
    return get_status(ssh_client, (package,))


def enable_cloud(
    ssh_client, package: PinyinInputPackage, state_dir: str
) -> PinyinInputStatus:
    return enable(ssh_client, package, download_package(package, state_dir), state_dir)


def disable(
    ssh_client, catalog: Iterable[PinyinInputPackage] = ()
) -> PinyinInputStatus:
    identity = tap.get_device_identity(ssh_client)
    package = select_package(tuple(catalog) or _trusted_catalog(), identity)
    if package is None:
        raise RuntimeError("当前设备没有可验证的拼音输入法包。")
    runtime, trusted, _legacies = _trusted_shared_context(identity)
    token = uuid.uuid4().hex
    backup = f"{REMOTE_BASE}.backup-{token}"
    with _xovi_standalone._operation_lock(ssh_client):
        inspection, installed_trusted, outdated = _inspect_shared_revision(
            ssh_client, runtime, trusted, package, check_lower=True
        )
        if FEATURE_ID not in inspection.states:
            raise RuntimeError("拼音输入法尚未安装。")
        had_payload = _has_external_payload(ssh_client)
        if had_payload:
            _validate_external_payload(ssh_client, package)
            ssh_client.exec_checked(f"rm -rf {shlex.quote(backup)}")
            ssh_client.exec_checked(f"mv {shlex.quote(REMOTE_BASE)} {shlex.quote(backup)}")
        try:
            _xovi_standalone._disable_shared_locked(
                ssh_client,
                runtime,
                FEATURE_ID,
                installed_trusted,
                trusted[FEATURE_ID] if outdated else None,
            )
        except Exception:
            if had_payload:
                try:
                    ssh_client.exec_checked(
                        f"mv {shlex.quote(backup)} {shlex.quote(REMOTE_BASE)}"
                    )
                except Exception:
                    logging.exception("Could not restore Pinyin service payload")
            raise
        if had_payload:
            ssh_client.exec_checked(f"rm -rf {shlex.quote(backup)}")
    return get_status(ssh_client, tuple(catalog) or _trusted_catalog())
