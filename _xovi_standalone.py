"""Shared script generation for rmtool-owned standalone Xovi features."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import shlex
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional


@dataclass(frozen=True)
class StandaloneLayout:
    remote_base: str
    dropin_name: str
    log_tag: str
    mount_tag: str

    @property
    def dropin_path(self) -> str:
        return f"/etc/systemd/system/xochitl.service.d/{self.dropin_name}"

    @property
    def launcher_path(self) -> str:
        return f"{self.remote_base}/launcher.sh"


SHARED_LAYOUT = StandaloneLayout(
    remote_base="/data/rmtool/xovi-standalone",
    dropin_name="92-rmtool-xovi-standalone.conf",
    log_tag="rmtool-xovi-standalone",
    mount_tag="rmtool-xovi-shared",
)
LEGACY_SHARED_LAYOUT = StandaloneLayout(
    remote_base="/home/root/.local/share/rmtool/xovi-standalone",
    dropin_name=SHARED_LAYOUT.dropin_name,
    log_tag=SHARED_LAYOUT.log_tag,
    mount_tag=SHARED_LAYOUT.mount_tag,
)
SHARED_MARKER_PATH = f"{SHARED_LAYOUT.remote_base}/package.json"
SHARED_QRR_HOME = "exthome/qt-resource-rebuilder"
SHARED_RECOVERY_SENTINEL = "/data/rmtool/disable-xovi"
LEGACY_RECOVERY_SENTINEL = "/home/root/.local/share/rmtool/disable-xovi"
SHARED_STARTUP_PENDING = f"{SHARED_LAYOUT.remote_base}/startup.pending"
SHARED_STARTUP_STABLE_SECONDS = 90
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_OPERATION_LOCK = "/tmp/rmtool-xovi-standalone.lock"
_PROCESS_TOKEN_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9]+:[0-9]+"
)


@dataclass(frozen=True)
class SharedFileSpec:
    path: str
    sha256: str
    size: int
    mode: int


@dataclass(frozen=True)
class SharedRuntimeSpec:
    firmware: str
    platform: str
    architecture: str
    xochitl_sha256: str
    files: tuple[SharedFileSpec, ...]


@dataclass(frozen=True)
class SharedFeatureSpec:
    feature_id: str
    package_id: str
    archive_path: str
    runtime_path: str
    sha256: str
    size: int
    mode: int
    extra_files: tuple["SharedFeatureFileSpec", ...] = ()

    @property
    def files(self) -> tuple["SharedFeatureFileSpec", ...]:
        return (
            SharedFeatureFileSpec(
                self.archive_path,
                self.runtime_path,
                self.sha256,
                self.size,
                self.mode,
            ),
            *self.extra_files,
        )


@dataclass(frozen=True)
class SharedFeatureFileSpec:
    archive_path: str
    runtime_path: str
    sha256: str
    size: int
    mode: int


@dataclass(frozen=True)
class LegacyStandaloneSpec:
    feature: SharedFeatureSpec
    runtime: SharedRuntimeSpec
    layout: StandaloneLayout
    marker: Mapping[str, object]
    files: tuple[SharedFileSpec, ...]

    @property
    def marker_path(self) -> str:
        return f"{self.layout.remote_base}/package.json"


@dataclass(frozen=True)
class SharedFeatureState:
    spec: SharedFeatureSpec
    enabled: bool
    process_token: str


@dataclass(frozen=True)
class SharedInspection:
    states: Mapping[str, SharedFeatureState]
    active: bool
    dropin_present: bool
    startup_pending: bool = False
    layout: StandaloneLayout = SHARED_LAYOUT


_COMMON_ARCHIVE_PATHS = (
    "xovi.so",
    "extensions.d/qt-resource-rebuilder.so",
    f"{SHARED_QRR_HOME}/hashtab",
    "qmd-tool",
)


def specs_from_package(
    package,
    feature_id: str,
    archive_qmd_path: str,
    extra_archive_paths: Iterable[str] = (),
) -> tuple[SharedRuntimeSpec, SharedFeatureSpec]:
    files = tuple(
        SharedFileSpec(item.path, item.sha256, item.size, item.mode)
        for item in (package.file(path) for path in _COMMON_ARCHIVE_PATHS)
    )
    qmd = package.file(archive_qmd_path)
    extra_files = tuple(
        SharedFeatureFileSpec(
            item.path,
            item.path,
            item.sha256,
            item.size,
            item.mode,
        )
        for item in (package.file(path) for path in extra_archive_paths)
    )
    feature = SharedFeatureSpec(
        feature_id,
        package.package_id,
        archive_qmd_path,
        f"{SHARED_QRR_HOME}/rmtool-{feature_id}.qmd",
        qmd.sha256,
        qmd.size,
        qmd.mode,
        extra_files,
    )
    return (
        SharedRuntimeSpec(
            package.firmware,
            package.platform,
            package.architecture,
            package.xochitl_sha256,
            files,
        ),
        feature,
    )


def assert_common_runtime(
    runtime: SharedRuntimeSpec,
    features: Mapping[str, tuple[SharedRuntimeSpec, SharedFeatureSpec]],
) -> None:
    for peer_runtime, _feature in features.values():
        if peer_runtime != runtime:
            raise RuntimeError("两个功能的 Xovi/QRR 运行资源不一致，拒绝自动合并。")


def assert_feature_layout(
    runtime: SharedRuntimeSpec,
    features: Iterable[SharedFeatureSpec],
) -> None:
    """Reject ambiguous ownership before inspecting or mutating a shared tree."""
    owners = {item.path: "runtime" for item in runtime.files}
    if len(owners) != len(runtime.files):
        raise RuntimeError("共享 Xovi 运行资源路径重复。")
    for feature in features:
        for item in feature.files:
            path = PurePosixPath(item.runtime_path)
            if (
                path.is_absolute()
                or ".." in path.parts
                or str(path) != item.runtime_path
            ):
                raise RuntimeError("共享 Xovi 功能文件路径不安全。")
            if item.runtime_path.lower().endswith(".qmd") and (
                path.suffix != ".qmd" or str(path.parent) != SHARED_QRR_HOME
            ):
                raise RuntimeError("共享 Xovi 功能 QMD 必须位于共享 QRR 目录。")
            previous = owners.get(item.runtime_path)
            if previous is not None:
                raise RuntimeError(
                    f"共享 Xovi 文件路径冲突：{item.runtime_path}"
                )
            owners[item.runtime_path] = feature.feature_id


def shared_launcher(
    runtime: SharedRuntimeSpec,
    enabled: Iterable[SharedFeatureSpec],
    *,
    recovery_sentinel: bool = True,
    startup_guard: bool = True,
    layout: StandaloneLayout = SHARED_LAYOUT,
) -> str:
    enabled = tuple(sorted(enabled, key=lambda value: value.feature_id))
    assert_feature_layout(runtime, enabled)
    checks = []
    feature_files = tuple(
        SharedFileSpec(item.runtime_path, item.sha256, item.size, item.mode)
        for feature in enabled
        for item in feature.files
    )
    for item in (*runtime.files, *feature_files):
        remote = posixpath.join(layout.remote_base, item.path)
        checks.append(
            f'[ "$(file_sha {shlex.quote(remote)})" = "{item.sha256}" ] || stock'
        )
    checks_text = "\n".join(checks)
    qmd_names = tuple(
        posixpath.basename(item.runtime_path)
        for feature in enabled
        for item in feature.files
        if item.runtime_path.endswith(".qmd")
    )
    qmd_cases = "|".join(qmd_names) or "__rmtool_no_qmd__"
    extension_paths = tuple(sorted({
        "extensions.d/qt-resource-rebuilder.so",
        *(
            item.runtime_path
            for feature in enabled
            for item in feature.files
            if posixpath.dirname(item.runtime_path) == "extensions.d"
            and item.runtime_path.endswith(".so")
        ),
    }))
    extension_cases = "|".join(
        posixpath.basename(path) for path in extension_paths
    )
    extension_check = f"""extension_count=0
for extension in "$BASE"/extensions.d/*.so; do
    [ -f "$extension" ] && [ ! -L "$extension" ] || stock
    case "${{extension##*/}}" in
        {extension_cases}) ;;
        *) stock ;;
    esac
    extension_count=$((extension_count + 1))
