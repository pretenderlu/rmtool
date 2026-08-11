"""FontTab, TimeTab, ControlTab, ToolboxTab, and FontPage extracted from rmtool.py."""

import logging
import os
import posixpath
from datetime import datetime
from typing import Dict, Optional

from PyQt5 import QtCore, QtGui, QtWidgets, sip

from _dialogs import ask_confirmation, show_error, show_info, show_warning
import _rmkit_cn
import _fast_mono_reading
import _legacy_vellum
import _native_chinese
import _tap_page_turn
from _ssh import SSHClientWrapper, remount_rw, require_connection
import rmtool as _rmtool  # late-bound access to avoid circular import


def select_font_file(parent: QtWidgets.QWidget) -> Optional[str]:
    path, _ = QtWidgets.QFileDialog.getOpenFileName(
        parent, "选择字体文件", "", "字体文件 (*.ttf *.otf)"
    )
    return path or None


def load_font_file(file_path: str) -> tuple[int, Optional[str]]:
    font_id = QtGui.QFontDatabase.addApplicationFont(file_path)
    families = (
        QtGui.QFontDatabase.applicationFontFamilies(font_id)
        if font_id != -1
        else []
    )
    return font_id, families[0] if families else None


class FontTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, config: Dict, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.config = config
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._font_progress: Optional[QtWidgets.QProgressDialog] = None
        self._fonts: tuple[_rmkit_cn.UserFont, ...] = ()
        self._font_verification: Optional[_rmkit_cn.FontMirrorVerification] = None
        self._legacy_font_migration: Optional[
            _rmkit_cn.LegacySystemFontMigration
        ] = None
        self._busy = False
        self._connected: Optional[bool] = None
        self._worker_generation = 0
        self._connection_generation = 0
        self._pending_refresh: Optional[tuple[str, str]] = None
        self._selected_font_path: Optional[str] = None
        self._selected_font_family: Optional[str] = None
        self._preview_font_id = -1

        self.font_path_label = QtWidgets.QLabel("未选择文件")
        self.rename_checkbox = QtWidgets.QCheckBox(f"上传时重命名为 {_rmtool.DEFAULT_FONT_NAME}")
        self.rename_checkbox.setChecked(False)
        self.rename_checkbox.toggled.connect(self._update_target_name_label)

        self.target_name_label = QtWidgets.QLabel()
        self.target_name_label.setObjectName("fontTargetName")

        self.preview_panel = QtWidgets.QFrame()
        self.preview_panel.setObjectName("fontPreviewPanel")
        self.preview_panel.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        preview_layout = QtWidgets.QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
        )
        preview_layout.setSpacing(_rmtool.SUBSECTION_GAP)

        self.preview_title_label = QtWidgets.QLabel("选择字体后可在这里预览")
        self.preview_title_label.setObjectName("fontPreviewTitle")
        self.preview_sample_label = QtWidgets.QLabel()
        self.preview_sample_label.setObjectName("fontPreviewSample")
        self.preview_sample_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        self.preview_sample_label.setWordWrap(True)
        self.preview_sample_label.setMinimumHeight(120)
        preview_layout.addWidget(self.preview_title_label)
        preview_layout.addWidget(self.preview_sample_label)

        self.select_button = QtWidgets.QPushButton("选择字体")
        self.select_button.clicked.connect(self._select_font_file)
        self.upload_button = QtWidgets.QPushButton("上传字体")
        self.upload_button.setProperty("btnRole", "primary")
        self.upload_button.setEnabled(False)
        self.upload_button.clicked.connect(self._upload_selected_font)

        self.font_table = QtWidgets.QTableWidget(0, 3)
        self.font_table.setObjectName("fontManagerTable")
        self.font_table.setHorizontalHeaderLabels(("文件名", "字体族", "状态"))
        self.font_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.font_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.font_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.font_table.setAlternatingRowColors(True)
        self.font_table.verticalHeader().setVisible(False)
        self.font_table.setMinimumHeight(180)
        table_header = self.font_table.horizontalHeader()
        table_header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        table_header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        table_header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        self.font_table.itemSelectionChanged.connect(self._update_action_buttons)

        self.manager_status_label = QtWidgets.QLabel("连接设备后可刷新已上传字体。")
        self.manager_status_label.setObjectName("fontManagerStatus")
        self.manager_status_label.setWordWrap(True)

        self.refresh_button = QtWidgets.QPushButton("刷新")
        self.refresh_button.clicked.connect(self._refresh_fonts)
        self.set_active_button = QtWidgets.QPushButton("设为系统字体")
        self.set_active_button.clicked.connect(self._set_selected_active)
        self.delete_button = QtWidgets.QPushButton("删除")
        self.delete_button.setProperty("btnRole", "danger")
        self.delete_button.clicked.connect(self._delete_selected_font)
        self.migrate_font_button = QtWidgets.QPushButton("迁移旧版字体设置")
        self.migrate_font_button.clicked.connect(self._migrate_legacy_font)
        self.restart_button = QtWidgets.QPushButton("重启生效")
        self.restart_button.clicked.connect(self._restart_device)

        manager_actions = QtWidgets.QHBoxLayout()
        manager_actions.setContentsMargins(0, 0, 0, 0)
        manager_actions.setSpacing(_rmtool.SUBSECTION_GAP)
        manager_actions.addWidget(self.refresh_button)
        manager_actions.addWidget(self.set_active_button)
        manager_actions.addWidget(self.delete_button)
        manager_actions.addWidget(self.migrate_font_button)
        manager_actions.addStretch()
        manager_actions.addWidget(self.restart_button)

        actions_layout = QtWidgets.QHBoxLayout()
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(_rmtool.SUBSECTION_GAP)
        actions_layout.addWidget(self.select_button, 1)
        actions_layout.addWidget(self.upload_button, 1)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(self.manager_status_label)
        layout.addWidget(self.font_table)
        layout.addLayout(manager_actions)
        layout.addWidget(self.font_path_label)
        layout.addWidget(self.rename_checkbox)
        layout.addWidget(self.target_name_label)
        layout.addWidget(self.preview_panel)
        layout.addLayout(actions_layout)
        self.setLayout(layout)
        self._reset_font_preview()
        self._update_target_name_label()
        connection_changed = getattr(self.ssh_client, "connection_changed", None)
        if connection_changed is not None:
            connection_changed.connect(self._on_connection_changed)
        # Connect-time inventory refresh is driven by the MainWindow serial
        # post-connect coordinator (refresh_fonts_quiet); connecting here only
        # updates widget state.
        self._on_connection_changed(self.ssh_client.is_connected())

    def _select_font_file(self):
        file_path = select_font_file(self)
        if not file_path:
            return

        self._release_preview_font()
        self.font_path_label.setText(file_path)
        preview_font_id, preview_family = load_font_file(file_path)
        if preview_font_id == -1 or not preview_family:
            self._selected_font_path = None
            self._selected_font_family = None
            self._reset_font_preview("无法预览所选字体，请重新选择有效字体文件。")
            show_warning(self, _rmtool.APP_NAME, "无法加载所选字体的本地预览。")
            self._update_target_name_label()
            return

        self._selected_font_path = file_path
        self._preview_font_id = preview_font_id
        self._selected_font_family = preview_family
        self.preview_title_label.setText(f"{preview_family} 预览")
        # Family comes from the loaded file; the size lives in the
        # #fontPreviewSample QSS rule (type scale font_lg).
        preview_font = QtGui.QFont(preview_family)
        preview_font.setStyleStrategy(QtGui.QFont.PreferAntialias)
        self.preview_sample_label.setFont(preview_font)
        self.preview_sample_label.setText(_rmtool.FONT_PREVIEW_TEXT)
        self._update_action_buttons()
        self._update_target_name_label()

    @require_connection
    def _upload_selected_font(self):
        if not self._selected_font_path:
            show_info(self, _rmtool.APP_NAME, "请先选择需要上传的字体文件。")
            return
        file_path = self._selected_font_path
        new_name = self._target_font_name()
        active_target = next(
            (
                font
                for font in self._fonts
                if font.filename == new_name and font.active
            ),
            None,
        )
        if active_target is not None:
            show_warning(
                self,
                _rmtool.APP_NAME,
                f"{new_name} 当前正作为系统字体使用。上传不会隐式切换系统字体，"
                "请取消重命名或先切换到其他字体。",
            )
            return

        self._start_font_worker(
            self._upload_font,
            file_path,
            new_name,
            pending="正在上传字体并刷新缓存…",
            on_success=lambda font: self._refresh_fonts(
                select_filename=font.filename,
                success="字体已上传。上传不会切换系统字体，请按需点击“设为系统字体”。",
            ),
            error_prefix="字体上传失败",
        )

    def _close_font_progress(self):
        progress, self._font_progress = self._font_progress, None
        if progress is not None:
            try:
                progress.close()
                progress.deleteLater()
            except RuntimeError:
                # The C++ dialog may already be deleted if its parent was
                # destroyed while a worker signal was still queued.
                pass
        self._set_busy(False)

    def _target_font_name(self) -> str:
        if self.rename_checkbox.isChecked() or not self._selected_font_path:
            return _rmtool.DEFAULT_FONT_NAME
        return os.path.basename(self._selected_font_path)

    def _update_target_name_label(self):
        self.target_name_label.setText(f"上传后将保存为：{self._target_font_name()}")

    def _reset_font_preview(self, title: str = "选择字体后可在这里预览"):
        self.preview_title_label.setText(title)
        self.preview_sample_label.setFont(self.font())
        self.preview_sample_label.setText(_rmtool.FONT_PREVIEW_TEXT)
        self.upload_button.setEnabled(False)

    def _release_preview_font(self):
        if self._preview_font_id != -1:
            QtGui.QFontDatabase.removeApplicationFont(self._preview_font_id)
            self._preview_font_id = -1

    def _upload_font(self, file_path: str, new_name: str):
        font_dir = self.config.get("paths", {}).get("font", _rmtool.DEFAULT_FONT_DIR)
        return _rmkit_cn.upload_user_font(
            self.ssh_client,
            file_path,
            font_dir,
            new_name,
        )

    def _font_dir(self) -> str:
        return self.config.get("paths", {}).get("font", _rmtool.DEFAULT_FONT_DIR)

    def _on_connection_changed(self, connected: bool):
        connected = bool(connected)
        if connected != self._connected:
            self._connected = connected
            self._connection_generation += 1
            self._worker_generation += 1
            self._pending_refresh = None
            if self._font_progress:
                self._font_progress.close()
                self._font_progress.deleteLater()
                self._font_progress = None
        if not connected:
            self._fonts = ()
            self._font_verification = None
            self._legacy_font_migration = None
            self.font_table.setRowCount(0)
            self.manager_status_label.setText("设备未连接。")
        else:
            self.manager_status_label.setText("设备已连接，可刷新已上传字体。")
        self._update_action_buttons()

    def _selected_device_font(self) -> Optional[_rmkit_cn.UserFont]:
        rows = self.font_table.selectionModel().selectedRows()
        if len(rows) != 1:
            return None
        row = rows[0].row()
        if 0 <= row < len(self._fonts):
            return self._fonts[row]
        return None

    def _update_action_buttons(self):
        connected = self.ssh_client.is_connected() and not self._busy
        selected = self._selected_device_font()
        self.set_active_button.setText(
            "重新应用系统字体" if selected and selected.active else "设为系统字体"
        )
        self.refresh_button.setEnabled(connected)
        self.select_button.setEnabled(not self._busy)
        self.upload_button.setEnabled(
            connected and bool(self._selected_font_path)
        )
        self.set_active_button.setEnabled(
            connected and selected is not None
        )
        self.delete_button.setEnabled(
            connected and selected is not None and not selected.active
        )
        self.migrate_font_button.setEnabled(
            connected
            and self._legacy_font_migration is not None
            and self._legacy_font_migration.migratable
        )
        self.restart_button.setEnabled(connected)

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        if message:
            self.manager_status_label.setText(message)
        self._update_action_buttons()

    def _start_font_worker(
        self,
        fn,
        *args,
        pending: str,
        on_success,
        error_prefix: str,
    ):
        if self._busy:
            raise RuntimeError("已有字体操作正在进行。")
        self._worker_generation += 1
        worker_generation = self._worker_generation
        connection_generation = self._connection_generation
        self._set_busy(True, pending)
        progress = QtWidgets.QProgressDialog(pending, "", 0, 0, self)
        progress.setWindowTitle(_rmtool.APP_NAME)
        progress.setWindowModality(QtCore.Qt.ApplicationModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.show()
        self._font_progress = progress
        worker = _rmtool.Worker(fn, *args)

        def finish_stale_worker():
            pending_refresh = self._pending_refresh
            self._pending_refresh = None
            self._busy = False
            self._update_action_buttons()
            if pending_refresh is not None and self.ssh_client.is_connected():
                self._refresh_fonts(
                    select_filename=pending_refresh[0], success=pending_refresh[1]
                )

        def on_finished(result):
            if sip.isdeleted(self):
                # Worker outlived the tab (e.g. pool drained during teardown);
                # nothing safe left to update.
                return
            if (
                worker_generation != self._worker_generation
                or connection_generation != self._connection_generation
            ):
                finish_stale_worker()
                return
            pending_refresh = self._pending_refresh
            self._pending_refresh = None
            self._close_font_progress()
            on_success(result)
            if (
                pending_refresh is not None
                and not self._busy
                and self.ssh_client.is_connected()
            ):
                self._refresh_fonts(
                    select_filename=pending_refresh[0], success=pending_refresh[1]
                )

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                # Worker outlived the tab; only log, touching widgets would
                # raise RuntimeError (and abort the process on macOS).
                logging.error("Font manager operation failed after tab close: %s", exc)
                return
            if (
                worker_generation != self._worker_generation
                or connection_generation != self._connection_generation
            ):
                finish_stale_worker()
                return
            pending_refresh = self._pending_refresh
            self._pending_refresh = None
            self._close_font_progress()
            self.manager_status_label.setText("操作失败，请查看提示后重试。")
            logging.error("Font manager operation failed: %s", exc)
            show_error(self, _rmtool.APP_NAME, f"{error_prefix}：{exc}")
            if (
                pending_refresh is not None
                and not self._busy
                and self.ssh_client.is_connected()
            ):
                self._refresh_fonts(
                    select_filename=pending_refresh[0], success=pending_refresh[1]
                )

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _refresh_fonts(self, *, select_filename: str = "", success: str = ""):
        if self._busy:
            self._pending_refresh = (select_filename, success)
            return
        self._pending_refresh = None
        self._start_font_worker(
            self._load_font_inventory,
            self.ssh_client,
            self._font_dir(),
            pending="正在读取设备字体…",
            on_success=lambda inventory: self._apply_font_inventory(
                inventory[0],
                verification=inventory[1],
                migration=inventory[2],
                select_filename=select_filename,
                success=success,
            ),
            error_prefix="字体列表刷新失败",
        )

    @staticmethod
    def _load_font_inventory(ssh_client, remote_dir: str):
        return (
            _rmkit_cn.list_user_fonts(ssh_client, remote_dir),
            _rmkit_cn.get_font_mirror_verification(ssh_client),
            _rmkit_cn.get_legacy_system_font_migration(ssh_client, remote_dir),
        )

    def _apply_font_inventory(
        self,
        fonts: tuple[_rmkit_cn.UserFont, ...],
        *,
        verification: Optional[_rmkit_cn.FontMirrorVerification] = None,
        migration: Optional[_rmkit_cn.LegacySystemFontMigration] = None,
        select_filename: str = "",
        success: str = "",
    ):
        previous = select_filename
        if not previous:
            selected = self._selected_device_font()
            previous = selected.filename if selected else ""
        self._fonts = tuple(fonts)
        self._font_verification = verification
        self._legacy_font_migration = migration
        self.font_table.setRowCount(len(self._fonts))
        selected_row = -1
        for row, font in enumerate(self._fonts):
            values = (font.filename, font.family, "当前系统字体" if font.active else "已上传")
            for column, value in enumerate(values):
                self.font_table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
            if font.filename == previous:
                selected_row = row
        if selected_row >= 0:
            self.font_table.selectRow(selected_row)
        else:
            self.font_table.clearSelection()
        active_fonts = [font.filename for font in self._fonts if font.active]
        active = "、".join(active_fonts) if active_fonts else "未在列表中"
        legacy_note = (
            "；检测到多个 Fontconfig 匹配，切换前均按当前系统字体保护"
            if len(active_fonts) > 1
            else ""
        )
        verification_note = (
            f"；字体镜像：{self._font_verification.label}"
            if self._font_verification is not None
            else ""
        )
        migration_note = (
            f"；{self._legacy_font_migration.detail}"
            if self._legacy_font_migration is not None
            and self._legacy_font_migration.state != "none"
            else ""
        )
        self.manager_status_label.setText(
            f"已读取 {len(self._fonts)} 个用户字体；当前系统字体：{active}"
            f"{legacy_note}{verification_note}{migration_note}。"
        )
        tooltip = "\n".join(
            detail
            for detail in (
                self._font_verification.detail if self._font_verification else "",
                self._legacy_font_migration.detail
                if self._legacy_font_migration
                and self._legacy_font_migration.state != "none"
                else "",
            )
            if detail
        )
        self.manager_status_label.setToolTip(tooltip)
        self._update_action_buttons()
        if success:
            show_info(self, _rmtool.APP_NAME, success)

    def refresh_fonts_quiet(self, on_done) -> None:
        """Post-connect inventory refresh driven by the MainWindow serial
        coordinator.

        Unlike ``_refresh_fonts`` this runs without the modal progress dialog
        and never pops an error dialog; failures only reach the log and the
        status label. Calls ``on_done`` exactly once. When another font
        operation is busy, the refresh is queued via ``_pending_refresh`` and
        ``on_done`` is called immediately so the coordinator is not blocked.
        ``_busy`` is held for the duration so a manual refresh or upload
        cannot open a second SSH channel alongside the quiet one; a refresh
        queued by the user meanwhile runs after this one finishes.
        """
        if self._busy:
            self._pending_refresh = ("", "")
            on_done()
            return
        if not self.ssh_client.is_connected():
            on_done()
            return
        connection_generation = self._connection_generation
        self._set_busy(True)
        worker = _rmtool.Worker(
            self._load_font_inventory, self.ssh_client, self._font_dir()
        )

        def finish_quiet():
            self._set_busy(False)
            pending_refresh = self._pending_refresh
            self._pending_refresh = None
            if pending_refresh is not None and self.ssh_client.is_connected():
                self._refresh_fonts(
                    select_filename=pending_refresh[0], success=pending_refresh[1]
                )

        def on_finished(inventory):
            if sip.isdeleted(self):
                on_done()
                return
            try:
                if connection_generation != self._connection_generation:
                    # Stale result from a previous connection; discard it.
                    return
                self._apply_font_inventory(
                    inventory[0], verification=inventory[1], migration=inventory[2]
                )
            except Exception:
                # Never let an apply failure stall the serial coordinator.
                logging.exception("Failed to apply post-connect font inventory")
            finally:
                try:
                    finish_quiet()
                except Exception:
                    logging.exception("Failed to finish post-connect font refresh")
                on_done()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error(
                    "Post-connect font refresh failed after tab close: %s", exc
                )
                on_done()
                return
            try:
                logging.error("Post-connect font refresh failed: %s", exc)
                if connection_generation == self._connection_generation:
                    self.manager_status_label.setText(
                        "字体列表刷新失败，请点击“刷新”重试。"
                    )
            finally:
                try:
                    finish_quiet()
                except Exception:
                    logging.exception("Failed to finish post-connect font refresh")
                on_done()

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _migrate_legacy_font(self):
        migration = self._legacy_font_migration
        if migration is None or not migration.migratable:
            return
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"将旧版系统字体设置迁移到新的 /data 字体镜像。"
            f"用户字体 {migration.filename} 会继续保留在 /home，设备不会自动重启。"
            "是否继续？",
            confirm_text="迁移旧版字体设置",
            cancel_text="取消",
        ):
            return
        self._start_font_worker(
            _rmkit_cn.migrate_legacy_system_font,
            self.ssh_client,
            self._font_dir(),
            pending="正在迁移并验证旧版字体设置…",
            on_success=lambda font: self._refresh_fonts(
                select_filename=font.filename,
                success="旧版字体设置已迁移。请在准备好后点击“重启生效”。",
            ),
            error_prefix="旧版字体设置迁移失败",
        )

    @require_connection
    def _set_selected_active(self):
        selected = self._selected_device_font()
        if not selected:
            return
        reapply = selected.active
        action = "重新应用系统字体" if reapply else "设为系统字体"
        message = (
            f"重新应用 {selected.filename} 作为系统界面字体，并修复 /data 中供锁屏使用的当前字体副本。"
            if reapply
            else f"将 {selected.filename} 设为系统界面字体。"
        )
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"{message}操作完成后需手动重启设备才会完整生效，是否继续？",
            confirm_text=action,
            cancel_text="取消",
        ):
            return
        self._start_font_worker(
            _rmkit_cn.set_active_user_font,
            self.ssh_client,
            self._font_dir(),
            selected.filename,
            pending="正在设置并验证系统字体…",
            on_success=lambda font: self._refresh_fonts(
                select_filename=font.filename,
                success="系统字体配置已更新。请在准备好后点击“重启生效”。",
            ),
            error_prefix="设置系统字体失败",
        )

    @require_connection
    def _delete_selected_font(self):
        selected = self._selected_device_font()
        if not selected:
            return
        if selected.active:
            show_warning(self, _rmtool.APP_NAME, "当前系统字体不能删除，请先切换到其他字体。")
            return
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"将从设备删除字体 {selected.filename}。此操作不会影响其他字体，是否继续？",
            confirm_text="删除字体",
            cancel_text="取消",
        ):
            return
        self._start_font_worker(
            _rmkit_cn.delete_user_font,
            self.ssh_client,
            self._font_dir(),
            selected.filename,
            pending="正在删除字体并刷新缓存…",
            on_success=lambda _: self._refresh_fonts(success="所选字体已删除。"),
            error_prefix="删除字体失败",
        )

    @require_connection
    def _restart_device(self):
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "设备将立即重启，尚未保存的内容可能丢失。是否继续？",
            confirm_text="重启设备",
            cancel_text="取消",
        ):
            return
        try:
            self.ssh_client.exec_command("reboot")
            show_info(self, _rmtool.APP_NAME, "已发送重启命令。")
        except Exception as exc:
            logging.exception("Device reboot from font manager failed")
            show_error(self, _rmtool.APP_NAME, f"重启失败：{exc}")


class TimeTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.output = QtWidgets.QPlainTextEdit()
        self.output.setReadOnly(True)

        self.sync_button = QtWidgets.QPushButton("使用本地时间同步")
        self.info_button = QtWidgets.QPushButton("查看当前时间信息")
        self.tz_button = QtWidgets.QPushButton("设置为东八区")

        button_layout = QtWidgets.QHBoxLayout()
        button_layout.addWidget(self.sync_button)
        button_layout.addWidget(self.info_button)
        button_layout.addWidget(self.tz_button)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(button_layout)
        self.output.setMaximumHeight(200)
        layout.addWidget(self.output)
        self.setLayout(layout)

        self.sync_button.clicked.connect(self._sync_time)
        self.info_button.clicked.connect(self._show_time_info)
        self.tz_button.clicked.connect(self._set_timezone)

    def _append_output(self, text: str):
        self.output.appendPlainText(text)
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    @require_connection
    def _sync_time(self):
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with remount_rw(self.ssh_client):
                self.ssh_client.exec_checked(f'date -s "{now}"')
                self.ssh_client.exec_checked("hwclock -w")
            self._append_output(f"已同步设备时间到 {now}")
        except Exception as exc:
            logging.exception("Sync time failed")
            show_error(self, _rmtool.APP_NAME, f"同步失败：{exc}")

    @require_connection
    def _show_time_info(self):
        try:
            commands = {
                "系统时间": "date",
                "硬件时钟": "hwclock -r",
                "时区信息": "timedatectl",
            }
            for title, cmd in commands.items():
                stdout = self.ssh_client.exec_checked(cmd)
                self._append_output(f"[{title}]\n{stdout.strip()}\n")
        except Exception as exc:
            logging.exception("Get time info failed")
            show_error(self, _rmtool.APP_NAME, f"查询失败：{exc}")

    @require_connection
    def _set_timezone(self):
        try:
            with remount_rw(self.ssh_client):
                self.ssh_client.exec_checked("timedatectl set-timezone Asia/Shanghai")
            self._append_output("已将时区设置为 Asia/Shanghai")
        except Exception as exc:
            logging.exception("Set timezone failed")
            show_error(self, _rmtool.APP_NAME, f"设置失败：{exc}")


class ControlTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client

        self.restart_button = QtWidgets.QPushButton("重启设备")
        self.restart_button.setProperty("btnRole", "danger")
        self.enable_wifi_ssh_button = QtWidgets.QPushButton("开启 Wi-Fi SSH 通道")
        self.brightness_button = QtWidgets.QPushButton("提升前光亮度")

        layout = QtWidgets.QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.restart_button)
        layout.addWidget(self.enable_wifi_ssh_button)
        layout.addWidget(self.brightness_button)
        layout.addStretch()
        self.setLayout(layout)

        self.restart_button.clicked.connect(self._restart_device)
        self.enable_wifi_ssh_button.clicked.connect(self._enable_wifi_ssh)
        self.brightness_button.clicked.connect(self._increase_brightness)

    @require_connection
    def _restart_device(self):
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "确定要重启设备吗？这将断开连接。",
            confirm_text="重启",
            cancel_text="取消",
            danger=True,
        ):
            return
        try:
            self.ssh_client.exec_command("reboot")
            show_info(self, _rmtool.APP_NAME, "已发送重启命令。")
        except Exception as exc:
            logging.exception("Restart failed")
            show_error(self, _rmtool.APP_NAME, f"重启失败：{exc}")

    @require_connection
    def _enable_wifi_ssh(self):
        try:
            self.ssh_client.exec_checked("rm-ssh-over-wlan on")
            show_info(
                self,
                _rmtool.APP_NAME,
                "已开启 Wi-Fi SSH，请在断开 USB 后使用 WLAN 地址连接。",
            )
        except Exception as exc:
            logging.exception("Enable Wi-Fi SSH failed")
            show_error(self, _rmtool.APP_NAME, f"操作失败：{exc}")

    @require_connection
    def _increase_brightness(self):
        try:
            with remount_rw(self.ssh_client):
                self.ssh_client.exec_checked(
                    "cat /sys/class/backlight/rm_frontlight/max_brightness > /sys/class/backlight/rm_frontlight/brightness"
                )
                self.ssh_client.exec_checked(
                    "echo yes > /sys/class/backlight/rm_frontlight/linear_mapping"
                )
                self.ssh_client.exec_checked("umount -l /etc")

            with remount_rw(self.ssh_client):
                service_content = """
[Unit]
Description=Set frontlight linear mapping
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo yes > /sys/class/backlight/rm_frontlight/linear_mapping'
ExecStartPost=/bin/sh -c 'cat /sys/class/backlight/rm_frontlight/max_brightness > /sys/class/backlight/rm_frontlight/brightness'

[Install]
WantedBy=multi-user.target
""".strip()
                cmd = (
                    "tee /etc/systemd/system/tweak-brightness-slider.service > /dev/null <<'EOF'\n"
                    f"{service_content}\nEOF"
                )
                self.ssh_client.exec_checked(cmd)
                self.ssh_client.exec_checked("systemctl daemon-reload")
                self.ssh_client.exec_checked(
                    "systemctl enable --now tweak-brightness-slider.service"
                )
            show_info(self, _rmtool.APP_NAME, "前光亮度已调整。")
        except Exception as exc:
            logging.exception("Brightness tweak failed")
            show_error(self, _rmtool.APP_NAME, f"设置失败：{exc}")


