"""Quarantine and rebuild recognized shared installations without trusting old code.

Only the /data shared layout is recovered. Legacy /home layouts, mounted-over
systemd paths, and enabled external programs (AppLoad/KOReader or sidecars) need
separate ownership/rollback support and remain blocked when repair is needed.
An active drop-in with a corrupted launcher is also blocked: restoring that
launcher on rollback cannot be made safe by the emergency sentinel alone.
External settings, fonts, and books are never edited. Pre-existing emergency
sentinels remain intact; a temporary repair latch stays armed on failure and is
cleared only after strict post-repair verification. Stale startup.pending stays
in the quarantined original, never in the fresh installation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import stat
import tempfile
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from enum import Enum
from itertools import product
from pathlib import Path, PurePosixPath

import _residue_migration as migration
import _tap_page_turn as tap
import _xovi_standalone as shared


_MAX_MARKER_BYTES = 256 * 1024


class RecoveryState(str, Enum):
    NOT_NEEDED = "not_needed"
    REPAIR_AVAILABLE = "repair_available"
    CLEANUP_AVAILABLE = "cleanup_available"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class RecoveryReport:
    state: RecoveryState
    detail: str
    features: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    backup_path: str = ""

    @property
    def can_repair(self) -> bool:
        return self.state == RecoveryState.REPAIR_AVAILABLE

    @property
    def can_cleanup(self) -> bool:
        return self.state == RecoveryState.CLEANUP_AVAILABLE


@dataclass(frozen=True)
class _Plan:
    identity: tap.DeviceIdentity
    runtime: shared.SharedRuntimeSpec
    targets: dict[str, shared.SharedFeatureSpec]
    states: dict[str, shared.SharedFeatureState]
    fingerprint: tuple
    sentinels: tuple[str, ...]


@dataclass(frozen=True)
class _CleanupPlan:
    base_entries: tuple[tuple[str, int, int, str], ...]
    dropin: tuple[str, int, int, str] | None
    lower_dropin: tuple[str, int, int, str] | None
    sentinels: tuple[tuple[str, int, int, str], ...]
    features: tuple[str, ...]

    @property
    def fingerprint(self) -> tuple:
        return (self.base_entries, self.dropin, self.lower_dropin, self.sentinels)


def _published_predecessors(identity, trusted):
    # This existing aggregator calls the feature-owned published definitions,
    # including multi-file Pinyin/Chinese revisions. No marker-supplied digests.
    return migration.note._peer_revisions(identity, trusted)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("共享插件标记包含重复字段。")
        result[key] = value
    return result


def _read_marker(ssh_client, path):
    with ssh_client.open_remote(path, "r") as remote:
        data = remote.read(_MAX_MARKER_BYTES + 1)
    if len(data) > _MAX_MARKER_BYTES:
        raise RuntimeError("共享插件标记超过安全大小限制。")
    return data.decode("utf-8") if isinstance(data, bytes) else data


def _known_dropin_hashes(runtime, states):
    features = tuple(state.spec for state in states.values())
    return {
        hashlib.sha256(shared.shared_dropin(
            runtime, tuple(feature for feature, enabled in zip(features, flags) if enabled),
        ).encode()).hexdigest()
        for flags in product((False, True), repeat=len(features))
    }


def _recognized_marker(marker, runtime, trusted, revisions):
    if not isinstance(marker, dict) or type(marker.get("schema_version")) is not int:
        raise RuntimeError("共享插件标记格式无效，不能确认归属。")
    schema_version = marker["schema_version"]
    records = marker.get("features")
    if not isinstance(records, dict) or not records or not set(records) <= set(trusted):
        raise RuntimeError("共享插件标记包含未知功能，拒绝自动修复。")
    if schema_version == 2:
        states = shared._parse_receipt_states(marker, runtime, trusted)
        published = all(
            state.spec
            in {
                trusted[feature_id],
                *(item[1] for item in revisions.get(feature_id, ())),
            }
            for feature_id, state in states.items()
        )
        if not published and not shared._managed_receipt_is_known(marker):
            raise RuntimeError(
                "共享插件本地测试版没有当前电脑的完成安装登记。"
            )
        state_sets = (states,)
    elif schema_version == 1:
        choices = []
        for feature_id, record in sorted(records.items()):
            candidates = (
                trusted[feature_id],
                *(item[1] for item in revisions.get(feature_id, ())),
            )
            matches = []
            for spec in dict.fromkeys(candidates):
                try:
                    state = shared._parse_states(
                        {"features": {feature_id: record}}, {feature_id: spec}
                    )
                except RuntimeError:
                    continue
                matches.append(state[feature_id])
            if not matches:
                raise RuntimeError(f"{feature_id} 标记不属于已发布的受信版本。")
            choices.append(matches)
        state_sets = (
            {state.spec.feature_id: state for state in combination}
            for combination in product(*choices)
        )
    else:
        raise RuntimeError("共享插件标记版本无效，不能确认归属。")
    for states in state_sets:
        shared.assert_feature_layout(runtime, (state.spec for state in states.values()))
        enabled = tuple(state.spec for state in states.values() if state.enabled)
        dropin = shared.shared_dropin(runtime, enabled)
        dropin_sha = hashlib.sha256(dropin.encode()).hexdigest()
        for guard, sentinel, unmatched in (
            (True, True, False), (False, True, False), (False, False, False),
            (True, True, True), (False, True, True), (False, False, True),
        ):
            launcher = shared.shared_launcher(
                runtime, enabled, startup_guard=guard,
                recovery_sentinel=sentinel, legacy_unmatched_qmd_glob=unmatched,
            )
            launcher_sha = hashlib.sha256(launcher.encode()).hexdigest()
            if marker == shared._marker_document(
                runtime,
                states,
                launcher_sha,
                dropin_sha,
                schema_version=schema_version,
            ):
                if type(marker["runtime_present"]) is not bool:
                    break
                return states
    raise RuntimeError("共享插件标记不能由内置已发布模板重建，拒绝信任自报哈希。")


def _metadata(ssh_client, path, *, ancestor_directory=False):
    value = ssh_client.exec_checked(
        f"stat -c '%f|%u|%g|%s|%h' {shlex.quote(path)}"
    ).strip().split("|")
    try:
        mode, uid, gid, size, links = int(value[0], 16), *(int(v) for v in value[1:])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"无法验证路径元数据：{path}") from exc
    unsafe_permissions = mode & (0o7002 if ancestor_directory else 0o7022)
    if uid != 0 or gid != 0 or unsafe_permissions:
        raise RuntimeError(f"路径所有者或权限不安全：{path}")
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)) or (stat.S_ISREG(mode) and links != 1):
        raise RuntimeError(f"路径为符号链接、特殊文件或硬链接：{path}")
    if ancestor_directory and not stat.S_ISDIR(mode):
        raise RuntimeError(f"父路径不是安全目录：{path}")
    return mode, size


def _ancestors(ssh_client, path):
    for parent in reversed(PurePosixPath(path).parents):
        if shared._remote_entry_exists(ssh_client, str(parent)):
            _metadata(ssh_client, str(parent), ancestor_directory=True)


def _unhidden_paths(ssh_client):
    # A bind of / in the transaction exposes lower-root systemd files. Without
    # temporary inspection mounts we cannot authenticate hidden lower copies.
    # Refuse such layouts rather than describe the read-only check as complete.
    mounts = shared._remote_text(ssh_client, "/proc/self/mountinfo").splitlines()
    if not mounts:
        raise RuntimeError("无法读取挂载边界，拒绝自动修复。")
    root_seen = False
    for line in mounts:
        fields = line.split()
        if len(fields) < 10 or "-" not in fields or fields.index("-") + 3 >= len(fields):
            raise RuntimeError("挂载边界格式无效。")
        path = fields[4]
        root_seen |= path == "/"
        if path == "/" and fields[fields.index("-") + 1] == "overlay":
            raise RuntimeError("根目录为 overlay，无法只读确认底层配置归属。")
        for target in (shared.SHARED_LAYOUT.dropin_path, shared.SHARED_LAYOUT.remote_base):
            if path == "/" or path == "/data":
                continue
            if target == path or target.startswith(path + "/") or path.startswith(target + "/"):
                raise RuntimeError(f"插件路径存在独立挂载，无法确认底层归属：{path}")
    if not root_seen:
        raise RuntimeError("无法确认根挂载边界。")


def _external_loaders(ssh_client):
    directory = str(PurePosixPath(shared.SHARED_LAYOUT.dropin_path).parent)
    if shared._remote_entry_exists(ssh_client, directory):
        for path in ssh_client.exec_checked(
            f"find -P {shlex.quote(directory)} -mindepth 1 -maxdepth 1 -print"
        ).splitlines():
            if not path.startswith(directory + "/") or "/" in path[len(directory) + 1:]:
                raise RuntimeError("xochitl 配置路径越界。")
            if path.endswith(".conf") and path != shared.SHARED_LAYOUT.dropin_path:
                raise RuntimeError(f"xochitl 存在未知活动 drop-in，拒绝自动修复：{path}")
            mode, _size = _metadata(ssh_client, path)
            if not stat.S_ISREG(mode):
                raise RuntimeError(f"xochitl 配置存在未知目录：{path}")
    shared._assert_managed_dropins(ssh_client, (shared.SHARED_LAYOUT.dropin_path,))
    maps = ssh_client.exec_checked(
        'pid=$(systemctl show xochitl -p MainPID --value); '
        'if [ -n "$pid" ] && [ "$pid" != 0 ]; then cat /proc/$pid/maps; fi'
    )
    for line in maps.splitlines():
        if any(name in line.lower() for name in ("xovi", "qt-resource-rebuilder", "appload")):
            fields = line.split(None, 5)
            if len(fields) < 6 or not fields[5].startswith(shared.SHARED_LAYOUT.remote_base + "/"):
                raise RuntimeError("xochitl 正在加载未知外部插件，拒绝自动修复。")


def _assert_no_symlink_chain(ssh_client, path):
    candidates = tuple(reversed(PurePosixPath(path).parents)) + (PurePosixPath(path),)
    for candidate in candidates:
        ssh_client.exec_checked(f"[ ! -L {shlex.quote(str(candidate))} ]")


def _trusted_cleanup_contexts(identity):
    candidates = [identity]
    seen = {identity}
    for module in (tap, *migration._providers().values()):
        try:
            catalog = module._trusted_catalog()
        except (AttributeError, RuntimeError, OSError, ValueError, TypeError):
            continue
        for package in catalog:
            fields = tuple(
                getattr(package, name, None)
                for name in ("firmware", "platform", "architecture", "xochitl_sha256")
            )
            if any(value is None for value in fields):
                continue
            candidate = tap.DeviceIdentity(*fields)
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    contexts = []
    seen_runtimes = set()
    for candidate in candidates:
        try:
            runtime, trusted, _legacies = tap._trusted_shared_context(candidate)
        except (RuntimeError, OSError, ValueError, TypeError):
            continue
        if runtime is None or not trusted:
            continue
        key = (runtime, tuple(sorted(trusted.items())))
        if key in seen_runtimes:
            continue
        seen_runtimes.add(key)
        contexts.append((runtime, trusted))
    return tuple(contexts)


def _cleanup_trust_data(contexts):
    allowed_files = {"package.json", "startup.pending"}
    allowed_dirs = set()
    core_specs = {}
    file_specs = {}
    launcher_specs = set()
    dropin_hashes = set()

    def add_spec(path, digest, size, mode):
        allowed_files.add(path)
        file_specs.setdefault(path, set()).add((digest, size, mode))

    for runtime, trusted in contexts:
        for item in runtime.files:
            add_spec(item.path, item.sha256, item.size, item.mode)
            core_specs.setdefault(item.path, set()).add((item.sha256, item.size, item.mode))
        feature_values = tuple(trusted.values())
        for feature in feature_values:
            for item in feature.files:
                add_spec(item.runtime_path, item.sha256, item.size, item.mode)
        for flags in product((False, True), repeat=len(feature_values)):
            enabled = tuple(
                feature for feature, flag in zip(feature_values, flags) if flag
            )
            for recovery_sentinel, startup_guard, unmatched in product(
                (False, True), repeat=3
            ):
                try:
                    launcher = shared.shared_launcher(
                        runtime,
                        enabled,
                        recovery_sentinel=recovery_sentinel,
                        startup_guard=startup_guard,
                        legacy_unmatched_qmd_glob=unmatched,
                    ).encode()
                except (RuntimeError, OSError, ValueError, TypeError):
                    continue
                launcher_specs.add(
                    (hashlib.sha256(launcher).hexdigest(), len(launcher), 0o755)
                )
        try:
            dropin = shared.shared_dropin(runtime, ())
        except (RuntimeError, OSError, ValueError, TypeError):
            continue
        dropin_bytes = dropin.encode()
        dropin_hashes.add(hashlib.sha256(dropin_bytes).hexdigest())

    add_spec("launcher.sh", "", -1, 0o755)
    file_specs["launcher.sh"] = launcher_specs
    dropin_path = f"systemd/{shared.SHARED_LAYOUT.dropin_name}"
    add_spec(dropin_path, "", -1, 0o644)
    file_specs[dropin_path] = {
        (digest, -1, 0o644) for digest in dropin_hashes
    }
    allowed_dirs.update(shared._parent_directories(allowed_files))
    allowed_dirs.discard("")
    return allowed_files, allowed_dirs, core_specs, file_specs, dropin_hashes


def _cleanup_tree_snapshot(ssh_client, contexts):
    base = shared.SHARED_LAYOUT.remote_base
    allowed_files, allowed_dirs, core_specs, file_specs, _dropin_hashes = (
        _cleanup_trust_data(contexts)
    )
    if not shared._remote_entry_exists(ssh_client, base):
        return (), 0
    _ancestors(ssh_client, base)
    ssh_client.exec_checked(
        f"[ ! -L {shlex.quote(base)} ] && "
        f"! find -P {shlex.quote(base)} -mindepth 1 -type l -print -quit | grep -q ."
    )
    paths = ssh_client.exec_checked(f"find -P {shlex.quote(base)} -print").splitlines()
    if not paths or paths[0] != base or len(paths) != len(set(paths)):
        raise RuntimeError("共享 Xovi 残留目录清单无效。")
    snapshot = []
    core_matches = 0
    for index, path in enumerate(paths):
        if index == 0:
            relative = ""
        else:
            prefix = base + "/"
            if not path.startswith(prefix):
                raise RuntimeError("共享 Xovi 残留包含越界路径。")
            relative = path[len(prefix):]
        mode, size = _metadata(ssh_client, path)
        if stat.S_ISDIR(mode):
            if relative not in allowed_dirs and relative != "":
                raise RuntimeError(f"共享 Xovi 残留包含未知目录：{relative}")
            if stat.S_IMODE(mode) != 0o755:
                raise RuntimeError(f"共享 Xovi 残留目录权限不安全：{path}")
            snapshot.append((path, mode, size, ""))
            continue
        if not stat.S_ISREG(mode) or relative not in allowed_files:
            raise RuntimeError(f"共享 Xovi 残留包含未知文件：{relative}")
        digest = shared._remote_sha256(ssh_client, path)
        if relative == "package.json":
            if stat.S_IMODE(mode) != 0o644 or size > _MAX_MARKER_BYTES:
                raise RuntimeError("共享 Xovi 残留标记类型或权限不安全。")
        elif relative == "startup.pending":
            if stat.S_IMODE(mode) != 0o600 or size != 0 or digest != shared._EMPTY_SHA256:
                raise RuntimeError("共享 Xovi 残留启动标记不安全。")
        else:
            matches = file_specs.get(relative, set())
            if not any(
                digest == expected_digest
                and (expected_size < 0 or size == expected_size)
                and stat.S_IMODE(mode) == expected_mode
                for expected_digest, expected_size, expected_mode in matches
            ):
                raise RuntimeError(f"共享 Xovi 残留文件指纹未知：{relative}")
            if (digest, size, stat.S_IMODE(mode)) in core_specs.get(relative, set()):
                core_matches += 1
        snapshot.append((path, mode, size, digest))
    if not core_matches:
        raise RuntimeError("共享 Xovi 残留没有可由受信清单确认的核心文件。")
    return tuple(snapshot), core_matches


def _lower_dropin_snapshot(ssh_client, known_hashes):
    token = uuid.uuid4().hex
    mount_dir = f"/tmp/rmtool-xovi-incomplete-check-{token}"
    directory = str(PurePosixPath(shared.SHARED_LAYOUT.dropin_path).parent)
    output = ssh_client.exec_checked(f"""set -eu
