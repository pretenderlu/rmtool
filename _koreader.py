"""KOReader book file management: detection, directory resolution, file ops."""

import logging
import os
import posixpath
import re
import shlex
import stat
from dataclasses import dataclass
from typing import Callable, List, Optional

from _ssh import SSHClientWrapper


OFFICIAL_INSTALL_DIR = "/home/root/koreader"
TOLTEC_INSTALL_DIR = "/opt/koreader"
APPLOAD_INSTALL_DIR = "/home/root/xovi/exthome/appload/koreader"
SETTINGS_FILE_NAME = "settings.reader.lua"
DEFAULT_BOOKS_DIR = "/home/root/books"
FALLBACK_HOME_DIR = "/home/root"
XOCHITL_ROOT = "/home/root/.local/share/remarkable/xochitl"
SDR_SUFFIX = ".sdr"

HOME_DIR_PATTERN = re.compile(
    r'\[\s*"home_dir"\s*\]\s*=\s*"((?:[^"\\]|\\.)*)"'
)


@dataclass(frozen=True)
class KOReaderEntry:
    name: str
    path: str
    size: int
    mtime: Optional[float]
    is_dir: bool


# -- Internal helpers ---------------------------------------------------------
def _test_path(ssh_client: SSHClientWrapper, flag: str, path: str) -> bool:
    """Run ``test -<flag> <path>`` on the device and return the result."""
    _stdout, _stderr, code = ssh_client.exec_command(
        f"test -{flag} {shlex.quote(path)}"
    )
    return code == 0


def is_forbidden_path(path: str) -> bool:
    normalized = posixpath.normpath(path)
    return normalized == XOCHITL_ROOT or normalized.startswith(XOCHITL_ROOT + "/")


def _ensure_safe_path(path: str) -> None:
    if is_forbidden_path(path):
        raise RuntimeError(f"禁止操作 xochitl 文档目录：{path}")


def _ensure_writable(ssh_client: SSHClientWrapper, path: str) -> None:
    """Gate shared by every device write: xochitl ban + installation check."""
    _ensure_safe_path(path)
    if detect_installation(ssh_client) is None:
        raise RuntimeError("设备上未检测到 KOReader 安装，已取消写入操作。")


# -- Detection ----------------------------------------------------------------
def detect_installation(ssh_client: SSHClientWrapper) -> Optional[str]:
    """Return the KOReader install directory, or ``None`` when absent."""
    if _test_path(ssh_client, "f", posixpath.join(TOLTEC_INSTALL_DIR, "koreader.sh")):
        logging.info("Detected Toltec KOReader install at %s", TOLTEC_INSTALL_DIR)
        return TOLTEC_INSTALL_DIR
    if _test_path(ssh_client, "f", posixpath.join(OFFICIAL_INSTALL_DIR, "koreader.sh")):
        logging.info("Detected official KOReader install at %s", OFFICIAL_INSTALL_DIR)
        return OFFICIAL_INSTALL_DIR
    if _test_path(ssh_client, "d", APPLOAD_INSTALL_DIR):
        logging.info("Detected appload KOReader install at %s", APPLOAD_INSTALL_DIR)
        return APPLOAD_INSTALL_DIR
    logging.info("No KOReader installation detected")
    return None


def require_installation(ssh_client: SSHClientWrapper) -> str:
    install_dir = detect_installation(ssh_client)
    if install_dir is None:
        raise RuntimeError(
            "设备上未检测到 KOReader 安装。请先安装 KOReader 后再使用本页签。"
        )
    return install_dir


# -- Start directory resolution ------------------------------------------------
def parse_home_dir(settings_text: str) -> Optional[str]:
    """Extract ``["home_dir"] = "..."`` from settings.reader.lua content."""
    match = HOME_DIR_PATTERN.search(settings_text)
    if not match:
        return None
    value = match.group(1).replace('\\"', '"').replace("\\\\", "\\").strip()
    value = value.rstrip("/")
    if not value or not value.startswith("/"):
        return None
    return value