done
[ "$extension_count" -eq {len(extension_paths)} ] || stock"""
    if recovery_sentinel and layout == LEGACY_SHARED_LAYOUT:
        recovery_check = f"[ ! -e {LEGACY_RECOVERY_SENTINEL} ] || stock"
    elif recovery_sentinel:
        recovery_check = (
            f"[ ! -e {SHARED_RECOVERY_SENTINEL} ] || stock\n"
            f"[ ! -e {LEGACY_RECOVERY_SENTINEL} ] || stock"
        )
    else:
        recovery_check = ""
    pending_path = f"{layout.remote_base}/startup.pending"
    startup_check = (
        f'if [ -e "$PENDING" ] || [ -L "$PENDING" ]; then\n'
        f'    logger -t {shlex.quote(layout.log_tag)} '
        '"previous Xovi startup did not stabilize; starting stock xochitl" '
        '2>/dev/null || true\n'
        '    stock\n'
        'fi'
        if startup_guard
        else ""
    )
    preflight_checks = checks_text
    for check in (recovery_check, startup_check):
        if check:
            preflight_checks += "\n" + check
    pending_variable = (
        f"PENDING={shlex.quote(pending_path)}\n"
        f'PENDING_TMP="$BASE/.startup-pending-$$.tmp"'
        if startup_guard
        else ""
    )
    startup_functions = f"""
arm_startup_guard() {{
    [ ! -e "$PENDING" ] && [ ! -L "$PENDING" ] || return 1
    trap 'rm -f "$PENDING_TMP"' EXIT HUP INT TERM
    umask 077
    if ! (: > "$PENDING_TMP" &&
        chmod 0600 "$PENDING_TMP" &&
        chown root:root "$PENDING_TMP" &&
        ln "$PENDING_TMP" "$PENDING"); then
        rm -f "$PENDING_TMP"
        trap - EXIT HUP INT TERM
        return 1
    fi
    rm -f "$PENDING_TMP"
    trap - EXIT HUP INT TERM
    [ -f "$PENDING" ] && [ ! -L "$PENDING" ] &&
        [ "$(stat -c '%a:%u:%g:%s' "$PENDING")" = '600:0:0:0' ]
}}

clear_guard_when_stable() {{
    main_pid=$1
    sleep {SHARED_STARTUP_STABLE_SECONDS}
    [ "$(readlink "/proc/$main_pid/exe" 2>/dev/null)" = "/usr/bin/xochitl" ] || return 0
    [ -f "$PENDING" ] && [ ! -L "$PENDING" ] &&
        [ "$(stat -c '%a:%u:%g:%s' "$PENDING")" = '600:0:0:0' ] || return 0
    rm -f "$PENDING" || return 0
    logger -t {shlex.quote(layout.log_tag)} "Xovi startup stable; guard cleared" 2>/dev/null || true
}}
""" if startup_guard else ""
    startup_arm = (
        "arm_startup_guard || stock\n"
        "clear_guard_when_stable $$ &"
        if startup_guard
        else ""
    )
    return f"""#!/bin/sh
BASE={shlex.quote(layout.remote_base)}
{pending_variable}

stock() {{
    logger -t {shlex.quote(layout.log_tag)} "preflight failed; starting stock xochitl" 2>/dev/null || true
    unset LD_PRELOAD XOVI_ROOT QML_DISABLE_DISK_CACHE QML_XHR_ALLOW_FILE_WRITE QML_XHR_ALLOW_FILE_READ
    exec /usr/bin/xochitl --system
}}

file_sha() {{
    [ -f "$1" ] && [ ! -L "$1" ] || return 1
    sha256sum "$1" | awk '{{print $1}}'
}}
{startup_functions}

[ "$(uname -m)" = "{runtime.architecture}" ] || stock
machine=$(cat /sys/devices/soc0/machine 2>/dev/null || true)
case "$machine" in
    *Ferrari*) platform=ferrari ;;
    *Chiappa*) platform=chiappa ;;
    *Tatsu*) platform=tatsu ;;
    *"reMarkable 1"*) platform=rm1 ;;
    *"reMarkable 2"*) platform=rm2 ;;
    *) platform=unknown ;;
esac
[ "$platform" = "{runtime.platform}" ] || stock
version=$(tr -cd '0-9' < /etc/version)
[ "$version" = "{runtime.firmware}" ] || stock
[ "$(file_sha /usr/bin/xochitl)" = "{runtime.xochitl_sha256}" ] || stock
[ "$(stat -c '%a:%u:%g' "$BASE" 2>/dev/null)" = '755:0:0' ] || stock
{preflight_checks}

set -- "$BASE"/extensions.d/*.so
{extension_check}
qmd_count=0
for qmd in "$BASE"/{SHARED_QRR_HOME}/*.qmd; do
    [ -f "$qmd" ] && [ ! -L "$qmd" ] || stock
    case "${{qmd##*/}}" in
        {qmd_cases}) ;;
        *) stock ;;
    esac
    qmd_count=$((qmd_count + 1))
done
[ "$qmd_count" -eq {len(qmd_names)} ] || stock

{startup_arm}
export XOVI_ROOT="$BASE"
export QML_DISABLE_DISK_CACHE=1
export QML_XHR_ALLOW_FILE_WRITE=1
export QML_XHR_ALLOW_FILE_READ=1
export LD_PRELOAD="$BASE/xovi.so"
exec /usr/bin/xochitl --system
"""


def shared_dropin(
    runtime: SharedRuntimeSpec,
    enabled: Iterable[SharedFeatureSpec],
    *,
    layout: StandaloneLayout = SHARED_LAYOUT,
) -> str:
    if layout == LEGACY_SHARED_LAYOUT:
        return f"""[Unit]
After=home.mount
ConditionPathExists={layout.launcher_path}

[Service]
ExecStart=
ExecStart={layout.launcher_path}
WatchdogSec=0
"""
    launcher = shlex.quote(layout.launcher_path)
    return f"""[Unit]
After=data.mount

