import unittest
from unittest.mock import patch

from PyQt5 import QtCore, QtWidgets

import rmtool
import _rmkit_cn as cn
import _toolbox_fonts as fonts
import _toolbox_localization as localization


APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class ConnectedSSH(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def is_connected(self):
        return True


class ToolboxFontLocalizationTests(unittest.TestCase):
    def setUp(self):
        self.ssh = ConnectedSSH()

    def test_localization_recovery_is_visible_and_only_restore_is_enabled(self):
        section = localization.RmkitCnSection(self.ssh)
        self.addCleanup(section.deleteLater)
        section._apply_status(cn.LocalizationStatus(
            cn.LocalizationState.RECOVERY_REQUIRED, cn.SUPPORTED_FIRMWARE,
            package=cn._default_translation_package(), detail="备份待恢复",
        ))
        self.assertTrue(section.restore_button.isEnabled())
        self.assertFalse(section.enable_button.isEnabled())
        self.assertIn("备份待恢复", section.status_label.text())

    def test_font_transaction_recovery_does_not_require_font_selection(self):
        tab = fonts.FontTab(self.ssh, rmtool._default_config())
        self.addCleanup(tab.deleteLater)
        tab._font_recovery = cn.FontRecoveryStatus("managed", owner=cn.FONT_OWNER_NATIVE_CHINESE)
        tab._update_action_buttons()
        self.assertTrue(tab.set_active_button.isEnabled())
        with patch.object(fonts._ui, "ask_confirmation", return_value=True), patch.object(
            tab, "_start_font_worker"
        ) as worker, patch.object(cn, "recover_managed_font") as recover:
            tab._set_selected_active()
            fn, ssh = worker.call_args.args
            fn(ssh)
        recover.assert_called_once_with(
            self.ssh, expected_owner=cn.FONT_OWNER_NATIVE_CHINESE, expected_fingerprint=None
        )

    def test_stale_epub_index_repair_is_available_without_font_selection(self):
        tab = fonts.FontTab(self.ssh, rmtool._default_config())
        self.addCleanup(tab.deleteLater)
        tab._epub_font_status = cn.EpubFontSlotStatus("stale", "missing font")
        tab._update_action_buttons()
        self.assertTrue(tab.epub_font_button.isEnabled())
        with patch.object(fonts._ui, "ask_confirmation", return_value=True), patch.object(
            tab, "_start_font_worker"
        ) as worker:
            tab._toggle_selected_epub_font()
        self.assertEqual(worker.call_args.args, (cn.repair_epub_font_index, self.ssh))

    def test_cancelled_recovery_never_starts_worker(self):
        tab = fonts.FontTab(self.ssh, rmtool._default_config())
        self.addCleanup(tab.deleteLater)
        tab._font_recovery = cn.FontRecoveryStatus("managed", owner=cn.FONT_OWNER_RMKIT)
        with patch.object(fonts._ui, "ask_confirmation", return_value=False), patch.object(
            tab, "_start_font_worker"
        ) as worker:
            tab._set_selected_active()
        worker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
