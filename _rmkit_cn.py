"""Safe original-UI Chinese localization for supported reMarkable firmware."""

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import logging
import os
from pathlib import Path
import posixpath
import re
import shlex
import tempfile
from typing import Optional, Union
from urllib import request
from xml.sax.saxutils import escape

from _ssh import remount_rw


REPO_URL = "https://github.com/boangs/rmkit"
RELEASES_URL = "https://github.com/boangs/rmkit/releases"
INSTALL_GUIDE_URL = "https://github.com/boangs/rmkit#readme"

SUPPORTED_FIRMWARE = "20260612085811"
STOCK_FRENCH_QM_SHA256 = (
    "8e0db0f7a2d3116469e1aae4f52657ccc38d0422b5b958ae512554bd018f285e"
)
LOCALIZED_QM_SHA256 = (
    "47ba9d8a6f38b3763d013ecc489d44e8742704404b50a5de102b42e33dfebbfb"
)
TRANSLATION_MANIFEST_URL = (
    "https://github.com/pretenderlu/rmtool/releases/download/"
    "localization-assets/manifest.json"
)
TRANSLATION_RELEASE_URL = (
    "https://github.com/pretenderlu/rmtool/releases/download/localization-assets"
)
TRANSLATION_MANIFEST_SCHEMA = 1
TRANSLATION_DOWNLOAD_TIMEOUT = 10
MAX_MANIFEST_BYTES = 256 * 1024
MAX_TRANSLATION_BYTES = 16 * 1024 * 1024
CARRIER_LANGUAGE = "fr_FR"
CONFIG_PATH = "/home/root/.config/remarkable/xochitl.conf"
QM_PATH = "/usr/share/remarkable/xochitl/translations/reMarkable_fr.qm"
BACKUP_DIR = "/home/root/.local/share/rmtool/ui-zh-backup"
BACKUP_CONFIG_PATH = f"{BACKUP_DIR}/xochitl.conf"
BACKUP_QM_PATH = f"{BACKUP_DIR}/reMarkable_fr.qm"
BACKUP_READY_PATH = f"{BACKUP_DIR}/.ready"
BUNDLED_FONT_NAME = "NotoSansCJKsc-Regular.otf"
BUNDLED_FONT_SHA256 = (
    "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"
)
FONT_DIR = "/home/root/.local/share/fonts/rmtool-localization"
BUNDLED_FONT_PATH = f"{FONT_DIR}/{BUNDLED_FONT_NAME}"
CUSTOM_FONT_PATHS = {
    ".otf": f"{FONT_DIR}/CustomCJKFont.otf",
    ".ttf": f"{FONT_DIR}/CustomCJKFont.ttf",
}
FONT_MARKER_PATH = f"{BACKUP_DIR}/managed-font.json"
FONTCONFIG_DIR = "/home/root/.config/fontconfig"
FONTCONFIG_FILE = f"{FONTCONFIG_DIR}/fonts.conf"
FONTCONFIG_BACKUP_PATH = f"{BACKUP_DIR}/fonts.conf.before-localization"
MANAGED_FONT_PATHS = frozenset((BUNDLED_FONT_PATH, *CUSTOM_FONT_PATHS.values()))
PRIMARY_FONT_COMMAND = "fc-match --format='%{file}\\n' sans-serif | head -n 1"
CJK_FONT_LIST_COMMAND = "fc-list --format='%{file}\\n' ':lang=zh-cn'"

_FIRMWARE_RE = re.compile(r"^[0-9]{14}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]+\.qm$")
_RELEASE_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


@dataclass(frozen=True)
class TranslationPackage:
    firmware: str
    stock_french_sha256: str
    localized_qm_sha256: str
    asset: str
    size: int
    release_version: str = ""
    channel: str = "stable"

    @property
    def download_url(self) -> str:
        return f"{TRANSLATION_RELEASE_URL}/{self.asset}"


def _default_translation_package() -> TranslationPackage:
    return TranslationPackage(
        firmware=SUPPORTED_FIRMWARE,
        stock_french_sha256=STOCK_FRENCH_QM_SHA256,
        localized_qm_sha256=LOCALIZED_QM_SHA256,
        asset=f"reMarkable_zh_CN-{SUPPORTED_FIRMWARE}.qm",
        size=0,
        release_version="3.27.3.0",
        channel="stable",
    )


