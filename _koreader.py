"""KOReader book file management: detection, directory resolution, file ops."""

import hashlib
import json
import logging
import os
import posixpath
import re
import shlex
import stat
import tarfile
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional

from _ssh import SSHClientWrapper
import _appload
import _tap_page_turn as tap
import _xovi_standalone as shared


OFFICIAL_INSTALL_DIR = "/home/root/koreader"
TOLTEC_INSTALL_DIR = "/opt/koreader"
APPLOAD_INSTALL_DIR = "/home/root/xovi/exthome/appload/koreader"
SETTINGS_FILE_NAME = "settings.reader.lua"
DEFAULT_BOOKS_DIR = "/home/root/books"
FALLBACK_HOME_DIR = "/home/root"
XOCHITL_ROOT = "/home/root/.local/share/remarkable/xochitl"
SDR_SUFFIX = ".sdr"
MANAGED_MARKER = posixpath.join(APPLOAD_INSTALL_DIR, ".rmtool-install.json")
PRESERVED_INSTALL_DIR = "/home/root/.local/share/rmtool/koreader-preserved"
LEGACY_BACKUP_DIR = "/home/root/.local/share/rmtool/koreader-legacy-backup"

MIGRATED_USER_PATHS = (
    "settings.reader.lua",
    "settings.reader.lua.old",
    "history.lua",
    "defaults.custom.lua",
    "settings",
    "screenshots",
    "clipboard",
    "styletweaks",
    "data/cr3.ini",
    "books",
)

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


class ManagedState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    INSTALLED = "installed"
    REPAIRABLE = "repairable"
    LEGACY_DATA = "legacy_data"
    EXTERNAL = "external"
    BROKEN = "broken"


@dataclass(frozen=True)
class ManagedStatus:
    state: ManagedState
    identity: tap.DeviceIdentity
    asset: Optional[_appload.OfficialAsset] = None
    version: str = ""
    detail: str = ""


def _managed_marker_bytes(asset: _appload.OfficialAsset) -> bytes:
    document = {
        "schema_version": 1,
        "source": "koreader/koreader",
        "version": asset.version,
        "asset": asset.name,
        "sha256": asset.sha256,
    }
    return (json.dumps(document, ensure_ascii=True, sort_keys=True) + "\n").encode(
        "ascii"
    )


def _remote_text(ssh_client: SSHClientWrapper, path: str) -> str:
    with ssh_client.open_remote(path, "r") as remote:
        data = remote.read()
    return data.decode("utf-8") if isinstance(data, bytes) else str(data)


def _read_managed_marker(
    ssh_client: SSHClientWrapper,
    asset: _appload.OfficialAsset,
) -> bool:
    try:
        data = _remote_text(ssh_client, MANAGED_MARKER).encode("ascii")
    except (IOError, OSError, UnicodeError):
        return False
    return data == _managed_marker_bytes(asset)


def _bridge_enabled(ssh_client: SSHClientWrapper, identity: tap.DeviceIdentity) -> bool:
    runtime, trusted, _legacies = tap._trusted_shared_context(identity)
    inspection = shared.inspect_shared(ssh_client, runtime, trusted)
    record = inspection.states.get(_appload.KOREADER_FEATURE_ID)
    return record is not None and record.enabled


