import inspect
import unittest
from unittest import mock

import _fast_mono_reading as fast
import _legacy_vellum as legacy
import _tap_page_turn as tap
import _xovi_standalone


class FakeSSH:
    def __init__(self, *, vellum=True):
        self.vellum = vellum

    def file_exists(self, path):
        return self.vellum and path == tap.VELLUM_BIN


class LegacyVellumCleanupTests(unittest.TestCase):
    def test_all_valid_packages_are_validated_before_either_removal(self):
        events = []
        installed = {*tap.RMTOOL_VELLUM_PACKAGE_NAMES, "third-party-reader"}
        with mock.patch.object(
            _xovi_standalone, "has_shared_artifacts", return_value=False
        ), mock.patch.object(
            tap, "_vellum_installed_packages", return_value=installed
        ), mock.patch.object(
            tap, "_validate_vellum_removal", side_effect=lambda _ssh: events.append("validate-tap")
        ), mock.patch.object(
            fast, "_validate_vellum_removal", side_effect=lambda _ssh: events.append("validate-fast")
        ), mock.patch.object(
            tap, "_remove_validated_vellum", side_effect=lambda _ssh: events.append("remove-tap")
        ), mock.patch.object(
            fast, "_remove_validated_vellum", side_effect=lambda _ssh: events.append("remove-fast")
        ):
            removed = legacy.remove_legacy_plugins(FakeSSH())

        self.assertEqual(removed, tap.RMTOOL_VELLUM_PACKAGE_NAMES)
        self.assertEqual(
            events,
            ["validate-tap", "validate-fast", "remove-tap", "remove-fast"],
        )

    def test_none_found_is_a_read_only_noop(self):
        with mock.patch.object(
            tap, "_vellum_installed_packages", return_value={"third-party-reader"}
        ), mock.patch.object(tap, "_validate_vellum_removal") as validate_tap, mock.patch.object(
            fast, "_validate_vellum_removal"
        ) as validate_fast, mock.patch.object(
            tap, "_remove_validated_vellum"
        ) as remove_tap, mock.patch.object(
            fast, "_remove_validated_vellum"
        ) as remove_fast:
            self.assertEqual(legacy.remove_legacy_plugins(FakeSSH()), ())

        for operation in (validate_tap, validate_fast, remove_tap, remove_fast):
            operation.assert_not_called()

    def test_one_valid_package_removes_only_that_package(self):
        events = []
        with mock.patch.object(
            _xovi_standalone, "has_shared_artifacts", return_value=False
        ), mock.patch.object(
            tap, "_vellum_installed_packages", return_value={fast.VELLUM_PACKAGE_NAME}
        ), mock.patch.object(tap, "_validate_vellum_removal") as validate_tap, mock.patch.object(
            fast,
            "_validate_vellum_removal",
            side_effect=lambda _ssh: events.append("validate-fast"),
        ), mock.patch.object(tap, "_remove_validated_vellum") as remove_tap, mock.patch.object(
            fast,
            "_remove_validated_vellum",
            side_effect=lambda _ssh: events.append("remove-fast"),
        ):
            removed = legacy.remove_legacy_plugins(FakeSSH())

        self.assertEqual(removed, (fast.VELLUM_PACKAGE_NAME,))
        self.assertEqual(events, ["validate-fast", "remove-fast"])
        validate_tap.assert_not_called()
        remove_tap.assert_not_called()

    def test_shared_runtime_blocks_every_removal_before_validation(self):
        with mock.patch.object(
            tap,
            "_vellum_installed_packages",
            return_value=set(tap.RMTOOL_VELLUM_PACKAGE_NAMES),
        ), mock.patch.object(
            _xovi_standalone, "has_shared_artifacts", return_value=True
        ), mock.patch.object(tap, "_validate_vellum_removal") as validate_tap, mock.patch.object(
            fast, "_validate_vellum_removal"
        ) as validate_fast, mock.patch.object(
            tap, "_remove_validated_vellum"
        ) as remove_tap, mock.patch.object(
            fast, "_remove_validated_vellum"
        ) as remove_fast:
            with self.assertRaisesRegex(RuntimeError, "共享 Xovi"):
                legacy.remove_legacy_plugins(FakeSSH())

        for operation in (validate_tap, validate_fast, remove_tap, remove_fast):
            operation.assert_not_called()

    def test_one_invalid_package_blocks_every_removal(self):
        events = []
        with mock.patch.object(
            _xovi_standalone, "has_shared_artifacts", return_value=False
        ), mock.patch.object(
            tap,
            "_vellum_installed_packages",
            return_value=set(tap.RMTOOL_VELLUM_PACKAGE_NAMES),
        ), mock.patch.object(
            tap, "_validate_vellum_removal", side_effect=lambda _ssh: events.append("validate-tap")
        ), mock.patch.object(
            fast,
            "_validate_vellum_removal",
            side_effect=RuntimeError("快速黑白标记或哈希不可信"),
        ), mock.patch.object(tap, "_remove_validated_vellum") as remove_tap, mock.patch.object(
            fast, "_remove_validated_vellum"
        ) as remove_fast:
            with self.assertRaisesRegex(RuntimeError, "不可信"):
                legacy.remove_legacy_plugins(FakeSSH())

        self.assertEqual(events, ["validate-tap"])
        remove_tap.assert_not_called()
        remove_fast.assert_not_called()

    def test_second_removal_failure_reports_confirmed_partial_result(self):
        with mock.patch.object(
            _xovi_standalone, "has_shared_artifacts", return_value=False
        ), mock.patch.object(
            tap,
            "_vellum_installed_packages",
            return_value=set(tap.RMTOOL_VELLUM_PACKAGE_NAMES),
        ), mock.patch.object(
            tap, "_validate_vellum_removal"
        ), mock.patch.object(
            fast, "_validate_vellum_removal"
        ), mock.patch.object(
            tap, "_remove_validated_vellum"
        ) as remove_tap, mock.patch.object(
            fast,
            "_remove_validated_vellum",
            side_effect=RuntimeError("Vellum 命令失败"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                legacy.remove_legacy_plugins(FakeSSH())

        message = str(caught.exception)
        self.assertIn(tap.VELLUM_PACKAGE_NAME, message)
        self.assertIn(fast.VELLUM_PACKAGE_NAME, message)
        self.assertIn("卸载结果无法确认", message)
        remove_tap.assert_called_once()

    def test_cleanup_has_no_runtime_or_global_uninstall_path(self):
        source = inspect.getsource(legacy)
        self.assertNotIn("self uninstall", source)
        self.assertNotIn("--all", source)
        self.assertNotIn("remove_shared", source)
        self.assertEqual(
            set(tap.RMTOOL_VELLUM_PACKAGE_NAMES),
            {tap.VELLUM_PACKAGE_NAME, fast.VELLUM_PACKAGE_NAME},
        )


if __name__ == "__main__":
    unittest.main()
