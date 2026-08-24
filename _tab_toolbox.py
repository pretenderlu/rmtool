"""FontTab, TimeTab, ControlTab, ToolboxTab, and FontPage extracted from rmtool.py."""

import logging
import os
import posixpath
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from PyQt5 import QtCore, QtGui, QtWidgets, sip

from _dialogs import ask_confirmation, show_error, show_info, show_warning
import _diagnostics
import _package_download
import _rmkit_cn
import _legacy_vellum
import _native_chinese
import _pinyin_input
import _reading_enhancements
import _residue_migration
import _tap_page_turn
from _ssh import SSHClientWrapper, remount_rw, require_connection
import rmtool as _rmtool  # late-bound access to avoid circular import


def _show_package_download_error(
    parent, exc: "_package_download.PackageDownloadError", retry=None
) -> None:
    """Both mirrors failed: show the exact URLs and offer a manual load."""
    dialog = QtWidgets.QMessageBox(parent)
    dialog.setIcon(QtWidgets.QMessageBox.Warning)
    dialog.setWindowTitle(_rmtool.APP_NAME)
    dialog.setText("资源包自动下载失败")
    dialog.setInformativeText(
        f"{exc}\n\n"
        "可点击“复制下载地址”后在浏览器手动下载，"
        "再选择“手动加载资源包”完成校验与安装；"
        "也可直接选择“手动加载资源包”选用已下载到本机的文件。"
    )
    copy_button = dialog.addButton("复制下载地址", QtWidgets.QMessageBox.ActionRole)
    manual_button = dialog.addButton("手动加载资源包…", QtWidgets.QMessageBox.AcceptRole)
    dialog.addButton("关闭", QtWidgets.QMessageBox.RejectRole)
    dialog.exec_()
    clicked = dialog.clickedButton()
    if clicked is copy_button:
        QtWidgets.QApplication.clipboard().setText("\n".join(exc.urls))
        show_info(parent, _rmtool.APP_NAME, "全部下载地址已复制到剪贴板。")
        return
    if clicked is not manual_button:
        return
    source_path, _ = QtWidgets.QFileDialog.getOpenFileName(
        parent,
        f"选择{exc.feature_label}资源包",
        "",
        "资源包 (*.tar.gz);;所有文件 (*)",
    )
    if not source_path:
        return
    try:
        if exc.store is None:
            raise RuntimeError("该资源包暂不支持手动加载。")
        stored = exc.store(source_path)
    except Exception as store_exc:
        logging.error("Manual package load failed: %s", store_exc)
        show_error(parent, _rmtool.APP_NAME, f"手动加载失败：{store_exc}")
        return
    show_info(
        parent,
        _rmtool.APP_NAME,
        "本地资源包已通过大小与 SHA-256 校验，并写入缓存：\n"
        f"{stored}\n\n将自动重试安装，安装时会优先使用这份缓存。",
    )
    if retry is not None:
        retry()


def _collect_diagnostics(ssh_client, log_path):
    return _diagnostics.collect(ssh_client, log_path)


def _tap_page_turn_status(ssh_client, state_dir: str):
    catalog = _tap_page_turn.load_catalog(state_dir, refresh=True)
    return _tap_page_turn.get_status(ssh_client, catalog)


def _reading_enhancements_status(ssh_client, state_dir: str):
    catalog = _reading_enhancements.load_catalog(state_dir, refresh=True)
    return _reading_enhancements.get_status(ssh_client, catalog)


def _install_reading_enhancements(
    ssh_client,
    package,
    state_dir: str,
    migrate: bool,
):
    archive = _reading_enhancements.download_package(package, state_dir)
    operation = (
        _reading_enhancements.migrate
        if migrate
        else _reading_enhancements.install
    )
    return operation(ssh_client, package, archive)


