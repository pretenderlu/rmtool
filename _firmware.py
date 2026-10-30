"""Guarded official SWU handling. No raw partition writer or automatic reboot.

Discovery and native A/B approach derived from GPLv3 reManager/remarkable-go;
see NOTICE.md. Local container checks are NOT native signature verification.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import tempfile
import threading
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import _https
from _ssh import SSHClientWrapper

BUCKET = "https://remarkable-software.s3.us-east-2.amazonaws.com/"
DOWNLOAD = "https://dlqathbgqp3nv.cloudfront.net/"
PLATFORMS = ("ferrari", "chiappa", "tatsu", "rm1", "rm2")
MAX_SWU = 4 * 1024**3
MAX_DESCRIPTION = 256 * 1024
BASE = "/home/root/.rmtool-firmware"
UNIT = "rmtool-firmware"
KEY = "/usr/share/swupdate/swupdate-payload-key-pub.pem"
LPGPR = "/sys/devices/platform/lpgpr"
IMAGE_RE = re.compile(r"remarkable-production-image-(\d+\.\d+\.\d+\.\d+)-(ferrari|chiappa|tatsu|rm1|rm2)-public\.swu")


def version_key(value):
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value):
        raise RuntimeError("固件版本格式无效。")
    return tuple(map(int, value.split(".")))


@dataclass(frozen=True)
class Release:
    version: str
    platform: str
    filename: str
    size: int

    @property
    def channel(self):
        if self.version == "3.28.0.172":
            return "正式版"
        import _native_chinese
        channels = {p.channel for p in _native_chinese._trusted_catalog()
                    if p.release_version == self.version and p.platform == self.platform}
        return {frozenset({"stable"}): "正式版", frozenset({"beta"}): "测试版"}.get(
            frozenset(channels), "渠道未确认")


class _OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _official_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc not in
            {urllib.parse.urlsplit(BUCKET).netloc, urllib.parse.urlsplit(DOWNLOAD).netloc}):
        raise RuntimeError("拒绝非官方固件地址或重定向。")


def _open(url):
    _official_url(url)
    return _https.urlopen(url, timeout=60, handlers=(_OfficialRedirect(),))


def list_releases(platform):
    if platform not in PLATFORMS:
        raise RuntimeError("未知设备平台。")
    releases, seen, token = {}, set(), ""
    for _ in range(100):
        query = {"list-type": "2"}
        if token:
            query["continuation-token"] = token
        with _open(BUCKET + "?" + urllib.parse.urlencode(query)) as response:
            body = response.read(8 * 1024**2 + 1)
        if len(body) > 8 * 1024**2 or b"<!DOCTYPE" in body or b"<!ENTITY" in body:
            raise RuntimeError("官方清单超过限制或格式不安全。")
        root = ET.fromstring(body)
        for element in root.iter():
            element.tag = element.tag.rsplit("}", 1)[-1]
        if root.tag != "ListBucketResult":
            raise RuntimeError("官方清单格式无效。")
        for obj in root.findall("Contents"):
            name = obj.findtext("Key", "")
            match = IMAGE_RE.fullmatch(name)
            if match and match[2] == platform:
                size = int(obj.findtext("Size", "0"))
                if not 0 < size <= MAX_SWU:
                    raise RuntimeError("官方固件大小无效。")
                releases[name] = Release(match[1], platform, name, size)
        if root.findtext("IsTruncated") == "false":
            return sorted(releases.values(), key=lambda r: version_key(r.version), reverse=True)
        token = root.findtext("NextContinuationToken", "")
        if not token or token in seen:
            raise RuntimeError("官方清单分页状态无效。")
        seen.add(token)
    raise RuntimeError("官方清单分页超过限制。")


def _description(data):
    """Parse the deliberately restricted libconfig subset in official SWUs."""
    text = data.decode("utf-8")
    token_re = re.compile(r'\s+|/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|[A-Za-z_][\w-]*|[{}()\[\]:=;,]', re.S)
    tokens, pos = [], 0
    while pos < len(text):
        match = token_re.match(text, pos)
        if not match:
            raise RuntimeError("不支持的 sw-description 语法。")
        word = match[0]
        pos = match.end()
        if not word.isspace() and not word.startswith(("/*", "//")):
            tokens.append(word)
    index = 0

    def take():
        nonlocal index
        if index >= len(tokens):
            raise RuntimeError("sw-description 意外结束。")
        value = tokens[index]
        index += 1
        return value

    def value(depth=0):
        if depth > 16:
            raise RuntimeError("sw-description 嵌套过深。")
        word = take()
        if word == "{":
            return mapping("}", depth + 1)
        if word in ("(", "["):
            closing = ")" if word == "(" else "]"
            result = []
            while index < len(tokens) and tokens[index] != closing:
                result.append(value(depth + 1))
                if index < len(tokens) and tokens[index] == ",":
                    take()
                elif index >= len(tokens) or tokens[index] != closing:
                    raise RuntimeError("sw-description 列表无效。")
            if take() != closing:
                raise RuntimeError("sw-description 列表未结束。")
            return result
        if word.startswith('"'):
            return json.loads(word)
        if word in ("true", "false"):
            return word == "true"
        raise RuntimeError("sw-description 值无效。")

    def mapping(closing=None, depth=0):
        result = {}
        while index < len(tokens) and tokens[index] != closing:
            name = take()
            if not re.fullmatch(r"[A-Za-z_][\w-]*", name) or name in result or take() not in ("=", ":"):
                raise RuntimeError("sw-description 字段重复或无效。")
            result[name] = value(depth)
            if index == len(tokens) and closing is None:
                break
            if take() != ";":
                raise RuntimeError("sw-description 缺少分隔符。")
        if closing and take() != closing:
            raise RuntimeError("sw-description 对象未结束。")
        return result

    result = mapping()
    if index != len(tokens):
        raise RuntimeError("sw-description 包含多余内容。")
    return result


@dataclass(frozen=True)
class Image:
    path: Path
    version: str
    platform: str
    size: int
    sha256: str
    unpacked_size: int


def inspect_image(path, expected_platform=None, progress=None):
    """Stream CPIO without extracting; flat regular single-link files only."""
    path = Path(path)
    size = path.stat().st_size
    if not 0 < size <= MAX_SWU:
        raise RuntimeError("SWU 文件为空或超过 4 GiB 限制。")
    entries, metadata, total = {}, {}, 0
    whole = hashlib.sha256()
    with path.open("rb") as stream:
        def read(count):
            data = stream.read(count)
            if len(data) != count:
                raise RuntimeError("SWU 容器被截断。")
            whole.update(data)
            return data

        for _ in range(64):
            header = read(110)
            if header[:6] not in (b"070701", b"070702") or not re.fullmatch(b"[0-9a-fA-F]{104}", header[6:]):
                raise RuntimeError("仅支持 newc/CRC CPIO 固件容器。")
            fields = [int(header[n:n + 8], 16) for n in range(6, 110, 8)]
            mode, links, length, namesize, checksum = fields[1], fields[4], fields[6], fields[11], fields[12]
            if not 1 < namesize <= 256 or length > MAX_SWU:
                raise RuntimeError("SWU 条目大小无效。")
            raw_name = read(namesize)
            if raw_name[-1:] != b"\0":
                raise RuntimeError("SWU 文件名未终止。")
            name = raw_name[:-1].decode("ascii")
            read(-(110 + namesize) % 4)
            if name == "TRAILER!!!":
                if length:
                    raise RuntimeError("SWU 尾记录无效。")
                tail = stream.read(513)
                whole.update(tail)
                if len(tail) > 512 or any(tail):
                    raise RuntimeError("SWU 尾部包含未知数据。")
                break
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,239}", name)
                    or ".." in name or name in entries or not stat.S_ISREG(mode)
                    or links != 1 or mode & 0o7000 or fields[9] or fields[10]):
                raise RuntimeError("SWU 包含路径、链接、特殊文件、重复名称或危险权限。")
            if not entries and name != "sw-description":
                raise RuntimeError("SWU 第一项必须为 sw-description。")
            if len(entries) == 1 and name != "sw-description.sig":
                raise RuntimeError("SWU 第二项必须为原生签名。")
            total += length
            if total > MAX_SWU or (name in ("sw-description", "sw-description.sig", "postinstall.sh") and not 0 < length <= MAX_DESCRIPTION):
                raise RuntimeError("SWU 解包大小超过安全限制。")
            digest, crc, chunks = hashlib.sha256(), 0, []
            remaining = length
            while remaining:
                chunk = read(min(1024**2, remaining))
                digest.update(chunk)
                if header[:6] == b"070702":
                    crc = (crc + sum(chunk)) & 0xffffffff
                if name in ("sw-description", "sw-description.sig", "postinstall.sh"):
                    chunks.append(chunk)
                remaining -= len(chunk)
                if progress:
                    progress(stream.tell(), size)
            if header[:6] == b"070702" and crc != checksum:
                raise RuntimeError("SWU CPIO 校验和不匹配。")
            read(-length % 4)
            entries[name] = digest.hexdigest()
            if chunks:
                metadata[name] = b"".join(chunks)
        else:
            raise RuntimeError("SWU 条目过多或缺少尾记录。")
    try:
        software = _description(metadata["sw-description"])["software"]
        version = software["version"]
        version_key(version)
        platforms = set(software) - {"version"}
        if "CT-PCBA-IMX8MM" in platforms:
            if software.get("ferrari") != software["CT-PCBA-IMX8MM"]:
                raise ValueError("board alias disagreement")
            platforms.remove("CT-PCBA-IMX8MM")
        if len(platforms) != 1 or not platforms <= set(PLATFORMS):
            raise ValueError("platform")
        platform = platforms.pop()
        board = software[platform]
        if board["hardware-compatibility"] != ["1.0"] or set(board) != {"hardware-compatibility", "stable"}:
            raise ValueError("hardware")
        if set(board["stable"]) != {"copy1", "copy2"}:
            raise ValueError("selection")
        referenced = {"sw-description", "sw-description.sig"}
        for selection, target in (("copy1", "root_a"), ("copy2", "root_b")):
            contents = board["stable"][selection]
            if set(contents) != {"images", "files", "scripts"} or len(contents["images"]) != 1:
                raise ValueError("contents")
            image = contents["images"][0]
            if image["device"] != "/dev/disk/by-partlabel/" + target:
                raise ValueError("target")
            for group in contents.values():
                for item in group:
                    name = item["filename"]
                    if entries[name] != item["sha256"]:
                        raise ValueError("payload hash")
                    referenced.add(name)
        if set(entries) != referenced or "sw-description.sig" not in metadata:
            raise ValueError("unreferenced payload")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("SWU 内容、硬件或双分区目标无法精确验证。") from exc
    if expected_platform is not None and platform != expected_platform:
        raise RuntimeError("SWU 硬件平台与设备不匹配。")
    postinstall = metadata.get("postinstall.sh", b"")
    if not postinstall or any(b"/sys/devices/platform/lpgpr/root_part" in line and b"echo" in line
                              for line in postinstall.splitlines()):
        raise RuntimeError("SWU 使用旧 A/B 写入脚本或缺少安装脚本，拒绝安装。")
    return Image(path.resolve(), version, platform, size, whole.hexdigest(), total)


def download_release(release, cache, progress=None):
    match = IMAGE_RE.fullmatch(release.filename)
    if (not match or (match[1], match[2]) != (release.version, release.platform)
            or not 0 < release.size <= MAX_SWU):
        raise RuntimeError("官方固件记录无效。")
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / release.filename
    if target.is_file():
        image = inspect_image(target, release.platform, progress)
        if image.size == release.size and image.version == release.version:
            return image
        raise RuntimeError("缓存固件与官方记录不一致，请另选缓存目录。")
    if shutil.disk_usage(cache).free < release.size + 64 * 1024**2:
        raise RuntimeError("本地固件缓存空间不足。")
    fd, temporary = tempfile.mkstemp(prefix=".firmware-", suffix=".swu", dir=cache)
    try:
        with os.fdopen(fd, "wb") as output, _open(DOWNLOAD + release.filename) as response:
            received = 0
            while chunk := response.read(1024**2):
                received += len(chunk)
                if received > release.size:
                    raise RuntimeError("固件下载超过官方声明大小。")
                output.write(chunk)
                if progress:
                    progress(received, release.size)
            if received != release.size:
                raise RuntimeError("固件下载不完整。")
            output.flush()
            os.fsync(output.fileno())
        checked = inspect_image(temporary, release.platform, progress)
        if checked.version != release.version:
            raise RuntimeError("容器版本与官方文件名不一致。")
        os.replace(temporary, target)
        return Image(target, checked.version, checked.platform, checked.size, checked.sha256, checked.unpacked_size)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def key_values(text):
    result = {}
    for line in text.splitlines():
        if "=" not in line:
            raise RuntimeError("设备状态格式无效。")
        key, value = line.split("=", 1)
        if key in result:
            raise RuntimeError("设备状态字段重复。")
        result[key] = value.strip()
    return result


def shell(*args):
    return " ".join(shlex.quote(str(arg)) for arg in args)


# No sourced files: native conf.d has side effects (including counter resets).
GUARD_PROBE = r'''set -eu
if [ -d /sys/devices/platform/lpgpr ]; then
    swu_status=$(cat /sys/devices/platform/lpgpr/swu_status)
    case "$swu_status" in
        0) ;;
        1)
            printf '%s\n' '设备存在尚未完成的系统更新，已暂停插件和文件操作。请在设备设置中检查更新状态；若提示重启，请保存文档并按设备提示完成更新，再重新连接 rmtool。' >&2
            exit 1 ;;
        *)
            printf '%s\n' '设备系统更新状态异常，请导出诊断日志；不要强制安装插件或清除更新标记。' >&2
            exit 1 ;;
    esac
    test "$(cat /sys/devices/platform/lpgpr/swu_applied)" = 0
    test "$(cat /sys/devices/platform/lpgpr/swu_recovery)" = 0
    test "$(rootdev --active)" = "$(rootdev --next-boot)"
    inactive=$(rootdev --inactive)
    case "$inactive" in /dev/mmcblk0p2|/dev/mmcblk0p3) ;; *) exit 1;; esac
    if fuser "$inactive" >/dev/null 2>&1; then exit 1; else test "$?" = 1; fi
fi
'''

POWER_PROBE = r'''read_power_state() {
    battery=unknown
    power=unknown
    for supply in "$1"/*; do
        [ -d "$supply" ] || continue
        type=$(cat "$supply/type" 2>/dev/null) || continue
        if [ "$type" = Battery ]; then
            capacity=$(cat "$supply/capacity" 2>/dev/null) || continue
            case "$capacity" in ''|*[!0-9]*) continue;; esac
            [ "$capacity" -le 100 ] || continue
            if [ "$battery" = unknown ] || [ "$capacity" -lt "$battery" ]; then battery=$capacity; fi
        elif [ -f "$supply/online" ]; then
            online=$(cat "$supply/online" 2>/dev/null) || continue
            case "$online" in
                1) power=1;;
                0) [ "$power" = 1 ] || power=0;;
            esac
        fi
    done
}
'''

POWER_GUARD = POWER_PROBE + r'''require_safe_power() {
    read_power_state "$1"
    test "$battery" != unknown
    test "$power" = 1
    test "$battery" -ge 50
}
'''


PROBE = r'''set -eu
''' + POWER_PROBE + r'''read_power_state /sys/class/power_supply
printf 'machine='; cat /sys/devices/soc0/machine
printf 'arch='; uname -m
printf 'root='; swupdate -g
printf 'a='; realpath /dev/disk/by-partlabel/root_a
printf 'b='; realpath /dev/disk/by-partlabel/root_b
printf 'boot='; cat /sys/bus/mmc/devices/mmc0:0001/boot_part
for key in root_part roota_errcnt rootb_errcnt swu_status swu_applied swu_recovery boot_flow; do
    printf '%s=' "$key"; cat "/sys/devices/platform/lpgpr/$key"
done
printf 'schema='; stat -c '%a' /sys/devices/platform/lpgpr/root_part
printf 'battery=%s\n' "$battery"
printf 'power=%s\n' "$power"
printf 'free='; df -Pk /home/root | awk 'END {print $4}'
printf 'tmpfree='; df -Pk /tmp | awk 'END {print $4}'
printf 'engine='; systemctl show update-engine.service -p ActiveState --value
printf 'engine_file='; systemctl show update-engine.service -p UnitFileState --value
printf 'writer='
if pgrep -f '^/usr/sbin/swupdate-from-image-file ' >/dev/null; then echo busy; else r=$?; [ "$r" = 1 ]; echo idle; fi
printf 'holders='
inactive=$(rootdev --inactive)
case "$inactive" in /dev/mmcblk0p2|/dev/mmcblk0p3) ;; *) exit 1;; esac
if fuser "$inactive" >/dev/null 2>&1; then echo busy; else r=$?; [ "$r" = 1 ]; echo idle; fi
printf 'shared_lock='; if [ -e /tmp/rmtool-xovi-standalone.lock ]; then echo busy; else echo idle; fi
version=$(sed -n 's/^IMG_VERSION=//p' /usr/lib/os-release)
test -n "$version"
printf 'version=%s\n' "$version" | tr -d '"'
'''


@dataclass(frozen=True)
class DeviceState:
    values: dict
    platform: str
    active: str
    next_boot: str

    @property
    def pending(self):
        return self.values["swu_status"] == "1" or self.values["swu_applied"] != "0" or self.next_boot != self.active


def parse_state(text):
    values = key_values(text)
    required = {"machine", "arch", "root", "a", "b", "boot", "root_part", "roota_errcnt", "rootb_errcnt",
                "swu_status", "swu_applied", "swu_recovery", "boot_flow", "schema", "battery", "power",
                "free", "tmpfree", "engine", "engine_file", "writer", "holders", "shared_lock", "version"}
    if set(values) != required:
        raise RuntimeError("设备状态不完整，不能判断更新安全性。")
    from _tap_page_turn import _platform_from_machine
    platform = _platform_from_machine(values["machine"])
    platform = platform or {"remarkable paper pro": "ferrari", "remarkable paper pro move": "chiappa",
                            "ct-pcba-imx8mm": "ferrari"}.get(values["machine"].casefold(), "")
    platforms = [platform] if platform in ("ferrari", "chiappa") else []
    if len(platforms) != 1 or values["arch"] != "aarch64":
        raise RuntimeError("目前原生写入仅验证 Paper Pro / Move 的新 A/B 架构。")
    if (values["a"], values["b"]) != ("/dev/mmcblk0p2", "/dev/mmcblk0p3"):
        raise RuntimeError("根分区映射未知。")
    if values["root"] not in (values["a"], values["b"]) or values["boot"] not in ("1", "2"):
        raise RuntimeError("活动分区或下次启动分区未知。")
    active = "a" if values["root"] == values["a"] else "b"
    if values["root_part"] != active or values["schema"] != "444":
        raise RuntimeError("A/B 状态不一致或旧分区架构，拒绝写入。")
    if values["boot_flow"] != "regular":
        raise RuntimeError("设备不处于正常启动流程。")
    for key in ("roota_errcnt", "rootb_errcnt", "swu_status", "swu_applied", "swu_recovery", "free", "tmpfree"):
        if not re.fullmatch(r"\d{1,15}", values[key]):
            raise RuntimeError("设备数值状态无效：" + key)
    if ((values["battery"] != "unknown" and
         (not re.fullmatch(r"\d{1,3}", values["battery"]) or not 0 <= int(values["battery"]) <= 100))
            or values["power"] not in ("0", "1", "unknown")):
        raise RuntimeError("设备电源状态无效。")
    version_key(values["version"])
    return DeviceState(values, platforms[0], active, "a" if values["boot"] == "1" else "b")


def assert_idle(state, *, writing=False, image=None, check_plugin_lock=True):
    v = state.values
    if state.pending or any(v[k] != "0" for k in ("roota_errcnt", "rootb_errcnt", "swu_status", "swu_recovery")):
        raise RuntimeError("存在待应用更新、失败状态或 A/B 错误计数；不会自动清零。")
    if v["writer"] != "idle" or v["holders"] != "idle":
        raise RuntimeError("原生更新器或分区占用状态非空闲，拒绝操作。")
    if check_plugin_lock and v["shared_lock"] != "idle":
        raise RuntimeError("其他插件事务正在执行或遗留锁需要检查；请停止操作、保存文档后重启设备。")
    if writing:
        if v["engine_file"] not in ("masked", "masked-runtime") or v["engine"] not in ("inactive", "failed"):
            raise RuntimeError("自动更新服务尚未屏蔽，无法排除并发写入；请先独立确认自动更新策略。")
        assert_power(state)
    if image:
        if image.platform != state.platform:
            raise RuntimeError("SWU 硬件平台与设备不匹配。")
        if version_key(image.version) < (3, 22, 0, 0):
            raise RuntimeError("新 A/B 架构禁止回退到 3.22 之前的固件。")
        if int(v["free"]) * 1024 < image.size + 128 * 1024**2:
            raise RuntimeError("设备固件暂存空间不足。")
        if int(v["tmpfree"]) * 1024 < image.unpacked_size + 128 * 1024**2:
            raise RuntimeError("原生解包所需 /tmp 空间不足。")


def assert_power(state):
    if state.values["battery"] == "unknown" or state.values["power"] == "unknown":
        raise RuntimeError("无法确认设备电量或外接电源状态，拒绝固件写入。")
    if int(state.values["battery"]) < 50 or state.values["power"] != "1":
        raise RuntimeError("固件操作需要接通电源且电量至少 50%。")


@contextmanager
def firmware_session(ssh, token=None):
    local = getattr(ssh, "_firmware_local", None)
    previous = getattr(local, "allowed", False)
    if local is not None:
        local.allowed = True
    try:
        with ssh.operation_session():
            if token is not None and ssh.ensure_client() is not token:
                raise RuntimeError("连接已变化，请重新检测并确认。")
            yield
    finally:
        if local is not None:
            local.allowed = previous


def _reconciled_target_state(ssh, metadata, directory, boot_id):
    if not boot_id or boot_id == metadata["boot_id"]:
        return False
    state = parse_state(ssh.exec_checked(PROBE))
    target = key_values(_remote_text(ssh, directory + "/target"))
    if set(target) != {"version", "internal", "xochitl", "hardware"}:
        return False
    if (state.active != metadata["target"]
            or state.next_boot != state.active
            or state.platform != metadata["platform"]
            or state.values["version"] != metadata["version"]
            or target["version"] != metadata["version"]
            or target["hardware"].replace("CT-PCBA-IMX8MM", "ferrari")
                != "-H " + metadata["platform"] + ":1.0"
            or state.pending
            or state.values["writer"] != "idle"
            or state.values["holders"] != "idle"
            or state.values["shared_lock"] != "idle"
            or any(state.values[key] != "0" for key in (
                "swu_status", "swu_recovery", "roota_errcnt", "rootb_errcnt"))):
        return False
    digest = ssh.exec_checked("sha256sum /usr/bin/xochitl").split()[0]
    return digest == target["xochitl"]


def query_transaction(ssh):
    """Missing results are reconciled only after exact post-reboot proof."""
    with firmware_session(ssh):
        try:
            if ssh.exec_checked(f"if [ -e {BASE}/current ] || [ -L {BASE}/current ]; then echo yes; else echo no; fi").strip() == "no":
                return "none", "没有 rmtool 固件事务。"
            job = _remote_text(ssh, BASE + "/current", 100).strip()
            if not re.fullmatch(r"[0-9a-f]{32}", job):
                return "unknown", "固件事务标识无效。"
            directory = BASE + "/" + job
            ssh.exec_checked(f"set -eu; test ! -L {BASE}; test -d {BASE}; "
                             f"test \"$(stat -c '%u:%a' {BASE})\" = 0:700; "
                             f"test ! -L {BASE}/current; test ! -L {directory}; test -d {directory}; "
                             f"test \"$(stat -c '%u:%a' {directory})\" = 0:700")
            metadata = json.loads(_remote_text(ssh, directory + "/job.json"))
            if (metadata["job"] != job or metadata["target"] not in ("a", "b")
                    or metadata["platform"] not in ("chiappa", "ferrari")):
                raise RuntimeError("事务元数据无效。")
            props = key_values(ssh.exec_checked(shell("systemctl", "show", UNIT + "-" + job,
                "-p", "LoadState", "-p", "ActiveState", "-p", "SubState", "-p", "Result", "-p", "ExecMainStatus")))
            if props.get("ActiveState") in ("activating", "deactivating") or props.get("SubState") == "running":
                return "running", "设备端事务仍在运行；断线不会中止安装。"
            completed_path = directory + "/completed.json"
            ssh.exec_checked(f"test ! -L {completed_path}")
            try:
                completed = json.loads(_remote_text(ssh, completed_path))
            except FileNotFoundError:
                pass
            else:
                ssh.exec_checked(f"test ! -L {completed_path} && test -f {completed_path} && "
                                 f"test \"$(stat -c '%u:%a' {completed_path})\" = 0:600")
                completed_boot = completed.pop("completed_boot_id", "")
                if (completed != metadata or not isinstance(completed_boot, str)
                        or not completed_boot or completed_boot == metadata["boot_id"]):
                    raise RuntimeError("固件完成记录与事务不一致。")
                return "completed", "此前固件安装已完成，记录已保留。"
            boot_id = _remote_text(ssh, "/proc/sys/kernel/random/boot_id", 64).strip()
            try:
                result = _remote_text(ssh, directory + "/result", 64).strip()
            except FileNotFoundError:
                if _reconciled_target_state(ssh, metadata, directory, boot_id):
                    _record_completion(ssh, directory, metadata, boot_id)
                    return "completed", "已重启进入目标固件，已核销未完成的事务记录。"
                return "unknown", "事务结果缺失，且尚未确认设备已进入目标固件。"
            if result == "aborted":
                return _release_aborted(ssh, job, directory, metadata, boot_id)
            if result == "success" and boot_id != metadata["boot_id"]:
                if _reconciled_target_state(ssh, metadata, directory, boot_id):
                    _record_completion(ssh, directory, metadata, boot_id)
                    return "completed", "已重启进入目标固件，分区与系统指纹一致。"
                return "unknown", "重启后目标固件或分区不一致，需检查回退与更新状态。"
            if (result == "success" and props.get("Result") == "success"
                    and props.get("ExecMainStatus") == "0" and props.get("SubState") == "exited"):
                return "success", "设备端操作成功；尚未重启。"
            if result == "failed":
                return "failed", "设备端事务失败，请保留日志并检查分区。"
            return "unknown", "事务结果缺少一致证据，禁止重试或重启。"
        except Exception as exc:
            logging.warning("Firmware transaction query uncertain: %s", exc)
            return "unknown", "无法确认设备端事务结果，请重连后查询。"


def _record_completion(ssh, directory, metadata, boot_id):
    temporary = directory + "/completed-" + uuid.uuid4().hex + ".tmp"
    data = json.dumps(dict(metadata, completed_boot_id=boot_id)).encode("utf-8")
    try:
        _write_remote(ssh, temporary, data)
        ssh.exec_checked(f"set -eu; test \"$(cat {BASE}/current)\" = {metadata['job']}; "
                         f"chmod 600 {temporary}; mv -f {temporary} {directory}/completed.json; sync")
        if _remote_text(ssh, directory + "/completed.json") != data.decode("utf-8"):
            raise RuntimeError("无法保存固件完成记录。")
        # The installed image is no longer needed; keep the job logs only.
        try:
            ssh.exec_checked(f"rm -f -- {directory}/image.swu {directory}/image.tmp")
        except Exception:
            logging.warning("Unable to remove the installed firmware image in %s", directory, exc_info=True)
    finally:
        try:
            ssh.exec_checked(f"rm -f -- {temporary}")
        except Exception:
            logging.warning("Unable to clean firmware completion staging file %s", temporary, exc_info=True)


LOCK_PATH = "/tmp/rmtool-xovi-standalone.lock"
RELEASE_HINT = "可在“固件管理”页刷新状态；设备状态正常时可以解除锁定。"


def _archive_transaction(ssh, current, record, *, directory=None, boot_id="", remove_image=False):
    """Stop treating *current* as active; keep its job directory for diagnosis.

    The shared plugin lock is released only in the boot that created it: after
    a reboot /tmp no longer holds the lock this job took.
    """
    steps = ["set -eu", f"test \"$(cat {BASE}/current)\" = {shlex.quote(current)}"]
    temporary = None
    if directory is not None:
        temporary = directory + "/released-" + uuid.uuid4().hex + ".tmp"
        _write_remote(ssh, temporary, json.dumps(record).encode("utf-8"))
        steps.append(f"chmod 600 {temporary}; mv -f {temporary} {directory}/released.json")
        if remove_image:
            steps.append(f"rm -f {directory}/image.swu {directory}/image.tmp")
    steps.append(f"rm -f {BASE}/current {BASE}/current.tmp; sync")
    try:
        ssh.exec_checked("; ".join(steps))
    finally:
        if temporary is not None:
            try:
                ssh.exec_checked(f"rm -f -- {temporary}")
            except Exception:
                logging.warning("Unable to clean firmware release staging file %s", temporary, exc_info=True)
    if directory is not None and boot_id and record.get("boot_id") == boot_id:
        try:
            ssh.exec_checked(f"if [ -d {LOCK_PATH} ]; then rmdir {LOCK_PATH}; fi")
        except Exception:
            logging.warning("Firmware job released but the shared lock stayed", exc_info=True)


# Any rmtool firmware unit still running, or any native swupdate writer.
JOBS_RUNNING_PROBE = (
    f"systemctl list-units --all --plain --no-legend --no-pager '{UNIT}-*' | "
    "awk '$3 == \"activating\" || $3 == \"deactivating\" || $4 == \"start\" || $4 == \"running\" "
    "{ found = 1 } END { exit !found }' && echo busy || "
    "{ if pgrep -x swupdate >/dev/null; then echo busy; else echo idle; fi; }"
)


def _assert_no_job_running(ssh):
    if ssh.exec_checked(JOBS_RUNNING_PROBE).strip() != "idle":
        raise RuntimeError("设备端仍有固件任务或原生更新程序在运行，不能解除锁定。")


def _release_aborted(ssh, job, directory, metadata, boot_id):
    """A job that stopped before its first write leaves the device unchanged."""
    state = parse_state(ssh.exec_checked(PROBE))
    try:
        # This job may still hold the shared lock it took before starting.
        assert_idle(state, check_plugin_lock=False)
        _assert_no_job_running(ssh)
    except RuntimeError as exc:
        return "unknown", "上次固件操作在写入前中止，但设备当前状态异常：" + str(exc)
    _archive_transaction(ssh, job, dict(metadata, released="aborted", released_boot_id=boot_id),
                         directory=directory, boot_id=boot_id, remove_image=True)
    return "none", ("固件操作在写入分区前已停止（例如电源检查或固件校验未通过），"
                    "设备未改动，已自动解除锁定。")


def release_transaction(ssh, token, *, confirmed=False):
    """Explicitly release a failed or uncertain job once the device is verifiably idle.

    Never resets counters or boot flags. A pending update, a switched next-boot
    partition, A/B error counters or an active writer keep the lock in place.
    """
    if not confirmed:
        raise RuntimeError("解除固件锁定尚未明确确认。")
    with firmware_session(ssh, token):
        status, message = query_transaction(ssh)
        if status not in ("failed", "unknown"):
            raise RuntimeError("当前固件状态不需要解除锁定：" + message)
        if ssh.exec_checked(f"if [ -e {BASE}/current ] || [ -L {BASE}/current ]; then echo yes; else echo no; fi").strip() == "no":
            raise RuntimeError("固件事务记录已变化，请刷新后重试。")
        ssh.exec_checked(f"set -eu; test ! -L {BASE}; test -d {BASE}; test ! -L {BASE}/current; test -f {BASE}/current")
        current = _remote_text(ssh, BASE + "/current", 100).strip()
        directory = metadata = None
        if re.fullmatch(r"[0-9a-f]{32}", current):
            props = key_values(ssh.exec_checked(shell("systemctl", "show", UNIT + "-" + current,
                "-p", "LoadState", "-p", "ActiveState", "-p", "SubState")))
            if props.get("ActiveState") in ("activating", "deactivating") or props.get("SubState") in ("start", "running"):
                raise RuntimeError("设备端固件任务仍在运行，不能解除锁定。")
            candidate = BASE + "/" + current
            try:
                ssh.exec_checked(f"set -eu; test ! -L {candidate}; test -d {candidate}")
                loaded = json.loads(_remote_text(ssh, candidate + "/job.json"))
                if loaded.get("job") == current:
                    directory, metadata = candidate, loaded
            except (RuntimeError, OSError, ValueError):
                logging.warning("Releasing firmware job without readable metadata", exc_info=True)
        _assert_no_job_running(ssh)
        state = parse_state(ssh.exec_checked(PROBE))
        # Same gate as starting a new job, except for the lock this job holds.
        assert_idle(state, check_plugin_lock=False)
        boot_id = _remote_text(ssh, "/proc/sys/kernel/random/boot_id", 64).strip()
        record = dict(metadata or {}, released=status, released_reason=message, released_boot_id=boot_id)
        _archive_transaction(ssh, current, record, directory=directory, boot_id=boot_id, remove_image=True)
        state, _transaction = inspect_device(ssh)
        result = "已解除固件锁定，任务日志已保留。备用分区内容可能不完整，请不要切换到备用分区；需要时请重新安装固件。"
        if state.values["shared_lock"] != "idle":
            result += "\n插件事务锁仍在（无法确认归属），请保存文档后重启设备再继续操作。"
        if state.values["engine_file"] == "masked-runtime":
            result += "\n自动更新仍处于临时暂停状态，可用“恢复自动更新”或重启设备恢复。"
        return result


def inspect_device(ssh):
    with firmware_session(ssh):
        state = parse_state(ssh.exec_checked(PROBE))
        transaction = query_transaction(ssh)
        reason = ""
        try:
            if transaction[0] not in ("none", "completed"):
                raise RuntimeError(_guard_message(transaction))
            # A plugin lock protects mutations, not unrelated read-only tools.
            assert_idle(state, check_plugin_lock=False)
        except RuntimeError as exc:
            reason = str(exc)
        if hasattr(ssh, "firmware_guard_reason"):
            ssh.firmware_guard_reason = reason
        return state, transaction


def _guard_message(transaction):
    status, message = transaction
    return message + RELEASE_HINT if status in ("failed", "unknown") else message


def reboot_for_plugin_recovery(ssh):
    """Recheck updates before an explicitly confirmed restart; never delete locks."""
    with ssh.operation_session():
        with firmware_session(ssh):
            transaction = query_transaction(ssh)
            if transaction[0] not in ("none", "completed"):
                raise RuntimeError(transaction[1])
            ssh.exec_checked(GUARD_PROBE)
        ssh.exec_checked("systemctl reboot")


class FirmwareSSHClientWrapper(SSHClientWrapper):
    """Application-wide transport gate, including queued work and SFTP writes.

