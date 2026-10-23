import io
import posixpath
import stat
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import _rmkit_cn as fonts


ROOT = "1 0 179:2 / / rw,relatime - ext4 /dev/root rw"
TOKEN = "e6e1f43c837d"
BACKUP = f"{fonts.BACKUP_DIR}/.font-root-backup-{TOKEN}"
MOUNT = f"/tmp/rmtool-font-rootfs-{TOKEN}"


class RecoverySSH:
    def __init__(self):
        self.nodes = {}
        path = BACKUP
        while path != "/":
            self.nodes[path] = SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o755, st_uid=0, st_gid=0, st_size=0
            )
            path = posixpath.dirname(path)
        self.files = {}
        self.mounts = [ROOT]
        self.commands = []
        self.renames = []
        self.locked = False
        self.busy = False
        self.rename_failure = False

    def add_file(self, name, data=b"original snapshot"):
        path = f"{BACKUP}/{name}"
        self.files[path] = data
        self.nodes[path] = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0, st_size=len(data)
        )
        return path

    def add_mount(self, mount_id=2, target=MOUNT, device="179:2"):
        self.mounts.append(f"{mount_id} 1 {device} / {target} rw,relatime - ext4 /dev/root rw")

    @contextmanager
    def operation_session(self):
        assert not self.locked
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    @contextmanager
    def sftp_session(self):
        yield self

    def lstat(self, path):
        if path not in self.nodes:
            raise FileNotFoundError(path)
        return self.nodes[path]

    def listdir_attr(self, path):
        return [SimpleNamespace(filename=posixpath.basename(p), **vars(attrs))
                for p, attrs in self.nodes.items() if posixpath.dirname(p) == path]

    def open(self, path, mode):
        assert mode == "rb"
        return io.BytesIO(self.files[path])

    def rename(self, source, target):
        assert self.locked
        if self.rename_failure:
            raise OSError("archive failed")
        self.renames.append((source, target))
        for path in list(self.nodes):
            if path == source or path.startswith(source + "/"):
                new_path = target + path[len(source):]
                self.nodes[new_path] = self.nodes.pop(path)
                if path in self.files:
                    self.files[new_path] = self.files.pop(path)

    def exec_checked(self, command):
        self.commands.append(command)
        if command == "cat /proc/self/mountinfo":
            return "\n".join(self.mounts)
        assert self.locked and command == f"umount {MOUNT}", command
        if self.busy:
            raise RuntimeError("mount point is busy")
        for i in range(len(self.mounts) - 1, -1, -1):
            if self.mounts[i].split()[4] == MOUNT:
                self.mounts.pop(i)
                return ""
        raise AssertionError("no owned mount")


