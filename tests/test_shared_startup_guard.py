"""Execute startup/clean-stop guard fragments without contacting a device."""

import os
import hashlib
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _tap_page_turn as tap
import _xovi_standalone as shared
from test_plugin_recovery_transactions import SH


@unittest.skipUnless(SH, "A POSIX shell is required")
class StartupGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rmtool-startup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        package = next(p for p in tap._trusted_catalog() if p.platform == "chiappa")
        self.runtime, self.feature = tap._shared_specs(package)
        self.base = self.root / "sandbox/data/rmtool/xovi-standalone"
        self.base.mkdir(parents=True)
        (self.root / "sandbox/run").mkdir()
        self.pending = self.base / "startup.pending"
        self.invocation = "a" * 32
        self.proof = self.root / ("sandbox/run/rmtool-xovi-startup-" + self.invocation)
        launcher = shared.shared_launcher(self.runtime, (self.feature,))
        self.arm = launcher[launcher.index("arm_startup_guard() {"):launcher.index("clear_guard_when_stable() {")]
        unit = shared.shared_dropin(self.runtime, (self.feature,))
        line = next(line for line in unit.splitlines() if line.startswith("ExecStopPost="))
        self.assertIn("$$SERVICE_RESULT", line)
        self.stop = shlex.split(line.split("=", 1)[1])[2].replace("$$", "$").replace("%%", "%")
        self.prelude = r'''
stat() {
    case "$2" in
        %a:%u:%g:%s)
            if [ "$3" = "$BAD_FILE" ]; then printf '666:1000:0:0';
            else printf '600:0:0:%s' "$(wc -c < "$3" | tr -d ' ')"; fi ;;
        %u:%g) printf '0:0' ;;
        %a) if [ "$3" = "$BAD_PARENT" ]; then printf '777'; else printf '755'; fi ;;
        *) exit 98 ;;
    esac
}
chown() { :; }
chmod() { :; }
PENDING=./sandbox/data/rmtool/xovi-standalone/startup.pending
PENDING_TMP=./sandbox/data/rmtool/xovi-standalone/.pending-tmp
'''

    def execute(self, body, **environment):
        command = self.prelude + body.replace("/run/", "./sandbox/run/").replace("/data/", "./sandbox/data/")
        command = command.replace('"/"', '"./sandbox"')
        command = command.replace('"/data"', '"./sandbox/data"')
        env = dict(os.environ, INVOCATION_ID=self.invocation, SERVICE_RESULT="success",
                   EXIT_CODE="killed", EXIT_STATUS="TERM", BAD_FILE="", BAD_PARENT="")
        env.update(environment)
        syntax = subprocess.run([SH, "-n"], input=command, cwd=self.root, env=env,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        result = subprocess.run([SH], input=command, cwd=self.root, env=env,
                                text=True, capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 98, result.stderr)
        return result

    def arm_guard(self):
        result = self.execute(self.arm + "\narm_startup_guard\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.pending.exists())
        self.assertTrue(self.proof.exists())

    def test_clean_restart_inside_stability_window_does_not_latch(self):
        for code, status in (("killed", "TERM"), ("exited", "0")):
            with self.subTest(code=code):
                self.arm_guard()
                result = self.execute(self.stop, EXIT_CODE=code, EXIT_STATUS=status)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.pending.exists())
                self.assertFalse(self.proof.exists())

    def test_crash_timeout_or_unknown_result_preserves_latch(self):
        for result, code, status in (("core-dump", "dumped", "SEGV"),
                                     ("timeout", "killed", "KILL"),
                                     ("exit-code", "exited", "1"),
                                     ("", "", ""), ("success", "", "")):
            with self.subTest(result=result, code=code):
                self.pending.unlink(missing_ok=True)
                self.arm_guard()
                self.assertEqual(self.execute(self.stop, SERVICE_RESULT=result,
                                             EXIT_CODE=code, EXIT_STATUS=status).returncode, 0)
                self.assertTrue(self.pending.exists())
                self.assertFalse(self.proof.exists())

    def test_stock_fallback_cannot_clear_an_earlier_crash_latch(self):
        self.arm_guard()
        self.execute(self.stop, SERVICE_RESULT="core-dump", EXIT_CODE="dumped", EXIT_STATUS="SEGV")
        result = self.execute(self.stop, INVOCATION_ID="b" * 32)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.pending.exists())
        result = self.execute(self.arm + "\narm_startup_guard\n", INVOCATION_ID="b" * 32)
        self.assertNotEqual(result.returncode, 0)

    def test_missing_or_malformed_invocation_never_arms(self):
        for invocation in ("", "../outside", "a" * 31, "g" * 32):
            with self.subTest(invocation=invocation):
                result = self.execute(self.arm + "\narm_startup_guard\n", INVOCATION_ID=invocation)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.pending.exists())

    def test_unsafe_marker_proof_or_parent_is_not_cleared(self):
        for kind in ("pending-mode", "proof-mode", "pending-data", "parent-mode"):
            with self.subTest(kind=kind):
                self.pending.unlink(missing_ok=True)
                self.proof.unlink(missing_ok=True)
                self.arm_guard()
                env = {}
                if kind == "pending-mode":
                    env["BAD_FILE"] = "./sandbox/data/rmtool/xovi-standalone/startup.pending"
                elif kind == "proof-mode":
                    env["BAD_FILE"] = "./sandbox/run/rmtool-xovi-startup-" + self.invocation
                elif kind == "pending-data":
                    self.pending.write_bytes(b"unknown")
                else:
                    env["BAD_PARENT"] = "./sandbox/data/rmtool"
                self.execute(self.stop, **env)
                self.assertTrue(self.pending.exists())

    def test_stopped_process_token_is_explicit_and_remains_strict_by_default(self):
        ssh = mock.Mock()
        token = "12345678-1234-1234-1234-123456789abc:0:0"
        prelude = '''
systemctl() {
    case "$*" in
        "show xochitl -p MainPID --value") printf '0' ;;
        "show xochitl -p ActiveState --value") printf '%s' "$ACTIVE_STATE" ;;
        *) exit 98 ;;
    esac
}
cat() { printf '12345678-1234-1234-1234-123456789abc'; }
'''
        state = "failed"
        def execute(command):
            result = self.execute(prelude + command, ACTIVE_STATE=state)
            if result.returncode:
                raise RuntimeError("process not stable")
            return result.stdout
        ssh.exec_checked.side_effect = execute
        self.assertEqual(shared._process_token(ssh, allow_stopped=True), token)
        command = ssh.exec_checked.call_args.args[0]
        self.assertIn("inactive|failed", command)
        self.assertNotIn("|| true", command)
        with self.assertRaises(RuntimeError):
            shared._process_token(ssh)
        self.assertIn('"$pid" != 0', ssh.exec_checked.call_args.args[0])
        for state in ("active", "activating", "deactivating", ""):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                shared._process_token(ssh, allow_stopped=True)

    def test_control_staging_uploads_only_verified_generated_configuration(self):
        ssh = mock.Mock()
        uploaded = {}
        stage = "/data/rmtool/xovi-standalone.staging-test"
        states = {self.feature.feature_id: shared.SharedFeatureState(
            self.feature, True, "12345678-1234-1234-1234-123456789abc:0:0")}
        def upload(_ssh, data, path, mode):
            uploaded[path] = (data, mode)
        with mock.patch.object(shared, "_upload_bytes", side_effect=upload), \
                mock.patch.object(shared, "_remote_sha256", side_effect=lambda _ssh, path:
                                  hashlib.sha256(uploaded[path][0]).hexdigest()), \
                mock.patch.object(shared, "check_shared_capacity") as capacity:
            shared._stage_shared(ssh, self.runtime, states, {}, {}, stage, controls_only=True)
        capacity.assert_not_called()
        self.assertEqual(set(uploaded), {stage + "/package.json", stage + "/launcher.sh",
                                         stage + "/systemd/" + shared.SHARED_LAYOUT.dropin_name})
        self.assertFalse(any("qmd-tool" in c.args[0] or c.args[0].startswith("cp ")
                             for c in ssh.exec_checked.call_args_list))

    def test_all_known_device_firmware_templates_have_valid_shell_syntax(self):
        import _native_chinese as native

        seen = set()
        for package in (*tap._trusted_catalog(), *native._trusted_catalog()):
            identity = tap.DeviceIdentity(package.firmware, package.platform,
                                          package.architecture, package.xochitl_sha256)
            if identity in seen:
                continue
            seen.add(identity)
            runtime, trusted, _legacy = tap._trusted_shared_context(identity)
            enabled = (next(iter(trusted.values())),)
            for historical in (False, True):
                with self.subTest(identity=identity, historical=historical):
                    launcher = shared.shared_launcher(runtime, enabled, clean_stop_guard=not historical)
                    result = subprocess.run([SH, "-n"], input=launcher, text=True,
                                            capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    unit = shared.shared_dropin(runtime, enabled, clean_stop_guard=not historical)
                    hooks = [line for line in unit.splitlines() if line.startswith("ExecStopPost=")]
                    self.assertEqual(len(hooks), 0 if historical else 1)
                    for hook in hooks:
                        command = shlex.split(hook.split("=", 1)[1])[2].replace("$$", "$").replace("%%", "%")
                        result = subprocess.run([SH, "-n"], input=command, text=True,
                                                capture_output=True, timeout=20)
                        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({identity.platform for identity in seen}, {"rm1", "rm2", "ferrari", "chiappa", "tatsu"})


if __name__ == "__main__":
    unittest.main()