While uncertain, even other reads are refused instead of trying to classify
arbitrary shell commands. Firmware queries use a thread-local scoped bypass.
"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._firmware_local = threading.local()
        self.firmware_guard_reason = "固件事务状态尚未检查。"

    def _before_connected(self):
        self.firmware_guard_reason = "固件事务状态尚未检查。"
        try:
            with firmware_session(self):
                transaction = query_transaction(self)
                self.firmware_guard_reason = (
                    "" if transaction[0] in ("none", "completed") else _guard_message(transaction)
                )
                if not self.firmware_guard_reason:
                    self.exec_checked(GUARD_PROBE)
        except Exception as exc:
            self.firmware_guard_reason = "无法确认更新状态：" + str(exc)

    def _firmware_gate(self):
        if getattr(self._firmware_local, "allowed", False):
            return
        if self.firmware_guard_reason:
            raise RuntimeError(self.firmware_guard_reason)

    @contextmanager
    def operation_session(self):
        with super().operation_session():
            self._firmware_gate()
            depth = getattr(self._firmware_local, "depth", 0)
            self._firmware_local.depth = depth + 1
            try:
                yield
            finally:
                self._firmware_local.depth = depth

    def exec_command(self, command, *, timeout=1800, decode_errors="strict"):
        with self._transport_lock:
            self._firmware_gate()
            return super().exec_command(command, timeout=timeout, decode_errors=decode_errors)

    @contextmanager
    def sftp_session(self):
        with self._transport_lock:
            self._firmware_gate()
            with super().sftp_session() as sftp:
                yield sftp