MOUNT_DIR={shlex.quote(mount_dir)}
cleanup() {{
    umount "$MOUNT_DIR" 2>/dev/null || true
    rmdir "$MOUNT_DIR" 2>/dev/null || true
}}
trap cleanup EXIT INT TERM
mkdir -m 0700 "$MOUNT_DIR"
mount --bind / "$MOUNT_DIR"
for file in "$MOUNT_DIR"{directory}/*.conf; do
    [ -e "$file" ] || continue
    path="${{file#"$MOUNT_DIR"}}"
    if [ -L "$file" ]; then
        printf '%s|symlink\\n' "$path"
    else
        printf '%s|%s|%s|%s|%s|%s|%s\\n' "$path" \\
            "$(stat -c '%f' "$file")" "$(stat -c '%u' "$file")" \\
            "$(stat -c '%g' "$file")" "$(stat -c '%a' "$file")" \\
            "$(stat -c '%s' "$file")" "$(sha256sum "$file" | awk '{{print $1}}')"
    fi
done
cleanup
trap - EXIT INT TERM
""").splitlines()
    found = []
    for line in output:
        parts = line.split("|")
        if len(parts) == 2 and parts[1] == "symlink":
            raise RuntimeError("底层 xochitl 配置包含符号链接，拒绝清理。")
        if len(parts) != 7:
            raise RuntimeError("底层 xochitl 配置清单无效。")
        path, raw_mode, uid, gid, permissions, size, digest = parts
        if path != shared.SHARED_LAYOUT.dropin_path:
            raise RuntimeError(f"检测到未知底层 xochitl drop-in，拒绝清理：{path}")
        if (
            raw_mode != "81a4"
            or uid != "0"
            or gid != "0"
            or permissions != "644"
            or digest not in known_hashes
        ):
            raise RuntimeError("底层 rmtool drop-in 类型、权限、所有权或内容已变化。")
        found.append((path, int(raw_mode, 16), int(size), digest))
    if len(found) > 1:
        raise RuntimeError("底层 xochitl drop-in 清单包含重复路径。")
    return found[0] if found else None


def _incomplete_forbidden_paths():
    paths = {
        tap.VELLUM_ROOT,
        tap.SHARED_XOVI_BASE,
        tap.SHARED_XOVI_LIBRARY,
        tap.SHARED_QRR_LIBRARY,
        tap.SHARED_QRR_HOME,
        tap.SHARED_APPLOAD_LIBRARY,
        tap.REMOTE_BASE,
        migration.fast.REMOTE_BASE,
        tap.DROPIN_PATH,
        migration.fast.DROPIN_PATH,
    }
    for module in migration._providers().values():
        for name in ("REMOTE_BASE", "DROPIN_PATH"):
            value = getattr(module, name, None)
            if value:
                paths.add(value)
    return tuple(sorted(paths))


def _complete_shared_exists(ssh_client, contexts):
    for runtime, trusted in contexts:
        try:
            inspection = shared.inspect_shared(ssh_client, runtime, trusted)
        except (RuntimeError, OSError, ValueError, TypeError):
            continue
        if inspection.states:
            return True
    return False


def _inspect_incomplete(ssh_client):
    base = shared.SHARED_LAYOUT.remote_base
    legacy_base = shared.LEGACY_SHARED_LAYOUT.remote_base
    artifacts = (
        base,
        legacy_base,
        shared.SHARED_LAYOUT.dropin_path,
        shared.SHARED_RECOVERY_SENTINEL,
        shared.LEGACY_RECOVERY_SENTINEL,
    )
    visible_artifacts = any(
        shared._remote_entry_exists(ssh_client, path) for path in artifacts
    )
    if not visible_artifacts:
        return RecoveryReport(RecoveryState.NOT_NEEDED, "未检测到共享 Xovi 残留，无需清理。"), None
    if shared._remote_entry_exists(ssh_client, legacy_base):
        raise RuntimeError("检测到旧 /home 共享布局，拒绝自动清理；请使用迁移或人工恢复。")

    identity = tap.get_device_identity(ssh_client)
    contexts = _trusted_cleanup_contexts(identity)
    if not contexts:
        raise RuntimeError("当前或历史受信清单无法确认共享 Xovi 文件归属。")
    for path in (base, shared.SHARED_LAYOUT.dropin_path,
                 shared.SHARED_RECOVERY_SENTINEL, shared.LEGACY_RECOVERY_SENTINEL):
        if shared._remote_entry_exists(ssh_client, path):
            _assert_no_symlink_chain(ssh_client, path)
            _ancestors(ssh_client, path)
    _unhidden_paths(ssh_client)
    forbidden = _incomplete_forbidden_paths()
    if any(
        shared._remote_entry_exists(ssh_client, path)
        for path in forbidden
        if path not in (base, shared.SHARED_LAYOUT.dropin_path)
    ):
        raise RuntimeError("检测到 Vellum、AppLoad、非托管 Xovi 或旧版插件，拒绝自动清理。")
    _external_loaders(ssh_client)
    if shared._active(ssh_client):
        raise RuntimeError("共享 Xovi 仍在当前 xochitl 中载入，拒绝自动清理。")

    allowed_files, _allowed_dirs, _core_specs, _file_specs, dropin_hashes = (
        _cleanup_trust_data(contexts)
    )
    del allowed_files
    lower_dropin = _lower_dropin_snapshot(ssh_client, dropin_hashes)
    if lower_dropin is not None:
        _assert_no_symlink_chain(ssh_client, shared.SHARED_LAYOUT.dropin_path)
        _ancestors(ssh_client, shared.SHARED_LAYOUT.dropin_path)
    base_entries, core_matches = _cleanup_tree_snapshot(ssh_client, contexts)
    visible_dropin = None
    dropin = shared.SHARED_LAYOUT.dropin_path
    if shared._remote_entry_exists(ssh_client, dropin):
        ssh_client.exec_checked(f"[ ! -L {shlex.quote(dropin)} ]")
        mode, size = _metadata(ssh_client, dropin)
        digest = shared._remote_sha256(ssh_client, dropin)
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o644 or digest not in dropin_hashes:
            raise RuntimeError("rmtool 可见 drop-in 类型、权限或内容无法确认。")
        visible_dropin = (dropin, mode, size, digest)
    if not core_matches and visible_dropin is None and lower_dropin is None:
        raise RuntimeError("共享 Xovi 残留没有可由受信清单确认的核心文件。")
    sentinels = []
    for sentinel in (shared.SHARED_RECOVERY_SENTINEL, shared.LEGACY_RECOVERY_SENTINEL):
        if not shared._remote_entry_exists(ssh_client, sentinel):
            continue
        ssh_client.exec_checked(f"[ ! -L {shlex.quote(sentinel)} ]")
        mode, size = _metadata(ssh_client, sentinel)
        digest = shared._remote_sha256(ssh_client, sentinel)
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600 or size != 0 or digest != shared._EMPTY_SHA256:
            raise RuntimeError(f"紧急停用标记类型、权限或内容不安全：{sentinel}")
        sentinels.append((sentinel, mode, size, digest))

    if _complete_shared_exists(ssh_client, contexts):
        return RecoveryReport(RecoveryState.NOT_NEEDED, "共享 Xovi 完整，无需清理残缺状态。"), None
    plan = _CleanupPlan(
        tuple(base_entries),
        visible_dropin,
        lower_dropin,
        tuple(sentinels),
        (),
    )
    return RecoveryReport(
        RecoveryState.CLEANUP_AVAILABLE,
        "已确认这是固定 rmtool 路径中的残缺共享 Xovi；可安全清理并恢复为未安装。"
        "清理不会删除微信读书、KOReader、字体、书籍或其他 /data/rmtool 内容。",
    ), plan


def inspect_incomplete(ssh_client) -> RecoveryReport:
    """Read-only proof that only safely attributable shared residue can be removed."""
    try:
        report, _plan = _inspect_incomplete(ssh_client)
        return report
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        return RecoveryReport(RecoveryState.BLOCKED, str(exc), issues=(str(exc),))


def _incomplete_cleanup_script(plan: _CleanupPlan, token: str) -> str:
    base = shlex.quote(shared.SHARED_LAYOUT.remote_base)
    dropin = shlex.quote(shared.SHARED_LAYOUT.dropin_path)
    backup = shlex.quote(f"/tmp/rmtool-xovi-incomplete-backup-{token}")
    mount_dir = shlex.quote(f"/tmp/rmtool-xovi-incomplete-root-{token}")
    sentinel_paths = tuple(item[0] for item in plan.sentinels)
    base_checks = []
    for path, mode, size, digest in plan.base_entries:
        quoted = shlex.quote(path)
        if stat.S_ISDIR(mode):
            base_checks.append(
                f'[ -d {quoted} ] && [ ! -L {quoted} ] && '
                f'[ "$(stat -c \'%a:%u:%g:%s\' {quoted})" = {shlex.quote(f"755:0:0:{size}")} ]'
            )
        else:
            base_checks.append(
                f'[ -f {quoted} ] && [ ! -L {quoted} ] && '
                f'[ "$(stat -c \'%a:%u:%g:%s\' {quoted})" = {shlex.quote(f"{stat.S_IMODE(mode):o}:0:0:{size}")} ] && '
                f'[ "$(sha256sum {quoted} | awk \'{{print $1}}\')" = {shlex.quote(digest)} ]'
            )
    sentinel_checks = []
    for path, mode, size, digest in plan.sentinels:
        quoted = shlex.quote(path)
        sentinel_checks.append(
            f'[ -f {quoted} ] && [ ! -L {quoted} ] && '
            f'[ "$(stat -c \'%a:%u:%g:%s\' {quoted})" = \'600:0:0:0\' ] && '
            f'[ "$(sha256sum {quoted} | awk \'{{print $1}}\')" = {shlex.quote(digest)} ]'
        )
    backup_sentinels = "\n".join(
        f'cp -p {shlex.quote(path)} "$BACKUP_DIR/sentinel-{index}"; '
        f'rm -f {shlex.quote(path)}'
        for index, path in enumerate(sentinel_paths)
    ) or ":"
    restore_sentinels = "\n".join(
        f'if [ -e "$BACKUP_DIR/sentinel-{index}" ] || [ -L "$BACKUP_DIR/sentinel-{index}" ]; then '
        f'if [ -e {shlex.quote(path)} ] || [ -L {shlex.quote(path)} ]; then ROLLBACK_OK=0; '
        f'elif cp -p "$BACKUP_DIR/sentinel-{index}" {shlex.quote(path)}.tmp && '
        f'mv -f {shlex.quote(path)}.tmp {shlex.quote(path)}; then :; else ROLLBACK_OK=0; fi; fi'
        for index, path in enumerate(sentinel_paths)
    ) or ":"
    upper_backup_line = (
        f'cp -p {dropin} "$BACKUP_DIR/dropin-upper"; rm -f {dropin}'
        if plan.dropin else ":"
    )
    upper_restore_line = (
        f'if [ -e "$BACKUP_DIR/dropin-upper" ] || [ -L "$BACKUP_DIR/dropin-upper" ]; then '
        f'if [ -e {dropin} ] || [ -L {dropin} ]; then ROLLBACK_OK=0; '
        f'elif cp -p "$BACKUP_DIR/dropin-upper" {dropin}.tmp && '
        f'mv -f {dropin}.tmp {dropin}; then :; else ROLLBACK_OK=0; fi; fi'
        if plan.dropin else ":"
    )
    lower_backup_line = (
        f'cp -p "$MOUNT_DIR{shared.SHARED_LAYOUT.dropin_path}" "$BACKUP_DIR/dropin-lower"; '
        f'rm -f "$MOUNT_DIR{shared.SHARED_LAYOUT.dropin_path}"'
        if plan.lower_dropin else ":"
    )
    lower_restore_line = (
        f'if [ -e "$BACKUP_DIR/dropin-lower" ] || [ -L "$BACKUP_DIR/dropin-lower" ]; then '
        f'if [ -e "$MOUNT_DIR{shared.SHARED_LAYOUT.dropin_path}" ] || '
        f'[ -L "$MOUNT_DIR{shared.SHARED_LAYOUT.dropin_path}" ]; then ROLLBACK_OK=0; '
        f'elif cp -p "$BACKUP_DIR/dropin-lower" "$MOUNT_DIR{shared.SHARED_LAYOUT.dropin_path}.tmp" && '
        f'mv -f "$MOUNT_DIR{shared.SHARED_LAYOUT.dropin_path}.tmp" '
        f'"$MOUNT_DIR{shared.SHARED_LAYOUT.dropin_path}"; then :; else ROLLBACK_OK=0; fi; fi'
        if plan.lower_dropin else ":"
    )
    upper_check = ":"
    if plan.dropin:
        _path, mode, size, digest = plan.dropin
        upper_check = (
            f'[ -f {dropin} ] && [ ! -L {dropin} ] && '
            f'[ "$(stat -c \'%a:%u:%g:%s\' {dropin})" = '
            f'{shlex.quote(f"{stat.S_IMODE(mode):o}:0:0:{size}")} ] && '
            f'[ "$(sha256sum {dropin} | awk \'{{print $1}}\')" = {shlex.quote(digest)} ]'
        )
    lower_check = ":"
    if plan.lower_dropin:
        _path, mode, size, digest = plan.lower_dropin
        lower_path = f'"$MOUNT_DIR{shared.SHARED_LAYOUT.dropin_path}"'
        lower_check = (
            f'[ -f {lower_path} ] && [ ! -L {lower_path} ] && '
            f'[ "$(stat -c \'%a:%u:%g:%s\' {lower_path})" = '
            f'{shlex.quote(f"{stat.S_IMODE(mode):o}:0:0:{size}")} ] && '
            f'[ "$(sha256sum {lower_path} | awk \'{{print $1}}\')" = {shlex.quote(digest)} ]'
        )
    base_check = (
        "[ ! -e \"$BASE\" ] && [ ! -L \"$BASE\" ]"
        if not plan.base_entries
        else "[ ! -L \"$BASE\" ]"
    )
    base_move = (
        'mv "$BASE" "$BACKUP_DIR/base"\nBASE_MOVED=1'
        if plan.base_entries
        else ":"
    )
    base_restore = (
        'if [ -e "$BACKUP_DIR/base" ] || [ -L "$BACKUP_DIR/base" ]; then '
        'if [ -e "$BASE" ] || [ -L "$BASE" ]; then ROLLBACK_OK=0; '
        'elif mv "$BACKUP_DIR/base" "$BASE"; then :; else ROLLBACK_OK=0; fi; '
        'else ROLLBACK_OK=0; fi'
        if plan.base_entries
        else ":"
    )
    parent_paths = (
        "/data",
        "/data/rmtool",
        "/etc",
        "/etc/systemd",
        "/etc/systemd/system",
        "/etc/systemd/system/xochitl.service.d",
    )
    parent_checks = "\n".join(
        f"[ ! -L {shlex.quote(path)} ]" for path in parent_paths
    )
    return f"""#!/bin/sh
set -eu
BASE={base}
DROPIN={dropin}
BACKUP_DIR={backup}
MOUNT_DIR={mount_dir}
BASE_MOVED=0
MOUNTED=0
COMMITTED=0
ROLLBACK_OK=1

unmount_root() {{
    [ "$MOUNTED" -eq 1 ] || return 0
    sync
    mount -o remount,ro "$MOUNT_DIR"
    umount "$MOUNT_DIR"
    MOUNTED=0
    rmdir "$MOUNT_DIR"
}}

rollback() {{
    rc=$?
    [ "$rc" -ne 0 ] || rc=1
    trap - EXIT INT TERM
    set +e
    if [ "$COMMITTED" -eq 0 ]; then
        if [ "$MOUNTED" -eq 1 ]; then
            mount -o remount,ro "$MOUNT_DIR" 2>/dev/null || true
            umount "$MOUNT_DIR" 2>/dev/null || ROLLBACK_OK=0
            MOUNTED=0
        fi
        if [ "$BASE_MOVED" -eq 1 ]; then
            {base_restore}
        fi
        {upper_restore_line}
        {restore_sentinels}
        mkdir -m 0700 "$MOUNT_DIR"
        if mount --bind / "$MOUNT_DIR"; then
            MOUNTED=1
            if mount -o remount,rw "$MOUNT_DIR"; then
                {lower_restore_line}
                mount -o remount,ro "$MOUNT_DIR" 2>/dev/null || ROLLBACK_OK=0
            else
                ROLLBACK_OK=0
            fi
            umount "$MOUNT_DIR" 2>/dev/null || ROLLBACK_OK=0
            MOUNTED=0
            rmdir "$MOUNT_DIR" 2>/dev/null || true
        else
            ROLLBACK_OK=0
        fi
        systemctl daemon-reload 2>/dev/null || ROLLBACK_OK=0
    fi
    if [ "$ROLLBACK_OK" -eq 1 ]; then
        rm -rf "$BACKUP_DIR"
    else
        echo "rmtool incomplete Xovi cleanup rollback incomplete; recovery kept at $BACKUP_DIR" >&2
    fi
    exit "$rc"
}}
trap rollback EXIT INT TERM

{parent_checks}
{base_check}
{chr(10).join(base_checks) or ":"}
{upper_check}
{chr(10).join(sentinel_checks) or ":"}
mkdir -m 0700 "$BACKUP_DIR"
{base_move}
{upper_backup_line}
{backup_sentinels}
mkdir -m 0700 "$MOUNT_DIR"
mount --bind / "$MOUNT_DIR"
MOUNTED=1
mount -o remount,rw "$MOUNT_DIR"
{lower_check}
{lower_backup_line}
mount -o remount,ro "$MOUNT_DIR"
umount "$MOUNT_DIR"
MOUNTED=0
rmdir "$MOUNT_DIR"
systemctl daemon-reload
rm -rf "$BACKUP_DIR"
COMMITTED=1
trap - EXIT INT TERM
"""


def cleanup_incomplete(ssh_client) -> RecoveryReport:
    """Revalidate and atomically remove only confirmed rmtool residue."""
    session = getattr(ssh_client, "operation_session", None)
    with session() if callable(session) else nullcontext():
        with shared._operation_lock(ssh_client):
            report, plan = _inspect_incomplete(ssh_client)
            if not report.can_cleanup or plan is None:
                raise RuntimeError(report.detail)
            current_report, current_plan = _inspect_incomplete(ssh_client)
            if not current_report.can_cleanup or current_plan != plan:
                raise RuntimeError("清理前共享 Xovi 状态发生变化，请重新检测后重试。")
            token = uuid.uuid4().hex
            script_path = f"/tmp/rmtool-xovi-incomplete-cleanup-{token}.sh"
            script = _incomplete_cleanup_script(plan, token).encode()
            try:
                if shared._remote_entry_exists(ssh_client, script_path):
                    raise RuntimeError("残缺状态清理临时脚本已存在，拒绝覆盖。")
                shared._upload_bytes(ssh_client, script, script_path, 0o700)
                if shared._remote_sha256(ssh_client, script_path) != hashlib.sha256(script).hexdigest():
                    raise RuntimeError("残缺状态清理脚本上传校验失败。")
                ssh_client.exec_checked(f"/bin/sh {shlex.quote(script_path)}")
                final = inspect_incomplete(ssh_client)
                if final.state != RecoveryState.NOT_NEEDED:
                    raise RuntimeError("清理后仍检测到共享 Xovi 残留。")
            except Exception as exc:
                raise RuntimeError(f"清理不完整共享 Xovi 失败：{exc}") from exc
            finally:
                try:
                    ssh_client.exec_checked(f"rm -f {shlex.quote(script_path)}")
                except Exception:
                    logging.exception("Could not remove incomplete Xovi cleanup script")
    return RecoveryReport(
        RecoveryState.NOT_NEEDED,
        "不完整共享 Xovi 已清理，插件状态已恢复为未安装；SSH 会话关闭后请手动重启设备。",
    )


def _check_capacity(ssh_client, plan, *, staged=False):
    enabled = tuple(plan.targets[fid] for fid, state in plan.states.items() if state.enabled)
    states = {fid: shared.SharedFeatureState(plan.targets[fid], state.enabled, state.process_token)
              for fid, state in plan.states.items()}
    sizes = [len(shared.shared_marker(plan.runtime, states, "0" * 64, "0" * 64))]
    if enabled:
        sizes.extend(item.size for item in plan.runtime.files)
        sizes.extend(item.size for feature in enabled for item in feature.files)
        sizes.extend((len(shared.shared_launcher(plan.runtime, enabled).encode()),
                      len(shared.shared_dropin(plan.runtime, enabled).encode())))
    # Free bytes already exclude the old base, which will only be renamed.
    # Include filesystem allocation rounding and transaction/drop-in overhead.
    needed = (24 + 1) * 1024 * 1024
    if not staged:
        needed += sum(((size + 4095) // 4096) * 4096 for size in sizes)
    try:
        available = int(ssh_client.exec_checked("df -Pk /data | awk 'NR==2 {print $4}'").strip()) * 1024
    except ValueError as exc:
        raise RuntimeError("无法确认 /data 剩余空间。") from exc
    if available < needed:
        raise RuntimeError(f"/data 空间不足：保留旧安装并暂存新包至少需要 {needed} 字节，当前 {available} 字节。")


def _ownership_snapshot(ssh_client, runtime, states, marker_text):
    base = shared.SHARED_LAYOUT.remote_base
    files = {item.path: item.mode for item in runtime.files}
    for state in states.values():
        files.update({item.runtime_path: item.mode for item in state.spec.files})
    files.update({"package.json": 0o644, "launcher.sh": 0o755,
                  f"systemd/{shared.SHARED_LAYOUT.dropin_name}": 0o644,
                  "startup.pending": 0o600})
    directories = shared._parent_directories(files)
    paths = ssh_client.exec_checked(f"find -P {shlex.quote(base)} -print").splitlines()
    if not paths or paths[0] != base or len(paths) != len(set(paths)):
        raise RuntimeError("共享插件目录清单无效。")
    snapshot = []
    launcher_digest = None
    for path in sorted(paths):
        relative = path[len(base) + 1:] if path.startswith(base + "/") else ""
        if path != base and (not relative or relative not in files.keys() | directories):
            raise RuntimeError(f"共享插件包含未知路径：{path}")
        mode, size = _metadata(ssh_client, path)
        if path == base or relative in directories:
            if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o755:
                raise RuntimeError(f"共享插件目录类型或权限变化：{path}")
            digest = ""
        else:
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != files[relative]:
                raise RuntimeError(f"共享插件文件类型或权限变化：{path}")
            digest = shared._remote_sha256(ssh_client, path)
            if relative == "package.json" and digest != hashlib.sha256(marker_text.encode()).hexdigest():
                raise RuntimeError("读取期间插件标记发生变化，请重新检测。")
            if relative == "startup.pending" and size != 0:
                raise RuntimeError("启动保护标记损坏，拒绝自动修复。")
            if relative == "launcher.sh":
                launcher_digest = digest
        snapshot.append((path, mode, size, digest))
    if base + "/package.json" not in paths:
        raise RuntimeError("共享插件标记缺失，不能确认归属。")
    dropin = shared.SHARED_LAYOUT.dropin_path
    if shared._remote_entry_exists(ssh_client, dropin):
        mode, size = _metadata(ssh_client, dropin)
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o644:
            raise RuntimeError("共享插件 drop-in 类型或权限变化。")
        digest = shared._remote_sha256(ssh_client, dropin)
        if digest not in _known_dropin_hashes(runtime, states):
            raise RuntimeError("活动共享 drop-in 内容未知，拒绝自动修复；请先人工确认配置归属。")
        marker = json.loads(marker_text)
        if launcher_digest is not None and launcher_digest != marker["launcher_sha256"]:
            raise RuntimeError("活动配置指向损坏的启动脚本；回滚后无法保证紧急停用保护，拒绝自动修复。请先人工隔离启动配置。")
        snapshot.append((dropin, mode, size, digest))
    sentinels = []
    for sentinel in (shared.SHARED_RECOVERY_SENTINEL, shared.LEGACY_RECOVERY_SENTINEL):
        if shared._remote_entry_exists(ssh_client, sentinel):
            _ancestors(ssh_client, sentinel)
            shared._assert_recovery_sentinel(ssh_client, sentinel)
            snapshot.append((sentinel,))
            sentinels.append(sentinel)
    return tuple(snapshot), tuple(sentinels)


def _inspect(ssh_client):
    base = shared.SHARED_LAYOUT.remote_base
    artifacts = (base, shared.LEGACY_SHARED_LAYOUT.remote_base, shared.SHARED_LAYOUT.dropin_path)
    if not any(shared._remote_entry_exists(ssh_client, path) for path in artifacts):
        return RecoveryReport(RecoveryState.NOT_NEEDED, "未检测到共享插件安装，无需修复。"), None
    identity = tap.get_device_identity(ssh_client)
    try:
        new_runtime, targets, _legacies = tap._trusted_shared_context(identity)
    except RuntimeError as exc:
        return RecoveryReport(RecoveryState.UNSUPPORTED, "当前固件没有精确受信包，不能自动修复。", issues=(str(exc),)), None
    if shared._remote_entry_exists(ssh_client, shared.LEGACY_SHARED_LAYOUT.remote_base):
        raise RuntimeError("检测到旧 /home 共享布局或混合布局，暂不支持自动修复。")
    for path in (base + "/package.json", shared.SHARED_LAYOUT.dropin_path):
        _ancestors(ssh_client, path)
    _unhidden_paths(ssh_client)
    forbidden = (
        tap.VELLUM_ROOT, tap.SHARED_XOVI_LIBRARY, tap.SHARED_QRR_LIBRARY,
        tap.SHARED_APPLOAD_LIBRARY, tap.REMOTE_BASE, migration.fast.REMOTE_BASE,
        tap.DROPIN_PATH, migration.fast.DROPIN_PATH,
    )
    if any(shared._remote_entry_exists(ssh_client, path) for path in forbidden):
        raise RuntimeError("检测到 Vellum、独立旧版或混合运行环境，拒绝自动修复。")
    _external_loaders(ssh_client)
    marker_path = base + "/package.json"
    mode, size = _metadata(ssh_client, marker_path)
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o644:
        raise RuntimeError("共享插件标记不是 root 所有的普通 0644 文件。")
    if size > _MAX_MARKER_BYTES:
        raise RuntimeError("共享插件标记超过安全大小限制。")
    marker_text = _read_marker(ssh_client, marker_path)
    try:
        marker = json.loads(marker_text, object_pairs_hook=_unique_object)
        fields = marker.get("identity") if isinstance(marker, dict) else None
        if not isinstance(fields, dict) or set(fields) != {
            "firmware", "platform", "architecture", "xochitl_sha256"
        } or not all(isinstance(value, str) for value in fields.values()):
            raise ValueError("共享插件标记中的身份无效。")
        old_identity = tap.DeviceIdentity(**fields)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("共享插件标记 JSON 无效。") from exc
    old_runtime, old_trusted, _old_legacies = tap._trusted_shared_context(old_identity)
    states = _recognized_marker(marker, old_runtime, old_trusted, _published_predecessors(old_identity, old_trusted))
    fingerprint, sentinels = _ownership_snapshot(ssh_client, old_runtime, states, marker_text)
    features = tuple(sorted(states))
    issues = []
    if old_identity != identity:
        issues.append("检测到固件升级残留，需要重建全部已启用功能。")
    else:
        try:
            installed = shared.inspect_shared(ssh_client, new_runtime, targets)
            if not installed.launcher_update_available and not installed.startup_pending:
                return RecoveryReport(RecoveryState.NOT_NEEDED, "共享插件完整，无需修复。", features), None
            if installed.launcher_update_available:
                issues.append("启动脚本属于已发布旧模板。")
            if installed.startup_pending:
                issues.append("检测到上次未完成的启动保护标记；重建时仅保留于隔离备份。")
        except RuntimeError as exc:
            issues.append(str(exc))
    missing = set(states) - set(targets)
    if missing:
        return RecoveryReport(RecoveryState.UNSUPPORTED, "当前固件缺少部分功能的精确包，无法保留开关状态。", features, tuple(sorted(missing))), None
    unsupported = [fid for fid, state in states.items() if state.enabled and (
        fid not in migration._providers() or state.spec.sidecars or targets[fid].sidecars
    )]
    if unsupported:
        return RecoveryReport(RecoveryState.BLOCKED, "以下功能含外部程序，暂不能保证全新重建及配置保留：" + "、".join(unsupported), features, tuple(unsupported)), None
    plan = _Plan(identity, new_runtime, {fid: targets[fid] for fid in states}, states, fingerprint, sentinels)
    tap._preflight_device(ssh_client)
    if "native-chinese" in states and states["native-chinese"].enabled:
        migration.native._reject_active_french_slot(ssh_client, identity)
    return RecoveryReport(
        RecoveryState.REPAIR_AVAILABLE,
        "归属已由 rmtool 完成安装收据或已发布标记确认；可隔离原安装并用受信新包重建，保留外部设置及紧急停用标记。",
        features,
        tuple(issues),
    ), plan


def inspect_recovery(ssh_client) -> RecoveryReport:
    """Inspect without uploads, temporary mounts, executing old tools, or writes."""
    try:
        report, plan = _inspect(ssh_client)
        if plan is not None:
            _check_capacity(ssh_client, plan)
        return report
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        return RecoveryReport(RecoveryState.BLOCKED, str(exc), issues=(str(exc),))


def repair(ssh_client, state_dir) -> RecoveryReport:
    """Rebuild every enabled peer from fresh verified bytes; retain quarantine."""
    session = getattr(ssh_client, "operation_session", None)
    with session() if callable(session) else nullcontext():
        report, plan = _inspect(ssh_client)
        if not report.can_repair or plan is None:
            raise RuntimeError(report.detail)
        _check_capacity(ssh_client, plan)
        with tempfile.TemporaryDirectory() as temporary:
            roots = {}
            for feature_id, state in sorted(plan.states.items()):
                if not state.enabled:
                    continue
                module = migration._providers()[feature_id]
                package = module.select_package(module._trusted_catalog(), plan.identity)
                if package is None or module._shared_specs(package) != (plan.runtime, plan.targets[feature_id]):
                    raise RuntimeError(f"{feature_id} 没有一致的精确受信包。")
                archive = module.download_package(package, state_dir)
                destination = Path(temporary) / feature_id
                destination.mkdir()
                extractor = getattr(module, "extract_verified_package", tap.extract_verified_package)
                roots[feature_id] = Path(extractor(archive, package, destination))
            with shared._operation_lock(ssh_client):
                current_report, current = _inspect(ssh_client)
                if not current_report.can_repair or current != plan:
                    raise RuntimeError("下载期间设备或插件状态发生变化，请重新检测后重试。")
                _check_capacity(ssh_client, plan)
                process_token = shared._process_token(ssh_client)
                states = {
                    fid: shared.SharedFeatureState(plan.targets[fid], state.enabled,
                                                   process_token if state.enabled else state.process_token)
                    for fid, state in plan.states.items()
                }
                token = uuid.uuid4().hex
                stage = f"{shared.SHARED_LAYOUT.remote_base}.staging-{token}"
                backup = f"/data/rmtool/.xovi-dropins-{token}"
                script_path = f"/tmp/rmtool-xovi-recovery-{token}.sh"
                stage_created = False
                script_owned = False
                try:
                    ssh_client.exec_checked(f"mkdir -m 0755 {shlex.quote(stage)}")
                    stage_created = True
                    shared._stage_shared(ssh_client, plan.runtime, states, roots, {}, stage)
                    # Stale startup.pending belongs only to the quarantined
                    # original, never to this fresh, verified installation.
                    final_report, final = _inspect(ssh_client)
                    if not final_report.can_repair or final != plan or shared._process_token(ssh_client) != process_token:
                        raise RuntimeError("暂存期间设备或插件状态发生变化，已中止修复。")
                    _check_capacity(ssh_client, plan, staged=True)
                    # A rollback may restore damaged bytes. Never clear this
                    # latch on failure or clear a pre-existing user latch.
                    created_sentinel = shared._set_recovery_sentinel_locked(ssh_client)
                    script = shared.shared_transaction_script(
                        stage, token, (), enable_dropin=any(s.enabled for s in states.values()), retain_backup=True,
                    ).encode()
                    if shared._remote_entry_exists(ssh_client, script_path):
                        raise RuntimeError("恢复事务临时路径已存在，拒绝覆盖。")
                    script_owned = True
                    shared._upload_bytes(ssh_client, script, script_path, 0o700)
                    if shared._remote_sha256(ssh_client, script_path) != hashlib.sha256(script).hexdigest():
                        raise RuntimeError("恢复事务脚本上传校验失败。")
                    ssh_client.exec_checked(f"/bin/sh {shlex.quote(script_path)}")
                    verified = shared.inspect_shared(ssh_client, plan.runtime, plan.targets)
                    if dict(verified.states) != states:
                        raise RuntimeError("修复后功能状态验证失败。")
                    if created_sentinel and shared.SHARED_RECOVERY_SENTINEL not in plan.sentinels:
                        try:
                            shared._clear_recovery_sentinel_locked(ssh_client, shared.SHARED_RECOVERY_SENTINEL)
                        except Exception:
                            try:
                                shared._set_recovery_sentinel_locked(ssh_client)
                            except Exception:
                                logging.exception("Could not re-arm plugin recovery protection")
                            raise
                except Exception as exc:
                    raise RuntimeError(f"插件修复失败：{exc}。回滚未完成或提交后校验失败时保留的隔离备份位于 {backup}；如存在请保留用于恢复，成功回滚后该目录可能已清理。") from exc
                finally:
                    commands = []
                    if stage_created:
                        commands.append(f"rm -rf {shlex.quote(stage)}")
                    if script_owned:
                        commands.append(f"rm -f {shlex.quote(script_path)}")
                    for command in commands:
                        try:
                            ssh_client.exec_checked(command)
                        except Exception:
                            logging.exception("Could not clean plugin recovery staging")
    protection = (
        "原有紧急停用保护仍开启，请诊断后单独解除，再手动重启。"
        if plan.sentinels or not created_sentinel else
        "修复临时保护已解除；请手动重启设备使插件生效。"
    )
    return RecoveryReport(RecoveryState.NOT_NEEDED, "插件已从受信新包重建；原安装已隔离保留，未自动重启。" + protection, report.features, backup_path=backup)
