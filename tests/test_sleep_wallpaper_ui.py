import unittest
from unittest.mock import patch

from PIL import Image
from PyQt5 import QtWidgets

import rmtool
import _sleep_wallpaper as sleep
import _tab_wallpaper as wallpaper
from test_rmtool_behaviors import FakeConnectionClient


APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class SleepWallpaperUiTests(unittest.TestCase):
    def setUp(self):
        self.ssh = FakeConnectionClient(connected=True)
        self.tab = wallpaper.WallpaperTab(self.ssh, rmtool._default_config())
        self.addCleanup(self.tab.deleteLater)
        self.tab._cached_source_image = Image.new("RGB", (8, 8), "white")

    def test_broken_state_has_detail_recovery_and_blocks_root_fallback(self):
        self.tab._sleep_user_status = sleep.SleepWallpaperStatus(False, broken=True, detail="旧固件状态待恢复")
        self.tab._sync_user_partition_sleep_checkbox()
        self.tab._update_upload_button_state()
        self.assertFalse(self.tab.upload_button.isEnabled())
        self.assertFalse(self.tab.sleep_recovery_button.isHidden())
        self.assertIn("旧固件状态待恢复", self.tab.sleep_status_label.text())
        with patch.object(wallpaper, "show_warning"), patch.object(self.tab.thread_pool, "start") as start:
            self.tab._upload_wallpaper()
        start.assert_not_called()

    def test_failed_first_enable_clears_target_and_requires_rescan(self):
        self.tab._sleep_user_status = sleep.SleepWallpaperStatus(True)
        self.tab._sleep_user_requested = True
        self.tab._sync_user_partition_sleep_checkbox()
        with patch.object(self.tab, "_process_image", return_value=Image.new("RGB", (8, 8))), patch.object(
            self.tab.thread_pool, "start"
        ) as start, patch.object(wallpaper, "show_error"):
            self.tab._upload_wallpaper()
            start.call_args.args[0].signals.error.emit(RuntimeError("connection lost"))
        self.assertFalse(self.tab.user_partition_sleep_checkbox.isChecked())
        self.assertNotIn("用户分区管理", self.tab.target_label.text())
        self.assertTrue(self.tab._sleep_user_status.broken)
        self.assertFalse(self.tab.upload_button.isEnabled())

    def test_explicit_recovery_calls_core_then_refreshes_status(self):
        self.tab._sleep_user_status = sleep.SleepWallpaperStatus(True, broken=True, detail="pending")
        with patch.object(wallpaper, "ask_confirmation", return_value=True), patch.object(
            self.tab.thread_pool, "start"
        ) as start, patch.object(sleep, "recover") as recover, patch.object(
            sleep, "get_status", return_value=sleep.SleepWallpaperStatus(True)
        ), patch.object(wallpaper, "show_info"):
            self.tab._recover_user_partition_sleep()
            start.call_args.args[0].run()
        recover.assert_called_once_with(self.ssh)
        self.assertFalse(self.tab._sleep_user_status.broken)


if __name__ == "__main__":
    unittest.main()