def _remote_text(ssh, path, limit=MAX_DESCRIPTION):
    with ssh.open_remote(path, "r") as file:
        data = file.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("设备文件超过检查上限。")
    return data.decode("utf-8") if isinstance(data, bytes) else data


def slot_script(slot, *, require_clean=True):
    if slot not in ("a", "b"):
        raise RuntimeError("无效目标分区。")
    device = "/dev/mmcblk0p" + ("2" if slot == "a" else "3")
    clean_checks = '''
# Direct switching must not boot unknown root-local plugin hooks.
if [ -d "$m/etc/systemd/system/xochitl.service.d" ] &&
        [ -n "$(find "$m/etc/systemd/system/xochitl.service.d" -mindepth 1 -print)" ]; then
    echo '备用分区包含旧版 xochitl 插件，不能直接切换。' >&2
    exit 1
fi
if [ -e "$m/opt/xovi" ]; then
    echo '备用分区包含旧版 Xovi，不能直接切换。' >&2
    exit 1
fi
''' if require_clean else ""
    # Isolated namespace; ro,noload forbids ext4 journal replay. Cleanup errors
    # propagate instead of treating an unreadable or mounted slot as healthy.
    return f'''set -eu
test "$(rootdev --inactive)" = {device}
e2fsck -fn {device} >&2
mount --make-rprivate /
m=$(mktemp -d /tmp/rmtool-slot.XXXXXXXX)
cleanup() {{ umount "$m" && rmdir "$m"; }}
trap cleanup EXIT
mount -o ro,noload {device} "$m"
{clean_checks}
test -f "$m/usr/bin/xochitl"
test -x "$m/usr/bin/xochitl"
version=$(sed -n 's/^IMG_VERSION=//p' "$m/usr/lib/os-release")
test -n "$version"
printf 'version=%s\\n' "$version" | tr -d '"'
printf 'internal='; cat "$m/etc/version"
printf 'xochitl='; sha256sum "$m/usr/bin/xochitl" | cut -d ' ' -f 1
printf 'hardware='
grep -oE -- '-H (chiappa|ferrari|CT-PCBA-IMX8MM):1.0' "$m/usr/lib/swupdate/conf.d/09-swupdate-args"
'''