def _read_settings_text(ssh_client: SSHClientWrapper, install_dir: str) -> Optional[str]:
    settings_path = posixpath.join(install_dir, SETTINGS_FILE_NAME)
    try:
        with ssh_client.open_remote(settings_path, "r") as fh:
            data = fh.read()
    except (IOError, OSError):
        logging.info("No readable KOReader settings at %s", settings_path)
        return None
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def resolve_start_directory(ssh_client: SSHClientWrapper, install_dir: str) -> str:
    """Pick the initial books directory: home_dir -> /home/root/books -> home."""
    settings_text = _read_settings_text(ssh_client, install_dir)
    if settings_text is not None:
        home_dir = parse_home_dir(settings_text)
        if home_dir and not is_forbidden_path(home_dir):
            logging.info("KOReader home_dir resolved to %s", home_dir)
            return home_dir
    if _test_path(ssh_client, "d", DEFAULT_BOOKS_DIR):
        logging.info("Falling back to default books dir %s", DEFAULT_BOOKS_DIR)
        return DEFAULT_BOOKS_DIR
    logging.info("Falling back to device home %s", FALLBACK_HOME_DIR)
    return FALLBACK_HOME_DIR


# -- Listing ------------------------------------------------------------------
def list_directory(ssh_client: SSHClientWrapper, remote_dir: str) -> List[KOReaderEntry]:
    """List one directory; folders first, dotfiles and .sdr dirs hidden."""
    _ensure_safe_path(remote_dir)
    entries: List[KOReaderEntry] = []
    for attr in ssh_client.listdir_attr(remote_dir):
        name = attr.filename
        if name.startswith("."):
            continue
        is_dir = stat.S_ISDIR(attr.st_mode)
        if is_dir and name.endswith(SDR_SUFFIX):
            continue
        entries.append(
            KOReaderEntry(
                name=name,
                path=posixpath.join(remote_dir, name),
                size=int(attr.st_size or 0),
                mtime=float(attr.st_mtime) if attr.st_mtime else None,
                is_dir=is_dir,
            )
        )
    entries.sort(key=lambda entry: (not entry.is_dir, entry.name.lower()))
    return entries


# -- File operations -----------------------------------------------------------
def upload_file(
    ssh_client: SSHClientWrapper,
    local_path: str,
    remote_dir: str,
    *,
    overwrite: bool = False,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> str:
    """Upload one local file into *remote_dir*; returns the remote path."""
    remote_path = posixpath.join(remote_dir, os.path.basename(local_path))
    _ensure_writable(ssh_client, remote_path)
    if not overwrite and ssh_client.file_exists(remote_path):
        raise RuntimeError(f"远端已存在同名文件：{os.path.basename(local_path)}")
    logging.info("Uploading %s -> %s", local_path, remote_path)
    with ssh_client.sftp_session() as sftp:
        sftp.put(local_path, remote_path, callback=progress_callback)
    return remote_path


def download_file(
    ssh_client: SSHClientWrapper,
    remote_path: str,
    local_path: str,
    *,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> None:
    logging.info("Downloading %s -> %s", remote_path, local_path)
    ssh_client.download_file(remote_path, local_path, callback=progress_callback)


def delete_entry(ssh_client: SSHClientWrapper, remote_path: str, is_dir: bool) -> None:
    """Delete a book file (with its ``.sdr`` sidecar) or a whole folder."""
    _ensure_writable(ssh_client, remote_path)
    if is_dir:
        logging.info("Deleting directory %s", remote_path)
        ssh_client.exec_checked(f"rm -rf -- {shlex.quote(remote_path)}")
        return
    logging.info("Deleting file %s", remote_path)
    ssh_client.exec_checked(f"rm -f -- {shlex.quote(remote_path)}")
    sdr_path = remote_path + SDR_SUFFIX
    if _test_path(ssh_client, "d", sdr_path):
        logging.info("Deleting sidecar directory %s", sdr_path)
        ssh_client.exec_checked(f"rm -rf -- {shlex.quote(sdr_path)}")


def create_folder(
    ssh_client: SSHClientWrapper, remote_dir: str, name: str
) -> str:
    """Create *name* below *remote_dir*; returns the new directory path."""
    name = name.strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise RuntimeError("文件夹名称无效。")
    remote_path = posixpath.join(remote_dir, name)
    _ensure_writable(ssh_client, remote_path)
    if ssh_client.file_exists(remote_path):
        raise RuntimeError(f"同名文件或文件夹已存在：{name}")
    logging.info("Creating directory %s", remote_path)
    ssh_client.exec_checked(f"mkdir -- {shlex.quote(remote_path)}")
    return remote_path
