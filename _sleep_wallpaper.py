"""Exact-target user-partition sleep wallpaper transaction."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import shlex
import tempfile
import uuid
from dataclasses import dataclass

import _tap_page_turn as tap


CONFIG_PATH = "/home/root/.config/remarkable/xochitl.conf"
MANAGED_DIR = "/home/root/.local/share/rmtool/wallpapers"
MANAGED_IMAGE_PATH = f"{MANAGED_DIR}/sleep-current.png"
MARKER_PATH = f"{MANAGED_DIR}/sleep-screen-state.json"
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_MARKER_BYTES = 16 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024
_DIRECTORY_CHAINS = {
    posixpath.dirname(CONFIG_PATH): (
        ("/home", False),
        ("/home/root", False),
        ("/home/root/.config", True),
    ),
    MANAGED_DIR: (
        ("/home", False),
        ("/home/root", False),
        ("/home/root/.local", True),
        ("/home/root/.local/share", True),
        ("/home/root/.local/share/rmtool", True),
        (MANAGED_DIR, True),
    ),
}
SUPPORTED_IDENTITY = tap.DeviceIdentity(
    "20260827113527",
    "chiappa",
    "aarch64",
    "5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4",
)


@dataclass(frozen=True)
class SleepWallpaperStatus:
    supported: bool
    enabled: bool = False
    conflict: bool = False
    broken: bool = False
    detail: str = ""


@dataclass(frozen=True)
class _FileSnapshot:
    data: bytes | None
    mode: int | None = None


def _snapshot_file(ssh, path: str, limit: int) -> _FileSnapshot:
    allowed_modes = {
        CONFIG_PATH: {"600", "644"},
        MARKER_PATH: {"600"},
        MANAGED_IMAGE_PATH: {"644"},
    }[path]
    output, _stderr, code = ssh.exec_command(
        f"if [ -L {shlex.quote(path)} ]; then echo unsafe; "
        f"elif [ ! -e {shlex.quote(path)} ]; then echo missing; "
        f"elif [ -f {shlex.quote(path)} ]; "
        f"then printf 'regular:'; stat -c '%u:%g:%a' {shlex.quote(path)}; "
        "else echo unsafe; fi"
    )
    kind = output.strip()
    if code:
        raise RuntimeError(f"设备文件状态无法确认：{path}")
    if kind == "missing":
        return _FileSnapshot(None)
    prefix = "regular:0:0:"
    if not kind.startswith(prefix) or kind[len(prefix):] not in allowed_modes:
        raise RuntimeError(f"设备文件状态无法确认：{path}")
    mode = int(kind[len(prefix):], 8)
    with ssh.open_remote(path, "rb") as remote:
        data = remote.read(limit + 1)
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > limit:
        raise RuntimeError(f"设备文件过大，拒绝处理：{path}")
    return _FileSnapshot(data, mode)


def _read_limited(ssh, path: str, limit: int) -> bytes | None:
    return _snapshot_file(ssh, path, limit).data


def _same_snapshot(ssh, path: str, limit: int, expected: _FileSnapshot) -> bool:
    return _snapshot_file(ssh, path, limit) == expected


def _require_snapshot(ssh, path: str, limit: int, expected: _FileSnapshot) -> None:
    if not _same_snapshot(ssh, path, limit, expected):
        raise RuntimeError("设备状态在操作期间发生变化，已停止修改。")


def _ensure_safe_directory(ssh, directory: str) -> None:
    try:
        chain = _DIRECTORY_CHAINS[directory]
    except KeyError as exc:
        raise RuntimeError("拒绝写入未知的休眠壁纸目录。") from exc
    for path, may_create in chain:
        quoted = shlex.quote(path)
        if may_create:
            command = (
                f"if [ -e {quoted} ]; then test -d {quoted} && "
                f"test ! -L {quoted} && test \"$(stat -c '%u:%g' {quoted})\" = '0:0'; "
                f"else mkdir {quoted} && chmod 700 {quoted}; fi"
            )
        else:
            command = (
                f"test -d {quoted} && test ! -L {quoted} && "
                f"test \"$(stat -c '%u:%g' {quoted})\" = '0:0'"
            )
        try:
            ssh.exec_checked(command)
        except Exception as exc:
            raise RuntimeError(f"休眠壁纸目录状态不安全：{path}") from exc


def _general_sleep_line(config: bytes) -> tuple[int | None, str | None, str]:
    try:
        text = config.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("设备配置不是有效 UTF-8，已停止修改。") from exc
    lines = text.splitlines(keepends=True)
    section = ""
    matches = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section == "General" and "=" in line:
            key, value = line.rstrip("\r\n").split("=", 1)
            if key.strip() == "SleepScreenPath":
                matches.append((index, value.strip(), line.rstrip("\r\n")))
    if len(matches) > 1:
        raise RuntimeError("设备配置包含多个休眠壁纸路径，已停止修改。")
    return matches[0] if matches else (None, None, "")


def _set_sleep_line(config: bytes, value: str | None, restore_line: str = "") -> bytes:
    index, _old, _line = _general_sleep_line(config)
    text = config.decode("utf-8")
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    replacement = restore_line if value is None and restore_line else (
        "" if value is None else f"SleepScreenPath={value}"
    )
    if index is not None:
        ending = newline if lines[index].endswith(("\n", "\r")) else ""
        if replacement:
            lines[index] = replacement + ending
        else:
            del lines[index]
    elif replacement:
        general = next(
            (i for i, line in enumerate(lines) if line.strip() == "[General]"), None
        )
        insertion = general + 1 if general is not None else 0
        if general is None:
            lines[0:0] = ["[General]" + newline]
            insertion = 1
        lines.insert(insertion, replacement + newline)
    result = "".join(lines).encode("utf-8")
    if len(result) > MAX_CONFIG_BYTES:
        raise RuntimeError("设备配置过大，已停止修改。")
    return result


def _parse_marker(data: bytes) -> dict:
    try:
        marker = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("休眠壁纸状态记录损坏，已停止修改。") from exc
    if (
        not isinstance(marker, dict)
        or set(marker)
        != {"schema_version", "managed_path", "image_sha256", "previous"}
        or marker.get("schema_version") != 2
        or marker.get("managed_path") != MANAGED_IMAGE_PATH
        or not isinstance(marker.get("image_sha256"), str)
        or len(marker["image_sha256"]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in marker["image_sha256"]
        )
        or not isinstance(marker.get("previous"), dict)
        or set(marker["previous"])
        != {"key_present", "line", "config_present", "config_mode"}
        or type(marker["previous"].get("key_present")) is not bool
        or type(marker["previous"].get("config_present")) is not bool
        or not isinstance(marker["previous"].get("line"), str)
        or (
            marker["previous"].get("config_mode") is not None
            and marker["previous"].get("config_mode") not in {0o600, 0o644}
        )
        or marker["previous"]["config_present"]
        != (marker["previous"]["config_mode"] is not None)
        or (
            marker["previous"]["key_present"]
            and not marker["previous"]["config_present"]
        )
        or (not marker["previous"]["key_present"] and marker["previous"]["line"])
        or (marker["previous"]["key_present"] and not marker["previous"]["line"])
    ):
        raise RuntimeError("休眠壁纸状态记录格式无效，已停止修改。")
    previous = marker["previous"]
    if previous["key_present"]:
        line = previous["line"]
        if "\n" in line or "\r" in line or "=" not in line:
            raise RuntimeError("休眠壁纸状态记录格式无效，已停止修改。")
        key, _value = line.split("=", 1)
        if key.strip() != "SleepScreenPath":
            raise RuntimeError("休眠壁纸状态记录格式无效，已停止修改。")
    return marker


def get_status(ssh) -> SleepWallpaperStatus:
    identity = tap.get_device_identity(ssh)
    if identity != SUPPORTED_IDENTITY:
        return SleepWallpaperStatus(False, detail="当前设备或固件尚未完成实机验证。")
    config = _read_limited(ssh, CONFIG_PATH, MAX_CONFIG_BYTES) or b""
    _index, value, _line = _general_sleep_line(config)
    marker_data = _read_limited(ssh, MARKER_PATH, MAX_MARKER_BYTES)
    image = _read_limited(ssh, MANAGED_IMAGE_PATH, MAX_IMAGE_BYTES)
    if marker_data is None:
        if value == MANAGED_IMAGE_PATH or image is not None:
            return SleepWallpaperStatus(
                True, broken=True, detail="发现来源不明的用户分区休眠壁纸状态。"
            )
        return SleepWallpaperStatus(True, conflict=bool(value))
    try:
        marker = _parse_marker(marker_data)
    except RuntimeError as exc:
        return SleepWallpaperStatus(True, broken=True, detail=str(exc))
    if (
        value != MANAGED_IMAGE_PATH
        or image is None
        or hashlib.sha256(image).hexdigest() != marker["image_sha256"]
    ):
        return SleepWallpaperStatus(True, broken=True, detail="用户分区休眠壁纸状态不完整。")
    return SleepWallpaperStatus(True, enabled=True)


def _atomic_write(
    ssh,
    path: str,
    data: bytes,
    mode: int,
    *,
    expected: _FileSnapshot | None = None,
) -> None:
    directory = posixpath.dirname(path)
    _ensure_safe_directory(ssh, directory)
    remote_tmp = f"{path}.rmtool-{uuid.uuid4().hex}.tmp"
    fd, local_tmp = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        ssh.transfer_file(local_tmp, remote_tmp)
        expected_digest = hashlib.sha256(data).hexdigest()
        actual = ssh.exec_checked(f"sha256sum {shlex.quote(remote_tmp)}").split()[0]
        if actual != expected_digest:
            raise RuntimeError("休眠壁纸临时文件校验失败。")
        if expected is not None:
            _require_snapshot(ssh, path, MAX_CONFIG_BYTES if path == CONFIG_PATH else (
                MAX_MARKER_BYTES if path == MARKER_PATH else MAX_IMAGE_BYTES
            ), expected)
        ssh.exec_checked(
            f"chmod {mode:o} {shlex.quote(remote_tmp)} && mv -f "
            f"{shlex.quote(remote_tmp)} {shlex.quote(path)}"
        )
        written = _snapshot_file(
            ssh,
            path,
            MAX_CONFIG_BYTES if path == CONFIG_PATH else (
                MAX_MARKER_BYTES if path == MARKER_PATH else MAX_IMAGE_BYTES
            ),
        )
        if written != _FileSnapshot(data, mode):
            raise RuntimeError("休眠壁纸文件写入后校验失败。")
    finally:
        if os.path.exists(local_tmp):
            os.remove(local_tmp)
        try:
            ssh.exec_checked(f"rm -f {shlex.quote(remote_tmp)}")
        except Exception:
            logging.warning("Unable to remove sleep wallpaper temporary file %s", remote_tmp)


def _remove_file(
    ssh, path: str, limit: int, *, expected: _FileSnapshot
) -> None:
    _require_snapshot(ssh, path, limit, expected)
    ssh.exec_checked(f"rm -f {shlex.quote(path)}")
    if _snapshot_file(ssh, path, limit).data is not None:
        raise RuntimeError("休眠壁纸文件删除后校验失败。")


def _restore_snapshot(
    ssh,
    snapshot: dict[str, _FileSnapshot],
    desired: dict[str, _FileSnapshot],
) -> None:
    limits = {
        CONFIG_PATH: MAX_CONFIG_BYTES,
        MANAGED_IMAGE_PATH: MAX_IMAGE_BYTES,
        MARKER_PATH: MAX_MARKER_BYTES,
    }
    errors = []
    for path, original in snapshot.items():
        try:
            current = _snapshot_file(ssh, path, limits[path])
            if current == original:
                continue
            if current != desired[path]:
                raise RuntimeError("回滚期间检测到设备文件被其他程序修改。")
            if original.data is None:
                _remove_file(ssh, path, limits[path], expected=current)
            else:
                _atomic_write(
                    ssh,
                    path,
                    original.data,
                    original.mode or 0o600,
                    expected=current,
                )
        except Exception as exc:
            logging.exception("Sleep wallpaper rollback path failed: %s", path)
            errors.append(f"{path}: {exc}")
    if errors:
        raise RuntimeError("；".join(errors))


def enable(ssh, image: bytes, *, take_over: bool = False) -> None:
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise RuntimeError("休眠壁纸图片为空或过大。")
    with ssh.operation_session():
        if tap.get_device_identity(ssh) != SUPPORTED_IDENTITY:
            raise RuntimeError("当前设备或固件尚未完成用户分区休眠壁纸验证。")
        snapshot = {
            CONFIG_PATH: _snapshot_file(ssh, CONFIG_PATH, MAX_CONFIG_BYTES),
            MANAGED_IMAGE_PATH: _snapshot_file(
                ssh, MANAGED_IMAGE_PATH, MAX_IMAGE_BYTES
            ),
            MARKER_PATH: _snapshot_file(ssh, MARKER_PATH, MAX_MARKER_BYTES),
        }
        config = snapshot[CONFIG_PATH].data or b""
        _index, value, line = _general_sleep_line(config)
        marker_data = snapshot[MARKER_PATH].data
        if marker_data is not None:
            marker = _parse_marker(marker_data)
            current_image = snapshot[MANAGED_IMAGE_PATH].data
            if (
                value != MANAGED_IMAGE_PATH
                or current_image is None
                or hashlib.sha256(current_image).hexdigest()
                != marker["image_sha256"]
            ):
                raise RuntimeError("用户分区休眠壁纸状态与设备配置不一致。")
        else:
            if value == MANAGED_IMAGE_PATH or snapshot[MANAGED_IMAGE_PATH].data is not None:
                raise RuntimeError("发现未记录来源的 rmtool 休眠壁纸状态，已停止修改。")
            if value and not take_over:
                raise RuntimeError("设备已有其他休眠壁纸路径，需要确认后才能接管。")
            marker = {
                "schema_version": 2,
                "managed_path": MANAGED_IMAGE_PATH,
                "previous": {
                    "key_present": value is not None,
                    "line": line,
                    "config_present": snapshot[CONFIG_PATH].data is not None,
                    "config_mode": snapshot[CONFIG_PATH].mode,
                },
            }
        marker["image_sha256"] = hashlib.sha256(image).hexdigest()
        marker_bytes = (json.dumps(marker, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        desired = {
            CONFIG_PATH: _FileSnapshot(
                _set_sleep_line(config, MANAGED_IMAGE_PATH), 0o600
            ),
            MANAGED_IMAGE_PATH: _FileSnapshot(image, 0o644),
            MARKER_PATH: _FileSnapshot(marker_bytes, 0o600),
        }
        try:
            _atomic_write(
                ssh,
                MANAGED_IMAGE_PATH,
                image,
                0o644,
                expected=snapshot[MANAGED_IMAGE_PATH],
            )
            _atomic_write(
                ssh,
                CONFIG_PATH,
                desired[CONFIG_PATH].data or b"",
                0o600,
                expected=snapshot[CONFIG_PATH],
            )
            _atomic_write(
                ssh,
                MARKER_PATH,
                marker_bytes,
                0o600,
                expected=snapshot[MARKER_PATH],
            )
        except Exception:
            try:
                _restore_snapshot(ssh, snapshot, desired)
            except Exception as rollback_exc:
                logging.exception("Sleep wallpaper rollback failed")
                raise RuntimeError("休眠壁纸修改失败，且自动回滚未完成；请导出诊断日志。") from rollback_exc
            raise


def disable(ssh) -> None:
    with ssh.operation_session():
        if tap.get_device_identity(ssh) != SUPPORTED_IDENTITY:
            raise RuntimeError("当前设备或固件尚未完成用户分区休眠壁纸验证。")
        snapshot = {
            CONFIG_PATH: _snapshot_file(ssh, CONFIG_PATH, MAX_CONFIG_BYTES),
            MANAGED_IMAGE_PATH: _snapshot_file(
                ssh, MANAGED_IMAGE_PATH, MAX_IMAGE_BYTES
            ),
            MARKER_PATH: _snapshot_file(ssh, MARKER_PATH, MAX_MARKER_BYTES),
        }
        if snapshot[MARKER_PATH].data is None:
            raise RuntimeError("没有可停用的用户分区休眠壁纸状态。")
        marker = _parse_marker(snapshot[MARKER_PATH].data)
        config = snapshot[CONFIG_PATH].data or b""
        _index, value, _line = _general_sleep_line(config)
        current_image = snapshot[MANAGED_IMAGE_PATH].data
        if (
            value != MANAGED_IMAGE_PATH
            or current_image is None
            or hashlib.sha256(current_image).hexdigest() != marker["image_sha256"]
        ):
            raise RuntimeError("用户分区休眠壁纸状态与设备配置不一致。")
        previous = marker["previous"]
        restored = _set_sleep_line(
            config,
            None,
            previous["line"] if previous["key_present"] else "",
        )
        if not previous["config_present"] and restored == b"[General]\n":
            restored = b""
        restored_data = restored if restored or previous["config_present"] else None
        restored_mode = (
            previous["config_mode"]
            if previous["config_present"]
            else snapshot[CONFIG_PATH].mode
        )
        restored_config = _FileSnapshot(
            restored_data,
            restored_mode if restored_data is not None else None,
        )
        desired = {
            CONFIG_PATH: restored_config,
            MANAGED_IMAGE_PATH: _FileSnapshot(None),
            MARKER_PATH: _FileSnapshot(None),
        }
        try:
            if restored_config.data is None:
                _remove_file(
                    ssh,
                    CONFIG_PATH,
                    MAX_CONFIG_BYTES,
                    expected=snapshot[CONFIG_PATH],
                )
            else:
                _atomic_write(
                    ssh,
                    CONFIG_PATH,
                    restored_config.data,
                    restored_config.mode or 0o600,
                    expected=snapshot[CONFIG_PATH],
                )
            _remove_file(
                ssh,
                MARKER_PATH,
                MAX_MARKER_BYTES,
                expected=snapshot[MARKER_PATH],
            )
            _remove_file(
                ssh,
                MANAGED_IMAGE_PATH,
                MAX_IMAGE_BYTES,
                expected=snapshot[MANAGED_IMAGE_PATH],
            )
        except Exception:
            try:
                _restore_snapshot(ssh, snapshot, desired)
            except Exception as rollback_exc:
                logging.exception("Sleep wallpaper rollback failed")
                raise RuntimeError("停用失败，且自动回滚未完成；请导出诊断日志。") from rollback_exc
            raise
