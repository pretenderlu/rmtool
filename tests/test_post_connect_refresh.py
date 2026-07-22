"""Post-connect serial refresh coordinator tests.

Connecting used to start the documents, wallpaper-preview, and font
background workers at once; the concurrent SSH channels made the device's
dropbear server drop the connection (2026-07-22 incident). These tests pin
the serial coordinator and the quiet per-tab refresh interfaces.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets

import rmtool
import _rmkit_cn
import _tab_documents
import _tab_toolbox


_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FakeSSHClient(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self, *, connected=False):
        super().__init__()
        self._connected = connected

    def is_connected(self):
        return self._connected


class RecordingStep:
    """Fake quiet refresh step: records its start, completes via callback."""

    def __init__(self, name, log):
        self.name = name
        self.log = log
        self.callbacks = []

    def __call__(self, on_done):
        self.log.append(f"start:{self.name}")
        self.callbacks.append(on_done)


def make_window_with_fake_steps():
    window = rmtool.MainWindow()
    log = []
    steps = {
        "documents": RecordingStep("documents", log),
        "wallpaper": RecordingStep("wallpaper", log),
        "fonts": RecordingStep("fonts", log),
    }
    window.documents_tab.refresh_quiet = steps["documents"]
    window.wallpaper_tab.refresh_previews_quiet = steps["wallpaper"]
    window.font_tab.refresh_fonts_quiet = steps["fonts"]
    return window, log, steps


class PostConnectCoordinatorTests(unittest.TestCase):
    def test_steps_run_strictly_in_series(self):
        window, log, steps = make_window_with_fake_steps()
        self.addCleanup(window.deleteLater)

        window._start_post_connect_refresh()

        self.assertEqual(log, ["start:documents"])
        steps["documents"].callbacks[0]()
        self.assertEqual(log, ["start:documents", "start:wallpaper"])
        steps["wallpaper"].callbacks[0]()
        self.assertEqual(log, ["start:documents", "start:wallpaper", "start:fonts"])
        self.assertTrue(window._post_connect_active)
        steps["fonts"].callbacks[0]()
        self.assertFalse(window._post_connect_active)

    def test_reentrant_start_does_not_stack_sequences(self):
        window, log, steps = make_window_with_fake_steps()
        self.addCleanup(window.deleteLater)

        window._start_post_connect_refresh()
        window._start_post_connect_refresh()

        self.assertEqual(log, ["start:documents"])
        for name in ("documents", "wallpaper", "fonts"):
            steps[name].callbacks[0]()
        self.assertFalse(window._post_connect_active)

        # Once the sequence finishes, a new connect may start it again.
        window._start_post_connect_refresh()
        self.assertEqual(
            log,
            [
                "start:documents",
                "start:wallpaper",
                "start:fonts",
                "start:documents",
            ],
        )

    def test_failing_step_does_not_block_remaining_steps(self):
        window, log, steps = make_window_with_fake_steps()
        self.addCleanup(window.deleteLater)

        def broken_step(_on_done):
            raise RuntimeError("断开连接")

        window.wallpaper_tab.refresh_previews_quiet = broken_step

        window._start_post_connect_refresh()
        # The broken step raises synchronously; the coordinator logs it and
        # still advances to the fonts step.
        with self.assertLogs(level="ERROR"):
            steps["documents"].callbacks[0]()
        self.assertEqual(log, ["start:documents", "start:fonts"])
        steps["fonts"].callbacks[0]()
        self.assertFalse(window._post_connect_active)

    def test_connected_signal_drives_coordinator_not_documents_refresh(self):
        window, log, steps = make_window_with_fake_steps()
        self.addCleanup(window.deleteLater)

        with mock.patch.object(window.documents_tab, "refresh") as refresh:
            window.connection_widget.connected.emit()

        # The connected signal starts the serial chain (documents first)
        # instead of the old direct documents_tab.refresh wiring.
        self.assertEqual(log, ["start:documents"])
        self.assertEqual(len(steps["documents"].callbacks), 1)
        refresh.assert_not_called()

    def test_device_changed_routes_through_coordinator(self):
        window, log, steps = make_window_with_fake_steps()
        self.addCleanup(window.deleteLater)

        with mock.patch.object(
            window.ssh_client, "is_connected", return_value=True
        ), mock.patch.object(window.documents_tab, "refresh") as refresh:
            window._on_device_changed({"name": "Device A", "mode": "usb"})

        self.assertEqual(log, ["start:documents"])
        refresh.assert_not_called()


class DocumentsRefreshQuietTests(unittest.TestCase):
    def _make_tab(self):
        tab = rmtool.DocumentsTab(FakeSSHClient(connected=True))
        self.addCleanup(tab.deleteLater)
        return tab

    def _start_quiet(self, tab):
        worker_instance = mock.Mock()
        worker_instance.signals = mock.Mock()
        done = []
        with mock.patch.object(
            rmtool, "Worker", return_value=worker_instance
        ), mock.patch.object(tab.thread_pool, "start"):
            tab.refresh_quiet(lambda: done.append(True))
        return worker_instance, done

    def test_success_applies_documents_and_calls_on_done_once(self):
        tab = self._make_tab()
        worker_instance, done = self._start_quiet(tab)

        on_finished = worker_instance.signals.finished.connect.call_args.args[0]
        on_finished([])

        self.assertEqual(done, [True])
        self.assertEqual(tab.table.rowCount(), 0)

    def test_error_logs_and_calls_on_done_once_without_dialog(self):
        tab = self._make_tab()
        messages = []
        tab.status_message.connect(lambda *args: messages.append(args))
        worker_instance, done = self._start_quiet(tab)

        on_error = worker_instance.signals.error.connect.call_args.args[0]
        with mock.patch.object(_tab_documents, "show_error") as show_error:
            with self.assertLogs(level="ERROR"):
                on_error(RuntimeError("读取失败"))

        self.assertEqual(done, [True])
        show_error.assert_not_called()
        self.assertTrue(any("文档列表刷新失败" in args[1] for args in messages))

    def test_not_connected_returns_immediately_without_worker(self):
        tab = rmtool.DocumentsTab(FakeSSHClient(connected=False))
        self.addCleanup(tab.deleteLater)
        done = []

        with mock.patch.object(tab.thread_pool, "start") as start_worker:
            tab.refresh_quiet(lambda: done.append(True))

        self.assertEqual(done, [True])
        start_worker.assert_not_called()

    def test_stale_result_after_disconnect_is_discarded(self):
        tab = self._make_tab()
        tab.set_connection_state(True)
        worker_instance, done = self._start_quiet(tab)

        # Disconnecting invalidates results of workers started before it.
        tab.set_connection_state(False)
        on_finished = worker_instance.signals.finished.connect.call_args.args[0]
        with mock.patch.object(tab, "_on_documents_loaded") as apply:
            on_finished([])

        self.assertEqual(done, [True])
        apply.assert_not_called()

    def test_apply_exception_still_calls_on_done_once(self):
        tab = self._make_tab()
        worker_instance, done = self._start_quiet(tab)

        on_finished = worker_instance.signals.finished.connect.call_args.args[0]
        with mock.patch.object(
            tab, "_on_documents_loaded", side_effect=RuntimeError("界面异常")
        ):
            with self.assertLogs(level="ERROR"):
                on_finished([])

        # The coordinator chain must advance even when applying results fails.
        self.assertEqual(done, [True])


class WallpaperRefreshQuietTests(unittest.TestCase):
    def test_not_connected_returns_immediately_without_worker(self):
        tab = rmtool.WallpaperTab(
            FakeSSHClient(connected=False), rmtool._default_config()
        )
        self.addCleanup(tab.deleteLater)
        done = []

        with mock.patch.object(tab.thread_pool, "start") as start_worker:
            tab.refresh_previews_quiet(lambda: done.append(True))

        self.assertEqual(done, [True])
        start_worker.assert_not_called()

    def test_error_calls_on_done_once_without_dialog(self):
        tab = rmtool.WallpaperTab(
            FakeSSHClient(connected=True), rmtool._default_config()
        )
        self.addCleanup(tab.deleteLater)
        worker_instance = mock.Mock()
        worker_instance.signals = mock.Mock()
        done = []

        with mock.patch.object(
            rmtool, "Worker", return_value=worker_instance
        ), mock.patch.object(tab.thread_pool, "start"):
            tab.refresh_previews_quiet(lambda: done.append(True))

        self.assertTrue(
            all(
                preview.text() == "加载中…"
                for preview in tab.variant_previews.values()
            )
        )
        on_error = worker_instance.signals.error.connect.call_args.args[0]
        with self.assertLogs(level="ERROR"):
            on_error(RuntimeError("通道断开"))

        self.assertEqual(done, [True])
        self.assertTrue(
            all(
                preview.text() == "加载失败"
                for preview in tab.variant_previews.values()
            )
        )

    def test_connect_no_longer_auto_refreshes_previews(self):
        tab = rmtool.WallpaperTab(
            FakeSSHClient(connected=False), rmtool._default_config()
        )
        self.addCleanup(tab.deleteLater)

        with mock.patch.object(
            tab, "_refresh_variant_previews"
        ) as refresh, mock.patch.object(tab.thread_pool, "start") as start_worker:
            tab._on_connection_changed(True)

        refresh.assert_not_called()
        start_worker.assert_not_called()

    def test_stale_result_after_disconnect_is_discarded(self):
        tab = rmtool.WallpaperTab(
            FakeSSHClient(connected=True), rmtool._default_config()
        )
        self.addCleanup(tab.deleteLater)
        tab._on_connection_changed(True)
        worker_instance = mock.Mock()
        worker_instance.signals = mock.Mock()
        done = []

        with mock.patch.object(
            rmtool, "Worker", return_value=worker_instance
        ), mock.patch.object(tab.thread_pool, "start"):
            tab.refresh_previews_quiet(lambda: done.append(True))

        # Disconnecting invalidates results of workers started before it.
        tab._on_connection_changed(False)
        on_finished = worker_instance.signals.finished.connect.call_args.args[0]
        with mock.patch.object(tab, "_apply_variant_previews") as apply:
            on_finished({})

        self.assertEqual(done, [True])
        apply.assert_not_called()


class FontsRefreshQuietTests(unittest.TestCase):
    def _make_tab(self):
        tab = rmtool.FontTab(
            FakeSSHClient(connected=True), rmtool._default_config()
        )
        self.addCleanup(tab.deleteLater)
        return tab

    def _start_quiet(self, tab):
        worker_instance = mock.Mock()
        worker_instance.signals = mock.Mock()
        done = []
        with mock.patch.object(
            rmtool, "Worker", return_value=worker_instance
        ) as worker_cls, mock.patch.object(
            _tab_toolbox.QtWidgets, "QProgressDialog"
        ) as progress_cls, mock.patch.object(
            tab.thread_pool, "start"
        ):
            tab.refresh_fonts_quiet(lambda: done.append(True))
        # The quiet path never opens the modal font progress dialog.
        progress_cls.assert_not_called()
        worker_cls.assert_called_once_with(
            _rmkit_cn.list_user_fonts, tab.ssh_client, tab._font_dir()
        )
        return worker_instance, done

    def test_busy_queues_pending_refresh_and_finishes_immediately(self):
        tab = self._make_tab()
        tab._busy = True
        done = []

        with mock.patch.object(tab.thread_pool, "start") as start_worker:
            tab.refresh_fonts_quiet(lambda: done.append(True))

        self.assertEqual(done, [True])
        self.assertEqual(tab._pending_refresh, ("", ""))
        start_worker.assert_not_called()

    def test_success_applies_inventory_without_modal_progress(self):
        tab = self._make_tab()
        worker_instance, done = self._start_quiet(tab)

        on_finished = worker_instance.signals.finished.connect.call_args.args[0]
        font = _rmkit_cn.UserFont(
            "alpha.ttf", "Alpha", f"{rmtool.DEFAULT_FONT_DIR}alpha.ttf", False
        )
        on_finished((font,))

        self.assertEqual(done, [True])
        self.assertEqual(tab.font_table.rowCount(), 1)
        self.assertEqual(tab.font_table.item(0, 0).text(), "alpha.ttf")

    def test_error_updates_status_label_and_calls_on_done_once(self):
        tab = self._make_tab()
        worker_instance, done = self._start_quiet(tab)

        on_error = worker_instance.signals.error.connect.call_args.args[0]
        with self.assertLogs(level="ERROR"):
            on_error(RuntimeError("读取字体失败"))

        self.assertEqual(done, [True])
        self.assertIn("字体列表刷新失败", tab.manager_status_label.text())

    def test_stale_result_after_reconnect_is_discarded(self):
        tab = self._make_tab()
        worker_instance, done = self._start_quiet(tab)

        tab.ssh_client._connected = False
        tab._on_connection_changed(False)
        on_finished = worker_instance.signals.finished.connect.call_args.args[0]
        font = _rmkit_cn.UserFont(
            "stale.ttf", "Stale", f"{rmtool.DEFAULT_FONT_DIR}stale.ttf", False
        )
        on_finished((font,))

        self.assertEqual(done, [True])
        self.assertEqual(tab.font_table.rowCount(), 0)
        self.assertEqual(tab.manager_status_label.text(), "设备未连接。")

    def test_quiet_refresh_holds_busy_until_done(self):
        tab = self._make_tab()
        worker_instance, done = self._start_quiet(tab)

        # While the quiet worker is in flight, manual refresh/upload must see
        # the tab as busy so no second SSH channel is opened alongside it.
        self.assertTrue(tab._busy)
        on_finished = worker_instance.signals.finished.connect.call_args.args[0]
        on_finished(())

        self.assertFalse(tab._busy)
        self.assertEqual(done, [True])

    def test_manual_refresh_during_quiet_runs_after_it(self):
        tab = self._make_tab()
        quiet_worker = mock.Mock()
        quiet_worker.signals = mock.Mock()
        manual_worker = mock.Mock()
        manual_worker.signals = mock.Mock()
        done = []

        with mock.patch.object(
            rmtool, "Worker", side_effect=[quiet_worker, manual_worker]
        ) as worker_cls, mock.patch.object(
            _tab_toolbox.QtWidgets, "QProgressDialog"
        ), mock.patch.object(
            tab.thread_pool, "start"
        ):
            tab.refresh_fonts_quiet(lambda: done.append(True))
            # A manual refresh while the quiet one is busy is queued, not
            # started concurrently.
            tab._refresh_fonts()
            self.assertEqual(worker_cls.call_count, 1)

            on_finished = quiet_worker.signals.finished.connect.call_args.args[0]
            on_finished(())
            # After the quiet refresh finishes, the queued manual refresh runs
            # (with its usual modal progress).
            self.assertEqual(worker_cls.call_count, 2)
            self.assertTrue(tab._busy)

        self.assertEqual(done, [True])


if __name__ == "__main__":
    unittest.main()
