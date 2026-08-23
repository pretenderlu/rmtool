"""Read-only diagnostic bundle export for user support.

Collects a fixed, whitelisted set of diagnostic outputs — the local rmtool
log tail plus read-only device commands over SSH — and packs them into a zip
the user can hand to the maintainer. The whitelist is deliberate: nothing
outside these entries is ever read, so PC-side secrets (for example the
plaintext device passwords in ``devices.json``) cannot enter a bundle, and
every remote command is read-only with a hard output cap.

The entries most likely to contain local paths, document names, or font names
are flagged ``optional``; the preview dialog lets the user exclude them before
saving.
"""

from __future__ import annotations

import platform as _platform
import sys
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

# Per-item and total size budgets keep a bundle small even on noisy devices.
ITEM_CAP_BYTES = 64 * 1024
SIDECAR_TAIL_BYTES = 16 * 1024
PC_LOG_TAIL_BYTES = 256 * 1024
TOTAL_BUDGET_BYTES = 2 * 1024 * 1024

BUNDLE_STEM = "rmtool-diag"


@dataclass(frozen=True)
class DiagItem:
    """One whitelisted diagnostic entry."""

    name: str
    description: str
    command: str = ""
    optional: bool = False


DEVICE_ITEMS: tuple[DiagItem, ...] = (
    DiagItem(
        "device/system-identity.txt",
        "系统身份与版本",
        "cat /etc/version 2>/dev/null; echo ---; uname -a; echo ---; "
        "cat /sys/devices/soc0/machine 2>/dev/null; echo ---; "
        "sha256sum /usr/bin/xochitl 2>/dev/null",
    ),
    DiagItem(
        "device/xochitl-service.txt",
        "xochitl 服务状态",
        "systemctl show xochitl -p ActiveState -p SubState -p MainPID "
        "-p NRestarts -p ExecMainStartTimestamp 2>/dev/null",
    ),
    DiagItem(
        "device/journal-xovi.txt",
        "rmtool Xovi 启动日志（journald）",
        "journalctl -t rmtool-xovi-standalone --no-pager -n 300 2>/dev/null",
    ),
    DiagItem(
        "device/journal-xochitl.txt",
        "xochitl 运行日志（可能包含文档名）",
        "journalctl -u xochitl --no-pager -n 300 2>/dev/null",
        optional=True,
    ),
    DiagItem(
        "device/shared-marker.txt",
        "共享 Xovi 标记文件",
        "cat /data/rmtool/xovi-standalone/package.json 2>/dev/null "
        "|| echo MISSING",
    ),
    DiagItem(
        "device/shared-tree.txt",
        "共享目录与 drop-in 清单",
        "ls -la /data/rmtool/ 2>/dev/null; echo ---; "
        "ls -la /data/rmtool/xovi-standalone/ 2>/dev/null; echo ---; "
        "ls -la /etc/systemd/system/xochitl.service.d/ 2>/dev/null "
        "|| echo NO-DROPIN-DIR",
    ),
    DiagItem(
        "device/protection-markers.txt",
        "启动保护与紧急停用标记",
        "ls -la /data/rmtool/xovi-standalone/startup.pending 2>/dev/null "
        "|| echo NONE; echo ---; "
        "ls -la /data/rmtool/disable-xovi 2>/dev/null || echo NONE; echo ---; "
        "ls -la /home/root/.local/share/rmtool/disable-xovi 2>/dev/null "
        "|| echo NONE",
    ),
    DiagItem(
        "device/dropin.txt",
        "xochitl drop-in 内容",
        "cat /etc/systemd/system/xochitl.service.d/*.conf 2>/dev/null "
        "|| echo NO-DROPIN",
    ),
    DiagItem(
        "device/sidecar-logs.txt",
        "拼音输入法 sidecar 日志",
        'for f in /tmp/rmtool-*.log; do [ -f "$f" ] || continue; '
        f'echo "== $f"; tail -c {SIDECAR_TAIL_BYTES} "$f"; echo; done 2>/dev/null',
    ),
    DiagItem(
        "device/home-base.txt",
        "/home 下 rmtool 目录",
        "ls -la /home/root/.local/share/rmtool/ 2>/dev/null; echo ---; "
        "ls -la /home/root/.local/share/rmtool/pinyin-input/ 2>/dev/null "
        "|| echo NONE",
    ),
    DiagItem(
        "device/resources.txt",
        "磁盘与内存",
        "df -h / /data /home 2>/dev/null; echo ---; "
        "cat /proc/meminfo 2>/dev/null | head -5; echo ---; uptime",
    ),
)

# Commands must stay read-only. Verified by tests against this denylist.
FORBIDDEN_FRAGMENTS = (
    " rm ", "; rm", "&& rm", "| rm", "reboot", "shutdown", "kill ",
    "passwd", "mkfs", " mount ", " umount ", "curl", "wget", "scp ", "sftp ",
)


@dataclass
class CollectedItem:
    """Result of collecting one DiagItem."""

    item: DiagItem
    text: str = ""
    error: str = ""
    truncated: bool = False


