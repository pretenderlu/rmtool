"""Official AppLoad installation on rmtool's firmware-gated shared Xovi."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Optional

import _tap_page_turn as tap
import _xovi_standalone as shared


APPLOAD_REPO_URL = "https://github.com/asivery/rm-appload"
KOREADER_REPO_URL = "https://github.com/koreader/koreader"
APPLOAD_VERSION = "v0.5.3"
KOREADER_VERSION = "v2026.07.1"
FEATURE_ID = "appload"
KOREADER_FEATURE_ID = "koreader"
KOREADER_INSTALL_DIR = "/home/root/xovi/exthome/appload/koreader"
SHIM_LINK_DIR = "/home/root/shims"

MAX_APPLOAD_BYTES = 16 * 1024 * 1024
MAX_KOREADER_BYTES = 64 * 1024 * 1024
MAX_KOREADER_UNPACKED_BYTES = 160 * 1024 * 1024
MAX_ZIP_ENTRIES = 5000


@dataclass(frozen=True)
class OfficialAsset:
    product: str
    version: str
    architecture: str
    name: str
    size: int
    sha256: str
    url: str


@dataclass(frozen=True)
class OfficialFile:
    path: str
    size: int
    sha256: str
    mode: int


APPLOAD_ASSETS = {
    "aarch64": OfficialAsset(
        "AppLoad",
        APPLOAD_VERSION,
        "aarch64",
        "appload-aarch64.zip",
        4_118_708,
        "032e3f2c57a004aba4425894758e4b542c67590efd222e3b3d5141124c45e84d",
        f"{APPLOAD_REPO_URL}/releases/download/"
        f"{APPLOAD_VERSION}/appload-aarch64.zip",
    ),
    "armv7l": OfficialAsset(
        "AppLoad",
        APPLOAD_VERSION,
        "armv7l",
        "appload-arm32.zip",
        4_201_361,
        "dd68c6816c121934da78f59eb497c215e5a9729200de0a8a5bcbeaa5d0aa068b",
        f"{APPLOAD_REPO_URL}/releases/download/"
        f"{APPLOAD_VERSION}/appload-arm32.zip",
    ),
}

APPLOAD_FILES = {
    "aarch64": (
        OfficialFile(
            "appload.so",
            4_056_880,
            "31214cbbe64c8bfe7d99096f077c3009dba8a42ef1a733801aa0ec59c134e7cc",
            0o755,
        ),
        OfficialFile(
            "shims/qtfb-shim-32bit.so",
            251_072,
            "aa4fb1e6f2edf5ef0137360cac77713a24ab508800301f81c19c579fee3f5031",
            0o755,
        ),
        OfficialFile(
            "shims/qtfb-shim.so",
            251_040,
            "6df704049aa057ff6374eaaa03a4f4a4d683b7c1ce772920d1a124be74d782c4",
            0o755,
        ),
    ),
    "armv7l": (
        OfficialFile(
            "appload.so",
            9_351_704,
            "0c5592e48098288fd00b71f72b3eb6821aae0bf17cc3646dd8e95bf19c391810",
            0o755,
        ),
        OfficialFile(
            "shims/qtfb-shim-32bit.so",
            544_188,
            "19a9d2c75741113f37f81f7affead40eeb12fa3cc41109b7f41ba154f60799cc",
            0o755,
        ),
        OfficialFile(
            "shims/qtfb-shim.so",
            544_156,
            "4eab5f8f54d5fbaaba86497128b3b5029dc07033ac9dd499a226638cc255e9d2",
            0o755,
        ),
    ),
}

KOREADER_ASSETS = {
    "aarch64": OfficialAsset(
        "KOReader",
        KOREADER_VERSION,
        "aarch64",
        "koreader-remarkable-aarch64-v2026.07.1.zip",
        41_844_453,
        "d1de77b7c6bad07875e12306481ca35d62470a6ceb414580aadf9eb79dba8cd7",
        f"{KOREADER_REPO_URL}/releases/download/{KOREADER_VERSION}/"
        "koreader-remarkable-aarch64-v2026.07.1.zip",
    ),
    "armv7l": OfficialAsset(
        "KOReader",
        KOREADER_VERSION,
        "armv7l",
        "koreader-remarkable-v2026.07.1.zip",
        40_441_730,
        "f3576f15956f4cdcb08df81ecd85236d68f63d2791209672aca12a7981668daa",
        f"{KOREADER_REPO_URL}/releases/download/{KOREADER_VERSION}/"
        "koreader-remarkable-v2026.07.1.zip",
    ),
}

KOREADER_ICON = OfficialFile(
    "koreader/icon.png",
    10_760,
    "f73a6538c2a9e7722a0b1f87bae805805e7af7c4c44dc0dbad1f1f08d4af9fe0",
    0o644,
)


class AppLoadState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    REPAIRABLE = "repairable"
    ENABLE_PENDING_REBOOT = "enable_pending_reboot"
    ENABLED = "enabled"
    INSTALLED_DISABLED = "installed_disabled"
    DISABLE_PENDING_REBOOT = "disable_pending_reboot"
    BROKEN = "broken"


@dataclass(frozen=True)
class AppLoadStatus:
    state: AppLoadState
    identity: tap.DeviceIdentity
    asset: Optional[OfficialAsset] = None
    detail: str = ""


def _safe_zip_path(value: str) -> str:
    if not value or "\\" in value:
        raise RuntimeError("官方 ZIP 包含无效路径。")
    path = PurePosixPath(value.rstrip("/"))
    if (
        path.is_absolute()
        or ".." in path.parts
        or str(path) != value.rstrip("/")
    ):
        raise RuntimeError("官方 ZIP 包含不安全路径。")
    return str(path)


def verify_official_asset(path: str | Path, asset: OfficialAsset) -> Path:
    candidate = Path(path)
    if not candidate.is_file() or candidate.name != asset.name:
        raise RuntimeError(f"请选择官方文件 {asset.name}。")
    if candidate.stat().st_size != asset.size:
        raise RuntimeError(f"{asset.product} 官方包大小校验失败。")
    digest = hashlib.sha256()
    with candidate.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != asset.sha256:
        raise RuntimeError(f"{asset.product} 官方包 SHA-256 校验失败。")
    return candidate


def download_official_asset(asset: OfficialAsset, state_dir: str) -> Path:
    destination = (
        Path(state_dir)
        / "cache"
        / "official"
        / asset.product.lower()
        / asset.version
        / asset.name
    )
    if destination.is_file():
        try:
            return verify_official_asset(destination, asset)
        except RuntimeError:
            destination.unlink(missing_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    digest = hashlib.sha256()
    written = 0
    request = urllib.request.Request(
        asset.url, headers={"User-Agent": "rmtool-official-installer/1"}
    )
    logging.info(
        "Downloading official %s asset from %s", asset.product, asset.url
    )
    try:
        with (
            urllib.request.urlopen(request, timeout=45) as response,
            temporary.open("wb") as output,
        ):
            declared = response.headers.get("Content-Length")
            if declared and int(declared) != asset.size:
                raise RuntimeError(
                    f"{asset.product} 官方服务器返回的文件大小异常。"
                )
            while chunk := response.read(1024 * 1024):
                written += len(chunk)
                if written > asset.size:
                    raise RuntimeError(
                        f"{asset.product} 官方下载超过预期大小。"
                    )
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if written != asset.size or digest.hexdigest() != asset.sha256:
            raise RuntimeError(f"{asset.product} 官方下载校验失败。")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def extract_official_zip(
    archive: str | Path,
    destination: str | Path,
    *,
    maximum_unpacked: int,
    expected_prefix: str = "",
) -> Path:
    output = Path(destination)
    output.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if not entries or len(entries) > MAX_ZIP_ENTRIES:
                raise RuntimeError("官方 ZIP 文件数量异常。")
            for info in entries:
                name = _safe_zip_path(info.filename)
                if expected_prefix and not (
                    name == expected_prefix
                    or name.startswith(expected_prefix + "/")
                ):
                    raise RuntimeError("官方 ZIP 顶层目录不符合预期。")
                unix_type = (info.external_attr >> 16) & 0o170000
                if unix_type not in (0, 0o040000, 0o100000):
                    raise RuntimeError("官方 ZIP 包含链接或特殊文件。")
                total += info.file_size
                if total > maximum_unpacked:
                    raise RuntimeError("官方 ZIP 解压后超过允许大小。")
                target = output.joinpath(*PurePosixPath(name).parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, target.open("wb") as target_file:
                    copied = 0
                    while chunk := source.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > info.file_size:
                            raise RuntimeError("官方 ZIP 文件大小异常。")
                        target_file.write(chunk)
                if copied != info.file_size:
                    raise RuntimeError("官方 ZIP 文件不完整。")
                mode = (info.external_attr >> 16) & 0o777
                os.chmod(target, mode if mode in (0o644, 0o755) else 0o644)
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError("无法解压官方 ZIP。") from exc
    return output


def _runtime_package(identity: tap.DeviceIdentity):
    package = tap.select_package(tap._trusted_catalog(), identity)
    return package if package is not None and package.channel == "stable" else None


def app_asset(identity: tap.DeviceIdentity) -> Optional[OfficialAsset]:
    if _runtime_package(identity) is None:
        return None
    return APPLOAD_ASSETS.get(identity.architecture)


def koreader_asset(identity: tap.DeviceIdentity) -> Optional[OfficialAsset]:
    if _runtime_package(identity) is None:
        return None
    return KOREADER_ASSETS.get(identity.architecture)


def _appload_feature(identity: tap.DeviceIdentity) -> shared.SharedFeatureSpec:
    files = APPLOAD_FILES[identity.architecture]
    primary = files[0]
    return shared.SharedFeatureSpec(
        FEATURE_ID,
        f"appload-{APPLOAD_VERSION}-{identity.architecture}",
        f"official-appload/{primary.path}",
        "extensions.d/appload.so",
        primary.sha256,
        primary.size,
        primary.mode,
        tuple(
            shared.SharedFeatureFileSpec(
                f"official-appload/{item.path}",
                item.path,
                item.sha256,
                item.size,
                item.mode,
            )
            for item in files[1:]
        ),
        strict_metadata_paths=(
            "extensions.d/appload.so",
            "shims/qtfb-shim.so",
            "shims/qtfb-shim-32bit.so",
        ),
    )


def koreader_bridge_bytes() -> bytes:
    text = (
        '{\n  "name": "KOReader",\n'
        f'  "application": "{KOREADER_INSTALL_DIR}/koreader.sh",\n'
        f'  "workingDirectory": "{KOREADER_INSTALL_DIR}",\n'
        '  "environment": {"KO_USE_QTFB": "1"},\n'
        '  "qtfb": true\n}\n'
    )
    return text.encode("utf-8")


def _koreader_feature(identity: tap.DeviceIdentity) -> shared.SharedFeatureSpec:
    manifest = koreader_bridge_bytes()
    return shared.SharedFeatureSpec(
        KOREADER_FEATURE_ID,
        f"koreader-bridge-{KOREADER_VERSION}-{identity.architecture}",
        "koreader-bridge/external.manifest.json",
        "exthome/appload/koreader/external.manifest.json",
        hashlib.sha256(manifest).hexdigest(),
        len(manifest),
        0o644,
        (
            shared.SharedFeatureFileSpec(
                "koreader-bridge/icon.png",
                "exthome/appload/koreader/icon.png",
                KOREADER_ICON.sha256,
                KOREADER_ICON.size,
                0o644,
            ),
        ),
    )


def trusted_specs(identity: tap.DeviceIdentity):
    package = _runtime_package(identity)
    if package is None or identity.architecture not in APPLOAD_ASSETS:
        return None, {}
    runtime, _tap_feature = tap._shared_specs(package)
    return runtime, {
        FEATURE_ID: _appload_feature(identity),
        KOREADER_FEATURE_ID: _koreader_feature(identity),
    }


def _extension_active(ssh_client) -> bool:
    path = f"{shared.SHARED_LAYOUT.remote_base}/extensions.d/appload.so"
    command = (
        "pid=$(systemctl show xochitl -p MainPID --value 2>/dev/null || true); "
        '[ -n "$pid" ] && [ "$pid" != 0 ] && '
        f"grep -Fq {shlex.quote(path)} /proc/$pid/maps 2>/dev/null"
    )
    return ssh_client.exec_command(command)[2] == 0


def get_status(ssh_client) -> AppLoadStatus:
    identity = tap.get_device_identity(ssh_client)
    asset = app_asset(identity)
    if asset is None:
        return AppLoadStatus(
            AppLoadState.INCOMPATIBLE,
            identity,
            detail=(
                "当前设备没有精确匹配的正式版共享运行资源；"
                "3.28 测试版不受支持"
            ),
        )
    if not shared.has_shared_artifacts(ssh_client):
        return AppLoadStatus(AppLoadState.NOT_INSTALLED, identity, asset)
    try:
        marker_identity = tap.DeviceIdentity(*shared.read_shared_identity(ssh_client))
        if marker_identity != identity:
            raise RuntimeError(
                "检测到固件升级后遗留的共享 Xovi 状态，"
                "请先在设备工具中清理。"
            )
        runtime, trusted, _legacies = tap._trusted_shared_context(identity)
        inspection = shared.inspect_shared(ssh_client, runtime, trusted)
        record = inspection.states.get(FEATURE_ID)
        if record is None:
            return AppLoadStatus(AppLoadState.NOT_INSTALLED, identity, asset)
        if inspection.launcher_update_available:
            return AppLoadStatus(
                AppLoadState.REPAIRABLE,
                identity,
                asset,
                "检测到旧版启动器，可直接修复并重新启用",
            )
        current = tap._xochitl_process_token(ssh_client)
        changed = current != record.process_token
        if record.enabled:
            if not changed:
                return AppLoadStatus(
                    AppLoadState.ENABLE_PENDING_REBOOT,
                    identity,
                    asset,
                    "等待手动重启后载入",
                )
            if _extension_active(ssh_client):
                return AppLoadStatus(AppLoadState.ENABLED, identity, asset)
            raise RuntimeError("AppLoad 未在当前 xochitl 进程中载入。")
        return AppLoadStatus(
            AppLoadState.INSTALLED_DISABLED
            if changed
            else AppLoadState.DISABLE_PENDING_REBOOT,
            identity,
            asset,
            "" if changed else "等待手动重启后停用",
        )
    except Exception as exc:
        return AppLoadStatus(AppLoadState.BROKEN, identity, asset, str(exc))


def _prepare_appload_root(
    identity: tap.DeviceIdentity,
    official_archive: str | Path,
    state_dir: str,
    destination: str | Path,
) -> Path:
    runtime_package = _runtime_package(identity)
    if runtime_package is None:
        raise RuntimeError("当前设备没有可用的正式版共享运行资源。")
    runtime_archive = tap.download_package(runtime_package, state_dir)
    root = tap.extract_verified_package(
        runtime_archive, runtime_package, destination
    )
    official_root = root / "official-appload"
    extract_official_zip(
        official_archive,
        official_root,
        maximum_unpacked=MAX_APPLOAD_BYTES,
    )
    expected = {
        item.path: item for item in APPLOAD_FILES[identity.architecture]
    }
    found = {
        path.relative_to(official_root).as_posix(): path
        for path in official_root.rglob("*")
        if path.is_file()
    }
    if set(found) != set(expected):
        raise RuntimeError("AppLoad 官方包文件清单不符合预期。")
    for name, path in found.items():
        item = expected[name]
        if (
            path.stat().st_size != item.size
            or hashlib.sha256(path.read_bytes()).hexdigest() != item.sha256
        ):
            raise RuntimeError(f"AppLoad 官方文件 {name} 校验失败。")
        os.chmod(path, item.mode)
    return root


def enable(
    ssh_client,
    archive_path: str | Path,
    state_dir: str,
) -> AppLoadStatus:
    identity = tap.get_device_identity(ssh_client)
    asset = app_asset(identity)
    if asset is None:
        raise RuntimeError("当前设备不是 rmtool 精确支持的正式版固件。")
    archive = verify_official_asset(archive_path, asset)
    tap._preflight_device(ssh_client)
    runtime, trusted, legacies = tap._trusted_shared_context(identity)
    feature = trusted[FEATURE_ID]
    with tempfile.TemporaryDirectory() as temporary:
        root = _prepare_appload_root(
            identity, archive, state_dir, temporary
        )
        shared.enable_shared(
            ssh_client, runtime, feature, root, trusted, legacies
        )
    return get_status(ssh_client)


def enable_cloud(ssh_client, state_dir: str) -> AppLoadStatus:
    identity = tap.get_device_identity(ssh_client)
    asset = app_asset(identity)
    if asset is None:
        raise RuntimeError("当前设备不是 rmtool 精确支持的正式版固件。")
    archive = download_official_asset(asset, state_dir)
    return enable(ssh_client, archive, state_dir)


def ensure_shim_links(ssh_client, identity: tap.DeviceIdentity) -> None:
    files = {
        PurePosixPath(item.path).name: item
        for item in APPLOAD_FILES[identity.architecture]
        if item.path.startswith("shims/")
    }
    commands = [f"mkdir -p {shlex.quote(SHIM_LINK_DIR)}"]
    for name, item in sorted(files.items()):
        link = f"{SHIM_LINK_DIR}/{name}"
        target = f"{shared.SHARED_LAYOUT.remote_base}/shims/{name}"
        commands.append(
            f"""