class RmkitCnSection(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status: Optional[_rmkit_cn.LocalizationStatus] = None
        self._busy = False
        self._other_packages_count = 0

        title = QtWidgets.QLabel("原生界面中文")
        title.setObjectName("toolboxFeatureTitle")

        detail = QtWidgets.QLabel(
            "默认会按固件版本精确匹配、下载并安装云端汉化包；"
            "网络不畅时也可下载到电脑或加载本地汉化包。"
            "中文翻译借用法语槽位，不安装后台服务。"
        )
        detail.setWordWrap(True)

        self.catalog_label = QtWidgets.QLabel("云端汉化包：检测后显示")
        self.catalog_label.setObjectName("rmkitCnCatalog")
        self.catalog_label.setWordWrap(True)
        self.catalog_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        self.other_packages_button = QtWidgets.QPushButton("其他固件版本")
        self.other_packages_button.setCheckable(True)
        self.other_packages_button.setSizePolicy(
            QtWidgets.QSizePolicy.Maximum,
            QtWidgets.QSizePolicy.Preferred,
        )
        self.other_packages_button.hide()

        self.other_packages_label = QtWidgets.QLabel()
        self.other_packages_label.setObjectName("rmkitCnOtherCatalog")
        self.other_packages_label.setWordWrap(True)
        self.other_packages_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse
        )
        self.other_packages_label.hide()

        self.status_label = QtWidgets.QLabel("设备已连接，尚未检测")
        self.status_label.setObjectName("rmkitCnDeviceStatus")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.enable_button = QtWidgets.QPushButton("启用中文")
        self.enable_button.setProperty("btnRole", "primary")
        self.restore_button = QtWidgets.QPushButton("还原")
        self.enable_button.setEnabled(False)
        self.restore_button.setEnabled(False)
        self.project_button = QtWidgets.QPushButton("查看源码")

        self.package_button = QtWidgets.QPushButton("获取汉化包")
        self.package_menu = QtWidgets.QMenu(self.package_button)
        self.download_package_action = self.package_menu.addAction("下载到电脑…")
        self.copy_package_link_action = self.package_menu.addAction("复制下载链接")
        self.package_button.setMenu(self.package_menu)
        self.load_package_button = QtWidgets.QPushButton("加载本地汉化包…")

        primary_buttons = QtWidgets.QHBoxLayout()
        primary_buttons.setContentsMargins(0, 0, 0, 0)
        primary_buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        primary_buttons.addWidget(self.detect_button)
        primary_buttons.addWidget(self.enable_button)
        primary_buttons.addWidget(self.restore_button)
        primary_buttons.addStretch()

        package_buttons = QtWidgets.QHBoxLayout()
        package_buttons.setContentsMargins(0, 0, 0, 0)
        package_buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        package_buttons.addWidget(self.package_button)
        package_buttons.addWidget(self.load_package_button)
        package_buttons.addWidget(self.project_button)
        package_buttons.addStretch()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(self.catalog_label)
        layout.addWidget(
            self.other_packages_button,
            alignment=QtCore.Qt.AlignLeft,
        )
        layout.addWidget(self.other_packages_label)
        layout.addWidget(self.status_label)
        layout.addLayout(primary_buttons)
        layout.addLayout(package_buttons)

        self.other_packages_button.toggled.connect(
            self._toggle_other_packages
        )
        self.detect_button.clicked.connect(self._detect_status)
        self.enable_button.clicked.connect(self._enable_localization)
        self.restore_button.clicked.connect(self._restore_localization)
        self.download_package_action.triggered.connect(
            self._download_package_to_computer
        )
        self.copy_package_link_action.triggered.connect(
            self._copy_package_download_link
        )
        self.load_package_button.clicked.connect(self._load_local_package)
        self.project_button.clicked.connect(
            lambda: self._open_external(_rmkit_cn.REPO_URL)
        )
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    def _open_external(self, url: str):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._status = None
            self.status_label.setText("设备未连接")
        elif self._status is None:
            self.status_label.setText("设备已连接，尚未检测")
        self.detect_button.setEnabled(connected and not self._busy)
        self._update_action_buttons()

    def _update_action_buttons(self):
        connected = self.ssh_client.is_connected() and not self._busy
        state = self._status.state if self._status else None
        repair_font = bool(
            self._status
            and self._status.package is not None
            and state is _rmkit_cn.LocalizationState.ENABLED
            and not self._status.has_cjk_font
        )
        self.enable_button.setText("修复中文字体" if repair_font else "启用中文")
        self.enable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and (
                repair_font
                or state
                in (
                    _rmkit_cn.LocalizationState.NOT_INSTALLED,
                    _rmkit_cn.LocalizationState.INSTALLED_NOT_ENABLED,
                )
            )
        )
        self.restore_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and state
            in (
                _rmkit_cn.LocalizationState.ENABLED,
                _rmkit_cn.LocalizationState.INSTALLED_NOT_ENABLED,
            )
        )
        package_available = bool(
            connected and self._status and self._status.package is not None
        )
        self.package_button.setEnabled(package_available)
        self.download_package_action.setEnabled(package_available)
        self.copy_package_link_action.setEnabled(package_available)
        self.load_package_button.setEnabled(package_available)

    @staticmethod
    def _package_display_text(package: _rmkit_cn.TranslationPackage) -> str:
        channel_names = {"stable": "正式版", "beta": "测试版"}
        return (
            f"{package.release_version} | "
            f"{channel_names[package.channel]} | "
            + (f"硬件 {package.platform.title()} | " if package.platform else "")
            + f"内部版本 {package.firmware}"
        )

    def _toggle_other_packages(self, expanded: bool):
        self.other_packages_button.setText(
            f"其他固件版本（{self._other_packages_count}） "
            + ("⌄" if expanded else "›")
        )
        self.other_packages_label.setVisible(
            expanded and not self.other_packages_button.isHidden()
        )

    def _apply_status(self, status: _rmkit_cn.LocalizationStatus):
        self._status = status
        if status.available_packages is not None:
            if status.package is not None:
                self.catalog_label.setText(
                    "当前固件汉化包：\n"
                    + self._package_display_text(status.package)
                )
            else:
                self.catalog_label.setText(
                    "当前固件汉化包：没有精确匹配版本"
                )

            other_packages = tuple(
                package
                for package in status.available_packages
                if package != status.package
            )
            self.other_packages_button.setChecked(False)
            if other_packages:
                self._other_packages_count = len(other_packages)
                self.other_packages_button.setText(
                    f"其他固件版本（{self._other_packages_count}） ›"
                )
                self.other_packages_label.setText(
                    "\n".join(
                        self._package_display_text(package)
                        for package in other_packages
                    )
                )
                self.other_packages_button.show()
            else:
                self._other_packages_count = 0
                self.other_packages_button.hide()
            self.other_packages_label.hide()
        messages = {
            _rmkit_cn.LocalizationState.INCOMPATIBLE: (
                f"云端没有与固件 {status.firmware or '未知'} 精确匹配的汉化包，未执行任何修改"
            ),
            _rmkit_cn.LocalizationState.NOT_INSTALLED: "尚未安装中文翻译",
            _rmkit_cn.LocalizationState.INSTALLED_NOT_ENABLED: (
                "已发现中文翻译，但当前未启用"
            ),
            _rmkit_cn.LocalizationState.ENABLED: "中文翻译已启用",
        }
        message = messages[status.state]
        if status.state is not _rmkit_cn.LocalizationState.INCOMPATIBLE:
            font_status = (
                "已检测到简体中文字体"
                if status.has_cjk_font
                else "未检测到简体中文字体"
            )
            message = f"{message}；{font_status}"
        self.status_label.setText(message)
        self._update_action_buttons()

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        self.detect_button.setEnabled(
            self.ssh_client.is_connected() and not busy
        )
        if message:
            self.status_label.setText(message)
        self._update_action_buttons()

    def _start_worker(
        self,
        fn,
        *args,
        pending: str,
        success: str = "",
        error_hint: str = "若设备界面无响应，请手动重启设备。",
    ):
        self._set_busy(True, pending)
        worker = _rmtool.Worker(fn, *args)

        def on_finished(status: _rmkit_cn.LocalizationStatus):
            if sip.isdeleted(self):
                # Worker outlived the tab; nothing safe left to update.
                return
            self._set_busy(False)
            self._apply_status(status)
            if success:
                show_info(self, _rmtool.APP_NAME, success)

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                # Worker outlived the tab; only log, touching widgets would
                # raise RuntimeError (and abort the process on macOS).
                logging.error("Original UI localization failed after tab close: %s", exc)
                return
            self._set_busy(False)
            self.status_label.setText("操作失败，请查看提示后重试")
            logging.error("Original UI localization failed: %s", exc)
            show_error(
                self,
                _rmtool.APP_NAME,
                f"操作失败：{exc}\n{error_hint}",
            )

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    def _start_package_worker(
        self,
        fn,
        *args,
        pending: str,
        success_status: str,
        success_message,
    ):
        self._set_busy(True, pending)
        worker = _rmtool.Worker(fn, *args)

        def on_finished(path):
            if sip.isdeleted(self):
                return
            self._set_busy(False)
            if self._status is not None:
                self._apply_status(self._status)
                self.status_label.setText(
                    f"{self.status_label.text()}；{success_status}"
                )
            else:
                self._on_connection_changed(self.ssh_client.is_connected())
            show_info(
                self,
                _rmtool.APP_NAME,
                success_message(path),
            )

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error(
                    "Localization package operation failed after tab close: %s",
                    exc,
                )
                return
            self._set_busy(False)
            if self._status is not None:
                self._apply_status(self._status)
            else:
                self._on_connection_changed(self.ssh_client.is_connected())
            logging.error("Localization package operation failed: %s", exc)
            show_error(self, _rmtool.APP_NAME, f"汉化包操作失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _detect_status(self):
        self._start_worker(
            _rmkit_cn.get_cloud_localization_status,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在获取云端清单并检测固件与汉化状态…",
            error_hint="设备未被修改，请检查电脑网络连接后重试。",
        )

    @require_connection
    def _download_package_to_computer(self):
        if not self._status or not self._status.package:
            return
        package = self._status.package
        download_dir = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.DownloadLocation
        )
        suggested_path = os.path.join(download_dir, package.asset)
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "下载汉化包",
            suggested_path,
            "Qt 翻译文件 (*.qm)",
        )
        if not destination:
            return
        if not destination.lower().endswith(".qm"):
            destination += ".qm"

        self._start_package_worker(
            _rmkit_cn.export_translation_package,
            package,
            str(_rmtool.app_state_dir()),
            destination,
            pending="正在下载并校验汉化包…",
            success_status="匹配的汉化包已保存到电脑",
            success_message=lambda path: f"汉化包已校验并保存到：\n{path}",
        )

    @require_connection
    def _copy_package_download_link(self):
        if not self._status or not self._status.package:
            return
        QtWidgets.QApplication.clipboard().setText(
            self._status.package.download_url
        )
        show_info(self, _rmtool.APP_NAME, "汉化包下载链接已复制到剪贴板。")

    @require_connection
    def _load_local_package(self):
        if not self._status or not self._status.package:
            return
        source_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "加载本地汉化包",
            "",
            "Qt 翻译文件 (*.qm)",
        )
        if not source_path:
            return

        self._start_package_worker(
            _rmkit_cn.import_translation_package,
            self._status.package,
            str(_rmtool.app_state_dir()),
            source_path,
            pending="正在校验本地汉化包…",
            success_status="本地汉化包已校验并缓存，可点击“启用中文”安装",
            success_message=lambda path: (
                "本地汉化包已通过当前固件对应的大小与 SHA-256 校验，"
                f"并写入缓存：\n{path}\n\n"
                "点击“启用中文”时会优先使用这份缓存。"
            ),
        )

    def _choose_missing_font(self) -> Optional[tuple[str, str]]:
        dialog = QtWidgets.QMessageBox(self)
        dialog.setWindowTitle(_rmtool.APP_NAME)
        dialog.setIcon(QtWidgets.QMessageBox.Warning)
        dialog.setText("设备缺少简体中文字体，请选择用于本次汉化的字体。")
        bundled_button = dialog.addButton(
            "安装内置 Noto", QtWidgets.QMessageBox.AcceptRole
        )
        local_button = dialog.addButton(
            "选择本地字体…", QtWidgets.QMessageBox.ActionRole
        )
        dialog.addButton("取消", QtWidgets.QMessageBox.RejectRole)
        dialog.exec_()
        if dialog.clickedButton() is bundled_button:
            path = str(
                _rmtool.resource_path(
                    "assets", "fonts", _rmkit_cn.BUNDLED_FONT_NAME
                )
            )
        elif dialog.clickedButton() is local_button:
            path = select_font_file(self)
            if not path:
                return None
        else:
            return None

        font_id, family = load_font_file(path)
        if font_id != -1:
            QtGui.QFontDatabase.removeApplicationFont(font_id)
        if not family:
            show_warning(self, _rmtool.APP_NAME, "无法识别所选字体的字体族。")
            return None
        return path, family

    @require_connection
    def _enable_localization(self):
        if not self._status or not self._status.package:
            return
        repair_font = self._status.state is _rmkit_cn.LocalizationState.ENABLED
        if not repair_font and not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将停止原生界面、备份当前配置并启用中文。完成后不会自动重启设备，是否继续？",
            confirm_text="启用中文",
            cancel_text="取消",
        ):
            return

        font_path = None
        font_family = None
        if not self._status.has_cjk_font:
            selected_font = self._choose_missing_font()
            if not selected_font:
                return
            font_path, font_family = selected_font
        self._start_worker(
            _rmkit_cn.enable_cloud_localization,
            self.ssh_client,
            self._status.package,
            str(_rmtool.app_state_dir()),
            font_path,
            font_family,
            pending=(
                "正在安装并验证中文字体…"
                if repair_font
                else "正在下载并校验固件对应的汉化包，然后备份并部署…"
            ),
            success=(
                "中文字体已安装并验证，SSH 会话已关闭。\n"
                if repair_font
                else "汉化文件与语言配置已写入，原生界面已停止，SSH 会话已关闭。\n"
            )
            + (
                "请手动重启设备，然后在“设置 → 语言”中选择“法语”，"
                "中文界面才会正式启用。"
            ),
        )

    @require_connection
    def _restore_localization(self):
        if not self._status or not self._status.package:
            return
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将停止原生界面并恢复汉化前的配置与翻译文件。完成后不会自动重启设备，是否继续？",
            confirm_text="还原",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _rmkit_cn.restore_localization,
            self.ssh_client,
            self._status.package,
            pending="正在还原汉化前状态…",
            success=(
                "原配置与翻译文件已还原，原生界面已停止，SSH 会话已关闭。\n"
                "请手动重启设备使修改生效。"
            ),
        )


