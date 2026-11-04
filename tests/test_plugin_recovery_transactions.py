"""Execute recovery transactions locally; never mount or contact a device."""

import os
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import _xovi_standalone as shared
import _plugin_recovery as recovery


SH = shutil.which("sh")
if os.name == "nt":
    # Avoid Windows' bash.exe WSL launcher and exercise the scripts in Git Bash.
    git_sh = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
    if git_sh.is_file():
        SH = str(git_sh)


@unittest.skipUnless(SH, "A POSIX shell (or Git Bash) is required")
class RecoveryTransactionTests(unittest.TestCase):
    TOKEN = "a" * 32
    LAYOUT = shared.SHARED_LAYOUT

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rmtool-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = self.root / "sandbox" / self.LAYOUT.remote_base.lstrip("/")
        self.stage = self.base.with_name(self.base.name + ".staging-" + self.TOKEN)
        self.upper = self.root / "sandbox" / shared.SHARED_LAYOUT.dropin_path.lstrip("/")
        # Rewritten absolute suffixes also apply underneath the fake bind mount.
        self.lower_root = self.root / "sandbox/lower"
        self.lower = self.lower_root / "sandbox" / shared.SHARED_LAYOUT.dropin_path.lstrip("/")
        self.backup = self.base.parent / (".xovi-dropins-" + self.TOKEN)
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
        self.external.parent.mkdir(parents=True, exist_ok=True)
        self.external.write_bytes(b"user settings\n")
        self.sentinel = self.root / "sandbox" / shared.SHARED_RECOVERY_SENTINEL.lstrip("/")
        self.sentinel.parent.mkdir(parents=True, exist_ok=True)
        self.sentinel.write_bytes(b"")

    def tree(self, directory):
        return {str(path.relative_to(directory)).replace("\\", "/"): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def execute(self, failure="", *, enable_dropin=True, root_mode="ro", legacy_layouts=(), controls_only=False,
                stop_units=(), pinyin_stage=None, runtime_pid=False):
        script = shared.shared_transaction_script(
            self.LAYOUT.remote_base + ".staging-" + self.TOKEN,
            self.TOKEN, legacy_layouts, enable_dropin=enable_dropin, retain_backup=True,
            layout=self.LAYOUT,
            controls_only=controls_only,
            stop_units=stop_units,
            pinyin_stage=pinyin_stage,
        )
        self.assertNotRegex(script, r"\b(?:reboot|shutdown)\b|systemctl\s+(?:start|restart|try-restart)\s+xochitl")
        # The trailing slash preserves concatenated lower-root paths after the
        # absolute device paths become relative sandbox paths.
        script = script.replace("/tmp/rmtool-xovi-rootfs-" + self.TOKEN, "./sandbox/mount/")
        for prefix in ("/data/", "/etc/", "/tmp/", "/home/"):
            script = script.replace(prefix, "./sandbox" + prefix)
        script = script.replace("mount --bind / ", "mount --bind ./sandbox/root ")
        script = script.replace("/proc/mounts", "./sandbox/proc-mounts")
        if runtime_pid:
            script = script.replace("/proc/", "./sandbox/proc/").replace("/run", "./sandbox/run")
            (self.root / "sandbox/run").mkdir()
            boot = self.root / "sandbox/proc/sys/kernel/random/boot_id"
            boot.parent.mkdir(parents=True)
            boot.write_text("test-boot\n")
            process = self.root / "sandbox/proc/778/stat"
            process.parent.mkdir()
            process.write_text(" ".join(["0"] * 21 + ["1000"]) + "\n")
        script = script.removeprefix("#!/bin/sh\n")
        self.assertNotRegex(script, r"(?<![\w.])/(?:data|etc|tmp|home|system)(?:/|\b)")
        self.assertNotIn("mount --bind / ", script)
        self.assertNotIn("\0", script)
        self.assertIn(root_mode, ("ro", "rw"))
        (self.root / "sandbox/proc-mounts").write_text(
            f"rootfs / ext4 {root_mode},relatime 0 0\n", encoding="ascii"
        )
        self.assertIn(failure, ("", "base-move", "stage-move", "upper-write", "lower-write", "rollback-move",
                                "control-write", "control-rollback", "daemon-reload", "hangup"))
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
    if [ "$#" = 2 ] && [ "$1" = "$STAGE" ] && [ "$FAILURE" = hangup ]; then
        # The SSH session drops after the live base was moved to the backup.
        printf '%s\n' hangup >> ./sandbox/failures
        kill -HUP $$
    fi
    if [ "$FAILURE" = rollback-move ] && [ "$1" = "$BACKUP_DIR/base-0" ]; then
        printf '%s\n' rollback-move >> ./sandbox/failures
        return 72
    fi
    command mv "$@"
}
cp() {
    guard_paths "$@"
    if [ "$1" = "$STAGE/launcher.sh" ]; then
        inject control-write || { printf 'partial write' > "$2"; return 71; }
    fi
    if [ "$FAILURE" = control-rollback ] && { [ "$1" = "$BACKUP_DIR/control-1" ] || [ "$2" = "$BACKUP_DIR/control-1" ]; }; then
        printf '%s\n' control-rollback >> ./sandbox/failures
        return 72
    fi
    if [ "$1" = "$BASE/systemd/__DROPIN__" ]; then
        case "$2" in
            "$MOUNT_DIR"*)
                inject lower-write || { printf 'partial write' > "$2"; return 71; }
                ;;
            *)
                if [ "$FAILURE" = rollback-move ] || [ "$FAILURE" = control-rollback ]; then
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
    case "$1" in
        show) if [ "$2" = xochitl ] && [ '__RUNTIME_PID__' = 1 ]; then echo 778; else cat ./sandbox/unit-state; fi ;;
        stop) printf 'inactive\n' > ./sandbox/unit-state ;;
        start) printf 'active\n' > ./sandbox/unit-state ;;
        daemon-reload) inject daemon-reload ;;
        *) return 98 ;;
    esac
}
'''.replace("__FAILURE__", "'" + failure + "'").replace(
            "__WINDOWS__", "1" if os.name == "nt" else "0"
        ).replace("__DROPIN__", shared.SHARED_LAYOUT.dropin_name)
        prelude = prelude.replace("__RUNTIME_PID__", "1" if runtime_pid else "0")
        if runtime_pid:
            # Root ownership and 0600 are modeled; content and atomic moves use
            # real files even on Windows, where POSIX metadata is unavailable.
            prelude += '''