def _cleanup_reading_enhancements(ssh_client, state_dir: str):
    catalog = _reading_enhancements.load_catalog(state_dir, refresh=True)
    return _reading_enhancements.cleanup_legacy(ssh_client, catalog)


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
            "重新应用系统字体" if selected and selected.active else "设为系统字体"
        )
        if selected_slot is not None and selected is not None:
            expected_label = posixpath.splitext(selected.filename)[0]
            epub_action = (
                "从 EPUB 字体菜单移除"
                if selected_slot.label == expected_label
                else "更新 EPUB 字体名称"
            )
        elif len(assigned_slots) >= len(_rmkit_cn.EPUB_FONT_SLOT_NUMBERS):
            epub_action = "EPUB 字体已满（3/3）"
        else:
            epub_action = f"添加为 EPUB 第 {len(assigned_slots) + 1} 项"
        self.epub_font_button.setText(epub_action)
        self.refresh_button.setEnabled(connected)
        self.select_button.setEnabled(not self._busy)
        self.upload_button.setEnabled(
            connected and bool(self._selected_font_path)
        )
        self.set_active_button.setEnabled(
            connected and selected is not None
        )
        self.epub_font_button.setEnabled(
            connected
            and selected is not None
            and epub_supported
            and (
                selected_slot is not None
                or len(assigned_slots) < len(_rmkit_cn.EPUB_FONT_SLOT_NUMBERS)
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
            show_error(self, _rmtool.APP_NAME, f"{error_prefix}：{exc}")
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
        )

    def _apply_font_inventory(
        self,
        fonts: tuple[_rmkit_cn.UserFont, ...],
        *,
        verification: Optional[_rmkit_cn.FontMirrorVerification] = None,
        migration: Optional[_rmkit_cn.LegacySystemFontMigration] = None,
        epub_status: Optional[_rmkit_cn.EpubFontSlotStatus] = None,
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
            f"{legacy_note}{verification_note}{migration_note}{epub_note}。"
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
            f"将旧版系统字体设置迁移到当前格式。"
            f"用户字体文件 {migration.filename} 仍会保留。"
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
            posixpath.dirname(selected.remote_path),
            selected.filename,
            pending="正在设置并验证系统字体…",
            on_success=lambda font: self._refresh_fonts(
                select_remote_path=font.remote_path,
                select_filename=font.filename,
                success="系统字体配置已更新。请在准备好后点击“重启生效”。",
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
        expected_label = posixpath.splitext(selected.filename)[0]
        removing = (
            selected_slot is not None and selected_slot.label == expected_label
        )
        action = self.epub_font_button.text()
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"{action}：{selected.filename}。操作完成后需手动重启设备，"
            "EPUB 字体菜单才会更新。是否继续？",
            confirm_text=action,
            cancel_text="取消",
        ):
            return
        fn = (
            _rmkit_cn.remove_epub_font_slot
            if removing
            else _rmkit_cn.set_epub_font_slot
        )
        args = (
            self.ssh_client,
            posixpath.dirname(selected.remote_path),
            selected.filename,
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
            show_warning(self, _rmtool.APP_NAME, "当前系统字体不能删除，请先切换到其他字体。")
            return
        if selected.epub:
            show_warning(
                self,
                _rmtool.APP_NAME,
                "当前 EPUB 字体不能删除，请先从 EPUB 字体菜单移除。",
            )
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
            posixpath.dirname(selected.remote_path),
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


class ToolboxStatusLabel(QtWidgets.QLabel):
    text_changed = QtCore.pyqtSignal(str)

    def setText(self, text: str):
        changed = text != self.text()
        super().setText(text)
        if changed:
            self.text_changed.emit(text)


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

        self.status_label = ToolboxStatusLabel("设备已连接，尚未检测")
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
        self.status_label = ToolboxStatusLabel("设备已连接，尚未检测")
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
                _native_chinese.NativeChineseState.OUTDATED,
            )
        )
        self.enable_button.setText(
            "修复并更新"
            if state is _native_chinese.NativeChineseState.OUTDATED
            else "启用原生中文"
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
            _native_chinese.NativeChineseState.OUTDATED: "已安装的原生中文包需要修复更新",
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
        on_done=None,
        show_errors: bool = True,
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
            if on_done is not None:
                on_done()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                if close_connection:
                    self.ssh_client.close()
                logging.error("Native Chinese operation failed after tab close: %s", exc)
                return
            if isinstance(exc, _package_download.PackageDownloadError):
                # Download failed before any device change; keep the session.
                self._set_busy(False, "资源包下载失败，可手动加载后重试")
                logging.error("Native Chinese package download failed: %s", exc)
                if show_errors:
                    _show_package_download_error(self, exc, retry=self._enable)
                if on_done is not None:
                    on_done()
                return
            if close_connection:
                self.ssh_client.close()
            self._set_busy(False)
            self._status = None
            self._update_buttons()
            self.status_label.setText("操作失败，未自动重启设备；请重新连接并检测状态")
            logging.error("Native Chinese operation failed: %s", exc)
            if show_errors:
                show_error(
                    self,
                    _rmtool.APP_NAME,
                    f"操作失败：{exc}\n设备不会被自动重启。",
                )
            if on_done is not None:
                on_done()

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _detect_status(self):
        self._start_status_detection()

    def _start_status_detection(self, *, on_done=None, show_errors: bool = True):
        self._start_worker(
            _native_chinese.get_cloud_status,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在核对设备身份、精确包与共享 Xovi 状态…",
            on_done=on_done,
            show_errors=show_errors,
        )

    @require_connection
    def _enable(self):
        if not self._status or not self._status.package:
            return
        package = self._status.package
        updating = self._status.state is _native_chinese.NativeChineseState.OUTDATED
        verification_notice = (
            "该精确包已完成对应真机验证。"
            if package.device_verified
            else "该精确包已完成官方固件离线验证，尚待对应真机验证。"
        )
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"{verification_notice}"
            + (
                "将把键盘中文名称补丁迁移到原生中文功能，并保留其他插件。"
                if updating
                else ""
            )
            +
            "它会增加独立的“简体中文”选项，不替换法语。"
            "如果旧版法语槽位汉化仍在启用，操作会被拒绝；请先还原并手动重启，"
            "再重新连接设备执行本步骤。"
            "本次只部署文件，不重启 xochitl 或设备；完成后请手动重启。"
            "是否继续？",
            confirm_text="修复并更新" if updating else "部署并启用",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _native_chinese.enable_cloud,
            self.ssh_client,
            self._status.package,
            str(_rmtool.app_state_dir()),
            pending=(
                "正在验证并更新原生中文资源…"
                if updating
                else "正在下载、验证并部署原生中文资源…"
            ),
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


class PinyinInputSection(QtWidgets.QWidget):
    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status: Optional[_pinyin_input.PinyinInputStatus] = None
        self._busy = False

        title = QtWidgets.QLabel("拼音输入法")
        title.setObjectName("toolboxFeatureTitle")
        detail = QtWidgets.QLabel(
            "为系统软键盘和实体键盘增加离线拼音候选栏。词库完全保存在设备上，"
            "不使用云端预测；功能接入 rmtool 共享 Xovi，启停不会影响其他插件。"
        )
        detail.setWordWrap(True)
        self.catalog_label = QtWidgets.QLabel("精确包：检测后显示")
        self.catalog_label.setWordWrap(True)
        self.status_label = ToolboxStatusLabel("设备已连接，尚未检测")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.enable_button = QtWidgets.QPushButton("安装并启用")
        self.disable_button = QtWidgets.QPushButton("停用")
        self.project_button = QtWidgets.QPushButton("查看来源")
        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        for button in (
            self.detect_button,
            self.enable_button,
            self.disable_button,
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
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.detect_button.clicked.connect(self._detect_status)
        self.enable_button.clicked.connect(self._enable)
        self.disable_button.clicked.connect(self._disable)
        self.project_button.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl("https://github.com/boangs/rmkit")
            )
        )
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
        self.detect_button.setEnabled(connected)
        self.enable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and state in (
                _pinyin_input.PinyinInputState.NOT_INSTALLED,
                _pinyin_input.PinyinInputState.INSTALLED_DISABLED,
                _pinyin_input.PinyinInputState.OUTDATED,
            )
        )
        self.enable_button.setText(
            "修复并更新"
            if state == _pinyin_input.PinyinInputState.OUTDATED
            else "安装并启用"
        )
        self.disable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.installed
            and state
            not in (
                _pinyin_input.PinyinInputState.INSTALLED_DISABLED,
                _pinyin_input.PinyinInputState.DISABLE_PENDING_REBOOT,
                _pinyin_input.PinyinInputState.BROKEN,
            )
        )

    def _apply_status(self, status: _pinyin_input.PinyinInputStatus):
        self._status = status
        package = status.package
        if package is None:
            self.catalog_label.setText("精确包：当前设备不匹配")
        else:
            channel_names = {"stable": "正式版", "beta": "测试版"}
            verification = "已实机验证" if package.device_verified else "离线验证，尚待实机"
            self.catalog_label.setText(
                f"精确包：{package.release_version} | {channel_names[package.channel]} | "
                f"硬件 {package.platform.title()} | "
                f"内部版本 {package.firmware} | {verification}"
            )
        messages = {
            _pinyin_input.PinyinInputState.INCOMPATIBLE: "当前固件没有精确匹配的拼音输入法包",
            _pinyin_input.PinyinInputState.NOT_INSTALLED: "尚未安装拼音输入法",
            _pinyin_input.PinyinInputState.INSTALLED_DISABLED: "拼音输入法当前未启用",
            _pinyin_input.PinyinInputState.ENABLE_PENDING_REBOOT: "已部署，等待手动重启后生效",
            _pinyin_input.PinyinInputState.ENABLED: "拼音输入法已加载",
            _pinyin_input.PinyinInputState.DISABLE_PENDING_REBOOT: "已停用，等待手动重启后完全移除",
            _pinyin_input.PinyinInputState.EMERGENCY_DISABLED: "共享 Xovi 已被紧急标记停用",
            _pinyin_input.PinyinInputState.OUTDATED: "已安装的拼音包需要修复更新",
            _pinyin_input.PinyinInputState.BROKEN: "检测到不完整或不可验证的拼音输入法状态",
        }
        message = messages[status.state]
        if status.detail:
            message += f"：{status.detail}"
        self.status_label.setText(message)
        self._update_buttons()

    def _start_worker(
        self,
        fn,
        *args,
        pending: str,
        success: str = "",
        close_connection: bool = False,
        on_done=None,
        show_errors: bool = True,
    ):
        self._busy = True
        self.status_label.setText(pending)
        self._update_buttons()
        worker = _rmtool.Worker(fn, *args)

        def on_finished(status: _pinyin_input.PinyinInputStatus):
            if sip.isdeleted(self):
                if close_connection:
                    self.ssh_client.close()
                return
            self._busy = False
            self._apply_status(status)
            if close_connection:
                self.ssh_client.close()
            if success:
                show_info(self, _rmtool.APP_NAME, success)
            if on_done is not None:
                on_done()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                if close_connection:
                    self.ssh_client.close()
                logging.error("Pinyin input operation failed after tab close: %s", exc)
                return
            if isinstance(exc, _package_download.PackageDownloadError):
                # Download failed before any device change; keep the session.
                self._busy = False
                self.status_label.setText("资源包下载失败，可手动加载后重试")
                self._update_buttons()
                logging.error("Pinyin input package download failed: %s", exc)
                if show_errors:
                    _show_package_download_error(self, exc, retry=self._enable)
                if on_done is not None:
                    on_done()
                return
            if close_connection:
                self.ssh_client.close()
            self._busy = False
            self._status = None
            self.status_label.setText("操作失败，设备不会被自动重启；请检查日志后重试")
            self._update_buttons()
            logging.error("Pinyin input operation failed: %s", exc)
            if show_errors:
                show_error(
                    self,
                    _rmtool.APP_NAME,
                    f"操作失败：{exc}\n设备不会被自动重启。",
                )
            if on_done is not None:
                on_done()

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _detect_status(self):
        self._start_status_detection()

    def _start_status_detection(self, *, on_done=None, show_errors: bool = True):
        self._start_worker(
            _pinyin_input.get_status,
            self.ssh_client,
            _pinyin_input._trusted_catalog(),
            pending="正在核对设备身份、服务载荷与共享 Xovi 状态…",
            on_done=on_done,
            show_errors=show_errors,
        )

    @require_connection
    def _enable(self):
        if not self._status or not self._status.package:
            return
        repairing = self._status.state == _pinyin_input.PinyinInputState.OUTDATED
        action = (
            "将严格验证并修复已安装的旧版拼音包，把中文键盘布局资源迁移到 QRR 可读取的位置。"
            if repairing
            else "将下载并验证离线拼音输入法包，把候选栏和输入拦截器接入 rmtool 共享 Xovi。"
        )
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"{action}"
            "若设备已有 rmkit 输入法，操作会在写入前停止。部署过程不会重启 xochitl 或设备；"
            "完成后请手动重启。是否继续？",
            confirm_text="修复并更新" if repairing else "安装并启用",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _pinyin_input.enable_cloud,
            self.ssh_client,
            self._status.package,
            str(_rmtool.app_state_dir()),
            pending=("正在验证并修复旧版拼音输入法…" if repairing else
                     "正在下载、验证并部署拼音输入法…"),
            success=(
                "拼音输入法已补齐中文键盘布局并通过校验，SSH 会话已关闭。\n"
                if repairing
                else "拼音输入法已部署并通过校验，SSH 会话已关闭。\n"
            ) + "请手动重启设备后，在键盘语言中选择中文。",
            close_connection=True,
        )

    @require_connection
    def _disable(self):
        if not self._status or not self._status.installed:
            return
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将从共享 Xovi 中停用拼音候选栏和输入拦截器，并移除 rmtool 管理的离线词库服务。"
            "其他 rmtool 插件会完整保留；本次不会重启设备。是否继续？",
            confirm_text="停用拼音输入法",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _pinyin_input.disable,
            self.ssh_client,
            _pinyin_input._trusted_catalog(),
            pending="正在安全停用拼音输入法并保留其他共享插件…",
            success="拼音输入法已停用，SSH 会话已关闭。\n请手动重启设备使修改生效。",
            close_connection=True,
        )