def _decode(data: str) -> str:
    return data if isinstance(data, str) else data.decode("utf-8", "replace")


def _collect_pc_log_tail(log_path: Optional[Path]) -> CollectedItem:
    item = DiagItem(
        "pc/rmtool-log-tail.txt",
        "rmtool 本机日志（末段，可能包含本地路径、文档名或字体名）",
        optional=True,
    )
    if log_path is None or not log_path.is_file():
        return CollectedItem(item, error="日志文件不存在")
    data = log_path.read_bytes()[-PC_LOG_TAIL_BYTES:]
    truncated = log_path.stat().st_size > PC_LOG_TAIL_BYTES
    return CollectedItem(
        item,
        _decode(data.decode("utf-8", "replace")),
        truncated=truncated,
    )


def _collect_pc_environment() -> CollectedItem:
    item = DiagItem("pc/environment.txt", "运行环境")
    lines = [
        f"created: {datetime.now().isoformat(timespec='seconds')}",
        f"python: {sys.version.split()[0]}",
        f"platform: {_platform.platform()}",
    ]
    try:
        from PyQt5 import QtCore

        lines.append(
            f"pyqt: {QtCore.PYQT_VERSION_STR} (Qt {QtCore.QT_VERSION_STR})"
        )
    except Exception:  # pragma: no cover - environment without PyQt5
        pass
    return CollectedItem(item, "\n".join(lines) + "\n")


def _collect_device_item(ssh_client, diag: DiagItem) -> CollectedItem:
    # The device BusyBox head has no -c option (and its dd counts short
    # reads as full blocks), but tail -c works everywhere; keeping the tail
    # also preserves the most recent log lines when output exceeds the cap.
    capped = f"({diag.command}) 2>&1 | tail -c {ITEM_CAP_BYTES}"
    try:
        stdout, _stderr, code = ssh_client.exec_command(capped)
    except Exception as exc:
        return CollectedItem(diag, error=f"采集失败：{exc}")
    text = _decode(stdout)
    truncated = False
    # Enforce the cap locally as well: never trust the remote head alone.
    data = text.encode("utf-8")
    if len(data) > ITEM_CAP_BYTES:
        data = data[:ITEM_CAP_BYTES]
        truncated = True
        text = data.decode("utf-8", "replace")
    if code != 0:
        note = f"命令退出码 {code}"
        return CollectedItem(diag, text, note, truncated)
    return CollectedItem(diag, text, "", truncated)


def collect(ssh_client, pc_log_path: Optional[Path] = None) -> List[CollectedItem]:
    """Collect every whitelisted item; individual failures are recorded."""
    results = [_collect_pc_environment(), _collect_pc_log_tail(pc_log_path)]
    results.extend(_collect_device_item(ssh_client, item) for item in DEVICE_ITEMS)
    return results


def platform_label(collected: List[CollectedItem]) -> str:
    """Derive a short device label from the collected identity output."""
    for result in collected:
        if result.item.name != "device/system-identity.txt":
            continue
        for line in result.text.splitlines():
            line = line.strip()
            # Machine strings look like "reMarkable Ferrari" / "reMarkable 1".
            if not line.lower().startswith("remarkable"):
                continue
            tokens = line.split()
            if len(tokens) < 2:
                continue
            label = tokens[1].lower()
            if label in {"1", "2"}:
                label = "rm" + label
            if label.isalnum():
                return label
    return "device"


def bundle_name(platform: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{BUNDLE_STEM}-{platform}-{stamp}-{uuid.uuid4().hex[:8]}.zip"


def _manifest(
    collected: List[CollectedItem],
    included: List[CollectedItem],
    label: str,
) -> str:
    lines = [
        "rmtool diagnostic bundle",
        f"created: {datetime.now().isoformat(timespec='seconds')}",
        f"device platform: {label}",
        "",
        "items:",
    ]
    included_names = {result.item.name for result in included}
    for result in collected:
        status = "excluded"
        if result.item.name in included_names:
            if result.error:
                status = f"error: {result.error}"
            else:
                status = f"ok ({len(result.text.encode('utf-8'))} bytes"
                if result.truncated:
                    status += ", truncated"
                status += ")"
        lines.append(f"  - {result.item.name}: {status}")
    return "\n".join(lines) + "\n"


def write_bundle(
    destination: str | Path,
    collected: List[CollectedItem],
    included: Optional[List[CollectedItem]] = None,
) -> Path:
    """Write the diagnostic zip; returns the written path.

    ``included`` selects the entries to write; the manifest records every
    non-included entry as excluded so the receiver knows what was left out.
    """
    if included is None:
        included = list(collected)
    label = platform_label(collected)
    entries = {result.item.name: result.text for result in included}
    entries["MANIFEST.txt"] = _manifest(collected, included, label)
    total = sum(len(text.encode("utf-8")) for text in entries.values())
    if total > TOTAL_BUDGET_BYTES:
        raise RuntimeError(
            f"诊断内容超过大小预算（{total} > {TOTAL_BUDGET_BYTES} 字节）。"
        )
    path = Path(destination)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in sorted(entries.items()):
            archive.writestr(name, text)
    return path