def isolated_slot_command(slot, *, require_clean=True):
    """Run the read-only slot probe in a systemd private mount namespace."""
    return shell(
        "systemd-run",
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        "--service-type=oneshot",
        "--property=PrivateMounts=yes",
        "/bin/sh",
        "-c",
        slot_script(slot, require_clean=require_clean),
    )


def _inspect_slot(ssh, state, *, require_clean):
    target = "b" if state.active == "a" else "a"
    with firmware_session(ssh):
        assert_idle(state)
        result = key_values(ssh.exec_checked(isolated_slot_command(
            target, require_clean=require_clean
        )))
    if (set(result) != {"version", "internal", "xochitl", "hardware"}
            or result["hardware"].replace("CT-PCBA-IMX8MM", "ferrari") != "-H " + state.platform + ":1.0"
            or not re.fullmatch(r"\d{14}", result["internal"])
            or not re.fullmatch(r"[0-9a-f]{64}", result["xochitl"])):
        raise RuntimeError("备用分区版本、平台或系统内容无法验证。")
    if version_key(result["version"]) < (3, 22, 0, 0):
        raise RuntimeError("备用分区使用旧 A/B 架构，禁止切换。")
    return result


def inspect_slot_metadata(ssh, state):
    """Read standby firmware identity without deciding whether it is safe to boot."""
    return _inspect_slot(ssh, state, require_clean=False)