class ReadingEnhancementsSection(QtWidgets.QWidget):
    """Combined browser entry for the native reading-enhancement package."""

    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status: Optional[_reading_enhancements.ReadingEnhancementsStatus] = None
        self._busy = False
        self._other_packages_count = 0

        title = QtWidgets.QLabel("阅读增强")
        title.setObjectName("toolboxFeatureTitle")
        detail = QtWidgets.QLabel(
            "为 PDF 和 EPUB 阅读提供点击翻页、快速黑白阅读和翻页清残影。"
            "日常开关由设备的“设置 > 阅读增强”页面控制。"
        )
        detail.setWordWrap(True)

        self.catalog_label = QtWidgets.QLabel("当前固件阅读增强包：检测后显示")
        self.catalog_label.setObjectName("readingEnhancementsCatalog")
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
        self.other_packages_label.setObjectName("readingEnhancementsOtherCatalog")
        self.other_packages_label.setWordWrap(True)
        self.other_packages_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse
        )
        self.other_packages_label.hide()

        self.status_label = ToolboxStatusLabel("设备已连接，尚未检测")
        self.status_label.setObjectName("readingEnhancementsDeviceStatus")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.install_button = QtWidgets.QPushButton("安装阅读增强")
        self.install_button.setProperty("btnRole", "primary")
        self.disable_button = QtWidgets.QPushButton("停用")
        self.cleanup_legacy_button = QtWidgets.QPushButton("清理旧版")
        self.cleanup_legacy_button.setProperty("btnRole", "danger")
        self.explain_button = QtWidgets.QPushButton("查看说明")

        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        for button in (
            self.detect_button,
            self.install_button,
            self.disable_button,
            self.cleanup_legacy_button,
            self.explain_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(self.catalog_label)
        layout.addWidget(self.other_packages_button, alignment=QtCore.Qt.AlignLeft)
        layout.addWidget(self.other_packages_label)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.other_packages_button.toggled.connect(self._toggle_other_packages)
        self.detect_button.clicked.connect(self._detect_status)
        self.install_button.clicked.connect(self._install)
        self.disable_button.clicked.connect(self._disable)
        self.cleanup_legacy_button.clicked.connect(self._cleanup_legacy)
        self.explain_button.clicked.connect(self._show_explanation)
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    @staticmethod
    def _package_display_text(package):
        channel_names = {"stable": "正式版", "beta": "测试版"}
        verification = "已实机验证" if package.device_verified else "离线验证，尚待实机"
        return (
            f"{package.release_version} | {channel_names[package.channel]} | "
            f"硬件 {package.platform.title()} | 内部版本 {package.firmware} | "
            f"{verification}"
        )

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._status = None
            self.catalog_label.setText("当前固件阅读增强包：检测后显示")
            self.other_packages_button.setChecked(False)
            self._other_packages_count = 0
            self.other_packages_button.setText("其他固件版本")
            self.other_packages_label.clear()
            self.other_packages_button.hide()
            self.other_packages_label.hide()
            self.status_label.setText("设备未连接")
        elif self._status is None:
            self.status_label.setText("设备已连接，尚未检测")
        self._update_buttons()

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
        install_states = (
            _reading_enhancements.ReadingEnhancementsState.NOT_INSTALLED,
            _reading_enhancements.ReadingEnhancementsState.INSTALLED_DISABLED,
            _reading_enhancements.ReadingEnhancementsState.MIGRATION_AVAILABLE,
            _reading_enhancements.ReadingEnhancementsState.REPAIR_AVAILABLE,
        )
        if state is _reading_enhancements.ReadingEnhancementsState.MIGRATION_AVAILABLE:
            self.install_button.setText("迁移到阅读增强")
        elif state is _reading_enhancements.ReadingEnhancementsState.REPAIR_AVAILABLE:
            self.install_button.setText("修复并更新")
        elif state is _reading_enhancements.ReadingEnhancementsState.INSTALLED_DISABLED:
            self.install_button.setText("重新启用")
        else:
            self.install_button.setText("安装阅读增强")
        self.detect_button.setEnabled(connected)
        self.install_button.setEnabled(
            connected
            and self._status is not None
            and self._status.package is not None
            and state in install_states
        )
        self.disable_button.setEnabled(
            connected
            and self._status is not None
            and self._status.recovery_available
            and state
            not in (
                _reading_enhancements.ReadingEnhancementsState.NOT_INSTALLED,
                _reading_enhancements.ReadingEnhancementsState.INCOMPATIBLE,
                _reading_enhancements.ReadingEnhancementsState.MIGRATION_AVAILABLE,
                _reading_enhancements.ReadingEnhancementsState.INSTALLED_DISABLED,
                _reading_enhancements.ReadingEnhancementsState.BROKEN,
            )
        )
        self.cleanup_legacy_button.setEnabled(
            connected
            and self._status is not None
            and self._status.cleanup_available
            and state
            in (
                _reading_enhancements.ReadingEnhancementsState.MIGRATION_AVAILABLE,
                _reading_enhancements.ReadingEnhancementsState.REPAIR_AVAILABLE,
            )
        )
        self.explain_button.setEnabled(True)

    def _apply_status(self, status):
        self._status = status
        if status.package is None:
            self.catalog_label.setText("当前固件阅读增强包：没有精确匹配版本")
        else:
            self.catalog_label.setText(
                "当前固件阅读增强包：\n" + self._package_display_text(status.package)
            )

        other_packages = tuple(
            package
            for package in status.available_packages
            if package != status.package and package.platform == status.identity.platform
        )
        self.other_packages_button.setChecked(False)
        if other_packages:
            self._other_packages_count = len(other_packages)
            self.other_packages_button.setText(
                f"其他固件版本（{self._other_packages_count}） ›"
            )
            self.other_packages_label.setText(
                "\n".join(self._package_display_text(package) for package in other_packages)
            )
            self.other_packages_button.show()
        else:
            self._other_packages_count = 0
            self.other_packages_button.setText("其他固件版本")
            self.other_packages_label.clear()
            self.other_packages_button.hide()
        self.other_packages_label.hide()

        messages = {
            _reading_enhancements.ReadingEnhancementsState.INCOMPATIBLE:
                "当前设备没有精确匹配的阅读增强包",
            _reading_enhancements.ReadingEnhancementsState.NOT_INSTALLED:
                "尚未安装阅读增强",
            _reading_enhancements.ReadingEnhancementsState.MIGRATION_AVAILABLE:
                "检测到旧版阅读功能，可迁移",
            _reading_enhancements.ReadingEnhancementsState.REPAIR_AVAILABLE:
                "检测到已知缺陷版本，可安全修复",
            _reading_enhancements.ReadingEnhancementsState.INSTALLED_DISABLED:
                "阅读增强已安装，当前未启用",
            _reading_enhancements.ReadingEnhancementsState.ENABLE_PENDING_REBOOT:
                "阅读增强已部署，等待手动重启后生效",
            _reading_enhancements.ReadingEnhancementsState.ENABLED:
                "阅读增强已启用",
            _reading_enhancements.ReadingEnhancementsState.DISABLE_PENDING_REBOOT:
                "阅读增强已停用，等待手动重启后完成",
            _reading_enhancements.ReadingEnhancementsState.BROKEN:
                "检测到不完整或不可验证的阅读增强状态",
        }
        message = messages[status.state]
        if status.detail:
            message += f"：{status.detail}"
        self.status_label.setText(message)
        self._update_buttons()

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        if message:
            self.status_label.setText(message)
        self._update_buttons()

    def _start_worker(
        self,
        fn,
        *args,
        pending: str,
        success: str = "",
        close_connection: bool = False,
        on_done=None,
        show_errors: bool = True,
    ):
        self._set_busy(True, pending)
        worker = _rmtool.Worker(fn, *args)

        def on_finished(status):
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
            if on_done is not None:
                on_done()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                if close_connection:
                    self.ssh_client.close()
                logging.error("Reading-enhancements operation failed after tab close: %s", exc)
                return
            if isinstance(exc, _package_download.PackageDownloadError):
                # Download failed before any device change; keep the session.
                self._set_busy(False, "资源包下载失败，可手动加载后重试")
                logging.error("Reading-enhancements package download failed: %s", exc)
                if show_errors:
                    _show_package_download_error(self, exc, retry=self._install)
                if on_done is not None:
                    on_done()
                return
            if close_connection:
                self.ssh_client.close()
            self._set_busy(False, "操作失败，设备不会被自动重启；请检查日志后重试")
            logging.error("Reading-enhancements operation failed: %s", exc)
            if show_errors:
                show_error(
                    self,
                    _rmtool.APP_NAME,
                    f"操作失败：{exc}\n设备不会被自动重启。",
                )
            if on_done is not None:
                on_done()

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _detect_status(self):
        self._start_status_detection()

    def _start_status_detection(self, *, on_done=None, show_errors: bool = True):
        self._start_worker(
            _reading_enhancements_status,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在获取清单并核对阅读增强包与设备状态…",
            on_done=on_done,
            show_errors=show_errors,
        )

    @require_connection
    def _install(self):
        if not self._status or not self._status.package:
            return
        migration = self._status.state is _reading_enhancements.ReadingEnhancementsState.MIGRATION_AVAILABLE
        repair = self._status.state is _reading_enhancements.ReadingEnhancementsState.REPAIR_AVAILABLE
        action = (
            "迁移已验证的旧版点击翻页/快速黑白功能"
            if migration
            else "修复并更新已验证的阅读增强"
            if repair
            else "安装当前固件对应的阅读增强"
        )
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"将{action}，完成后 SSH 会话会关闭。"
            "本次不会自动重启设备；请手动重启后，在“设置 > 阅读增强”中重新开启需要的开关。"
            "迁移不会沿用旧版开关状态。是否继续？",
            confirm_text=(
                "迁移到阅读增强"
                if migration
                else "修复并更新"
                if repair
                else "安装阅读增强"
            ),
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _install_reading_enhancements,
            self.ssh_client,
            self._status.package,
            str(_rmtool.app_state_dir()),
            migration,
            pending=(
                "正在验证旧版并迁移到阅读增强…"
                if migration
                else "正在校验并原子替换缺陷包…"
                if repair
                else "正在下载、校验并部署阅读增强…"
            ),
            success=(
                "阅读增强已迁移并通过校验，SSH 会话已关闭。\n"
                if migration
                else "阅读增强已修复并通过校验，SSH 会话已关闭。\n"
                if repair
                else "阅读增强已部署并通过校验，SSH 会话已关闭。\n"
            )
            + "请手动重启设备，然后在“设置 > 阅读增强”中开启需要的开关。",
            close_connection=True,
        )

    @require_connection
    def _disable(self):
        if not self._status or not self._status.available_packages:
            return
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将停用阅读增强并保留其他已验证的 rmtool 功能。"
            "本次不会自动重启设备，完成后请手动重启使修改生效。是否继续？",
            confirm_text="停用阅读增强",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _reading_enhancements.disable,
            self.ssh_client,
            self._status.available_packages,
            pending="正在安全停用阅读增强并保留其他功能…",
            success="阅读增强已停用，SSH 会话已关闭。\n请手动重启设备使修改生效。",
            close_connection=True,
        )

    @require_connection
    def _cleanup_legacy(self):
        if not self._status or not self._status.cleanup_available:
            return
        state = self._status.state
        if state not in (
            _reading_enhancements.ReadingEnhancementsState.MIGRATION_AVAILABLE,
            _reading_enhancements.ReadingEnhancementsState.REPAIR_AVAILABLE,
        ):
            return
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将清理已逐文件验证的旧版阅读插件，并保留其他已验证的 rmtool 功能。"
            "清理完成后 SSH 会话会关闭；请重新连接设备，再进行全新安装。"
            "本次不会自动重启设备。是否继续？",
            confirm_text="清理旧版",
            cancel_text="取消",
        ):
            return
        self._start_worker(
            _cleanup_reading_enhancements,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在再次验证并原子清理旧版阅读插件…",
            success=(
                "已清理旧版阅读插件，SSH 会话已关闭。\n"
                "请重新连接设备后，再进行全新安装当前固件对应的阅读增强。"
            ),
            close_connection=True,
        )

    def _show_explanation(self):
        show_info(
            self,
            "阅读增强",
            "阅读增强只作用于 PDF 和 EPUB 阅读页，包含点击翻页、快速黑白阅读和翻页清残影。"
            "安装或迁移完成后请手动重启设备，再到“设置 > 阅读增强”开启需要的开关。"
            "快速黑白阅读每次重启后默认关闭。",
        )


