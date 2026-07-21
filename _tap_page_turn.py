"""Persistent, firmware-gated tap-to-turn support for reMarkable devices."""

from __future__ import annotations

import hashlib
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

MAX_MANIFEST_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 40 * 1024 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_FIRMWARE_RE = re.compile(r"[0-9]{14}")
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2,3}")
_PLATFORM_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
_ARCH_RE = re.compile(r"[a-z0-9_][a-z0-9_-]{0,31}")
_ASSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.tar\.gz")

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


def _active_with_rmtool_payload(ssh_client) -> bool:
    command = (
        "pid=$(systemctl show xochitl -p MainPID --value 2>/dev/null || true); "
        "[ -n \"$pid\" ] && [ \"$pid\" != 0 ] && "
        f"grep -Fq '{REMOTE_BASE}/xovi.so' /proc/$pid/maps 2>/dev/null"
    )
    _stdout, _stderr, code = ssh_client.exec_command(command)
    return code == 0


def _payload_valid(ssh_client, package: TapPageTurnPackage) -> tuple[bool, str]:
    try:
        marker = json.loads(_remote_text(ssh_client, MARKER_PATH))
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
    package = select_package(packages, identity)
    if package is None:
        return TapPageTurnStatus(
            TapPageTurnState.INCOMPATIBLE,
            identity,
            available_packages=available,
            detail="没有与设备身份和 xochitl 哈希精确匹配的包",
            dropin_present=dropin_exists,
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


def _assert_no_xovi_conflict(ssh_client) -> None:
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
    if output:
        raise RuntimeError(
            "检测到其他 xochitl/Xovi 持久化配置，拒绝自动合并：" + output
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
    _assert_no_xovi_conflict(ssh_client)
    _preflight_device(ssh_client)

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


def disable(
    ssh_client,
    catalog: Iterable[TapPageTurnPackage] = (),
) -> TapPageTurnStatus:
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