class NativeChineseSection(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status: Optional[_native_chinese.NativeChineseStatus] = None
        self._busy = False

        title = QtWidgets.QLabel("原生简体中文")
        title.setObjectName("toolboxFeatureTitle")
        detail = QtWidgets.QLabel(
            "为精确支持的固件增加独立的“简体中文”选项，保留法语。"
            "旧版法语槽位汉化需先还原并手动重启，再连接设备启用此插件。"
        )
        detail.setWordWrap(True)
        self.catalog_label = QtWidgets.QLabel("精确包：检测后显示")
        self.catalog_label.setWordWrap(True)
        self.status_label = QtWidgets.QLabel("设备已连接，尚未检测")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.enable_button = QtWidgets.QPushButton("启用原生中文")
        self.disable_button = QtWidgets.QPushButton("停用")
        self.set_emergency_button = QtWidgets.QPushButton("紧急停用共享 Xovi")
        self.clear_emergency_button = QtWidgets.QPushButton("清除紧急停用")
        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        for button in (
            self.detect_button,
            self.enable_button,
            self.disable_button,
            self.set_emergency_button,
            self.clear_emergency_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(self.catalog_label)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.detect_button.clicked.connect(self._detect_status)
        self.enable_button.clicked.connect(self._enable)
        self.disable_button.clicked.connect(self._disable)
        self.set_emergency_button.clicked.connect(self._set_emergency)
        self.clear_emergency_button.clicked.connect(self._clear_emergency)
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._status = None
            self.catalog_label.setText("精确包：检测后显示")
            self.status_label.setText("设备未连接")
        elif self._status is None:
            self.status_label.setText("设备已连接，尚未检测")
        self._update_buttons()

    def _update_buttons(self):
        connected = self.ssh_client.is_connected() and not self._busy
        state = self._status.state if self._status else None
        self.disable_button.setText(
            "清理残留"
            if state is _native_chinese.NativeChineseState.FIRMWARE_RESIDUE
            else "停用"
        )
        self.detect_button.setEnabled(connected)
        self.enable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and state
            in (
                _native_chinese.NativeChineseState.NOT_INSTALLED,
                _native_chinese.NativeChineseState.INSTALLED_DISABLED,
            )
        )
        self.disable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.installed
            and state
            not in (
                _native_chinese.NativeChineseState.INSTALLED_DISABLED,
                _native_chinese.NativeChineseState.DISABLE_PENDING_REBOOT,
            )
        )
        self.clear_emergency_button.setEnabled(
            connected
            and self._status is not None
            and self._status.emergency_disabled
        )
        self.set_emergency_button.setEnabled(
            connected
            and self._status is not None
            and self._status.installed
            and not self._status.emergency_disabled
            and state is not _native_chinese.NativeChineseState.FIRMWARE_RESIDUE
        )

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        if message:
            self.status_label.setText(message)
        self._update_buttons()

    def _apply_status(self, status: _native_chinese.NativeChineseStatus):
        self._status = status
        package = status.package
        if package is None:
            self.catalog_label.setText("精确包：当前设备不匹配")
        else:
            channel_names = {"stable": "正式版", "beta": "测试版"}
            verification = (
                "已实机验证"
                if package.device_verified
                else "离线验证，尚待实机"
            )
            self.catalog_label.setText(
                f"精确包：{package.release_version} | {channel_names[package.channel]} | "
                f"硬件 {package.platform.title()} | 内部版本 {package.firmware} | "
                f"{verification}"
            )
        messages = {
            _native_chinese.NativeChineseState.INCOMPATIBLE: "当前设备没有精确匹配的原生中文包",
            _native_chinese.NativeChineseState.NOT_INSTALLED: "尚未安装原生简体中文",
            _native_chinese.NativeChineseState.INSTALLED_DISABLED: "原生中文当前未启用",
            _native_chinese.NativeChineseState.ENABLE_PENDING_REBOOT: "已部署，等待手动重启后生效",
            _native_chinese.NativeChineseState.ENABLED: "原生简体中文已加载",
            _native_chinese.NativeChineseState.DISABLE_PENDING_REBOOT: "已停用，等待手动重启后完全移除",
            _native_chinese.NativeChineseState.EMERGENCY_DISABLED: "共享 Xovi 已被紧急标记停用",
            _native_chinese.NativeChineseState.FIRMWARE_RESIDUE: (
                "检测到固件升级后遗留的原生中文共享 Xovi 状态，可安全清理"
            ),
            _native_chinese.NativeChineseState.BROKEN: "检测到不完整或不可信的原生中文安装",
        }
        message = messages[status.state]
        if status.detail:
            message += f"：{status.detail}"
        if status.emergency_disabled and status.state != _native_chinese.NativeChineseState.EMERGENCY_DISABLED:
            message += "\n紧急停用标记存在，共享 Xovi 不会在下次启动时载入。"
        self.status_label.setText(message)
        self._update_buttons()

    def _start_worker(
        self,
        fn,
        *args,
        pending: str,
        success: str = "",
        close_connection: bool = False,
    ):
        self._set_busy(True, pending)
        worker = _rmtool.Worker(fn, *args)

        def on_finished(status: _native_chinese.NativeChineseStatus):
            if sip.isdeleted(self):
                if close_connection:
                    self.ssh_client.close()
                return
            self._set_busy(False)
            self._apply_status(status)
            if close_connection:
                self.ssh_client.close()
            if success:
                show_info(self, _rmtool.APP_NAME, success)

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                if close_connection:
                    self.ssh_client.close()
                logging.error("Native Chinese operation failed after tab close: %s", exc)
                return
            if close_connection:
                self.ssh_client.close()
            self._set_busy(False)
            self._status = None
            self._update_buttons()
            self.status_label.setText("操作失败，未自动重启设备；请重新连接并检测状态")
            logging.error("Native Chinese operation failed: %s", exc)
            show_error(
                self,
                _rmtool.APP_NAME,
                f"操作失败：{exc}\n设备不会被自动重启。",
            )

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _detect_status(self):
        self._start_worker(
            _native_chinese.get_cloud_status,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在核对设备身份、精确包与共享 Xovi 状态…",
        )

    @require_connection
    def _enable(self):
        if not self._status or not self._status.package:
            return
        package = self._status.package
        verification_notice = (
            "该精确包已完成对应真机验证。"
            if package.device_verified
            else "该精确包已完成官方固件离线验证，尚待对应真机验证。"
        )
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"{verification_notice}"
            "它会增加独立的“简体中文”选项，不替换法语。"
            "如果旧版法语槽位汉化仍在启用，操作会被拒绝；请先还原并手动重启，"
            "再重新连接设备执行本步骤。"
            "本次只部署文件，不重启 xochitl 或设备；完成后请手动重启。"
            "是否继续？",
            confirm_text="部署并启用",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _native_chinese.enable_cloud,
            self.ssh_client,
            self._status.package,
            str(_rmtool.app_state_dir()),
            pending="正在下载、验证并部署原生中文资源…",
            success=(
                "原生简体中文已部署并通过校验，SSH 会话已关闭。\n"
                "请手动重启设备，然后在语言设置中选择“简体中文”。"
            ),
            close_connection=True,
        )

    @require_connection
    def _disable(self):
        if not self._status or not self._status.installed:
            return
        residue = (
            self._status.state
            is _native_chinese.NativeChineseState.FIRMWARE_RESIDUE
        )
        if residue:
            title = "清理旧固件原生中文残留"
            confirmation = (
                "固件升级后，旧共享 Xovi 已不再载入。将先把 zh_CN 安全改为 en，"
                "再删除经内置清单验证的整套旧共享状态；旧固件的其他 rmtool Xovi "
                "功能也会一并移除。本次不会重启设备，是否继续？"
            )
            confirm_text = "清理残留"
            pending = "正在验证并清理旧固件原生中文共享 Xovi 残留…"
            success = (
                "旧固件共享 Xovi 残留已清理，SSH 会话已关闭。\n"
                "重新连接并检测后，可安装当前固件支持的功能。"
            )
        else:
            title = _rmtool.APP_NAME
            confirmation = (
                "将停用原生简体中文。如果当前选中 zh_CN，rmtool 会先把"
                "[General] language 安全改为 en，再移除该功能的 QMD、扩展和翻译目录。"
                "其他共享 Xovi 功能会保留。操作不会重启设备，是否继续？"
            )
            confirm_text = "停用原生中文"
            pending = "正在安全停用原生简体中文…"
            success = "原生简体中文已停用，SSH 会话已关闭。\n请手动重启设备。"
        if not ask_confirmation(
            self,
            title,
            confirmation,
            confirm_text=confirm_text,
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _native_chinese.disable,
            self.ssh_client,
            _native_chinese._trusted_catalog(),
            pending=pending,
            success=success,
            close_connection=True,
        )

    @require_connection
    def _set_emergency(self):
        if (
            not self._status
            or not self._status.installed
            or self._status.emergency_disabled
        ):
            return
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将创建共享 Xovi 紧急停用标记。当前界面不会重启；下次手动重启时，"
            "点击翻页、快速黑白和原生中文等 rmtool 共享 Xovi 功能都不会载入。"
            "是否继续？",
            confirm_text="创建停用标记",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _native_chinese.set_emergency_disable,
            self.ssh_client,
            _native_chinese._trusted_catalog(),
            pending="正在创建紧急停用标记…",
            success="紧急停用标记已创建；当前界面未重启，下次手动重启时生效。",
        )

    @require_connection
    def _clear_emergency(self):
        if not self._status or not self._status.emergency_disabled:
            return
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "清除紧急停用标记后，已启用的共享 Xovi 功能会在下次手动重启时恢复加载。是否继续？",
            confirm_text="清除标记",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _native_chinese.clear_emergency_disable,
            self.ssh_client,
            _native_chinese._trusted_catalog(),
            pending="正在清除紧急停用标记…",
            success="紧急停用标记已清除；如需恢复加载，请稍后手动重启设备。",
        )