if [ -L {shlex.quote(link)} ]; then
    [ "$(readlink {shlex.quote(link)})" = {shlex.quote(target)} ] || {{ echo 'unexpected shim link' >&2; exit 1; }}
elif [ -e {shlex.quote(link)} ]; then
    [ -f {shlex.quote(link)} ] && [ "$(sha256sum {shlex.quote(link)} | awk '{{print $1}}')" = {shlex.quote(item.sha256)} ] || {{ echo 'unmanaged shim file' >&2; exit 1; }}
    rm -f {shlex.quote(link)}
    ln -s {shlex.quote(target)} {shlex.quote(link)}
else
    ln -s {shlex.quote(target)} {shlex.quote(link)}
fi
[ -L {shlex.quote(link)} ] && [ "$(readlink {shlex.quote(link)})" = {shlex.quote(target)} ]
""".strip()
        )
    ssh_client.exec_checked("\n".join(commands))


def remove_shim_links(ssh_client, identity: tap.DeviceIdentity) -> None:
    commands = []
    for item in APPLOAD_FILES[identity.architecture]:
        if not item.path.startswith("shims/"):
            continue
        name = PurePosixPath(item.path).name
        link = f"{SHIM_LINK_DIR}/{name}"
        target = f"{shared.SHARED_LAYOUT.remote_base}/shims/{name}"
        commands.append(
            f"if [ -L {shlex.quote(link)} ] && "
            f"[ \"$(readlink {shlex.quote(link)})\" = "
            f"{shlex.quote(target)} ]; then rm -f {shlex.quote(link)}; fi"
        )
    commands.append(f"rmdir {shlex.quote(SHIM_LINK_DIR)} 2>/dev/null || true")
    ssh_client.exec_checked("\n".join(commands))


def disable(ssh_client) -> AppLoadStatus:
    status = get_status(ssh_client)
    if status.state in (
        AppLoadState.INCOMPATIBLE,
        AppLoadState.NOT_INSTALLED,
    ):
        return status
    if status.state == AppLoadState.BROKEN:
        raise RuntimeError(status.detail or "AppLoad 状态无法验证。")
    identity = status.identity
    runtime, trusted, _legacies = tap._trusted_shared_context(identity)
    inspection = shared.inspect_shared(ssh_client, runtime, trusted)
    koreader = inspection.states.get(KOREADER_FEATURE_ID)
    if koreader is not None and koreader.enabled:
        raise RuntimeError("请先卸载 rmtool 管理的 KOReader，再停用 AppLoad。")
    if ssh_client.file_exists(KOREADER_INSTALL_DIR):
        raise RuntimeError(
            "检测到 KOReader 目录；为避免破坏应用，请先处理 KOReader。"
        )
    shared.disable_shared(ssh_client, runtime, FEATURE_ID, trusted)
    remove_shim_links(ssh_client, identity)
    return get_status(ssh_client)
