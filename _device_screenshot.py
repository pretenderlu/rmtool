"""Native screenshot settings and safe direct screen capture."""

from __future__ import annotations

import errno
import io
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PIL import Image


CONFIG_PATH = "/home/root/.config/remarkable/xochitl.conf"
XOCHITL_PATH = "/usr/bin/xochitl"
MAX_PNG_BYTES = 32 * 1024 * 1024
MAX_IMAGE_DIMENSION = 10_000
MAX_IMAGE_PIXELS = 50_000_000
MOVE_MACHINE = "reMarkable Chiappa"
MOVE_BUFFER_WIDTH = 960
MOVE_SCREEN_WIDTH = 954
MOVE_SCREEN_HEIGHT = 1696

_SECTION_RE = re.compile(r"^[ \t]*\[[^\]\r\n]+\][ \t]*(?:\r?\n)?$")
_GENERAL_RE = re.compile(r"^[ \t]*\[General\][ \t]*(?:\r?\n)?$", re.IGNORECASE)
_SCREENSHOT_RE = re.compile(r"^[ \t]*Screenshot[ \t]*=", re.IGNORECASE)


class ScreenshotState(Enum):
    UNSUPPORTED = "unsupported"
    DISABLED = "disabled"
    RESTART_REQUIRED = "restart_required"
    READY = "ready"


@dataclass(frozen=True)
class ScreenshotStatus:
    state: ScreenshotState
    configured: bool
    direct_supported: bool = False
    machine: str = ""

    @property
    def supported(self) -> bool:
        return self.state is not ScreenshotState.UNSUPPORTED

    @property
    def ready(self) -> bool:
        return self.direct_supported


@dataclass(frozen=True)
class ScreenshotCapture:
    png: bytes
    width: int
    height: int
    remote_path: str


@dataclass(frozen=True)
class SavedScreenshot:
    path: Path
    width: int
    height: int


def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _general_bounds(lines: list[str]) -> tuple[int | None, int | None]:
    start = next((i for i, line in enumerate(lines) if _GENERAL_RE.match(line)), None)
    if start is None:
        return None, None
    end = next(
        (i for i in range(start + 1, len(lines)) if _SECTION_RE.match(lines[i])),
        len(lines),
    )
    return start, end


def screenshot_configured(config: bytes) -> bool:
    text = config.decode("utf-8", "surrogateescape")
    lines = text.splitlines(keepends=True)
    start, end = _general_bounds(lines)
    if start is None:
        return False
    values = [
        line.split("=", 1)[1].strip().casefold()
        for line in lines[start + 1 : end]
        if _SCREENSHOT_RE.match(line)
    ]
    return len(values) == 1 and values[0] == "true"


def set_screenshot_config(config: bytes, enabled: bool) -> bytes:
    """Set one General/Screenshot key while preserving all unrelated bytes."""
    text = config.decode("utf-8", "surrogateescape")
    lines = text.splitlines(keepends=True)
    start, end = _general_bounds(lines)
    newline = _line_ending(text)

    if start is None:
        prefix = "" if not text or text.endswith(("\n", "\r")) else newline
        value = "true" if enabled else "false"
        return (
            f"{text}{prefix}[General]{newline}Screenshot={value}{newline}"
        ).encode("utf-8", "surrogateescape")

    indexes = [
        i for i in range(start + 1, end) if _SCREENSHOT_RE.match(lines[i])
    ]
    value = "true" if enabled else "false"
    if indexes:
        old_ending = (
            "\r\n" if lines[indexes[0]].endswith("\r\n")
            else "\n" if lines[indexes[0]].endswith("\n")
            else ""
        )
        lines[indexes[0]] = f"Screenshot={value}{old_ending}"
        for index in reversed(indexes[1:]):
            del lines[index]
    else:
        if end and not lines[end - 1].endswith(("\n", "\r")):
            lines[end - 1] += newline
        lines.insert(end, f"Screenshot={value}{newline}")
    return "".join(lines).encode("utf-8", "surrogateescape")


def _read_remote_bytes(ssh_client, path: str, limit: int | None = None) -> bytes:
    with ssh_client.sftp_session() as sftp:
        with sftp.open(path, "rb") as remote:
            data = remote.read() if limit is None else remote.read(limit + 1)
    if limit is not None and len(data) > limit:
        raise RuntimeError(f"设备文件超过允许大小：{path}")
    return data