def get_managed_status(ssh_client: SSHClientWrapper) -> ManagedStatus:
    identity = tap.get_device_identity(ssh_client)
    asset = _appload.koreader_asset(identity)
    if asset is None:
        return ManagedStatus(
            ManagedState.INCOMPATIBLE,
            identity,
            detail="当前设备不是 rmtool 精确支持的正式版固件",
        )
    target_exists = _test_path(ssh_client, "d", APPLOAD_INSTALL_DIR)
    marker_exists = _test_path(ssh_client, "f", MANAGED_MARKER)
    if not target_exists:
        if marker_exists:
            return ManagedStatus(
                ManagedState.BROKEN,
                identity,
                asset,
                detail="KOReader 管理标记存在，但安装目录缺失",
            )
        preserved = _test_path(ssh_client, "d", PRESERVED_INSTALL_DIR)
        return ManagedStatus(
            ManagedState.NOT_INSTALLED,
            identity,
            asset,
            detail="已保留上次用户数据" if preserved else "",
        )
    if not marker_exists:
        program_files = (
            "koreader.sh",
            "reader.lua",
            "external.manifest.json",
            "git-rev",
        )
        if not any(
            _test_path(ssh_client, "f", posixpath.join(APPLOAD_INSTALL_DIR, name))
            for name in program_files
        ):
            return ManagedStatus(
                ManagedState.LEGACY_DATA,
                identity,
                asset,
                detail="检测到可迁移的旧版 KOReader 用户数据",
            )
        return ManagedStatus(
            ManagedState.EXTERNAL,
            identity,
            asset,
            detail="检测到非 rmtool 管理的 KOReader 安装，可备份后迁移",
        )
    if not _read_managed_marker(ssh_client, asset):
        return ManagedStatus(
            ManagedState.BROKEN,
            identity,
            asset,
            detail="KOReader 管理标记无法验证",
        )
    try:
        version = _remote_text(
            ssh_client, posixpath.join(APPLOAD_INSTALL_DIR, "git-rev")
        ).strip()
        if version != asset.version:
            raise RuntimeError("KOReader 版本文件与管理标记不一致")
        required_executables = ("koreader.sh", "luajit", "fbink")
        if not all(
            _test_path(
                ssh_client,
                "x",
                posixpath.join(APPLOAD_INSTALL_DIR, name),
            )
            for name in required_executables
        ):
            return ManagedStatus(
                ManagedState.REPAIRABLE,
                identity,
                asset,
                version,
                "官方程序文件缺少可执行权限，可重新安装修复",
            )
        if not _bridge_enabled(ssh_client, identity):
            raise RuntimeError("KOReader 文件存在，但 AppLoad 启动入口未启用")
    except Exception as exc:
        return ManagedStatus(
            ManagedState.BROKEN, identity, asset, detail=str(exc)
        )
    return ManagedStatus(ManagedState.INSTALLED, identity, asset, version)


def _validate_koreader_tree(
    root: Path,
    asset: _appload.OfficialAsset,
) -> Path:
    app = root / "koreader"
    required = (
        app / "koreader.sh",
        app / "reader.lua",
        app / "external.manifest.json",
        app / "icon.png",
        app / "git-rev",
        app / "ota" / "package.index",
    )
    if not all(path.is_file() and not path.is_symlink() for path in required):
        raise RuntimeError("KOReader 官方包缺少必要文件。")
    if (app / "git-rev").read_text(encoding="utf-8").strip() != asset.version:
        raise RuntimeError("KOReader 官方包版本与固定发布不一致。")
    icon = app / "icon.png"
    if (
        icon.stat().st_size != _appload.KOREADER_ICON.size
        or hashlib.sha256(icon.read_bytes()).hexdigest()
        != _appload.KOREADER_ICON.sha256
    ):
        raise RuntimeError("KOReader 官方图标校验失败。")
    return app


def _official_zip_modes(archive: Path) -> dict[str, int]:
    modes = {}
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            name = _appload._safe_zip_path(info.filename)
            if info.is_dir():
                continue
            raw_mode = (info.external_attr >> 16) & 0o777
            modes[name] = 0o755 if raw_mode & 0o111 else 0o644
    return modes


def _make_payload_archive(
    app: Path,
    destination: Path,
    official_modes: dict[str, int],
) -> Path:
    def normalize(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        if info.isdir():
            info.mode = 0o755
        elif info.isfile():
            info.mode = official_modes.get(info.name, 0o644)
        return info

    with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
        bundle.add(app, arcname="koreader", recursive=True, filter=normalize)
    return destination


def _prepare_bridge_root(
    identity: tap.DeviceIdentity,
    icon: Path,
    state_dir: str,
    destination: str | Path,
) -> Path:
    runtime_package = tap.select_package(tap._trusted_catalog(), identity)
    if runtime_package is None or runtime_package.channel != "stable":
        raise RuntimeError("当前设备没有可用的正式版共享运行资源。")
    runtime_archive = tap.download_package(runtime_package, state_dir)
    root = tap.extract_verified_package(
        runtime_archive, runtime_package, destination
    )
    bridge = root / "koreader-bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    (bridge / "external.manifest.json").write_bytes(
        _appload.koreader_bridge_bytes()
    )
    (bridge / "icon.png").write_bytes(icon.read_bytes())
    os.chmod(bridge / "external.manifest.json", 0o644)
    os.chmod(bridge / "icon.png", 0o644)
    return root


def _koreader_running(ssh_client: SSHClientWrapper) -> bool:
    command = f"""
for proc in /proc/[0-9]*; do
    exe=$(readlink "$proc/exe" 2>/dev/null || true)
    case "$exe" in {shlex.quote(APPLOAD_INSTALL_DIR)}/*) exit 0 ;; esac
    cmd=$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null || true)
    case "$cmd" in *{shlex.quote(APPLOAD_INSTALL_DIR)}*) exit 0 ;; esac
done
exit 1
""".strip()
    return ssh_client.exec_command(command)[2] == 0


