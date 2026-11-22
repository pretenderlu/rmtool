"""Bounded adoption of the non-stock root font on RM2 3.29.2.1."""

from dataclasses import dataclass, replace
import hashlib
import posixpath
import re
import shlex
import stat

import _device_storage as storage
import _rmkit_cn as fonts


TARGET = ("20261110104847", "rm2", "armv7l",
          "dec257e07b443685a612a058119a88ede6dcba5365b81e265180785851ada6cc")
NAME = "LXGWWenKaiMonoGB-Regular.ttf"
SOURCE = "/usr/share/fonts/ttf/" + NAME
BACKUP = fonts.USER_FONT_REPOSITORY + "/" + NAME
SMALL = fonts.USER_FONT_REPOSITORY + "/" + fonts.BUNDLED_FALLBACK_FONT_NAME
MIRROR = fonts.SYSTEM_FONT_PATHS[".otf"]
FALLBACK_PATHS = frozenset((SMALL, MIRROR, fonts.FONTCONFIG_FILE, fonts.SYSTEM_FONTCONFIG_FILE))
# Upstream lxgw/LxgwWenkaiGB v1.510 and v1.511 both match the reported mmap length.
RELEASES = {
    19639888: "84f46cbc599e249930a5bcde6063fb94f463010b4141ab6f5077fb96c3079c6a",
    19639344: "0ac099555f90a8ec71aff75931866c5b9364b674f5d507da2acee77d363c2a66",
}


@dataclass(frozen=True)
class WenKaiPlan:
    digest: str
    source_stat: str
    mounts: str
    backup_exists: bool
    layout: str
    allocated: int
    configured: tuple[str, ...]

    @property
    def estimated_reclaim_bytes(self):
        return self.allocated - (fonts.BUNDLED_FALLBACK_FONT_SIZE if self.layout == "new" else 0)

    @property
    def description(self):
        placement = ("沿用本机已验证的仅 /home 配置，解锁前可能使用默认字体。" if self.layout == "home" else
                     "系统改用小体积中文字体，保留解锁前的中文显示，不改变密码设置。")
        return "将霞鹜文楷完整保存在 /home 字体库。" + placement + "验证新配置后才移除系统分区的旧文楷。"


def _entry(sftp, path):
    try:
        return sftp.lstat(path)
    except FileNotFoundError:
        return None


def _stat(ssh, path):
    value = ssh.exec_checked("stat -c '%d:%i:%h:%s:%b:%u:%g:%a' " + shlex.quote(path)).strip()
    if not re.fullmatch(r"[0-9]+:[0-9]+:1:[0-9]+:[0-9]+:0:0:644", value):
        raise RuntimeError("字体文件类型、权限或硬链接不符合已知布局。")
    return value


def _mounts(ssh, paths):
    raw = ssh.exec_checked("cat /proc/self/mountinfo")
    root = storage.root_mount_fields(raw)
    homes = []
    relevant = []
    for line in raw.splitlines():
        f = line.split()
        if len(f) < 10 or "-" not in f:
            raise RuntimeError("字体挂载记录不完整。")
        point = f[4]
        if point not in ("/", "/home"):
            if any(point == p or p.startswith(point + "/") or point.startswith(p + "/") for p in paths):
                raise RuntimeError("字体路径存在独立挂载，保留原件。")
            continue
        if point == "/home":
            homes.append(f)
        relevant.append(line)
    if (len(homes) != 1 or homes[0][3] != "/" or homes[0][2] == root[2]
            or homes[0][homes[0].index("-") + 1] != "ext4" or "rw" not in homes[0][5].split(",")
            or root[root.index("-") + 1] != "ext4" or "rw" not in root[5].split(",")):
        raise RuntimeError("未确认可写的 RM2 根分区和独立 /home，保留原件。")
    home = homes[0]
    source = home[home.index("-") + 2]
    if source != "/dev/mmcblk2p4":
        # A separate ext4 loop mount is not evidence of a durable backup.
        if (not re.fullmatch(r"/dev/(?:mapper/encrypted|dm-[0-9]+)", source)
                or not re.fullmatch(r"[0-9]+:[0-9]+", home[2])
                or ssh.exec_checked(f"ls -1 /sys/dev/block/{home[2]}/slaves").splitlines() != ["mmcblk2p4"]):
            raise RuntimeError("未确认 /home 位于 RM2 的持久数据分区。")
    return hashlib.sha256("\n".join(relevant).encode()).hexdigest()