[Service]
ExecStart=
ExecStart=/bin/sh -c 'if [ -f {launcher} ] && [ ! -L {launcher} ] && [ -r {launcher} ]; then exec /bin/sh {launcher}; else exec /usr/bin/xochitl --system; fi'
KillMode=control-group
WatchdogSec=0
"""


def _marker_document(
    runtime: SharedRuntimeSpec,
    states: Mapping[str, SharedFeatureState],
    launcher_sha256: str,
    dropin_sha256: str,
) -> dict:
    return {
        "schema_version": 1,
        "deployment_mode": "rmtool_shared_standalone",
        "runtime_present": any(state.enabled for state in states.values()),
        "identity": {
            "firmware": runtime.firmware,
            "platform": runtime.platform,
            "architecture": runtime.architecture,
            "xochitl_sha256": runtime.xochitl_sha256,
        },
        "runtime": {item.path: item.sha256 for item in runtime.files},
        "features": {
            feature_id: {
                "enabled": state.enabled,
                "package_id": state.spec.package_id,
                "qmd_path": state.spec.runtime_path,
                "qmd_sha256": state.spec.sha256,
                "process_token": state.process_token,
            }
            for feature_id, state in sorted(states.items())
        },
        "launcher_sha256": launcher_sha256,
        "dropin_sha256": dropin_sha256,
    }


def shared_marker(
    runtime: SharedRuntimeSpec,
    states: Mapping[str, SharedFeatureState],
    launcher_sha256: str,
    dropin_sha256: str,
) -> bytes:
    document = _marker_document(runtime, states, launcher_sha256, dropin_sha256)
    return (json.dumps(document, ensure_ascii=True, sort_keys=True) + "\n").encode("ascii")


def _remote_text(ssh_client, path: str) -> str:
    with ssh_client.open_remote(path, "r") as remote:
        data = remote.read()
    return data.decode("utf-8") if isinstance(data, bytes) else data


def _remote_sha256(ssh_client, path: str) -> str:
    output = ssh_client.exec_checked(f"sha256sum {shlex.quote(path)}").strip()
    digest = output.split()[0] if output else ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError(f"设备未返回 {path} 的有效 SHA-256。")
    return digest


def _validate_owned_tree(
    ssh_client,
    base: str,
    expected_files: Mapping[str, SharedFileSpec],
    label: str,
) -> None:
    expected_dirs = set()
    for path in expected_files:
        parent = PurePosixPath(path).parent
        while str(parent) != ".":
            expected_dirs.add(str(parent))
            parent = parent.parent
    expected_paths = set(expected_files) | expected_dirs
    command = (
        f"stat -c '%f|%u|%g|%s|%n' {shlex.quote(base)}; "
        f"find {shlex.quote(base)} -mindepth 1 -exec stat -c '%f|%u|%g|%s|%n' {{}} \\;"
    )
    records = ssh_client.exec_checked(command).splitlines()
    if not records:
        raise RuntimeError(f"{label}目录状态无效。")
    seen = set()
    for index, record in enumerate(records):
        parts = record.split("|", 4)
        if len(parts) != 5:
            raise RuntimeError(f"{label}目录包含无法验证的路径。")
        try:
            raw_mode, uid, gid, size = int(parts[0], 16), int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError as exc:
            raise RuntimeError(f"{label}目录元数据无效。") from exc
        remote = parts[4]
        if index == 0:
            relative = ""
            if remote != base:
                raise RuntimeError(f"{label}目录根路径无效。")
        else:
            prefix = base + "/"
            if not remote.startswith(prefix):
                raise RuntimeError(f"{label}目录包含越界路径。")
            relative = remote[len(prefix):]
        file_type = raw_mode & 0o170000
        permissions = raw_mode & 0o7777
        if uid != 0 or gid != 0:
            raise RuntimeError(f"{label}路径 {relative or '.'} 不是 root 所有。")
        if relative == "" or relative in expected_dirs:
            if file_type != 0o040000 or permissions != 0o755:
                raise RuntimeError(f"{label}目录 {relative or '.'} 类型或权限已变化。")
        elif relative in expected_files:
            item = expected_files[relative]
            if (
                file_type != 0o100000
                or permissions != item.mode
                or (item.size >= 0 and size != item.size)
            ):
                raise RuntimeError(f"{label}文件 {relative} 类型、权限或大小已变化。")
            if _remote_sha256(ssh_client, remote) != item.sha256:
                raise RuntimeError(f"{label}文件 {relative} 已被修改。")
        else:
            raise RuntimeError(f"{label}目录包含未托管路径 {relative}。")
        if relative in seen:
            raise RuntimeError(f"{label}目录包含重复路径。")
        seen.add(relative)
    if seen != expected_paths | {""}:
        raise RuntimeError(f"{label}目录包含缺失或未托管路径。")


def _assert_owned_dropin(ssh_client, path: str, digest: str, label: str) -> None:
    metadata = ssh_client.exec_checked(
        f"stat -c '%f %u %g' {shlex.quote(path)}"
    ).strip().split()
    if metadata != ["81a4", "0", "0"] or _remote_sha256(ssh_client, path) != digest:
        raise RuntimeError(f"{label} drop-in 类型、权限、所有权或内容已变化。")


def _process_token(ssh_client) -> str:
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


def _active(ssh_client, layout: StandaloneLayout = SHARED_LAYOUT) -> bool:
    command = (
        "pid=$(systemctl show xochitl -p MainPID --value 2>/dev/null || true); "
        '[ -n "$pid" ] && [ "$pid" != 0 ] && '
        f"grep -Fq '{layout.remote_base}/xovi.so' /proc/$pid/maps 2>/dev/null"
    )
    _stdout, _stderr, code = ssh_client.exec_command(command)
    return code == 0


def has_shared_artifacts(ssh_client) -> bool:
    return any(
        ssh_client.file_exists(path)
        for path in (
            SHARED_LAYOUT.remote_base,
            SHARED_MARKER_PATH,
            LEGACY_SHARED_LAYOUT.remote_base,
            f"{LEGACY_SHARED_LAYOUT.remote_base}/package.json",
            SHARED_LAYOUT.dropin_path,
        )
    )


def read_shared_identity(ssh_client) -> tuple[str, str, str, str]:
    current = ssh_client.file_exists(SHARED_LAYOUT.remote_base)
    legacy = ssh_client.file_exists(LEGACY_SHARED_LAYOUT.remote_base)
    if current and legacy:
        raise RuntimeError("检测到 /data 与 /home 两套共享 Xovi 布局。")
    marker_path = (
        SHARED_MARKER_PATH
        if current
        else f"{LEGACY_SHARED_LAYOUT.remote_base}/package.json"
    )
    try:
        marker = json.loads(_remote_text(ssh_client, marker_path))
    except Exception as exc:
        raise RuntimeError("共享 Xovi 标记不是有效 JSON。") from exc
    identity = marker.get("identity") if isinstance(marker, dict) else None
    if not isinstance(identity, dict) or set(identity) != {
        "firmware", "platform", "architecture", "xochitl_sha256"
    }:
        raise RuntimeError("共享 Xovi 标记中的设备身份无效。")
    firmware = identity["firmware"]
    platform = identity["platform"]
    architecture = identity["architecture"]
    xochitl_sha256 = identity["xochitl_sha256"]
    if (
        not isinstance(firmware, str)
        or not re.fullmatch(r"[0-9]{14}", firmware)
        or platform not in {"ferrari", "chiappa"}
        or architecture != "aarch64"
        or not isinstance(xochitl_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", xochitl_sha256)
    ):
        raise RuntimeError("共享 Xovi 标记中的设备身份不受本版本支持。")
    return firmware, platform, architecture, xochitl_sha256


@contextmanager
def _operation_lock(ssh_client):
    ssh_client.exec_checked(
        f"mkdir {shlex.quote(_OPERATION_LOCK)} 2>/dev/null || "
        "{ echo 'another rmtool Xovi operation is active' >&2; exit 1; }"
    )
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        try:
            ssh_client.exec_checked(f"rmdir {shlex.quote(_OPERATION_LOCK)}")
        except Exception:
            if failed:
                logging.exception("Could not release shared Xovi operation lock")
            else:
                raise


def _assert_managed_dropins(
    ssh_client, allowed: Iterable[str]
) -> None:
    command = """
