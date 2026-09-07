import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtGui, QtWidgets
import rmtool
import _firmware as f
import _residue_migration as residue_migration
from _tab_firmware import FirmwareTab
from tests.test_firmware import state_text


class UIConnection(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.connected = False

    def is_connected(self):
        return self.connected


class FirmwareUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.original_font = cls.app.font()
        font = Path(__file__).resolve().parents[1] / "assets/fonts/NotoSansCJKsc-Regular.otf"
        font_id = QtGui.QFontDatabase.addApplicationFont(str(font))
        families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
        if families:
            cls.app.setFont(QtGui.QFont(families[0], 10))

    @classmethod
    def tearDownClass(cls):
        cls.app.setFont(cls.original_font)

    def setUp(self):
        self.ssh = UIConnection()
        self.page = FirmwareTab(self.ssh)

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def test_offline_download_and_local_remain_available(self):
        self.assertTrue(self.page.buttons["local"].isEnabled())
        self.assertTrue(self.page.buttons["list"].isEnabled())
        self.assertFalse(self.page.buttons["install"].isEnabled())
        self.assertFalse(self.page.buttons["reboot"].isEnabled())
        self.assertFalse(self.page.advanced.isVisible())
        self.assertNotIn("pause", self.page.buttons)

    def test_advanced_options_expand_on_demand(self):
        self.page.show()
        self.page.advanced_toggle.setChecked(True)
        self.app.processEvents()
        self.assertTrue(self.page.advanced.isVisible())
        self.assertEqual(self.page.advanced_toggle.arrowType(), QtCore.Qt.DownArrow)
        self.page.advanced_toggle.setChecked(False)
        self.app.processEvents()
        self.assertFalse(self.page.advanced.isVisible())

    def test_primary_status_hides_technical_details(self):
        state = f.parse_state(state_text())
        standby = {"version": "3.27.3.0"}
        self.page._device_loaded((state, ("none", "没有固件事务"), standby, ""))
        self.page._update()
        self.assertIn("Paper Pro Move", self.page.status.text())
        self.assertNotIn("chiappa", self.page.status.text())
        self.assertNotIn("当前 A", self.page.status.text())
        self.assertEqual(self.page.partition_cards["a"]._badge.text(), "当前运行")
        self.assertEqual(self.page.partition_cards["a"]._version.text(), "3.28.0.169")
        self.assertEqual(self.page.partition_cards["b"]._badge.text(), "备用")
        self.assertEqual(self.page.partition_cards["b"]._version.text(), "3.27.3.0")

    def test_reboot_requires_confirmed_durable_success(self):
        self.ssh.connected = True
        for status in ("none", "unknown", "failed", "running", "completed", "success"):
            self.page.transaction = (status, "state")
            self.page._update()
            self.assertEqual(self.page.buttons["reboot"].isEnabled(), status == "success")
            self.assertEqual(self.page.buttons["switch"].isEnabled(), status in ("none", "completed"))

    def test_running_transaction_polls_and_success_offers_reboot(self):
        with mock.patch.object(self.page, "_schedule_transaction_poll") as schedule:
            self.page._transaction_loaded(("running", "设备端事务仍在运行"))
        schedule.assert_called_once_with()

        with mock.patch("_tab_firmware.ask_confirmation", return_value=False) as confirm:
            self.page._transaction_loaded(("success", "设备端操作成功；尚未重启"))
        confirm.assert_called_once()
        self.assertEqual(confirm.call_args.kwargs["confirm_text"], "立即重启")
        self.assertEqual(confirm.call_args.kwargs["cancel_text"], "稍后重启")

    def test_success_dialog_can_reboot_without_second_confirmation(self):
        with mock.patch("_tab_firmware.ask_confirmation", return_value=True), \
                mock.patch.object(self.page, "_reboot_device") as reboot:
            self.page._transaction_loaded(("success", "设备端操作成功；尚未重启"))
        reboot.assert_called_once_with()

    def test_plugin_restore_only_appears_for_detected_residue(self):
        self.ssh.connected = True
        self.page.transaction = ("completed", "已进入新固件")
        self.page._update()
        self.assertTrue(self.page.buttons["restore_plugins"].isHidden())
        report = residue_migration.ResidueReport(
            mock.Mock(), mock.Mock(), (), True, (), "可以恢复"
        )
        self.page.restore_report = report
        self.page._update()
        self.assertFalse(self.page.buttons["restore_plugins"].isHidden())
        self.assertTrue(self.page.buttons["restore_plugins"].isEnabled())
        self.page.restore_report = residue_migration.ResidueReport(
            mock.Mock(), mock.Mock(), (), False, ("缺少精确包",), "不能恢复"
        )
        self.page._update()
        self.assertTrue(self.page.buttons["restore_plugins"].isEnabled())

    def test_completed_firmware_schedules_restore_detection(self):
        self.ssh.connected = True
        state = f.parse_state(state_text())
        with mock.patch.object(QtCore.QTimer, "singleShot") as schedule:
            self.page._device_loaded((state, ("completed", "已进入新固件"), None, "无法读取"))
        schedule.assert_called_once_with(0, self.page._detect_plugin_restore)

    def test_partition_cards_stack_in_narrow_view(self):
        self.page.resize(560, 700)
        self.page.show()
        self.app.processEvents()
        _row_a, column_a, _row_span, _column_span = self.page.partition_grid.getItemPosition(0)
        row_b, column_b, _row_span, _column_span = self.page.partition_grid.getItemPosition(1)
        self.assertEqual((column_a, row_b, column_b), (0, 1, 0))

    def test_plugin_restore_confirms_or_explains_blockers(self):
        blocked = residue_migration.ResidueReport(
            mock.Mock(), mock.Mock(), (), False, ("阅读增强暂无精确包",), "不能恢复"
        )
        self.page.restore_report = blocked
        with mock.patch("_tab_firmware.show_error") as error:
            self.page.restore_plugins()
        self.assertIn("阅读增强暂无精确包", error.call_args.args[2])

        feature = residue_migration.ResidueFeatureReport(
            "reading-enhancements", "阅读增强", True, True
        )
        self.page.restore_report = residue_migration.ResidueReport(
            mock.Mock(), mock.Mock(), (feature,), True, (), "可以恢复"
        )
        with mock.patch("_tab_firmware.ask_confirmation", return_value=True), \
                mock.patch.object(self.page, "_run") as run:
            self.page.restore_plugins()
        run.assert_called_once()

    def test_progress_is_determinate(self):
        self.page._progress(45, 100)
        self.assertEqual(self.page.progress.value(), 45)
        self.assertEqual(self.page.progress.maximum(), 100)

    def test_compact_and_normal_screenshots(self):
        old = self.app.styleSheet()
        self.addCleanup(self.app.setStyleSheet, old)
        self.app.setStyleSheet(rmtool._resolve_stylesheet(rmtool._LIGHT_STYLESHEET))
        self.ssh.connected = True
        state = f.parse_state(state_text())
        self.page._device_loaded((
            state,
            ("none", "没有正在进行的固件操作"),
            {"version": "3.27.3.0"},
            "",
        ))
        self.page._image_loaded(f.Image(Path("remarkable-production-image-3.28.0.172-chiappa-public.swu"),
                                       "3.28.0.172", "chiappa", 100, "a" * 64, 100))
        self.page._update()
        destination = os.environ.get("RMTOOL_FIRMWARE_SCREENSHOTS")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(destination or temporary)
            root.mkdir(parents=True, exist_ok=True)
            for width, height in ((660, 480), (980, 700)):
                self.page.resize(width, height)
                self.page.show()
                self.app.processEvents()
                for button in self.page.buttons.values():
                    self.assertGreaterEqual(button.width(), button.fontMetrics().horizontalAdvance(button.text()) + 12)
                pixmap = self.page.grab()
                self.assertFalse(pixmap.isNull())
                self.assertTrue(pixmap.save(str(root / f"firmware-page-{width}.png")))


if __name__ == "__main__":
    unittest.main()
