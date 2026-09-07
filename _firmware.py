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
    return urllib.request.build_opener(_OfficialRedirect()).open(url, timeout=60)


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
    test "$(cat /sys/devices/platform/lpgpr/swu_status)" = 0
    test "$(cat /sys/devices/platform/lpgpr/swu_applied)" = 0
    test "$(cat /sys/devices/platform/lpgpr/swu_recovery)" = 0
    test "$(rootdev --active)" = "$(rootdev --next-boot)"
    inactive=$(rootdev --inactive)
    case "$inactive" in /dev/mmcblk0p2|/dev/mmcblk0p3) ;; *) exit 1;; esac
    if fuser "$inactive" >/dev/null 2>&1; then exit 1; else test "$?" = 1; fi
fi
'''

PROBE = r'''set -eu
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
printf 'battery='; cat /sys/class/power_supply/max77818_battery/capacity
printf 'power='; cat /sys/class/power_supply/max77818-charger/online
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
    for key in ("roota_errcnt", "rootb_errcnt", "swu_status", "swu_applied", "swu_recovery", "battery", "power", "free", "tmpfree"):
        if not re.fullmatch(r"\d{1,15}", values[key]):
            raise RuntimeError("设备数值状态无效：" + key)
    if not 0 <= int(values["battery"]) <= 100 or values["power"] not in ("0", "1"):
        raise RuntimeError("设备电源状态无效。")
    version_key(values["version"])
    return DeviceState(values, platforms[0], active, "a" if values["boot"] == "1" else "b")


def assert_idle(state, *, writing=False, image=None):
    v = state.values
    if state.pending or any(v[k] != "0" for k in ("roota_errcnt", "rootb_errcnt", "swu_status", "swu_recovery")):
        raise RuntimeError("存在待应用更新、失败状态或 A/B 错误计数；不会自动清零。")
    if v["writer"] != "idle" or v["holders"] != "idle":
        raise RuntimeError("原生更新器或分区占用状态非空闲，拒绝操作。")
    if v["shared_lock"] != "idle":
        raise RuntimeError("其他插件事务正在执行或遗留锁需要检查。")
    if writing:
        if v["engine_file"] not in ("masked", "masked-runtime") or v["engine"] not in ("inactive", "failed"):
            raise RuntimeError("自动更新服务尚未屏蔽，无法排除并发写入；请先独立确认自动更新策略。")
        if int(v["battery"]) < 50 or v["power"] != "1":
            raise RuntimeError("固件操作需要接通电源且电量至少 50%。")
    if image:
        if image.platform != state.platform:
            raise RuntimeError("SWU 硬件平台与设备不匹配。")
        if version_key(image.version) < (3, 22, 0, 0):
            raise RuntimeError("新 A/B 架构禁止回退到 3.22 之前的固件。")
        if int(v["free"]) * 1024 < image.size + 128 * 1024**2:
            raise RuntimeError("设备固件暂存空间不足。")
        if int(v["tmpfree"]) * 1024 < image.unpacked_size + 128 * 1024**2:
            raise RuntimeError("原生解包所需 /tmp 空间不足。")


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


def query_transaction(ssh):
    """Missing/collected units and transport loss never imply success."""
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
            result = _remote_text(ssh, directory + "/result", 64).strip()
            boot_id = _remote_text(ssh, "/proc/sys/kernel/random/boot_id", 64).strip()
            if result == "success" and boot_id != metadata["boot_id"]:
                state = parse_state(ssh.exec_checked(PROBE))
                target = key_values(_remote_text(ssh, directory + "/target"))
                digest = ssh.exec_checked("sha256sum /usr/bin/xochitl").split()[0]
                if (state.active == metadata["target"] and state.platform == metadata["platform"]
                        and state.values["version"] == metadata["version"] == target["version"]
                        and digest == target["xochitl"] and not state.pending
                        and all(state.values[k] == "0" for k in ("swu_status", "swu_recovery", "roota_errcnt", "rootb_errcnt"))):
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