def _read_config(ssh_client) -> bytes | None:
    try:
        return _read_remote_bytes(ssh_client, CONFIG_PATH)
    except Exception as exc:
        if isinstance(exc, OSError) and exc.errno == errno.ENOENT:
            return None
        raise RuntimeError("无法读取设备截图配置。") from exc


def _machine_name(ssh_client) -> str:
    stdout, _stderr, code = ssh_client.exec_command(
        "cat /sys/devices/soc0/machine 2>/dev/null", timeout=10
    )
    return stdout.strip() if code == 0 else ""


def _direct_capture_supported(ssh_client, machine: str) -> bool:
    if machine != MOVE_MACHINE:
        return False
    _stdout, _stderr, code = ssh_client.exec_command(
        "pid=$(pgrep -o xochitl) || exit 1; "
        "test -r /proc/$pid/mem && grep -q '/dev/dri/card0' /proc/$pid/maps",
        timeout=10,
    )
    return code == 0


def get_status(ssh_client) -> ScreenshotStatus:
    native_supported = ssh_client.exec_command(
        f"test -x {XOCHITL_PATH}", timeout=10
    )[2] == 0
    machine = _machine_name(ssh_client)
    direct_supported = _direct_capture_supported(ssh_client, machine)
    config = _read_config(ssh_client)
    configured = config is not None and screenshot_configured(config)
    if not native_supported and not direct_supported:
        return ScreenshotStatus(
            ScreenshotState.UNSUPPORTED, configured, False, machine
        )
    if not configured:
        return ScreenshotStatus(
            ScreenshotState.DISABLED, False, direct_supported, machine
        )
    return ScreenshotStatus(
        ScreenshotState.READY, True, direct_supported, machine
    )


def _write_remote_atomic(ssh_client, path: str, data: bytes, mode: int) -> None:
    token = uuid.uuid4().hex
    remote_temp = f"{path}.rmtool-{token}.tmp"
    local_temp = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            local_temp = handle.name
        ssh_client.transfer_file(local_temp, remote_temp)
        if _read_remote_bytes(ssh_client, remote_temp) != data:
            raise RuntimeError("设备配置临时文件校验失败。")
        ssh_client.exec_checked(f"chmod {mode & 0o7777:04o} {remote_temp}")
        ssh_client.exec_checked(f"mv -f {remote_temp} {path}")
        if _read_remote_bytes(ssh_client, path) != data:
            raise RuntimeError("设备配置写入后校验失败。")
    finally:
        if local_temp:
            Path(local_temp).unlink(missing_ok=True)
        try:
            ssh_client.exec_checked(f"rm -f {remote_temp}", timeout=10)
        except Exception:
            logging.exception("Failed to clean screenshot config temporary file")


def set_enabled(ssh_client, enabled: bool) -> ScreenshotStatus:
    with ssh_client.operation_session():
        current = get_status(ssh_client)
        if not current.supported:
            raise RuntimeError("当前固件不支持设备原生截图。")
        original = _read_config(ssh_client)
        if original is None and not enabled:
            return current
        desired = set_screenshot_config(original or b"", enabled)
        if desired != original:
            if original is None:
                mode = 0o600
            else:
                try:
                    with ssh_client.sftp_session() as sftp:
                        mode = sftp.stat(CONFIG_PATH).st_mode
                except Exception as exc:
                    raise RuntimeError("无法读取设备截图配置属性。") from exc
            try:
                _write_remote_atomic(ssh_client, CONFIG_PATH, desired, mode)
            except Exception:
                logging.exception("Screenshot configuration update failed")
                try:
                    current_config = _read_config(ssh_client)
                    if original is None and current_config == desired:
                        ssh_client.exec_checked(f"rm -f {CONFIG_PATH}", timeout=10)
                    elif original is None and current_config is not None:
                        raise RuntimeError("设备截图配置出现未知并发修改。")
                    elif original is not None and current_config != original:
                        _write_remote_atomic(ssh_client, CONFIG_PATH, original, mode)
                except Exception as rollback_exc:
                    logging.exception("Screenshot configuration rollback failed")
                    raise RuntimeError(
                        "截图配置写入失败且无法确认已还原，请勿重启设备，并导出诊断日志。"
                    ) from rollback_exc
                raise
        return get_status(ssh_client)


