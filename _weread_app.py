"""Verified remote installer for the official RemarkableWeRead application."""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import shlex
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

import _package_download
import _tap_page_turn as tap


OFFICIAL_URL = (
    "https://cdn.weread.qq.com/app/remarkable/"
    "remarkable-weread-v1.0.0-universal-release.zip"
)
BUNDLED_MANIFEST = Path(__file__).with_name("weread-app") / "manifest.json"
MAX_PACKAGE_BYTES = 48 * 1024 * 1024
MAX_UNPACKED_BYTES = 72 * 1024 * 1024
MAX_MEMBERS = 128
INSTALL_ROOT = "/home/root/.local/opt/remarkable-weread"
OFFICIAL_FILES = {
    f"{INSTALL_ROOT}/bin/remarkable-weread":
        "5a117d08c8503bad49b65a54357cdc1698b0ae3e52550d704fe9c6262f061ac7",
    f"{INSTALL_ROOT}/bin/start-remarkable-weread.sh":
        "8fa38f84242cce01f9e6ef547e075a166d8d61255e3df8f9e1292d9876c337e8",
    f"{INSTALL_ROOT}/systemd/remarkable-weread-app.service":
        "5ae39c435f1f47e34f2805b6eb8c0cbdeca398ff22d99c7a572321380547b185",
}
class WeReadAppState(Enum):
    INCOMPATIBLE = "incompatible"
    NOT_INSTALLED = "not_installed"
    REPAIR_AVAILABLE = "repair_available"
    INSTALLED = "installed"
    BROKEN = "broken"


@dataclass(frozen=True)
class WeReadAppPackage:
    name: str
    version: str
    source_commit: str
    asset: str
    size: int
    sha256: str
    urls: tuple[str, ...]
    inner_asset: str
    inner_size: int
    inner_sha256: str
    platforms: tuple[str, ...]
    architectures: tuple[str, ...]
    firmware_series: tuple[str, ...]

    @property
    def download_urls(self) -> tuple[str, ...]:
        return self.urls


@dataclass(frozen=True)
class WeReadAppDevice:
    platform: str
    architecture: str
    release_version: str


@dataclass(frozen=True)
class WeReadAppStatus:
    state: WeReadAppState
    device: WeReadAppDevice
    package: WeReadAppPackage | None = None
    detail: str = ""


def parse_manifest(data: bytes) -> WeReadAppPackage:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("微信读书 App 清单不是有效 JSON。") from exc
    required = {
        "schema_version", "name", "version", "source_commit", "asset",
        "size", "sha256", "urls", "inner", "supported",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise RuntimeError("微信读书 App 清单格式无效。")
    supported = document.get("supported")
    if (
        document.get("schema_version") != 1
        or document.get("name") != "RemarkableWeRead"
        or document.get("version") != "1.0.0"
        or document.get("source_commit") != "ba506af03a959092ecdea4e075e93ce9d204ac5e"
        or document.get("asset") != "remarkable-weread-v1.0.0-universal-release.zip"
        or document.get("size") != 43336015
        or document.get("sha256") != "3b2a918d67ab0d8dfbc7b2ce1cb96145a44af1b5520a9b6310ef183bc2a0dfd5"
        or document.get("urls") != [OFFICIAL_URL]
        or document.get("inner") != {
            "asset": "remarkable-weread-v1.0.0-universal-aarch64.tar.gz",
            "size": 43329981,
            "sha256": "b14398e9564e1e86f99329772f5eedc55c1cb65794cbba3ea89c175104ba8ccf",
        }
        or not isinstance(supported, dict)
        or set(supported) != {"platforms", "architectures", "firmware_series"}
        or supported.get("platforms") != ["ferrari", "chiappa"]
        or supported.get("architectures") != ["aarch64"]
        or supported.get("firmware_series") != ["3.28"]
    ):
        raise RuntimeError("微信读书 App 清单与内置信任信息不一致。")
    urls = document.get("urls")
    return WeReadAppPackage(
        document["name"], document["version"], document["source_commit"],
        document["asset"], document["size"], document["sha256"], tuple(urls),
        document["inner"]["asset"], document["inner"]["size"],
        document["inner"]["sha256"],
        tuple(supported["platforms"]), tuple(supported["architectures"]),
        tuple(supported["firmware_series"]),
    )


def trusted_package() -> WeReadAppPackage:
    if not BUNDLED_MANIFEST.is_file():
        raise RuntimeError("缺少内置微信读书 App 信任清单。")
    return parse_manifest(BUNDLED_MANIFEST.read_bytes())


def _platform_from_machine(machine: str) -> str:
    normalized = machine.casefold()
    for platform in ("ferrari", "chiappa"):
        if platform in normalized:
            return platform
    return ""


def inspect_device(ssh_client) -> WeReadAppDevice:
    command = r"""
arch=$(uname -m)
machine=$(cat /sys/devices/soc0/machine 2>/dev/null || tr -d "\000" < /proc/device-tree/model 2>/dev/null || true)
version=$(sed -n 's/^IMG_VERSION=//p' /etc/os-release 2>/dev/null | tail -n 1 | sed 's/^"//;s/"$//')
printf "arch=%s\nmachine=%s\nversion=%s\n" "$arch" "$machine" "$version"
"""
    values = {}
    for line in ssh_client.exec_checked(command).splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"arch", "machine", "version"}:
            values[key] = value.strip()
    return WeReadAppDevice(
        _platform_from_machine(values.get("machine", "")),
        values.get("arch", ""),
        values.get("version", ""),
    )