class FontRecoveryTests(unittest.TestCase):
    def test_legacy_empty_directory_is_repairable_without_guessing_missing_files(self):
        ssh = RecoverySSH()
        status = fonts.get_font_recovery_status(ssh)
        self.assertEqual(status.state, "ready")
        self.assertEqual(status.directories, (BACKUP,))
        self.assertEqual(status.mounts, ())
        self.assertTrue(status.fingerprint)

    def test_clean_device_never_probes_mounts(self):
        ssh = RecoverySSH()
        del ssh.nodes[BACKUP]
        self.assertEqual(fonts.get_font_recovery_status(ssh), fonts.FontRecoveryStatus())
        self.assertEqual(ssh.commands, [])

    def test_identical_duplicate_roots_allow_owned_mount_recovery(self):
        ssh = RecoverySSH()
        ssh.mounts.append(ROOT.replace("1 0 ", "4 1 ", 1))
        ssh.add_mount()
        status = fonts.get_font_recovery_status(ssh)
        self.assertEqual(status.state, "ready")
        with mock.patch.object(fonts, "set_active_user_font"):
            fonts.repair_user_font(ssh, "/home/fonts", "test.ttf", False, status)
        self.assertEqual(fonts.get_font_recovery_status(ssh).state, "none")
        self.assertEqual(ssh.commands.count(f"umount {MOUNT}"), 1)

    def test_conflicting_root_records_request_restart_without_writes(self):
        for root in (ROOT.replace("179:2", "179:3"), ROOT.replace("rw,relatime", "ro,relatime")):
            with self.subTest(root=root):
                ssh = RecoverySSH()
                ssh.mounts.append(root)
                status = fonts.get_font_recovery_status(ssh)
                self.assertEqual(status.state, "blocked")
                self.assertIn("手动重启", status.detail)
                self.assertEqual(ssh.renames, [])

    def test_hash_covers_original_bytes_and_detects_same_size_replacement(self):
        ssh = RecoverySSH()
        path = ssh.add_file("upper-0", b"original")
        before = fonts.get_font_recovery_status(ssh)
        ssh.files[path] = b"modified"
        after = fonts.get_font_recovery_status(ssh)
        self.assertNotEqual(before.fingerprint, after.fingerprint)

    def test_untrusted_records_and_parent_paths_are_blocked(self):
        for case in ("parent-symlink", "directory-owner", "file-symlink", "file-mode",
                     "unknown-file", "too-large", "short-read", "invalid-token"):
            with self.subTest(case=case):
                ssh = RecoverySSH()
                path = ssh.add_file("upper-0")
                if case == "parent-symlink":
                    ssh.nodes["/home/root/.local"].st_mode = stat.S_IFLNK | 0o777
                elif case == "directory-owner":
                    ssh.nodes[BACKUP].st_uid = 1000
                elif case == "file-symlink":
                    ssh.nodes[path].st_mode = stat.S_IFLNK | 0o777
                elif case == "file-mode":
                    ssh.nodes[path].st_mode |= 0o002
                elif case == "unknown-file":
                    ssh.add_file("apply.sh")
                elif case == "too-large":
                    ssh.nodes[path].st_size = 64 * 1024 * 1024 + 1
                elif case == "short-read":
                    ssh.nodes[path].st_size += 1
                else:
                    ssh.nodes[BACKUP + "-foreign"] = ssh.nodes.pop(BACKUP)
                self.assertEqual(fonts.get_font_recovery_status(ssh).state, "blocked")
                self.assertEqual(ssh.renames, [])

    def test_unknown_foreign_or_child_mount_is_never_detached(self):
        for target, device in ((MOUNT + "bad", "179:2"), (MOUNT, "7:0"),
                               (MOUNT + "/child", "179:2")):
            with self.subTest(target=target, device=device):
                ssh = RecoverySSH()
                ssh.add_mount(target=target, device=device)
                status = fonts.get_font_recovery_status(ssh)
                self.assertEqual(status.state, "blocked")
                with mock.patch.object(fonts, "set_active_user_font") as apply:
                    with self.assertRaisesRegex(RuntimeError, "状态已变化"):
                        fonts.repair_user_font(ssh, "/home/fonts", "test.ttf", False, status)
                apply.assert_not_called()
                self.assertFalse(any(c.startswith("umount") for c in ssh.commands))

    def test_changed_snapshot_aborts_before_mutation(self):
        ssh = RecoverySSH()
        path = ssh.add_file("upper-0")
        status = fonts.get_font_recovery_status(ssh)
        ssh.files[path] = b"changed"
        with mock.patch.object(fonts, "set_active_user_font") as apply:
            with self.assertRaisesRegex(RuntimeError, "状态已变化"):
                fonts.repair_user_font(ssh, "/home/fonts", "test.ttf", False, status)
        apply.assert_not_called()
        self.assertEqual(ssh.renames, [])

    def test_success_detaches_stacked_owned_mounts_reapplies_choice_and_archives_bytes(self):
        ssh = RecoverySSH()
        ssh.add_file("upper-0")
        ssh.add_mount(2)
        ssh.add_mount(3)
        status = fonts.get_font_recovery_status(ssh)
        font = fonts.UserFont("test.ttf", "Family", "/home/fonts/test.ttf", True)

        def apply(*args):
            self.assertTrue(ssh.locked)
            self.assertEqual(ssh.mounts, [ROOT])
            self.assertEqual(args, (ssh, "/home/fonts", "test.ttf", False))
            return font

        with mock.patch.object(fonts, "set_active_user_font", side_effect=apply):
            result = fonts.repair_user_font(ssh, "/home/fonts", "test.ttf", False, status)
        self.assertEqual(result, font)
        self.assertEqual(ssh.commands.count(f"umount {MOUNT}"), 2)
        self.assertEqual(ssh.files[ssh.renames[0][1] + "/upper-0"], b"original snapshot")
        self.assertEqual(fonts.get_font_recovery_status(ssh).state, "none")

    def test_busy_mount_stops_before_font_changes_and_keeps_evidence(self):
        ssh = RecoverySSH()
        ssh.add_mount()
        ssh.busy = True
        status = fonts.get_font_recovery_status(ssh)
        with mock.patch.object(fonts, "set_active_user_font") as apply:
            with self.assertRaisesRegex(RuntimeError, "手动重启.*mount point is busy"):
                fonts.repair_user_font(ssh, "/home/fonts", "test.ttf", False, status)
        apply.assert_not_called()
        self.assertIn(BACKUP, ssh.nodes)
        self.assertFalse(ssh.locked)

    def test_changed_records_after_detach_or_reapply_never_get_archived(self):
        for phase in ("detach", "reapply"):
            with self.subTest(phase=phase):
                ssh = RecoverySSH()
                path = ssh.add_file("upper-0", b"original")
                if phase == "detach":
                    ssh.add_mount()
                status = fonts.get_font_recovery_status(ssh)
                execute = ssh.exec_checked

                def detach(command):
                    result = execute(command)
                    if command.startswith("umount"):
                        ssh.files[path] = b"modified"
                    return result

                def apply(*_args):
                    ssh.files[path] = b"modified"

                with mock.patch.object(ssh, "exec_checked", side_effect=detach), mock.patch.object(
                    fonts, "set_active_user_font", side_effect=apply
                ) as set_font:
                    with self.assertRaisesRegex(RuntimeError, "发生变化"):
                        fonts.repair_user_font(ssh, "/home/fonts", "test.ttf", False, status)
                if phase == "detach":
                    set_font.assert_not_called()
                self.assertEqual(ssh.renames, [])
                self.assertIn(BACKUP, ssh.nodes)

    def test_root_and_data_swap_recovery_are_archived_without_losing_bytes(self):
        ssh = RecoverySSH()
        ssh.add_file("lower-1", b"root snapshot")
        swap = f"{fonts.BACKUP_DIR}/.font-data-swap-{TOKEN}"
        ssh.nodes[swap] = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700, st_uid=0, st_gid=0, st_size=0
        )
        ssh.nodes[swap + "/file-0"] = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o644, st_uid=0, st_gid=0, st_size=13
        )
        ssh.files[swap + "/file-0"] = b"data snapshot"
        status = fonts.get_font_recovery_status(ssh)
        self.assertEqual(status.state, "ready")
        self.assertEqual(len(status.directories), 2)
        with mock.patch.object(fonts, "set_active_user_font"):
            fonts.repair_user_font(ssh, "/home/fonts", "test.ttf", True, status)
        self.assertEqual(sorted(ssh.files.values()), [b"data snapshot", b"root snapshot"])
        self.assertEqual(len(ssh.renames), 2)

    def test_failed_reapplication_or_archiving_keeps_original_backup(self):
        for failure in ("apply", "archive"):
            with self.subTest(failure=failure):
                ssh = RecoverySSH()
                path = ssh.add_file("lower-0")
                status = fonts.get_font_recovery_status(ssh)
                ssh.rename_failure = failure == "archive"
                error = RuntimeError("font write failed") if failure == "apply" else None
                with mock.patch.object(fonts, "set_active_user_font", side_effect=error):
                    with self.assertRaisesRegex(RuntimeError, "font write failed|归档未完成"):
                        fonts.repair_user_font(ssh, "/home/fonts", "test.ttf", True, status)
                self.assertEqual(ssh.files[path], b"original snapshot")