def inspect_device(ssh):
    with firmware_session(ssh):
        state = parse_state(ssh.exec_checked(PROBE))
        transaction = query_transaction(ssh)
        reason = ""
        try:
            if transaction[0] not in ("none", "completed"):
                raise RuntimeError(transaction[1])
            if (state.pending or state.values["swu_status"] != "0"
                    or state.values["holders"] != "idle"):
                raise RuntimeError("设备存在更新、待重启状态或分区占用，其他操作暂时锁定。")
        except RuntimeError as exc:
            reason = str(exc)
        if hasattr(ssh, "firmware_guard_reason"):
            ssh.firmware_guard_reason = reason
        return state, transaction


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
                self.firmware_guard_reason = "" if transaction[0] in ("none", "completed") else transaction[1]
                if not self.firmware_guard_reason:
                    self.exec_checked(GUARD_PROBE)
        except Exception as exc:
            self.firmware_guard_reason = "无法确认更新状态：" + str(exc)

    def _firmware_gate(self):
        if getattr(self._firmware_local, "allowed", False):
            return
        if not self.firmware_guard_reason and not getattr(self._firmware_local, "depth", 0):
            self._before_connected()
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

    def exec_command(self, command, *, timeout=1800):
        with self._transport_lock:
            self._firmware_gate()
            return super().exec_command(command, timeout=timeout)

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


def inspect_plugins(ssh):
    """Only authenticated current shared launchers can be held disabled.

Nothing is migrated/deleted, and support for the new OS is never inferred
from the old OS. Overlaid/foreign hooks and external sidecars fail closed.
"""
    import _plugin_recovery as recovery
    import _tap_page_turn as tap
    import _xovi_standalone as shared
    recovery._unhidden_paths(ssh)
    recovery._external_loaders(ssh)
    forbidden = (shared.LEGACY_SHARED_LAYOUT.remote_base, tap.VELLUM_ROOT,
                 tap.SHARED_XOVI_LIBRARY, tap.SHARED_QRR_LIBRARY, tap.SHARED_APPLOAD_LIBRARY,
                 tap.REMOTE_BASE, tap.DROPIN_PATH,
                 recovery.migration.fast.REMOTE_BASE, recovery.migration.fast.DROPIN_PATH)
    if any(shared._remote_entry_exists(ssh, path) for path in forbidden):
        raise RuntimeError("检测到旧插件或其他启动程序，请先检查其兼容性；未修改任何配置。")
    if not shared._remote_entry_exists(ssh, shared.SHARED_LAYOUT.remote_base):
        if shared._remote_entry_exists(ssh, shared.SHARED_LAYOUT.dropin_path):
            raise RuntimeError("插件启动配置不完整。")
        return ()
    identity = tap.get_device_identity(ssh)
    runtime, trusted, _ = tap._trusted_shared_context(identity)
    inspected = shared.inspect_shared(ssh, runtime, trusted)
    if inspected.startup_pending or inspected.launcher_update_available or inspected.legacy_templates:
        raise RuntimeError("插件启动状态需要先修复，固件操作已阻止。")
    for state in inspected.states.values():
        for sidecar in state.spec.sidecars:
            if not sidecar.unit_name:
                raise RuntimeError("旧版内联拼音后台无法验证启动归属，请先升级插件；配置未修改。")
            # inspect_shared authenticates the launcher and unit payload. Only
            # runtime-linked services started behind that launcher's firmware
            # and emergency gates are safe across a reboot.
            props = key_values(ssh.exec_checked(shell("systemctl", "show", sidecar.unit_name,
                "-p", "FragmentPath", "-p", "DropInPaths", "-p", "UnitFileState")))
            expected = shared.SHARED_LAYOUT.remote_base + "/" + sidecar.unit_runtime_path
            path = shlex.quote(sidecar.remote_path)
            ssh.exec_checked(f"set -eu; test -f {path}; test ! -L {path}; "
                             f"test \"$(stat -c '%a:%u:%g:%s' {path})\" = '755:0:0:{sidecar.size}'; "
                             f"test \"$(sha256sum {path} | cut -d ' ' -f 1)\" = {sidecar.sha256}")
            if (props.get("FragmentPath") != expected or props.get("DropInPaths")
                    or props.get("UnitFileState") not in ("linked-runtime", "static")):
                raise RuntimeError("拼音后台存在持久启动或未知配置，无法安全准备固件操作。")
            for directory in ("/etc/systemd/system", "/usr/lib/systemd/system"):
                if ssh.exec_checked(shell("find", "-P", directory, "-name", sidecar.unit_name, "-print")).strip():
                    raise RuntimeError("拼音后台存在独立持久启动项，请先检查其归属。")
    for path in (shared.SHARED_LAYOUT.launcher_path, shared.SHARED_MARKER_PATH):
        recovery._ancestors(ssh, path)
    return tuple(sorted(inspected.states))