def inspect_slot(ssh, state):
    """Read and validate a standby slot before direct A/B switching."""
    return _inspect_slot(ssh, state, require_clean=True)


@dataclass(frozen=True)
class Plan:
    state: DeviceState
    image: Image | None
    slot: dict | None
    token: object

    @property
    def downgrade(self):
        target = self.image.version if self.image else self.slot["version"]
        return version_key(target) < version_key(self.state.values["version"])


def preflight(ssh, image=None, *, switch=False, prepared=False):
    with firmware_session(ssh):
        token = ssh.ensure_client()
        state, transaction = inspect_device(ssh)
        if transaction[0] not in ("none", "completed"):
            raise RuntimeError(transaction[1])
        if image is None and not switch:
            raise RuntimeError("尚未选择固件。")
        if image:
            checked = inspect_image(image.path, state.platform)
            if checked != image:
                raise RuntimeError("固件自上次检查后已变化，请重新选择。")
        assert_idle(state, writing=prepared, image=image)
        assert_power(state)
        for name in ("rootdev", "swupdate", "systemd-run", "e2fsck", "fuser", "sha256sum", "mount", "umount", "mktemp"):
            _stdout, _stderr, code = ssh.exec_command(shell("command", "-v", name))
            if code:
                raise RuntimeError(f"设备缺少固件管理所需命令：{name}")
        help_text = ssh.exec_checked("systemd-run --help")
        for option in ("--wait", "--pipe", "--collect", "--property"):
            if option not in help_text:
                raise RuntimeError(f"设备的 systemd-run 不支持固件检查所需参数：{option}")
        if ssh.exec_checked("rootdev --active").strip() != state.values["root"]:
            raise RuntimeError("原生工具报告的当前分区不一致。")
        if ssh.exec_checked("rootdev --next-boot").strip() != state.values["root"]:
            raise RuntimeError("已有下次启动分区切换，拒绝再次切换。")
        # Match the stock updater: third-party applications and plugins do not
        # participate in official firmware validation or installation.
        if image and ssh.exec_checked("systemctl is-enabled rm-apply-ota.service").strip() != "enabled":
            raise RuntimeError("原生重启应用更新服务未启用，无法保证安装后的启动切换。")
        slot = inspect_slot(ssh, state) if switch else None
        return Plan(state, image, slot, token)


