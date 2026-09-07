import hashlib
import io
import json
import shlex
import stat
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock

import _plugin_recovery as recovery
import _tap_page_turn as tap
import _xovi_standalone as shared


TOKEN = "12345678-1234-1234-1234-123456789abc:778:1000"
BASE = shared.SHARED_LAYOUT.remote_base
DROPIN = shared.SHARED_LAYOUT.dropin_path


class Device:
    """Read-only device model with real marker/template/ownership validation."""

    def __init__(self, *, enabled=True):
        self.package = next(p for p in tap._trusted_catalog()
                            if p.release_version == "3.28.0.169" and p.platform == "chiappa")
        self.identity = tap.DeviceIdentity(self.package.firmware, self.package.platform,
                                           self.package.architecture, self.package.xochitl_sha256)
        self.runtime, self.trusted, _ = tap._trusted_shared_context(self.identity)
        self.states = {"tap-page-turn": shared.SharedFeatureState(self.trusted["tap-page-turn"], enabled, TOKEN)}
        self.entries = {}
        self.events = []
        self.pinned = False
        self.free_kib = 1024 * 1024
        self.service_state = "active"
        self.counters = "0 0"
        self.maps = ""
        self.mountinfo = "1 0 8:1 / / ro - ext4 /dev/root ro\n2 1 8:2 / /data rw - ext4 /dev/data rw\n"
        self.install(self.states)

    def add(self, path, data=b"", mode=0o100644, digest=None, size=None):
        for parent in reversed(PurePosixPath(path).parents):
            self.entries.setdefault(str(parent), SimpleNamespace(mode=0o40755, uid=0, gid=0,
                                    size=0, links=2, digest="", data=b""))
        self.entries[path] = SimpleNamespace(mode=mode, uid=0, gid=0,
            size=len(data) if size is None else size, links=1,
            digest=digest or hashlib.sha256(data).hexdigest(), data=data)

    def install(self, states):
        for path in list(self.entries):
            if path == BASE or path.startswith(BASE + "/") or path == DROPIN:
                del self.entries[path]
        enabled = tuple(state.spec for state in states.values() if state.enabled)
        launcher = shared.shared_launcher(self.runtime, enabled).encode()
        dropin = shared.shared_dropin(self.runtime, enabled).encode()
        self.add(BASE + "/package.json", shared.shared_marker(
            self.runtime, states, hashlib.sha256(launcher).hexdigest(), hashlib.sha256(dropin).hexdigest()))
        if enabled:
            for item in self.runtime.files:
                self.add(BASE + "/" + item.path, mode=stat.S_IFREG | item.mode, digest=item.sha256, size=item.size)
            for feature in enabled:
                for item in feature.files:
                    self.add(BASE + "/" + item.runtime_path, mode=stat.S_IFREG | item.mode,
                             digest=item.sha256, size=item.size)
            self.add(BASE + "/launcher.sh", launcher, 0o100755)
            self.add(BASE + "/systemd/" + shared.SHARED_LAYOUT.dropin_name, dropin)
            self.add(DROPIN, dropin)
        self.states = dict(states)

    def marker(self, mutate):
        value = json.loads(self.entries[BASE + "/package.json"].data)
        mutate(value)
        self.add(BASE + "/package.json", (json.dumps(value, sort_keys=True) + "\n").encode())

    def break_payload(self):
        self.entries[BASE + "/qmd-tool"].digest = "0" * 64
        self.entries[BASE + "/qmd-tool"].size = 3

    @contextmanager
    def operation_session(self):
        self.pinned = True
        self.events.append("session-enter")
        try:
            yield
        finally:
            self.pinned = False
            self.events.append("session-exit")

    def file_exists(self, path):
        return path in self.entries

    def open_remote(self, path, mode="r"):
        if path == "/proc/self/mountinfo":
            return io.BytesIO(self.mountinfo.encode())
        return io.BytesIO(self.entries[path].data)

    def exec_command(self, command):
        if command.startswith("[ -e "):
            return "", "", 0 if shlex.split(command)[2] in self.entries else 1
        return "", "", 1

    def exec_checked(self, command):
        self.events.append(command)
        if command.startswith("stat -c '%f|%u|%g|%s|%h'"):
            path = shlex.split(command)[-1]
            if path not in self.entries:
                raise RuntimeError("missing: " + path)
            e = self.entries[path]
            return f"{e.mode:x}|{e.uid}|{e.gid}|{e.size}|{e.links}"
        if command.startswith("stat -c '%f|%u|%g|%s|%n'"):
            paths = [BASE] + sorted(p for p in self.entries if p.startswith(BASE + "/"))
            return "\n".join(f"{e.mode:x}|{e.uid}|{e.gid}|{e.size}|{p}"
                             for p in paths for e in [self.entries[p]])
        if command.startswith("stat -c '%f %u %g'"):
            e = self.entries[shlex.split(command)[3].rstrip(";")]
            return f"{e.mode:x} {e.uid} {e.gid}"
        if command.startswith("sha256sum "):
            path = shlex.split(command)[1]
            return self.entries[path].digest + "  " + path
        if command.startswith("find -P "):
            path = shlex.split(command)[2]
            return "\n".join(p for p in sorted(self.entries)
                             if (p == path and "-mindepth" not in command) or
                             (p.startswith(path + "/") and ("-maxdepth" not in command or
                              "/" not in p[len(path) + 1:])))
        if command.startswith("for file in /etc/systemd/system"):
            return "\n".join(p for p, e in self.entries.items() if p.endswith(".conf")
                             and p.startswith("/etc/systemd/") and
                             any(x in e.data for x in (b"LD_PRELOAD", b"XOVI_ROOT", b"ExecStart=")))
        if command.startswith("pid=$(systemctl"):
            return TOKEN if "printf" in command else self.maps
        if command.startswith("for cmd in "):
            return ""
        if command == "systemctl is-active xochitl":
            return self.service_state
        if command.startswith("df -Pk "):
            return str(self.free_kib)
        if command.startswith("for file in /sys/devices/platform/lpgpr"):
            return self.counters
        if command.startswith("[ -f ") and "600:0:0:0" in command:
            e = self.entries[shlex.split(command)[2]]
            if (e.mode, e.uid, e.gid, e.size) != (0o100600, 0, 0, 0):
                raise RuntimeError("invalid sentinel")
            return ""
        if command.startswith("mkdir /tmp/rmtool-xovi-standalone.lock"):
            return ""
        if command.startswith(("rmdir /tmp/rmtool-xovi-standalone.lock", "mkdir -m 0755 ", "rm -rf ", "rm -f ")):
            return ""
        if command.startswith("/bin/sh /tmp/rmtool-xovi-recovery-"):
            self.install(self.staged_states)
            return ""
        raise AssertionError("Unexpected command: " + command)


