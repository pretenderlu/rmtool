"""Font management page."""

import logging
import os
import posixpath
from typing import Dict, Optional

from PyQt5 import QtCore, QtGui, QtWidgets, sip

import _rmkit_cn
from _ssh import SSHClientWrapper, require_connection
from _ui_tasks import run_in_background
import _device_actions
# Dialog helpers are looked up on _toolbox_common at call time so a single
# patch point covers every toolbox section.
import _toolbox_common as _ui
import rmtool as _rmtool  # late-bound access to avoid circular import
from _toolbox_common import load_font_file, select_font_file
from _toolbox_reading import _set_epub_font_with_menu_support


class FontTab(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, config: Dict, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.config = config
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._font_progress: Optional[QtWidgets.QProgressDialog] = None
        self._fonts: tuple[_rmkit_cn.UserFont, ...] = ()
        self._font_verification: Optional[_rmkit_cn.FontMirrorVerification] = None
        self._font_recovery = _rmkit_cn.FontRecoveryStatus()
        self._epub_font_status: Optional[_rmkit_cn.EpubFontSlotStatus] = None
        self._legacy_font_migration: Optional[
            _rmkit_cn.LegacySystemFontMigration
        ] = None
        self._busy = False
        self._connected: Optional[bool] = None
        self._worker_generation = 0
        self._connection_generation = 0
        self._pending_refresh: Optional[tuple[str, str, str]] = None
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

        self.lock_screen_font_checkbox = QtWidgets.QCheckBox(
            "为锁屏密码界面保留字体（写入 /data）"
        )
        self.lock_screen_font_checkbox.setChecked(
            bool(config.get("font_lock_screen_support", True))
        )
        self.lock_screen_font_checkbox.setToolTip(
            "开启后，解锁前的锁屏密码界面也能使用当前系统字体，但会受 /data 空间限制。"
        )
        self.lock_screen_font_checkbox.toggled.connect(
            self._on_lock_screen_font_setting_changed
        )
        self.lock_screen_font_hint = QtWidgets.QLabel()
        self.lock_screen_font_hint.setObjectName("fontManagerStatus")
        self.lock_screen_font_hint.setWordWrap(True)

        self.refresh_button = QtWidgets.QPushButton("刷新")
        self.refresh_button.clicked.connect(self._refresh_fonts)
        self.set_active_button = QtWidgets.QPushButton("设为系统字体")
        self.set_active_button.clicked.connect(self._set_selected_active)
        self.epub_font_button = QtWidgets.QPushButton("添加到 EPUB 字体菜单")
        self.epub_font_button.clicked.connect(self._toggle_selected_epub_font)
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
        manager_actions.addWidget(self.epub_font_button)
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
        layout.addWidget(self.lock_screen_font_checkbox)
        layout.addWidget(self.lock_screen_font_hint)
        layout.addWidget(self.font_table)
        layout.addLayout(manager_actions)
        layout.addWidget(self.font_path_label)
        layout.addWidget(self.rename_checkbox)
        layout.addWidget(self.target_name_label)
        layout.addWidget(self.preview_panel)
        layout.addLayout(actions_layout)
        self.setLayout(layout)
        self._reset_font_preview()
        self._update_lock_screen_font_hint()
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
            _ui.show_warning(self, _rmtool.APP_NAME, "无法加载所选字体的本地预览。")
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
            _ui.show_info(self, _rmtool.APP_NAME, "请先选择需要上传的字体文件。")
            return
        file_path = self._selected_font_path
        new_name = self._target_font_name()
        upload_target = posixpath.join(
            posixpath.normpath(self._font_dir()), new_name
        )
        active_target = next(
            (
                font
                for font in self._fonts
                if font.remote_path == upload_target and font.active
            ),
            None,
        )
        if active_target is not None:
            _ui.show_warning(
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
                select_remote_path=font.remote_path,
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

    def _lock_screen_font_support(self) -> bool:
        return self.lock_screen_font_checkbox.isChecked()

    def _update_lock_screen_font_hint(self):
        if self._lock_screen_font_support():
            self.lock_screen_font_hint.setText(
                "开启：下次设置或重新应用系统字体时，会复制到 /data，支持解锁前的锁屏密码界面；"
                "设置前会检查空间。"
            )
        else:
            self.lock_screen_font_hint.setText(
                "关闭：下次设置或重新应用系统字体时，字体仅保存在 /home，不占用 /data；"
                "请确认设备不使用锁屏密码。"
            )

    def _on_lock_screen_font_setting_changed(self, enabled: bool):
        previous = self.config.get("font_lock_screen_support", True)
        self.config["font_lock_screen_support"] = bool(enabled)
        try:
            _rmtool.save_config(self.config)
        except Exception as exc:
            self.config["font_lock_screen_support"] = previous
            self.lock_screen_font_checkbox.blockSignals(True)
            self.lock_screen_font_checkbox.setChecked(bool(previous))
            self.lock_screen_font_checkbox.blockSignals(False)
            _ui.show_error(self, _rmtool.APP_NAME, f"保存字体选项失败：{exc}")
            return
        self._update_lock_screen_font_hint()

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
            self._epub_font_status = None
            self._legacy_font_migration = None
            self._font_recovery = _rmkit_cn.FontRecoveryStatus()
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
        epub_supported = (
            self._epub_font_status is not None
            and self._epub_font_status.supported
        )
        assigned_slots = (
            tuple(
                slot
                for slot in self._epub_font_status.slots
                if slot.target_path
            )
            if epub_supported
            else ()
        )
        selected_slot = next(
            (
                slot
                for slot in assigned_slots
                if selected and slot.target_path == selected.remote_path
            ),
            None,
        )
        self.set_active_button.setText(
            "修复字体设置" if self._font_recovery.state == "ready"
            else "重新应用系统字体" if selected and selected.active else "设为系统字体"
        )
        if selected_slot is not None:
            epub_action = "从 EPUB 字体菜单移除"
        elif len(assigned_slots) >= _rmkit_cn.EPUB_FONT_INDEX_MAX_ENTRIES:
            epub_action = "EPUB 字体菜单已达到安全上限"
        else:
            epub_action = f"添加为 EPUB 第 {len(assigned_slots) + 1} 项"
        self.epub_font_button.setText(epub_action)
        self.refresh_button.setEnabled(connected)
        self.select_button.setEnabled(not self._busy)
        self.upload_button.setEnabled(
            connected and bool(self._selected_font_path)
        )
        self.set_active_button.setEnabled(
            connected and selected is not None and self._font_recovery.state != "blocked"
        )
        self.epub_font_button.setEnabled(
            connected
            and selected is not None
            and epub_supported
            and (
                selected_slot is not None
                or len(assigned_slots) < _rmkit_cn.EPUB_FONT_INDEX_MAX_ENTRIES
            )
        )
        self.delete_button.setEnabled(
            connected
            and selected is not None
            and not selected.active
            and not selected.epub
        )
        self.migrate_font_button.setEnabled(
            connected
            and self._font_recovery.state == "none"
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
                    select_remote_path=pending_refresh[0],
                    select_filename=pending_refresh[1],
                    success=pending_refresh[2],
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
                    select_remote_path=pending_refresh[0],
                    select_filename=pending_refresh[1],
                    success=pending_refresh[2],
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
            _ui.show_error(self, _rmtool.APP_NAME, f"{error_prefix}：{exc}")
            if (
                pending_refresh is not None
                and not self._busy
                and self.ssh_client.is_connected()
            ):
                self._refresh_fonts(
                    select_remote_path=pending_refresh[0],
                    select_filename=pending_refresh[1],
                    success=pending_refresh[2],
                )

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _refresh_fonts(
        self,
        *,
        select_remote_path: str = "",
        select_filename: str = "",
        success: str = "",
    ):
        if self._busy:
            self._pending_refresh = (select_remote_path, select_filename, success)
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
                epub_status=inventory[3],
                recovery=inventory[4],
                select_remote_path=select_remote_path,
                select_filename=select_filename,
                success=success,
            ),
            error_prefix="字体列表刷新失败",
        )

    @staticmethod
    def _load_font_inventory(ssh_client, remote_dir: str):
        epub_status = _rmkit_cn.get_epub_font_slot_status(ssh_client)
        font_dirs = [remote_dir]
        migration_dir = remote_dir
        if posixpath.normpath(remote_dir) == posixpath.normpath(
            _rmtool.DEFAULT_FONT_DIR
        ):
            font_dirs.append(_rmtool.LEGACY_DEFAULT_FONT_DIR)
            migration_dir = _rmtool.LEGACY_DEFAULT_FONT_DIR
        fonts = {
            font.remote_path: font
            for font_dir in font_dirs
            for font in _rmkit_cn.list_user_fonts(
                ssh_client,
                font_dir,
                epub_slots=epub_status.slots,
            )
        }
        return (
            tuple(
                sorted(
                    fonts.values(),
                    key=lambda font: (
                        font.filename.casefold(),
                        font.filename,
                        font.remote_path,
                    ),
                )
            ),
            _rmkit_cn.get_font_mirror_verification(ssh_client),
            _rmkit_cn.get_legacy_system_font_migration(
                ssh_client, migration_dir
            ),
            epub_status,
            _rmkit_cn.get_font_recovery_status(ssh_client),
        )

    def _apply_font_inventory(
        self,
        fonts: tuple[_rmkit_cn.UserFont, ...],
        *,
        verification: Optional[_rmkit_cn.FontMirrorVerification] = None,
        migration: Optional[_rmkit_cn.LegacySystemFontMigration] = None,
        epub_status: Optional[_rmkit_cn.EpubFontSlotStatus] = None,
        recovery: Optional[_rmkit_cn.FontRecoveryStatus] = None,
        select_remote_path: str = "",
        select_filename: str = "",
        success: str = "",
    ):
        previous_path = select_remote_path
        previous_filename = select_filename
        if not previous_path and not previous_filename:
            selected = self._selected_device_font()
            if selected:
                previous_path = selected.remote_path
                previous_filename = selected.filename
        self._fonts = tuple(fonts)
        self._font_verification = verification
        self._legacy_font_migration = migration
        self._epub_font_status = epub_status
        self._font_recovery = recovery or _rmkit_cn.FontRecoveryStatus()
        self.font_table.setRowCount(len(self._fonts))
        selected_row = -1
        for row, font in enumerate(self._fonts):
            roles = []
            if font.active:
                roles.append("当前系统字体")
            if font.epub_slots:
                roles.append(
                    "EPUB 顺序 " + "、".join(str(number) for number in font.epub_slots)
                )
            values = (font.filename, font.family, " / ".join(roles) or "已上传")
            for column, value in enumerate(values):
                self.font_table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
            if (
                previous_path and font.remote_path == previous_path
            ) or (
                not previous_path
                and font.filename == previous_filename
                and selected_row < 0
            ):
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
        legacy_space_note = (
            "；老设备提示：系统空间较小，建议只保留少量小型自定义字体"
            if self._font_verification is not None
            and self._font_verification.platform in {"rm1", "rm2"}
            else ""
        )
        migration_note = (
            f"；{self._legacy_font_migration.detail}"
            if self._legacy_font_migration is not None
            and self._legacy_font_migration.state != "none"
            else ""
        )
        epub_note = (
            f"；EPUB 字体：{self._epub_font_status.detail}"
            if self._epub_font_status is not None
            else ""
        )
        self.manager_status_label.setText(
            f"已读取 {len(self._fonts)} 个用户字体；当前系统字体：{active}"
            f"{legacy_note}{verification_note}{legacy_space_note}"
            f"{migration_note}{epub_note}"
            + (f"；{self._font_recovery.detail}" if self._font_recovery.detail else "")
            + "。"
        )
        tooltip = "\n".join(
            detail
            for detail in (
                self._font_verification.detail if self._font_verification else "",
                self._legacy_font_migration.detail
                if self._legacy_font_migration
                and self._legacy_font_migration.state != "none"
                else "",
                self._epub_font_status.detail if self._epub_font_status else "",
                self._font_recovery.detail,
            )
            if detail
        )
        self.manager_status_label.setToolTip(tooltip)
        self._update_action_buttons()
        if success:
            _ui.show_info(self, _rmtool.APP_NAME, success)

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
            self._pending_refresh = ("", "", "")
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
                    select_remote_path=pending_refresh[0],
                    select_filename=pending_refresh[1],
                    success=pending_refresh[2],
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
                    inventory[0],
                    verification=inventory[1],
                    migration=inventory[2],
                    epub_status=inventory[3],
                    recovery=inventory[4],
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
        lock_screen_support = self._lock_screen_font_support()
        mode_notice = (
            "当前迁移会在 /data 保留锁屏密码界面的字体副本，并受 /data 可用空间限制。"
            if lock_screen_support
            else "当前迁移只保留 /home 中的系统字体，不占用 /data；请确认设备不使用锁屏密码。"
        )
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"将旧版系统字体设置迁移到当前格式。"
            f"用户字体文件 {migration.filename} 仍会保留。"
            f"{mode_notice}"
            "迁移完成后需手动重启设备才会完整生效，是否继续？",
            confirm_text="迁移旧版字体设置",
            cancel_text="取消",
        ):
            return
        self._start_font_worker(
            _rmkit_cn.migrate_legacy_system_font,
            self.ssh_client,
            (
                _rmtool.LEGACY_DEFAULT_FONT_DIR
                if posixpath.normpath(self._font_dir())
                == posixpath.normpath(_rmtool.DEFAULT_FONT_DIR)
                else self._font_dir()
            ),
            self._lock_screen_font_support(),
            pending="正在迁移并验证旧版字体设置…",
            on_success=lambda font: self._refresh_fonts(
                select_remote_path=font.remote_path,
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
        repairing = self._font_recovery.state == "ready"
        if self._font_recovery.state == "blocked":
            _ui.show_warning(self, _rmtool.APP_NAME, self._font_recovery.detail)
            return
        action = "修复字体设置" if repairing else "重新应用系统字体" if reapply else "设为系统字体"
        lock_screen_support = self._lock_screen_font_support()
        message = (
            f"使用 {selected.filename} 修复上次未完成的字体设置，旧恢复备份将保留归档。"
            if repairing
            else f"重新应用 {selected.filename} 作为系统界面字体，并修复 /data 中供锁屏使用的当前字体副本。"
            if reapply and lock_screen_support
            else f"重新应用 {selected.filename} 作为系统界面字体，仅保存在 /home。"
            if reapply
            else f"将 {selected.filename} 设为系统界面字体。"
        )
        legacy_warning = ""
        if (
            lock_screen_support
            and
            self._font_verification is not None
            and self._font_verification.platform in {"rm1", "rm2"}
        ):
            legacy_warning = (
                "\n\n当前为 RM1/RM2 老设备，系统空间较小，本次使用低空间兼容模式。"
                "建议不要继续上传过多或过大的自定义字体。"
            )
        mode_notice = (
            "当前会为锁屏密码界面保留 /data 字体副本，字体大小受 /data 可用空间限制。"
            if lock_screen_support
            else "当前不会写入 /data 字体副本；如果设备启用锁屏密码，解锁前界面可能无法使用该字体。"
        )
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"{message}{legacy_warning}"
            f"\n\n{mode_notice}"
            "操作完成后需手动重启设备才会完整生效，是否继续？",
            confirm_text=action,
            cancel_text="取消",
        ):
            return
        self._start_font_worker(
            _rmkit_cn.repair_user_font if repairing else _rmkit_cn.set_active_user_font,
            self.ssh_client,
            posixpath.dirname(selected.remote_path),
            selected.filename,
            lock_screen_support,
            *([self._font_recovery] if repairing else []),
            pending="正在设置并验证系统字体…",
            on_success=lambda font: self._refresh_fonts(
                select_remote_path=font.remote_path,
                select_filename=font.filename,
                success=(
                    "字体状态已修复，旧恢复备份已保留归档。请在准备好后点击“重启生效”。"
                    if repairing else "系统字体配置已更新。请在准备好后点击“重启生效”。"
                ),
            ),
            error_prefix="设置系统字体失败",
        )

    @require_connection
    def _toggle_selected_epub_font(self):
        selected = self._selected_device_font()
        status = self._epub_font_status
        if not selected or status is None or not status.supported:
            return
        selected_slot = next(
            (slot for slot in status.slots if slot.target_path == selected.remote_path),
            None,
        )
        removing = selected_slot is not None
        action = self.epub_font_button.text()
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"{action}：{selected.filename}。"
            + (
                "操作完成后需手动重启设备，EPUB 字体菜单才会更新；"
                "EPUB 字体保存在 /home，不占用系统字体的 /data 镜像空间。是否继续？"
                if removing
                else "若设备尚未具备 EPUB 字体菜单支持，rmtool 会先自动安装或更新所需组件；"
                "不会开启任何阅读功能。字体仅供 EPUB 阅读器使用，保存在 /home，"
                "不触发系统字体的 /data 空间限制。操作完成后需手动重启设备，是否继续？"
            ),
            confirm_text=action,
            cancel_text="取消",
        ):
            return
        fn = (
            _rmkit_cn.remove_epub_font_slot
            if removing
            else _set_epub_font_with_menu_support
        )
        args = (
            self.ssh_client,
            posixpath.dirname(selected.remote_path),
            selected.filename,
            *((str(_rmtool.app_state_dir()),) if not removing else ()),
        )
        self._start_font_worker(
            fn,
            *args,
            pending=(
                "正在移除并整理 EPUB 字体顺序…"
                if removing
                else "正在更新并验证 EPUB 字体菜单…"
            ),
            on_success=lambda _: self._refresh_fonts(
                select_remote_path=selected.remote_path,
                select_filename=selected.filename,
                success=(
                    "已从 EPUB 字体菜单移除，后续字体顺序已自动前移。"
                    "请在准备好后点击“重启生效”。"
                    if removing
                    else "EPUB 字体菜单已更新。请在准备好后点击“重启生效”。"
                ),
            ),
            error_prefix="EPUB 字体菜单更新失败",
        )

    @require_connection
    def _delete_selected_font(self):
        selected = self._selected_device_font()
        if not selected:
            return
        if selected.active:
            _ui.show_warning(self, _rmtool.APP_NAME, "当前系统字体不能删除，请先切换到其他字体。")
            return
        if selected.epub:
            _ui.show_warning(
                self,
                _rmtool.APP_NAME,
                "当前 EPUB 字体不能删除，请先从 EPUB 字体菜单移除。",
            )
            return
        if not _ui.ask_confirmation(
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
            posixpath.dirname(selected.remote_path),
            selected.filename,
            pending="正在删除字体并刷新缓存…",
            on_success=lambda _: self._refresh_fonts(success="所选字体已删除。"),
            error_prefix="删除字体失败",
        )

    @require_connection
    def _restart_device(self):
        if not _ui.ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "设备将立即重启，尚未保存的内容可能丢失。是否继续？",
            confirm_text="重启设备",
            cancel_text="取消",
        ):
            return
        run_in_background(
            self,
            _device_actions.reboot,
            self.ssh_client,
            on_success=lambda _: _ui.show_info(self, _rmtool.APP_NAME, "已发送重启命令。"),
            error_prefix="重启失败",
            log_message="Device reboot from font manager failed",
            disable=(self.restart_button,),
        )


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