def _write_remote(ssh, path, data):
    with ssh.open_remote(path, "wx") as file:
        file.write(data)
        file.flush()


def same_device_state(left, right):
    volatile = {"battery", "power", "free", "tmpfree", "engine", "engine_file"}
    return ({k: v for k, v in left.values.items() if k not in volatile}
            == {k: v for k, v in right.values.items() if k not in volatile})


def _lock_and_revalidate(ssh, plan):
    ssh.exec_checked(f"mkdir {LOCK_PATH}")
    try:
        state = parse_state(ssh.exec_checked(PROBE))
        # The lock was acquired above by this session, not inherited from an
        # unknown owner. Normalize only this local snapshot for preflight.
        state.values["shared_lock"] = "idle"
        assert_idle(state, writing=True, image=plan.image)
        if not same_device_state(state, plan.state):
            raise RuntimeError("暂存期间设备状态已变化，请重新确认。")
        if plan.slot is not None and inspect_slot(ssh, state) != plan.slot:
            raise RuntimeError("备用分区内容已变化。")
    except Exception:
        ssh.exec_checked(f"rmdir {LOCK_PATH}")
        raise


def _pause_updater_locked(ssh, state):
    """Pause an idle updater and return whether rmtool changed its policy."""
    receipt = BASE + "/pause.json"
    if state.values["engine_file"] in ("masked", "masked-runtime"):
        return False
    if state.values["engine"] not in ("active", "inactive"):
        raise RuntimeError("自动更新服务不处于稳定状态。")
    ssh.exec_checked(f"set -eu; test ! -L {BASE}; if [ -e {BASE} ]; then "
                     f"test -d {BASE}; test \"$(stat -c '%u:%a' {BASE})\" = 0:700; "
                     f"else mkdir -m 700 {BASE}; fi")
    saved = {"engine": state.values["engine"], "engine_file": state.values["engine_file"],
             "boot_id": _remote_text(ssh, "/proc/sys/kernel/random/boot_id", 64).strip()}
    # A leftover temporary file can only come from an interrupted earlier pause.
    ssh.exec_checked(f"rm -f {receipt}.tmp")
    _write_remote(ssh, receipt + ".tmp", json.dumps(saved).encode())
    ssh.exec_checked(f"sync {receipt}.tmp && mv {receipt}.tmp {receipt} && sync")
    # Masking prevents service activation; both probes must still report no
    # writer immediately before the idle daemon is stopped.
    try:
        ssh.exec_checked(GUARD_PROBE + "\nsystemctl mask --runtime update-engine.service\n" +
                         GUARD_PROBE + "\nsystemctl stop update-engine.service\n"
                         "test \"$(systemctl show update-engine.service -p ActiveState --value)\" = inactive")
    except Exception as exc:
        try:
            current = parse_state(ssh.exec_checked(PROBE))
            if current.values["engine_file"] == "masked-runtime":
                _restore_updater_locked(ssh)
        except Exception as restore_exc:
            raise RuntimeError(f"暂停自动更新失败：{exc}；恢复原状态失败：{restore_exc}") from exc
        raise
    return True


def _restore_updater_locked(ssh):
    receipt = BASE + "/pause.json"
    state = parse_state(ssh.exec_checked(PROBE))
    saved = json.loads(_remote_text(ssh, receipt))
    if saved.get("boot_id") != _remote_text(ssh, "/proc/sys/kernel/random/boot_id", 64).strip():
        return "临时屏蔽已随重启失效；未改动当前自动更新策略。"
    if saved.get("engine") not in ("active", "inactive"):
        raise RuntimeError("原自动更新服务状态记录无效。")
    if state.values["engine_file"] != "masked-runtime":
        raise RuntimeError("自动更新策略已被其他操作改变，不会覆盖。")
    ssh.exec_checked(GUARD_PROBE + "\nsystemctl unmask --runtime update-engine.service")
    if saved["engine"] == "active":
        ssh.exec_checked("systemctl start update-engine.service")
    return "已恢复原自动更新服务状态。"


def prepare_updater(ssh, token, *, confirmed=False, restore=False):
    if not confirmed:
        raise RuntimeError("自动更新服务变更尚未确认。")
    with firmware_session(ssh, token):
        state, transaction = inspect_device(ssh)
        if transaction[0] not in ("none", "completed"):
            raise RuntimeError(transaction[1])
        assert_idle(state)
        if restore:
            return _restore_updater_locked(ssh)
        changed = _pause_updater_locked(ssh, state)
        return ("自动更新已临时暂停。未安装时可以恢复；重启后临时屏蔽失效。"
                if changed else "自动更新服务已屏蔽；不会覆盖原策略。")


