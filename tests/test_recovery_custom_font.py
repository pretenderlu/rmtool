"""Reproduce RM2's external root font, using real backup and unlink shell commands."""

from contextlib import nullcontext
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _capacity_recovery as recovery
import _recovery_custom_font as w
import _rmkit_cn as fonts


ROOT = "17 1 179:2 / / rw,relatime - ext4 /dev/root rw"
HOME = "26 17 179:4 / /home rw,relatime - ext4 /dev/mmcblk2p4 rw"
SH = shutil.which("sh")
if os.name == "nt":
    git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
    if git_bash.exists():
        SH = str(git_bash)


class Device:
    def __init__(self, root, name="用户's $(自选) 字体.otf"):
        self.root = root
        self.source = w.ROOT_FONTS + "/" + name
        self.backup = fonts.USER_FONT_REPOSITORY + "/" + name
        self.mounts = ROOT + "\n" + HOME
        self.fail = ""
        self.family = "Noto Sans CJK SC"
        self.source_stat = "456:8362:1:19639344:38360:0:0:644"
        self.active = self.source
        self.early = self.source
        self.mutations = []

    def path(self, p):
        return self.root / p.lstrip("/")

    def put(self, p, data):
        local = self.path(p)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)

    def sftp_session(self):
        return nullcontext(self)

    def listdir(self, p):
        return [f.name for f in self.path(p).iterdir()]

    def open(self, p, mode):
        return self.path(p).open(mode)

    def lstat(self, p):
        s = self.path(p).lstat()
        return SimpleNamespace(st_size=s.st_size, st_uid=0, st_gid=0,
                               st_mode=(stat.S_IFDIR | 0o755 if stat.S_ISDIR(s.st_mode) else stat.S_IFREG | 0o644))

    def install(self, ssh, local, directory, name, *, lock_screen_support):
        assert ssh is self and lock_screen_support and directory + "/" + name == w.SMALL
        assert self.path(self.backup).read_bytes() == self.path(self.source).read_bytes()
        self.mutations.append("configure")
        if self.fail == "install":
            raise RuntimeError("failed deployment")
        self.put(w.SMALL, Path(local).read_bytes())
        self.put(w.MIRROR, self.path(w.SMALL).read_bytes())
        self.put(fonts.FONTCONFIG_FILE, fonts.fontconfig_override(self.family, w.SMALL).encode())
        self.put(fonts.SYSTEM_FONTCONFIG_FILE, fonts.fontconfig_override(self.family, w.MIRROR).replace(
            "<!-- ponytail: user-level UI font override; restore the prior file to undo. -->",
            "<!-- rmtool system UI font; owner: user -->",
        ).encode())
        self.active = w.SMALL
        if self.fail != "early":
            self.early = w.MIRROR

    def exec_checked(self, cmd):
        if cmd == "cat /proc/self/mountinfo":
            return self.mounts
        if cmd.startswith("stat -c "):
            return self.source_stat if shlex.split(cmd)[-1] == self.source else "999:55:1:100:8:0:0:644"
        if cmd.startswith("sha256sum "):
            return hashlib.sha256(self.path(shlex.split(cmd)[1]).read_bytes()).hexdigest()
        if cmd.startswith("fc-scan "):
            return ' family: "' + self.family + '"'
        if "fc-match " in cmd and not cmd.startswith("set -eu"):
            return self.early if cmd.startswith(fonts.SYSTEM_FONT_MATCH_ENV) else self.active
        if cmd == fonts.CJK_FONT_LIST_COMMAND:
            return self.active
        if not cmd.startswith("set -eu"):
            raise AssertionError(cmd)
        op = "backup" if ".font-backup.XXXXXX" in cmd else "record" if "mktemp" in cmd else "unlink"
        self.mutations.append(op)
        if op == "unlink" and self.fail == "drift":
            self.put(self.source, b"changed after verification")
        base = subprocess.check_output([SH, "-c", "pwd -P"], cwd=self.root, text=True).strip()
        script = cmd.replace("/home", base + "/home").replace("/usr/share/fonts", base + "/usr/share/fonts").replace("/data/", base + "/data/")
        script = script.replace("fc-match ", "fc_match ")
        prefix = """
set -eux
base=$(pwd -P)
sync() { :; }
stat() {
    if [ "$2" = %d ]; then
        case "$3" in "$base"/home*) echo 999;; *) echo 456;; esac
    else echo STATVALUE; fi
}
fc_match() { if [ "$HOME" = /nonexistent ]; then printf '%s' "$base"EARLY; else printf '%s' "$base"ACTIVE; fi; }
""".replace("STATVALUE", self.source_stat).replace("EARLY", shlex.quote(self.early)).replace("ACTIVE", shlex.quote(self.active))
        if self.fail == "backup":
            prefix += '\ncp() { printf partial > "$2"; }\n'
        result = subprocess.run([SH, "-c", prefix + script], cwd=self.root,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        if result.returncode:
            raise RuntimeError(f"script failed: {result.stderr}")
        return result.stdout


@unittest.skipUnless(SH, "POSIX shell or Git Bash required")
class CustomFontRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ssh = Device(Path(tmp.name))
        # Same disk usage as the reported RM2, unrelated name and bytes.
        self.original = b"x" * 19639344
        self.ssh.put(self.ssh.source, self.original)
        self.ssh.path("/home").mkdir()
        self.digest = hashlib.sha256(self.original).hexdigest()
        for mocker in (
            patch.object(fonts, "install_user_font_override", side_effect=self.ssh.install),
            patch("rmtool.resource_path", side_effect=lambda *p: Path(__file__).resolve().parents[1].joinpath(*p)),
            patch("_xovi_standalone._operation_lock", return_value=nullcontext()),
            patch.object(w.storage, "system_free_bytes", return_value=("/", 19577000)),
            patch.object(w.storage, "df_free_bytes", return_value=2791728742),
        ):
            self.enterContext(mocker)

    def scan(self):
        return w.inspect(self.ssh, w.TARGET)

    def test_reported_layout_unknown_passcode_preserves_preunlock(self):
        plan = self.scan()
        self.assertEqual(plan.layout, "new")
        self.assertEqual(self.ssh.mutations, [])
        self.assertGreater(plan.estimated_reclaim_bytes, 8.22 * 1048576)
        w.apply(self.ssh, w.TARGET, plan)
        self.assertEqual(self.ssh.mutations, ["backup", "record", "configure", "unlink"])
        self.assertEqual(self.ssh.path(self.ssh.backup).read_bytes(), self.original)
        self.assertFalse(self.ssh.path(self.ssh.source).exists())
        self.assertFalse(self.ssh.path(w.PENDING).exists())
        self.assertEqual(self.ssh.early, w.MIRROR)
        self.assertIsNone(self.scan())

    def test_existing_verified_home_preference_keeps_custom_font(self):
        self.ssh.put(self.ssh.backup, self.original)
        self.ssh.put(fonts.FONTCONFIG_FILE, fonts.fontconfig_override(self.ssh.family, self.ssh.backup).encode())
        self.ssh.active = self.ssh.backup
        plan = self.scan()
        self.assertEqual(plan.layout, "home")
        w.apply(self.ssh, w.TARGET, plan)
        self.assertEqual(self.ssh.mutations, ["unlink"])
        self.assertFalse(self.ssh.path(w.MIRROR).exists())

    def test_other_families_and_sizes_do_not_require_a_hash_allowlist(self):
        for name, data in (("Bookerly.ttf", b"A" * 8192), ("思源黑体.OTF", b"B" * 12288),
                           ("LXGWWenKaiMonoGB-Regular.ttf", b"C" * 16384)):
            with self.subTest(name=name):
                self.ssh.source = w.ROOT_FONTS + "/" + name
                self.ssh.active = self.ssh.source
                self.ssh.put(self.ssh.source, data)
                self.ssh.source_stat = f"456:8362:1:{len(data)}:32:0:0:644"
                plan = self.scan()
                self.assertEqual((plan.name, plan.digest), (name, hashlib.sha256(data).hexdigest()))
                self.assertIn(name, plan.description)
        self.assertFalse(self.ssh.mutations)

    def test_stock_epub_only_and_unselected_fonts_are_not_candidates(self):
        for path in (w.ROOT_FONTS + "/noto/NotoSans.ttf",
                     w.ROOT_FONTS + "/ebgaramond/EBGaramond.ttf",
                     fonts.USER_FONT_REPOSITORY + "/OnlyInEPUB.ttf"):
            self.ssh.put(path, b"keep")
            self.ssh.active = path
            self.assertIsNone(self.scan())
            self.assertTrue(self.ssh.path(path).exists())
        self.assertTrue(self.ssh.path(self.ssh.source).exists())

    def test_two_active_custom_fonts_and_invalid_font_metadata_are_not_adopted(self):
        other = w.ROOT_FONTS + "/Other.ttf"
        self.ssh.put(other, b"keep")
        with patch.object(fonts, "_matched_font_path", side_effect=(self.ssh.source, other)):
            with self.assertRaisesRegex(RuntimeError, "多个"):
                self.scan()
        with patch.object(fonts, "_scan_font_family", side_effect=RuntimeError("unreadable font")):
            with self.assertRaisesRegex(RuntimeError, "unreadable"):
                self.scan()
        self.assertFalse(self.ssh.mutations)

    def test_managed_mirror_keeps_its_normal_recovery_even_with_same_named_root_file(self):
        self.ssh.put(self.ssh.backup, self.original)
        self.ssh.active = self.ssh.backup
        self.ssh.put(fonts.SYSTEM_FONTCONFIG_FILE, b"managed separately")
        self.assertIsNone(self.scan())
        self.assertFalse(self.ssh.mutations)

    def test_pending_record_cannot_authorize_other_paths_or_different_bytes(self):
        for name in ("../noto/Stock.ttf", self.ssh.source, fonts.BUNDLED_FALLBACK_FONT_NAME):
            self.ssh.put(w.PENDING, f"{name}\n{self.ssh.source_stat}\n{self.digest}\n".encode())
            with self.assertRaisesRegex(RuntimeError, "记录"):
                self.scan()
        self.ssh.put(self.ssh.backup, self.original)
        self.ssh.put(w.PENDING, f"{self.ssh.source.split('/')[-1]}\n{self.ssh.source_stat}\n{'0' * 64}\n".encode())
        with self.assertRaisesRegex(RuntimeError, "已变化"):
            self.scan()
        self.assertFalse(self.ssh.mutations)

    def test_failures_keep_source_and_verified_backup_is_reusable(self):
        for failure in ("backup", "install", "early"):
            with self.subTest(failure=failure):
                self.ssh.fail = failure
                with self.assertRaises(RuntimeError):
                    w.apply(self.ssh, w.TARGET, self.scan())
                self.assertEqual(self.ssh.path(self.ssh.source).read_bytes(), self.original)
                self.assertEqual(self.ssh.path(self.ssh.backup).exists(), failure != "backup")
        self.ssh.fail = ""
        self.ssh.early = w.MIRROR
        retry = self.scan()
        self.assertEqual(retry.layout, "protected")
        w.apply(self.ssh, w.TARGET, retry)
        self.assertEqual(self.ssh.mutations.count("configure"), 2)

    def test_source_drift_at_unlink_and_unknown_font_are_not_deleted(self):
        self.ssh.fail = "drift"
        with self.assertRaises(RuntimeError):
            w.apply(self.ssh, w.TARGET, self.scan())
        self.assertEqual(self.ssh.path(self.ssh.source).read_bytes(), b"changed after verification")
        self.assertEqual(self.ssh.path(self.ssh.backup).read_bytes(), self.original)
        with self.assertRaises(RuntimeError):
            self.scan()

    def test_other_identity_conflicting_mount_backup_and_missing_capacity(self):
        self.assertIsNone(w.inspect(self.ssh, ("other", *w.TARGET[1:])))
        self.ssh.mounts += "\n66 17 179:2 /else /usr/share/fonts rw - ext4 /dev/root rw"
        with self.assertRaisesRegex(RuntimeError, "挂载"):
            self.scan()
        self.ssh.mounts = ROOT + "\n" + HOME.replace("/dev/mmcblk2p4", "/dev/loop0")
        with self.assertRaisesRegex(RuntimeError, "持久"):
            self.scan()
        self.ssh.mounts = ROOT + "\n" + HOME
        self.ssh.put(self.ssh.backup, b"somebody else's file")
        with self.assertRaisesRegex(RuntimeError, "未覆盖"):
            self.scan()
        self.assertEqual(self.ssh.path(self.ssh.backup).read_bytes(), b"somebody else's file")
        self.ssh.path(self.ssh.backup).unlink()
        with patch.object(w.storage, "df_free_bytes", return_value=1024), self.assertRaisesRegex(RuntimeError, "空间不足"):
            w.apply(self.ssh, w.TARGET, self.scan())
        self.assertEqual(self.ssh.mutations, [])

    def test_changed_content_after_scan_and_hardlinks_are_rejected(self):
        self.ssh.source_stat = self.ssh.source_stat.replace(":8362:1:", ":8362:2:")
        with self.assertRaisesRegex(RuntimeError, "硬链接"):
            self.scan()
        self.ssh.source_stat = self.ssh.source_stat.replace(":8362:2:", ":8362:1:")
        plan = self.scan()
        with self.ssh.path(self.ssh.source).open("r+b") as stream:
            stream.write(b"y")
        with self.assertRaisesRegex(RuntimeError, "已变化"):
            w.apply(self.ssh, w.TARGET, plan)
        self.assertEqual(self.ssh.mutations, [])

    def test_capacity_plan_binds_source_even_if_required_bytes_unchanged(self):
        plan = self.scan()
        intent = recovery.OriginalIntent(self.ssh, None, w.TARGET, "qr-upload", object(), ".")
        cap = recovery.CapacityContext("shared-capacity", "/", 28196208, 19577000, 8619208)
        initial = recovery.CapacityPlan(intent, cap, plan, "install")
        other = recovery.CapacityPlan(intent, cap, replace(plan, source_stat="changed"), "install")
        self.assertNotEqual(initial.fingerprint, other.fingerprint)

    def test_reported_qr_install_only_resumes_when_remeasured_space_is_sufficient(self):
        cap = recovery.CapacityContext("shared-capacity", "/", 28196208, 19577000, 8619208)
        intent = recovery.OriginalIntent(self.ssh, None, w.TARGET, "qr-upload", object(), ".")
        error = RuntimeError("capacity")
        error.recovery_context = vars(cap)
        error.shared_enable_plan = "qr"
        with patch.object(recovery.tap, "get_device_identity", return_value=recovery.tap.DeviceIdentity(*w.TARGET)), \
                patch.object(recovery, "_run_preflight", side_effect=(error, error, "qr")), \
                patch.object(recovery, "_run_original") as install:
            plan = recovery.scan(self.ssh, intent)
            self.assertIsInstance(plan.home_only, w.CustomFontPlan)
            # Simulate xochitl still holding the unlinked inode.
            result = recovery.execute(self.ssh, plan)
        self.assertEqual(result.state, "insufficient")
        self.assertTrue(result.cleaned)
        self.assertEqual(result.after_capacity.shortfall, cap.shortfall)
        install.assert_not_called()
