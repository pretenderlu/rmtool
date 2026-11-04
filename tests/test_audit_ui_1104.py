"""Offline regressions for review M21/M37 and the assigned UI Low findings."""

import shlex
import tempfile
import threading
import unittest
from io import BytesIO
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from PyQt5 import QtCore, QtTest, QtWidgets, sip

import rmtool
import _capacity_recovery as recovery
import _dialogs
import _koreader
import _rmkit_cn
import _tab_documents
import _tab_koreader
import _tab_screen_preview
import _tab_wallpaper
import _toolbox_common
import _toolbox_legacy
import _toolbox_recovery


APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FakeSSH(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self._client = object()
        self._state_lock = threading.RLock()
        self.commands = []

    def is_connected(self):
        return self._client is not None

    def operation_session(self):
        return nullcontext()

    def ensure_client(self):
        return self._client

    def close(self):
        self._client = None
        self.connection_changed.emit(False)

    def exec_checked(self, command, **_kwargs):
        self.commands.append(command)
        return ""


class AuditUiTests(unittest.TestCase):
    def widget(self, cls, *args):
        widget = cls(*args)
        self.addCleanup(widget.deleteLater)
        widget.thread_pool = Mock()
        return widget

    def test_plugin_disconnect_error_kept_but_replacement_discarded(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                ssh = FakeSSH()
                owner = self.widget(_toolbox_recovery.CapacityRecoverySection, ssh)
                done = Mock()
                worker = _toolbox_common.start_plugin_worker(
                    owner, Mock(), pending="pending", on_done=done,
                )
                ssh.close()
                if replacement:
                    ssh._client = object()
                    ssh.connection_changed.emit(True)
                with patch.object(_toolbox_common, "show_error") as error:
                    worker.signals.error.emit(RuntimeError("remote result unknown"))
                self.assertEqual(error.call_count, 0 if replacement else 1)
                self.assertFalse(owner._busy)
                done.assert_called_once()

    def test_plugin_stale_result_cannot_unlock_new_task(self):
        ssh = FakeSSH()
        owner = self.widget(_toolbox_recovery.CapacityRecoverySection, ssh)
        apply = Mock()
        first = _toolbox_common.start_plugin_worker(owner, Mock(), pending="first", apply_status=apply)
        ssh.close()
        ssh._client = object()
        ssh.connection_changed.emit(True)
        owner._busy = False
        second = _toolbox_common.start_plugin_worker(owner, Mock(), pending="second", apply_status=apply)
        first.signals.finished.emit("old")
        self.assertTrue(owner._busy)
        self.assertEqual(owner.status_label.text(), "second")
        apply.assert_not_called()
        second.signals.finished.emit("new")
        apply.assert_called_once_with("new")

    def test_plugin_queued_work_is_bound_to_original_client(self):
        ssh = FakeSSH()
        owner = self.widget(_toolbox_recovery.CapacityRecoverySection, ssh)
        operation = Mock()
        worker = _toolbox_common.start_plugin_worker(owner, operation, pending="pending")
        ssh._client = object()
        with patch.object(_toolbox_common, "show_error") as error:
            worker.run()
        operation.assert_not_called()
        error.assert_not_called()

    def test_legacy_recovery_disconnect_error_is_not_lost(self):
        ssh = FakeSSH()
        owner = self.widget(_toolbox_legacy.LegacyPluginMigrationSection, ssh)
        owner._start_worker(Mock(), pending="pending")
        worker = owner.thread_pool.start.call_args.args[0]
        ssh.close()
        with patch.object(_toolbox_common, "show_error") as error:
            worker.signals.error.emit(RuntimeError("unknown result"))
        error.assert_called_once()
        self.assertFalse(owner._connected)
        self.assertFalse(owner._busy)

    def test_document_upload_disconnect_error_is_not_lost(self):
        ssh = FakeSSH()
        tab = self.widget(_tab_documents.DocumentsTab, ssh)
        tab.set_connection_state(True)
        ssh.connection_changed.connect(tab.set_connection_state)
        with patch.object(QtWidgets.QFileDialog, "getOpenFileNames", return_value=(["book.pdf"], "")):
            tab.upload_document()
        worker = tab.thread_pool.start.call_args.args[0]
        ssh.close()
        with patch.object(tab, "_on_error") as error:
            worker.signals.error.emit(RuntimeError("import result unknown"))
        error.assert_called_once()

    def test_document_upload_cannot_close_or_update_export_progress(self):
        ssh = FakeSSH()
        tab = self.widget(_tab_documents.DocumentsTab, ssh)
        tab.set_connection_state(True)
        with patch.object(QtWidgets.QFileDialog, "getOpenFileNames", return_value=(["book.pdf"], "")):
            tab.upload_document()
        upload = tab.thread_pool.start.call_args.args[0]
        tab._show_progress_dialog("export", "export owns this")
        progress = tab._active_progress
        upload.signals.status.emit("upload status")
        self.assertEqual(progress.labelText(), "export owns this")
        with patch.object(tab, "refresh"):
            upload.signals.finished.emit(1)
        self.assertIs(tab._active_progress, progress)

    def test_screen_queued_capture_cannot_run_on_replacement(self):
        ssh = FakeSSH()
        tab = self.widget(_tab_screen_preview.ScreenPreviewTab, ssh)
        tab.set_connection_state(True)
        operation = Mock()
        with patch("_ui_tasks.QtCore.QThreadPool.globalInstance", return_value=tab.thread_pool):
            tab._start_worker(operation, Mock(), error_prefix="capture")
        worker = tab.thread_pool.start.call_args.args[0]
        ssh._client = object()
        with patch.object(_tab_screen_preview, "show_error"):
            worker.run()
        operation.assert_not_called()

    def test_screen_bad_frame_reports_callback_error_and_releases_busy(self):
        ssh = FakeSSH()
        tab = self.widget(_tab_screen_preview.ScreenPreviewTab, ssh)
        tab.set_connection_state(True)
        tab._supported = True
        with patch("_ui_tasks.QtCore.QThreadPool.globalInstance", return_value=tab.thread_pool):
            tab._capture()
        worker = tab.thread_pool.start.call_args.args[0]
        with patch.object(_tab_screen_preview, "show_error") as error:
            worker.signals.finished.emit(SimpleNamespace(png=b"bad png", width=1, height=1))
        error.assert_called_once()
        self.assertFalse(tab._busy)

    def test_screen_reconnect_detects_after_old_capture_finishes(self):
        ssh = FakeSSH()
        tab = self.widget(_tab_screen_preview.ScreenPreviewTab, ssh)
        tab.set_connection_state(True)
        tab._page_active = True
        tab._supported = True
        tab._busy = False
        with patch("_ui_tasks.QtCore.QThreadPool.globalInstance", return_value=tab.thread_pool):
            tab._capture()
            worker = tab.thread_pool.start.call_args.args[0]
            tab.set_connection_state(False)
            ssh._client = object()
            tab.set_connection_state(True)
            with patch.object(tab, "_detect") as detect:
                worker.signals.finished.emit(SimpleNamespace(png=b"old"))
        detect.assert_called_once()
        self.assertIsNone(tab._latest_png)

    def test_dialogs_are_plain_and_released(self):
        parent = QtWidgets.QWidget()
        self.addCleanup(parent.deleteLater)
        dialog = _dialogs._build_dialog(parent, "title", "<b>title</b>", "<img src='x'>", kind="error", detail="<b>detail</b>")
        self.addCleanup(dialog.deleteLater)
        for name in ("appDialogTitle", "appDialogBody", "appDialogNoteText"):
            self.assertEqual(dialog.findChild(QtWidgets.QLabel, name).textFormat(), QtCore.Qt.PlainText)
        with patch.object(QtWidgets.QDialog, "exec_", return_value=QtWidgets.QDialog.Accepted):
            _dialogs.ask_confirmation(parent, "title", "body")
            _dialogs.show_info(parent, "title", "body")
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        self.assertEqual(parent.findChildren(QtWidgets.QDialog), [dialog])

    def test_wallpaper_exif_and_interactive_preview_are_bounded(self):
        tab = self.widget(_tab_wallpaper.WallpaperTab, FakeSSH(), rmtool._default_config())
        tab.frame_preview_checkbox.setChecked(False)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "portrait.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (1200, 800), "red").save(path, exif=exif)
            with patch.object(QtWidgets.QFileDialog, "getOpenFileName", return_value=(str(path), "")):
                tab._select_image()
        self.assertEqual(tab._cached_source_image.size, (800, 1200))
        with patch.object(tab, "_process_image", wraps=tab._process_image) as process:
            for value in range(1, 15):
                tab.offset_x_slider.setValue(value)
            self.assertEqual(process.call_count, 0)
            QtTest.QTest.qWait(100)
            self.assertEqual(process.call_count, 1)
        pixmap = tab.preview_label._original_pixmap
        self.assertLessEqual(max(pixmap.width(), pixmap.height()), 800)
        self.assertEqual(tab._process_image().size, tab.current_resolution)

    def test_wallpaper_raster_formats_keep_webp_and_reject_other_decoders(self):
        tab = self.widget(_tab_wallpaper.WallpaperTab, FakeSSH(), rmtool._default_config())
        tab.frame_preview_checkbox.setChecked(False)
        with tempfile.TemporaryDirectory() as folder:
            for format_name in ("PNG", "JPEG", "BMP", "WEBP"):
                path = Path(folder) / (format_name + ".image")
                Image.new("RGB", (20, 30), "red").save(path, format=format_name)
                with patch.object(QtWidgets.QFileDialog, "getOpenFileName", return_value=(str(path), "")):
                    tab._select_image()
                self.assertEqual(tab._cached_source_image.size, (20, 30))
                self.assertEqual(_tab_wallpaper._usable_cover_data(path.read_bytes()), path.read_bytes())
            previous = tab._cached_source_image
            path = Path(folder) / "disguised.jpg"
            Image.new("RGB", (20, 30)).save(path, format="TIFF")
            with patch.object(QtWidgets.QFileDialog, "getOpenFileName", return_value=(str(path), "")), patch.object(
                _tab_wallpaper, "show_error"
            ) as error:
                tab._select_image()
            error.assert_called_once()
            self.assertIs(tab._cached_source_image, previous)
            self.assertIsNone(_tab_wallpaper._usable_cover_data(path.read_bytes()))

    def test_remote_wallpaper_preview_refuses_non_raster_format(self):
        tab = self.widget(_tab_wallpaper.WallpaperTab, FakeSSH(), rmtool._default_config())
        buffer = BytesIO()
        Image.new("RGB", (20, 30), "red").save(buffer, format="TIFF")
        key = next(iter(tab.variant_previews))
        tab._apply_variant_previews({key: _tab_wallpaper._WallpaperPreviewResult(data=buffer.getvalue())})
        self.assertEqual(tab.variant_previews[key].text(), "无预览")

    def test_document_cover_uses_pillow_without_qt_image_plugins(self):
        tab = self.widget(_tab_documents.DocumentsTab, FakeSSH())
        tab._current_preview_request = "book"
        for image_format in ("PNG", "JPEG", "BMP", "WEBP", "TIFF"):
            with self.subTest(image_format=image_format):
                buffer = BytesIO()
                with Image.new("RGB", (20, 30), "red") as source:
                    source.save(buffer, format=image_format)
                with patch.object(_tab_documents.QtGui.QImage, "fromData", side_effect=AssertionError("unsafe decoder")):
                    tab._on_preview_loaded("book", buffer.getvalue())
                if image_format == "TIFF":
                    self.assertEqual(tab.preview_image.text(), "无法解析预览图像")
                else:
                    self.assertEqual(tab.preview_image.text(), "")
                    self.assertFalse(tab.preview_image._original_pixmap.isNull())

    def test_wallpaper_cover_dialog_released_on_cancel_and_accept(self):
        tab = self.widget(_tab_wallpaper.WallpaperTab, FakeSSH(), rmtool._default_config())
        entries = [SimpleNamespace(cover=b"cover")]
        for accepted in (False, True):
            dialog = QtWidgets.QDialog(tab)
            dialog.selected_entries = Mock(return_value=[])
            dialog.title_edit = QtWidgets.QLineEdit()
            dialog.subtitle_edit = QtWidgets.QLineEdit()
            dialog.layout_combo = QtWidgets.QComboBox()
            with patch.object(_tab_wallpaper, "_CoverWallDialog", return_value=dialog), patch.object(
                QtWidgets.QDialog, "exec_", return_value=int(accepted)
            ), patch.object(tab, "_apply_cover_wall"):
                tab._on_cover_wall_entries_loaded(entries)
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
            self.assertTrue(sip.isdeleted(dialog))

    def test_cover_wall_composes_full_resolution_only_for_save(self):
        tab = self.widget(_tab_wallpaper.WallpaperTab, FakeSSH(), rmtool._default_config())
        tab.frame_preview_checkbox.setChecked(False)
        resolutions = []

        def compose(_covers, resolution, *_args, **_kwargs):
            resolutions.append(resolution)
            return Image.new("RGB", resolution, "white")

        with patch.object(_tab_wallpaper, "compose_cover_wallpaper", side_effect=compose):
            tab._apply_cover_wall([SimpleNamespace(cover=b"cover")], "title", "subtitle")
            self.assertLessEqual(max(resolutions[-1]), 800)
            final = tab._process_image()
        self.assertEqual(resolutions[-1], tab.current_resolution)
        self.assertEqual(final.size, tab.current_resolution)

    def test_carousel_migration_quotes_both_paths(self):
        ssh = FakeSSH()
        name = "pic ' $(touch nope).png.backup"
        ssh.listdir_attr = Mock(return_value=[SimpleNamespace(filename=name)])
        tab = self.widget(_tab_wallpaper.WallpaperTab, ssh, rmtool._default_config())
        tab._migrate_legacy_carousel_backups()
        command = ssh.commands[-1]
        self.assertIn(shlex.quote(_tab_wallpaper._CAROUSEL_DIR + "/" + name), command)
        self.assertIn(shlex.quote(_tab_wallpaper._CAROUSEL_BACKUP_DIR + "/" + name[:-7]), command)

    def test_wallpaper_upload_rejects_wrong_device_before_root_or_home_write(self):
        ssh = FakeSSH()
        ssh.exec_command = Mock(return_value=("reMarkable 2", "", 0))
        tab = self.widget(_tab_wallpaper.WallpaperTab, ssh, rmtool._default_config())
        for user_partition in (False, True):
            with self.subTest(user_partition=user_partition), patch.object(
                _tab_wallpaper, "remount_rw"
            ) as remount, patch.object(_tab_wallpaper._sleep_wallpaper, "enable") as enable:
                with self.assertRaisesRegex(RuntimeError, "型号"):
                    tab._do_upload_wallpaper("unused", "/usr/share/remarkable/suspended.png", user_partition)
            remount.assert_not_called()
            enable.assert_not_called()

    def test_carousel_migration_read_failure_is_not_treated_as_empty(self):
        ssh = FakeSSH()
        ssh.listdir_attr = Mock(side_effect=OSError("connection lost"))
        tab = self.widget(_tab_wallpaper.WallpaperTab, ssh, rmtool._default_config())
        with self.assertRaisesRegex(OSError, "connection lost"):
            tab._migrate_legacy_carousel_backups()
        self.assertEqual(ssh.commands, [])

    def test_recovery_feature_change_invalidates_old_intent_and_plan(self):
        ssh = FakeSSH()
        tab = self.widget(_toolbox_recovery.CapacityRecoverySection, ssh)
        tab._intent = SimpleNamespace(client=ssh._client, feature_id="pinyin-input")
        tab._plan = object()
        tab.feature_combo.setCurrentIndex(1)
        with patch.object(tab, "_start_fresh_intent") as fresh, patch.object(_toolbox_common, "start_plugin_worker") as scan:
            tab._start_scan()
        fresh.assert_called_once()
        scan.assert_not_called()
        self.assertIsNone(tab._plan)

    def test_koreader_case_collision_refused_before_any_download(self):
        files = [SimpleNamespace(name=name, path="/books/" + name, size=1) for name in ("Book.epub", "book.epub")]
        tab = self.widget(_tab_koreader.KOReaderTab, FakeSSH())
        with tempfile.TemporaryDirectory() as folder, patch.object(_koreader, "download_file") as download:
            with self.assertRaisesRegex(RuntimeError, "文件名|同名|大小写"):
                tab._perform_download(files, folder, "/books")
        download.assert_not_called()

    def test_koreader_download_progress_is_owned_by_its_worker(self):
        ssh = FakeSSH()
        tab = self.widget(_tab_koreader.KOReaderTab, ssh)
        tab.set_connection_state(True)
        tab._library_root = "/books"
        entry = SimpleNamespace(name="book.epub", path="/books/book.epub", size=1, is_dir=False)
        with patch.object(tab, "_selected_entries", return_value=[entry]), patch.object(
            QtWidgets.QFileDialog, "getExistingDirectory", return_value="unused"
        ):
            tab._download_books()
        worker = tab.thread_pool.start.call_args.args[0]
        tab._show_progress_dialog("new task", "new task owns this")
        progress = tab._active_progress
        worker.signals.progress.emit(1, 2)
        self.assertEqual(progress.labelText(), "new task owns this")
        with patch.object(_tab_koreader, "show_info"):
            worker.signals.finished.emit(None)
        self.assertIs(tab._active_progress, progress)

    def test_koreader_progress_keeps_plain_token_not_auto_deleted_runnable(self):
        ssh = FakeSSH()
        tab = self.widget(_tab_koreader.KOReaderTab, ssh)
        tab.set_connection_state(True)
        worker = tab._session_worker(ssh._client, lambda: None)
        tab._show_progress_dialog("task", "task", worker=worker.progress_task)
        self.assertNotIsInstance(tab._progress_worker, QtCore.QRunnable)
        worker.signals.progress.emit(1, 2)
        self.assertIn("50.0%", tab._active_progress.labelText())


class CapacityPartitionTests(unittest.TestCase):
    def setUp(self):
        self.ssh = FakeSSH()
        self.intent = recovery.OriginalIntent(self.ssh, self.ssh._client, ("fw", "rm1", "arm", "hash"), "pinyin-input", SimpleNamespace(package_id="test"), "state")
        self.snapshot = _rmkit_cn.HomeOnlyFontState("/home/fonts", "a.ttf", "/home/fonts/a.ttf", "/data/rmtool/fonts/active-ui-font.ttf", "Font", "a", "b", "c", 100)

    def scan(self, partition, shortfall, system_partition="/"):
        context = recovery.CapacityContext("shared-capacity", partition, 200, 200 - shortfall, shortfall)
        with patch.object(recovery, "_validate_intent"), patch.object(recovery, "_preflight_result", return_value=(context, RuntimeError("low"), {})), patch.object(
            _rmkit_cn, "inspect_home_only_font", return_value=self.snapshot
        ), patch.object(recovery.storage, "system_free_bytes", return_value=(system_partition, 1)):
            return recovery.scan(self.ssh, self.intent)

    def test_different_partition_and_insufficient_reclaim_have_no_plan(self):
        for partition, shortfall in (("/home", 50), ("/", 101)):
            with self.subTest(partition=partition, shortfall=shortfall):
                result = self.scan(partition, shortfall)
                self.assertIsInstance(result, recovery.CapacityResult)
                self.assertEqual(result.state, "no_candidate")

    def test_sufficient_system_partition_reclaim_has_plan(self):
        self.assertIsInstance(self.scan("/", 100), recovery.CapacityPlan)
        self.assertIsInstance(self.scan("/data", 100, "/data"), recovery.CapacityPlan)

    def test_execute_rejects_wrong_partition_before_cleanup(self):
        context = recovery.CapacityContext("shared-capacity", "/home", 200, 150, 50)
        plan = recovery.CapacityPlan(self.intent, context, recovery.HomeOnlyFontPlan(self.snapshot), {})
        with patch.object(recovery, "_validate_intent"), patch.object(recovery, "_preflight_result", return_value=(context, RuntimeError("low"), {})), patch.object(
            _rmkit_cn, "inspect_home_only_font", return_value=self.snapshot
        ), patch.object(_rmkit_cn, "set_active_user_font") as mutate, patch.object(
            recovery.storage, "system_free_bytes", return_value=("/", 1)
        ):
            result = recovery.execute(self.ssh, plan)
        mutate.assert_not_called()
        self.assertEqual(result.state, "no_candidate")
        self.assertFalse(result.cleaned)


if __name__ == "__main__":
    unittest.main()