@contextmanager
def _prepared_operation(ssh, plan, *, switch=False):
    """Pause the updater around final validation and restore on early failure."""
    initial = preflight(ssh, plan.image, switch=switch, prepared=False)
    if (not same_device_state(initial.state, plan.state)
            or (switch and initial.slot != plan.slot)):
        raise RuntimeError("设备状态已变化，请重新检测并确认。")
    paused_here = _pause_updater_locked(ssh, initial.state)
    committed = {"value": False}
    try:
        current = preflight(ssh, plan.image, switch=switch, prepared=True)
        if (not same_device_state(current.state, plan.state)
                or (switch and current.slot != plan.slot)):
            raise RuntimeError("设备状态已变化，请重新检测并确认。")
        yield current, committed
    except Exception as exc:
        if paused_here and not committed["value"]:
            try:
                _restore_updater_locked(ssh)
            except Exception as restore_exc:
                raise RuntimeError(f"{exc}；自动更新恢复失败：{restore_exc}") from exc
        raise
    else:
        if paused_here and not committed["value"]:
            _restore_updater_locked(ssh)


def native_check_command(image_path, platform, active):
    if platform not in ("chiappa", "ferrari") or active not in ("a", "b"):
        raise RuntimeError("原生验证目标无效。")
    if not re.fullmatch(re.escape(BASE) + r"/[0-9a-f]{32}/image\.swu", image_path):
        raise RuntimeError("固件暂存路径无效。")
    return shell("swupdate", "-c", "-f", image_path.rsplit("/", 1)[0] + "/native.cfg", "-i", image_path, "-k", KEY,
                 "-H", platform + ":1.0", "-e", "stable,copy2" if active == "a" else "stable,copy1")


def installation_script(job, plan):
    if not re.fullmatch(r"[0-9a-f]{32}", job) or plan.image is None:
        raise RuntimeError("固件安装计划无效。")
    directory = BASE + "/" + job
    path = directory + "/image.swu"
    active = plan.state.values["root"]
    # No trap stops the native writer. The result is written only after its
    # exit; power loss or missing final record remains explicitly uncertain.
    return f'''#!/bin/bash
set -eu
umask 077
result={directory}/result
WRITE_STARTED=0
finish() {{
    code=$?
    trap - EXIT
    if [ "$code" = 0 ]; then echo success > "$result.tmp"
    elif [ "$WRITE_STARTED" = 0 ]; then echo aborted > "$result.tmp"
    else echo failed > "$result.tmp"; fi
    sync "$result.tmp"
    mv "$result.tmp" "$result"
    sync
    exit "$code"
}}
trap finish EXIT
test "$(rootdev --active)" = {active}
test "$(rootdev --next-boot)" = {active}
test "$(cat {LPGPR}/roota_errcnt)" = 0
test "$(cat {LPGPR}/rootb_errcnt)" = 0
test "$(cat {LPGPR}/swu_status)" = 0
test "$(cat {LPGPR}/swu_applied)" = 0
test "$(cat {LPGPR}/swu_recovery)" = 0
test "$(systemctl show update-engine.service -p ActiveState --value)" = inactive
case "$(systemctl show update-engine.service -p UnitFileState --value)" in masked|masked-runtime) ;; *) exit 1;; esac
if fuser {plan.state.values['b' if plan.state.active == 'a' else 'a']} >/dev/null 2>&1; then exit 1; else test "$?" = 1; fi
echo '{plan.image.sha256}  {path}' | sha256sum -c -
{native_check_command(path, plan.state.platform, plan.state.active)}
{POWER_GUARD}require_safe_power /sys/class/power_supply
# Everything above only reads; a failure there is reported as "aborted".
WRITE_STARTED=1
# Use the same native engine without sourcing conf.d (which resets counters).
# Native extraction stages archive filenames before preinstall scripts.
{native_check_command(path, plan.state.platform, plan.state.active).replace(' -c ', ' ')}
test "$(cat {LPGPR}/swu_status)" = 1
{isolated_slot_command('b' if plan.state.active == 'a' else 'a')} > {directory}/target.tmp
grep -Fx 'version={plan.image.version}' {directory}/target.tmp
mv {directory}/target.tmp {directory}/target
sync
'''


def _points_to(ssh, job):
    command = f"if [ \"$(cat {BASE}/current 2>/dev/null)\" = {job} ]; then echo yes; else echo no; fi"
    return ssh.exec_checked(command).strip() == "yes"


def _commit_job(ssh, plan, job, committed, guard_reason):
    """Take the shared lock and atomically point `current` at *job*."""
    _lock_and_revalidate(ssh, plan)
    ssh.firmware_guard_reason = guard_reason
    try:
        # preflight allowed a new job, so a temporary pointer is an uncommitted leftover.
        ssh.exec_checked(f"rm -f {BASE}/current.tmp")
        _write_remote(ssh, BASE + "/current.tmp", (job + "\n").encode())
        # Another client may have started a job since our preflight. Only a
        # completed or released earlier job may be replaced.
        if query_transaction(ssh)[0] not in ("none", "completed"):
            raise RuntimeError("设备上已有其他固件任务，请刷新后重试。")
        ssh.exec_checked(f"sync {BASE}/current.tmp && mv {BASE}/current.tmp {BASE}/current && sync")
    except Exception:
        # Unlock only when the device proves that the pointer was not committed.
        try:
            if not _points_to(ssh, job):
                ssh.exec_checked(f"rm -f {BASE}/current.tmp; rmdir {LOCK_PATH}")
                ssh.firmware_guard_reason = ""
        except Exception:
            logging.warning("Could not verify uncommitted firmware job %s", job, exc_info=True)
        raise
    committed["value"] = True


def _start_job(ssh, job, committed, command, uncertain, started):
    """Start the detached unit; withdraw or report a job that never wrote anything."""
    try:
        ssh.exec_checked(command)
        return started
    except Exception as exc:
        start_error = exc
    # systemd-run waits for oneshot units, so an error can also mean the job
    # ran and stopped. Only an absent unit and result prove nothing started.
    try:
        props = key_values(ssh.exec_checked(shell("systemctl", "show", UNIT + "-" + job, "-p", "LoadState")))
        has_result = ssh.exec_checked(
            f"if [ -e {BASE}/{job}/result ]; then echo yes; else echo no; fi").strip() == "yes"
        never_started = props.get("LoadState") == "not-found" and not has_result
        if never_started:
            # Disarm the script first: if systemd still processes the queued
            # request later, bash cannot open it and the job does nothing.
            ssh.exec_checked(f"set -eu; for script in install.sh switch.sh; do "
                             f"if [ -e {BASE}/{job}/$script ]; then mv {BASE}/{job}/$script {BASE}/{job}/$script.withdrawn; fi; "
                             f"done; sync")
            props = key_values(ssh.exec_checked(shell("systemctl", "show", UNIT + "-" + job, "-p", "LoadState")))
            has_result = ssh.exec_checked(
                f"if [ -e {BASE}/{job}/result ]; then echo yes; else echo no; fi").strip() == "yes"
            never_started = props.get("LoadState") == "not-found" and not has_result
        if never_started:
            ssh.exec_checked(f"set -eu; test \"$(cat {BASE}/current)\" = {job}; "
                             f"rm -f {BASE}/current {BASE}/{job}/image.swu; sync; rmdir {LOCK_PATH}")
    except Exception:
        logging.warning("Firmware job start uncertain", exc_info=True)
        return uncertain
    if never_started:
        ssh.firmware_guard_reason = ""
        committed["value"] = False
        raise RuntimeError("设备未能启动固件任务，本次操作已撤回，设备未改动：" + str(start_error)) from start_error
    status = query_transaction(ssh)
    if status[0] == "none":
        try:
            released = json.loads(_remote_text(ssh, f"{BASE}/{job}/released.json")).get("released")
        except Exception:
            released = None
        if released != "aborted":
            return uncertain
        # The job stopped before writing and was released; the updater is restored too.
        ssh.firmware_guard_reason = ""
        committed["value"] = False
        raise RuntimeError(status[1] + f"详细原因见设备日志 {BASE}/{job}/。")
    return status if status[0] in ("failed", "success", "running") else uncertain