for file in /etc/systemd/system/xochitl.service.d/*.conf; do
    [ -f "$file" ] || continue
    if grep -Eq 'LD_PRELOAD|XOVI_ROOT|^ExecStart=' "$file"; then
        echo "$file"
    fi
done
""".strip()
    found = set(ssh_client.exec_checked(command).splitlines())
    unmanaged = sorted(found - set(allowed))
    if unmanaged:
        raise RuntimeError(
            "检测到非 rmtool 管理的 xochitl/Xovi 持久化配置："
            + ", ".join(unmanaged)
        )


def _parse_states(
    marker: dict,
    trusted: Mapping[str, SharedFeatureSpec],
) -> dict[str, SharedFeatureState]:
    records = marker.get("features")
    if not isinstance(records, dict) or not records or not set(records) <= set(trusted):
        raise RuntimeError("共享 Xovi 标记包含未知功能。")
    states: dict[str, SharedFeatureState] = {}
    for feature_id, record in records.items():
        if not isinstance(record, dict) or set(record) != {
            "enabled", "package_id", "qmd_path", "qmd_sha256", "process_token"
        }:
            raise RuntimeError("共享 Xovi 功能状态格式无效。")
        spec = trusted[feature_id]
        if type(record["enabled"]) is not bool or not _PROCESS_TOKEN_RE.fullmatch(
            str(record["process_token"])
        ):
            raise RuntimeError("共享 Xovi 功能状态或进程身份无效。")
        expected = (spec.package_id, spec.runtime_path, spec.sha256)
        actual = (record["package_id"], record["qmd_path"], record["qmd_sha256"])
        if actual != expected:
            raise RuntimeError("共享 Xovi 功能状态与内置信任清单不匹配。")
        states[feature_id] = SharedFeatureState(
            spec, record["enabled"], record["process_token"]
        )
    return states


def inspect_shared(
    ssh_client,
    runtime: SharedRuntimeSpec,
    trusted: Mapping[str, SharedFeatureSpec],
    *,
    check_lower: bool = False,
) -> SharedInspection:
    assert_feature_layout(runtime, trusted.values())
    current = ssh_client.file_exists(SHARED_LAYOUT.remote_base)
    legacy = ssh_client.file_exists(LEGACY_SHARED_LAYOUT.remote_base)
    if current and legacy:
        raise RuntimeError("检测到 /data 与 /home 两套共享 Xovi 布局。")
    layout = LEGACY_SHARED_LAYOUT if legacy else SHARED_LAYOUT
    return _inspect_shared(
        ssh_client,
        runtime,
        trusted,
        layout=layout,
        check_lower=check_lower,
        expected_dropin=None,
    )


def assert_startup_guard_not_latched(inspection: SharedInspection) -> None:
    if inspection.startup_pending and not inspection.active:
        raise RuntimeError(
            "共享 Xovi 自动启动保护已触发；上一次插件启动未稳定，当前正使用原生 xochitl。"
        )


def _remote_entry_exists(ssh_client, path: str) -> bool:
    if ssh_client.file_exists(path) is True:
        return True
    result = ssh_client.exec_command(
        f"[ -e {shlex.quote(path)} ] || [ -L {shlex.quote(path)} ]"
    )
    if not isinstance(result, tuple) or len(result) != 3:
        return False
    _stdout, _stderr, code = result
    return code == 0


def recovery_sentinel_present(ssh_client) -> bool:
    return any(
        _remote_entry_exists(ssh_client, path)
        for path in (SHARED_RECOVERY_SENTINEL, LEGACY_RECOVERY_SENTINEL)
    )


def _assert_recovery_sentinel(ssh_client, sentinel: str = SHARED_RECOVERY_SENTINEL) -> None:
    path = shlex.quote(sentinel)
    ssh_client.exec_checked(
        f"[ -f {path} ] && [ ! -L {path} ] && "
        f"[ \"$(stat -c '%a:%u:%g:%s' {path})\" = '600:0:0:0' ]"
    )


def set_recovery_sentinel(ssh_client) -> None:
    with _operation_lock(ssh_client):
        path = shlex.quote(SHARED_RECOVERY_SENTINEL)
        if _remote_entry_exists(ssh_client, SHARED_RECOVERY_SENTINEL):
            _assert_recovery_sentinel(ssh_client)
            return
        if _remote_entry_exists(ssh_client, LEGACY_RECOVERY_SENTINEL):
            _assert_recovery_sentinel(ssh_client, LEGACY_RECOVERY_SENTINEL)
        directory = shlex.quote(posixpath.dirname(SHARED_RECOVERY_SENTINEL))
        temporary = shlex.quote(
            f"{posixpath.dirname(SHARED_RECOVERY_SENTINEL)}/"
            f".disable-xovi-{uuid.uuid4().hex}.tmp"
        )
        ssh_client.exec_checked(
            f"set -eu; mkdir -p {directory}; "
            f"[ -d {directory} ] && [ ! -L {directory} ]; "
            f"trap 'rm -f {temporary}' EXIT HUP INT TERM; "
            f"umask 077; : > {temporary}; chmod 0600 {temporary}; "
            f"chown root:root {temporary}; ln {temporary} {path}; rm -f {temporary}; "
            "trap - EXIT HUP INT TERM"
        )
        if not recovery_sentinel_present(ssh_client):
            raise RuntimeError("共享 Xovi 紧急停用标记未能创建。")
        _assert_recovery_sentinel(ssh_client)


def clear_recovery_sentinel(ssh_client) -> None:
    with _operation_lock(ssh_client):
        if not recovery_sentinel_present(ssh_client):
            return
        for sentinel in (SHARED_RECOVERY_SENTINEL, LEGACY_RECOVERY_SENTINEL):
            if not _remote_entry_exists(ssh_client, sentinel):
                continue
            path = shlex.quote(sentinel)
            ssh_client.exec_checked(
                f"[ -f {path} ] && [ ! -L {path} ] && "
                f"[ \"$(stat -c '%a:%u:%g:%s' {path})\" = '600:0:0:0' ] && "
                f"rm -f {path}"
            )
        if recovery_sentinel_present(ssh_client):
            raise RuntimeError("共享 Xovi 紧急停用标记未能清除。")


def inspect_shared_firmware_residue(
    ssh_client,
    runtime: SharedRuntimeSpec,
    trusted: Mapping[str, SharedFeatureSpec],
    current_identity: tuple[str, str, str, str],
) -> SharedInspection:
    installed_identity = (
        runtime.firmware,
        runtime.platform,
        runtime.architecture,
        runtime.xochitl_sha256,
    )
    if installed_identity == current_identity:
        raise RuntimeError("共享 Xovi 与当前固件身份相同，不属于固件升级残留。")
    current = ssh_client.file_exists(SHARED_LAYOUT.remote_base)
    legacy = ssh_client.file_exists(LEGACY_SHARED_LAYOUT.remote_base)
    if current and legacy:
        raise RuntimeError("检测到 /data 与 /home 两套共享 Xovi 布局。")
    layout = LEGACY_SHARED_LAYOUT if legacy else SHARED_LAYOUT
    inspection = _inspect_shared(
        ssh_client,
        runtime,
        trusted,
        layout=layout,
        check_lower=True,
        expected_dropin=False,
    )
    if not inspection.states:
        raise RuntimeError("未检测到可验证的共享 Xovi 固件升级残留。")
    if inspection.active:
        raise RuntimeError("旧共享 Xovi 仍在当前 xochitl 进程中载入，拒绝自动清理。")
    _assert_no_lower_xovi_dropins(ssh_client)
    return inspection


def _inspect_shared(
    ssh_client,
    runtime: SharedRuntimeSpec,
    trusted: Mapping[str, SharedFeatureSpec],
    *,
    layout: StandaloneLayout,
    check_lower: bool,
    expected_dropin: Optional[bool],
) -> SharedInspection:
    artifacts = has_shared_artifacts(ssh_client)
    if not artifacts:
        return SharedInspection({}, False, False, False, layout)
    _assert_managed_dropins(
        ssh_client,
        (SHARED_LAYOUT.dropin_path,) if expected_dropin is None else (),
    )
    if not (
        ssh_client.file_exists(layout.remote_base)
        and ssh_client.file_exists(f"{layout.remote_base}/package.json")
    ):
        raise RuntimeError("共享 Xovi 目录、标记或 drop-in 不完整。")
    try:
        marker = json.loads(_remote_text(ssh_client, f"{layout.remote_base}/package.json"))
    except Exception as exc:
        raise RuntimeError("共享 Xovi 标记不是有效 JSON。") from exc
    if not isinstance(marker, dict) or set(marker) != {
        "schema_version", "deployment_mode", "identity", "runtime", "features",
        "runtime_present", "launcher_sha256", "dropin_sha256"
    }:
        raise RuntimeError("共享 Xovi 标记字段无效。")
    states = _parse_states(marker, trusted)
    enabled = tuple(state.spec for state in states.values() if state.enabled)
    dropin_text = shared_dropin(runtime, enabled, layout=layout)
    dropin_sha = hashlib.sha256(dropin_text.encode()).hexdigest()
    launcher_text = shared_launcher(
        runtime,
        enabled,
        layout=layout,
        startup_guard=layout == SHARED_LAYOUT,
    )
    launcher_sha = hashlib.sha256(launcher_text.encode()).hexdigest()
    if marker != _marker_document(runtime, states, launcher_sha, dropin_sha):
        candidates = (
            shared_launcher(
                runtime,
                enabled,
                layout=layout,
                startup_guard=False,
            ),
            shared_launcher(
                runtime,
                enabled,
                layout=layout,
                recovery_sentinel=False,
                startup_guard=False,
            ),
        )
        for candidate in candidates:
            candidate_sha = hashlib.sha256(candidate.encode()).hexdigest()
            if marker == _marker_document(
                runtime, states, candidate_sha, dropin_sha
            ):
                launcher_text = candidate
                launcher_sha = candidate_sha
                break
        else:
            raise RuntimeError("共享 Xovi 标记与内置信任清单不匹配。")
    marker_bytes = (
        json.dumps(marker, ensure_ascii=True, sort_keys=True) + "\n"
    ).encode("ascii")
    expected_files = {}
    if enabled:
        expected_files.update({item.path: item for item in runtime.files})
        expected_files.update({
            item.runtime_path: SharedFileSpec(
                item.runtime_path, item.sha256, item.size, item.mode
            )
            for state in states.values() if state.enabled
            for item in state.spec.files
        })
        expected_files["launcher.sh"] = SharedFileSpec(
            "launcher.sh", launcher_sha, len(launcher_text.encode()), 0o755
        )
        expected_files[f"systemd/{SHARED_LAYOUT.dropin_name}"] = SharedFileSpec(
            f"systemd/{SHARED_LAYOUT.dropin_name}",
            dropin_sha,
            len(dropin_text.encode()),
            0o644,
        )
    expected_files["package.json"] = SharedFileSpec(
        "package.json", hashlib.sha256(marker_bytes).hexdigest(),
        len(marker_bytes), 0o644
    )
    startup_pending = _remote_entry_exists(
        ssh_client, f"{layout.remote_base}/startup.pending"
    )
    if startup_pending:
        expected_files["startup.pending"] = SharedFileSpec(
            "startup.pending", _EMPTY_SHA256, 0, 0o600
        )
    _validate_owned_tree(ssh_client, layout.remote_base, expected_files, "共享 Xovi")
    dropin_present = ssh_client.file_exists(SHARED_LAYOUT.dropin_path)
    dropin_required = bool(enabled) if expected_dropin is None else expected_dropin
    if dropin_present != dropin_required:
        raise RuntimeError("共享 Xovi drop-in 状态与功能状态不一致。")
    if dropin_present:
        _assert_owned_dropin(
            ssh_client, SHARED_LAYOUT.dropin_path, dropin_sha, "共享 Xovi 可见"
        )
    if check_lower:
        _check_lower_dropins(
            ssh_client,
            {SHARED_LAYOUT.dropin_path: dropin_sha if dropin_required else None},
        )
    return SharedInspection(
        states,
        _active(ssh_client, layout),
        dropin_present,
        startup_pending,
        layout,
    )


def _check_lower_dropins(ssh_client, expected: Mapping[str, Optional[str]]) -> None:
    token = uuid.uuid4().hex
    mount_dir = f"/tmp/rmtool-xovi-check-{token}"
    checks = []
    for path, digest in expected.items():
        lower = f"$MOUNT_DIR{path}"
        if digest is None:
            checks.append(f"[ ! -e {lower} ]")
        else:
            checks.append(
                f"[ -f {lower} ] && [ ! -L {lower} ] && "
                f"[ \"$(stat -c '%f %u %g' {lower})\" = '81a4 0 0' ] && "
                f"[ \"$(sha256sum {lower} | awk '{{print $1}}')\" = {shlex.quote(digest)} ]"
            )
    script = " && ".join(checks) or ":"
    ssh_client.exec_checked(f"""set -eu
MOUNT_DIR={shlex.quote(mount_dir)}
cleanup() {{ umount "$MOUNT_DIR" 2>/dev/null || true; rmdir "$MOUNT_DIR" 2>/dev/null || true; }}
trap cleanup EXIT INT TERM
mkdir -p "$MOUNT_DIR"
mount --bind / "$MOUNT_DIR"
{script}
cleanup
trap - EXIT INT TERM
""")


def _assert_no_lower_xovi_dropins(ssh_client) -> None:
    token = uuid.uuid4().hex
    mount_dir = f"/tmp/rmtool-xovi-residue-check-{token}"
    found = ssh_client.exec_checked(f"""set -eu
MOUNT_DIR={shlex.quote(mount_dir)}
cleanup() {{ umount "$MOUNT_DIR" 2>/dev/null || true; rmdir "$MOUNT_DIR" 2>/dev/null || true; }}
trap cleanup EXIT INT TERM
mkdir -p "$MOUNT_DIR"
mount --bind / "$MOUNT_DIR"
for file in "$MOUNT_DIR"/etc/systemd/system/xochitl.service.d/*.conf; do
    [ -f "$file" ] || continue
    if grep -Eq 'LD_PRELOAD|XOVI_ROOT|^ExecStart=' "$file"; then
        printf '%s\n' "${{file#"$MOUNT_DIR"}}"
    fi
done
cleanup
trap - EXIT INT TERM
""").splitlines()
    if found:
        raise RuntimeError(
            "底层 root 中仍存在 xochitl/Xovi drop-in，拒绝把共享状态认定为固件升级残留："
            + ", ".join(sorted(found))
        )


def validate_legacy(
    ssh_client,
    legacy: LegacyStandaloneSpec,
    *,
    check_lower: bool = True,
) -> bool:
    base = ssh_client.file_exists(legacy.layout.remote_base)
    marker_exists = ssh_client.file_exists(legacy.marker_path)
    dropin_exists = ssh_client.file_exists(legacy.layout.dropin_path)
    if not (base or marker_exists or dropin_exists):
        return False
    if not (base and marker_exists):
        raise RuntimeError(f"{legacy.feature.feature_id} 旧版独立安装不完整。")
    try:
        marker = json.loads(_remote_text(ssh_client, legacy.marker_path))
    except Exception as exc:
        raise RuntimeError(f"{legacy.feature.feature_id} 旧版标记无效。") from exc
    if marker != dict(legacy.marker):
        raise RuntimeError(f"{legacy.feature.feature_id} 旧版标记与内置信任清单不匹配。")
    expected_launcher = str(legacy.marker["launcher_sha256"])
    expected_dropin = str(legacy.marker["dropin_sha256"])
    marker_bytes = (
        json.dumps(dict(legacy.marker), ensure_ascii=True, sort_keys=True) + "\n"
    ).encode("ascii")
    expected_files = {item.path: item for item in legacy.files}
    expected_files.update(
        {
            "launcher.sh": SharedFileSpec("launcher.sh", expected_launcher, -1, 0o755),
            f"systemd/{legacy.layout.dropin_name}": SharedFileSpec(
                f"systemd/{legacy.layout.dropin_name}", expected_dropin, -1, 0o644
            ),
            "package.json": SharedFileSpec(
                "package.json", hashlib.sha256(marker_bytes).hexdigest(), len(marker_bytes), 0o644
            ),
        }
    )
    _validate_owned_tree(
        ssh_client,
        legacy.layout.remote_base,
        expected_files,
        f"{legacy.feature.feature_id} 旧版",
    )
    if dropin_exists:
        _assert_owned_dropin(
            ssh_client,
            legacy.layout.dropin_path,
            expected_dropin,
            f"{legacy.feature.feature_id} 旧版可见",
        )
    if check_lower:
        _check_lower_dropins(
            ssh_client,
            {legacy.layout.dropin_path: expected_dropin if dropin_exists else None},
        )
    return True


def launcher(package, files: Iterable, runtime_paths: set[str], layout: StandaloneLayout) -> str:
    checks = []
    for item in files:
        if item.path in runtime_paths:
            remote = posixpath.join(layout.remote_base, item.path)
            checks.append(
                f'[ "$(file_sha {shlex.quote(remote)})" = "{item.sha256}" ] || stock'
            )
    checks_text = "\n".join(checks)
    return f"""#!/bin/sh
BASE={shlex.quote(layout.remote_base)}

stock() {{
    logger -t {shlex.quote(layout.log_tag)} "preflight failed; starting stock xochitl" 2>/dev/null || true
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


def dropin(runtime_paths: set[str], layout: StandaloneLayout) -> str:
    conditions = [layout.launcher_path]
    conditions.extend(
        posixpath.join(layout.remote_base, path) for path in sorted(runtime_paths)
    )
    condition_lines = "\n".join(
        f"ConditionPathExists={path}" for path in conditions
    )
    return f"""[Unit]
After=home.mount
{condition_lines}

[Service]
ExecStart=
ExecStart={layout.launcher_path}
WatchdogSec=0
"""


def activation_script(stage: str, backup: str, token: str, layout: StandaloneLayout) -> str:
    mount_dir = f"/tmp/{layout.mount_tag}-rootfs-{token}"
    source_dropin = f"{layout.remote_base}/systemd/{layout.dropin_name}"
    return f"""#!/bin/sh
set -eu
BASE={shlex.quote(layout.remote_base)}
STAGE={shlex.quote(stage)}
BACKUP={shlex.quote(backup)}
DROPIN={shlex.quote(layout.dropin_path)}
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
        fi
        if [ "$HAD_BASE" -eq 1 ] && [ -d "$BACKUP" ]; then
            mv "$BACKUP" "$BASE"
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


def disable_script(token: str, layout: StandaloneLayout) -> str:
    mount_dir = f"/tmp/{layout.mount_tag}-rootfs-{token}"
    return f"""#!/bin/sh
set -eu
DROPIN={shlex.quote(layout.dropin_path)}
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


def shared_transaction_script(
    stage: str,
    token: str,
    legacy_layouts: Iterable[StandaloneLayout],
    *,
    enable_dropin: bool,
    remove_base: bool = False,
) -> str:
    layouts = tuple(legacy_layouts)
    bases = tuple(dict.fromkeys(
        (SHARED_LAYOUT.remote_base, *(layout.remote_base for layout in layouts))
    ))
    dropins = tuple(dict.fromkeys(
        (SHARED_LAYOUT.dropin_path, *(layout.dropin_path for layout in layouts))
    ))
    mount_dir = f"/tmp/rmtool-xovi-rootfs-{token}"
    backup_dir = f"/data/rmtool/.xovi-dropins-{token}"
    base_backups = tuple(f"{base}.backup-{token}" for base in bases)
    upper_backups = tuple(f"{backup_dir}/upper-{index}" for index in range(len(dropins)))
    lower_backups = tuple(f"{backup_dir}/lower-{index}" for index in range(len(dropins)))

    def backup_lines(prefix: str, paths: tuple[str, ...], backups: tuple[str, ...]) -> str:
        return "\n".join(
            f'if [ -f "{prefix}{path}" ]; then cp -p "{prefix}{path}" {shlex.quote(backup)}; fi'
            for path, backup in zip(paths, backups)
        )

    def restore_lines(prefix: str, paths: tuple[str, ...], backups: tuple[str, ...]) -> str:
        return "\n".join(
            f'if [ -f {shlex.quote(backup)} ]; then '
            f'if mkdir -p "$(dirname "{prefix}{path}")" && '
            f'cp -p {shlex.quote(backup)} "{prefix}{path}.tmp" && '
            f'mv -f "{prefix}{path}.tmp" "{prefix}{path}" && '
            f'cmp -s {shlex.quote(backup)} "{prefix}{path}"; then :; else ROLLBACK_OK=0; fi; '
            f'else if rm -f "{prefix}{path}" && [ ! -e "{prefix}{path}" ]; '
            f'then :; else ROLLBACK_OK=0; fi; fi'
            for path, backup in zip(paths, backups)
        )

    base_flags = tuple(f"MOVED_BASE_{index}" for index in range(len(bases)))
    base_backup = "\n".join(
        f'if [ -e {shlex.quote(base)} ]; then mv {shlex.quote(base)} {shlex.quote(backup)}; {flag}=1; fi'
        for base, backup, flag in zip(bases, base_backups, base_flags)
    )
    base_restore = "\n".join(
        f'if [ "${flag}" -eq 1 ]; then if rm -rf {shlex.quote(base)} && '
        f'mv {shlex.quote(backup)} {shlex.quote(base)}; then :; else ROLLBACK_OK=0; fi; fi'
        for base, backup, flag in zip(bases, base_backups, base_flags)
    )
    cleanup_backups = " ".join(shlex.quote(path) for path in base_backups)
    remove_upper = "\n".join(f"rm -f {shlex.quote(path)}" for path in dropins)
    remove_lower = "\n".join(f'rm -f "$MOUNT_DIR{path}"' for path in dropins)
    source_dropin = f"{SHARED_LAYOUT.remote_base}/systemd/{SHARED_LAYOUT.dropin_name}"
    if enable_dropin:
        write_upper = f"""mkdir -p "$(dirname {shlex.quote(SHARED_LAYOUT.dropin_path)})"
cp {shlex.quote(source_dropin)} {shlex.quote(SHARED_LAYOUT.dropin_path + '.tmp')}
chmod 0644 {shlex.quote(SHARED_LAYOUT.dropin_path + '.tmp')}
mv -f {shlex.quote(SHARED_LAYOUT.dropin_path + '.tmp')} {shlex.quote(SHARED_LAYOUT.dropin_path)}"""
        write_lower = f"""mkdir -p "$MOUNT_DIR$(dirname {shlex.quote(SHARED_LAYOUT.dropin_path)})"
cp {shlex.quote(source_dropin)} "$MOUNT_DIR{SHARED_LAYOUT.dropin_path}.tmp"
chmod 0644 "$MOUNT_DIR{SHARED_LAYOUT.dropin_path}.tmp"
mv -f "$MOUNT_DIR{SHARED_LAYOUT.dropin_path}.tmp" "$MOUNT_DIR{SHARED_LAYOUT.dropin_path}"
cmp -s {shlex.quote(source_dropin)} {shlex.quote(SHARED_LAYOUT.dropin_path)}
cmp -s {shlex.quote(source_dropin)} "$MOUNT_DIR{SHARED_LAYOUT.dropin_path}"
"""
    else:
        write_upper = ":"
        write_lower = ":"
    remove_shared_base = 'rmdir "$BASE"' if remove_base else ":"

    return f"""#!/bin/sh
set -eu
STAGE={shlex.quote(stage)}
BASE={shlex.quote(SHARED_LAYOUT.remote_base)}
MOUNT_DIR={shlex.quote(mount_dir)}
BACKUP_DIR={shlex.quote(backup_dir)}
MOUNTED=0
COMMITTED=0
STAGE_MOVED=0
DROPINS_SNAPSHOTTED=0
ROOT_RELEASED=1
ROLLBACK_OK=1
{chr(10).join(flag + '=0' for flag in base_flags)}

unmount_root() {{
    [ "$MOUNTED" -eq 1 ] || return 0
    sync
    mount -o remount,ro "$MOUNT_DIR" || return 1
    umount "$MOUNT_DIR" || return 1
    MOUNTED=0
    rmdir "$MOUNT_DIR"
}}

mount_root_rw() {{
    mkdir -p "$MOUNT_DIR"
    mount --bind / "$MOUNT_DIR"
    MOUNTED=1
    mount -o remount,rw "$MOUNT_DIR"
}}

rollback() {{
    rc=$?
    [ "$rc" -ne 0 ] || rc=1
    trap - EXIT INT TERM
    if [ "$COMMITTED" -eq 0 ]; then
        if [ "$MOUNTED" -eq 1 ]; then
            sync
            mount -o remount,ro "$MOUNT_DIR" 2>/dev/null || true
            if umount "$MOUNT_DIR" 2>/dev/null; then
                MOUNTED=0
            else
                ROOT_RELEASED=0
                ROLLBACK_OK=0
            fi
        fi
        if [ "$STAGE_MOVED" -eq 1 ]; then rm -rf "$BASE" || ROLLBACK_OK=0; fi
        {base_restore}
        if [ "$DROPINS_SNAPSHOTTED" -eq 1 ]; then
            {restore_lines('', dropins, upper_backups)}
            if [ "$ROOT_RELEASED" -eq 1 ] && mount_root_rw 2>/dev/null; then
                {restore_lines('$MOUNT_DIR', dropins, lower_backups)}
                if ! unmount_root 2>/dev/null; then ROOT_RELEASED=0; ROLLBACK_OK=0; fi
            else
                ROLLBACK_OK=0
                if [ "$MOUNTED" -eq 1 ]; then
                    mount -o remount,ro "$MOUNT_DIR" 2>/dev/null || true
                    if umount "$MOUNT_DIR" 2>/dev/null; then MOUNTED=0; else ROOT_RELEASED=0; fi
                fi
            fi
        fi
        if [ "$ROOT_RELEASED" -eq 1 ]; then
            systemctl daemon-reload 2>/dev/null || ROLLBACK_OK=0
        fi
    fi
    rm -rf "$STAGE" || ROLLBACK_OK=0
    if [ "$ROLLBACK_OK" -eq 1 ] && [ "$ROOT_RELEASED" -eq 1 ]; then
        rm -rf "$BACKUP_DIR" {cleanup_backups}
        rmdir "$MOUNT_DIR" 2>/dev/null || true
    else
        echo "rmtool Xovi rollback incomplete; recovery kept at $BACKUP_DIR and {cleanup_backups}" >&2
    fi
    exit "$rc"
}}
trap rollback EXIT INT TERM

rm -rf "$BACKUP_DIR" {cleanup_backups}
mkdir -p "$BACKUP_DIR"
{backup_lines('', dropins, upper_backups)}
mount_root_rw
{backup_lines('$MOUNT_DIR', dropins, lower_backups)}
unmount_root
DROPINS_SNAPSHOTTED=1

{base_backup}
mv "$STAGE" "$BASE"
STAGE_MOVED=1

{remove_upper}
{write_upper}
mount_root_rw
{remove_lower}
{write_lower}
unmount_root
{remove_shared_base}
systemctl daemon-reload

COMMITTED=1
rm -rf "$BACKUP_DIR" {cleanup_backups}
trap - EXIT INT TERM
"""


def _upload_bytes(ssh_client, data: bytes, remote: str, mode: int) -> None:
    with tempfile.NamedTemporaryFile(delete=False) as temporary:
        temporary.write(data)
        local = temporary.name
    try:
        ssh_client.transfer_file(local, remote)
        ssh_client.exec_checked(f"chmod {mode:o} {shlex.quote(remote)}")
    finally:
        try:
            os.unlink(local)
        except OSError:
            pass


def _upload_path(ssh_client, local: Path, remote: str, mode: int) -> None:
    ssh_client.transfer_file(str(local), remote)
    ssh_client.exec_checked(f"chmod {mode:o} {shlex.quote(remote)}")


def _qmd_check_command(stage: str, enabled: Iterable[SharedFeatureSpec]) -> str:
    check = f"{stage}/check"
    copies = " && ".join(
        f"cp {shlex.quote(stage + '/' + item.runtime_path)} "
        f"{shlex.quote(check + '/qmd/' + feature.feature_id + '-' + str(index) + '.qmd')}"
        for feature in sorted(enabled, key=lambda value: value.feature_id)
        for index, item in enumerate(
            (item for item in feature.files if item.runtime_path.endswith('.qmd')),
            start=1,
        )
    )
    return (
        f"mkdir -p {shlex.quote(check + '/hashtabs')} {shlex.quote(check + '/qmd')} && "
        f"cp {shlex.quote(stage + '/' + SHARED_QRR_HOME + '/hashtab')} "
        f"{shlex.quote(check + '/hashtabs/hashtab-device')} && "
        + (copies + " && " if copies else "")
        + f"{shlex.quote(stage + '/qmd-tool')} check -hashtabs "
        f"{shlex.quote(check + '/hashtabs')} -qmd {shlex.quote(check + '/qmd')}"
    )


def _stage_shared(
    ssh_client,
    runtime: SharedRuntimeSpec,
    states: Mapping[str, SharedFeatureState],
    incoming: SharedFeatureSpec,
    extracted_root: Path,
    previous_sources: Mapping[str, str],
    stage: str,
) -> tuple[str, str]:
    enabled = tuple(state.spec for state in states.values() if state.enabled)
    launcher_text = shared_launcher(runtime, enabled)
    dropin_text = shared_dropin(runtime, enabled)
    launcher_sha = hashlib.sha256(launcher_text.encode()).hexdigest()
    dropin_sha = hashlib.sha256(dropin_text.encode()).hexdigest()
    marker = shared_marker(runtime, states, launcher_sha, dropin_sha)
    expected = {item.path: item for item in runtime.files}
    expected.update({
        item.runtime_path: SharedFileSpec(
            item.runtime_path, item.sha256, item.size, item.mode
        )
        for feature in enabled
        for item in feature.files
    })
    directories = {stage, f"{stage}/systemd"}
    directories.update(posixpath.dirname(f"{stage}/{path}") for path in expected)
    ssh_client.exec_checked(
        "mkdir -p " + " ".join(shlex.quote(path) for path in sorted(directories))
    )
    for item in runtime.files:
        local = extracted_root.joinpath(*PurePosixPath(item.path).parts)
        _upload_path(ssh_client, local, f"{stage}/{item.path}", item.mode)
    for feature in enabled:
        for item in feature.files:
            remote = f"{stage}/{item.runtime_path}"
            if feature.feature_id == incoming.feature_id:
                local = extracted_root.joinpath(*PurePosixPath(item.archive_path).parts)
                _upload_path(ssh_client, local, remote, item.mode)
            else:
                source = previous_sources.get(item.runtime_path)
                if source is None and item.runtime_path == feature.runtime_path:
                    source = previous_sources.get(feature.feature_id)
                if source is None:
                    raise RuntimeError("无法从已验证安装中保留另一项功能。")
                ssh_client.exec_checked(
                    f"cp {shlex.quote(source)} {shlex.quote(remote)} && chmod {item.mode:o} {shlex.quote(remote)}"
                )
    _upload_bytes(ssh_client, launcher_text.encode(), f"{stage}/launcher.sh", 0o755)
    _upload_bytes(
        ssh_client,
        dropin_text.encode(),
        f"{stage}/systemd/{SHARED_LAYOUT.dropin_name}",
        0o644,
    )
    _upload_bytes(ssh_client, marker, f"{stage}/package.json", 0o644)
    ssh_client.exec_checked(f"chown -R root:root {shlex.quote(stage)}")
    for path, item in expected.items():
        remote = f"{stage}/{path}"
        if _remote_sha256(ssh_client, remote) != item.sha256:
            raise RuntimeError(f"设备端共享 Xovi 资源 {path} 上传校验失败。")
    for path, digest in (
        ("launcher.sh", launcher_sha),
        (f"systemd/{SHARED_LAYOUT.dropin_name}", dropin_sha),
        ("package.json", hashlib.sha256(marker).hexdigest()),
    ):
        if _remote_sha256(ssh_client, f"{stage}/{path}") != digest:
            raise RuntimeError(f"设备端共享 Xovi 文件 {path} 上传校验失败。")
    if enabled:
        ssh_client.exec_checked(_qmd_check_command(stage, enabled))
    ssh_client.exec_checked(f"rm -rf {shlex.quote(stage + '/check')}")
    return launcher_sha, dropin_sha


def enable_shared(
    ssh_client,
    runtime: SharedRuntimeSpec,
    feature: SharedFeatureSpec,
    extracted_root: str | Path,
    trusted: Mapping[str, SharedFeatureSpec],
    legacy_specs: Iterable[LegacyStandaloneSpec],
) -> SharedInspection:
    with _operation_lock(ssh_client):
        return _enable_shared_locked(
            ssh_client, runtime, feature, extracted_root, trusted, legacy_specs
        )


def _enable_shared_locked(
    ssh_client,
    runtime: SharedRuntimeSpec,
    feature: SharedFeatureSpec,
    extracted_root: str | Path,
    trusted: Mapping[str, SharedFeatureSpec],
    legacy_specs: Iterable[LegacyStandaloneSpec],
) -> SharedInspection:
    if set(trusted) - {"tap-page-turn", "fast-mono-reading", "native-chinese"}:
        raise RuntimeError("共享 Xovi 包含不受支持的功能。")
    assert_feature_layout(runtime, trusted.values())
    legacy_specs = tuple(legacy_specs)
    _assert_managed_dropins(
        ssh_client,
        (SHARED_LAYOUT.dropin_path, *(item.layout.dropin_path for item in legacy_specs)),
    )
    assert_common_runtime(runtime, {
        feature_id: (legacy.runtime, legacy.feature)
        for feature_id, legacy in ((item.feature.feature_id, item) for item in legacy_specs)
        if feature_id in trusted
    })
    shared_exists = has_shared_artifacts(ssh_client)
    inspection = inspect_shared(ssh_client, runtime, trusted, check_lower=True) if shared_exists else SharedInspection({}, False, False)
    present_legacy = [item for item in legacy_specs if validate_legacy(ssh_client, item)]
    if shared_exists and present_legacy:
        raise RuntimeError("检测到共享与旧版独立 Xovi 混合布局，拒绝修改。")
    if len(present_legacy) > 1:
        raise RuntimeError("检测到两套旧版独立 Xovi 布局，拒绝自动合并。")
    if (
        feature.feature_id in inspection.states
        and inspection.states[feature.feature_id].enabled
        and inspection.layout == SHARED_LAYOUT
    ):
        return inspection
    current = _process_token(ssh_client)
    states = dict(inspection.states)
    previous_sources = {
        item.runtime_path: f"{inspection.layout.remote_base}/{item.runtime_path}"
        for state in inspection.states.values() if state.enabled
        for item in state.spec.files
    }
    previous_sources.update({
        state.spec.feature_id: f"{inspection.layout.remote_base}/{state.spec.runtime_path}"
        for state in inspection.states.values() if state.enabled
    })
    for legacy in present_legacy:
        enabled = ssh_client.file_exists(legacy.layout.dropin_path)
        states[legacy.feature.feature_id] = SharedFeatureState(
            legacy.feature, enabled, current
        )
        if enabled:
            for item in legacy.feature.files:
                previous_sources[item.runtime_path] = posixpath.join(
                    legacy.layout.remote_base, item.archive_path
                )
            previous_sources[legacy.feature.feature_id] = posixpath.join(
                legacy.layout.remote_base, legacy.feature.archive_path
            )
    states[feature.feature_id] = SharedFeatureState(feature, True, current)
    enabled_specs = tuple(state.spec for state in states.values() if state.enabled)
    feature_paths = [
        item.runtime_path for spec in enabled_specs for item in spec.files
    ]
    if len(set(feature_paths)) != len(feature_paths):
        raise RuntimeError("共享 Xovi 功能路径发生冲突。")

    token = uuid.uuid4().hex
    stage = f"{SHARED_LAYOUT.remote_base}.staging-{token}"
    remote_script = f"/tmp/rmtool-xovi-activate-{token}.sh"
    ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)}")
    try:
        _stage_shared(
            ssh_client, runtime, states, feature, Path(extracted_root), previous_sources, stage
        )
        migrated_layouts = [item.layout for item in present_legacy]
        if inspection.states and inspection.layout != SHARED_LAYOUT:
            migrated_layouts.append(inspection.layout)
        script = shared_transaction_script(
            stage, token, migrated_layouts, enable_dropin=True
        )
        _upload_bytes(ssh_client, script.encode(), remote_script, 0o755)
        ssh_client.exec_checked(f"/bin/sh {shlex.quote(remote_script)}")
    except Exception:
        try:
            ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)}")
        except Exception:
            logging.exception("Could not clean shared Xovi staging directory")
        raise
    finally:
        try:
            ssh_client.exec_checked(f"rm -f {shlex.quote(remote_script)}")
        except Exception:
            logging.exception("Could not remove shared Xovi transaction script")
    return inspect_shared(ssh_client, runtime, trusted)


def disable_shared(
    ssh_client,
    runtime: SharedRuntimeSpec,
    feature_id: str,
    trusted: Mapping[str, SharedFeatureSpec],
    replacement_spec: Optional[SharedFeatureSpec] = None,
) -> SharedInspection:
    with _operation_lock(ssh_client):
        return _disable_shared_locked(
            ssh_client,
            runtime,
            feature_id,
            trusted,
            replacement_spec,
        )


def _disable_shared_locked(
    ssh_client,
    runtime: SharedRuntimeSpec,
    feature_id: str,
    trusted: Mapping[str, SharedFeatureSpec],
    replacement_spec: Optional[SharedFeatureSpec] = None,
) -> SharedInspection:
    inspection = inspect_shared(ssh_client, runtime, trusted, check_lower=True)
    state = inspection.states.get(feature_id)
    if state is None:
        raise RuntimeError("该功能尚未安装。")
    if not state.enabled and replacement_spec is None:
        return inspection
    if replacement_spec is not None and (
        replacement_spec.feature_id != feature_id
        or replacement_spec.package_id != state.spec.package_id
        or replacement_spec.runtime_path != state.spec.runtime_path
    ):
        raise RuntimeError("共享 Xovi 旧版功能替换规则无效。")
    current = _process_token(ssh_client) if state.enabled else state.process_token
    states = dict(inspection.states)
    states[feature_id] = SharedFeatureState(
        replacement_spec or state.spec,
        False,
        current,
    )
    target_trusted = dict(trusted)
    if replacement_spec is not None:
        target_trusted[feature_id] = replacement_spec
    enabled = tuple(item.spec for item in states.values() if item.enabled)
    token = uuid.uuid4().hex
    stage = f"{SHARED_LAYOUT.remote_base}.staging-{token}"
    remote_script = f"/tmp/rmtool-xovi-disable-{token}.sh"
    ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)}")
    try:
        ssh_client.exec_checked(
            f"cp -a {shlex.quote(inspection.layout.remote_base)} {shlex.quote(stage)}"
        )
        ssh_client.exec_checked(
            f"rm -f {shlex.quote(stage + '/startup.pending')}"
        )
        launcher_text = shared_launcher(runtime, enabled)
        dropin_text = shared_dropin(runtime, enabled)
        launcher_sha = hashlib.sha256(launcher_text.encode()).hexdigest()
        dropin_sha = hashlib.sha256(dropin_text.encode()).hexdigest()
        marker = shared_marker(runtime, states, launcher_sha, dropin_sha)
        if enabled:
            for item in state.spec.files:
                ssh_client.exec_checked(
                    f"rm -f {shlex.quote(stage + '/' + item.runtime_path)}"
                )
                ssh_client.exec_checked(
                    f"test ! -e {shlex.quote(stage + '/' + item.runtime_path)}"
                )
            _upload_bytes(
                ssh_client, launcher_text.encode(), f"{stage}/launcher.sh", 0o755
            )
            _upload_bytes(
                ssh_client,
                dropin_text.encode(),
                f"{stage}/systemd/{SHARED_LAYOUT.dropin_name}",
                0o644,
            )
        else:
            ssh_client.exec_checked(
                f"find {shlex.quote(stage)} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +"
            )
        _upload_bytes(ssh_client, marker, f"{stage}/package.json", 0o644)
        ssh_client.exec_checked(f"chown -R root:root {shlex.quote(stage)}")
        expected_generated = [("package.json", hashlib.sha256(marker).hexdigest())]
        if enabled:
            expected_generated.extend(
                (
                    ("launcher.sh", launcher_sha),
                    (f"systemd/{SHARED_LAYOUT.dropin_name}", dropin_sha),
                )
            )
        for path, digest in expected_generated:
            if _remote_sha256(ssh_client, f"{stage}/{path}") != digest:
                raise RuntimeError(f"设备端共享 Xovi 文件 {path} 更新校验失败。")
        for peer in enabled:
            for item in peer.files:
                if _remote_sha256(
                    ssh_client, f"{stage}/{item.runtime_path}"
                ) != item.sha256:
                    raise RuntimeError(f"共享 Xovi 未能完整保留 {peer.feature_id}。")
        if enabled:
            ssh_client.exec_checked(_qmd_check_command(stage, enabled))
        ssh_client.exec_checked(f"rm -rf {shlex.quote(stage + '/check')}")
        migrated_layouts = (
            (inspection.layout,) if inspection.layout != SHARED_LAYOUT else ()
        )
        script = shared_transaction_script(
            stage,
            token,
            migrated_layouts,
            enable_dropin=bool(enabled),
        )
        _upload_bytes(ssh_client, script.encode(), remote_script, 0o755)
        ssh_client.exec_checked(f"/bin/sh {shlex.quote(remote_script)}")
    except Exception:
        try:
            ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)}")
        except Exception:
            logging.exception("Could not clean shared Xovi disable staging directory")
        raise
    finally:
        try:
            ssh_client.exec_checked(f"rm -f {shlex.quote(remote_script)}")
        except Exception:
            logging.exception("Could not remove shared Xovi disable script")
    return inspect_shared(ssh_client, runtime, target_trusted)


def remove_shared_firmware_residue(
    ssh_client,
    runtime: SharedRuntimeSpec,
    trusted: Mapping[str, SharedFeatureSpec],
    current_identity: tuple[str, str, str, str],
) -> SharedInspection:
    with _operation_lock(ssh_client):
        inspection = inspect_shared_firmware_residue(
            ssh_client,
            runtime,
            trusted,
            current_identity,
        )
        token = uuid.uuid4().hex
        stage = f"{SHARED_LAYOUT.remote_base}.staging-{token}"
        remote_script = f"/tmp/rmtool-xovi-remove-residue-{token}.sh"
        ssh_client.exec_checked(
            f"rm -rf {shlex.quote(stage)}; mkdir -m 0755 {shlex.quote(stage)}; "
            f"chown root:root {shlex.quote(stage)}"
        )
        try:
            script = shared_transaction_script(
                stage,
                token,
                (inspection.layout,) if inspection.layout != SHARED_LAYOUT else (),
                enable_dropin=False,
                remove_base=True,
            )
            _upload_bytes(ssh_client, script.encode(), remote_script, 0o755)
            ssh_client.exec_checked(f"/bin/sh {shlex.quote(remote_script)}")
        except Exception:
            try:
                ssh_client.exec_checked(f"rm -rf {shlex.quote(stage)}")
            except Exception:
                logging.exception("Could not clean shared Xovi residue staging directory")
            raise
        finally:
            try:
                ssh_client.exec_checked(f"rm -f {shlex.quote(remote_script)}")
            except Exception:
                logging.exception("Could not remove shared Xovi residue cleanup script")
    return SharedInspection({}, False, False)


def remove_verified_legacy(
    ssh_client,
    legacy: LegacyStandaloneSpec,
) -> None:
    with _operation_lock(ssh_client):
        if not validate_legacy(ssh_client, legacy, check_lower=True):
            raise RuntimeError(f"{legacy.feature.feature_id} 旧版安装不存在。")
        token = uuid.uuid4().hex
        remote_script = f"/tmp/rmtool-xovi-remove-legacy-{token}.sh"
        try:
            _upload_bytes(
                ssh_client,
                disable_script(token, legacy.layout).encode(),
                remote_script,
                0o755,
            )
            ssh_client.exec_checked(f"/bin/sh {shlex.quote(remote_script)}")
            _check_lower_dropins(
                ssh_client, {legacy.layout.dropin_path: None}
            )
            ssh_client.exec_checked(
                f"rm -rf {shlex.quote(legacy.layout.remote_base)}; "
                f"test ! -e {shlex.quote(legacy.layout.remote_base)}"
            )
        finally:
            try:
                ssh_client.exec_checked(f"rm -f {shlex.quote(remote_script)}")
            except Exception:
                logging.exception("Could not remove legacy Xovi cleanup script")
