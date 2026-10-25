"""Run generated font transactions in a sandbox with simulated mounts."""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import _rmkit_cn as fonts


SH = shutil.which("sh")
if os.name == "nt":
    git_sh = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
    if git_sh.is_file():
        SH = str(git_sh)


@unittest.skipUnless(SH, "A POSIX shell or Git Bash is required")
class FontRootTransactionTests(unittest.TestCase):
    TOKEN = "e6e1f43c837d"
    PATHS = ("upper/a.ttf", "upper/b.otf", "upper/fonts.conf")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rmtool-font-transaction-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for relative in ("upper", "lower/upper", "stage", "backups"):
            (self.root / relative).mkdir(parents=True)
        for relative in ("upper/a.ttf", "upper/fonts.conf", "lower/upper/a.ttf", "lower/upper/fonts.conf"):
            (self.root / relative).write_bytes(("original:" + relative).encode())
        (self.root / "stage/config").write_bytes(b"new config")
        self.backup = self.root / "backups" / (".font-root-backup-" + self.TOKEN)

    def files(self, relative):
        directory = self.root / relative
        return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}

    def execute(self, mode="rw", failure=""):
        import hashlib

        with patch.object(fonts, "SYSTEM_FONT_ROOT_PATHS", self.PATHS), patch.object(fonts, "BACKUP_DIR", "./backups"):
            script = fonts._root_font_transaction_script(
                self.TOKEN,
                {self.PATHS[0]: None, self.PATHS[1]: None,
                 self.PATHS[2]: ("stage/config", hashlib.sha256(b"new config").hexdigest())},
                None,
            )
        script = script.removeprefix("#!/bin/sh\n").replace(
            "/tmp/rmtool-font-rootfs-" + self.TOKEN, "./mount/"
        ).replace("/proc/mounts", "./proc-mounts").replace(
            "$MOUNT_DIRupper", "${MOUNT_DIR}upper"
        ).replace("fc-cache ", "fc_cache ")
        self.assertNotRegex(script, r"\b(reboot|shutdown)\b|systemctl")
        self.assertNotIn("/proc/", script)
        (self.root / "proc-mounts").write_text(f"root / ext4 {mode},relatime 0 0\n")
        (self.root / "original-mode").write_text(mode)
        prelude = r'''
FAILURE=__FAILURE__
FAILED=0
guard() {
    for path do
        case "$path" in
            -*|[0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) continue ;;
            upper|upper/*|stage/*|./backups/*|./mount|./mount/*) ;;
            *) echo "unsafe operand: $path" >&2; exit 98 ;;
        esac
        case "$path" in *../*) echo "unsafe traversal" >&2; exit 98 ;; esac
    done
}
inject() {
    if [ "$FAILURE" = "$1" ] && [ "$FAILED" -eq 0 ]; then
        FAILED=1
        printf '%s\n' "$1" >> failures
        return 71
    fi
}
mkdir() {
    guard "$@"
    if [ "__WINDOWS__" = 1 ] && [ "$1" = -m ]; then shift 2; fi
    command mkdir "$@"
}
rmdir() { guard "$@"; command rmdir "$@"; }
rm() { guard "$@"; command rm "$@"; }
mv() { guard "$@"; command mv "$@"; }
chmod() { guard "$@"; [ "__WINDOWS__" = 1 ] || command chmod "$@"; }
cp() {
    guard "$@"
    if [ "$1" = stage/config ]; then
        case "$2" in ./mount/*) phase=lower-write ;; *) phase=upper-write ;; esac
        inject "$phase" || { printf 'partial' > "$2"; return 71; }
    fi
    if [ "$FAILURE" = rollback-write ] && [ "$1" = -p ] && [ "$2" = "$BACKUP_DIR/upper-0" ]; then
        printf '%s\n' rollback-write >> failures
        return 72
    fi
    if [ "$FAILURE" = rollback-write ] && [ "$1" = stage/config ]; then
        printf '%s\n' lower-write >> failures
        return 71
    fi
    command cp "$@"
}
sync() { :; }
fc_cache() { inject cache; }
mount() {
    printf 'mount %s\n' "$*" >> events
    if [ "$1" = --bind ]; then
        [ "$2" = / ] && [ "$3" = ./mount/ ] || return 98
        inject bind || return $?
        command rmdir ./mount && command mv ./lower ./mount
    else
        [ "$1" = -o ] || return 98
        case "$3" in /|./mount/) ;; *) return 98 ;; esac
        case "$2" in remount,rw|remount,ro) ;; *) return 98 ;; esac
        [ "$(cat original-mode)" = ro ] || { echo "busy remount" >&2; return 72; }
        if [ "$2" = remount,ro ]; then inject remount-ro || return $?; fi
        printf '%s\n' "$2" > final-mode
    fi
}
umount() {
    [ "$1" = ./mount/ ] || return 98
    printf 'umount\n' >> events
    if [ "$FAILURE" = umount-busy ]; then
        printf '%s\n' umount-busy >> failures
        return 72
    fi
    command mv ./mount ./lower && command mkdir ./mount
}
'''.replace("__FAILURE__", "'" + failure + "'").replace("__WINDOWS__", "1" if os.name == "nt" else "0")
        command = prelude + script
        result = subprocess.run([SH], input=command, cwd=self.root, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=30)
        self.assertNotEqual(result.returncode, 98, result.stderr)
        self.assertNotIn("unsafe", result.stderr)
        return result

    def test_writable_root_never_remounts_and_unmounts_both_views(self):
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.files("upper"), {"fonts.conf": b"new config"})
        self.assertEqual(self.files("lower/upper"), {"fonts.conf": b"new config"})
        events = (self.root / "events").read_text()
        self.assertNotIn("remount", events)
        self.assertEqual(events.count("umount"), 2)
        self.assertFalse(self.backup.exists())
        self.assertFalse((self.root / "mount").exists())

    def test_read_only_root_restores_read_only_mode(self):
        result = self.execute(mode="ro")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "final-mode").read_text().strip(), "remount,ro")

    def test_partial_writes_and_cache_failure_restore_both_views(self):
        before = self.files("upper"), self.files("lower/upper")
        for failure in ("upper-write", "lower-write", "cache"):
            with self.subTest(failure=failure):
                result = self.execute(failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(failure, (self.root / "failures").read_text())
                self.assertEqual((self.files("upper"), self.files("lower/upper")), before)
                self.assertFalse(self.backup.exists())

    def test_failed_bind_does_not_modify_fonts_or_leave_mount_directory(self):
        before = self.files("upper"), self.files("lower/upper")
        result = self.execute(failure="bind")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.files("upper"), self.files("lower/upper")), before)
        self.assertFalse((self.root / "mount").exists())
        self.assertFalse(self.backup.exists())

    def test_read_only_root_write_failure_rolls_back_and_restores_mode(self):
        before = self.files("upper"), self.files("lower/upper")
        result = self.execute(mode="ro", failure="lower-write")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.files("upper"), self.files("lower/upper")), before)
        self.assertEqual((self.root / "final-mode").read_text().strip(), "remount,ro")
        self.assertFalse(self.backup.exists())

    def test_read_only_remount_failure_still_unmounts(self):
        before = self.files("upper"), self.files("lower/upper")
        result = self.execute(mode="ro", failure="remount-ro")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.files("upper"), self.files("lower/upper")), before)
        self.assertIn("umount", (self.root / "events").read_text())
        self.assertEqual((self.root / "final-mode").read_text().strip(), "remount,ro")
        self.assertFalse((self.root / "mount").exists())

    def test_failed_unmount_preserves_recovery_and_never_stacks_mounts(self):
        result = self.execute(failure="umount-busy")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recovery kept", result.stderr)
        self.assertTrue(self.backup.is_dir())
        self.assertEqual((self.root / "events").read_text().count("mount --bind"), 1)

    def test_failed_restore_retains_original_backup_bytes(self):
        result = self.execute(failure="rollback-write")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recovery kept", result.stderr)
        self.assertEqual((self.backup / "upper-0").read_bytes(), b"original:upper/a.ttf")
        before = {p.name: p.read_bytes() for p in self.backup.iterdir()}
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual({p.name: p.read_bytes() for p in self.backup.iterdir()}, before)

    def test_existing_mount_directory_is_not_removed(self):
        (self.root / "mount").mkdir()
        (self.root / "mount/foreign").write_bytes(b"keep")
        result = self.execute()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "mount/foreign").read_bytes(), b"keep")

    def test_expected_clear_validation_precedes_marker_and_delete(self):
        source = "/home/root/.local/share/fonts/selected.ttf"
        mirror = "/data/rmtool/fonts/active-ui-font.ttf"
        expected = fonts.HomeOnlyFontState(
            "/home/root/.local/share/fonts",
            "selected.ttf",
            source,
            mirror,
            "Selected",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            100,
            (
                fonts.HomeOnlyFontMetadata(
                    "/data", stat.S_IFDIR | 0o755, 0, 0, 2
                ),
            ),
            "d" * 64,
        )
        script = fonts._expected_clear_shell(expected, self.TOKEN)
        result = subprocess.run(
            [SH, "-n"], input=script, capture_output=True, encoding="utf-8", timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = f": > {fonts._expected_clear_marker(self.TOKEN)}"
        self.assertLess(script.index("sha256sum"), script.index(marker))
        self.assertLess(script.index("mount_now="), script.index(marker))
        self.assertLess(script.index(marker), script.index("rm -f"))