def parse_translation_manifest(data: bytes) -> dict[str, TranslationPackage]:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("云端汉化清单不是有效的 UTF-8 JSON。") from exc
    if not isinstance(document, dict) or document.get("schema") != TRANSLATION_MANIFEST_SCHEMA:
        raise RuntimeError("云端汉化清单版本不受支持。")
    entries = document.get("firmwares")
    if not isinstance(entries, dict):
        raise RuntimeError("云端汉化清单缺少固件列表。")

    packages: dict[str, TranslationPackage] = {}
    for firmware, entry in entries.items():
        if not isinstance(firmware, str) or not _FIRMWARE_RE.fullmatch(firmware):
            raise RuntimeError("云端汉化清单包含无效的固件版本。")
        if not isinstance(entry, dict):
            raise RuntimeError(f"固件 {firmware} 的汉化清单格式无效。")
        stock_sha256 = entry.get("stock_french_sha256")
        localized_sha256 = entry.get("sha256")
        asset = entry.get("asset")
        size = entry.get("size")
        release_version = entry.get("release_version")
        channel = entry.get("channel")
        if not isinstance(stock_sha256, str) or not _SHA256_RE.fullmatch(stock_sha256):
            raise RuntimeError(f"固件 {firmware} 的原始法语文件哈希无效。")
        if not isinstance(localized_sha256, str) or not _SHA256_RE.fullmatch(localized_sha256):
            raise RuntimeError(f"固件 {firmware} 的中文文件哈希无效。")
        if not isinstance(asset, str) or not _ASSET_RE.fullmatch(asset):
            raise RuntimeError(f"固件 {firmware} 的云端文件名无效。")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or size > MAX_TRANSLATION_BYTES
        ):
            raise RuntimeError(f"固件 {firmware} 的中文文件大小无效。")
        if (
            not isinstance(release_version, str)
            or not _RELEASE_VERSION_RE.fullmatch(release_version)
        ):
            raise RuntimeError(f"固件 {firmware} 的对外版本号无效。")
        if channel not in ("stable", "beta"):
            raise RuntimeError(f"固件 {firmware} 的发布类型无效。")
        packages[firmware] = TranslationPackage(
            firmware=firmware,
            stock_french_sha256=stock_sha256,
            localized_qm_sha256=localized_sha256,
            asset=asset,
            size=size,
            release_version=release_version,
            channel=channel,
        )
    return packages


def _download_limited(url: str, max_bytes: int) -> bytes:
    http_request = request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "rmtool-localization/1",
        },
    )
    with request.urlopen(http_request, timeout=TRANSLATION_DOWNLOAD_TIMEOUT) as response:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise RuntimeError("云端汉化文件超过允许大小。")
            except ValueError as exc:
                raise RuntimeError("云端返回了无效的文件长度。") from exc
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise RuntimeError("云端汉化文件超过允许大小。")
    return data


def _write_cache_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.tmp")
    try:
        temporary_path.write_bytes(data)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _translation_cache_dir(state_dir: Union[str, Path]) -> Path:
    return Path(state_dir) / "cache" / "localization"


def load_translation_catalog(
    state_dir: Union[str, Path], *, refresh: bool = True
) -> dict[str, TranslationPackage]:
    manifest_path = _translation_cache_dir(state_dir) / "manifest.json"
    remote_error: Optional[Exception] = None
    if refresh:
        try:
            data = _download_limited(TRANSLATION_MANIFEST_URL, MAX_MANIFEST_BYTES)
            packages = parse_translation_manifest(data)
            _write_cache_file(manifest_path, data)
            return packages
        except Exception as exc:
            remote_error = exc
            logging.warning("Could not refresh localization manifest: %s", exc)

    if manifest_path.is_file():
        try:
            return parse_translation_manifest(manifest_path.read_bytes())
        except Exception as exc:
            logging.warning("Cached localization manifest is invalid: %s", exc)

    if remote_error:
        raise RuntimeError("无法获取云端汉化清单，且本地没有可用缓存。") from remote_error
    raise RuntimeError("本地没有可用的汉化清单。")


