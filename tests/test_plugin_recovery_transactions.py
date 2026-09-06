"""Execute recovery transactions locally; never mount or contact a device."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import _xovi_standalone as shared


SH = shutil.which("sh")
if os.name == "nt":
    # Avoid Windows' bash.exe WSL launcher and exercise the scripts in Git Bash.
    git_sh = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
    if git_sh.is_file():
        SH = str(git_sh)


@unittest.skipUnless(SH, "A POSIX shell (or Git Bash) is required")
class RecoveryTransactionTests(unittest.TestCase):
    TOKEN = "a" * 32

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rmtool-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "sandbox" / shared.SHARED_LAYOUT.remote_base.lstrip("/")
        self.stage = self.base.with_name(self.base.name + ".staging-" + self.TOKEN)
        self.upper = self.root / "sandbox" / shared.SHARED_LAYOUT.dropin_path.lstrip("/")
        # Rewritten absolute suffixes also apply underneath the fake bind mount.
        self.lower_root = self.root / "sandbox/lower"
        self.lower = self.lower_root / "sandbox" / shared.SHARED_LAYOUT.dropin_path.lstrip("/")
        self.backup = self.root / "sandbox/data/rmtool" / (".xovi-dropins-" + self.TOKEN)
        self.old_tree = {
            "package.json": b"old marker\n", "launcher.sh": b"old damaged program\n",
            "startup.pending": b"",
        }
        self.new_tree = {
            "package.json": b"new verified marker\n",
            "launcher.sh": b"new verified program\n",
            "systemd/" + shared.SHARED_LAYOUT.dropin_name: b"new dropin\n",
        }
        for directory, files in ((self.base, self.old_tree), (self.stage, self.new_tree)):
            for name, data in files.items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        for path, data in ((self.upper, b"old upper\n"), (self.lower, b"old lower\n")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.external = self.root / "sandbox/home/root/settings.reader.lua"
        self.external.parent.mkdir(parents=True)
        self.external.write_bytes(b"user settings\n")
        self.sentinel = self.root / "sandbox" / shared.SHARED_RECOVERY_SENTINEL.lstrip("/")
        self.sentinel.write_bytes(b"")

    def tree(self, directory):
        return {str(path.relative_to(directory)).replace("\\", "/"): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def execute(self, failure="", *, enable_dropin=True):
        script = shared.shared_transaction_script(
            shared.SHARED_LAYOUT.remote_base + ".staging-" + self.TOKEN,
            self.TOKEN, (), enable_dropin=enable_dropin, retain_backup=True,
        )
        self.assertNotRegex(script, r"\b(?:reboot|shutdown)\b|systemctl\s+(?:start|restart|try-restart)")
        # The trailing slash preserves concatenated lower-root paths after the
        # absolute device paths become relative sandbox paths.
        script = script.replace("/tmp/rmtool-xovi-rootfs-" + self.TOKEN, "./sandbox/mount/")
        for prefix in ("/data/", "/etc/", "/tmp/", "/home/"):
            script = script.replace(prefix, "./sandbox" + prefix)
        script = script.replace("mount --bind / ", "mount --bind ./sandbox/root ")
        script = script.removeprefix("#!/bin/sh\n")
        self.assertNotRegex(script, r"(?<![\w.])/(?:data|etc|tmp|home|system)(?:/|\b)")
        self.assertNotIn("mount --bind / ", script)
        self.assertNotIn("\0", script)
        self.assertIn(failure, ("", "base-move", "stage-move", "upper-write", "lower-write", "rollback-move"))
        prelude = r'''
FAILURE=__FAILURE__
WINDOWS=__WINDOWS__
FAILED=0
guard_paths() {
    for path do
        case "$path" in
            -*|[0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) continue ;;
            ./sandbox/*) ;;
            *) echo "unsafe test operand: $path" >&2; exit 98 ;;
        esac
        case "$path" in *../*) echo "unsafe traversal" >&2; exit 98 ;; esac
    done
}
inject() {
    if [ "$FAILED" = 0 ] && [ "$FAILURE" = "$1" ]; then
        FAILED=1
        printf '%s\n' "$1" >> ./sandbox/failures
        return 71
    fi
}
mv() {
    guard_paths "$@"
    if [ "$#" = 2 ] && [ "$1" = "$BASE" ]; then inject base-move || return $?; fi
    if [ "$#" = 2 ] && [ "$1" = "$STAGE" ]; then inject stage-move || return $?; fi
    if [ "$FAILURE" = rollback-move ] && [ "$1" = "$BACKUP_DIR/base-0" ]; then
        printf '%s\n' rollback-move >> ./sandbox/failures
        return 72
    fi
    command mv "$@"
}
cp() {
    guard_paths "$@"
    if [ "$1" = "$BASE/systemd/__DROPIN__" ]; then
        case "$2" in
            "$MOUNT_DIR"*)
                inject lower-write || { printf 'partial write' > "$2"; return 71; }
                ;;
            *)
                if [ "$FAILURE" = rollback-move ]; then
                    printf '%s\n' upper-write >> ./sandbox/failures
                    return 71
                fi
                inject upper-write || { printf 'partial write' > "$2"; return 71; }
                ;;
        esac
    fi
    command cp "$@"
}
mkdir() {
    guard_paths "$@"
    # Git Bash cannot reliably apply POSIX modes on Windows temporary folders.
    if [ "$WINDOWS" = 1 ] && [ "$1" = -m ]; then shift 2; fi
    command mkdir "$@"
}
rmdir() { guard_paths "$@"; command rmdir "$@"; }
rm() { guard_paths "$@"; command rm "$@"; }
chmod() {
    guard_paths "$@"
    [ "$WINDOWS" = 1 ] || command chmod "$@"
}
cmp() { guard_paths "$@"; command cmp "$@"; }
sync() { :; }
mount() {
    printf 'mount %s\n' "$*" >> ./sandbox/events
    if [ "$1" = --bind ]; then
        [ "$2" = ./sandbox/root ] && [ "$3" = ./sandbox/mount/ ] || return 98
        command rmdir ./sandbox/mount && command mv ./sandbox/lower ./sandbox/mount
    else
        [ "$1" = -o ] && [ "$3" = ./sandbox/mount/ ] || return 98
        case "$2" in remount,ro|remount,rw) ;; *) return 98 ;; esac
    fi
}
umount() {
    [ "$1" = ./sandbox/mount/ ] || return 98
    printf 'umount\n' >> ./sandbox/events
    command mv ./sandbox/mount ./sandbox/lower && command mkdir ./sandbox/mount
}
systemctl() {
    printf 'systemctl %s\n' "$*" >> ./sandbox/events
    [ "$*" = daemon-reload ]
}
'''.replace("__FAILURE__", "'" + failure + "'").replace(
            "__WINDOWS__", "1" if os.name == "nt" else "0"
        ).replace("__DROPIN__", shared.SHARED_LAYOUT.dropin_name)
        command = prelude + script
        syntax = subprocess.run([SH, "-n"], input=command, cwd=self.root,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        result = subprocess.run([SH], input=command, cwd=self.root,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertNotIn("unsafe", result.stderr)
        self.assertNotEqual(result.returncode, 98, result.stderr)
        self.assertEqual(self.external.read_bytes(), b"user settings\n")
        self.assertEqual(self.sentinel.read_bytes(), b"")
        self.assertFalse(self.stage.exists())
        events = (self.root / "sandbox/events").read_text().splitlines()
        mounted = False
        for event in events:
            if event.startswith("mount --bind"):
                mounted = True
            elif event == "umount":
                mounted = False
            elif event.startswith("systemctl"):
                self.assertEqual(event, "systemctl daemon-reload")
                self.assertFalse(mounted, "daemon-reload must follow unmount")
        self.assertFalse(mounted)
        if failure:
            self.assertTrue((self.root / "sandbox/failures").exists(), result.stderr)
            self.assertIn(failure, (self.root / "sandbox/failures").read_text().splitlines())
        return result

    def assert_restored(self):
        self.assertEqual(self.tree(self.base), self.old_tree)
        self.assertEqual(self.upper.read_bytes(), b"old upper\n")
        self.assertEqual(self.lower.read_bytes(), b"old lower\n")
        self.assertFalse(self.upper.with_name(self.upper.name + ".tmp").exists())
        self.assertFalse(self.lower.with_name(self.lower.name + ".tmp").exists())

    def test_success_retains_original_base_and_both_dropins(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tree(self.base), self.new_tree)
        self.assertEqual(self.tree(self.backup / "base-0"), self.old_tree)
        self.assertEqual((self.backup / "upper-0").read_bytes(), b"old upper\n")
        self.assertEqual((self.backup / "lower-0").read_bytes(), b"old lower\n")
        self.assertEqual(self.upper.read_bytes(), b"new dropin\n")
        self.assertEqual(self.lower.read_bytes(), b"new dropin\n")

    def test_disabled_target_removes_dropins_but_keeps_originals(self):
        result = self.execute(enable_dropin=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.upper.exists())
        self.assertFalse(self.lower.exists())
        self.assertEqual(self.tree(self.backup / "base-0"), self.old_tree)
        self.assertEqual((self.backup / "upper-0").read_bytes(), b"old upper\n")
        self.assertEqual((self.backup / "lower-0").read_bytes(), b"old lower\n")

    def test_base_move_failure_restores_originals(self):
        self.assertNotEqual(self.execute("base-move").returncode, 0)
        self.assert_restored()

    def test_stage_move_failure_restores_originals(self):
        self.assertNotEqual(self.execute("stage-move").returncode, 0)
        self.assert_restored()

    def test_upper_dropin_write_failure_restores_originals(self):
        self.assertNotEqual(self.execute("upper-write").returncode, 0)
        self.assert_restored()

    def test_lower_dropin_write_failure_restores_originals(self):
        self.assertNotEqual(self.execute("lower-write").returncode, 0)
        self.assert_restored()

    def test_colliding_backup_directory_is_not_removed_or_changed(self):
        self.backup.mkdir()
        (self.backup / "prior-evidence").write_bytes(b"keep previous quarantine\n")
        before = self.tree(self.backup)
        self.assertNotEqual(self.execute().returncode, 0)
        self.assert_restored()
        self.assertEqual(self.tree(self.backup), before)

    def test_colliding_backup_file_is_not_removed_or_changed(self):
        self.backup.write_bytes(b"keep previous evidence\n")
        self.assertNotEqual(self.execute().returncode, 0)
        self.assert_restored()
        self.assertEqual(self.backup.read_bytes(), b"keep previous evidence\n")

    def test_failed_base_restore_retains_quarantine_and_reports_path(self):
        result = self.execute("rollback-move")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback incomplete; recovery kept", result.stderr)
        self.assertIn(".xovi-dropins-" + self.TOKEN, result.stderr)
        self.assertEqual(self.tree(self.backup / "base-0"), self.old_tree)
        self.assertEqual((self.backup / "upper-0").read_bytes(), b"old upper\n")
        self.assertEqual((self.backup / "lower-0").read_bytes(), b"old lower\n")
        self.assertEqual(self.upper.read_bytes(), b"old upper\n")
        self.assertEqual(self.lower.read_bytes(), b"old lower\n")


if __name__ == "__main__":
    unittest.main()