def slot_script(slot):
    if slot not in ("a", "b"):
        raise RuntimeError("无效目标分区。")
    device = "/dev/mmcblk0p" + ("2" if slot == "a" else "3")
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
# Unknown old root-local boot hooks are not covered by the shared sentinel.
if [ -d "$m/etc/systemd/system/xochitl.service.d" ]; then
    test -z "$(find "$m/etc/systemd/system/xochitl.service.d" -mindepth 1 -print)"
fi
test ! -e "$m/opt/xovi"
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


def inspect_slot(ssh, state):
    target = "b" if state.active == "a" else "a"
    with firmware_session(ssh):
        assert_idle(state, writing=True)
        result = key_values(ssh.exec_checked(shell("unshare", "--mount", "--", "/bin/sh", "-c", slot_script(target))))
    if (set(result) != {"version", "internal", "xochitl", "hardware"}
            or result["hardware"].replace("CT-PCBA-IMX8MM", "ferrari") != "-H " + state.platform + ":1.0"
            or not re.fullmatch(r"\d{14}", result["internal"])
            or not re.fullmatch(r"[0-9a-f]{64}", result["xochitl"])):
        raise RuntimeError("备用分区版本、平台或系统内容无法验证。")
    if version_key(result["version"]) < (3, 22, 0, 0):
        raise RuntimeError("备用分区使用旧 A/B 架构，禁止切换。")
    return result


@dataclass(frozen=True)
class Plan:
    state: DeviceState
    image: Image | None
    plugins: tuple[str, ...]
    slot: dict | None
    token: object

    @property
    def downgrade(self):
        target = self.image.version if self.image else self.slot["version"]
        return version_key(target) < version_key(self.state.values["version"])


def preflight(ssh, image=None, *, switch=False):
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
        assert_idle(state, writing=True, image=image)
        for name in ("rootdev", "swupdate", "systemd-run", "unshare", "e2fsck", "fuser", "sha256sum"):
            ssh.exec_checked(shell("command", "-v", name))
        if ssh.exec_checked("rootdev --active").strip() != state.values["root"]:
            raise RuntimeError("原生工具报告的当前分区不一致。")
        if ssh.exec_checked("rootdev --next-boot").strip() != state.values["root"]:
            raise RuntimeError("已有下次启动分区切换，拒绝再次切换。")
        plugins = inspect_plugins(ssh)
        if image and ssh.exec_checked("systemctl is-enabled rm-apply-ota.service").strip() != "enabled":
            raise RuntimeError("原生重启应用更新服务未启用，无法保证安装后的启动切换。")
        slot = inspect_slot(ssh, state) if switch else None
        return Plan(state, image, plugins, slot, token)


def _write_remote(ssh, path, data):
    with ssh.open_remote(path, "wx") as file:
        file.write(data)
        file.flush()


def same_device_state(left, right):
    volatile = {"battery", "free", "tmpfree"}
    return ({k: v for k, v in left.values.items() if k not in volatile}
            == {k: v for k, v in right.values.items() if k not in volatile})