class TapPageTurnSection(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status: Optional[_tap_page_turn.TapPageTurnStatus] = None
        self._busy = False
        self._other_packages_count = 0

        title = QtWidgets.QLabel("点击翻页")
        title.setObjectName("toolboxFeatureTitle")

        detail = QtWidgets.QLabel(
            "在 PDF 和 EPUB 阅读页使用屏幕分区点击上一页或下一页，滑动翻页保持可用。"
            "功能按硬件、内部固件版本和 xochitl 哈希精确匹配，并在冷启动后持续生效。"
        )
        detail.setWordWrap(True)

        self.catalog_label = QtWidgets.QLabel("云端点击翻页包：检测后显示")
        self.catalog_label.setObjectName("tapPageTurnCatalog")
        self.catalog_label.setWordWrap(True)
        self.catalog_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        self.other_packages_button = QtWidgets.QPushButton("其他固件版本")
        self.other_packages_button.setCheckable(True)
        self.other_packages_button.setSizePolicy(
            QtWidgets.QSizePolicy.Maximum,
            QtWidgets.QSizePolicy.Preferred,
        )
        self.other_packages_button.hide()

        self.other_packages_label = QtWidgets.QLabel()
        self.other_packages_label.setObjectName("tapPageTurnOtherCatalog")
        self.other_packages_label.setWordWrap(True)
        self.other_packages_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse
        )
        self.other_packages_label.hide()

        self.status_label = QtWidgets.QLabel("设备已连接，尚未检测")
        self.status_label.setObjectName("tapPageTurnDeviceStatus")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.enable_button = QtWidgets.QPushButton("启用点击翻页")
        self.disable_button = QtWidgets.QPushButton("停用")
        self.vellum_help_button = QtWidgets.QPushButton("Vellum 官方卸载")
        self.vellum_help_button.hide()
        self.project_button = QtWidgets.QPushButton("查看说明")

        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        buttons.addWidget(self.detect_button)
        buttons.addWidget(self.enable_button)
        buttons.addWidget(self.disable_button)
        buttons.addWidget(self.vellum_help_button)
        buttons.addWidget(self.project_button)
        buttons.addStretch()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(self.catalog_label)
        layout.addWidget(
            self.other_packages_button,
            alignment=QtCore.Qt.AlignLeft,
        )
        layout.addWidget(self.other_packages_label)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.other_packages_button.toggled.connect(
            self._toggle_other_packages
        )
        self.detect_button.clicked.connect(self._detect_status)
        self.enable_button.clicked.connect(self._enable)
        self.disable_button.clicked.connect(self._disable)
        self.vellum_help_button.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl(_tap_page_turn.VELLUM_UNINSTALL_URL)
            )
        )
        self.project_button.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl(f"{_tap_page_turn.REPO_URL}/tree/main/tap-page-turn")
            )
        )
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._status = None
            self.catalog_label.setText("云端点击翻页包：检测后显示")
            self.other_packages_button.setChecked(False)
            self._other_packages_count = 0
            self.other_packages_button.setText("其他固件版本")
            self.other_packages_label.clear()
            self.other_packages_button.hide()
            self.other_packages_label.hide()
            self.status_label.setText("设备未连接")
        elif self._status is None:
            self.status_label.setText("设备已连接，尚未检测")
        self.detect_button.setEnabled(connected and not self._busy)
        self._update_buttons()

    @staticmethod
    def _package_display_text(package: _tap_page_turn.TapPageTurnPackage) -> str:
        channel_names = {"stable": "正式版", "beta": "测试版"}
        return (
            f"{package.release_version} | {channel_names[package.channel]} | "
            f"硬件 {package.platform.title()} | 内部版本 {package.firmware}"
        )

    def _toggle_other_packages(self, expanded: bool):
        self.other_packages_button.setText(
            f"其他固件版本（{self._other_packages_count}） "
            + ("⌄" if expanded else "›")
        )
        self.other_packages_label.setVisible(
            expanded and not self.other_packages_button.isHidden()
        )

    def _update_buttons(self):
        connected = self.ssh_client.is_connected() and not self._busy
        state = self._status.state if self._status else None
        if state == _tap_page_turn.TapPageTurnState.FIRMWARE_RESIDUE:
            self.disable_button.setText("清理残留")
        elif state in (
            _tap_page_turn.TapPageTurnState.OUTDATED,
            _tap_page_turn.TapPageTurnState.LEGACY_VELLUM,
        ):
            self.disable_button.setText("卸载旧版")
        else:
            self.disable_button.setText("停用")
        self.enable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and state
            in (
                _tap_page_turn.TapPageTurnState.NOT_INSTALLED,
                _tap_page_turn.TapPageTurnState.INSTALLED_DISABLED,
            )
        )
        self.disable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.dropin_present
            and state
            not in (
                _tap_page_turn.TapPageTurnState.NOT_INSTALLED,
                _tap_page_turn.TapPageTurnState.INSTALLED_DISABLED,
            )
        )
        self.vellum_help_button.setVisible(
            state == _tap_page_turn.TapPageTurnState.VELLUM_RUNTIME
        )

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        self.detect_button.setEnabled(
            self.ssh_client.is_connected() and not busy
        )
        if message:
            self.status_label.setText(message)
        self._update_buttons()

    def _apply_status(self, status: _tap_page_turn.TapPageTurnStatus):
        self._status = status
        if status.package is not None:
            self.catalog_label.setText(
                "当前固件点击翻页包：\n"
                + self._package_display_text(status.package)
            )
        else:
            self.catalog_label.setText(
                "当前固件点击翻页包：没有精确匹配版本"
            )

        other_packages = tuple(
            package
            for package in status.available_packages
            if package != status.package
            and package.platform == status.identity.platform
        )
        self.other_packages_button.setChecked(False)
        if other_packages:
            self._other_packages_count = len(other_packages)
            self.other_packages_button.setText(
                f"其他固件版本（{self._other_packages_count}） ›"
            )
            self.other_packages_label.setText(
                "\n".join(
                    self._package_display_text(package)
                    for package in other_packages
                )
            )
            self.other_packages_button.show()
        else:
            self._other_packages_count = 0
            self.other_packages_button.setText("其他固件版本")
            self.other_packages_label.clear()
            self.other_packages_button.hide()
        self.other_packages_label.hide()

        messages = {
            _tap_page_turn.TapPageTurnState.INCOMPATIBLE: (
                "没有与当前设备、固件和 xochitl 哈希精确匹配的点击翻页包"
            ),
            _tap_page_turn.TapPageTurnState.NOT_INSTALLED: "尚未安装点击翻页",
            _tap_page_turn.TapPageTurnState.INSTALLED_DISABLED: (
                "点击翻页资源已缓存，持久化当前未启用"
            ),
            _tap_page_turn.TapPageTurnState.ENABLE_PENDING_REBOOT: (
                "持久化已部署，等待冷启动生效"
            ),
            _tap_page_turn.TapPageTurnState.ENABLED: "点击翻页已启用并正在运行",
            _tap_page_turn.TapPageTurnState.DISABLE_PENDING_REBOOT: (
                "持久化已停用，当前进程将在冷启动后恢复原生"
            ),
            _tap_page_turn.TapPageTurnState.OUTDATED: (
                "检测到旧固件的 rmtool 点击翻页，请先卸载旧版"
            ),
            _tap_page_turn.TapPageTurnState.LEGACY_VELLUM: (
                "检测到 rmtool 安装的旧版 Vellum 点击翻页包，请先卸载"
            ),
            _tap_page_turn.TapPageTurnState.VELLUM_RUNTIME: (
                "Vellum/AppLoader Xovi 仍在设备中，rmtool 插件安装已暂停"
            ),
            _tap_page_turn.TapPageTurnState.FIRMWARE_RESIDUE: (
                "检测到固件升级后遗留的旧共享 Xovi 状态，可安全清理"
            ),
            _tap_page_turn.TapPageTurnState.BROKEN: (
                "检测到不完整或被修改的点击翻页安装，请先停用"
            ),
        }
        message = messages[status.state]
        if status.detail:
            message = f"{message}：{status.detail}"
        identity = status.identity
        message += (
            f"\n设备：{identity.platform or '未知'} | "
            f"内部版本 {identity.firmware or '未知'}"
        )
        self.status_label.setText(message)
        self._update_buttons()

    def _start_worker(
        self,
        fn,
        *args,
        pending: str,
        success: str = "",
        close_connection: bool = False,
    ):
        self._set_busy(True, pending)
        worker = _rmtool.Worker(fn, *args)

        def on_finished(status: _tap_page_turn.TapPageTurnStatus):
            if sip.isdeleted(self):
                # Worker outlived the tab; nothing safe left to update.
                return
            self._set_busy(False)
            self._apply_status(status)
            if close_connection:
                self.ssh_client.close()
            if success:
                show_info(self, _rmtool.APP_NAME, success)

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                # Worker outlived the tab; only log, touching widgets would
                # raise RuntimeError (and abort the process on macOS).
                logging.error("Tap-to-turn operation failed after tab close: %s", exc)
                return
            self._set_busy(False)
            self.status_label.setText("操作失败，未自动重启设备")
            logging.error("Tap-to-turn operation failed: %s", exc)
            show_error(
                self,
                _rmtool.APP_NAME,
                f"操作失败：{exc}\n设备不会被自动重启，请检查日志后重试。",
            )

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _detect_status(self):
        self._start_worker(
            _tap_page_turn.get_cloud_status,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在获取云端清单并核对设备、固件与 xochitl 哈希…",
        )

    @require_connection
    def _enable(self):
        if not self._status or not self._status.package:
            return
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将下载并校验固件专用资源，并安装到 rmtool 管理的共享 Xovi；"
            "点击翻页可与快速黑白和原生简体中文共用同一运行时。"
            "若检测到 Vellum/AppLoader Xovi，安装会在上传前停止。"
            "本次操作不会重启界面或设备；完成后 SSH 会话会关闭，请从设备菜单手动冷启动。"
            "是否继续？",
            confirm_text="部署持久化",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _tap_page_turn.enable_cloud,
            self.ssh_client,
            self._status.package,
            str(_rmtool.app_state_dir()),
            pending="正在下载、逐文件校验并部署点击翻页资源…",
            success=(
                "点击翻页持久化已部署并通过校验，SSH 会话已关闭。\n"
                "请从设备菜单手动重新启动；不要通过 rmtool 立即重启。"
            ),
            close_connection=True,
        )

    @require_connection
    def _disable(self):
        if not self._status or not self._status.dropin_present:
            return
        outdated = self._status.state == _tap_page_turn.TapPageTurnState.OUTDATED
        legacy_vellum = (
            self._status.state
            == _tap_page_turn.TapPageTurnState.LEGACY_VELLUM
        )
        residue = (
            self._status.state
            == _tap_page_turn.TapPageTurnState.FIRMWARE_RESIDUE
        )
        if residue:
            title = "清理旧固件共享残留"
            confirmation = (
                "固件升级已移除上下层启动配置，旧点击翻页和旧快速黑白均未载入。"
                "将删除经内置清单逐文件验证的整套旧共享状态；不会在当前固件重建旧组件，"
                "也不会重启设备。清理后可分别安装两项功能的当前版本。是否继续？"
            )
            confirm_text = "清理残留"
            pending = "正在验证并清理旧固件共享 Xovi 残留…"
            success = (
                "旧固件共享 Xovi 残留已完整清理，SSH 会话已关闭。\n"
                "重新连接并检测后，可安装当前固件的点击翻页和快速黑白。"
            )
        elif legacy_vellum:
            title = "卸载旧版 Vellum 点击翻页"
            confirmation = (
                "将通过 Vellum 仅卸载已精确验证的 rmtool-tap-page-turn 包。"
                "不会卸载 Vellum、AppLoader、Xovi 或任何第三方包。"
                "完成后请按界面中的官方链接自行卸载 Vellum 运行环境，"
                "再重新检测并安装 rmtool 共享 Xovi 版本。是否继续？"
            )
            confirm_text = "卸载旧版"
            pending = "正在验证并卸载 rmtool 的旧版 Vellum 点击翻页包…"
            success = (
                "旧版 Vellum 点击翻页包已卸载，SSH 会话已关闭。\n"
                "请重新连接，按 Vellum 官方说明卸载其运行环境后，再安装 rmtool 版本。"
            )
        elif outdated:
            title = "卸载旧版点击翻页"
            confirmation = (
                "将卸载经精确验证的旧固件点击翻页。若快速黑白仍存在，"
                "其 QMD 和所需共享 Xovi 组件会保留。操作不会重启设备；"
                "完成后请手动重启，再重新检测并安装当前版本。是否继续？"
            )
            confirm_text = "卸载旧版"
            pending = "正在卸载旧固件点击翻页并保留可验证的同伴功能…"
            success = (
                "点击翻页持久化已移除，SSH 会话已关闭。\n"
                "请从设备菜单手动重新启动；若快速黑白仍启用，相关共享 Xovi 组件会继续保留。"
            )
        else:
            title = _rmtool.APP_NAME
            confirmation = (
                "将停用 rmtool 共享 Xovi 中的点击翻页配置；"
                "其他 rmtool 功能及其共享运行时会按需保留。"
                "资源缓存会保留，本次操作不会重启界面或设备；完成后请手动冷启动。"
                "是否继续？"
            )
            confirm_text = "停用点击翻页"
            pending = "正在移除点击翻页持久化配置…"
            success = (
                "点击翻页持久化已移除，SSH 会话已关闭。\n"
                "请从设备菜单手动重新启动；若快速黑白仍启用，相关共享 Xovi 组件会继续保留。"
            )
        if not ask_confirmation(
            self,
            title,
            confirmation,
            confirm_text=confirm_text,
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _tap_page_turn.disable,
            self.ssh_client,
            self._status.available_packages,
            pending=pending,
            success=success,
            close_connection=True,
        )


