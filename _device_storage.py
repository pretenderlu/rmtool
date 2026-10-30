"""Read-only capacity checks for small, root-backed legacy device storage."""

import logging
import re
import shlex
import struct

from _ssh import quick_checked


LEGACY_ROOT_RESERVE = 8 * 1024 * 1024
ROOT_MOUNT_AWK = r'''$5 == "/" {
    if (NF < 10 || $4 != "/") { bad=1; next }
    signature=$3
    for (i=4; i<=NF; i++) signature=signature " " $i
    if (found && signature != previous) bad=1
    previous=signature; found=1; selected=$0
}
END { if (bad || !found) exit 1; print selected }'''
SYSTEM_SPACE_COMMAND = (
    "target=/; if mountpoint -q /data 2>/dev/null; then target=/data; fi; "
    "printf '%s=' \"$target\"; "
    "df -Pk \"$target\" 2>/dev/null | "
    "awk 'NR > 1 && $4 ~ /^[0-9]+$/ {print $4; exit}'"
)
ROOT_SPACE_COMMAND = r"""set -efu
export LC_ALL=C
fail() { printf 'root-capacity: %s\n' "$1" >&2; exit 1; }
machine=$(cat /sys/devices/soc0/machine) || fail 'machine read failed'
case "$machine" in
    'reMarkable 1.0'|'reMarkable 2.0') ;;
    *) printf 'ordinary\n'; exit 0 ;;
esac
uid=$(id -u) || fail 'uid read failed'
if [ "$uid" != 0 ]; then printf 'ordinary\n'; exit 0; fi
root_mount=$(awk __ROOT_MOUNT_FILTER__ /proc/self/mountinfo) || fail 'conflicting or invalid root mounts; save documents and restart device'
device_id=$(printf '%s\n' "$root_mount" | awk '{print $3}')
case "$device_id" in ''|*[!0-9:]*) fail 'invalid root device id' ;; esac
device_path=$(readlink -f "/sys/dev/block/$device_id") || fail 'root device resolution failed'
device=${device_path##*/}
case "$device" in ''|*[!a-zA-Z0-9_-]*) fail 'invalid root device name' ;; esac
[ -b "/dev/$device" ] || fail 'root block device missing'
printf 'mount=%s\n' "$root_mount"
stats=$(stat -f -c '%t %S %b %f %a %c %d' /) || fail 'filesystem statistics failed'
printf 'stats=%s\n' "$stats"
reserved=$(cat "/sys/fs/ext4/$device/reserved_clusters") || fail 'reserved clusters read failed'
printf 'reserved=%s\n' "$reserved"
# Stock RM1/RM2 .172 lacks base64 and full od; hexdump supports a checked bounded read.
superblock=$(hexdump -v -s 1024 -n 1024 -e '1/1 "%02x"' "/dev/$device") || fail 'superblock read failed'
[ "${#superblock}" -eq 2048 ] || fail 'superblock short or invalid read'
printf 'superblock=%s\n' "$superblock"
""".replace("__ROOT_MOUNT_FILTER__", shlex.quote(ROOT_MOUNT_AWK))


def root_mount_fields(mountinfo: str) -> list[str]:
    records = (line.split() for line in mountinfo.splitlines())
    roots = [fields for fields in records if len(fields) >= 5 and fields[4] == "/"]
    if not roots:
        raise RuntimeError("无法确认根分区挂载，请保存文档并手动重启设备后重试。")
    for fields in roots:
        if (len(fields) < 10 or fields[3:5] != ["/", "/"]
                or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2])
                or "-" not in fields or len(fields) != fields.index("-") + 4
                or fields[2:] != roots[0][2:]):
            raise RuntimeError("根分区挂载记录无效或存在冲突，请保存文档并手动重启设备后重试。")
    return roots[0]


