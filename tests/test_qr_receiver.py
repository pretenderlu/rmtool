"""Execute the RM2 /home receiver transaction against private sandbox files."""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _qr_receiver as home
import _xovi_standalone as shared


SHELL = shutil.which("sh")
if os.name == "nt":
    bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
    SHELL = str(bash) if bash.is_file() else None


@unittest.skipUnless(SHELL, "requires a POSIX shell")
class ReceiverTransactions(unittest.TestCase):
    token = "a" * 32

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "home").mkdir()
        (self.root / "mountinfo").write_text("0 1 2 3 ./home rw\n")
        self.source = self.root / "source"
        self.source.write_bytes(b"pin this exact binary")
        self.r = home.Receiver(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source.stat().st_size)
        self.live = self.root / self.r.path.lstrip("/")
        self.free = self.enterContext(patch.object(home.storage, "df_free_bytes", return_value=30 * 1024**2))

    def transfer_file(self, local, remote):
        shutil.copyfile(local, self.root / remote.lstrip("/"))

    def exec_checked(self, command):
        # Only mount information and root-owned POSIX modes are simulated.
        # Link counts, sizes, SHA-256, file operations and shell exits are real.
        prelude = """
stat() {
    case "$2" in
        '%u:%g') echo 0:0 ;;
        '%a') echo 755 ;;
        '%d') if [ "$3" = / ]; then echo 1; else echo 2; fi ;;
        '%a:%u:%g') echo 700:0:0 ;;
        '%f:%u:%g:%s:%h') LC_ALL=C ls -ldn "$3" | awk '{print "81ed:0:0:" $5 ":" $2}' ;;
        *) exit 97 ;;
    esac
}
command -v sha256sum >/dev/null || sha256sum() { shasum -a 256 "$@"; }
"""
        if os.name == "nt":
            prelude += 'mkdir() { [ "$1" != -m ] || shift 2; command mkdir "$@"; }\n'
        command = command.replace("/home", "./home").replace("/proc/self/mountinfo", "./mountinfo")
        result = subprocess.run([SHELL], input=prelude + command, encoding="utf-8", errors="replace", cwd=self.root,
                                capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(f"device command failed ({result.returncode}): {result.stderr}")
        return result.stdout

    def test_staged_receiver_is_published_only_at_commit_and_removed_on_rollback(self):
        with home.prepare(self, self.r, self.source, self.token) as prepared:
            self.assertFalse(self.live.exists())
            self.exec_checked("set -eu\nQR_CREATED=0\nROLLBACK_OK=1\n" + prepared.commit)
            self.assertTrue(self.r.present(self))
            self.exec_checked("set -eu\nQR_CREATED=1\nROLLBACK_OK=1\n" + prepared.undo)
            self.assertFalse(self.live.exists())
        self.assertEqual(list(self.live.parent.iterdir()), [])

    def test_existing_receiver_kept_on_rollback_and_uninstall_is_reversible(self):
        self.live.parent.mkdir(parents=True)
        self.live.write_bytes(self.source.read_bytes())
        with patch.object(self, "transfer_file", side_effect=AssertionError("duplicate upload")):
            with home.prepare(self, self.r, self.source, self.token) as prepared:
                self.exec_checked("set -eu\nQR_CREATED=0\nROLLBACK_OK=1\n" + prepared.commit + "\n" + prepared.undo)
        self.assertTrue(self.r.present(self))
        self.free.assert_not_called()
        retired = home.retire(self, self.r, self.token)
        self.exec_checked("set -eu\n" + retired.commit)
        self.assertFalse(self.live.exists())
        self.exec_checked("set -eu\nQR_RETIRING=1\nROLLBACK_OK=1\n" + retired.undo)
        self.assertTrue(self.r.present(self))
        self.exec_checked("set -eu\n" + retired.commit + "\n" + retired.finish)
        self.assertEqual(list(self.live.parent.iterdir()), [])

    def test_corruption_low_space_and_unmounted_home_fail_closed(self):
        self.live.parent.mkdir(parents=True)
        self.live.write_bytes(b"x" * self.r.size)
        with self.assertRaises(RuntimeError):
            home.retire(self, self.r, self.token)
        self.assertEqual(self.live.read_bytes(), b"x" * self.r.size)
        self.live.unlink()
        self.free.return_value = 0
        with self.assertRaisesRegex(RuntimeError, "/home 空间不足"):
            with home.prepare(self, self.r, self.source, self.token):
                self.fail("cannot deploy on full home")
        (self.root / "mountinfo").write_text("")
        with self.assertRaises(RuntimeError):
            self.r.present(self)
        self.assertEqual(list(self.live.parent.iterdir()), [])

    def test_partial_transfer_is_not_published(self):
        def fail(local, remote):
            (self.root / remote.lstrip("/")).write_bytes(b"partial")
            raise OSError("interrupted")
        with patch.object(self, "transfer_file", side_effect=fail), self.assertRaises(OSError):
            with home.prepare(self, self.r, self.source, self.token):
                self.fail("partial receiver accepted")
        self.assertEqual(list(self.live.parent.iterdir()), [])

    def test_home_switch_is_part_of_shared_rollback(self):
        with home.prepare(self, self.r, self.source, self.token) as prepared:
            script = shared.shared_transaction_script("/tmp/stage", self.token, (), enable_dropin=True,
                                                      qr_receiver=prepared)
        self.assertLess(script.index("trap rollback EXIT"), script.index(prepared.commit))
        self.assertLess(script.index(prepared.commit), script.index('mv "$STAGE" "$BASE"'))
        self.assertIn(prepared.undo, script.split("trap rollback EXIT")[0])