def _rollback_payload(
    ssh_client: SSHClientWrapper,
    target: str,
    backup: str,
) -> None:
    ssh_client.exec_checked(
        f"rm -rf {shlex.quote(target)}; "
        f"if [ -d {shlex.quote(backup)} ]; then "
        f"mv {shlex.quote(backup)} {shlex.quote(target)}; fi"
    )


def install_managed(
    ssh_client: SSHClientWrapper,
    archive_path: str | Path,
    state_dir: str,
    *,
    migrate_existing: bool = False,
) -> ManagedStatus:
    identity = tap.get_device_identity(ssh_client)
    asset = _appload.koreader_asset(identity)
    if asset is None:
        raise RuntimeError("当前设备不是 rmtool 精确支持的正式版固件。")
    app_status = _appload.get_status(ssh_client)
    if app_status.state not in (
        _appload.AppLoadState.ENABLED,
        _appload.AppLoadState.ENABLE_PENDING_REBOOT,
    ):
        raise RuntimeError("请先安装并启用 AppLoad。")
    current = get_managed_status(ssh_client)
    legacy_states = (ManagedState.LEGACY_DATA, ManagedState.EXTERNAL)
    if current.state in legacy_states and not migrate_existing:
        raise RuntimeError("检测到旧版 KOReader，请明确选择迁移后再安装。")
    if current.state == ManagedState.BROKEN:
        raise RuntimeError(current.detail or "KOReader 状态无法验证。")
    if current.state in legacy_states and _test_path(
        ssh_client, "d", LEGACY_BACKUP_DIR
    ):
        raise RuntimeError(
            "已存在旧版 KOReader 完整备份，请先人工确认该目录："
            f"{LEGACY_BACKUP_DIR}"
        )
    if _koreader_running(ssh_client):
        raise RuntimeError("KOReader 正在运行，请退出后重试。")
    archive = _appload.verify_official_asset(archive_path, asset)
    tap._preflight_device(ssh_client)

    token = uuid.uuid4().hex
    remote_archive = f"/home/root/.cache/rmtool/koreader-{token}.tar.gz"
    stage = f"/home/root/.cache/rmtool/koreader-stage-{token}"
    backup = f"{APPLOAD_INSTALL_DIR}.rmtool-backup-{token}"
    marker = _managed_marker_bytes(asset)
    committed = False
    rollback_backup = backup
    with tempfile.TemporaryDirectory() as temporary:
        extracted = _appload.extract_official_zip(
            archive,
            Path(temporary) / "official",
            maximum_unpacked=_appload.MAX_KOREADER_UNPACKED_BYTES,
            expected_prefix="koreader",
        )
        app = _validate_koreader_tree(extracted, asset)
        official_modes = _official_zip_modes(archive)
        (app / ".rmtool-install.json").write_bytes(marker)
        os.chmod(app / ".rmtool-install.json", 0o644)
        payload = _make_payload_archive(
            app,
            Path(temporary) / "koreader.tar.gz",
            official_modes,
        )
        payload_sha = hashlib.sha256(payload.read_bytes()).hexdigest()
        bridge_root = _prepare_bridge_root(
            identity,
            app / "icon.png",
            state_dir,
            Path(temporary) / "bridge",
        )
        ssh_client.exec_checked(
            "mkdir -p /home/root/.cache/rmtool; "
            f"rm -rf {shlex.quote(stage)} {shlex.quote(backup)}"
        )
        ssh_client.transfer_file(str(payload), remote_archive)
        if (
            ssh_client.exec_checked(
                f"sha256sum {shlex.quote(remote_archive)}"
            ).split()[0]
            != payload_sha
        ):
            raise RuntimeError("KOReader 设备端上传校验失败。")
        try:
            migration_paths = " ".join(
                shlex.quote(path) for path in MIGRATED_USER_PATHS
            )
            executable_paths = " ".join(
                shlex.quote(path)
                for path, mode in sorted(official_modes.items())
                if mode & 0o111
            )
            ssh_client.exec_checked(
                f"""
set -eu
TARGET={shlex.quote(APPLOAD_INSTALL_DIR)}
TARGET_PARENT={shlex.quote(posixpath.dirname(APPLOAD_INSTALL_DIR))}
PRESERVED={shlex.quote(PRESERVED_INSTALL_DIR)}
STAGE={shlex.quote(stage)}
ARCHIVE={shlex.quote(remote_archive)}
BACKUP={shlex.quote(backup)}
mkdir -p "$STAGE"
tar -xzf "$ARCHIVE" -C "$STAGE"
SOURCE=
if [ -d "$TARGET" ]; then
    SOURCE="$TARGET"
elif [ -d "$PRESERVED" ]; then
    SOURCE="$PRESERVED"
fi
if [ -n "$SOURCE" ]; then
    for item in {migration_paths}; do
        [ -e "$SOURCE/$item" ] || [ -L "$SOURCE/$item" ] || continue
        [ ! -L "$SOURCE/$item" ] || {{ echo "unsafe migrated link: $item" >&2; exit 1; }}
        if [ -d "$SOURCE/$item" ] && find "$SOURCE/$item" -type l | grep -q .; then
            echo "unsafe migrated tree: $item" >&2
            exit 1
        fi
        mkdir -p "$(dirname "$STAGE/koreader/$item")"
        rm -rf "$STAGE/koreader/$item"
        cp -a "$SOURCE/$item" "$STAGE/koreader/$item"
    done
fi
chown -R root:root "$STAGE/koreader"
[ -f "$STAGE/koreader/koreader.sh" ] && [ ! -L "$STAGE/koreader/koreader.sh" ]
[ -f "$STAGE/koreader/external.manifest.json" ] && [ ! -L "$STAGE/koreader/external.manifest.json" ]
[ "$(cat "$STAGE/koreader/git-rev")" = {shlex.quote(asset.version)} ]
for item in {executable_paths}; do [ -x "$STAGE/$item" ] || {{ echo "non-executable official file: $item" >&2; exit 1; }}; done
for directory in /home/root/xovi /home/root/xovi/exthome "$TARGET_PARENT"; do
    if [ -L "$directory" ]; then
        echo "unsafe KOReader target parent" >&2
        exit 1
    fi
    if [ ! -e "$directory" ]; then mkdir "$directory"; fi
    if [ ! -d "$directory" ] || [ -L "$directory" ]; then
        echo "unsafe KOReader target parent" >&2
        exit 1
    fi
done
if [ -d "$TARGET" ]; then mv "$TARGET" "$BACKUP"; fi
mv "$STAGE/koreader" "$TARGET"
rmdir "$STAGE"
""".strip()
            )
            committed = True
            if current.state in legacy_states:
                ssh_client.exec_checked(
                    f"mkdir -p {shlex.quote(posixpath.dirname(LEGACY_BACKUP_DIR))}; "
                    f"mv {shlex.quote(backup)} {shlex.quote(LEGACY_BACKUP_DIR)}"
                )
                rollback_backup = LEGACY_BACKUP_DIR
            _appload.ensure_shim_links(ssh_client, identity)
            runtime, trusted, legacies = tap._trusted_shared_context(identity)
            feature = trusted[_appload.KOREADER_FEATURE_ID]
            shared.enable_shared(
                ssh_client,
                runtime,
                feature,
                bridge_root,
                trusted,
                legacies,
            )
            try:
                ssh_client.exec_checked(
                    f"rm -rf {shlex.quote(backup)} "
                    f"{shlex.quote(PRESERVED_INSTALL_DIR)}; "
                    f"rm -f {shlex.quote(remote_archive)}"
                )
            except Exception:
                logging.exception("Could not clean old KOReader data")
        except Exception:
            if committed:
                try:
                    _rollback_payload(
                        ssh_client, APPLOAD_INSTALL_DIR, rollback_backup
                    )
                except Exception:
                    logging.exception("Could not roll back KOReader payload")
            raise
        finally:
            try:
                ssh_client.exec_checked(
                    f"rm -rf {shlex.quote(stage)}; "
                    f"rm -f {shlex.quote(remote_archive)}"
                )
            except Exception:
                logging.exception("Could not clean KOReader staging files")
    return get_managed_status(ssh_client)