def start_install(ssh, plan, *, confirmed=False, downgrade_confirmed=False, progress=None):
    if not confirmed or (plan.downgrade and not downgrade_confirmed):
        raise RuntimeError("安装或降级尚未明确确认。")
    with firmware_session(ssh, plan.token):
        with _prepared_operation(ssh, plan) as (_current, committed):
            job = uuid.uuid4().hex
            directory = BASE + "/" + job
            # Never follow pre-existing transaction roots/locks or overwrite a job.
            ssh.exec_checked(f"set -eu; test ! -L {BASE}; if [ -e {BASE} ]; then test -d {BASE}; "
                             f"test \"$(stat -c '%u:%a' {BASE})\" = 0:700; else mkdir -m 700 {BASE}; fi; mkdir -m 700 {directory}")
            with ssh.sftp_session() as sftp:
                sftp.put(str(plan.image.path), directory + "/image.tmp", callback=progress)
            ssh.exec_checked(f"echo '{plan.image.sha256}  {directory}/image.tmp' | sha256sum -c - && "
                             f"chmod 600 {directory}/image.tmp && mv {directory}/image.tmp {directory}/image.swu")
            # Native verification runs inside the detached job's private /tmp,
            # before installation. Do not extract metadata into the shared /tmp.
            _write_remote(ssh, directory + "/native.cfg", b"globals : { };\n")
            script = installation_script(job, plan)
            _write_remote(ssh, directory + "/install.sh", script.encode())
            metadata = {"job": job, "kind": "install", "version": plan.image.version, "sha256": plan.image.sha256,
                        "platform": plan.image.platform, "target": "b" if plan.state.active == "a" else "a",
                        "boot_id": _remote_text(ssh, "/proc/sys/kernel/random/boot_id", 64).strip()}
            _write_remote(ssh, directory + "/job.json", json.dumps(metadata).encode())
            # Same remote lock as plugin installers. Retained through the detached
            # job and reboot: uncertain jobs must not unlock competing writers.
            _commit_job(ssh, plan, job, committed, "固件安装已启动或结果待确认；其他设备操作已锁定。")
            return _start_job(ssh, job, committed, shell("systemd-run", "--unit=" + UNIT + "-" + job, "--service-type=oneshot",
                    "--property=RemainAfterExit=yes", "--property=TimeoutStartSec=infinity",
                    "--property=StandardOutput=append:" + directory + "/install.log",
                    "--property=StandardError=append:" + directory + "/install.log",
                    "--property=TemporaryFileSystem=/tmp:rw,size=" + str(plan.image.unpacked_size + 128 * 1024**2),
                    "/bin/bash", directory + "/install.sh"),
                ("unknown", "启动结果待确认；请查询设备端安装状态，不要重复安装。"),
                ("running", "设备端安装已提交；完成后仍需单独确认重启。"))


def switch_slot(ssh, plan, *, confirmed=False, downgrade_confirmed=False):
    if not confirmed or plan.slot is None or (plan.downgrade and not downgrade_confirmed):
        raise RuntimeError("切换或降级尚未明确确认。")
    with firmware_session(ssh, plan.token):
        with _prepared_operation(ssh, plan, switch=True) as (_current, committed):
            job = uuid.uuid4().hex
            directory = BASE + "/" + job
            target_slot = "b" if plan.state.active == "a" else "a"
            target = plan.state.values[target_slot]
            ssh.exec_checked(f"set -eu; test ! -L {BASE}; if [ -e {BASE} ]; then "
                             f"test -d {BASE}; test \"$(stat -c '%u:%a' {BASE})\" = 0:700; "
                             f"else mkdir -m 700 {BASE}; fi; mkdir -m 700 {directory}")
            metadata = {"job": job, "kind": "switch", "version": plan.slot["version"], "platform": plan.state.platform,
                        "target": target_slot, "boot_id": _remote_text(ssh, "/proc/sys/kernel/random/boot_id", 64).strip()}
            _write_remote(ssh, directory + "/job.json", json.dumps(metadata).encode())
            _write_remote(ssh, directory + "/target", "".join(f"{k}={v}\n" for k, v in plan.slot.items()).encode())
            script = f'''#!/bin/bash
set -eu
WRITE_STARTED=0
finish() {{
 code=$?
 trap - EXIT
 if [ "$code" = 0 ]; then echo success
 elif [ "$WRITE_STARTED" = 0 ]; then echo aborted
 else echo failed; fi > {directory}/result.tmp
 sync {directory}/result.tmp
 mv {directory}/result.tmp {directory}/result
 sync
 exit "$code"
}}
trap finish EXIT
{GUARD_PROBE}
test "$(rootdev --active)" = {plan.state.values['root']}
test "$(rootdev --next-boot)" = {plan.state.values['root']}
test "$(cat {LPGPR}/roota_errcnt)" = 0
test "$(cat {LPGPR}/rootb_errcnt)" = 0
test "$(systemctl show update-engine.service -p ActiveState --value)" = inactive
case "$(systemctl show update-engine.service -p UnitFileState --value)" in masked|masked-runtime) ;; *) exit 1;; esac
{POWER_GUARD}require_safe_power /sys/class/power_supply
WRITE_STARTED=1
rootdev --switch
test "$(rootdev --next-boot)" = {target}
'''
            _write_remote(ssh, directory + "/switch.sh", script.encode())
            _commit_job(ssh, plan, job, committed, "分区切换结果待确认，其他设备操作已锁定。")
            return _start_job(ssh, job, committed, shell("systemd-run", "--unit=" + UNIT + "-" + job, "--service-type=oneshot",
                                      "--property=RemainAfterExit=yes", "/bin/bash", directory + "/switch.sh"),
                ("unknown", "切换提交结果待确认，请重连查询。"),
                ("running", "切换已提交；完成后仍需单独确认重启。"))


def reboot_after_success(ssh, token, *, confirmed=False):
    if not confirmed:
        raise RuntimeError("重启尚未明确确认。")
    with firmware_session(ssh, token):
        status, message = query_transaction(ssh)
        if status != "success":
            raise RuntimeError(message)
        state = parse_state(ssh.exec_checked(PROBE))
        if (state.values["holders"] != "idle" or state.values["writer"] != "idle"
                or state.values["swu_status"] not in ("0", "1")
                or any(state.values[k] != "0" for k in ("roota_errcnt", "rootb_errcnt", "swu_recovery"))):
            raise RuntimeError("设备更新状态不允许重启。")
        job = _remote_text(ssh, BASE + "/current", 100).strip()
        if not re.fullmatch(r"[0-9a-f]{32}", job):
            raise RuntimeError("安装状态标识无效。")
        directory = BASE + "/" + job
        metadata = json.loads(_remote_text(ssh, directory + "/job.json"))
        target = metadata["target"]
        if state.next_boot != target:
            if (metadata.get("kind") != "install" or state.values["swu_status"] != "1"
                    or ssh.exec_checked("systemctl is-enabled rm-apply-ota.service").strip() != "enabled"):
                raise RuntimeError("下次启动分区与已确认目标不一致，拒绝重启。")
        contents = key_values(ssh.exec_checked(isolated_slot_command(target)))
        if contents != key_values(_remote_text(ssh, directory + "/target")):
            raise RuntimeError("目标分区健康状态或内容已变化，拒绝重启。")
        ssh.exec_checked("systemctl reboot")
        ssh.close()