def download_translation_package(
    package: TranslationPackage, state_dir: Union[str, Path]
) -> Path:
    destination = _translation_cache_dir(state_dir) / package.firmware / package.asset
    if destination.is_file():
        data = destination.read_bytes()
        if (
            len(data) == package.size
            and hashlib.sha256(data).hexdigest() == package.localized_qm_sha256
        ):
            return destination

    data = _download_limited(package.download_url, MAX_TRANSLATION_BYTES)
    if len(data) != package.size:
        raise RuntimeError("下载的中文翻译文件大小与云端清单不一致。")
    if hashlib.sha256(data).hexdigest() != package.localized_qm_sha256:
        raise RuntimeError("下载的中文翻译文件校验失败。")
    _write_cache_file(destination, data)
    return destination


class LocalizationState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    INSTALLED_NOT_ENABLED = "installed_not_enabled"
    ENABLED = "enabled"


@dataclass(frozen=True)
class LocalizationStatus:
    state: LocalizationState
    firmware: str
    has_cjk_font: bool = False
    package: Optional[TranslationPackage] = field(
        default=None, compare=False, repr=False
    )
    available_packages: Optional[tuple[TranslationPackage, ...]] = field(
        default=None, compare=False, repr=False
    )


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


def _validate_font_file(local_path: str) -> Path:
    path = Path(local_path)
    if path.suffix.lower() not in CUSTOM_FONT_PATHS:
        raise RuntimeError("仅支持非空的 TTF/OTF 字体文件。")
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("字体文件不存在或为空。")
    return path


def fontconfig_override(font_family: str) -> str:
    family = escape(font_family.strip())
    if not family:
        raise RuntimeError("无法识别所选字体的字体族。")
    return f"""<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <!-- ponytail: user-level UI font override; restore the prior file to undo. -->
  <alias binding="strong">
    <family>sans-serif</family>
    <prefer><family>{family}</family></prefer>
  </alias>
  <alias binding="strong">
    <family>Noto Sans SC</family>
    <prefer><family>{family}</family></prefer>
  </alias>
</fontconfig>
"""


def refresh_font_cache(ssh_client, *paths: str) -> None:
    cache_args = " ".join(shlex.quote(path) for path in paths)
    stdout = ssh_client.exec_checked(f"fc-cache -f -v {cache_args}")
    logging.info("fc-cache output: %s", stdout.strip())


def upload_font(
    ssh_client,
    local_path: str,
    remote_dir: str,
    remote_name: str,
    *,
    fontconfig_local_path: Optional[str] = None,
    fontconfig_remote_path: Optional[str] = None,
    refresh_cache: bool = True,
) -> str:
    """Upload a font and optionally a Fontconfig override using the shared path."""
    path = _validate_font_file(local_path)
    if bool(fontconfig_local_path) != bool(fontconfig_remote_path):
        raise ValueError("Fontconfig local and remote paths must be provided together.")

    remote_path = posixpath.join(remote_dir, remote_name)
    remote_temp_path = f"{remote_path}.tmp"
    fontconfig_temp_path = (
        f"{fontconfig_remote_path}.tmp" if fontconfig_remote_path else None
    )
    cache_paths = [remote_dir]
    with remount_rw(ssh_client):
        ssh_client.exec_checked(f"mkdir -p {shlex.quote(remote_dir)}")
        try:
            ssh_client.transfer_file(str(path), remote_temp_path)
            ssh_client.exec_checked(f"chmod 0644 {shlex.quote(remote_temp_path)}")
            if fontconfig_local_path and fontconfig_remote_path:
                fontconfig_dir = posixpath.dirname(fontconfig_remote_path)
                ssh_client.exec_checked(f"mkdir -p {shlex.quote(fontconfig_dir)}")
                ssh_client.transfer_file(fontconfig_local_path, fontconfig_temp_path)
                ssh_client.exec_checked(
                    f"chmod 0644 {shlex.quote(fontconfig_temp_path)}"
                )
                cache_paths.append(fontconfig_dir)
            ssh_client.exec_checked(
                f"mv -f {shlex.quote(remote_temp_path)} {shlex.quote(remote_path)}"
            )
            if fontconfig_temp_path and fontconfig_remote_path:
                ssh_client.exec_checked(
                    f"mv -f {shlex.quote(fontconfig_temp_path)} "
                    f"{shlex.quote(fontconfig_remote_path)}"
                )
            if refresh_cache:
                refresh_font_cache(ssh_client, *cache_paths)
        except Exception:
            temp_paths = [remote_temp_path]
            if fontconfig_temp_path:
                temp_paths.append(fontconfig_temp_path)
            try:
                ssh_client.exec_checked(
                    "rm -f " + " ".join(shlex.quote(item) for item in temp_paths)
                )
            except Exception:
                logging.exception("Could not remove temporary font upload files")
            raise
    return remote_path