stat() {
    case "$2" in '%u:%g') echo 0:0 ;; '%a:%u:%g:%h') echo 600:0:0:1 ;; *) command stat "$@" ;; esac
}
'''
        command = prelude + script
        (self.root / "sandbox/unit-state").write_text("active\n", encoding="ascii")
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
                if event == "systemctl daemon-reload":
                    self.assertFalse(mounted, "daemon-reload must follow unmount")
                elif event == "systemctl show xochitl -p MainPID --value":
                    pass
                else:
                    self.assertIn("rmtool-pinyin-input.service", event)
                    self.assertFalse(mounted, "service changes must follow unmount")
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
        events = (self.root / "sandbox/events").read_text().splitlines()
        self.assertEqual(events.count("mount -o remount,rw ./sandbox/mount/"), 2)
        self.assertEqual(events.count("mount -o remount,ro ./sandbox/mount/"), 2)
        self.assertEqual(self.tree(self.base), self.new_tree)
        self.assertEqual(self.tree(self.backup / "base-0"), self.old_tree)
        self.assertEqual((self.backup / "upper-0").read_bytes(), b"old upper\n")
        self.assertEqual((self.backup / "lower-0").read_bytes(), b"old lower\n")
        self.assertEqual(self.upper.read_bytes(), b"new dropin\n")
        self.assertEqual(self.lower.read_bytes(), b"new dropin\n")

    def test_pinyin_stop_commits_with_shared_state(self):
        result = self.execute(stop_units=("rmtool-pinyin-input.service",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "sandbox/unit-state").read_text(), "inactive\n")

    def test_pinyin_stop_rolls_back_with_shared_state(self):
        result = self.execute("stage-move", stop_units=("rmtool-pinyin-input.service",))
        self.assertNotEqual(result.returncode, 0)
        self.assert_restored()
        self.assertEqual((self.root / "sandbox/unit-state").read_text(), "active\n")

    def prepare_controls(self):
        self.old_tree["systemd/" + self.LAYOUT.dropin_name] = b"old control dropin\n"
        self.old_tree["extensions.d/payload.so"] = b"verified payload\n"
        for name, data in self.old_tree.items():
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def test_control_only_repair_keeps_payload_and_backs_up_old_pending(self):
        self.prepare_controls()
        result = self.execute(controls_only=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tree(self.base), {**self.new_tree, "extensions.d/payload.so": b"verified payload\n"})
        self.assertEqual((self.backup / "control-0").read_bytes(), self.old_tree["package.json"])
        self.assertEqual((self.backup / "control-3").read_bytes(), b"")
        self.assertFalse((self.backup / "base-0").exists())

    def test_control_only_write_failure_restores_original_controls_and_pending(self):
        self.prepare_controls()
        self.assertNotEqual(self.execute("lower-write", controls_only=True).returncode, 0)
        self.assert_restored()

    def test_control_only_partial_control_write_rolls_back(self):
        self.prepare_controls()
        self.assertNotEqual(self.execute("control-write", controls_only=True).returncode, 0)
        self.assert_restored()

    def test_control_only_reload_failure_rolls_back(self):
        self.prepare_controls()
        self.assertNotEqual(self.execute("daemon-reload", controls_only=True).returncode, 0)
        self.assert_restored()

    def test_control_only_failed_rollback_keeps_original_configuration_backup(self):
        self.prepare_controls()
        result = self.execute("control-rollback", controls_only=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback incomplete; recovery kept", result.stderr)
        self.assertEqual((self.backup / "control-1").read_bytes(), self.old_tree["launcher.sh"])
        self.assertEqual((self.base / "extensions.d/payload.so").read_bytes(), b"verified payload\n")

    def test_control_only_refuses_migration_or_missing_rollback(self):
        for kwargs in ({"remove_base": True}, {"retain_backup": False}, {"enable_dropin": False}):
            options = dict(enable_dropin=True, retain_backup=True, controls_only=True)
            options.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                shared.shared_transaction_script("/unused", self.TOKEN, (), **options)

    def test_already_writable_root_skips_busy_remounts(self):
        result = self.execute(root_mode="rw")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = (self.root / "sandbox/events").read_text().splitlines()
        self.assertEqual(sum(event.startswith("mount --bind") for event in events), 2)
        self.assertFalse(any("remount" in event for event in events))
        self.assertEqual(self.lower.read_bytes(), b"new dropin\n")

    def test_runtime_moves_record_exact_process_and_transaction_paths(self):
        result = self.execute(runtime_pid=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        proof = self.root / "sandbox/run/rmtool-xovi-runtime-moves"
        self.assertEqual(proof.read_text().splitlines(), [
            "test-boot:778:1000|./sandbox" + self.LAYOUT.remote_base +
            "|./sandbox" + self.LAYOUT.remote_base.rsplit("/", 1)[0] + "/.xovi-dropins-" + self.TOKEN + "/base-0"
        ])

    def test_pinyin_payload_and_service_rollback_are_one_transaction(self):
        external = self.root / "sandbox/home/root/.local/share/rmtool/pinyin-input"
        stage = external.with_name(external.name + ".staging-" + self.TOKEN)
        external.mkdir(parents=True)
        stage.mkdir()
        (external / "ime-server").write_bytes(b"old service")
        (stage / "ime-server").write_bytes(b"new service")
        result = self.execute("stage-move", stop_units=("rmtool-pinyin-input.service",),
                              pinyin_stage="/home/root/.local/share/rmtool/pinyin-input.staging-" + self.TOKEN)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual((external / "ime-server").read_bytes(), b"old service")
        self.assertEqual((self.root / "sandbox/unit-state").read_text().strip(), "active")
        self.assert_restored()

    def test_pinyin_payload_and_shared_marker_commit_together(self):
        external = self.root / "sandbox/home/root/.local/share/rmtool/pinyin-input"
        stage = external.with_name(external.name + ".staging-" + self.TOKEN)
        external.mkdir(parents=True)
        stage.mkdir()
        (external / "ime-server").write_bytes(b"old service")
        (stage / "ime-server").write_bytes(b"new service")
        result = self.execute(stop_units=("rmtool-pinyin-input.service",),
                              pinyin_stage="/home/root/.local/share/rmtool/pinyin-input.staging-" + self.TOKEN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((external / "ime-server").read_bytes(), b"new service")
        self.assertEqual(
            (external.with_name(external.name + ".backup-" + self.TOKEN) / "ime-server").read_bytes(),
            b"old service",
        )
        self.assertEqual(self.tree(self.base), self.new_tree)
        self.assertEqual((self.root / "sandbox/unit-state").read_text().strip(), "inactive")

    def test_already_writable_root_rollback_skips_busy_remounts(self):
        self.assertNotEqual(self.execute("lower-write", root_mode="rw").returncode, 0)
        self.assert_restored()
        events = (self.root / "sandbox/events").read_text().splitlines()
        self.assertFalse(any("remount" in event for event in events))

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

    def test_hangup_mid_transaction_restores_originals(self):
        result = self.execute("hangup")
        self.assertEqual(result.returncode, 129, result.stderr)
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


class RM1RecoveryTransactionTests(RecoveryTransactionTests):
    LAYOUT = shared.LEGACY_SHARED_LAYOUT

    def test_data_to_home_migration_retains_source_backup(self):
        source = self.root / "sandbox" / shared.SHARED_LAYOUT.remote_base.lstrip("/")
        self.base.rename(source)
        result = self.execute(legacy_layouts=(shared.SHARED_LAYOUT,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.tree(self.base), self.new_tree)
        self.assertFalse(source.exists())
        self.assertEqual(self.tree(source.with_name(source.name + ".backup-" + self.TOKEN)), self.old_tree)

    def test_data_to_home_migration_failure_restores_source(self):
        source = self.root / "sandbox" / shared.SHARED_LAYOUT.remote_base.lstrip("/")
        self.base.rename(source)
        result = self.execute("lower-write", legacy_layouts=(shared.SHARED_LAYOUT,))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.base.exists())
        self.assertEqual(self.tree(source), self.old_tree)
        self.assertEqual(self.upper.read_bytes(), b"old upper\n")
        self.assertEqual(self.lower.read_bytes(), b"old lower\n")


@unittest.skipUnless(SH, "A POSIX shell (or Git Bash) is required")
class IncompleteCleanupTransactionTests(unittest.TestCase):
    def run_cleanup(self, *, same_inode=False, root_mode="rw", failure="", tampered=False, missing_parent=False):
        temporary = tempfile.TemporaryDirectory(prefix="rmtool-incomplete-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        device = root / "sandbox"
        upper = device / "etc/systemd/system/xochitl.service.d/92-rmtool-xovi-standalone.conf"
        lower = device / "mount/etc/systemd/system/xochitl.service.d/92-rmtool-xovi-standalone.conf"
        for path in (upper, lower):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"known dropin\n")
            path.chmod(0o644)
        (device / "data").mkdir()
        if not missing_parent:
            (device / "data/rmtool").mkdir()
        (device / "tmp").mkdir()
        (device / "mounts").write_text(f"rootfs / ext4 {root_mode} 0 0\n")
        digest = hashlib.sha256(b"known dropin\n").hexdigest()
        snapshot = (shared.SHARED_LAYOUT.dropin_path, 0o100644, 13, digest)
        plan = recovery._CleanupPlan((), snapshot, snapshot, (), ())
        script = recovery._incomplete_cleanup_script(plan, "b" * 32)
        script = script.replace("/proc/mounts", "./sandbox/mounts")
        script = script.replace("/tmp/rmtool-xovi-incomplete-root-" + "b" * 32, "./sandbox/mount")
        script = script.replace("$MOUNT_DIR/etc/", "./sandbox/mount/etc/")
        for prefix in ("/data/", "/etc/", "/tmp/"):
            script = script.replace(prefix, "./sandbox" + prefix)
        for parent in ("/data", "/etc", "/tmp", "/"):
            script = script.replace(f"[ ! -L {parent} ]", f"[ ! -L ./sandbox{parent.rstrip('/')} ]")
        # Repair the lower path suffix after rewriting its /etc component.
        script = script.replace("./sandbox/mount./sandbox/etc/", "./sandbox/mount/etc/")
        script = script.replace("mount --bind / ", "mount --bind ./sandbox/root ")
        script = script.removeprefix("#!/bin/sh\n")
        if same_inode:
            # A bind mount with no /etc overlay resolves both names to one file.
            script = script.replace("./sandbox/mount/etc/", "./sandbox/etc/")
        self.assertNotRegex(script, r"\[ ! -L /")
        self.assertNotRegex(script, r"(?<![\w.])/(?:data|etc|tmp)(?:/|\b)")
        for parent in ("", "/data", "/etc"):
            self.assertIn(f"[ ! -L ./sandbox{parent} ]", script)
        if tampered:
            upper.write_bytes(b"tampered bytes\n")
        prelude = r'''
