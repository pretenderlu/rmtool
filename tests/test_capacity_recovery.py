import unittest
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _capacity_recovery as recovery
import _rmkit_cn
import _tap_page_turn as tap
import _xovi_standalone as shared


IDENTITY = tap.DeviceIdentity("firmware", "rm2", "armv7l", "a" * 64)
PACKAGE = SimpleNamespace(package_id="rm2-package")
CONTEXT = recovery.CapacityContext("shared-capacity", "/", 100, 40, 60)
FEATURE = shared.SharedFeatureSpec(
    "pinyin-input", "package", "pinyin.qmd", "pinyin.qmd", "e" * 64, 1, 0o644
)
ENABLE_PLAN = shared.SharedEnablePlan(
    shared.SharedInspection({}, False, True),
    {FEATURE.feature_id: FEATURE},
    {FEATURE.feature_id: shared.SharedFeatureState(FEATURE, True, "token")},
    {},
    (),
)
CHANGED_ENABLE_PLAN = replace(
    ENABLE_PLAN,
    states={
        FEATURE.feature_id: shared.SharedFeatureState(
            replace(FEATURE, size=2), True, "token"
        ),
    },
)
SNAPSHOT = _rmkit_cn.HomeOnlyFontState(
    "/home/root/.local/share/fonts",
    "selected.ttf",
    "/home/root/.local/share/fonts/selected.ttf",
    "/data/rmtool/fonts/active-ui-font.ttf",
    "Selected",
    "b" * 64,
    "c" * 64,
    "d" * 64,
    16_000_000,
)


def capacity_error(context=CONTEXT, enable_plan=ENABLE_PLAN):
    error = RuntimeError("容量错误文本不参与路由")
    error.recovery_context = {
        "kind": context.kind,
        "partition": context.partition,
        "required": context.required,
        "available": context.available,
        "shortfall": context.shortfall,
    }
    error.shared_enable_plan = enable_plan
    return error


class CapacityRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.ssh = Mock()
        self.ssh._client = object()
        self.ssh.operation_session.return_value = nullcontext()
        self.intent = recovery.make_original_intent(
            self.ssh, "pinyin-input", PACKAGE, ".rmtool", IDENTITY
        )
        self.plan = recovery.CapacityPlan(
            self.intent, CONTEXT, recovery.HomeOnlyFontPlan(SNAPSHOT), ENABLE_PLAN
        )
        self.identity = patch.object(tap, "get_device_identity", return_value=IDENTITY)
        self.identity.start()
        self.addCleanup(self.identity.stop)
        self.capacity_probe = patch.object(
            recovery.storage, "system_free_bytes", return_value=("/", 100)
        )
        self.capacity_probe.start()
        self.addCleanup(self.capacity_probe.stop)

    def test_structured_context_never_uses_error_text(self):
        self.assertEqual(recovery.capacity_context(capacity_error()), CONTEXT)
        for error in (
            RuntimeError("/ 空间不足"),
            capacity_error(),
        ):
            if getattr(error, "recovery_context", None):
                error.recovery_context["shortfall"] += 1
            self.assertIsNone(recovery.capacity_context(error))

    def test_scan_only_inspects_font_for_fresh_shared_capacity(self):
        with patch.object(recovery, "_run_preflight"), patch.object(
            _rmkit_cn, "inspect_home_only_font"
        ) as inspect:
            result = recovery.scan(self.ssh, self.intent)
        self.assertEqual(result.state, "not_needed")
        inspect.assert_not_called()

        with patch.object(
            recovery, "_run_preflight", side_effect=RuntimeError("font prerequisite")
        ), patch.object(_rmkit_cn, "inspect_home_only_font") as inspect:
            result = recovery.scan(self.ssh, self.intent)
        self.assertEqual(result.state, "preflight_failed")
        inspect.assert_not_called()

        with patch.object(
            recovery, "_run_preflight", side_effect=capacity_error()
        ), patch.object(
            _rmkit_cn, "inspect_home_only_font", side_effect=RuntimeError("unknown")
        ), patch.object(_rmkit_cn, "set_active_user_font") as mutate:
            result = recovery.scan(self.ssh, self.intent)
        self.assertEqual(result.state, "no_candidate")
        self.assertEqual(result.capacity, CONTEXT)
        mutate.assert_not_called()

    def test_execute_success_runs_original_entry_exactly_once(self):
        with patch.object(
            recovery, "_run_preflight", side_effect=(capacity_error(), ENABLE_PLAN)
        ), patch.object(
            _rmkit_cn, "inspect_home_only_font", return_value=SNAPSHOT
        ), patch.object(_rmkit_cn, "set_active_user_font") as mutate, patch.object(
            recovery, "_run_original", return_value=object()
        ) as original:
            result = recovery.execute(self.ssh, self.plan)
        self.assertEqual(result.state, "deployed")
        self.assertTrue(result.cleaned)
        mutate.assert_called_once_with(
            self.ssh,
            SNAPSHOT.remote_dir,
            SNAPSHOT.filename,
            False,
            expected_home_only=SNAPSHOT,
        )
        original.assert_called_once_with(self.intent)
        self.assertEqual(result.before_capacity, CONTEXT)
        self.assertEqual(result.after_capacity.available, 100)
        self.assertEqual(result.after_capacity.required, CONTEXT.required)

    def test_same_required_enable_plan_drift_stops_before_and_after_cleanup(self):
        self.assertNotEqual(
            self.plan.fingerprint,
            recovery.CapacityPlan(
                self.intent, CONTEXT, self.plan.home_only, CHANGED_ENABLE_PLAN
            ).fingerprint,
        )
        with patch.object(
            recovery,
            "_run_preflight",
            side_effect=capacity_error(enable_plan=CHANGED_ENABLE_PLAN),
        ), patch.object(_rmkit_cn, "set_active_user_font") as mutate, patch.object(
            recovery, "_run_original"
        ) as original:
            result = recovery.execute(self.ssh, self.plan)
        self.assertEqual(result.state, "state_drift")
        mutate.assert_not_called()
        original.assert_not_called()

        with patch.object(
            recovery,
            "_run_preflight",
            side_effect=(capacity_error(), CHANGED_ENABLE_PLAN),
        ), patch.object(
            _rmkit_cn, "inspect_home_only_font", return_value=SNAPSHOT
        ), patch.object(_rmkit_cn, "set_active_user_font") as mutate, patch.object(
            recovery, "_run_original"
        ) as original:
            result = recovery.execute(self.ssh, self.plan)
        self.assertEqual(result.state, "state_drift")
        self.assertTrue(result.cleaned)
        self.assertEqual(result.before_capacity, CONTEXT)
        self.assertEqual(result.after_capacity.available, 100)
        mutate.assert_called_once()
        original.assert_not_called()

    def test_execute_when_space_is_already_sufficient_does_not_mutate_or_resume(self):
        with patch.object(recovery, "_run_preflight"), patch.object(
            _rmkit_cn, "inspect_home_only_font"
        ) as inspect, patch.object(_rmkit_cn, "set_active_user_font") as mutate, patch.object(
            recovery, "_run_original"
        ) as original:
            result = recovery.execute(self.ssh, self.plan)
        self.assertEqual(result.state, "not_needed")
        inspect.assert_not_called()
        mutate.assert_not_called()
        original.assert_not_called()

    def test_cleanup_failure_and_state_drift_never_resume(self):
        with patch.object(
            recovery, "_run_preflight", side_effect=capacity_error()
        ), patch.object(
            _rmkit_cn, "inspect_home_only_font", return_value=SNAPSHOT
        ), patch.object(
            _rmkit_cn, "set_active_user_font", side_effect=RuntimeError("rollback")
        ), patch.object(recovery, "_run_original") as original:
            result = recovery.execute(self.ssh, self.plan)
        self.assertEqual(result.state, "cleanup_failed")
        original.assert_not_called()

        changed = _rmkit_cn.HomeOnlyFontState(
            SNAPSHOT.remote_dir,
            SNAPSHOT.filename,
            SNAPSHOT.remote_path,
            SNAPSHOT.mirror_path,
            SNAPSHOT.family,
            "0" * 64,
            SNAPSHOT.user_config_sha256,
            SNAPSHOT.system_config_sha256,
            SNAPSHOT.size,
        )
        with patch.object(
            recovery, "_run_preflight", side_effect=capacity_error()
        ), patch.object(
            _rmkit_cn, "inspect_home_only_font", return_value=changed
        ), patch.object(_rmkit_cn, "set_active_user_font") as mutate, patch.object(
            recovery, "_run_original"
        ) as original:
            result = recovery.execute(self.ssh, self.plan)
        self.assertEqual(result.state, "state_drift")
        mutate.assert_not_called()
        original.assert_not_called()

    def test_insufficient_and_other_post_cleanup_preflight_failures_do_not_resume(self):
        remaining = recovery.CapacityContext("shared-capacity", "/", 100, 50, 50)
        for error, expected in (
            (capacity_error(remaining), "insufficient"),
            (RuntimeError("peer changed"), "preflight_failed"),
        ):
            with self.subTest(expected=expected), patch.object(
                recovery, "_run_preflight", side_effect=(capacity_error(), error)
            ), patch.object(
                _rmkit_cn, "inspect_home_only_font", return_value=SNAPSHOT
            ), patch.object(_rmkit_cn, "set_active_user_font"), patch.object(
                recovery, "_run_original"
            ) as original:
                result = recovery.execute(self.ssh, self.plan)
            self.assertEqual(result.state, expected)
            self.assertTrue(result.cleaned)
            original.assert_not_called()

    def test_install_failure_is_not_retried(self):
        with patch.object(
            recovery, "_run_preflight", side_effect=(capacity_error(), ENABLE_PLAN)
        ), patch.object(
            _rmkit_cn, "inspect_home_only_font", return_value=SNAPSHOT
        ), patch.object(_rmkit_cn, "set_active_user_font"), patch.object(
            recovery, "_run_original", side_effect=RuntimeError("install failed")
        ) as original:
            result = recovery.execute(self.ssh, self.plan)
        self.assertEqual(result.state, "install_failed")
        self.assertTrue(result.cleaned)
        original.assert_called_once_with(self.intent)

    def test_reconnect_device_and_required_plan_drift_stop_before_mutation(self):
        self.ssh._client = object()
        with patch.object(recovery, "_run_preflight") as preflight, patch.object(
            _rmkit_cn, "set_active_user_font"
        ) as mutate:
            result = recovery.execute(self.ssh, self.plan)
        self.assertEqual(result.state, "state_drift")
        preflight.assert_not_called()
        mutate.assert_not_called()

        self.ssh._client = self.intent.client
        with patch.object(
            tap, "get_device_identity", return_value=tap.DeviceIdentity(
                "other", "rm2", "armv7l", "a" * 64
            )
        ), patch.object(_rmkit_cn, "set_active_user_font") as mutate:
            result = recovery.execute(self.ssh, self.plan)
        self.assertEqual(result.state, "state_drift")
        mutate.assert_not_called()

        with patch.object(
            recovery, "_run_preflight", side_effect=capacity_error(
                recovery.CapacityContext("shared-capacity", "/", 101, 40, 61)
            )
        ), patch.object(_rmkit_cn, "set_active_user_font") as mutate:
            result = recovery.execute(self.ssh, self.plan)
        self.assertEqual(result.state, "state_drift")
        mutate.assert_not_called()

    def test_fresh_intent_uses_current_status_package_and_client(self):
        status = SimpleNamespace(package=PACKAGE, identity=IDENTITY)
        with patch("_pinyin_input.get_status", return_value=status) as get_status:
            intent = recovery.fresh_intent(self.ssh, "pinyin-input", ".rmtool")
        get_status.assert_called_once()
        self.assertIs(intent.client, self.ssh._client)
        self.assertIs(intent.package, PACKAGE)
        self.assertEqual(intent.device, recovery._identity_tuple(IDENTITY))

    def test_original_dispatch_uses_public_cloud_entries(self):
        with patch("_pinyin_input.enable_cloud") as enable:
            recovery._run_original(self.intent)
        enable.assert_called_once_with(self.ssh, PACKAGE, ".rmtool")

        qr_intent = recovery.OriginalIntent(
            self.ssh,
            self.ssh._client,
            self.intent.device,
            "qr-upload",
            PACKAGE,
            ".rmtool",
        )
        with patch("_qr_upload.install_cloud") as install:
            recovery._run_original(qr_intent)
        install.assert_called_once_with(self.ssh, PACKAGE, ".rmtool")


if __name__ == "__main__":
    unittest.main()