def has_cjk_font(ssh_client) -> bool:
    """Return whether the active sans-serif font has Simplified Chinese coverage."""
    primary = ssh_client.exec_checked(PRIMARY_FONT_COMMAND).splitlines()
    cjk_fonts = {
        line.strip()
        for line in ssh_client.exec_checked(CJK_FONT_LIST_COMMAND).splitlines()
        if line.strip()
    }
    return bool(primary and primary[0].strip() in cjk_fonts)


def _remote_sha256(ssh_client, path: str) -> str:
    return hashlib.sha256(_read_bytes(ssh_client, path)).hexdigest()


def _font_marker(ssh_client) -> Optional[tuple[str, str, bool]]:
    if not _file_exists(ssh_client, FONT_MARKER_PATH):
        return None
    try:
        marker = json.loads(_read_bytes(ssh_client, FONT_MARKER_PATH))
        path = marker["path"]
        digest = marker["sha256"]
        had_fontconfig = marker["had_fontconfig"]
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return None
    if (
        not isinstance(path, str)
        or not isinstance(digest, str)
        or path not in MANAGED_FONT_PATHS
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or type(had_fontconfig) is not bool
    ):
        return None
    return path, digest, had_fontconfig


def _write_font_marker(
    ssh_client, path: str, digest: str, had_fontconfig: bool
) -> None:
    marker = json.dumps(
        {
            "path": path,
            "sha256": digest,
            "had_fontconfig": had_fontconfig,
        },
        separators=(",", ":"),
    ).encode("ascii")
    with tempfile.NamedTemporaryFile(delete=False) as marker_file:
        marker_file.write(marker)
        local_marker_path = marker_file.name
    try:
        ssh_client.exec_checked(f"mkdir -p {BACKUP_DIR}")
        ssh_client.transfer_file(local_marker_path, f"{FONT_MARKER_PATH}.tmp")
        ssh_client.exec_checked(f"chmod 0644 {FONT_MARKER_PATH}.tmp")
        ssh_client.exec_checked(f"mv -f {FONT_MARKER_PATH}.tmp {FONT_MARKER_PATH}")
        if _font_marker(ssh_client) != (path, digest, had_fontconfig):
            raise RuntimeError("汉化字体标记写入后校验失败，已停止操作。")
    except Exception:
        try:
            ssh_client.exec_checked(f"rm -f {FONT_MARKER_PATH}.tmp")
        except Exception:
            logging.exception("Could not remove the temporary font marker")
        raise
    finally:
        Path(local_marker_path).unlink(missing_ok=True)


def _remove_matching_font(ssh_client, path: str, digest: str) -> bool:
    if not _file_exists(ssh_client, path) or _remote_sha256(ssh_client, path) != digest:
        return False
    ssh_client.exec_checked(f"rm -f {shlex.quote(path)}")
    return True


def _validate_font_rollback(ssh_client) -> None:
    if not _file_exists(ssh_client, FONT_MARKER_PATH):
        return
    marker = _font_marker(ssh_client)
    if not marker:
        raise RuntimeError("汉化字体标记无效，已停止操作。")
    if marker[2] and not _file_exists(ssh_client, FONTCONFIG_BACKUP_PATH):
        raise RuntimeError("汉化字体配置备份不完整，已停止操作。")