class RecoveryInspectionTests(unittest.TestCase):
    def setUp(self):
        self.device = Device()
        self.identity = mock.patch.object(tap, "get_device_identity", side_effect=lambda ssh: ssh.identity)
        self.identity.start()
        self.addCleanup(self.identity.stop)

    def test_unknown_active_dropin_is_blocked_without_loader_keywords(self):
        self.device.break_payload()
        path = str(PurePosixPath(DROPIN).parent / "99-unmanaged.conf")
        for content in (b"[Service]\nExecStartPre=/home/root/custom-hook\n",
                        b"[Service]\nEnvironmentFile=/home/root/custom-env\n"):
            with self.subTest(content=content):
                self.device.add(path, content)
                result = recovery.inspect_recovery(self.device)
                self.assertEqual(result.state, recovery.RecoveryState.BLOCKED)
                self.assertIn(path, result.detail)

    def inspect(self):
        report = recovery.inspect_recovery(self.device)
        self.assertFalse(any(event.startswith(("mkdir", "rm ", "/bin/sh", "mount "))
                             for event in self.device.events))
        return report

    def test_healthy_and_absent_need_no_repair(self):
        self.assertEqual(self.inspect().state, recovery.RecoveryState.NOT_NEEDED)
        self.device.entries.clear()
        self.assertEqual(self.inspect().state, recovery.RecoveryState.NOT_NEEDED)

    def test_corrupt_missing_payload_does_not_weaken_strict_execution(self):
        for path in ("qmd-tool", "xovi.so", "launcher.sh", self.device.trusted["tap-page-turn"].runtime_path):
            for missing in (True, False):
                with self.subTest(path=path, missing=missing):
                    self.device.install(self.device.states)
                    if missing:
                        del self.device.entries[BASE + "/" + path]
                    else:
                        self.device.entries[BASE + "/" + path].digest = "0" * 64
                    report = self.inspect()
                    if path == "launcher.sh" and not missing:
                        self.assertEqual(report.state, recovery.RecoveryState.BLOCKED)
                        self.assertIn("启动脚本", report.detail)
                    else:
                        self.assertTrue(report.can_repair)
                    with self.assertRaises(RuntimeError):
                        shared.inspect_shared(self.device, self.device.runtime, self.device.trusted)

    def test_unknown_and_forged_marker_refused(self):
        mutations = (
            lambda marker: marker.update(launcher_sha256="0" * 64),
            lambda marker: marker["runtime"].update({"qmd-tool": "0" * 64}),
            lambda marker: marker["features"].update({"unknown-peer": {}}),
            lambda marker: marker["features"]["tap-page-turn"].update(qmd_sha256="0" * 64),
            lambda marker: marker["features"]["tap-page-turn"].update(qmd_path="../escape"),
            lambda marker: marker.update(schema_version=True),
            lambda marker: marker.update(runtime_present=1),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.device.install(self.device.states)
                self.device.marker(mutate)
                self.assertEqual(self.inspect().state, recovery.RecoveryState.BLOCKED)

    def test_malformed_duplicate_oversized_or_missing_marker(self):
        for data in (b"[]", b"{", b'{"features": {}, "features": {}}', b"x" * (recovery._MAX_MARKER_BYTES + 1)):
            with self.subTest(size=len(data)):
                self.device.add(BASE + "/package.json", data)
                self.assertEqual(self.inspect().state, recovery.RecoveryState.BLOCKED)
        del self.device.entries[BASE + "/package.json"]
        self.assertEqual(self.inspect().state, recovery.RecoveryState.BLOCKED)

    def test_owner_link_unknown_path_and_ancestor_refused(self):
        changes = ((BASE + "/qmd-tool", "uid", 1000), (BASE + "/qmd-tool", "links", 2),
                   (BASE + "/qmd-tool", "mode", 0o120777), ("/data/rmtool", "mode", 0o120777),
                   ("/data", "gid", 1000), (BASE + "/package.json", "mode", 0o100666))
        for path, field, value in changes:
            with self.subTest(path=path, field=field):
                old = getattr(self.device.entries[path], field)
                setattr(self.device.entries[path], field, value)
                self.assertEqual(self.inspect().state, recovery.RecoveryState.BLOCKED)
                setattr(self.device.entries[path], field, old)
        self.device.add(BASE + "/unknown.so")
        self.assertEqual(self.inspect().state, recovery.RecoveryState.BLOCKED)

    def test_root_group_writable_ancestor_is_allowed_but_managed_directory_is_not(self):
        systemd = self.device.entries["/etc/systemd/system"]
        systemd.mode = 0o40775
        self.assertEqual(self.inspect().state, recovery.RecoveryState.NOT_NEEDED)

        self.device.entries[BASE].mode = 0o40775
        self.assertEqual(self.inspect().state, recovery.RecoveryState.BLOCKED)

    def test_ancestor_still_rejects_world_write_special_bits_and_wrong_owner(self):
        systemd = self.device.entries["/etc/systemd/system"]
        for field, value in (("mode", 0o40777), ("mode", 0o42775), ("uid", 1000), ("gid", 1000)):
            with self.subTest(field=field, value=oct(value) if field == "mode" else value):
                old = getattr(systemd, field)
                setattr(systemd, field, value)
                self.assertEqual(self.inspect().state, recovery.RecoveryState.BLOCKED)
                setattr(systemd, field, old)

    def test_active_unknown_dropin_blocked_even_at_known_name(self):
        self.device.add(DROPIN, b"[Service]\nExecStart=/tmp/foreign\n")
        self.assertIn("drop-in", self.inspect().detail)
        self.assertEqual(self.inspect().state, recovery.RecoveryState.BLOCKED)

    def test_known_dropin_state_drift_and_absence_repairable(self):
        self.device.add(DROPIN, shared.shared_dropin(self.device.runtime, ()).encode())
        self.device.break_payload()
        self.assertTrue(self.inspect().can_repair)
        del self.device.entries[DROPIN]
        self.assertTrue(self.inspect().can_repair)

    def test_damaged_launcher_without_active_dropin_is_repairable(self):
        del self.device.entries[DROPIN]
        self.device.entries[BASE + "/launcher.sh"].digest = "0" * 64
        self.assertTrue(self.inspect().can_repair)

    def test_vellum_external_dropins_runtime_and_hidden_mounts_blocked(self):
        for path in (tap.VELLUM_ROOT, tap.SHARED_XOVI_LIBRARY, shared.LEGACY_SHARED_LAYOUT.remote_base,
                     "/etc/systemd/system/xochitl.service.d/foreign.conf"):
            with self.subTest(path=path):
                self.device.add(path, b"ExecStart=/external\n")
                self.assertEqual(self.inspect().state, recovery.RecoveryState.BLOCKED)
                del self.device.entries[path]
        self.device.mountinfo += "3 1 0:4 / /etc rw - tmpfs tmpfs rw\n"
        self.assertEqual(self.inspect().state, recovery.RecoveryState.BLOCKED)
        self.device.mountinfo = self.device.mountinfo.splitlines()[0] + "\n"
        self.device.maps = "a-b r-xp 0 0:0 0 /external/xovi.so"
        self.assertEqual(self.inspect().state, recovery.RecoveryState.BLOCKED)

    def test_unsupported_firmware_and_operational_preflight(self):
        self.device.identity = replace(self.device.identity, xochitl_sha256="0" * 64)
        self.assertEqual(self.inspect().state, recovery.RecoveryState.UNSUPPORTED)
        self.device.identity = replace(self.device.identity, xochitl_sha256=self.device.package.xochitl_sha256)
        self.device.break_payload()
        for field, value in (("service_state", "inactive"), ("counters", "1 0"), ("free_kib", 1)):
            with self.subTest(field=field):
                old = getattr(self.device, field)
                setattr(self.device, field, value)
                self.assertEqual(self.inspect().state, recovery.RecoveryState.BLOCKED)
                setattr(self.device, field, old)

    def test_known_published_predecessor_and_firmware_residue(self):
        revisions = recovery._published_predecessors(self.device.identity, self.device.trusted)
        predecessor = revisions["reading-enhancements"][0][1]
        self.device.install({predecessor.feature_id: shared.SharedFeatureState(predecessor, True, TOKEN)})
        self.assertTrue(self.inspect().can_repair)
        package = next(p for p in tap._trusted_catalog()
                       if p.release_version == "3.28.0.166" and p.platform == "chiappa")
        self.device.identity = tap.DeviceIdentity(package.firmware, package.platform, package.architecture, package.xochitl_sha256)
        self.assertTrue(self.inspect().can_repair)

    def test_external_peers_name_the_repair_limit_and_disabled_preserved(self):
        for feature_id in ("appload", "koreader", "pinyin-input"):
            if feature_id not in self.device.trusted:
                continue
            with self.subTest(feature_id=feature_id):
                states = dict(self.device.states)
                states[feature_id] = shared.SharedFeatureState(self.device.trusted[feature_id], True, TOKEN)
                self.device.install(states)
                self.device.break_payload()
                report = self.inspect()
                self.assertEqual(report.state, recovery.RecoveryState.BLOCKED)
                self.assertIn(feature_id, report.detail)
                states[feature_id] = replace(states[feature_id], enabled=False)
                self.device.install(states)
                self.device.break_payload()
                # Any earlier enabled external peer must be disabled too.
                for fid in states:
                    if fid != "tap-page-turn":
                        states[fid] = replace(states[fid], enabled=False)
                self.device.install(states)
                self.device.break_payload()
                report = self.inspect()
                self.assertTrue(report.can_repair)
                self.assertIn(feature_id, report.features)


class RecoveryRepairTests(unittest.TestCase):
    def setUp(self):
        self.device = Device()
        self.device.break_payload()
        self.patch(tap, "get_device_identity", side_effect=lambda ssh: ssh.identity)
        self.patch(shared, "_stage_shared", side_effect=self.stage)
        self.patch(shared, "_upload_bytes", side_effect=self.upload)
        self.patch(shared, "_set_recovery_sentinel_locked", side_effect=self.latch)
        self.patch(shared, "_clear_recovery_sentinel_locked", side_effect=self.clear_latch)
        self.download = self.patch(tap, "download_package", side_effect=self.fetch)
        self.extract = self.patch(tap, "extract_verified_package", side_effect=self.extract_package)

    def patch(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def fetch(self, package, state_dir):
        self.assertTrue(self.device.pinned)
        self.device.events.append("download:" + package.package_id)
        return Path("verified.tar.gz")

    def extract_package(self, archive, package, destination):
        self.device.events.append("extract:" + package.package_id)
        return destination

    def stage(self, ssh, runtime, states, roots, sources, stage):
        self.assertFalse(sources)
        self.assertEqual(set(roots), {fid for fid, state in states.items() if state.enabled})
        self.device.events.append("stage")
        self.device.staged_states = states

    def upload(self, ssh, data, path, mode):
        self.device.add(path, data, stat.S_IFREG | mode)

    def latch(self, ssh):
        self.device.events.append("latch")
        existed = shared.SHARED_RECOVERY_SENTINEL in self.device.entries
        self.device.add(shared.SHARED_RECOVERY_SENTINEL, mode=0o100600)
        return not existed

    def clear_latch(self, ssh, path):
        self.device.events.append("clear-latch")
        del self.device.entries[path]

    def test_success_rebuilds_peers_and_reports_quarantine_keeps_external_data(self):
        state = shared.SharedFeatureState(self.device.trusted["fast-mono-reading"], False, TOKEN)
        self.device.states[state.spec.feature_id] = state
        self.device.install(self.device.states)
        self.device.break_payload()
        protected = ("/home/root/books/book.epub", "/home/root/.config/remarkable/xochitl.conf",
                     "/home/root/.local/share/rmtool/fonts/font.ttf", shared.LEGACY_RECOVERY_SENTINEL)
        for path in protected:
            self.device.add(path, b"" if path.endswith("disable-xovi") else b"user bytes",
                            0o100600 if path.endswith("disable-xovi") else 0o100644)
        before = {path: self.device.entries[path].data for path in protected}
        result = recovery.repair(self.device, "state")
        self.assertEqual(result.state, recovery.RecoveryState.NOT_NEEDED)
        self.assertTrue(result.backup_path.startswith("/data/rmtool/.xovi-dropins-"))
        self.assertIn("单独解除", result.detail)
        self.assertFalse(self.device.states["fast-mono-reading"].enabled)
        self.assertEqual(before, {path: self.device.entries[path].data for path in protected})
        self.assertIn(shared.LEGACY_RECOVERY_SENTINEL, self.device.entries)
        self.assertNotIn(shared.SHARED_RECOVERY_SENTINEL, self.device.entries)
        events = self.device.events
        lock = next(i for i, event in enumerate(events) if event.startswith("mkdir /tmp/rmtool-xovi-standalone.lock"))
        self.assertLess(next(i for i, event in enumerate(events) if event.startswith("extract:")), lock)
        self.assertLess(events.index("latch"), next(i for i, event in enumerate(events) if event.startswith("/bin/sh ")))
        self.assertFalse(any("restart xochitl" in event or "reboot" in event for event in events))

    def test_all_enabled_packages_verified_before_first_device_mutation(self):
        fast = recovery.migration.fast
        self.device.states["fast-mono-reading"] = shared.SharedFeatureState(self.device.trusted["fast-mono-reading"], True, TOKEN)
        self.device.install(self.device.states)
        self.device.break_payload()
        self.patch(fast, "download_package", side_effect=self.fetch)
        self.patch(fast, "extract_verified_package", side_effect=self.extract_package, create=True)
        self.download.side_effect = RuntimeError("second download failed")
        with self.assertRaisesRegex(RuntimeError, "second download failed"):
            recovery.repair(self.device, "state")
        self.assertFalse(any(event.startswith(("mkdir", "rm ", "/bin/sh ")) for event in self.device.events))
        self.assertTrue(any(event.startswith("extract:") for event in self.device.events))

    def test_success_drops_stale_pending_only_and_preserves_original_sentinel_state(self):
        for sentinel in (None, shared.SHARED_RECOVERY_SENTINEL, shared.LEGACY_RECOVERY_SENTINEL):
            with self.subTest(sentinel=sentinel):
                for path in (shared.SHARED_RECOVERY_SENTINEL, shared.LEGACY_RECOVERY_SENTINEL):
                    self.device.entries.pop(path, None)
                self.device.install(self.device.states)
                self.device.break_payload()
                self.device.add(BASE + "/startup.pending", mode=0o100600)
                if sentinel:
                    self.device.add(sentinel, mode=0o100600)
                result = recovery.repair(self.device, "state")
                self.assertNotIn(BASE + "/startup.pending", self.device.entries)
                self.assertFalse(any(path.endswith("/startup.pending") and ".staging-" in path for path in self.device.entries))
                remaining = {path for path in (shared.SHARED_RECOVERY_SENTINEL, shared.LEGACY_RECOVERY_SENTINEL)
                             if path in self.device.entries}
                self.assertEqual(remaining, {sentinel} if sentinel else set())
                self.assertIn("原有紧急停用保护" if sentinel else "临时保护已解除", result.detail)

    def test_state_drift_during_download_or_staging_aborts_before_transaction(self):
        original = self.extract_package
        def drift(*args):
            result = original(*args)
            self.device.entries[BASE + "/xovi.so"].digest = "f" * 64
            return result
        self.extract.side_effect = drift
        with self.assertRaisesRegex(RuntimeError, "状态发生变化"):
            recovery.repair(self.device, "state")
        self.assertNotIn("stage", self.device.events)
        self.extract.side_effect = original
        staged = shared._stage_shared
        def drift_stage(*args):
            self.stage(*args)
            self.device.entries[BASE + "/xovi.so"].digest = "e" * 64
        staged.side_effect = drift_stage
        with self.assertRaisesRegex(RuntimeError, "状态发生变化"):
            recovery.repair(self.device, "state")
        self.assertFalse(any(event.startswith("/bin/sh ") for event in self.device.events))

    def test_transaction_or_postcheck_failure_keeps_latch_and_reports_backup(self):
        execute = self.device.exec_checked
        for postcheck in (False, True):
            with self.subTest(postcheck=postcheck):
                self.device.install(self.device.states)
                self.device.break_payload()
                def fail(command):
                    if command.startswith("/bin/sh "):
                        if postcheck:
                            execute(command)
                            self.device.break_payload()
                            return ""
                        raise RuntimeError("transaction failure")
                    return execute(command)
                self.device.exec_checked = fail
                with self.assertRaisesRegex(RuntimeError, r"隔离备份位于 /data/rmtool/\.xovi-dropins-"):
                    recovery.repair(self.device, "state")
                self.assertIn(shared.SHARED_RECOVERY_SENTINEL, self.device.entries)
                self.assertFalse(any(event.startswith("rm ") and ".xovi-dropins-" in event for event in self.device.events))

    def test_capacity_uses_data_even_when_home_has_space(self):
        execute = self.device.exec_checked
        self.device.exec_checked = lambda command: "0" if command.startswith("df -Pk /data") else execute(command)
        with self.assertRaisesRegex(RuntimeError, "/data 空间不足"):
            recovery.repair(self.device, "state")
        self.download.assert_not_called()

    def test_healthy_retry_refuses_no_mutation(self):
        self.device.install(self.device.states)
        with self.assertRaisesRegex(RuntimeError, "无需修复"):
            recovery.repair(self.device, "state")
        self.download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