class FastMonoReadingSection(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status: Optional[_fast_mono_reading.FastMonoReadingStatus] = None
        self._busy = False
        self._other_packages_count = 0

        title = QtWidgets.QLabel("快速黑白阅读")
        title.setObjectName("toolboxFeatureTitle")
        detail = QtWidgets.QLabel(
            "为精确支持的彩色 reMarkable 固件安装 PDF/EPUB 阅读菜单中的“快速黑白”开关。"
            "开启后黑白文字翻页更利落，但暂时不显示彩色并可能增加残影；关闭即恢复原生刷新模式。"
            "可选择每 5、10、20、30 次真实翻页调用系统清屏，或从不；默认每 10 次。"
            "包会标明实机或离线验证级别；开关仅在当前 xochitl 会话有效，每次重启后默认关闭。"
        )
        detail.setWordWrap(True)

        self.catalog_label = QtWidgets.QLabel("快速黑白包：检测后显示")
        self.catalog_label.setObjectName("fastMonoReadingCatalog")
        self.catalog_label.setWordWrap(True)
        self.catalog_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)

        self.other_packages_button = QtWidgets.QPushButton("其他固件版本")
        self.other_packages_button.setCheckable(True)
        self.other_packages_button.setSizePolicy(
            QtWidgets.QSizePolicy.Maximum,
            QtWidgets.QSizePolicy.Preferred,
        )
        self.other_packages_button.hide()

        self.other_packages_label = QtWidgets.QLabel()
        self.other_packages_label.setObjectName("fastMonoReadingOtherCatalog")
        self.other_packages_label.setWordWrap(True)
        self.other_packages_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse
        )
        self.other_packages_label.hide()
        self.status_label = QtWidgets.QLabel("设备已连接，尚未检测")
        self.status_label.setObjectName("fastMonoReadingDeviceStatus")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.enable_button = QtWidgets.QPushButton("安装并启用")
        self.disable_button = QtWidgets.QPushButton("停用")
        self.clear_button = QtWidgets.QPushButton("清除状态")
        self.vellum_help_button = QtWidgets.QPushButton("Vellum 官方卸载")
        self.vellum_help_button.hide()
        self.project_button = QtWidgets.QPushButton("查看说明")

        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        for button in (
            self.detect_button,
            self.enable_button,
            self.disable_button,
            self.clear_button,
            self.vellum_help_button,
            self.project_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(self.catalog_label)
        layout.addWidget(
            self.other_packages_button,
            alignment=QtCore.Qt.AlignLeft,
        )
        layout.addWidget(self.other_packages_label)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.other_packages_button.toggled.connect(
            self._toggle_other_packages
        )
        self.detect_button.clicked.connect(self._detect_status)
        self.enable_button.clicked.connect(self._enable)
        self.disable_button.clicked.connect(self._disable)
        self.clear_button.clicked.connect(self._clear_status)
        self.vellum_help_button.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl(_tap_page_turn.VELLUM_UNINSTALL_URL)
            )
        )
        self.project_button.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl(
                    f"{_fast_mono_reading.REPO_URL}/tree/main/fast-mono-reading"
                )
            )
        )
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._status = None
            self.catalog_label.setText("快速黑白包：检测后显示")
            self.other_packages_button.setChecked(False)
            self._other_packages_count = 0
            self.other_packages_button.setText("其他固件版本")
            self.other_packages_label.clear()
            self.other_packages_button.hide()
            self.other_packages_label.hide()
            self.status_label.setText("设备未连接")
        elif self._status is None:
            self.status_label.setText("设备已连接，尚未检测")
        self.detect_button.setEnabled(connected and not self._busy)
        self._update_buttons()

    @staticmethod
    def _package_display_text(
        package: _fast_mono_reading.FastMonoReadingPackage,
    ) -> str:
        channel_names = {"stable": "正式版", "beta": "测试版"}
        verification = (
            "实机验证" if package.device_verified else "离线验证，尚待实机"
        )
        return (
            f"{package.release_version} | {channel_names[package.channel]} | "
            f"硬件 {package.platform.title()} | 内部版本 {package.firmware} | {verification}"
        )

    def _toggle_other_packages(self, expanded: bool):
        self.other_packages_button.setText(
            f"其他固件版本（{self._other_packages_count}） "
            + ("⌄" if expanded else "›")
        )
        self.other_packages_label.setVisible(
            expanded and not self.other_packages_button.isHidden()
        )

    def _update_buttons(self):
        connected = self.ssh_client.is_connected() and not self._busy
        state = self._status.state if self._status else None
        if state == _fast_mono_reading.FastMonoReadingState.FIRMWARE_RESIDUE:
            self.disable_button.setText("清理残留")
        elif state in (
            _fast_mono_reading.FastMonoReadingState.OUTDATED,
            _fast_mono_reading.FastMonoReadingState.LEGACY_VELLUM,
        ):
            self.disable_button.setText("卸载旧版")
        else:
            self.disable_button.setText("停用")
        self.enable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and state
            in (
                _fast_mono_reading.FastMonoReadingState.NOT_INSTALLED,
                _fast_mono_reading.FastMonoReadingState.INSTALLED_DISABLED,
            )
        )
        self.disable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.recovery_available
            and state
            not in (
                _fast_mono_reading.FastMonoReadingState.NOT_INSTALLED,
                _fast_mono_reading.FastMonoReadingState.INSTALLED_DISABLED,
            )
        )
        self.clear_button.setEnabled(connected and self._status is not None)
        self.vellum_help_button.setVisible(
            state == _fast_mono_reading.FastMonoReadingState.VELLUM_RUNTIME
        )

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        self.detect_button.setEnabled(self.ssh_client.is_connected() and not busy)
        if message:
            self.status_label.setText(message)
        self._update_buttons()

    def _apply_status(self, status: _fast_mono_reading.FastMonoReadingStatus):
        self._status = status
        if status.package is not None:
            self.catalog_label.setText(
                "当前固件快速黑白包：\n"
                + self._package_display_text(status.package)
            )
        else:
            self.catalog_label.setText(
                "当前固件快速黑白包：没有精确匹配版本"
            )

        other_packages = tuple(
            package
            for package in status.available_packages
            if package != status.package
            and package.platform == status.identity.platform
        )
        self.other_packages_button.setChecked(False)
        if other_packages:
            self._other_packages_count = len(other_packages)
            self.other_packages_button.setText(
                f"其他固件版本（{self._other_packages_count}） ›"
            )
            self.other_packages_label.setText(
                "\n".join(
                    self._package_display_text(package)
                    for package in other_packages
                )
            )
            self.other_packages_button.show()
        else:
            self._other_packages_count = 0
            self.other_packages_button.setText("其他固件版本")
            self.other_packages_label.clear()
            self.other_packages_button.hide()
        self.other_packages_label.hide()
        messages = {
            _fast_mono_reading.FastMonoReadingState.INCOMPATIBLE: (
                "当前设备的硬件、固件、架构或 xochitl 哈希没有精确匹配包"
            ),
            _fast_mono_reading.FastMonoReadingState.NOT_INSTALLED: "尚未安装快速黑白阅读",
            _fast_mono_reading.FastMonoReadingState.INSTALLED_DISABLED: (
                "快速黑白资源已保留，持久化当前未启用"
            ),
            _fast_mono_reading.FastMonoReadingState.ENABLE_PENDING_REBOOT: (
                "持久化已部署，等待手动重启设备生效"
            ),
            _fast_mono_reading.FastMonoReadingState.ENABLED: (
                "快速黑白阅读扩展已加载；PDF/EPUB 菜单开关默认关闭"
            ),
            _fast_mono_reading.FastMonoReadingState.DISABLE_PENDING_REBOOT: (
                "持久化已停用，当前进程将在手动重启后恢复原生"
            ),
            _fast_mono_reading.FastMonoReadingState.OUTDATED: (
                "检测到 rmtool 安装的旧版快速黑白，请先卸载旧版"
            ),
            _fast_mono_reading.FastMonoReadingState.LEGACY_VELLUM: (
                "检测到 rmtool 安装的旧版 Vellum 快速黑白包，请先卸载"
            ),
            _fast_mono_reading.FastMonoReadingState.VELLUM_RUNTIME: (
                "Vellum/AppLoader Xovi 仍在设备中，rmtool 插件安装已暂停"
            ),
            _fast_mono_reading.FastMonoReadingState.FIRMWARE_RESIDUE: (
                "检测到固件升级后遗留的旧共享 Xovi 状态，可安全清理"
            ),
            _fast_mono_reading.FastMonoReadingState.BROKEN: (
                "检测到不完整或被修改的快速黑白安装，请先停用"
            ),
        }
        message = messages[status.state]
        if status.detail:
            message = f"{message}：{status.detail}"
        identity = status.identity
        message += (
            f"\n设备：{identity.platform or '未知'} | "
            f"内部版本 {identity.firmware or '未知'}"
        )
        self.status_label.setText(message)
        self._update_buttons()

    def _clear_status(self):
        if self._busy:
            return
        self._status = None
        self.catalog_label.setText("快速黑白包：检测后显示")
        self.other_packages_button.setChecked(False)
        self._other_packages_count = 0
        self.other_packages_button.setText("其他固件版本")
        self.other_packages_label.clear()
        self.other_packages_button.hide()
        self.other_packages_label.hide()
        self.status_label.setText(
            "设备已连接，尚未检测"
            if self.ssh_client.is_connected()
            else "设备未连接"
        )
        self._update_buttons()

    def _start_worker(
        self,
        fn,
        *args,
        pending: str,
        success: str = "",
        close_connection: bool = False,
    ):
        self._set_busy(True, pending)
        worker = _rmtool.Worker(fn, *args)

        def on_finished(status: _fast_mono_reading.FastMonoReadingStatus):
            if sip.isdeleted(self):
                if close_connection:
                    self.ssh_client.close()
                return
            self._set_busy(False)
            self._apply_status(status)
            if close_connection:
                self.ssh_client.close()
            if success:
                show_info(self, _rmtool.APP_NAME, success)

        def on_error(exc: Exception):
            if close_connection:
                self.ssh_client.close()
            if sip.isdeleted(self):
                logging.error("Fast-mono operation failed after tab close: %s", exc)
                return
            self._set_busy(False)
            self._status = None
            self._update_buttons()
            self.status_label.setText("操作失败，未自动重启设备；请重新连接并检测状态")
            logging.error("Fast-mono operation failed: %s", exc)
            show_error(
                self,
                _rmtool.APP_NAME,
                f"操作失败：{exc}\n设备不会被自动重启，请检查日志后重试。",
            )

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _detect_status(self):
        self._start_worker(
            _fast_mono_reading.get_cloud_status,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在核对彩色设备、固件、架构与 xochitl 哈希…",
        )

    @require_connection
    def _enable(self):
        if not self._status or not self._status.package:
            return
        package = self._status.package
        if package.device_verified:
            verification_notice = "该精确包已完成对应真机验证。"
        else:
            verification_notice = (
                "该包仅完成官方固件离线兼容性与回放验证，尚未在对应真机验证。"
                "安装仍有界面无法启动或需要恢复的风险；确认承担风险后再继续。"
            )
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"将安装 {package.platform.title()} {package.release_version} "
            f"（内部版本 {package.firmware}）快速黑白阅读扩展。"
            f"{verification_notice}"
            "扩展将安装到 rmtool 管理的共享 Xovi，并与点击翻页和原生简体中文共用同一运行时。"
            "若检测到非托管 Xovi，将在上传前拒绝操作。"
            "本次不会重启 xochitl 或设备；完成后 SSH 会话会关闭，请手动重启设备。"
            "重启后请在 PDF/EPUB 阅读页的更多菜单中按需开启“快速黑白”，每次重启默认关闭。"
            "是否继续？",
            confirm_text="安装并启用",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _fast_mono_reading.enable_cloud,
            self.ssh_client,
            self._status.package,
            str(_rmtool.app_state_dir()),
            pending="正在下载、逐文件校验并部署快速黑白阅读资源…",
            success=(
                "快速黑白阅读扩展已部署并通过校验，SSH 会话已关闭。\n"
                "请手动重启设备；随后在 PDF/EPUB 阅读页的更多菜单中开启“快速黑白”。\n"
                "该阅读开关每次 xochitl 启动后默认关闭。"
            ),
            close_connection=True,
        )

    @require_connection
    def _disable(self):
        if not self._status or not self._status.recovery_available:
            return
        outdated = (
            self._status.state
            == _fast_mono_reading.FastMonoReadingState.OUTDATED
        )
        legacy_vellum = (
            self._status.state
            == _fast_mono_reading.FastMonoReadingState.LEGACY_VELLUM
        )
        residue = (
            self._status.state
            == _fast_mono_reading.FastMonoReadingState.FIRMWARE_RESIDUE
        )
        if residue:
            dialog_title = "清理旧固件共享残留"
            confirmation = (
                "固件升级已移除上下层启动配置，旧点击翻页和旧快速黑白均未载入。"
                "将删除经内置清单逐文件验证的整套旧共享状态；不会在当前固件重建旧组件，"
                "也不会重启设备。清理后可分别安装两项功能的当前版本。是否继续？"
            )
            confirm_text = "清理残留"
            pending = "正在验证并清理旧固件共享 Xovi 残留…"
            success = (
                "旧固件共享 Xovi 残留已完整清理，SSH 会话已关闭。\n"
                "重新连接并检测后，可安装当前固件的点击翻页和快速黑白。"
            )
        elif legacy_vellum:
            dialog_title = "卸载旧版 Vellum 快速黑白"
            confirmation = (
                "将通过 Vellum 仅卸载已精确验证的 rmtool-fast-mono-reading 包。"
                "不会卸载 Vellum、AppLoader、Xovi、点击翻页或任何第三方包。"
                "完成后请按界面中的官方链接自行卸载 Vellum 运行环境，"
                "再重新检测并安装 rmtool 共享 Xovi 版本。是否继续？"
            )
            confirm_text = "卸载旧版"
            pending = "正在验证并卸载 rmtool 的旧版 Vellum 快速黑白包…"
            success = (
                "旧版 Vellum 快速黑白包已卸载，SSH 会话已关闭。\n"
                "请重新连接，按 Vellum 官方说明卸载其运行环境后，再安装 rmtool 版本。"
            )
        elif outdated:
            dialog_title = "卸载旧版快速黑白"
            confirmation = (
                "将卸载 rmtool 安装的旧版快速黑白阅读。"
                "只移除旧版快速黑白；点击翻页及其所需的共享 Xovi 组件会完整保留。"
                "操作不会重启设备；完成后请手动重启，再重新检测并安装新版。"
                "是否卸载旧版？"
            )
            confirm_text = "卸载旧版"
            pending = "正在卸载旧版快速黑白并保留点击翻页所需的共享 Xovi 组件…"
            success = (
                "旧版快速黑白已卸载，点击翻页所需的共享 Xovi 组件已保留，"
                "SSH 会话已关闭。\n"
                "请手动重启设备，然后重新检测并安装新版快速黑白。"
            )
        else:
            dialog_title = _rmtool.APP_NAME
            confirmation = (
                "将停用 rmtool 共享 Xovi 中的快速黑白阅读配置。"
                "点击翻页、原生简体中文及共享运行时会按需保留。"
                "操作不会重启设备；完成后请手动重启。"
                "是否继续？"
            )
            confirm_text = "停用快速黑白"
            pending = "正在移除快速黑白阅读持久化配置…"
            success = (
                "快速黑白阅读持久化已移除，SSH 会话已关闭。\n"
                "请手动重启设备；若点击翻页仍启用，相关共享 Xovi 组件会继续保留。"
            )
        if not ask_confirmation(
            self,
            dialog_title,
            confirmation,
            confirm_text=confirm_text,
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _fast_mono_reading.disable,
            self.ssh_client,
            self._status.available_packages,
            pending=pending,
            success=success,
            close_connection=True,
        )