def _restore_fontconfig(ssh_client, had_fontconfig: bool) -> None:
    if had_fontconfig:
        if not _file_exists(ssh_client, FONTCONFIG_BACKUP_PATH):
            raise RuntimeError("汉化字体配置备份不完整，已停止操作。")
        ssh_client.exec_checked(
            f"cp -p {FONTCONFIG_BACKUP_PATH} {FONTCONFIG_FILE}.tmp"
        )
        ssh_client.exec_checked(f"mv -f {FONTCONFIG_FILE}.tmp {FONTCONFIG_FILE}")
    else:
        ssh_client.exec_checked(f"rm -f {FONTCONFIG_FILE} {FONTCONFIG_FILE}.tmp")


def _write_remote_bytes(ssh_client, remote_path: str, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(delete=False) as local_file:
        local_file.write(data)
        local_path = local_file.name
    try:
        ssh_client.transfer_file(local_path, f"{remote_path}.tmp")
        ssh_client.exec_checked(f"chmod 0644 {remote_path}.tmp")
        ssh_client.exec_checked(f"mv -f {remote_path}.tmp {remote_path}")
    finally:
        Path(local_path).unlink(missing_ok=True)


def _remove_managed_font(ssh_client) -> bool:
    if not _file_exists(ssh_client, FONT_MARKER_PATH):
        return False
    marker = _font_marker(ssh_client)
    if not marker:
        raise RuntimeError("汉化字体标记无效，已停止操作。")
    path, digest, had_fontconfig = marker
    _restore_fontconfig(ssh_client, had_fontconfig)
    removed = _remove_matching_font(ssh_client, path, digest)
    refresh_font_cache(ssh_client, FONT_DIR, FONTCONFIG_DIR)
    ssh_client.exec_checked(
        f"rm -f {FONT_MARKER_PATH} {FONT_MARKER_PATH}.tmp "
        f"{FONTCONFIG_BACKUP_PATH} {FONTCONFIG_BACKUP_PATH}.tmp"
    )
    return removed


def _install_managed_font(
    ssh_client, local_path: str, font_family: Optional[str]
) -> None:
    path = _validate_font_file(local_path)
    override = fontconfig_override(font_family or "")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if path.name == BUNDLED_FONT_NAME and digest != BUNDLED_FONT_SHA256:
        raise RuntimeError("内置 Noto 字体校验失败，已停止操作。")

    target = (
        BUNDLED_FONT_PATH
        if digest == BUNDLED_FONT_SHA256
        else CUSTOM_FONT_PATHS[path.suffix.lower()]
    )
    previous = _font_marker(ssh_client)
    previous_marker = _read_bytes(ssh_client, FONT_MARKER_PATH) if previous else None
    if _file_exists(ssh_client, target):
        if (
            not previous
            or previous[0] != target
            or _remote_sha256(ssh_client, target) != previous[1]
        ):
            raise RuntimeError("rmtool 字体目标已被其他文件占用，已停止操作。")

    had_current_fontconfig = _file_exists(ssh_client, FONTCONFIG_FILE)
    current_fontconfig = (
        _read_bytes(ssh_client, FONTCONFIG_FILE) if had_current_fontconfig else None
    )
    if previous:
        had_original_fontconfig = previous[2]
    else:
        had_original_fontconfig = had_current_fontconfig
        ssh_client.exec_checked(f"mkdir -p {BACKUP_DIR}")
        if had_original_fontconfig:
            ssh_client.exec_checked(
                f"cp -p {FONTCONFIG_FILE} {FONTCONFIG_BACKUP_PATH}.tmp"
            )
            ssh_client.exec_checked(
                f"mv -f {FONTCONFIG_BACKUP_PATH}.tmp {FONTCONFIG_BACKUP_PATH}"
            )
        else:
            ssh_client.exec_checked(
                f"rm -f {FONTCONFIG_BACKUP_PATH} {FONTCONFIG_BACKUP_PATH}.tmp"
            )

    font_rollback_path = None
    if previous and previous[0] == target and _file_exists(ssh_client, target):
        font_rollback_path = f"{target}.rmtool-rollback"
        ssh_client.exec_checked(
            f"rm -f {font_rollback_path} {font_rollback_path}.tmp"
        )
        ssh_client.exec_checked(f"cp -p {target} {font_rollback_path}.tmp")
        ssh_client.exec_checked(
            f"mv -f {font_rollback_path}.tmp {font_rollback_path}"
        )

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", delete=False
    ) as config_file:
        config_file.write(override)
        local_config_path = config_file.name
    try:
        upload_font(
            ssh_client,
            str(path),
            FONT_DIR,
            posixpath.basename(target),
            fontconfig_local_path=local_config_path,
            fontconfig_remote_path=FONTCONFIG_FILE,
        )
        if _remote_sha256(ssh_client, target) != digest:
            raise RuntimeError("字体上传后校验失败，已停止操作。")
        if not has_cjk_font(ssh_client):
            raise RuntimeError("所选字体不能作为支持简体中文的主界面字体，已撤销上传。")
        _write_font_marker(
            ssh_client, target, digest, had_original_fontconfig
        )
        if previous and previous[0] != target:
            try:
                _remove_matching_font(ssh_client, previous[0], previous[1])
            except Exception:
                logging.exception("Could not remove the superseded managed font")
        if font_rollback_path:
            try:
                ssh_client.exec_checked(
                    f"rm -f {font_rollback_path} {font_rollback_path}.tmp"
                )
            except Exception:
                logging.exception("Could not remove the managed-font rollback copy")
    except Exception as install_error:
        rollback_errors = []

        def rollback(description, action):
            try:
                action()
            except Exception as exc:
                logging.exception("Managed-font rollback failed: %s", description)
                rollback_errors.append(f"{description}: {exc}")

        if font_rollback_path:
            rollback(
                "restore previous font",
                lambda: ssh_client.exec_checked(
                    f"mv -f {font_rollback_path} {target}"
                ),
            )
            rollback(
                "remove font temporary file",
                lambda: ssh_client.exec_checked(f"rm -f {target}.tmp"),
            )
        else:
            rollback(
                "remove uploaded font",
                lambda: ssh_client.exec_checked(f"rm -f {target} {target}.tmp"),
            )
        if current_fontconfig is None:
            rollback(
                "remove generated fontconfig",
                lambda: ssh_client.exec_checked(
                    f"rm -f {FONTCONFIG_FILE} {FONTCONFIG_FILE}.tmp"
                ),
            )
        else:
            rollback(
                "restore previous fontconfig",
                lambda: _write_remote_bytes(
                    ssh_client, FONTCONFIG_FILE, current_fontconfig
                ),
            )
        rollback(
            "refresh font cache",
            lambda: refresh_font_cache(ssh_client, FONT_DIR, FONTCONFIG_DIR),
        )
        if not rollback_errors:
            if previous_marker is None:
                rollback(
                    "remove font marker",
                    lambda: ssh_client.exec_checked(
                        f"rm -f {FONT_MARKER_PATH} {FONT_MARKER_PATH}.tmp"
                    ),
                )
            else:
                rollback(
                    "restore previous font marker",
                    lambda: _write_remote_bytes(
                        ssh_client, FONT_MARKER_PATH, previous_marker
                    ),
                )
            if not previous and not rollback_errors:
                rollback(
                    "remove unused fontconfig backup",
                    lambda: ssh_client.exec_checked(
                        f"rm -f {FONTCONFIG_BACKUP_PATH} "
                        f"{FONTCONFIG_BACKUP_PATH}.tmp"
                    ),
                )
        if rollback_errors:
            raise RuntimeError(
                f"{install_error}；字体回滚未完整，已保留恢复数据："
                + "；".join(rollback_errors)
            ) from install_error
        raise
    finally:
        Path(local_config_path).unlink(missing_ok=True)