_LEGACY_PLATFORM_LABELS = {
    "ferrari": "Paper Pro",
    "chiappa": "Paper Pro Move",
    "tatsu": "Paper Pure",
    "rm1": "reMarkable 1",
    "rm2": "reMarkable 2",
}


class TapPageTurnSection(QtWidgets.QWidget):
    """Tap-to-turn management for RM1/RM2/Paper Pure (reading-enhancements
    has no packages for these devices). All these targets are offline
    verified only; the UI must keep saying so."""

    OFFLINE_NOTICE = (
        "注意：该设备系列的点击翻页包仅通过官方固件离线验证，"
        "尚未进行实机验证。"
    )

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
            + self.OFFLINE_NOTICE
        )
        detail.setWordWrap(True)

        self.catalog_label = QtWidgets.QLabel("当前固件点击翻页包：检测后显示")
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

        self.status_label = ToolboxStatusLabel("设备已连接，尚未检测")
        self.status_label.setObjectName("tapPageTurnDeviceStatus")
        self.status_label.setWordWrap(True)

        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.enable_button = QtWidgets.QPushButton("启用点击翻页")
        self.enable_button.setProperty("btnRole", "primary")
        self.disable_button = QtWidgets.QPushButton("停用")
        self.load_package_button = QtWidgets.QPushButton("加载本地资源包…")
        self.vellum_help_button = QtWidgets.QPushButton("Vellum 官方卸载说明")
        self.vellum_help_button.hide()
        self.project_button = QtWidgets.QPushButton("查看说明")

        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        for button in (
            self.detect_button,
            self.enable_button,
            self.disable_button,
            self.load_package_button,
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
        layout.addWidget(self.other_packages_button, alignment=QtCore.Qt.AlignLeft)
        layout.addWidget(self.other_packages_label)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.other_packages_button.toggled.connect(self._toggle_other_packages)
        self.detect_button.clicked.connect(self._detect_status)
        self.enable_button.clicked.connect(self._enable)
        self.disable_button.clicked.connect(self._disable)
        self.load_package_button.clicked.connect(self._load_local_package)
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

    @staticmethod
    def _package_display_text(package: _tap_page_turn.TapPageTurnPackage) -> str:
        # The tap manifest predates verification flags, but this section only
        # appears for RM1/RM2/Paper Pure, which are offline verified only.
        channel_names = {"stable": "正式版", "beta": "测试版"}
        return (
            f"{package.release_version} | {channel_names[package.channel]} | "
            f"硬件 {package.platform.title()} | 内部版本 {package.firmware} | "
            f"离线验证（未实机）"
        )

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._status = None
            self.catalog_label.setText("当前固件点击翻页包：检测后显示")
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
            in (
                _tap_page_turn.TapPageTurnState.ENABLE_PENDING_REBOOT,
                _tap_page_turn.TapPageTurnState.ENABLED,
                _tap_page_turn.TapPageTurnState.DISABLE_PENDING_REBOOT,
                _tap_page_turn.TapPageTurnState.OUTDATED,
                _tap_page_turn.TapPageTurnState.LEGACY_VELLUM,
                _tap_page_turn.TapPageTurnState.FIRMWARE_RESIDUE,
                _tap_page_turn.TapPageTurnState.BROKEN,
            )
        )
        self.load_package_button.setEnabled(
            connected and self._status is not None and self._status.package is not None
        )
        self.vellum_help_button.setVisible(
            state in (
                _tap_page_turn.TapPageTurnState.LEGACY_VELLUM,
                _tap_page_turn.TapPageTurnState.VELLUM_RUNTIME,
            )
        )
        self.project_button.setEnabled(True)

    def _apply_status(self, status: _tap_page_turn.TapPageTurnStatus):
        self._status = status
        if status.package is not None:
            self.catalog_label.setText(
                "当前固件点击翻页包：\n" + self._package_display_text(status.package)
            )
        else:
            self.catalog_label.setText("当前固件点击翻页包：没有精确匹配版本")

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
                    self._package_display_text(package) for package in other_packages
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
        message += "\n" + self.OFFLINE_NOTICE
        self.status_label.setText(message)
        self._update_buttons()

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        if message:
            self.status_label.setText(message)
        self._update_buttons()

    def _start_worker(
        self,
        fn,
        *args,
        pending: str,
        success: str = "",
        close_connection: bool = False,
        on_done=None,
        show_errors: bool = True,
    ):
        self._set_busy(True, pending)
        worker = _rmtool.Worker(fn, *args)

        def on_finished(status: _tap_page_turn.TapPageTurnStatus):
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
            if on_done is not None:
                on_done()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                if close_connection:
                    self.ssh_client.close()
                logging.error("Tap-to-turn operation failed after tab close: %s", exc)
                return
            if isinstance(exc, _package_download.PackageDownloadError):
                # Download failed before any device change; keep the session.
                self._set_busy(False, "资源包下载失败，可手动加载后重试")
                logging.error("Tap-to-turn package download failed: %s", exc)
                if show_errors:
                    _show_package_download_error(self, exc, retry=self._enable)
                if on_done is not None:
                    on_done()
                return
            if close_connection:
                self.ssh_client.close()
            self._set_busy(False, "操作失败，设备不会被自动重启；请检查日志后重试")
            logging.error("Tap-to-turn operation failed: %s", exc)
            if show_errors:
                show_error(
                    self,
                    _rmtool.APP_NAME,
                    f"操作失败：{exc}\n设备不会被自动重启。",
                )
            if on_done is not None:
                on_done()

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _detect_status(self):
        self._start_status_detection()

    def _start_status_detection(self, *, on_done=None, show_errors: bool = True):
        self._start_worker(
            _tap_page_turn_status,
            self.ssh_client,
            str(_rmtool.app_state_dir()),
            pending="正在获取清单并核对点击翻页包与设备状态…",
            on_done=on_done,
            show_errors=show_errors,
        )

    @require_connection
    def _enable(self):
        if not self._status or not self._status.package:
            return
        verification_notice = (
            "该精确包已完成对应真机验证。"
            if self._status.package.device_verified
            else "该精确包已完成官方固件离线验证，尚未进行实机验证。"
        )
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            f"{verification_notice}"
            "将下载并校验固件专用资源，并安装到 rmtool 管理的共享 Xovi。"
            "若检测到 Vellum/AppLoader Xovi，安装会在上传前停止。"
            "本次操作不会重启界面或设备；完成后 SSH 会话会关闭，"
            "请从设备菜单手动冷启动。是否继续？",
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
        if not self._status:
            return
        state = self._status.state
        if state is _tap_page_turn.TapPageTurnState.LEGACY_VELLUM:
            title = "卸载旧版 Vellum 点击翻页"
            confirmation = (
                "将卸载经精确验证的 rmtool Vellum 点击翻页包；"
                "不会卸载 Vellum/AppLoader 运行环境或其他插件。"
                "完成后请按 Vellum 官方说明处理其运行环境。是否继续？"
            )
            confirm_text = "卸载旧版"
            pending = "正在验证并卸载 rmtool 的旧版 Vellum 点击翻页包…"
            success = (
                "旧版 Vellum 点击翻页包已卸载，SSH 会话已关闭。\n"
                "请重新连接，按 Vellum 官方说明卸载其运行环境后，再安装 rmtool 版本。"
            )
        elif state is _tap_page_turn.TapPageTurnState.OUTDATED:
            title = "卸载旧版点击翻页"
            confirmation = (
                "将卸载经精确验证的旧固件点击翻页。"
                "操作不会重启设备；完成后请手动重启，"
                "再重新检测并安装当前版本。是否继续？"
            )
            confirm_text = "卸载旧版"
            pending = "正在卸载旧固件点击翻页并保留可验证的同伴功能…"
            success = (
                "点击翻页持久化已移除，SSH 会话已关闭。\n"
                "请从设备菜单手动重新启动。"
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
                "请从设备菜单手动重新启动。"
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

    def _load_local_package(self):
        if not self._status or not self._status.package:
            return
        package = self._status.package
        source_path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "加载本地点击翻页资源包",
            "",
            "资源包 (*.tar.gz);;所有文件 (*)",
        )
        if not source_path:
            return
        try:
            stored = _tap_page_turn.load_local_package(
                package, source_path, str(_rmtool.app_state_dir())
            )
        except Exception as exc:
            logging.error("Manual tap-to-turn package load failed: %s", exc)
            show_error(self, _rmtool.APP_NAME, f"手动加载失败：{exc}")
            return
        show_info(
            self,
            _rmtool.APP_NAME,
            "本地资源包已通过大小与 SHA-256 校验，并写入缓存：\n"
            f"{stored}\n\n点击“启用点击翻页”时会优先使用这份缓存。",
        )