def inspect(ssh, device):
    if device != TARGET:
        return None
    paths = (SOURCE, BACKUP, SMALL, fonts.FONTCONFIG_FILE, *fonts.SYSTEM_FONT_STATE_PATHS,
             *fonts.SYSTEM_FONT_VALIDATION_ARTIFACTS, fonts.BACKUP_DIR, fonts.FONT_MARKER_PATH)
    with ssh.sftp_session() as sftp:
        if _entry(sftp, SOURCE) is None:
            return None
        for parent in fonts._home_only_parents(paths):
            a = _entry(sftp, parent)
            if a and (not stat.S_ISDIR(a.st_mode) or a.st_uid != 0 or a.st_gid != 0 or a.st_mode & 0o022):
                raise RuntimeError(f"字体目录不可信：{parent}")
        files = {}
        for path in paths:
            if path == fonts.BACKUP_DIR:
                continue
            a = _entry(sftp, path)
            if a is not None:
                if not stat.S_ISREG(a.st_mode) or a.st_uid != 0 or a.st_gid != 0 or a.st_mode & 0o022:
                    raise RuntimeError(f"检测到非预期的字体文件：{path}")
                files[path] = a.st_size
        if _entry(sftp, fonts.SYSTEM_FONT_DIR) is not None:
            if set(sftp.listdir(fonts.SYSTEM_FONT_DIR)) - {posixpath.basename(MIRROR)}:
                raise RuntimeError("锁屏字体目录已有其他文件，保留原件。")
    mounts = _mounts(ssh, paths)
    source_stat = _stat(ssh, SOURCE)
    size, blocks = map(int, source_stat.split(":")[3:5])
    digest = RELEASES.get(size)
    if not digest or files.get(SOURCE) != size or fonts._remote_sha256(ssh, SOURCE) != digest:
        raise RuntimeError("根分区文楷与已验证的上游字体不一致，保留原件。")
    exists = BACKUP in files
    if exists:
        _stat(ssh, BACKUP)
        if files[BACKUP] != size or fonts._remote_sha256(ssh, BACKUP) != digest:
            raise RuntimeError("/home 已有不同的同名字体，未覆盖。")
    if fonts.get_font_recovery_status(ssh).state != "none":
        raise RuntimeError("请先完成上一次字体恢复。")
    keys = set(files) - {SOURCE, BACKUP}
    if not keys:
        layout = "new"
        if fonts._matched_font_path(ssh, "sans:lang=zh-cn") != SOURCE:
            raise RuntimeError("当前中文字体已变化，请重新检测。")
    elif exists and keys == {fonts.FONTCONFIG_FILE}:
        cfg = fonts.fontconfig_override(fonts._scan_font_family(ssh, BACKUP), BACKUP).encode()
        if fonts._remote_sha256(ssh, fonts.FONTCONFIG_FILE) != hashlib.sha256(cfg).hexdigest():
            raise RuntimeError("当前用户字体配置并非已知的仅 /home 配置。")
        fonts._verify_font_override_matches(ssh, BACKUP)
        layout = "home"
    elif exists and SMALL in keys and keys <= FALLBACK_PATHS:
        for path in (MIRROR, SMALL):
            if path not in keys:
                continue
            _stat(ssh, path)
            if fonts._remote_sha256(ssh, path) != fonts.BUNDLED_FALLBACK_FONT_SHA256:
                raise RuntimeError("现有小字体与内置字体不一致，保留原件。")
        family = fonts._scan_font_family(ssh, SMALL)
        user = fonts.fontconfig_override(family, SMALL)
        system = fonts.fontconfig_override(family, MIRROR).replace(
            "<!-- ponytail: user-level UI font override; restore the prior file to undo. -->",
            "<!-- rmtool system UI font; owner: user -->",
        )
        for path, config in ((fonts.FONTCONFIG_FILE, user), (fonts.SYSTEM_FONTCONFIG_FILE, system)):
            if path in keys and fonts._remote_sha256(ssh, path) != hashlib.sha256(config.encode()).hexdigest():
                raise RuntimeError("现有字体配置与本次恢复不匹配，保留原件。")
        if keys == FALLBACK_PATHS:
            fonts._verify_system_font_matches(ssh, MIRROR)
            if (any(fonts._matched_font_path(ssh, p) not in (SMALL, MIRROR) for p in fonts.FONT_OVERRIDE_MATCH_PATTERNS)
                    or not fonts.has_cjk_font(ssh)):
                raise RuntimeError("尚未验证新配置的中文覆盖，保留原件。")
            layout = "protected"
        else:
            layout = "new"  # Verified prefix of an interrupted fallback installation.
    else:
        raise RuntimeError("当前设备已有其他字体配置，未接管或覆盖。")
    return WenKaiPlan(digest, source_stat, mounts, exists, layout, min(size, blocks * 512), tuple(sorted(keys)))