def _qm_kind(
    ssh_client,
    path: str = QM_PATH,
    package: Optional[TranslationPackage] = None,
) -> str:
    package = package or _default_translation_package()
    if not _file_exists(ssh_client, path):
        raise RuntimeError("法语载体文件不存在，已停止操作。")
    digest = hashlib.sha256(_read_bytes(ssh_client, path)).hexdigest()
    if digest == package.stock_french_sha256:
        return "stock"
    if digest == package.localized_qm_sha256:
        return "localized"
    raise RuntimeError("法语载体文件与支持的原版或中文版均不匹配，已停止操作。")


def _validate_backup(
    ssh_client, package: Optional[TranslationPackage] = None
) -> None:
    if not _file_exists(ssh_client, BACKUP_CONFIG_PATH):
        raise RuntimeError("汉化备份不完整，已停止操作。")
    _read_bytes(ssh_client, BACKUP_CONFIG_PATH)
    if _qm_kind(ssh_client, BACKUP_QM_PATH, package) != "stock":
        raise RuntimeError("汉化备份中的法语载体不是预期原版，已停止操作。")


def _firmware(ssh_client) -> str:
    return ssh_client.exec_checked("cat /etc/version").strip()


def _require_supported(
    ssh_client, package: Optional[TranslationPackage] = None
) -> str:
    package = package or _default_translation_package()
    firmware = _firmware(ssh_client)
    if firmware != package.firmware:
        raise RuntimeError(
            f"当前固件 {firmware or '未知'} 与汉化包 {package.firmware} 不匹配。"
        )
    return firmware


