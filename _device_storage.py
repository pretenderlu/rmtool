"""Read-only capacity checks for small, root-backed legacy device storage."""

import base64
import re
import shlex
import struct


LEGACY_ROOT_RESERVE = 8 * 1024 * 1024
SYSTEM_SPACE_COMMAND = (
    "target=/; if mountpoint -q /data 2>/dev/null; then target=/data; fi; "
    "printf '%s=' \"$target\"; "
    "df -Pk \"$target\" 2>/dev/null | "
    "awk 'NR > 1 && $4 ~ /^[0-9]+$/ {print $4; exit}'"
)
ROOT_SPACE_COMMAND = r"""set -eu
export LC_ALL=C
machine=$(cat /sys/devices/soc0/machine)
case "$machine" in
    'reMarkable 1.0'|'reMarkable 2.0') ;;
    *) printf 'ordinary\n'; exit 0 ;;
esac
if [ "$(id -u)" != 0 ]; then printf 'ordinary\n'; exit 0; fi
root_mount=$(awk '$5 == "/" {print}' /proc/self/mountinfo)
device_id=$(printf '%s\n' "$root_mount" | awk '{print $3}')
case "$device_id" in ''|*[!0-9:]*) exit 1 ;; esac
device_path=$(readlink -f "/sys/dev/block/$device_id")
device=${device_path##*/}
case "$device" in ''|*[!a-zA-Z0-9_-]*) exit 1 ;; esac
[ -b "/dev/$device" ]
printf '%s\n' "$root_mount"
stat -f -c '%t %S %b %f %a %c %d' /
cat "/sys/fs/ext4/$device/reserved_clusters"
dd if="/dev/$device" bs=1024 skip=1 count=1 2>/dev/null | base64 | tr -d '\n'
printf '\n'
"""


def root_free_bytes(ssh_client, ordinary_free: int) -> int:
    """Count privileged ext4 blocks only with complete legacy-root evidence."""
    try:
        output = ssh_client.exec_checked(ROOT_SPACE_COMMAND).strip()
        if output == "ordinary":
            return ordinary_free
        mount, stats, reserved, encoded = output.splitlines()
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
        sb = base64.b64decode(encoded, validate=True)
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
        raise RuntimeError("无法确认根分区 / 的安全可用空间，已停止写入；请导出诊断日志。") from exc


def df_free_bytes(ssh_client, path: str) -> int:
    value = ssh_client.exec_checked(
        f"df -Pk {shlex.quote(path)} | "
        "awk 'NR > 1 && $(NF-2) ~ /^[0-9]+$/ {print $(NF-2); exit}'"
    ).strip()
    if not re.fullmatch(r"[0-9]+", value):
        raise RuntimeError(f"无法确认 {path} 剩余空间，已停止写入。")
    return int(value) * 1024


def system_free_bytes(ssh_client) -> tuple[str, int]:
    output = ssh_client.exec_checked(SYSTEM_SPACE_COMMAND).strip()
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


def require_capacity(partition: str, available: int, required: int) -> None:
    if available < required:
        raise RuntimeError(
            f"{partition} 空间不足：本次操作含安全余量需要 {required / 1048576:.2f} MiB，"
            f"可用 {available / 1048576:.2f} MiB，还差 {(required - available) / 1048576:.2f} MiB。"
            "请清理已确认归属的旧安装，勿删除未知系统文件；"
            "不需要密码锁屏的字体可选择仅部署到 /home。"
        )
