import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _device_storage as storage
import _native_chinese as native
import _plugin_recovery as recovery
import _xovi_standalone as shared


def root_snapshot(*, free=14311, available=0, internal=4096, resuid=0,
                  options="rw", log_block=0, log_cluster=0, bigalloc=False):
    sb = bytearray(1024)
    struct.pack_into("<I", sb, 4, 243712)
    struct.pack_into("<II", sb, 0x18, log_block, log_cluster)
    struct.pack_into("<HH", sb, 0x38, 0xEF53, 1)
    struct.pack_into("<H", sb, 0x50, resuid)
    struct.pack_into("<II", sb, 0x60, 0x80, 0x200 if bigalloc else 0)
    return (
        f"mount=1 0 179:2 / / {options} - ext4 /dev/root {options}\n"
        f"stats=ef53 {1024 << log_block} 230000 {free} {available} 10000 1000\n"
        f"reserved={internal}\nsuperblock={sb.hex()}"
    )


def root_probe_fixture(root):
    values = dict(line.split("=", 1) for line in root_snapshot().splitlines())
    (root / "machine").write_text("reMarkable 1.0\n")
    (root / "mountinfo").write_text(values["mount"] + "\n")
    (root / "reserved").write_text(values["reserved"] + "\n")
    (root / "mmcblk2p2").write_bytes(bytes(1024) + bytes.fromhex(values["superblock"]))
    # Replace hardware endpoints; the probe still reads the fixture file.
    script = storage.ROOT_SPACE_COMMAND.replace("/sys/devices/soc0/machine", "machine")
    script = script.replace("/proc/self/mountinfo", "mountinfo")
    script = script.replace('"/dev/$device"', '"$device"').replace('[ -b ', '[ -f ')
    script = script.replace('"/sys/fs/ext4/$device/reserved_clusters"', '"reserved"')
    # Git Bash lacks hexdump; emulate this format for shell-flow tests only.
    hexdump = r'''if ! command -v hexdump >/dev/null 2>&1; then
hexdump() {
    [ "$#" -eq 8 ] && [ "$1 $2 $3 $4 $5 $6 $7" = '-v -s 1024 -n 1024 -e 1/1 "%02x"' ] || return 2
    hex=$(od -An -v -tx1 -j "$3" -N "$5" "$8") || return $?
    printf '%s' "$hex" | tr -d ' \n'
}
fi
'''
    return (hexdump + f"stat() {{ printf '%s\\n' '{values['stats']}'; }}\n"
            "readlink() { printf '/devices/mmcblk2p2\\n'; }\n"
            "id() { printf '0\\n'; }\n"
            "base64() { printf 'base64: not found\\n' >&2; return 127; }\n" + script)