def _stop_xochitl(ssh_client) -> None:
    ssh_client.exec_checked("systemctl stop xochitl")
    state = ssh_client.exec_checked(
        "systemctl show xochitl -p ActiveState --value"
    ).strip()
    if state != "inactive":
        raise RuntimeError(f"xochitl 未完全停止，当前状态：{state or '未知'}。")


def get_localization_status(
    ssh_client, package: Optional[TranslationPackage] = None
) -> LocalizationStatus:
    package = package or _default_translation_package()
    firmware = _firmware(ssh_client)
    if firmware != package.firmware:
        return LocalizationStatus(LocalizationState.INCOMPATIBLE, firmware)

    carrier = _qm_kind(ssh_client, package=package)
    managed = _file_exists(ssh_client, BACKUP_READY_PATH)
    if managed:
        _validate_backup(ssh_client, package)
    elif carrier == "localized":
        raise RuntimeError("检测到中文载体，但缺少可还原的备份，已停止操作。")
    _validate_font_rollback(ssh_client)
    config = _read_bytes(ssh_client, CONFIG_PATH).decode("utf-8", "surrogateescape")
    if carrier == "localized" and _general_language(config) == CARRIER_LANGUAGE:
        state = LocalizationState.ENABLED
    elif managed:
        state = LocalizationState.INSTALLED_NOT_ENABLED
    else:
        state = LocalizationState.NOT_INSTALLED
    return LocalizationStatus(
        state, firmware, has_cjk_font(ssh_client), package
    )


def get_cloud_localization_status(
    ssh_client, state_dir: Union[str, Path]
) -> LocalizationStatus:
    firmware = _firmware(ssh_client)
    catalog = load_translation_catalog(state_dir)
    available_packages = tuple(
        sorted(catalog.values(), key=lambda item: item.firmware, reverse=True)
    )
    package = catalog.get(firmware)
    if package is None:
        return LocalizationStatus(
            LocalizationState.INCOMPATIBLE,
            firmware,
            available_packages=available_packages,
        )
    return replace(
        get_localization_status(ssh_client, package),
        available_packages=available_packages,
    )


def _prepare_backup(
    ssh_client, package: Optional[TranslationPackage] = None
) -> bool:
    if _file_exists(ssh_client, BACKUP_READY_PATH):
        return False

    ssh_client.exec_checked(f"mkdir -p {BACKUP_DIR}")
    ssh_client.exec_checked(
        f"cp -p {CONFIG_PATH} {BACKUP_CONFIG_PATH}.tmp"
    )
    ssh_client.exec_checked(
        f"mv -f {BACKUP_CONFIG_PATH}.tmp {BACKUP_CONFIG_PATH}"
    )
    ssh_client.exec_checked(f"cp -p {QM_PATH} {BACKUP_QM_PATH}.tmp")
    ssh_client.exec_checked(f"mv -f {BACKUP_QM_PATH}.tmp {BACKUP_QM_PATH}")
    _validate_backup(ssh_client, package)
    ssh_client.exec_checked(f"touch {BACKUP_READY_PATH}")
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