def install_managed_cloud(
    ssh_client: SSHClientWrapper,
    state_dir: str,
    *,
    migrate_existing: bool = False,
) -> ManagedStatus:
    identity = tap.get_device_identity(ssh_client)
    asset = _appload.koreader_asset(identity)
    if asset is None:
        raise RuntimeError("当前设备不是 rmtool 精确支持的正式版固件。")
    archive = _appload.download_official_asset(asset, state_dir)
    return install_managed(
        ssh_client,
        archive,
        state_dir,
        migrate_existing=migrate_existing,
    )


def purge_legacy_install(ssh_client: SSHClientWrapper) -> ManagedStatus:
    status = get_managed_status(ssh_client)
    if status.state not in (ManagedState.LEGACY_DATA, ManagedState.EXTERNAL):
        raise RuntimeError("当前没有可由 rmtool 清理的旧版 KOReader 残留。")
    if _koreader_running(ssh_client):
        raise RuntimeError("KOReader 正在运行，请退出后重试。")
    target = shlex.quote(APPLOAD_INSTALL_DIR)
    ssh_client.exec_checked(
        f"[ -d {target} ] && [ ! -L {target} ] || {{ "
        "echo 'legacy KOReader path is not a real directory' >&2; exit 1; }; "
        f"rm -rf {target}; "
        f"[ ! -e {target} ] && [ ! -L {target} ]"
    )
    result = get_managed_status(ssh_client)
    if result.state != ManagedState.NOT_INSTALLED:
        raise RuntimeError("旧版 KOReader 残留删除后仍可见，拒绝继续。")
    return result


