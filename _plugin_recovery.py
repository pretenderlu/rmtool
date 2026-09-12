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


@dataclass(frozen=True)
class _Plan:
    identity: tap.DeviceIdentity
    runtime: shared.SharedRuntimeSpec
    targets: dict[str, shared.SharedFeatureSpec]
    states: dict[str, shared.SharedFeatureState]
    fingerprint: tuple
    sentinels: tuple[str, ...]


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