FAILED=0
guard() {
    for arg; do
        case "$arg" in -*|[0-9][0-9][0-9]|[0-9][0-9][0-9][0-9]|./sandbox/*) ;;
        *) echo "unsafe test path: $arg" >&2; exit 98 ;; esac
        case "$arg" in *../*) exit 98 ;; esac
    done
}
stat() {
    if [ "$1" = -c ] && [ "$2" = '%a:%u:%g:%s' ]; then
        [ -e "$3" ] || return 1
        printf '644:0:0:%s\n' "$(wc -c < "$3" | tr -d '[:space:]')"
    else command stat "$@"; fi
}
mkdir() { guard "$@"; if [ "$1" = -m ]; then shift 2; fi; command mkdir "$@"; }
rmdir() { guard "$@"; command rmdir "$@"; }
cp() { guard "$@"; command cp "$@"; }
mv() { guard "$@"; command mv "$@"; }
rm() {
    guard "$@"
    command rm "$@" || return $?
    if [ "$FAILED" -eq 0 ] && [ '__FAILURE__' = removed-upper ] && [ "$*" = "-f $DROPIN" ]; then
        FAILED=1; return 71
    fi
}
sync() { :; }
mount() {
    printf 'mount %s\n' "$*" >> ./sandbox/events
    case "$*" in '--bind ./sandbox/root ./sandbox/mount'|'-o remount,rw ./sandbox/mount'|'-o remount,ro ./sandbox/mount') ;;
    *) return 98 ;; esac
}
umount() {
    [ "$1" = ./sandbox/mount ] || return 98
    printf 'umount\n' >> ./sandbox/events
    [ '__FAILURE__' != unmount-busy ] || return 73
}
systemctl() { [ "$*" = daemon-reload ] || return 98; echo daemon-reload >> ./sandbox/events; }
'''.replace("__FAILURE__", failure)
        # The fake bind root already contains lower files, unlike the empty
        # mountpoint on the tablet. Leave its directory in place after unmount.
        prelude += '\nrmdir() { guard "$@"; [ "$1" = ./sandbox/mount ] || command rmdir "$@"; }\n'
        script = script.replace('mkdir -m 0700 "$MOUNT_DIR"', 'mkdir -p "$MOUNT_DIR"')
        result = subprocess.run([SH], input=prelude + script, cwd=root,
                                capture_output=True, encoding="utf-8", timeout=30)
        self.assertNotEqual(result.returncode, 98, result.stderr)
        self.assertNotIn("unsafe", result.stderr)
        return result, upper, lower, device

    def test_both_layouts_clean_without_losing_original_root_mode(self):
        for same_inode in (True, False):
            for mode in ("rw", "ro"):
                with self.subTest(same_inode=same_inode, mode=mode):
                    result, upper, lower, device = self.run_cleanup(same_inode=same_inode, root_mode=mode)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(upper.exists())
                    if not same_inode:
                        self.assertFalse(lower.exists())
                    events = (device / "events").read_text()
                    self.assertEqual("remount" in events, mode == "ro")

    def test_both_layouts_restore_dropins_on_failure(self):
        for same_inode in (True, False):
            with self.subTest(same_inode=same_inode):
                result, upper, lower, _device = self.run_cleanup(same_inode=same_inode, failure="removed-upper")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(upper.read_bytes(), b"known dropin\n")
                self.assertEqual(lower.read_bytes(), b"known dropin\n")

    def test_changed_dropin_is_not_deleted(self):
        result, upper, lower, _device = self.run_cleanup(tampered=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(upper.read_bytes(), b"tampered bytes\n")
        self.assertEqual(lower.read_bytes(), b"known dropin\n")

    def test_dropin_only_cleanup_can_create_missing_backup_parent(self):
        result, upper, lower, _device = self.run_cleanup(missing_parent=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(upper.exists())
        self.assertFalse(lower.exists())

    def test_busy_unmount_keeps_persistent_backup_and_never_rebinds(self):
        result, _upper, _lower, device = self.run_cleanup(failure="unmount-busy")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rollback incomplete; recovery kept", result.stderr)
        backups = list((device / "data/rmtool").glob("*.incomplete-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "dropin-lower").read_bytes(), b"known dropin\n")
        events = (device / "events").read_text()
        self.assertEqual(events.count("mount --bind"), 1)
        self.assertNotIn("daemon-reload", events)


if __name__ == "__main__":
    unittest.main()