def apply(ssh, device, plan):
    from _xovi_standalone import _operation_lock
    from rmtool import resource_path

    with _operation_lock(ssh):
        if inspect(ssh, device) != plan:
            raise RuntimeError("文楷、配置或挂载已变化，请重新检测。")
        if plan.layout == "new":
            path = resource_path("assets", "fonts", fonts.BUNDLED_FALLBACK_FONT_NAME)
            if hashlib.sha256(path.read_bytes()).hexdigest() != fonts.BUNDLED_FALLBACK_FONT_SHA256:
                raise RuntimeError("内置兜底字体校验失败。")
            partition, free = storage.system_free_bytes(ssh)
            if partition != "/":
                raise RuntimeError("字体所在分区变化。")
            storage.require_capacity("/", free, fonts.BUNDLED_FALLBACK_FONT_SIZE + storage.LEGACY_ROOT_RESERVE)
        needed = ((0 if plan.backup_exists else int(plan.source_stat.split(":")[3]))
                  + (fonts.BUNDLED_FALLBACK_FONT_SIZE if plan.layout == "new" else 0))
        if needed:
            storage.require_capacity("/home", storage.df_free_bytes(ssh, "/home"),
                                     needed + 1048576)
        if not plan.backup_exists:
            ssh.exec_checked(f"""set -eu
umask 077
[ "$(stat -c %d /home/root)" = "$(stat -c %d /home)" ]
[ "$(stat -c %d /home)" != "$(stat -c %d /)" ]
mkdir -p {fonts.USER_FONT_REPOSITORY}
[ "$(readlink -f {fonts.USER_FONT_REPOSITORY})" = {fonts.USER_FONT_REPOSITORY} ]
[ "$(stat -c %d {fonts.USER_FONT_REPOSITORY})" = "$(stat -c %d /home)" ]
t=$(mktemp {BACKUP}.XXXXXX)
trap 'rm -f "$t"' EXIT
trap 'exit 1' HUP INT TERM PIPE
cp {SOURCE} "$t"
chmod 0644 "$t"
[ "$(sha256sum "$t" | cut -d' ' -f1)" = {plan.digest} ]
ln "$t" {BACKUP}
rm -f "$t"
sync""")
        if inspect(ssh, device) != replace(plan, backup_exists=True):
            raise RuntimeError("备份复核失败，保留根分区原件。")
        if plan.layout == "new":
            fonts.install_user_font_override(ssh, str(path), fonts.USER_FONT_REPOSITORY,
                                             fonts.BUNDLED_FALLBACK_FONT_NAME, lock_screen_support=True)
        expected = (plan if plan.layout == "home" else
                    replace(plan, backup_exists=True, layout="protected", configured=tuple(sorted(FALLBACK_PATHS))))
        if inspect(ssh, device) != expected:
            raise RuntimeError("新字体配置未通过验证，保留根分区原件。")
        check_matches = []
        for p in fonts.FONT_OVERRIDE_MATCH_PATTERNS:
            query = "fc-match --format='%{file}' " + shlex.quote(p)
            if expected.layout == "home":
                check_matches.append(f'[ "$({query})" = {BACKUP} ]')
            else:
                check_matches += [
                    f'case "$({query})" in {SMALL}|{MIRROR}) ;; *) exit 1 ;; esac',
                    f'[ "$({fonts.SYSTEM_FONT_MATCH_ENV} {query})" = {MIRROR} ]',
                ]
        ssh.exec_checked(f"""set -eu
{chr(10).join(check_matches)}
sync
[ "$(readlink -f {SOURCE})" = {SOURCE} ]
[ "$(readlink -f {BACKUP})" = {BACKUP} ]
[ "$(stat -c %d /)" = "$(stat -c %d {SOURCE})" ]
[ "$(stat -c %d /home)" != "$(stat -c %d /)" ]
[ "$(stat -c %d /home)" = "$(stat -c %d {BACKUP})" ]
[ "$(stat -c '%d:%i:%h:%s:%b:%u:%g:%a' {SOURCE})" = {shlex.quote(plan.source_stat)} ]
[ "$(sha256sum {BACKUP} | cut -d' ' -f1)" = {plan.digest} ]
cmp -s {SOURCE} {BACKUP}
rm {SOURCE}
sync""")