def uninstall_managed(ssh_client: SSHClientWrapper) -> ManagedStatus:
    status = get_managed_status(ssh_client)
    if status.state == ManagedState.NOT_INSTALLED:
        return status
    if status.state not in (ManagedState.INSTALLED, ManagedState.REPAIRABLE):
        raise RuntimeError(status.detail or "KOReader 安装不属于 rmtool。")
    if _koreader_running(ssh_client):
        raise RuntimeError("KOReader 正在运行，请退出后重试。")
    identity = status.identity
    runtime, trusted, _legacies = tap._trusted_shared_context(identity)
    if _test_path(ssh_client, "d", PRESERVED_INSTALL_DIR):
        raise RuntimeError(
            "已存在上次保留的 KOReader 数据，请先处理该目录后重试："
            f"{PRESERVED_INSTALL_DIR}"
        )
    ssh_client.exec_checked(
        f"mkdir -p {shlex.quote(posixpath.dirname(PRESERVED_INSTALL_DIR))}; "
        f"mv {shlex.quote(APPLOAD_INSTALL_DIR)} "
        f"{shlex.quote(PRESERVED_INSTALL_DIR)}"
    )
    try:
        shared.disable_shared(
            ssh_client,
            runtime,
            _appload.KOREADER_FEATURE_ID,
            trusted,
        )
    except Exception:
        try:
            ssh_client.exec_checked(
                f"mv {shlex.quote(PRESERVED_INSTALL_DIR)} "
                f"{shlex.quote(APPLOAD_INSTALL_DIR)}"
            )
        except Exception:
            logging.exception("Could not restore KOReader after bridge failure")
        raise
    return get_managed_status(ssh_client)


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


def _is_within_root(path: str, library_root: str) -> bool:
    return path == library_root or path.startswith(library_root + "/")


def canonicalize_library_root(ssh_client: SSHClientWrapper, path: str) -> str:
    """Resolve and validate the immutable root used by one browser session."""
    normalized = posixpath.normpath(path)
    if not normalized.startswith("/") or normalized == "/":
        raise RuntimeError(f"KOReader 书库根目录无效：{path}")
    canonical = posixpath.normpath(ssh_client.realpath(normalized))
    if not canonical.startswith("/") or canonical == "/":
        raise RuntimeError(f"KOReader 书库根目录无效：{path}")
    _ensure_safe_path(canonical)
    return canonical


def _canonical_library_path(
    ssh_client: SSHClientWrapper, path: str, library_root: str
) -> str:
    """Resolve *path* and reject lexical or symlink escapes from the root."""
    root = posixpath.normpath(library_root)
    normalized = posixpath.normpath(path)
    if (
        not root.startswith("/")
        or root == "/"
        or not normalized.startswith("/")
        or not _is_within_root(normalized, root)
    ):
        raise RuntimeError(f"路径超出 KOReader 书库范围：{path}")
    canonical = posixpath.normpath(ssh_client.realpath(normalized))
    if not canonical.startswith("/") or not _is_within_root(canonical, root):
        raise RuntimeError(f"路径超出 KOReader 书库范围：{path}")
    _ensure_safe_path(canonical)
    return canonical