def _lock_and_revalidate(ssh, plan):
    ssh.exec_checked("mkdir /tmp/rmtool-xovi-standalone.lock")
    try:
        state = parse_state(ssh.exec_checked(PROBE))
        # The lock was acquired above by this session, not inherited from an
        # unknown owner. Normalize only this local snapshot for preflight.
        state.values["shared_lock"] = "idle"
        assert_idle(state, writing=True, image=plan.image)
        if not same_device_state(state, plan.state) or inspect_plugins(ssh) != plan.plugins:
            raise RuntimeError("暂存期间设备或插件状态已变化，请重新确认。")
        if plan.slot is not None and inspect_slot(ssh, state) != plan.slot:
            raise RuntimeError("备用分区内容已变化。")
    except Exception:
        ssh.exec_checked("rmdir /tmp/rmtool-xovi-standalone.lock")
        raise


def prepare_updater(ssh, token, *, confirmed=False, restore=False):
    if not confirmed:
        raise RuntimeError("自动更新服务变更尚未确认。")
    with firmware_session(ssh, token):
        state, transaction = inspect_device(ssh)
        if transaction[0] not in ("none", "completed"):
            raise RuntimeError(transaction[1])
        assert_idle(state)
        receipt = BASE + "/pause.json"
        if restore:
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
        if state.values["engine_file"] in ("masked", "masked-runtime"):
            return "自动更新服务已屏蔽；不会覆盖原策略。"
        if state.values["engine"] not in ("active", "inactive"):
            raise RuntimeError("自动更新服务不处于稳定状态。")
        ssh.exec_checked(f"set -eu; test ! -L {BASE}; if [ -e {BASE} ]; then "
                         f"test -d {BASE}; test \"$(stat -c '%u:%a' {BASE})\" = 0:700; "
                         f"else mkdir -m 700 {BASE}; fi")
        saved = {"engine": state.values["engine"], "engine_file": state.values["engine_file"],
                 "boot_id": _remote_text(ssh, "/proc/sys/kernel/random/boot_id", 64).strip()}
        _write_remote(ssh, receipt + ".tmp", json.dumps(saved).encode())
        ssh.exec_checked(f"sync {receipt}.tmp && mv {receipt}.tmp {receipt} && sync")
        # Masking prevents service activation; recheck actual partition users
        # immediately before stopping the idle daemon. Never stop a writer.
        ssh.exec_checked(GUARD_PROBE + "\nsystemctl mask --runtime update-engine.service\n" +
                         GUARD_PROBE + "\nsystemctl stop update-engine.service\n"
                         "test \"$(systemctl show update-engine.service -p ActiveState --value)\" = inactive")
        return "自动更新已临时暂停。未安装或预检查失败时可恢复；重启后临时屏蔽失效。"


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
finish() {{
    code=$?
    trap - EXIT
    if [ "$code" = 0 ]; then echo success > "$result.tmp"; else echo failed > "$result.tmp"; fi
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
test "$(cat /sys/class/power_supply/max77818-charger/online)" = 1
test "$(cat /sys/class/power_supply/max77818_battery/capacity)" -ge 50
test "$(systemctl show update-engine.service -p ActiveState --value)" = inactive
case "$(systemctl show update-engine.service -p UnitFileState --value)" in masked|masked-runtime) ;; *) exit 1;; esac
if fuser {plan.state.values['b' if plan.state.active == 'a' else 'a']} >/dev/null 2>&1; then exit 1; else test "$?" = 1; fi
echo '{plan.image.sha256}  {path}' | sha256sum -c -
{native_check_command(path, plan.state.platform, plan.state.active)}
# Use the same native engine without sourcing conf.d (which resets counters).
# Native extraction stages archive filenames before preinstall scripts.
{native_check_command(path, plan.state.platform, plan.state.active).replace(' -c ', ' ')}
test "$(cat {LPGPR}/swu_status)" = 1
{shell('unshare', '--mount', '--', '/bin/sh', '-c', slot_script('b' if plan.state.active == 'a' else 'a'))} > {directory}/target.tmp
grep -Fx 'version={plan.image.version}' {directory}/target.tmp
mv {directory}/target.tmp {directory}/target
sync
'''


def start_install(ssh, plan, *, confirmed=False, downgrade_confirmed=False, progress=None):
    if not confirmed or (plan.downgrade and not downgrade_confirmed):
        raise RuntimeError("安装或降级尚未明确确认。")
    with firmware_session(ssh, plan.token):
        current = preflight(ssh, plan.image)
        if not same_device_state(current.state, plan.state) or current.plugins != plan.plugins:
            raise RuntimeError("设备状态已变化，请重新检测并确认。")
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
        import _xovi_standalone as shared
        # Same remote lock as plugin installers. Retained through the detached
        # job and reboot: uncertain jobs must not unlock competing writers.
        _lock_and_revalidate(ssh, plan)
        ssh.firmware_guard_reason = "固件安装已启动或结果待确认；其他设备操作已锁定。"
        _write_remote(ssh, BASE + "/current.tmp", (job + "\n").encode())
        ssh.exec_checked(f"sync {BASE}/current.tmp && mv {BASE}/current.tmp {BASE}/current && sync")
        if plan.plugins:
            shared._set_recovery_sentinel_locked(ssh)
        try:
            ssh.exec_checked(shell("systemd-run", "--unit=" + UNIT + "-" + job, "--service-type=oneshot",
                "--property=RemainAfterExit=yes", "--property=TimeoutStartSec=infinity",
                "--property=StandardOutput=append:" + directory + "/install.log",
                "--property=StandardError=append:" + directory + "/install.log",
                "--property=TemporaryFileSystem=/tmp:rw,size=" + str(plan.image.unpacked_size + 128 * 1024**2),
                "/bin/bash", directory + "/install.sh"))
        except Exception:
            # Starting may have succeeded despite a lost SSH acknowledgement.
            return "unknown", "启动结果待确认；请查询设备端事务，不要重复安装。"
        return "running", "设备端安装已提交；完成后仍需单独确认重启。"


def switch_slot(ssh, plan, *, confirmed=False, downgrade_confirmed=False):
    if not confirmed or plan.slot is None or (plan.downgrade and not downgrade_confirmed):
        raise RuntimeError("切换或降级尚未明确确认。")
    with firmware_session(ssh, plan.token):
        current = preflight(ssh, switch=True)
        if not same_device_state(current.state, plan.state) or current.slot != plan.slot or current.plugins != plan.plugins:
            raise RuntimeError("分区状态已变化，请重新确认。")
        import _xovi_standalone as shared
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
finish() {{
 code=$?
 trap - EXIT
 if [ "$code" = 0 ]; then echo success; else echo failed; fi > {directory}/result.tmp
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
test "$(cat /sys/class/power_supply/max77818-charger/online)" = 1
test "$(cat /sys/class/power_supply/max77818_battery/capacity)" -ge 50
rootdev --switch
test "$(rootdev --next-boot)" = {target}
'''
        _write_remote(ssh, directory + "/switch.sh", script.encode())
        _lock_and_revalidate(ssh, plan)
        ssh.firmware_guard_reason = "分区切换结果待确认，其他设备操作已锁定。"
        _write_remote(ssh, BASE + "/current.tmp", (job + "\n").encode())
        ssh.exec_checked(f"sync {BASE}/current.tmp && mv {BASE}/current.tmp {BASE}/current && sync")
        if plan.plugins:
            shared._set_recovery_sentinel_locked(ssh)
        try:
            ssh.exec_checked(shell("systemd-run", "--unit=" + UNIT + "-" + job, "--service-type=oneshot",
                                  "--property=RemainAfterExit=yes", "/bin/bash", directory + "/switch.sh"))
        except Exception:
            return "unknown", "切换提交结果待确认，请重连查询。"
        return "running", "切换已提交；完成后仍需单独确认重启。"


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
        contents = key_values(ssh.exec_checked(shell("unshare", "--mount", "--", "/bin/sh", "-c", slot_script(target))))
        if contents != key_values(_remote_text(ssh, directory + "/target")):
            raise RuntimeError("目标分区健康状态或内容已变化，拒绝重启。")
        ssh.exec_checked("systemctl reboot")
        ssh.close()