class LegacyPluginMigrationSection(QtWidgets.QWidget):
    """Firmware-residue migration/cleanup plus historical Vellum cleanup."""

    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._busy = False
        self._report = None

        migration_title = QtWidgets.QLabel("固件升级插件迁移")
        migration_title.setObjectName("toolboxFeatureTitle")
        self.status_label = ToolboxStatusLabel(
            "固件升级后，已安装的 rmtool 插件会因固件身份变化而停用。"
            "在这里可以先用旧固件清单逐文件验证残留，再把全部已启用功能"
            "一次性迁移到当前固件的精确包，或只清理已验证残留；"
            "迁移完成后需手动重启设备。"
        )
        self.status_label.setWordWrap(True)
        self.detect_button = QtWidgets.QPushButton("检测迁移状态")
        self.detect_button.clicked.connect(self._detect)
        self.migrate_button = QtWidgets.QPushButton("迁移到当前固件")
        self.migrate_button.clicked.connect(self._migrate)
        self.cleanup_residue_button = QtWidgets.QPushButton("清理固件残留")
        self.cleanup_residue_button.setProperty("btnRole", "danger")
        self.cleanup_residue_button.clicked.connect(self._cleanup_residue)

        vellum_title = QtWidgets.QLabel("旧版 Vellum 插件清理")
        vellum_title.setObjectName("toolboxFeatureTitle")
        self.cleanup_status_label = ToolboxStatusLabel(
            "仅清理 rmtool 历史安装的点击翻页和快速黑白 Vellum 包；"
            "不会卸载 Vellum、AppLoader、Xovi 或其他插件。"
        )
        self.cleanup_status_label.setWordWrap(True)
        self.cleanup_button = QtWidgets.QPushButton("一键卸载旧版插件")
        self.cleanup_button.clicked.connect(self._cleanup)
        self.vellum_help_button = QtWidgets.QPushButton("查看 Vellum 官方卸载说明")
        self.vellum_help_button.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl(_legacy_vellum.VELLUM_UNINSTALL_URL)
            )
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(migration_title)
        layout.addWidget(self.status_label)
        detect_row = QtWidgets.QHBoxLayout()
        detect_row.setSpacing(_rmtool.SUBSECTION_GAP)
        detect_row.addWidget(self.detect_button)
        detect_row.addWidget(self.migrate_button)
        detect_row.addWidget(self.cleanup_residue_button)
        detect_row.addStretch(1)
        layout.addLayout(detect_row)
        layout.addWidget(vellum_title)
        layout.addWidget(self.cleanup_status_label)
        cleanup_row = QtWidgets.QHBoxLayout()
        cleanup_row.setSpacing(_rmtool.SUBSECTION_GAP)
        cleanup_row.addWidget(self.cleanup_button)
        cleanup_row.addWidget(self.vellum_help_button)
        cleanup_row.addStretch(1)
        layout.addLayout(cleanup_row)

        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    def _on_connection_changed(self, connected: bool):
        active = connected and not self._busy
        self.detect_button.setEnabled(active)
        self.cleanup_button.setEnabled(active)
        self.migrate_button.setEnabled(
            active and self._report is not None and self._report.migratable
        )
        self.cleanup_residue_button.setEnabled(
            active and self._report is not None and bool(self._report.features)
        )

    def _report_text(self, report) -> str:
        if report is None:
            return "未检测到固件升级残留；如需清理历史 Vellum 包请使用下方按钮。"
        lines = [
            "残留固件：{}（{}）".format(
                report.old_identity.firmware,
                _LEGACY_PLATFORM_LABELS.get(
                    report.old_identity.platform, report.old_identity.platform
                ),
            ),
            "当前固件：{}（{}）".format(
                report.new_identity.firmware,
                _LEGACY_PLATFORM_LABELS.get(
                    report.new_identity.platform, report.new_identity.platform
                ),
            ),
        ]
        for item in report.features:
            state = "已启用" if item.enabled else "未启用"
            target = "可迁移" if item.target_available else "当前固件无精确包"
            lines.append(f"{item.label}：{state}，{target}")
        for blocker in report.blockers:
            lines.append(f"阻断：{blocker}")
        lines.append(report.detail)
        return "\n".join(lines)

    @require_connection
    def _detect(self):
        self._busy = True
        self._on_connection_changed(True)
        self.status_label.setText("正在验证共享 Xovi 固件残留…")
        worker = _rmtool.Worker(_residue_migration.inspect_residue, self.ssh_client)

        def on_finished(report):
            if sip.isdeleted(self):
                return
            self._busy = False
            self._report = report
            self._on_connection_changed(self.ssh_client.is_connected())
            self.status_label.setText(self._report_text(report))

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error("Residue detection failed after tab close: %s", exc)
                return
            self._busy = False
            self._on_connection_changed(self.ssh_client.is_connected())
            self.status_label.setText(f"检测迁移状态失败：{exc}")
            logging.error("Residue detection failed: %s", exc)
            show_error(self, _rmtool.APP_NAME, f"检测失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _migrate(self):
        if self._report is None or not self._report.migratable:
            return
        features = "、".join(
            item.label for item in self._report.features if item.enabled
        )
        if not ask_confirmation(
            self,
            "迁移到当前固件",
            "rmtool 会先用旧固件的受信清单逐文件验证共享 Xovi 残留，"
            f"再把已启用功能（{features}）一次性替换为当前固件的精确包，"
            "并保持各功能的启用状态。任一验证失败都不会写入。"
            "迁移完成后请从设备菜单手动重启设备。是否继续？",
            confirm_text="迁移插件",
            cancel_text="取消",
        ):
            return

        self._busy = True
        self._on_connection_changed(True)
        self.status_label.setText("正在验证并迁移共享 Xovi 插件…")
        worker = _rmtool.Worker(
            _residue_migration.migrate, self.ssh_client, _rmtool.app_state_dir()
        )

        def on_finished(report):
            if sip.isdeleted(self):
                return
            self._busy = False
            self._report = None
            self._on_connection_changed(self.ssh_client.is_connected())
            self.status_label.setText("迁移完成。请从设备菜单手动重启设备。")
            show_info(
                self,
                _rmtool.APP_NAME,
                "已把全部已启用插件迁移到当前固件的精确包。"
                "请从设备菜单手动重启设备后生效。",
            )

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error("Residue migration failed after tab close: %s", exc)
                return
            self._busy = False
            self._on_connection_changed(self.ssh_client.is_connected())
            if isinstance(exc, _package_download.PackageDownloadError):
                # Download failed before any device change; keep the session.
                self.status_label.setText("资源包下载失败，可手动加载后重试")
                logging.error("Residue migration package download failed: %s", exc)
                _show_package_download_error(self, exc, retry=self._migrate)
                return
            self.status_label.setText(f"插件迁移失败：{exc}")
            logging.error("Residue migration failed: %s", exc)
            show_error(self, _rmtool.APP_NAME, f"迁移失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    @require_connection
    def _cleanup_residue(self):
        if self._report is None or not self._report.features:
            return
        features = "、".join(item.label for item in self._report.features)
        if not ask_confirmation(
            self,
            "清理固件升级残留",
            "rmtool 会再次使用旧固件的受信清单逐文件验证残留，然后删除整套旧共享 "
            f"Xovi 状态（{features}）。不会在当前固件重建功能，也不会自动重启设备。"
            "任一验证失败都不会清理。是否继续？",
            confirm_text="清理残留",
            cancel_text="取消",
            danger=True,
        ):
            return

        self._busy = True
        self._on_connection_changed(True)
        self.status_label.setText("正在重新验证并清理共享 Xovi 固件残留…")
        worker = _rmtool.Worker(_residue_migration.cleanup, self.ssh_client)

        def on_finished(_report):
            if sip.isdeleted(self):
                self.ssh_client.close()
                return
            self._busy = False
            self._report = None
            self._on_connection_changed(self.ssh_client.is_connected())
            self.status_label.setText("固件升级残留已清理。")
            show_info(
                self,
                _rmtool.APP_NAME,
                "已清理通过旧固件清单验证的共享 Xovi 残留。SSH 会话已关闭；"
                "重新连接后可安装当前固件支持的功能。",
            )
            self.ssh_client.close()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error("Firmware residue cleanup failed after tab close: %s", exc)
                return
            self._busy = False
            self._on_connection_changed(self.ssh_client.is_connected())
            self.status_label.setText(f"固件残留清理失败：{exc}")
            logging.error("Firmware residue cleanup failed: %s", exc)
            show_error(self, _rmtool.APP_NAME, f"清理失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

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
        self.cleanup_status_label.setText("正在验证并卸载 rmtool 历史 Vellum 功能包…")
        worker = _rmtool.Worker(_legacy_vellum.remove_legacy_plugins, self.ssh_client)

        def on_finished(removed: tuple[str, ...]):
            if sip.isdeleted(self):
                return
            self._busy = False
            self._on_connection_changed(self.ssh_client.is_connected())
            if removed:
                names = "、".join(removed)
                self.cleanup_status_label.setText(f"已卸载：{names}")
                show_info(
                    self,
                    _rmtool.APP_NAME,
                    "已卸载通过验证的 rmtool 旧版插件。Vellum/AppLoader/Xovi 本体仍保留。",
                )
            else:
                self.cleanup_status_label.setText("未检测到 rmtool 安装的旧版 Vellum 插件")
                show_info(self, _rmtool.APP_NAME, "没有需要卸载的 rmtool 旧版插件。")

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error("Legacy Vellum cleanup failed after tab close: %s", exc)
                return
            self._busy = False
            self._on_connection_changed(self.ssh_client.is_connected())
            self.cleanup_status_label.setText(f"旧版插件清理失败：{exc}")
            logging.error("Legacy Vellum cleanup failed: %s", exc)
            show_error(self, _rmtool.APP_NAME, f"操作失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)


class DiagnosticPreviewDialog(QtWidgets.QDialog):
    """Shows exactly what a diagnostic bundle will contain before saving."""

    def __init__(self, collected, parent=None):
        super().__init__(parent)
        self.setWindowTitle("诊断包预览")
        self.resize(760, 560)
        layout = QtWidgets.QVBoxLayout(self)

        hint = QtWidgets.QLabel(
            "以下是将写入诊断包的全部内容。带勾选的可选条目可能包含本地路径、"
            "文档名或字体名，如不希望提供可取消勾选。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.optional_boxes = {}
        for result in collected:
            if not result.item.optional:
                continue
            box = QtWidgets.QCheckBox(f"包含：{result.item.description}")
            box.setChecked(True)
            self.optional_boxes[result.item.name] = box
            layout.addWidget(box)

        preview = QtWidgets.QPlainTextEdit(self._preview_text(collected))
        preview.setReadOnly(True)
        preview.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        font = QtGui.QFont("Consolas")
        font.setStyleHint(QtGui.QFont.Monospace)
        preview.setFont(font)
        layout.addWidget(preview)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save
            | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.button(QtWidgets.QDialogButtonBox.Save).setText("保存诊断包…")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _preview_text(collected) -> str:
        chunks = []
        for result in collected:
            header = f"===== {result.item.name}（{result.item.description}）"
            if result.error:
                header += f" [采集失败：{result.error}]"
            elif result.truncated:
                header += " [已截断]"
            chunks.append(header + " =====")
            chunks.append(result.text.rstrip())
            chunks.append("")
        return "\n".join(chunks)

    def include_optional(self, name: str) -> bool:
        box = self.optional_boxes.get(name)
        return box is None or box.isChecked()


class DiagnosticsSection(QtWidgets.QWidget):
    """One-click read-only diagnostic bundle export for user support."""

    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._busy = False

        title = QtWidgets.QLabel("诊断日志导出")
        title.setObjectName("toolboxFeatureTitle")
        detail = QtWidgets.QLabel(
            "把 rmtool 本机日志和设备上的只读诊断信息（版本、服务状态、"
            "rmtool Xovi 启动日志、共享目录清单等）打包成一个 zip，"
            "供你发给维护者定位问题。导出前会展示全部内容供确认；"
            "可能包含本地路径、文档名或字体名的条目可以单独取消。"
            "设备凭据永远不会进入诊断包。"
        )
        detail.setWordWrap(True)

        self.status_label = ToolboxStatusLabel("设备已连接，尚未导出")
        self.status_label.setWordWrap(True)

        self.export_button = QtWidgets.QPushButton("生成诊断包…")
        self.export_button.setProperty("btnRole", "primary")

        buttons = QtWidgets.QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        buttons.addWidget(self.export_button)
        buttons.addStretch()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(title)
        layout.addWidget(detail)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.export_button.clicked.connect(self._export)
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(self.ssh_client.is_connected())

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self.status_label.setText("设备未连接")
        elif not self._busy:
            self.status_label.setText("设备已连接，尚未导出")
        self.export_button.setEnabled(connected and not self._busy)

    def _set_busy(self, busy: bool, message: str = ""):
        self._busy = busy
        if message:
            self.status_label.setText(message)
        self.export_button.setEnabled(self.ssh_client.is_connected() and not busy)

    @require_connection
    def _export(self):
        self._set_busy(True, "正在通过 SSH 采集只读诊断信息…")
        worker = _rmtool.Worker(
            _collect_diagnostics,
            self.ssh_client,
            _rmtool.app_state_dir() / "remarkable_tool.log",
        )

        def on_finished(collected):
            if sip.isdeleted(self):
                return
            self._set_busy(False, "采集完成，请预览并保存诊断包")
            self._preview_and_save(collected)

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error("Diagnostics collection failed after tab close: %s", exc)
                return
            self._set_busy(False, "诊断信息采集失败，未生成文件")
            logging.error("Diagnostics collection failed: %s", exc)
            show_error(self, _rmtool.APP_NAME, f"诊断信息采集失败：{exc}")

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    def _preview_and_save(self, collected):
        dialog = DiagnosticPreviewDialog(collected, self)
        if dialog.exec_() != QtWidgets.QDialog.Accepted:
            self.status_label.setText("已取消，未生成诊断包")
            return
        default_name = _diagnostics.bundle_name(
            _diagnostics.platform_label(collected)
        )
        target, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "保存诊断包", default_name, "诊断包 (*.zip)"
        )
        if not target:
            self.status_label.setText("已取消，未生成诊断包")
            return
        included = [
            result
            for result in collected
            if dialog.include_optional(result.item.name)
        ]
        try:
            saved = _diagnostics.write_bundle(target, collected, included)
        except Exception as exc:
            logging.error("Diagnostic bundle write failed: %s", exc)
            show_error(self, _rmtool.APP_NAME, f"诊断包写入失败：{exc}")
            return
        self.status_label.setText(f"诊断包已保存：{saved}")
        show_info(
            self,
            _rmtool.APP_NAME,
            "诊断包已保存：\n"
            f"{saved}\n\n"
            "请把这个文件发给维护者以协助定位问题。"
            "包内不包含任何设备密码。",
        )
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(Path(saved).parent))
        )


class ToolboxTab(QtWidgets.QWidget):
    def __init__(
        self,
        ssh_client: SSHClientWrapper,
        config: Dict,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self._detect_all_busy = False
        self._detect_all_index = 0
        self.time_section = TimeTab(ssh_client)
        self.control_section = ControlTab(ssh_client)
        self.native_chinese_section = NativeChineseSection(ssh_client)
        self.pinyin_input_section = PinyinInputSection(ssh_client)
        self.reading_enhancements_section = ReadingEnhancementsSection(ssh_client)
        self.tap_page_turn_section = TapPageTurnSection(ssh_client)
        self.diagnostics_section = DiagnosticsSection(ssh_client)
        self.legacy_plugin_section = LegacyPluginMigrationSection(ssh_client)
        self._detectable_sections = (
            self.native_chinese_section,
            self.pinyin_input_section,
            self.reading_enhancements_section,
            self.tap_page_turn_section,
        )
        self._device_identity = None
        self._identity_probe_running = False
        self._identity_connection_state: Optional[bool] = None
        self._identity_connection_generation = 0
        self.thread_pool = QtCore.QThreadPool.globalInstance()

        self._tool_entries = (
            {
                "title": "原生简体中文",
                "category": "中文与输入",
                "keywords": "中文 汉化 语言 法语",
                "section": self.native_chinese_section,
                "status": self.native_chinese_section.status_label,
            },
            {
                "title": "拼音输入法",
                "category": "中文与输入",
                "keywords": "中文 键盘 输入 候选",
                "section": self.pinyin_input_section,
                "status": self.pinyin_input_section.status_label,
            },
            {
                "title": "阅读增强",
                "category": "阅读增强",
                "keywords": "阅读 点击翻页 手势 黑白 刷新 残影 PDF EPUB",
                "section": self.reading_enhancements_section,
                "status": self.reading_enhancements_section.status_label,
            },
            {
                "title": "点击翻页（RM1/RM2/Paper Pure）",
                "category": "阅读增强",
                "keywords": "阅读 点击翻页 手势 PDF EPUB RM1 RM2 一代 二代 Paper Pure 离线验证",
                "section": self.tap_page_turn_section,
                "status": self.tap_page_turn_section.status_label,
                "device_scoped": True,
                "hidden_for_device": True,
            },
            {
                "title": "时间管理",
                "category": "设备维护",
                "keywords": "同步 时区 时钟",
                "section": self.time_section,
                "status": None,
            },
            {
                "title": "设备控制",
                "category": "设备维护",
                "keywords": "重启 Wi-Fi SSH 前光",
                "section": self.control_section,
                "status": None,
            },
            {
                "title": "诊断日志导出",
                "category": "设备维护",
                "keywords": "诊断 日志 导出 反馈 支持 排障",
                "section": self.diagnostics_section,
                "status": self.diagnostics_section.status_label,
            },
            {
                "title": "旧版插件迁移/清理",
                "category": "设备维护",
                "keywords": "Vellum AppLoader Xovi 卸载 残留 迁移 固件升级",
                "section": self.legacy_plugin_section,
                "status": None,
            },
        )

        self.search_input = QtWidgets.QLineEdit()
        self.search_input.setObjectName("toolboxSearchInput")
        self.search_input.setPlaceholderText("搜索插件与工具")
        self.search_input.setClearButtonEnabled(True)

        self.category_combo = QtWidgets.QComboBox()
        self.category_combo.setObjectName("toolboxCategoryFilter")
        self.category_combo.addItems(("全部分类", "中文与输入", "阅读增强", "设备维护"))

        self.detect_all_button = QtWidgets.QPushButton("检测全部插件")
        self.detect_all_button.setObjectName("toolboxDetectAllButton")

        self.tool_table = QtWidgets.QTableWidget(len(self._tool_entries), 2)
        self.tool_table.setObjectName("toolboxBrowserList")
        self.tool_table.setHorizontalHeaderLabels(("功能", "状态"))
        self.tool_table.verticalHeader().setVisible(False)
        self.tool_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.tool_table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.tool_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tool_table.setShowGrid(False)
        self.tool_table.setAlternatingRowColors(False)
        self.tool_table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        header = self.tool_table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setMinimumSectionSize(72)

        self.detail_stack = QtWidgets.QStackedWidget()
        self.detail_stack.setObjectName("toolboxDetailStack")
        for row, entry in enumerate(self._tool_entries):
            name_item = QtWidgets.QTableWidgetItem(entry["title"])
            name_item.setData(QtCore.Qt.UserRole, entry["category"])
            name_item.setData(
                QtCore.Qt.UserRole + 1,
                f'{entry["title"]} {entry["category"]} {entry["keywords"]}'.casefold(),
            )
            status_item = QtWidgets.QTableWidgetItem()
            status_item.setTextAlignment(QtCore.Qt.AlignCenter)
            self.tool_table.setItem(row, 0, name_item)
            self.tool_table.setItem(row, 1, status_item)
            self.tool_table.setRowHeight(row, 48)
            self.detail_stack.addWidget(
                self._detail_page(entry["title"], entry["category"], entry["section"])
            )
            status_label = entry["status"]
            if status_label is not None:
                status_label.text_changed.connect(
                    lambda _text, current_row=row: self._refresh_row_status(current_row)
                )
            self._refresh_row_status(row)

        self.empty_detail_page = self._empty_detail_page()
        self.detail_stack.addWidget(self.empty_detail_page)

        browser_title = QtWidgets.QLabel("插件与工具")
        browser_title.setObjectName("toolboxBrowserTitle")
        self.browser_hint = QtWidgets.QLabel("选择项目后查看说明与操作")
        self.browser_hint.setObjectName("toolboxBrowserHint")

        self.filter_container = QtWidgets.QWidget()
        self.filter_container.setObjectName("toolboxFilterContainer")
        self.filter_layout = QtWidgets.QBoxLayout(
            QtWidgets.QBoxLayout.TopToBottom,
            self.filter_container,
        )
        self.filter_layout.setContentsMargins(0, 0, 0, 0)
        self.filter_layout.setSpacing(_rmtool.SUBSECTION_GAP)
        self.filter_layout.addWidget(self.search_input)
        self.filter_layout.addWidget(self.category_combo)

        self.left_panel = QtWidgets.QFrame()
        self.left_panel.setObjectName("toolboxBrowserSidebar")
        self.left_panel.setMinimumWidth(230)
        self.left_panel.setMaximumWidth(340)
        left_layout = QtWidgets.QVBoxLayout(self.left_panel)
        left_layout.setContentsMargins(
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
        )
        left_layout.setSpacing(_rmtool.SUBSECTION_GAP)
        left_layout.addWidget(browser_title)
        left_layout.addWidget(self.browser_hint)
        left_layout.addWidget(self.filter_container)
        left_layout.addWidget(self.detect_all_button)
        left_layout.addWidget(self.tool_table, 1)

        self.detail_panel = QtWidgets.QFrame()
        self.detail_panel.setObjectName("toolboxDetailPanel")
        detail_layout = QtWidgets.QVBoxLayout(self.detail_panel)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self.detail_stack)

        self.browser_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.browser_splitter.setObjectName("toolboxBrowserSplitter")
        self.browser_splitter.setChildrenCollapsible(False)
        self.browser_splitter.addWidget(self.left_panel)
        self.browser_splitter.addWidget(self.detail_panel)
        self.browser_splitter.setStretchFactor(0, 0)
        self.browser_splitter.setStretchFactor(1, 1)
        self.browser_splitter.setSizes((270, 850))

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
        )
        layout.addWidget(self.browser_splitter)

        self.tool_table.currentCellChanged.connect(self._show_current_tool)
        self.search_input.textChanged.connect(self._apply_filters)
        self.category_combo.currentTextChanged.connect(self._apply_filters)
        self.detect_all_button.clicked.connect(self._detect_all_statuses)
        self.ssh_client.connection_changed.connect(self._on_connection_changed)
        self.tool_table.setCurrentCell(0, 0)
        self._compact_browser = False
        self._on_connection_changed(self.ssh_client.is_connected())

    def _detail_page(self, title: str, category: str, section: QtWidgets.QWidget):
        for old_title in section.findChildren(QtWidgets.QLabel, "toolboxFeatureTitle"):
            old_title.hide()

        category_label = QtWidgets.QLabel(category)
        category_label.setObjectName("toolboxDetailCategory")
        title_label = QtWidgets.QLabel(title)
        title_label.setObjectName("toolboxDetailTitle")

        divider = QtWidgets.QFrame()
        divider.setFrameShape(QtWidgets.QFrame.HLine)
        divider.setObjectName("toolboxDetailDivider")

        content = QtWidgets.QWidget()
        content.setObjectName("toolboxDetailContent")
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
        )
        content_layout.setSpacing(_rmtool.SUBSECTION_GAP)
        content_layout.addWidget(category_label)
        content_layout.addWidget(title_label)
        content_layout.addWidget(divider)
        content_layout.addWidget(section)
        content_layout.addStretch()

        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("toolboxDetailScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(content)

        page = QtWidgets.QWidget()
        page_layout = QtWidgets.QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addWidget(scroll)
        return page

    def _empty_detail_page(self):
        title = QtWidgets.QLabel("没有匹配的工具")
        title.setObjectName("toolboxEmptyTitle")
        hint = QtWidgets.QLabel("请调整搜索内容或分类筛选。")
        hint.setObjectName("toolboxBrowserHint")
        layout = QtWidgets.QVBoxLayout()
        layout.addStretch()
        layout.addWidget(title, alignment=QtCore.Qt.AlignCenter)
        layout.addWidget(hint, alignment=QtCore.Qt.AlignCenter)
        layout.addStretch()
        page = QtWidgets.QWidget()
        page.setLayout(layout)
        return page

    @staticmethod
    def _status_summary(text: str) -> str:
        text = text.strip()
        if any(word in text for word in ("失败", "不完整", "被修改", "需要修复", "残留")):
            return "需处理"
        if "可迁移" in text:
            return "可迁移"
        if any(word in text for word in ("不兼容", "没有精确匹配", "当前设备没有")):
            return "不兼容"
        if "未连接" in text:
            return "未连接"
        if any(word in text for word in ("尚未检测", "检测后显示")):
            return "待检测"
        if any(word in text for word in ("尚未安装", "未安装")):
            return "未安装"
        if any(word in text for word in ("未启用", "已停用")):
            return "已停用"
        if any(word in text for word in ("已启用", "已加载", "正在运行")):
            return "已启用"
        return "已检测"

    def _refresh_row_status(self, row: int):
        entry = self._tool_entries[row]
        status_label = entry["status"]
        full_status = status_label.text() if status_label is not None else "设备维护工具"
        status_item = self.tool_table.item(row, 1)
        status_item.setText(
            self._status_summary(full_status) if status_label is not None else "工具"
        )
        status_item.setToolTip(full_status)

    def _show_current_tool(self, current_row: int, _column: int, _old_row: int, _old_column: int):
        if current_row >= 0 and not self.tool_table.isRowHidden(current_row):
            self.detail_stack.setCurrentIndex(current_row)
        else:
            self.detail_stack.setCurrentWidget(self.empty_detail_page)

    def _apply_filters(self, _value=None):
        query = self.search_input.text().strip().casefold()
        category = self.category_combo.currentText()
        visible_rows = []
        for row in range(self.tool_table.rowCount()):
            name_item = self.tool_table.item(row, 0)
            entry = self._tool_entries[row]
            matches_query = not query or query in name_item.data(QtCore.Qt.UserRole + 1)
            matches_category = (
                category == "全部分类" or category == name_item.data(QtCore.Qt.UserRole)
            )
            visible = (
                matches_query
                and matches_category
                and not entry.get("hidden_for_device", False)
            )
            self.tool_table.setRowHidden(row, not visible)
            if visible:
                visible_rows.append(row)

        current_row = self.tool_table.currentRow()
        if current_row not in visible_rows:
            self.tool_table.clearSelection()
            self.tool_table.setCurrentCell(-1, -1)
            if visible_rows:
                self.tool_table.setCurrentCell(visible_rows[0], 0)
            else:
                self.detail_stack.setCurrentWidget(self.empty_detail_page)

    def _on_connection_changed(self, connected: bool):
        connected = bool(connected)
        if connected != self._identity_connection_state:
            self._identity_connection_state = connected
            self._identity_connection_generation += 1
            self._identity_probe_running = False
        self.detect_all_button.setEnabled(connected and not self._detect_all_busy)
        if not connected:
            self._device_identity = None
            self._update_device_scoped_entries()
        else:
            self._probe_device_identity()

    def _probe_device_identity(self):
        """Quietly learn the device identity to scope device-specific entries."""
        if (
            self._identity_probe_running
            or not self.ssh_client.is_connected()
            or sip.isdeleted(self)
        ):
            return
        self._identity_probe_running = True
        connection_generation = self._identity_connection_generation
        worker = _rmtool.Worker(_tap_page_turn.get_device_identity, self.ssh_client)

        def on_finished(identity):
            if sip.isdeleted(self):
                return
            if connection_generation != self._identity_connection_generation:
                return
            self._identity_probe_running = False
            self._device_identity = identity
            self._update_device_scoped_entries()

        def on_error(_exc):
            if sip.isdeleted(self):
                return
            if connection_generation != self._identity_connection_generation:
                return
            self._identity_probe_running = False
            self._device_identity = None
            self._update_device_scoped_entries()

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    def _tap_entry_hidden(self) -> bool:
        return any(
            entry.get("device_scoped") and entry.get("hidden_for_device")
            for entry in self._tool_entries
        )

    def _update_device_scoped_entries(self):
        """Show the tap entry only where reading enhancements has no package."""
        show_tap = False
        if self._device_identity is not None:
            try:
                show_tap = (
                    _tap_page_turn.select_package(
                        _tap_page_turn._trusted_catalog(), self._device_identity
                    )
                    is not None
                    and _reading_enhancements.select_package(
                        _reading_enhancements._trusted_catalog(),
                        self._device_identity,
                    )
                    is None
                )
            except Exception:
                logging.exception("Could not evaluate device-scoped tool entries")
                show_tap = False
        for entry in self._tool_entries:
            if entry.get("device_scoped"):
                entry["hidden_for_device"] = not show_tap
        self._apply_filters()

    @require_connection
    def _detect_all_statuses(self):
        if self._detect_all_busy:
            return
        self._detect_all_busy = True
        self._detect_all_index = 0
        self.detect_all_button.setEnabled(False)
        self._run_next_status_detection()

    def _run_next_status_detection(self):
        if sip.isdeleted(self):
            return
        if (
            not self.ssh_client.is_connected()
            or self._detect_all_index >= len(self._detectable_sections)
        ):
            self._detect_all_busy = False
            self.detect_all_button.setText("检测全部插件")
            self.detect_all_button.setEnabled(self.ssh_client.is_connected())
            return

        section = self._detectable_sections[self._detect_all_index]
        while (
            section is self.tap_page_turn_section
            and self._tool_entries and self._tap_entry_hidden()
        ):
            self._detect_all_index += 1
            if self._detect_all_index >= len(self._detectable_sections):
                self._detect_all_busy = False
                self.detect_all_button.setText("检测全部插件")
                self.detect_all_button.setEnabled(self.ssh_client.is_connected())
                return
            section = self._detectable_sections[self._detect_all_index]
        self._detect_all_index += 1
        self.detect_all_button.setText(
            f"正在检测 {self._detect_all_index}/{len(self._detectable_sections)}"
        )
        section._start_status_detection(
            on_done=self._run_next_status_detection,
            show_errors=False,
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = self.width() < 900
        if compact == self._compact_browser:
            return
        self._compact_browser = compact
        if compact:
            self.browser_splitter.setOrientation(QtCore.Qt.Vertical)
            self.browser_hint.hide()
            self.filter_layout.setDirection(QtWidgets.QBoxLayout.LeftToRight)
            self.left_panel.setMinimumWidth(0)
            self.left_panel.setMaximumWidth(QtWidgets.QWIDGETSIZE_MAX)
            self.left_panel.setMaximumHeight(330)
            self.browser_splitter.setSizes((320, max(240, self.height() - 320)))
        else:
            self.browser_splitter.setOrientation(QtCore.Qt.Horizontal)
            self.browser_hint.show()
            self.filter_layout.setDirection(QtWidgets.QBoxLayout.TopToBottom)
            self.left_panel.setMaximumHeight(QtWidgets.QWIDGETSIZE_MAX)
            self.left_panel.setMinimumWidth(230)
            self.left_panel.setMaximumWidth(340)
            self.browser_splitter.setSizes((270, max(640, self.width() - 270)))


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
