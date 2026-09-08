"""Read-only screen preview capture for supported reMarkable hardware."""

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
LEGACY_FRAME_OFFSET = 2_629_632 + 8


@dataclass(frozen=True)
class DeviceProfile:
    platform: str
    label: str
    mapping: str
    buffer_width: int
    buffer_height: int
    visible_width: int
    visible_height: int
    bytes_per_pixel: int = 4


PROFILES = {
    "ferrari": DeviceProfile(
        "ferrari", "Paper Pro", "drm", 1632, 2154, 1620, 2154
    ),
    "chiappa": DeviceProfile(
        "chiappa", "Paper Pro Move", "drm", 960, 1696, 954, 1696
    ),
    "tatsu": DeviceProfile(
        "tatsu", "Paper Pure", "drm", 1408, 1872, 1404, 1872
    ),
    "rm1": DeviceProfile(
        "rm1", "reMarkable 1", "legacy", 1404, 1872, 1404, 1872
    ),
    "rm2": DeviceProfile(
        "rm2", "reMarkable 2", "legacy", 1404, 1872, 1404, 1872
    ),
}


@dataclass(frozen=True)
class PreviewStatus:
    supported: bool
    machine: str
    device_name: str = ""


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


def _profile_for_machine(machine: str) -> DeviceProfile | None:
    normalized = machine.casefold()
    if "chiappa" in normalized or "paper pro move" in normalized:
        return PROFILES["chiappa"]
    if "ferrari" in normalized or normalized == "remarkable paper pro":
        return PROFILES["ferrari"]
    if "tatsu" in normalized or "paper pure" in normalized:
        return PROFILES["tatsu"]
    if "remarkable 1" in normalized:
        return PROFILES["rm1"]
    if "remarkable 2" in normalized:
        return PROFILES["rm2"]
    return None


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


def _legacy_ready_command() -> str:
    return """set -eu
version=$(sed -n 's/^IMG_VERSION=//p' /etc/os-release | tr -d '\"')
major=${version%%.*}; rest=${version#*.}; minor=${rest%%.*}
case "$major:$minor" in *[!0-9:]*|:|*:) exit 2;; esac
[ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 24 ]; }
pid=""
for candidate in $(pidof xochitl); do
    if grep -q '/dev/fb0' /proc/$candidate/maps; then pid=$candidate; break; fi
done
[ -n "$pid" ] && test -r /proc/$pid/mem
"""


def get_status(ssh_client) -> PreviewStatus:
    stdout, _stderr, code = ssh_client.exec_command(
        "cat /sys/devices/soc0/machine 2>/dev/null || "
        "tr -d '\\0' < /proc/device-tree/model 2>/dev/null",
        timeout=10,
    )
    machine = stdout.strip() if code == 0 else ""
    profile = _profile_for_machine(machine)
    if profile is None:
        return PreviewStatus(False, machine)
    if profile.mapping == "drm":
        command = (
            "pid=$(pgrep -o xochitl) || exit 1; "
            "test -r /proc/$pid/mem && "
            "grep -q '/dev/dri/card0' /proc/$pid/maps"
        )
    else:
        command = _legacy_ready_command()
    _stdout, _stderr, code = ssh_client.exec_command(command, timeout=10)
    return PreviewStatus(code == 0, machine, profile.label)


def _drm_capture_command(profile: DeviceProfile, remote_path: str) -> str:
    target = profile.buffer_width * profile.buffer_height * profile.bytes_per_pixel
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


def _legacy_capture_command(profile: DeviceProfile, remote_path: str) -> str:
    target = profile.buffer_width * profile.buffer_height * profile.bytes_per_pixel
    return f"""set -eu
pid=""
for candidate in $(pidof xochitl); do
    if grep -q '/dev/fb0' /proc/$candidate/maps; then pid=$candidate; break; fi
done
[ -n "$pid" ] || exit 50
basehex=$(grep -A1 '/dev/fb0' /proc/$pid/maps | tail -n 1 | cut -d- -f1)
case "$basehex" in ""|*[!0-9a-fA-F]*) exit 51;; esac
ptr=$((0x$basehex+{LEGACY_FRAME_OFFSET})); target={target}
dd if=/proc/$pid/mem of={remote_path} iflag=skip_bytes,count_bytes skip=$ptr count=$target 2>/dev/null
[ "$(wc -c < {remote_path})" -eq "$target" ] || exit 52
"""


def _encode_png(profile: DeviceProfile, raw: bytes) -> bytes:
    expected = profile.buffer_width * profile.buffer_height * profile.bytes_per_pixel
    if len(raw) != expected:
        raise RuntimeError("设备返回的屏幕缓冲大小不正确。")
    try:
        if profile.mapping == "legacy":
            luminance = raw[0::4]
            image = Image.frombytes(
                "L", (profile.buffer_width, profile.buffer_height), luminance
            )
        else:
            image = Image.frombytes(
                "RGBA", (profile.buffer_width, profile.buffer_height), raw
            )
        image = image.crop((0, 0, profile.visible_width, profile.visible_height))
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
        profile = _profile_for_machine(status.machine)
        if not status.supported or profile is None:
            raise RuntimeError("当前设备或固件尚未适配屏幕预览。")
        remote_path = f"/tmp/rmtool-screen-preview-{uuid.uuid4().hex}.raw"
        command = (
            _drm_capture_command(profile, remote_path)
            if profile.mapping == "drm"
            else _legacy_capture_command(profile, remote_path)
        )
        try:
            _stdout, stderr, code = ssh_client.exec_command(command, timeout=30)
            if code != 0:
                raise RuntimeError(
                    "读取设备当前画面失败："
                    + (stderr.strip() or f"exit code {code}")
                )
            raw = _read_remote_bytes(ssh_client, remote_path, MAX_RAW_BYTES)
            png = _encode_png(profile, raw)
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