def _ensure_writable(
    ssh_client: SSHClientWrapper, path: str, library_root: str
) -> str:
    """Gate every device write by canonical library ownership and install."""
    canonical = _canonical_library_path(ssh_client, path, library_root)
    if detect_installation(ssh_client) is None:
        raise RuntimeError("设备上未检测到 KOReader 安装，已取消写入操作。")
    return canonical


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
            try:
                canonical = canonicalize_library_root(ssh_client, home_dir)
            except (IOError, OSError, RuntimeError):
                logging.warning("Ignoring unsafe KOReader home_dir %s", home_dir)
            else:
                logging.info("KOReader home_dir resolved to %s", canonical)
                return canonical
    if _test_path(ssh_client, "d", DEFAULT_BOOKS_DIR):
        canonical = canonicalize_library_root(ssh_client, DEFAULT_BOOKS_DIR)
        logging.info("Falling back to default books dir %s", canonical)
        return canonical
    canonical = canonicalize_library_root(ssh_client, FALLBACK_HOME_DIR)
    logging.info("Falling back to device home %s", canonical)
    return canonical


# -- Listing ------------------------------------------------------------------
def list_directory(
    ssh_client: SSHClientWrapper, remote_dir: str, library_root: str
) -> List[KOReaderEntry]:
    """List one directory; folders first, dotfiles and .sdr dirs hidden."""
    canonical_dir = _canonical_library_path(ssh_client, remote_dir, library_root)
    entries: List[KOReaderEntry] = []
    for attr in ssh_client.listdir_attr(canonical_dir):
        name = attr.filename
        if name.startswith("."):
            continue
        is_dir = stat.S_ISDIR(attr.st_mode)
        if is_dir and name.endswith(SDR_SUFFIX):
            continue
        entries.append(
            KOReaderEntry(
                name=name,
                path=posixpath.join(canonical_dir, name),
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
    library_root: str,
    *,
    overwrite: bool = False,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> str:
    """Upload one local file into *remote_dir*; returns the remote path."""
    canonical_dir = _ensure_writable(ssh_client, remote_dir, library_root)
    remote_path = posixpath.join(canonical_dir, os.path.basename(local_path))
    remote_exists = ssh_client.file_exists(remote_path)
    if remote_exists:
        _canonical_library_path(ssh_client, remote_path, library_root)
    if not overwrite and remote_exists:
        raise RuntimeError(f"远端已存在同名文件：{os.path.basename(local_path)}")
    logging.info("Uploading %s -> %s", local_path, remote_path)
    with ssh_client.sftp_session() as sftp:
        sftp.put(local_path, remote_path, callback=progress_callback)
    return remote_path


def download_file(
    ssh_client: SSHClientWrapper,
    remote_path: str,
    local_path: str,
    library_root: str,
    *,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> None:
    canonical = _canonical_library_path(ssh_client, remote_path, library_root)
    logging.info("Downloading %s -> %s", canonical, local_path)
    ssh_client.download_file(canonical, local_path, callback=progress_callback)


def delete_entry(
    ssh_client: SSHClientWrapper,
    remote_path: str,
    is_dir: bool,
    library_root: str,
) -> None:
    """Delete a book file (with its ``.sdr`` sidecar) or a whole folder."""
    remote_path = posixpath.normpath(remote_path)
    _ensure_writable(ssh_client, remote_path, library_root)
    sdr_path = remote_path + SDR_SUFFIX
    has_sidecar = not is_dir and _test_path(ssh_client, "d", sdr_path)
    if has_sidecar:
        _canonical_library_path(ssh_client, sdr_path, library_root)
    if is_dir:
        logging.info("Deleting directory %s", remote_path)
        ssh_client.exec_checked(f"rm -rf -- {shlex.quote(remote_path)}")
        return
    logging.info("Deleting file %s", remote_path)
    ssh_client.exec_checked(f"rm -f -- {shlex.quote(remote_path)}")
    if has_sidecar:
        logging.info("Deleting sidecar directory %s", sdr_path)
        ssh_client.exec_checked(f"rm -rf -- {shlex.quote(sdr_path)}")


def create_folder(
    ssh_client: SSHClientWrapper, remote_dir: str, name: str, library_root: str
) -> str:
    """Create *name* below *remote_dir*; returns the new directory path."""
    name = name.strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise RuntimeError("文件夹名称无效。")
    canonical_dir = _ensure_writable(ssh_client, remote_dir, library_root)
    remote_path = posixpath.join(canonical_dir, name)
    if ssh_client.file_exists(remote_path):
        raise RuntimeError(f"同名文件或文件夹已存在：{name}")
    logging.info("Creating directory %s", remote_path)
    ssh_client.exec_checked(f"mkdir -- {shlex.quote(remote_path)}")
    return remote_path