def _supported(device: WeReadAppDevice, package: WeReadAppPackage) -> bool:
    return (
        device.platform in package.platforms
        and device.architecture in package.architectures
        and any(
            device.release_version == series
            or device.release_version.startswith(series + ".")
            for series in package.firmware_series
        )
    )


def _require_supported(device: WeReadAppDevice, package: WeReadAppPackage) -> None:
    if not _supported(device, package):
        raise RuntimeError(
            "微信读书 App 仅支持运行 3.28 系列固件的 Paper Pro 和 Move（aarch64），"
            "未下载或修改设备。"
        )


def _installed_file_hashes(ssh_client) -> dict[str, str]:
    quoted = " ".join(shlex.quote(path) for path in OFFICIAL_FILES)
    output = ssh_client.exec_checked(
        "for p in " + quoted + "; do "
        "if [ -f \"$p\" ]; then printf '%s=' \"$p\"; sha256sum \"$p\" | awk '{print $1}'; "
        "else printf '%s=missing\\n' \"$p\"; fi; done"
    )
    result = {}
    for line in output.splitlines():
        path, separator, digest = line.partition("=")
        if separator and path in OFFICIAL_FILES:
            result[path] = digest.strip()
    return result


def get_status(ssh_client) -> WeReadAppStatus:
    package = trusted_package()
    device = inspect_device(ssh_client)
    if not _supported(device, package):
        return WeReadAppStatus(
            WeReadAppState.INCOMPATIBLE, device, detail=(
                "仅支持 Paper Pro/Move 的 3.28 系列固件。"
            )
        )
    try:
        hashes = _installed_file_hashes(ssh_client)
        if set(hashes) != set(OFFICIAL_FILES):
            raise RuntimeError("无法完整读取微信读书 App 文件状态。")
    except Exception as exc:
        return WeReadAppStatus(WeReadAppState.BROKEN, device, package, str(exc))
    present = [digest != "missing" for digest in hashes.values()]
    if not any(present):
        return WeReadAppStatus(WeReadAppState.NOT_INSTALLED, device, package)
    if hashes == OFFICIAL_FILES:
        return WeReadAppStatus(WeReadAppState.INSTALLED, device, package)
    return WeReadAppStatus(
        WeReadAppState.REPAIR_AVAILABLE, device, package,
        "官方程序文件不完整或版本不匹配，可保留用户数据进行修复。",
    )


