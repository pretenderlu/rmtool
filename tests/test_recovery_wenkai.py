"""Reproduce RM2's external root font, using real backup and unlink shell commands."""

from contextlib import nullcontext
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _capacity_recovery as recovery
import _recovery_wenkai as w
import _rmkit_cn as fonts


ROOT = "17 1 179:2 / / rw,relatime - ext4 /dev/root rw"
HOME = "26 17 179:4 / /home rw,relatime - ext4 /dev/mmcblk2p4 rw"
SH = shutil.which("sh")
if os.name == "nt":
    git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
    if git_bash.exists():
        SH = str(git_bash)


class Device:
    def __init__(self, root):
        self.root = root
        self.mounts = ROOT + "\n" + HOME
        self.fail = ""
        self.family = "Noto Sans CJK SC"
        self.source_stat = "456:8362:1:19639344:38360:0:0:644"
        self.active = w.SOURCE
        self.early = w.SOURCE
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

    def lstat(self, p):
        s = self.path(p).lstat()
        return SimpleNamespace(st_size=s.st_size, st_uid=0, st_gid=0,
                               st_mode=(stat.S_IFDIR | 0o755 if stat.S_ISDIR(s.st_mode) else stat.S_IFREG | 0o644))

    def install(self, ssh, local, directory, name, *, lock_screen_support):
        assert ssh is self and lock_screen_support and directory + "/" + name == w.SMALL
        assert self.path(w.BACKUP).read_bytes() == self.path(w.SOURCE).read_bytes()
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
            return self.source_stat if cmd.endswith(w.SOURCE) else "999:55:1:100:8:0:0:644"
        if cmd.startswith("sha256sum "):
            return hashlib.sha256(self.path(cmd.split()[1]).read_bytes()).hexdigest()
        if cmd.startswith("fc-scan "):
            return ' family: "' + self.family + '"'
        if "fc-match " in cmd and not cmd.startswith("set -eu"):
            return self.early if cmd.startswith(fonts.SYSTEM_FONT_MATCH_ENV) else self.active
        if cmd == fonts.CJK_FONT_LIST_COMMAND:
            return self.active
        if not cmd.startswith("set -eu"):
            raise AssertionError(cmd)
        copying = "mktemp" in cmd
        self.mutations.append("backup" if copying else "unlink")
        if not copying and self.fail == "drift":
            self.put(w.SOURCE, b"changed after verification")
        script = cmd.replace("/home", '"$base"/home').replace("/usr/share/fonts", '"$base"/usr/share/fonts').replace("/data/", '"$base"/data/')
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
fc_match() { if [ "$HOME" = /nonexistent ]; then echo -n "$base"EARLY; else echo -n "$base"ACTIVE; fi; }
""".replace("STATVALUE", self.source_stat).replace("EARLY", self.early).replace("ACTIVE", self.active)
        if self.fail == "backup":
            prefix += '\ncp() { printf partial > "$2"; }\n'
        result = subprocess.run([SH, "-c", prefix + script], cwd=self.root,
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(f"script failed: {result.stderr}")
        return result.stdout


@unittest.skipUnless(SH, "POSIX shell or Git Bash required")
class WenKaiRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ssh = Device(Path(tmp.name))
        # Synthetic font at the recorded mapping's v1.511 file size; no customer payload.
        self.original = b"x" * 19639344
        self.ssh.put(w.SOURCE, self.original)
        self.ssh.path("/home").mkdir()
        self.digest = hashlib.sha256(self.original).hexdigest()
        for mocker in (
            patch.dict(w.RELEASES, {len(self.original): self.digest}),
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
        self.assertEqual(self.ssh.mutations, ["backup", "configure", "unlink"])
        self.assertEqual(self.ssh.path(w.BACKUP).read_bytes(), self.original)
        self.assertFalse(self.ssh.path(w.SOURCE).exists())
        self.assertEqual(self.ssh.early, w.MIRROR)
        self.assertIsNone(self.scan())

    def test_existing_verified_home_preference_keeps_wenkai(self):
        self.ssh.put(w.BACKUP, self.original)
        self.ssh.put(fonts.FONTCONFIG_FILE, fonts.fontconfig_override(self.ssh.family, w.BACKUP).encode())
        self.ssh.active = w.BACKUP
        plan = self.scan()
        self.assertEqual(plan.layout, "home")
        w.apply(self.ssh, w.TARGET, plan)
        self.assertEqual(self.ssh.mutations, ["unlink"])
        self.assertFalse(self.ssh.path(w.MIRROR).exists())

    def test_failures_keep_source_and_verified_backup_is_reusable(self):
        for failure in ("backup", "install", "early"):
            with self.subTest(failure=failure):
                self.ssh.fail = failure
                with self.assertRaises(RuntimeError):
                    w.apply(self.ssh, w.TARGET, self.scan())
                self.assertEqual(self.ssh.path(w.SOURCE).read_bytes(), self.original)
                self.assertEqual(self.ssh.path(w.BACKUP).exists(), failure != "backup")
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
        self.assertEqual(self.ssh.path(w.SOURCE).read_bytes(), b"changed after verification")
        self.assertEqual(self.ssh.path(w.BACKUP).read_bytes(), self.original)
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
        self.ssh.put(w.BACKUP, b"somebody else's file")
        with self.assertRaisesRegex(RuntimeError, "未覆盖"):
            self.scan()
        self.assertEqual(self.ssh.path(w.BACKUP).read_bytes(), b"somebody else's file")
        self.ssh.path(w.BACKUP).unlink()
        with patch.object(w.storage, "df_free_bytes", return_value=1024), self.assertRaisesRegex(RuntimeError, "空间不足"):
            w.apply(self.ssh, w.TARGET, self.scan())
        self.assertEqual(self.ssh.mutations, [])

    def test_same_filename_and_size_are_not_proof_and_hardlinks_are_rejected(self):
        self.ssh.source_stat = self.ssh.source_stat.replace(":8362:1:", ":8362:2:")
        with self.assertRaisesRegex(RuntimeError, "硬链接"):
            self.scan()
        self.ssh.source_stat = self.ssh.source_stat.replace(":8362:2:", ":8362:1:")
        with self.ssh.path(w.SOURCE).open("r+b") as stream:
            stream.write(b"y")
        with self.assertRaisesRegex(RuntimeError, "上游字体不一致"):
            self.scan()
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
            self.assertIsInstance(plan.home_only, w.WenKaiPlan)
            # Simulate xochitl still holding the unlinked inode.
            result = recovery.execute(self.ssh, plan)
        self.assertEqual(result.state, "insufficient")
        self.assertTrue(result.cleaned)
        self.assertEqual(result.after_capacity.shortfall, cap.shortfall)
        install.assert_not_called()