def enable_localization(
    ssh_client,
    qm_local_path: str,
    font_local_path: Optional[str] = None,
    font_family: Optional[str] = None,
    package: Optional[TranslationPackage] = None,
) -> LocalizationStatus:
    """Install the translation and activate it without restarting xochitl."""
    package = package or _default_translation_package()
    firmware = ""
    installed_font = False
    created_backup = False
    translation_rollback_complete = False
    try:
        firmware = _require_supported(ssh_client, package)
        current = get_localization_status(ssh_client, package)
        if current.state is LocalizationState.ENABLED and current.has_cjk_font:
            return current

        needs_translation = current.state is not LocalizationState.ENABLED
        if needs_translation:
            qm_path = Path(qm_local_path)
            if not qm_path.is_file() or qm_path.stat().st_size == 0:
                raise RuntimeError("中文翻译文件不存在或为空。")
            if (
                (package.size and qm_path.stat().st_size != package.size)
                or hashlib.sha256(qm_path.read_bytes()).hexdigest()
                != package.localized_qm_sha256
            ):
                raise RuntimeError("中文翻译文件校验失败，已停止操作。")

        if not current.has_cjk_font:
            if not font_local_path:
                raise RuntimeError("设备缺少简体中文字体，且未选择要安装的字体。")
            _install_managed_font(ssh_client, font_local_path, font_family)
            installed_font = True

        if not needs_translation:
            return LocalizationStatus(
                LocalizationState.ENABLED, firmware, True, package
            )

        _stop_xochitl(ssh_client)
        original_config = _read_bytes(ssh_client, CONFIG_PATH)
        localized_config = set_language_config(
            original_config.decode("utf-8", "surrogateescape"), CARRIER_LANGUAGE
        ).encode("utf-8", "surrogateescape")
        created_backup = _prepare_backup(ssh_client, package)

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
                except Exception:
                    ssh_client.exec_checked(
                        f"rm -f {QM_PATH}.tmp {CONFIG_PATH}.tmp"
                    )
                    _restore_from_backup(ssh_client)
                    translation_rollback_complete = True
                    raise
        finally:
            Path(local_config_path).unlink(missing_ok=True)

        return LocalizationStatus(
            LocalizationState.ENABLED, firmware, True, package
        )
    except Exception:
        font_rollback_complete = True
        if installed_font:
            try:
                _remove_managed_font(ssh_client)
            except Exception:
                font_rollback_complete = False
                logging.exception("Could not roll back the managed localization font")
        if (
            created_backup
            and translation_rollback_complete
            and font_rollback_complete
        ):
            try:
                ssh_client.exec_checked(f"rm -f {BACKUP_READY_PATH}")
            except Exception:
                logging.exception("Could not remove the rolled-back backup marker")
        raise
    finally:
        ssh_client.close()


def enable_cloud_localization(
    ssh_client,
    package: TranslationPackage,
    state_dir: Union[str, Path],
    font_local_path: Optional[str] = None,
    font_family: Optional[str] = None,
) -> LocalizationStatus:
    qm_path = download_translation_package(package, state_dir)
    return enable_localization(
        ssh_client,
        str(qm_path),
        font_local_path,
        font_family,
        package,
    )


def restore_localization(
    ssh_client, package: Optional[TranslationPackage] = None
) -> LocalizationStatus:
    """Restore the carrier catalog and original language, without restart."""
    package = package or _default_translation_package()
    try:
        firmware = _require_supported(ssh_client, package)
        if not _file_exists(ssh_client, BACKUP_READY_PATH):
            return get_localization_status(ssh_client, package)
        get_localization_status(ssh_client, package)

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
                except Exception:
                    ssh_client.exec_checked(
                        f"rm -f {QM_PATH}.tmp {CONFIG_PATH}.tmp"
                    )
                    raise
        finally:
            Path(local_config_path).unlink(missing_ok=True)

        _remove_managed_font(ssh_client)
        ssh_client.exec_checked(
            f"rm -f {BACKUP_READY_PATH} {BACKUP_CONFIG_PATH} {BACKUP_QM_PATH}"
        )
        return get_localization_status(ssh_client, package)
    finally:
        ssh_client.close()