def _validate_archive(path: Path, package: WeReadAppPackage) -> None:
    data_size = path.stat().st_size
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if data_size != package.inner_size or digest != package.inner_sha256:
        raise RuntimeError("微信读书 App 内层安装包与固定清单校验不匹配。")
    total = 0
    names = set()
    metadata = {}
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if not 0 < len(members) <= MAX_MEMBERS:
                raise RuntimeError("微信读书 App 安装包成员数量异常。")
            for member in members:
                pure = PurePosixPath(member.name)
                if (
                    pure.is_absolute()
                    or not pure.parts
                    or pure.parts[0] != "remarkable-weread"
                    or any(part in ("", ".", "..") for part in pure.parts)
                    or member.name in names
                    or not (member.isfile() or member.isdir())
                ):
                    raise RuntimeError("微信读书 App 安装包包含不安全路径或成员。")
                names.add(member.name)
                total += member.size
                if total > MAX_UNPACKED_BYTES:
                    raise RuntimeError("微信读书 App 安装包解压后过大。")
            for name in (
                "remarkable-weread/install.sh", "remarkable-weread/LICENSE",
                "remarkable-weread/NOTICE", "remarkable-weread/VERSION",
                "remarkable-weread/SOURCE_COMMIT",
            ):
                member = archive.getmember(name)
                if not member.isfile():
                    raise RuntimeError("微信读书 App 安装包缺少可信元数据。")
                metadata[name] = archive.extractfile(member).read()
    except (tarfile.TarError, KeyError, OSError) as exc:
        raise RuntimeError("微信读书 App 安装包结构无效。") from exc
    if metadata["remarkable-weread/VERSION"].strip() != package.version.encode():
        raise RuntimeError("微信读书 App 安装包版本不匹配。")
    if metadata["remarkable-weread/SOURCE_COMMIT"].strip() != package.source_commit.encode():
        raise RuntimeError("微信读书 App 安装包来源不匹配。")


def _read_verified_outer(source: Path, package: WeReadAppPackage) -> bytes:
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise RuntimeError("无法读取微信读书官方下载 ZIP。") from exc
    if size != package.size or size > MAX_PACKAGE_BYTES:
        raise RuntimeError("微信读书官方下载 ZIP 与固定清单校验不匹配。")
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise RuntimeError("无法读取微信读书官方下载 ZIP。") from exc
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != package.sha256:
        raise RuntimeError("微信读书官方下载 ZIP 与固定清单校验不匹配。")
    return payload


def _extract_verified_inner_bytes(payload: bytes, package: WeReadAppPackage) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as release:
            members = release.infolist()
            if not 0 < len(members) <= MAX_MEMBERS:
                raise RuntimeError("微信读书官方下载 ZIP 成员数量异常。")
            names = set()
            total = 0
            for item in members:
                name = item.filename
                parts = name.split("/")[:-1] if item.is_dir() else name.split("/")
                mode = item.external_attr >> 16
                if (
                    not parts
                    or name.startswith("/")
                    or "\\" in name
                    or any(part in ("", ".", "..") for part in parts)
                    or name in names
                    or stat.S_ISLNK(mode)
                ):
                    raise RuntimeError("微信读书官方下载 ZIP 包含不安全路径或成员。")
                names.add(name)
                total += item.file_size
                if total > MAX_UNPACKED_BYTES:
                    raise RuntimeError("微信读书官方下载 ZIP 解压后过大。")
            matches = [item for item in members if item.filename == package.inner_asset]
            if len(matches) != 1 or matches[0].is_dir() or matches[0].file_size != package.inner_size:
                raise RuntimeError("微信读书官方下载 ZIP 的内层安装包结构无效。")
            with release.open(matches[0]) as inner:
                data = inner.read(package.inner_size + 1)
    except (zipfile.BadZipFile, OSError) as exc:
        raise RuntimeError("微信读书官方下载 ZIP 结构无效。") from exc
    if len(data) != package.inner_size or hashlib.sha256(data).hexdigest() != package.inner_sha256:
        raise RuntimeError("微信读书 App 内层安装包与固定清单校验不匹配。")
    return data


def _extract_verified_inner(source: Path, package: WeReadAppPackage) -> bytes:
    return _extract_verified_inner_bytes(_read_verified_outer(source, package), package)


