"""Safe original-UI Chinese localization for supported reMarkable firmware."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
import tempfile
from typing import Optional

from _ssh import remount_rw


REPO_URL = "https://github.com/boangs/rmkit"
RELEASES_URL = "https://github.com/boangs/rmkit/releases"
INSTALL_GUIDE_URL = "https://github.com/boangs/rmkit#readme"

SUPPORTED_FIRMWARE = "20260629074044"
CARRIER_LANGUAGE = "fr_FR"
CONFIG_PATH = "/home/root/.config/remarkable/xochitl.conf"
QM_PATH = "/usr/share/remarkable/xochitl/translations/reMarkable_fr.qm"
BACKUP_DIR = "/home/root/.local/share/rmtool/ui-zh-backup"
BACKUP_CONFIG_PATH = f"{BACKUP_DIR}/xochitl.conf"
BACKUP_QM_PATH = f"{BACKUP_DIR}/reMarkable_fr.qm"
BACKUP_READY_PATH = f"{BACKUP_DIR}/.ready"


class LocalizationState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    INSTALLED_NOT_ENABLED = "installed_not_enabled"
    ENABLED = "enabled"


@dataclass(frozen=True)
class LocalizationStatus:
    state: LocalizationState
    firmware: str


_SECTION_RE = re.compile(r"^[ \t]*\[[^\]\r\n]+\][ \t]*(?:\r?\n)?$")
_GENERAL_RE = re.compile(r"^[ \t]*\[General\][ \t]*(?:\r?\n)?$", re.IGNORECASE)
_LANGUAGE_RE = re.compile(r"^[ \t]*language[ \t]*=", re.IGNORECASE)


def _line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _general_bounds(lines: list[str]) -> tuple[Optional[int], Optional[int]]:
    start = next((i for i, line in enumerate(lines) if _GENERAL_RE.match(line)), None)
    if start is None:
        return None, None
    end = next(
        (i for i in range(start + 1, len(lines)) if _SECTION_RE.match(lines[i])),
        len(lines),
    )
    return start, end


def set_language_config(text: str, language: Optional[str]) -> str:
    """Set only the ``[General]`` language key, preserving unrelated text."""
    lines = text.splitlines(keepends=True)
    start, end = _general_bounds(lines)

    if start is None:
        if language is None:
            return text
        newline = _line_ending(text)
        prefix = "" if not text or text.endswith(("\n", "\r")) else newline
        return f"{text}{prefix}[General]{newline}language={language}{newline}"

    language_indexes = [
        i for i in range(start + 1, end) if _LANGUAGE_RE.match(lines[i])
    ]
    if language is None:
        return "".join(
            line for i, line in enumerate(lines) if i not in language_indexes
        )

    newline = _line_ending(text)
    replacement = f"language={language}{newline}"
    if language_indexes:
        lines[language_indexes[0]] = replacement
        for i in reversed(language_indexes[1:]):
            del lines[i]
        return "".join(lines)

    lines.insert(end, replacement)
    return "".join(lines)


def _general_language(text: str) -> Optional[str]:
    lines = text.splitlines(keepends=True)
    start, end = _general_bounds(lines)
    if start is None:
        return None
    for line in lines[start + 1 : end]:
        if _LANGUAGE_RE.match(line):
            return line.split("=", 1)[1].strip()
    return None


def _file_exists(ssh_client, path: str) -> bool:
    with ssh_client.sftp_session() as sftp:
        try:
            sftp.stat(path)
            return True
        except IOError:
            return False


def _read_bytes(ssh_client, path: str) -> bytes:
    with ssh_client.sftp_session() as sftp:
        with sftp.open(path, "rb") as remote_file:
            return remote_file.read()


def _firmware(ssh_client) -> str:
    return ssh_client.exec_checked("cat /etc/version").strip()


def _require_supported(ssh_client) -> str:
    firmware = _firmware(ssh_client)
    if firmware != SUPPORTED_FIRMWARE:
        raise RuntimeError(
            f"当前固件 {firmware or '未知'} 不受支持；仅支持 {SUPPORTED_FIRMWARE}。"
        )
    return firmware


def _stop_xochitl(ssh_client) -> None:
    ssh_client.exec_checked("systemctl stop xochitl")
    state = ssh_client.exec_checked(
        "systemctl show xochitl -p ActiveState --value"
    ).strip()
    if state != "inactive":
        raise RuntimeError(f"xochitl 未完全停止，当前状态：{state or '未知'}。")


def get_localization_status(ssh_client) -> LocalizationStatus:
    firmware = _firmware(ssh_client)
    if firmware != SUPPORTED_FIRMWARE:
        return LocalizationStatus(LocalizationState.INCOMPATIBLE, firmware)

    managed = _file_exists(ssh_client, BACKUP_READY_PATH)
    qm_exists = _file_exists(ssh_client, QM_PATH)
    config = _read_bytes(ssh_client, CONFIG_PATH).decode("utf-8", "surrogateescape")
    if managed and qm_exists and _general_language(config) == CARRIER_LANGUAGE:
        state = LocalizationState.ENABLED
    elif managed:
        state = LocalizationState.INSTALLED_NOT_ENABLED
    else:
        state = LocalizationState.NOT_INSTALLED
    return LocalizationStatus(state, firmware)


def _prepare_backup(ssh_client) -> bool:
    if _file_exists(ssh_client, BACKUP_READY_PATH):
        return False

    ssh_client.exec_checked(f"mkdir -p {BACKUP_DIR}")
    ssh_client.exec_checked(
        f"cp -p {CONFIG_PATH} {BACKUP_CONFIG_PATH}.tmp"
    )
    ssh_client.exec_checked(
        f"mv -f {BACKUP_CONFIG_PATH}.tmp {BACKUP_CONFIG_PATH}"
    )
    if _file_exists(ssh_client, QM_PATH):
        ssh_client.exec_checked(f"cp -p {QM_PATH} {BACKUP_QM_PATH}.tmp")
        ssh_client.exec_checked(f"mv -f {BACKUP_QM_PATH}.tmp {BACKUP_QM_PATH}")
    else:
        ssh_client.exec_checked(f"rm -f {BACKUP_QM_PATH} {BACKUP_QM_PATH}.tmp")
    return True


def _restore_from_backup(ssh_client) -> None:
    ssh_client.exec_checked(
        f"cp -p {BACKUP_CONFIG_PATH} {CONFIG_PATH}.tmp"
    )
    ssh_client.exec_checked(f"mv -f {CONFIG_PATH}.tmp {CONFIG_PATH}")
    if _file_exists(ssh_client, BACKUP_QM_PATH):
        ssh_client.exec_checked(f"cp -p {BACKUP_QM_PATH} {QM_PATH}.tmp")
        ssh_client.exec_checked(f"mv -f {QM_PATH}.tmp {QM_PATH}")
    else:
        ssh_client.exec_checked(f"rm -f {QM_PATH} {QM_PATH}.tmp")


def enable_localization(ssh_client, qm_local_path: str) -> LocalizationStatus:
    """Install the translation and activate it without restarting xochitl."""
    firmware = ""
    try:
        firmware = _require_supported(ssh_client)
        current = get_localization_status(ssh_client)
        if current.state is LocalizationState.ENABLED:
            return current

        qm_path = Path(qm_local_path)
        if not qm_path.is_file() or qm_path.stat().st_size == 0:
            raise RuntimeError("中文翻译文件不存在或为空。")

        _stop_xochitl(ssh_client)
        original_config = _read_bytes(ssh_client, CONFIG_PATH)
        localized_config = set_language_config(
            original_config.decode("utf-8", "surrogateescape"), CARRIER_LANGUAGE
        ).encode("utf-8", "surrogateescape")
        _prepare_backup(ssh_client)

        with tempfile.NamedTemporaryFile(delete=False) as config_file:
            config_file.write(localized_config)
            local_config_path = config_file.name
        try:
            with remount_rw(ssh_client):
                try:
                    ssh_client.transfer_file(str(qm_path), f"{QM_PATH}.tmp")
                    ssh_client.exec_checked(f"chmod 0644 {QM_PATH}.tmp")
                    ssh_client.transfer_file(local_config_path, f"{CONFIG_PATH}.tmp")
                    ssh_client.exec_checked(f"chmod 0644 {CONFIG_PATH}.tmp")
                    ssh_client.exec_checked(f"mv -f {QM_PATH}.tmp {QM_PATH}")
                    ssh_client.exec_checked(f"mv -f {CONFIG_PATH}.tmp {CONFIG_PATH}")
                    ssh_client.exec_checked(f"touch {BACKUP_READY_PATH}")
                except Exception:
                    ssh_client.exec_checked(
                        f"rm -f {QM_PATH}.tmp {CONFIG_PATH}.tmp {BACKUP_READY_PATH}"
                    )
                    _restore_from_backup(ssh_client)
                    raise
        finally:
            Path(local_config_path).unlink(missing_ok=True)

        return LocalizationStatus(LocalizationState.ENABLED, firmware)
    finally:
        ssh_client.close()


def restore_localization(ssh_client) -> LocalizationStatus:
    """Restore the carrier catalog and original language, without restart."""
    try:
        firmware = _require_supported(ssh_client)
        if not _file_exists(ssh_client, BACKUP_READY_PATH):
            return get_localization_status(ssh_client)
        if not _file_exists(ssh_client, BACKUP_CONFIG_PATH):
            raise RuntimeError("汉化备份不完整，已停止还原。")

        _stop_xochitl(ssh_client)
        backup_config = _read_bytes(ssh_client, BACKUP_CONFIG_PATH).decode(
            "utf-8", "surrogateescape"
        )
        current_config = _read_bytes(ssh_client, CONFIG_PATH).decode(
            "utf-8", "surrogateescape"
        )
        restored_config = set_language_config(
            current_config, _general_language(backup_config)
        ).encode("utf-8", "surrogateescape")

        with tempfile.NamedTemporaryFile(delete=False) as config_file:
            config_file.write(restored_config)
            local_config_path = config_file.name
        try:
            with remount_rw(ssh_client):
                try:
                    if _file_exists(ssh_client, BACKUP_QM_PATH):
                        ssh_client.exec_checked(
                            f"cp -p {BACKUP_QM_PATH} {QM_PATH}.tmp"
                        )
                        ssh_client.exec_checked(f"mv -f {QM_PATH}.tmp {QM_PATH}")
                    else:
                        ssh_client.exec_checked(f"rm -f {QM_PATH} {QM_PATH}.tmp")
                    ssh_client.transfer_file(local_config_path, f"{CONFIG_PATH}.tmp")
                    ssh_client.exec_checked(f"chmod 0644 {CONFIG_PATH}.tmp")
                    ssh_client.exec_checked(f"mv -f {CONFIG_PATH}.tmp {CONFIG_PATH}")
                    ssh_client.exec_checked(
                        f"rm -f {BACKUP_READY_PATH} {BACKUP_CONFIG_PATH} {BACKUP_QM_PATH}"
                    )
                except Exception:
                    ssh_client.exec_checked(
                        f"rm -f {QM_PATH}.tmp {CONFIG_PATH}.tmp"
                    )
                    raise
        finally:
            Path(local_config_path).unlink(missing_ok=True)

        return get_localization_status(ssh_client)
    finally:
        ssh_client.close()