def root_free_bytes(ssh_client, ordinary_free: int) -> int:
    """Count privileged ext4 blocks only with complete legacy-root evidence."""
    values = {}
    try:
        output = quick_checked(ssh_client, ROOT_SPACE_COMMAND).strip()
        if output == "ordinary":
            return ordinary_free
        keys = ("mount", "stats", "reserved", "superblock")
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if not separator or key not in keys or key in values:
                raise ValueError("unexpected or duplicate root capacity field")
            values[key] = value
        missing = [key for key in keys if key not in values]
        if missing:
            raise ValueError("missing root capacity fields: " + ", ".join(missing))
        mount, stats, reserved, encoded = (values[key] for key in keys)
        fields = mount.split()
        separator = fields.index("-")
        if fields[3:5] != ["/", "/"] or fields[separator + 1] != "ext4":
            raise ValueError("not an ext4 root mount")
        if not re.fullmatch(r"[0-9]+:[0-9]+", fields[2]):
            raise ValueError("invalid root device")
        magic, *numbers = stats.split()
        if magic.lower() != "ef53" or len(numbers) != 6:
            raise ValueError("invalid filesystem statistics")
        if any(not re.fullmatch(r"[0-9]+", item) for item in (*numbers, reserved)):
            raise ValueError("invalid capacity counters")
        block, total, free, available, inodes, free_inodes = map(int, numbers)
        if block not in (1024, 2048, 4096, 8192, 16384, 32768, 65536):
            raise ValueError("invalid block size")
        if total <= 0 or not 0 <= available <= free <= total or not 0 < free_inodes <= inodes:
            raise ValueError("invalid or exhausted blocks/inodes")
        if not re.fullmatch(r"[0-9a-fA-F]{2048}", encoded):
            raise ValueError("invalid superblock hex data or length")
        sb = bytes.fromhex(encoded)
        if len(sb) != 1024 or struct.unpack_from("<H", sb, 0x38)[0] != 0xEF53:
            raise ValueError("invalid ext4 superblock")
        log_block, log_cluster = struct.unpack_from("<II", sb, 0x18)
        if log_block > 6 or not log_block <= log_cluster <= 20 or block != 1024 << log_block:
            raise ValueError("inconsistent ext4 allocation size")
        if struct.unpack_from("<H", sb, 0x3A)[0] & 2:
            raise ValueError("ext4 reports filesystem errors")
        incompat, ro_compat = struct.unpack_from("<II", sb, 0x60)
        if not ro_compat & 0x200 and log_cluster != log_block:
            raise ValueError("cluster size without bigalloc")
        blocks = struct.unpack_from("<I", sb, 0x04)[0]
        if incompat & 0x80:
            blocks += struct.unpack_from("<I", sb, 0x150)[0] << 32
        if total > blocks or int(reserved) * (1024 << log_cluster) > blocks * block:
            raise ValueError("inconsistent ext4 size")
        resuid = struct.unpack_from("<H", sb, 0x50)[0]
        for option in (fields[5] + "," + fields[separator + 3]).split(","):
            if option.startswith("resuid="):
                resuid = int(option.partition("=")[2])
        if resuid != 0:
            return ordinary_free
        # Kernel-internal reserved clusters are not the root user's reserve.
        usable = max(0, free * block - int(reserved) * (1024 << log_cluster))
        if usable < available * block:
            raise ValueError("inconsistent available capacity")
        return usable
    except (RuntimeError, ValueError, IndexError, struct.error) as exc:
        # Superblock bytes include a filesystem UUID; keep payloads out of the PC log.
        logging.error("Legacy root capacity probe failed: %s; field lengths=%s",
                      exc, {key: len(value) for key, value in values.items()})
        raise RuntimeError("无法确认根分区 / 的安全可用空间，已停止写入；请导出诊断日志。") from exc


def df_free_bytes(ssh_client, path: str) -> int:
    value = quick_checked(
        ssh_client,
        f"df -Pk {shlex.quote(path)} | "
        "awk 'NR > 1 && $(NF-2) ~ /^[0-9]+$/ {print $(NF-2); exit}'"
    ).strip()
    if not re.fullmatch(r"[0-9]+", value):
        raise RuntimeError(f"无法确认 {path} 剩余空间，已停止写入。")
    return int(value) * 1024


def system_free_bytes(ssh_client) -> tuple[str, int]:
    output = quick_checked(ssh_client, SYSTEM_SPACE_COMMAND).strip()
    values = []
    partition = "/data"
    for line in output.splitlines():
        prefix, separator, value = line.partition("=")
        if separator and prefix in ("/", "/data"):
            if re.fullmatch(r"[0-9]+", value):
                values.append(value)
                partition = prefix
            continue
        fields = line.split()
        if len(fields) == 1 and re.fullmatch(r"[0-9]+", fields[0]):
            values.append(fields[0])
        elif len(fields) >= 4 and re.fullmatch(r"[0-9]+", fields[3]):
            values.append(fields[3])
    if len(values) != 1:
        raise RuntimeError("无法读取设备 /data 分区可用空间，已停止写入。")
    available = int(values[0]) * 1024
    if partition == "/":
        available = root_free_bytes(ssh_client, available)
    return partition, available


def require_capacity(
    partition: str, available: int, required: int, *, recovery_kind: str = "capacity"
) -> None:
    if available < required:
        error = RuntimeError(
            f"{partition} 空间不足：本次操作含安全余量需要 {required / 1048576:.2f} MiB，"
            f"可用 {available / 1048576:.2f} MiB，还差 {(required - available) / 1048576:.2f} MiB。"
            "请清理已确认归属的旧安装，勿删除未知系统文件；"
            "不需要密码锁屏的字体可选择仅部署到 /home。"
        )
        error.recovery_context = {
            "kind": recovery_kind,
            "partition": partition,
            "required": required,
            "available": available,
            "shortfall": required - available,
        }
        raise error