def store_local_package(source_path: str, state_dir: str) -> Path:
    package = trusted_package()
    source = Path(source_path)
    payload = _read_verified_outer(source, package)
    inner = _extract_verified_inner_bytes(payload, package)
    cache = Path(state_dir) / "weread-app"
    tap._write_atomic(cache / package.asset, payload)
    destination = cache / package.inner_asset
    tap._write_atomic(destination, inner)
    _validate_archive(destination, package)
    return destination


def download_package(package: WeReadAppPackage, state_dir: str) -> Path:
    cache = Path(state_dir) / "weread-app"
    outer = _package_download.download_verified_package(
        package, cache / package.asset, MAX_PACKAGE_BYTES,
        feature_label="微信读书 App",
        mismatch_message="微信读书官方下载 ZIP 与固定清单校验不匹配。",
        log_label="WeRead app",
        store=lambda source: store_local_package(source, state_dir),
    )
    inner = _extract_verified_inner(outer, package)
    destination = cache / package.inner_asset
    tap._write_atomic(destination, inner)
    _validate_archive(destination, package)
    return destination


def install(ssh_client, package: WeReadAppPackage, archive_path: Path) -> WeReadAppStatus:
    trusted = trusted_package()
    if package != trusted:
        raise RuntimeError("微信读书 App 安装包不在本地信任清单中。")
    device = inspect_device(ssh_client)
    _require_supported(device, package)
    _validate_archive(Path(archive_path), package)
    token = secrets.token_hex(8)
    remote_archive = f"/tmp/rmtool-weread-app-{token}.tar.gz"
    ssh_client.transfer_file(str(archive_path), remote_archive)
    archive_q = shlex.quote(remote_archive)
    expected_sha = shlex.quote(package.inner_sha256)
    script = f"""set -eu
archive={archive_q}
stage=''
cleanup() {{ rm -f "$archive"; [ -z "$stage" ] || rm -rf "$stage"; }}
trap cleanup EXIT HUP INT TERM
arch=$(uname -m)
machine=$(cat /sys/devices/soc0/machine 2>/dev/null || tr -d '\\000' < /proc/device-tree/model 2>/dev/null || true)
version=$(sed -n 's/^IMG_VERSION=//p' /etc/os-release 2>/dev/null | tail -n 1 | tr -d '\"')
[ "$arch" = aarch64 ] || {{ echo 'unsupported architecture' >&2; exit 1; }}
case "$(printf '%s' "$machine" | tr '[:upper:]' '[:lower:]')" in
  *ferrari*|*chiappa*) ;;
  *) echo 'unsupported device' >&2; exit 1 ;;
esac
case "$version" in 3.28|3.28.*) ;; *) echo 'unsupported firmware series' >&2; exit 1 ;; esac
[ "$(sha256sum "$archive" | awk '{{print $1}}')" = {expected_sha} ] || {{ echo 'archive checksum mismatch' >&2; exit 1; }}
stage=$(mktemp -d /tmp/rmtool-weread-app.XXXXXXXX)
tar -tzf "$archive" | grep -q '^remarkable-weread/install.sh$' || {{ echo 'installer missing' >&2; exit 1; }}
tar -xzf "$archive" -C "$stage"
[ -f "$stage/remarkable-weread/install.sh" ] || {{ echo 'installer missing' >&2; exit 1; }}
sh "$stage/remarkable-weread/install.sh"
"""
    try:
        ssh_client.exec_checked("sh -c " + shlex.quote(script), timeout=1800)
    finally:
        try:
            ssh_client.exec_checked(f"rm -f {archive_q}")
        except Exception:
            pass
    status = get_status(ssh_client)
    if status.state is not WeReadAppState.INSTALLED:
        raise RuntimeError(status.detail or "微信读书 App 安装后校验失败。")
    return status


def install_online(ssh_client, state_dir: str) -> WeReadAppStatus:
    package = trusted_package()
    device = inspect_device(ssh_client)
    _require_supported(device, package)
    archive = download_package(package, state_dir)
    return install(ssh_client, package, archive)
