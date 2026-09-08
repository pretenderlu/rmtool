"""Read-only screen preview capture for verified reMarkable hardware."""

from __future__ import annotations

import io
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


MAX_RAW_BYTES = 32 * 1024 * 1024
MAX_PNG_BYTES = 32 * 1024 * 1024
MOVE_MACHINE = "reMarkable Chiappa"
MOVE_BUFFER_WIDTH = 960
MOVE_SCREEN_WIDTH = 954
MOVE_SCREEN_HEIGHT = 1696


@dataclass(frozen=True)
class PreviewStatus:
    supported: bool
    machine: str


@dataclass(frozen=True)
class PreviewFrame:
    png: bytes
    width: int
    height: int


@dataclass(frozen=True)
class SavedFrame:
    path: Path
    width: int
    height: int


def _read_remote_bytes(ssh_client, path: str, limit: int) -> bytes:
    try:
        with ssh_client.sftp_session() as sftp:
            with sftp.open(path, "rb") as remote:
                data = remote.read(limit + 1)
    except Exception as exc:
        raise RuntimeError("无法读取设备屏幕缓冲。") from exc
    if len(data) > limit:
        raise RuntimeError("设备屏幕缓冲超过允许大小。")
    return data


def get_status(ssh_client) -> PreviewStatus:
    stdout, _stderr, code = ssh_client.exec_command(
        "cat /sys/devices/soc0/machine 2>/dev/null", timeout=10
    )
    machine = stdout.strip() if code == 0 else ""
    if machine != MOVE_MACHINE:
        return PreviewStatus(False, machine)
    _stdout, _stderr, code = ssh_client.exec_command(
        "pid=$(pgrep -o xochitl) || exit 1; "
        "test -r /proc/$pid/mem && grep -q '/dev/dri/card0' /proc/$pid/maps",
        timeout=10,
    )
    return PreviewStatus(code == 0, machine)


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
        raise RuntimeError("设备返回的屏幕缓冲大小不正确。")
    try:
        image = Image.frombytes(
            "RGBA", (MOVE_BUFFER_WIDTH, MOVE_SCREEN_HEIGHT), raw
        ).crop((0, 0, MOVE_SCREEN_WIDTH, MOVE_SCREEN_HEIGHT))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except Exception as exc:
        raise RuntimeError("无法解析设备屏幕缓冲。") from exc


def validate_png(data: bytes) -> tuple[int, int]:
    if not data or len(data) > MAX_PNG_BYTES:
        raise RuntimeError("设备预览为空或超过大小限制。")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise RuntimeError("设备预览不是 PNG 格式。")
            width, height = image.size
            image.verify()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("设备预览 PNG 无效。") from exc
    return width, height


def capture(ssh_client) -> PreviewFrame:
    with ssh_client.operation_session():
        status = get_status(ssh_client)
        if not status.supported:
            raise RuntimeError("当前设备尚未适配屏幕预览。")
        remote_path = f"/tmp/rmtool-screen-preview-{uuid.uuid4().hex}.raw"
        try:
            _stdout, stderr, code = ssh_client.exec_command(
                _move_capture_command(remote_path), timeout=30
            )
            if code != 0:
                raise RuntimeError(
                    "读取设备当前画面失败："
                    + (stderr.strip() or f"exit code {code}")
                )
            raw = _read_remote_bytes(ssh_client, remote_path, MAX_RAW_BYTES)
            png = _encode_move_png(raw)
            width, height = validate_png(png)
            return PreviewFrame(png, width, height)
        finally:
            try:
                ssh_client.exec_checked(f"rm -f {remote_path}", timeout=10)
            except Exception as exc:
                logging.exception("Failed to remove screen preview buffer")
                raise RuntimeError("无法清理设备临时屏幕缓冲。") from exc


def save_png_atomic(data: bytes, destination: str | Path) -> SavedFrame:
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
    return SavedFrame(path.resolve(), width, height)