class LegacyVellumCleanupSection(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._busy = False

        self.status_label = QtWidgets.QLabel(
            "仅清理 rmtool 历史安装的点击翻页和快速黑白 Vellum 包；"
            "不会卸载 Vellum、AppLoader、Xovi 或其他插件。"
        )
        self.status_label.setWordWrap(True)
        self.cleanup_button = QtWidgets.QPushButton("一键卸载旧版插件")
        self.cleanup_button.clicked.connect(self._cleanup)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(self.status_label)
        layout.addWidget(self.cleanup_button, 0, QtCore.Qt.AlignLeft)

        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    def _on_connection_changed(self, connected: bool):
        self.cleanup_button.setEnabled(connected and not self._busy)

    @require_connection
    def _cleanup(self):
        if not ask_confirmation(
            self,
            "一键卸载旧版插件",
            "rmtool 会先完整验证检测到的两个历史 Vellum 功能包，全部通过后才开始删除。"
            "任一包的标记、版本、所有权或文件哈希异常时，将不会删除任何包。"
            "Vellum、AppLoader、Xovi、当前 rmtool 共享功能和第三方插件都不会被卸载。"
            "是否继续？",
            confirm_text="卸载旧版插件",
            cancel_text="取消",
            danger=True,
        ):
            return

        self._busy = True
        self._on_connection_changed(True)
        self.status_label.setText("正在验证并卸载 rmtool 历史 Vellum 功能包…")
        worker = _rmtool.Worker(_legacy_vellum.remove_legacy_plugins, self.ssh_client)

        def on_finished(removed: tuple[str, ...]):
            if sip.isdeleted(self):
                return
            self._busy = False
            self._on_connection_changed(self.ssh_client.is_connected())
            if removed:
                names = "、".join(removed)
                self.status_label.setText(f"已卸载：{names}")
                show_info(
                    self,
                    _rmtool.APP_NAME,
                    "已卸载通过验证的 rmtool 旧版插件。Vellum/AppLoader/Xovi 本体仍保留。",
                )
            else:
                self.status_label.setText("未检测到 rmtool 安装的旧版 Vellum 插件")
                show_info(self, _rmtool.APP_NAME, "没有需要卸载的 rmtool 旧版插件。")

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error("Legacy Vellum cleanup failed after tab close: %s", exc)
                return
            self._busy = False
            self._on_connection_changed(self.ssh_client.is_connected())
            self.status_label.setText(f"旧版插件清理失败：{exc}")
            logging.error("Legacy Vellum cleanup failed: %s", exc)
            show_error(self, _rmtool.APP_NAME, f"操作失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)


class ToolboxTab(QtWidgets.QWidget):
    def __init__(
        self,
        ssh_client: SSHClientWrapper,
        config: Dict,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent)
        self.time_section = TimeTab(ssh_client)
        self.control_section = ControlTab(ssh_client)
        self.rmkit_cn_section = RmkitCnSection(ssh_client)
        self.native_chinese_section = NativeChineseSection(ssh_client)
        self.tap_page_turn_section = TapPageTurnSection(ssh_client)
        self.fast_mono_reading_section = FastMonoReadingSection(ssh_client)
        self.legacy_vellum_cleanup_section = LegacyVellumCleanupSection(ssh_client)

        time_group = QtWidgets.QGroupBox("时间管理")
        time_layout = QtWidgets.QVBoxLayout()
        time_layout.setContentsMargins(0, _rmtool.SUBSECTION_GAP, 0, 0)
        time_layout.addWidget(self.time_section)
        time_group.setLayout(time_layout)

        control_group = QtWidgets.QGroupBox("设备控制")
        control_layout = QtWidgets.QVBoxLayout()
        control_layout.setContentsMargins(0, _rmtool.SUBSECTION_GAP, 0, 0)
        control_layout.addWidget(self.control_section)
        control_group.setLayout(control_layout)

        rmkit_cn_group = QtWidgets.QGroupBox("系统汉化")
        rmkit_cn_layout = QtWidgets.QVBoxLayout()
        rmkit_cn_layout.setContentsMargins(0, _rmtool.SUBSECTION_GAP, 0, 0)
        rmkit_cn_layout.addWidget(self.rmkit_cn_section)
        localization_divider = QtWidgets.QFrame()
        localization_divider.setFrameShape(QtWidgets.QFrame.HLine)
        localization_divider.setFrameShadow(QtWidgets.QFrame.Sunken)
        rmkit_cn_layout.addWidget(localization_divider)
        rmkit_cn_layout.addWidget(self.native_chinese_section)
        rmkit_cn_group.setLayout(rmkit_cn_layout)

        tap_page_turn_group = QtWidgets.QGroupBox("阅读优化与手势")
        tap_page_turn_layout = QtWidgets.QVBoxLayout()
        tap_page_turn_layout.setContentsMargins(
            0, _rmtool.SUBSECTION_GAP, 0, 0
        )
        tap_page_turn_layout.addWidget(self.tap_page_turn_section)
        divider = QtWidgets.QFrame()
        divider.setFrameShape(QtWidgets.QFrame.HLine)
        divider.setFrameShadow(QtWidgets.QFrame.Sunken)
        tap_page_turn_layout.addWidget(divider)
        tap_page_turn_layout.addWidget(self.fast_mono_reading_section)
        cleanup_divider = QtWidgets.QFrame()
        cleanup_divider.setFrameShape(QtWidgets.QFrame.HLine)
        cleanup_divider.setFrameShadow(QtWidgets.QFrame.Sunken)
        tap_page_turn_layout.addWidget(cleanup_divider)
        tap_page_turn_layout.addWidget(self.legacy_vellum_cleanup_section)
        tap_page_turn_group.setLayout(tap_page_turn_layout)

        koreader_group = QtWidgets.QGroupBox("KOReader / 第三方应用")
        koreader_info = QtWidgets.QLabel(
            "安装 KOReader 等第三方应用仍可使用 Vellum；rmtool 自带插件已改用独立的共享 Xovi，"
            "不会代装或卸载 Vellum。请参考以下项目文档：\n"
        )
        koreader_info.setWordWrap(True)

        koreader_links = QtWidgets.QLabel(
            '<a href="https://github.com/vellum-dev/vellum-cli#usage">'
            "Vellum（安装与官方卸载）</a>"
            '  |  <a href="https://github.com/asivery/rm-xovi-extensions">'
            "xovi (扩展框架)</a>"
            '  |  <a href="https://github.com/asivery/rm-appload">'
            "rm-appload (应用加载器)</a>"
            '  |  <a href="https://github.com/koreader/koreader/wiki/'
            'Installation-on-Remarkable">KOReader 安装指南</a>'
        )
        koreader_links.setOpenExternalLinks(True)
        koreader_links.setWordWrap(True)

        koreader_layout = QtWidgets.QVBoxLayout()
        koreader_layout.setContentsMargins(0, _rmtool.SUBSECTION_GAP, 0, 0)
        koreader_layout.addWidget(koreader_info)
        koreader_layout.addWidget(koreader_links)
        koreader_group.setLayout(koreader_layout)

        self.content_widget = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(self.content_widget)
        content_layout.setContentsMargins(
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
        )
        content_layout.setSpacing(_rmtool.PANEL_GAP)
        content_layout.addWidget(time_group)
        content_layout.addWidget(control_group)
        content_layout.addWidget(rmkit_cn_group)
        content_layout.addWidget(tap_page_turn_group)
        content_layout.addWidget(koreader_group)
        content_layout.addStretch()

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(self.content_widget)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)


class FontPage(QtWidgets.QWidget):
    """Top-level font management page (FontTab extracted from ToolboxTab)."""

    def __init__(
        self,
        ssh_client: SSHClientWrapper,
        config: Dict,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent)
        self.font_section = FontTab(ssh_client, config)

        font_group = QtWidgets.QGroupBox("字体管理")
        font_layout = QtWidgets.QVBoxLayout()
        font_layout.setContentsMargins(0, 0, 0, 0)
        font_layout.addWidget(self.font_section)
        font_group.setLayout(font_layout)

        content_widget = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content_widget)
        content_layout.setContentsMargins(
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
        )
        content_layout.setSpacing(_rmtool.PANEL_GAP)
        content_layout.addWidget(font_group)
        content_layout.addStretch()

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(content_widget)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
