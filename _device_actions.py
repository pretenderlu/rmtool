"""Small device maintenance actions used by the toolbox tabs.

These functions run blocking SSH commands and must be called from a worker
thread, never directly from a Qt slot.  Keeping them free of widget code also
lets them be unit-tested with a fake SSH client.
"""

import re
from datetime import datetime
from typing import List, Optional, Tuple

from _ssh import SSHClientWrapper, remount_rw

# Quick commands should fail fast instead of inheriting the 30-minute default
# deadline intended for installers.
QUICK_COMMAND_TIMEOUT = 60.0

DEFAULT_TIMEZONE = "Asia/Shanghai"
# IANA names: letters, digits and _ + - separated by "/" (e.g. Etc/GMT-8).
_TIMEZONE_RE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*")

_FRONTLIGHT = "/sys/class/backlight/rm_frontlight"
_BRIGHTNESS_SERVICE_PATH = "/etc/systemd/system/tweak-brightness-slider.service"
_BRIGHTNESS_SERVICE = f"""
[Unit]
Description=Set frontlight linear mapping
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo yes > {_FRONTLIGHT}/linear_mapping'
ExecStartPost=/bin/sh -c 'cat {_FRONTLIGHT}/max_brightness > {_FRONTLIGHT}/brightness'

[Install]
WantedBy=multi-user.target
""".strip()


def _run(ssh_client: SSHClientWrapper, command: str) -> str:
    return ssh_client.exec_checked(command, timeout=QUICK_COMMAND_TIMEOUT)


def sync_time(ssh_client: SSHClientWrapper, now: Optional[datetime] = None) -> str:
    """Set the device clock (system and RTC) to the local time; return it."""
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    with remount_rw(ssh_client):
        # `stamp` comes from strftime with a fixed format: digits, '-', ':'
        # and one space only, so double quotes are sufficient.
        _run(ssh_client, f'date -s "{stamp}"')
        _run(ssh_client, "hwclock -w")
    return stamp


def time_info(ssh_client: SSHClientWrapper) -> List[Tuple[str, str]]:
    """Return (title, output) pairs describing the device clock."""
    commands = (
        ("系统时间", "date"),
        ("硬件时钟", "hwclock -r"),
        ("时区信息", "timedatectl"),
    )
    return [(title, _run(ssh_client, cmd).strip()) for title, cmd in commands]


def set_timezone(ssh_client: SSHClientWrapper, timezone: str = DEFAULT_TIMEZONE) -> str:
    if not _TIMEZONE_RE.fullmatch(timezone):
        raise ValueError(f"无效时区：{timezone}")
    with remount_rw(ssh_client):
        _run(ssh_client, f"timedatectl set-timezone {timezone}")
    return timezone


def enable_wifi_ssh(ssh_client: SSHClientWrapper) -> None:
    _run(ssh_client, "rm-ssh-over-wlan on")


def reboot(ssh_client: SSHClientWrapper) -> None:
    # Use exec_command rather than exec_checked: the connection may drop
    # before the command reports a status.
    ssh_client.exec_command("reboot")


def increase_brightness(ssh_client: SSHClientWrapper) -> None:
    """Max out the frontlight and persist linear mapping across reboots."""
    with remount_rw(ssh_client):
        _run(ssh_client, f"cat {_FRONTLIGHT}/max_brightness > {_FRONTLIGHT}/brightness")
        _run(ssh_client, f"echo yes > {_FRONTLIGHT}/linear_mapping")
        _run(ssh_client, "umount -l /etc")

    with remount_rw(ssh_client):
        _run(
            ssh_client,
            f"tee {_BRIGHTNESS_SERVICE_PATH} > /dev/null <<'EOF'\n{_BRIGHTNESS_SERVICE}\nEOF",
        )
        _run(ssh_client, "systemctl daemon-reload")
        _run(ssh_client, "systemctl enable --now tweak-brightness-slider.service")