def validate_png(data: bytes) -> tuple[int, int]:
    if not data or len(data) > MAX_PNG_BYTES:
        raise RuntimeError("设备截图为空或超过大小限制。")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise RuntimeError("设备返回的截图不是 PNG 格式。")
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or width > MAX_IMAGE_DIMENSION
                or height > MAX_IMAGE_DIMENSION
                or width * height > MAX_IMAGE_PIXELS
            ):
                raise RuntimeError("设备截图尺寸超出允许范围。")
            image.verify()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("设备返回的 PNG 截图无效。") from exc
    return width, height


def _move_capture_command(remote_path: str) -> str:
    target = MOVE_BUFFER_WIDTH * MOVE_SCREEN_HEIGHT * 4
    return f"""set -eu
pid=$(pgrep -o xochitl)
endhex=$(grep '/dev/dri/card0' /proc/$pid/maps | tail -n 1 | cut -d- -f2 | cut -d' ' -f1)
case "$endhex" in ""|*[!0-9a-fA-F]*) exit 40;; esac
start=$((0x$endhex)); target={target}; offset=0; length=2; loops=0
while [ "$length" -lt "$target" ] && [ "$loops" -lt 10000 ]; do
    offset=$((offset+length-2)); pos=$((start+offset+8))
    set -- $(dd if=/proc/$pid/mem iflag=skip_bytes,count_bytes skip=$pos count=4 2>/dev/null | od -x)
    [ -n "$3" ] || exit 41
    length=$((0x$3$2)); [ "$length" -ge 2 ] || exit 42
    loops=$((loops+1))
done
[ "$length" -ge "$target" ] || exit 43
[ "$(pgrep -o xochitl)" = "$pid" ] || exit 44
ptr=$((start+offset+16))
dd if=/proc/$pid/mem of={remote_path} iflag=skip_bytes,count_bytes skip=$ptr count=$target 2>/dev/null
[ "$(wc -c < {remote_path})" -eq "$target" ] || exit 45
"""


def _encode_move_png(raw: bytes) -> bytes:
    expected = MOVE_BUFFER_WIDTH * MOVE_SCREEN_HEIGHT * 4
    if len(raw) != expected:
        raise RuntimeError("设备返回的屏幕缓冲区大小不正确。")
    try:
        image = Image.frombytes(
            "RGBA", (MOVE_BUFFER_WIDTH, MOVE_SCREEN_HEIGHT), raw
        ).crop((0, 0, MOVE_SCREEN_WIDTH, MOVE_SCREEN_HEIGHT))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except Exception as exc:
        raise RuntimeError("无法解析设备屏幕缓冲区。") from exc


def capture(ssh_client) -> ScreenshotCapture:
    with ssh_client.operation_session():
        status = get_status(ssh_client)
        if not status.ready:
            raise RuntimeError("当前设备尚未适配 rmtool 实时截图。")
        remote_path = f"/tmp/rmtool-screenshot-{uuid.uuid4().hex}.raw"
        try:
            _stdout, stderr, code = ssh_client.exec_command(
                _move_capture_command(remote_path), timeout=30
            )
            if code != 0:
                detail = stderr.strip() or f"exit code {code}"
                raise RuntimeError(f"读取设备当前画面失败：{detail}")
            raw = _read_remote_bytes(
                ssh_client,
                remote_path,
                MOVE_BUFFER_WIDTH * MOVE_SCREEN_HEIGHT * 4,
            )
            data = _encode_move_png(raw)
            width, height = validate_png(data)
        finally:
            try:
                ssh_client.exec_checked(f"rm -f {remote_path}", timeout=10)
            except Exception as exc:
                logging.exception("Failed to remove device screenshot buffer")
                raise RuntimeError("截图后无法清理设备临时缓冲文件。") from exc
        return ScreenshotCapture(data, width, height, remote_path)


def save_png_atomic(data: bytes, destination: str | Path) -> SavedScreenshot:
    width, height = validate_png(data)
    path = Path(destination).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return SavedScreenshot(path.resolve(), width, height)


def capture_to_file(ssh_client, destination: str | Path) -> SavedScreenshot:
    result = capture(ssh_client)
    return save_png_atomic(result.png, destination)