class RootCapacityTests(unittest.TestCase):
    def test_zero_df_available_does_not_mean_root_cannot_allocate(self):
        ssh = Mock()
        ssh.exec_checked.return_value = root_snapshot()
        self.assertEqual(storage.root_free_bytes(ssh, 0), (14311 - 4096) * 1024)
        ssh.exec_checked.assert_called_once_with(storage.ROOT_SPACE_COMMAND)

    def test_allocatable_bytes_exclude_internal_clusters_and_respect_block_size(self):
        for log_block, log_cluster in ((0, 0), (2, 2), (2, 4)):
            with self.subTest(block=log_block, cluster=log_cluster):
                ssh = Mock()
                ssh.exec_checked.return_value = root_snapshot(
                    log_block=log_block, log_cluster=log_cluster,
                    bigalloc=log_cluster != log_block, internal=100)
                self.assertEqual(storage.root_free_bytes(ssh, 0),
                                 14311 * (1024 << log_block) - 100 * (1024 << log_cluster))

    def test_nonlegacy_nonroot_or_other_reserved_uid_does_not_gain_space(self):
        for output in ("ordinary", root_snapshot(resuid=1000),
                       root_snapshot(options="rw,resuid=1000")):
            with self.subTest(output=output[:50]):
                ssh = Mock()
                ssh.exec_checked.return_value = output
                self.assertEqual(storage.root_free_bytes(ssh, 123), 123)

    def test_mount_resuid_overrides_superblock_default(self):
        ssh = Mock()
        ssh.exec_checked.return_value = root_snapshot(resuid=1000, options="rw,resuid=0")
        self.assertEqual(storage.root_free_bytes(ssh, 0), 10215 * 1024)

    def test_missing_inconsistent_or_unsafe_metadata_fails_closed(self):
        valid = root_snapshot()
        cases = ["", "ordinary\nextra", valid + "\nextra", valid.replace("ext4", "xfs"),
                 valid.replace("179:2", "invalid"), valid.replace("/ / rw", "/sub / rw"),
                 valid.replace("ef53", "invalid"), valid.replace("10000 1000", "10000 0"),
                 valid.replace("230000", "999999"), valid.replace("230000 14311 0", "0 0 0"),
                 valid.replace("ef53 1024", "ef53 2048"), root_snapshot(available=15000),
                 root_snapshot(internal=300000), root_snapshot(free=5000, available=2000),
                 root_snapshot(log_cluster=2), root_snapshot(options="rw,resuid=oops")]
        lines = valid.splitlines()
        cases.extend("\n".join([*lines[:3], "superblock=" + encoded]) for encoded in ("!", "00"))
        sb = bytearray.fromhex(lines[3].split("=", 1)[1])
        struct.pack_into("<H", sb, 0x3A, 3)
        cases.append("\n".join([*lines[:3], "superblock=" + sb.hex()]))
        for output in cases:
            with self.subTest(output=output[:100]):
                ssh = Mock()
                ssh.exec_checked.return_value = output
                with self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "无法确认根分区"):
                    storage.root_free_bytes(ssh, 100)
        ssh.exec_checked.side_effect = RuntimeError("missing reserved_clusters")
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "已停止写入"):
            storage.root_free_bytes(ssh, 100)

    def test_missing_or_duplicate_fields_report_reason_without_logging_superblock(self):
        lines = root_snapshot().splitlines()
        for key in ("mount", "stats", "reserved", "superblock"):
            ssh = Mock()
            ssh.exec_checked.return_value = "\n".join(line for line in lines if not line.startswith(key + "="))
            with self.subTest(key=key), self.assertLogs(level="ERROR") as logs:
                with self.assertRaisesRegex(RuntimeError, "已停止写入") as error:
                    storage.root_free_bytes(ssh, 100)
            self.assertIn("missing root capacity fields: " + key, str(error.exception.__cause__))
            self.assertIn(key, " ".join(logs.output))
            self.assertNotIn(lines[3].split("=", 1)[1], " ".join(logs.output))
        ssh.exec_checked.return_value = "\n".join([*lines, lines[0]])
        with self.assertLogs(level="ERROR"), self.assertRaises(RuntimeError) as error:
            storage.root_free_bytes(ssh, 100)
        self.assertIn("duplicate", str(error.exception.__cause__))

    def test_remote_read_failure_preserves_cause(self):
        ssh = Mock()
        ssh.exec_checked.side_effect = RuntimeError("hexdump: read error; root-capacity: superblock read failed")
        with self.assertLogs(level="ERROR") as logs, self.assertRaises(RuntimeError) as error:
            storage.root_free_bytes(ssh, 100)
        self.assertIn("superblock read failed", str(error.exception.__cause__))
        self.assertIn("hexdump: read error", " ".join(logs.output))

    def test_only_root_backed_system_storage_uses_privileged_probe(self):
        for partition in ("/", "/data"):
            ssh = Mock()
            ssh.exec_checked.side_effect = [f"{partition}=0", root_snapshot()]
            result = storage.system_free_bytes(ssh)
            self.assertEqual(result, (partition, 10215 * 1024 if partition == "/" else 0))
            self.assertEqual(ssh.exec_checked.call_count, 2 if partition == "/" else 1)

    def test_ambiguous_or_invalid_system_capacity_fails_closed(self):
        for output in ("/=oops", "/data=-1", "/=12\n/data=34", "", "0\n0"):
            ssh = Mock()
            ssh.exec_checked.return_value = output
            with self.assertRaises(RuntimeError):
                storage.system_free_bytes(ssh)

    def test_capacity_error_has_partition_required_available_and_shortfall(self):
        with self.assertRaisesRegex(RuntimeError, r"/ 空间不足.*9\.00 MiB.*8\.00 MiB.*1\.00 MiB"):
            storage.require_capacity("/", 8 * 1048576, 9 * 1048576)
        storage.require_capacity("/", 9 * 1048576, 9 * 1048576)

    def test_probe_shell_executes_read_only_against_fixture(self):
        git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        shell = str(git_bash) if git_bash.is_file() else shutil.which("bash")
        self.assertIsNotNone(shell, "A POSIX shell is required for the storage probe test")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root_probe_fixture(root)
            for machine, uid in (("reMarkable 1.0", 0), ("reMarkable 2.0", 0),
                                 ("reMarkable Ferrari", 0), ("reMarkable 2.0", 1000)):
                (root / "machine").write_text(machine + "\n")
                before = {p.name: p.read_bytes() for p in root.iterdir()}
                probe = script.replace("id() { printf '0\\n'; }", f"id() {{ printf '{uid}\\n'; }}")
                result = subprocess.run([shell, "-c", probe], cwd=temp, capture_output=True,
                                        text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = "ordinary" if uid != 0 or "Ferrari" in machine else root_snapshot()
                self.assertEqual(result.stdout.strip(), expected)
                self.assertEqual(result.stderr, "")
                ssh = Mock()
                ssh.exec_checked.return_value = result.stdout
                self.assertEqual(storage.root_free_bytes(ssh, 0), 0 if expected == "ordinary" else 10215 * 1024)
                self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)
            (root / "machine").write_text("reMarkable 2.0\n")
            (root / "reserved").unlink()
            result = subprocess.run([shell, "-c", script], cwd=temp, capture_output=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"reserved clusters read failed", result.stderr)

    def test_probe_shell_rejects_missing_failed_empty_and_short_superblock_reads(self):
        git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        shell = str(git_bash) if git_bash.is_file() else shutil.which("bash")
        self.assertIsNotNone(shell)
        cases = [
            ("hexdump() { printf 'hexdump: not found\\n' >&2; return 127; }\n", "superblock read failed"),
            ("hexdump() { printf '00'; printf 'hexdump: read error\\n' >&2; return 1; }\n", "superblock read failed"),
            ("hexdump() { return 0; }\n", "superblock short or invalid read"),
            ("hexdump() { printf '0000'; }\n", "superblock short or invalid read"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            script = root_probe_fixture(root)
            for prefix, error in cases:
                with self.subTest(prefix=prefix):
                    before = {p.name: p.read_bytes() for p in root.iterdir()}
                    result = subprocess.run([shell, "-c", prefix + script], cwd=temp,
                                            capture_output=True, text=True, timeout=20)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(error, result.stderr)
                    self.assertNotIn("superblock=", result.stdout)
                    self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)
            (root / "mmcblk2p2").write_bytes(bytes(1024 + 100))
            result = subprocess.run([shell, "-c", script], cwd=temp,
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("superblock short or invalid read", result.stderr)


class LegacyPluginCapacityTests(unittest.TestCase):
    def setUp(self):
        self.runtime, self.feature = native._shared_specs(next(
            p for p in native._trusted_catalog()
            if p.platform == "rm2" and p.release_version == "3.28.0.172"))
        self.states = {self.feature.feature_id: shared.SharedFeatureState(self.feature, True, "token")}

    def ssh(self, *, free=14311, home=100000, partition="/"):
        ssh = Mock()
        def execute(command):
            if command == storage.SYSTEM_SPACE_COMMAND:
                return partition + "=0"
            if command == storage.ROOT_SPACE_COMMAND:
                return root_snapshot(free=free)
            if command.startswith("df -Pk /home"):
                return str(home)
            if command.startswith("df -Pk / "):
                return "0"
            if command == "mountpoint -q /home":
                return ""
            raise AssertionError(command)
        ssh.exec_checked.side_effect = execute
        return ssh

    def test_rm2_root_payload_budget_includes_allocation_and_reserve(self):
        with patch.object(storage, "require_capacity", wraps=storage.require_capacity) as check:
            shared.check_shared_capacity(self.ssh(free=50000), self.runtime, self.states)
        partition, available, needed = check.call_args.args
        self.assertEqual(partition, "/")
        self.assertEqual(available, (50000 - 4096) * 1024)
        self.assertGreater(needed, storage.LEGACY_ROOT_RESERVE + sum(f.size for f in self.runtime.files))
        self.assertEqual(shared.preferred_layout(self.runtime), shared.SHARED_LAYOUT)
        with self.assertRaisesRegex(RuntimeError, "/ 空间不足"):
            shared.check_shared_capacity(self.ssh(free=4096 + 8192), self.runtime, self.states)

    def test_control_repair_uses_small_budget_and_rechecks_remaining_headroom(self):
        shared.check_shared_capacity(self.ssh(), self.runtime, self.states, controls_only=True)
        shared.check_shared_capacity(self.ssh(), self.runtime, self.states, staged=True)
        with self.assertRaisesRegex(RuntimeError, "/ 空间不足"):
            shared.check_shared_capacity(self.ssh(free=4096 + 8192), self.runtime, self.states, staged=True)

    def test_rm1_home_payload_works_with_zero_df_root_but_does_not_ignore_root(self):
        runtime = replace(self.runtime, platform="rm1")
        shared.check_shared_capacity(self.ssh(), runtime, self.states)
        with self.assertRaisesRegex(RuntimeError, "/home 空间不足"):
            shared.check_shared_capacity(self.ssh(home=0), runtime, self.states)
        with self.assertRaisesRegex(RuntimeError, "/ 空间不足"):
            shared.check_shared_capacity(self.ssh(free=4096), runtime, self.states)

    def test_independent_data_retains_conservative_reserve(self):
        ssh = self.ssh(partition="/data")
        with self.assertRaisesRegex(RuntimeError, "/data 空间不足"):
            shared.check_shared_capacity(ssh, self.runtime, self.states)
        self.assertNotIn(storage.ROOT_SPACE_COMMAND, [c.args[0] for c in ssh.exec_checked.call_args_list])

    def test_recovery_reuses_install_budget_for_both_legacy_models(self):
        for platform in ("rm1", "rm2"):
            plan = SimpleNamespace(runtime=replace(self.runtime, platform=platform),
                                   states=self.states, targets={self.feature.feature_id: self.feature},
                                   controls_only=True)
            with patch.object(shared, "check_shared_capacity") as check:
                recovery._check_capacity(self.ssh(), plan, staged=True)
            self.assertEqual(check.call_args.args[1:], (plan.runtime, self.states))
            self.assertEqual(check.call_args.kwargs, {"staged": True, "controls_only": True})

    def test_modern_plugin_install_does_not_run_legacy_probe(self):
        for platform in ("chiappa", "ferrari", "tatsu"):
            ssh = self.ssh()
            shared.check_shared_capacity(ssh, replace(self.runtime, platform=platform), self.states)
            ssh.exec_checked.assert_not_called()

    def test_control_staging_rechecks_capacity_after_hash_verification(self):
        ssh = Mock()
        uploaded = {}
        def upload(_ssh, data, path, mode):
            uploaded[path] = data
        with patch.object(shared, "check_shared_capacity", side_effect=(None, RuntimeError("space drift"))) as check, \
                patch.object(shared, "_upload_bytes", side_effect=upload), \
                patch.object(shared, "_remote_sha256", side_effect=
                             lambda _ssh, path: hashlib.sha256(uploaded[path]).hexdigest()), \
                self.assertRaisesRegex(RuntimeError, "space drift"):
            shared._stage_shared(ssh, self.runtime, self.states, {}, {}, "/stage", controls_only=True)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(check.call_args.kwargs, {"staged": True, "controls_only": True})
        self.assertTrue(uploaded)


if __name__ == "__main__":
    unittest.main()
