"""DocumentsTab extracted from rmtool.py."""

import logging
import os
import posixpath
import shlex
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import paramiko
from PyQt5 import QtCore, QtGui, QtWidgets, sip

from _dialogs import ask_confirmation, show_error, show_info, show_warning
from _native_document_import import NativeDocumentImporter
import _native_import_control as native_control
from _ssh import SSHClientWrapper, require_connection
from _ui_tasks import run_in_background
import rmtool as _rmtool  # late-bound access to avoid circular import


@dataclass(frozen=True)
class _OrphanThumbnailScan:
    candidate_ids: Tuple[str, ...]
    file_count: int
    total_bytes: int

    @property
    def directory_count(self) -> int:
        return len(self.candidate_ids)


def _safe_extract_archive(archive_path: Path, target_dir: Path) -> None:
    if not zipfile.is_zipfile(archive_path):
        return
    target_root = target_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            destination = (target_dir / member.filename).resolve()
            if target_root != destination and target_root not in destination.parents:
                raise RuntimeError(f"导出文件包含不安全路径：{member.filename}")
        archive.extractall(target_dir)


class DocumentsTab(QtWidgets.QWidget):
    summary_changed = QtCore.pyqtSignal(dict)
    status_message = QtCore.pyqtSignal(str, str, int)
    disconnect_requested = QtCore.pyqtSignal()

    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self.documents: List[_rmtool.DocumentItem] = []
        self._documents_by_id: Dict[str, _rmtool.DocumentItem] = {}
        self._current_preview_request: Optional[str] = None
        self._preview_cover: Optional[bytes] = None
        self._active_progress: Optional[QtWidgets.QProgressDialog] = None
        self._progress_label_base: str = ""
        self._connected = False
        self._connection_generation = 0
        self._upload_active = False
        self._thumbnail_cleanup_running = False

        # --- Toolbar ---
        self.refresh_button = QtWidgets.QPushButton("刷新列表")
        self.upload_button = QtWidgets.QPushButton("上传文档")
        self.upload_button.setProperty("btnRole", "primary")
        self.native_transfer_button = QtWidgets.QToolButton()
        self.native_transfer_button.setIcon(self.style().standardIcon(QtWidgets.QStyle.SP_DriveNetIcon))
        self.native_transfer_button.setToolTip("原生传输服务")
        self.native_transfer_button.setAccessibleName("原生传输服务")
        self.native_transfer_button.setFixedSize(36, 36)
        self.native_transfer_button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        native_menu = QtWidgets.QMenu(self.native_transfer_button)
        native_menu.addAction("初始化自动传输…", self._initialize_native_transfer)
        native_menu.addAction("移除传输组件…", self._remove_native_transfer)
        self.native_transfer_button.setMenu(native_menu)
        self.cleanup_thumbnails_button = QtWidgets.QPushButton("清理缩略图")
        self.cleanup_thumbnails_button.setToolTip(
            "扫描已不存在文档留下的封面缩略图，确认后再删除"
        )
        self.delete_button = QtWidgets.QPushButton("删除文档")
        self.delete_button.setProperty("btnRole", "danger")
        self.export_button = QtWidgets.QPushButton("导出为 PDF")
        self.export_button.setToolTip("将笔记渲染并导出为 PDF 文件（需要 rm/note 数据）")
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("搜索文档名称")
        self.search_edit.setPlaceholderText("🔍 搜索文档…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setPlaceholderText("搜索文档名称")
        # Never let the toolbar squeeze the search box below its placeholder.
        self.search_edit.setMinimumWidth(180)

        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview_image = _rmtool.PreviewImageLabel("暂无预览")
        self.preview_image.setWordWrap(True)
        self.preview_image.set_corner_radius(_rmtool.INNER_PANEL_RADIUS)

        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["名称", "类型", "更新时间"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header.setStretchLastSection(False)

        self.results_summary_label = QtWidgets.QLabel("显示 0 / 0 个文档")
        self.results_summary_label.setObjectName("documentsSummaryLabel")
        self.selection_summary_label = QtWidgets.QLabel("未选择文档")
        self.selection_summary_label.setObjectName("documentsSummaryLabel")
        self.empty_state_label = QtWidgets.QLabel("连接设备后即可加载文档。")
        self.empty_state_label.setObjectName("documentsEmptyState")
        self.empty_state_label.setAlignment(QtCore.Qt.AlignCenter)
        self.empty_state_label.setWordWrap(True)
        self.empty_state_label.hide()

        top_layout = QtWidgets.QHBoxLayout()
        top_layout.addWidget(self.refresh_button)
        top_layout.addWidget(self.upload_button)
        top_layout.addWidget(self.native_transfer_button)
        top_layout.addWidget(self.cleanup_thumbnails_button)
        top_layout.addWidget(self.delete_button)
        top_layout.addWidget(self.export_button)
        top_layout.addStretch()
        top_layout.addWidget(self.search_edit)
        top_layout.addSpacing(8)

        summary_layout = QtWidgets.QHBoxLayout()
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.addWidget(self.results_summary_label)
        summary_layout.addStretch()
        summary_layout.addWidget(self.selection_summary_label)

        self._preview_stack = QtWidgets.QStackedWidget()
        self._preview_stack.addWidget(self.preview)        # index 0
        self._preview_stack.addWidget(self.preview_image)  # index 1

        self._meta_btn = QtWidgets.QPushButton("元数据")
        self._meta_btn.setCheckable(True)
        self._meta_btn.setChecked(True)
        self._image_btn = QtWidgets.QPushButton("图像预览")
        self._image_btn.setCheckable(True)

        preview_btn_group = QtWidgets.QButtonGroup(self)
        preview_btn_group.setExclusive(True)
        preview_btn_group.addButton(self._meta_btn, 0)
        preview_btn_group.addButton(self._image_btn, 1)
        preview_btn_group.idClicked.connect(self._preview_stack.setCurrentIndex)

        preview_switch_row = QtWidgets.QHBoxLayout()
        preview_switch_row.setContentsMargins(0, 0, 0, 0)
        preview_switch_row.addStretch()
        preview_switch_row.addWidget(self._meta_btn)
        preview_switch_row.addWidget(self._image_btn)
        preview_switch_row.addStretch()

        self.preview_panel = QtWidgets.QFrame()
        self.preview_panel.setObjectName("documentsPreviewPanel")
        self.preview_panel.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        preview_layout = QtWidgets.QVBoxLayout(self.preview_panel)
        preview_layout.setContentsMargins(
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
        )
        preview_layout.setSpacing(_rmtool.SUBSECTION_GAP)
        preview_layout.addLayout(preview_switch_row)
        preview_layout.addWidget(self._preview_stack)

        self.list_panel = QtWidgets.QFrame()
        self.list_panel.setObjectName("documentsListPanel")
        self.list_panel.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        left_layout = QtWidgets.QVBoxLayout(self.list_panel)
        left_layout.setContentsMargins(
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
        )
        left_layout.setSpacing(_rmtool.SUBSECTION_GAP)
        left_layout.addLayout(top_layout)
        left_layout.addLayout(summary_layout)
        left_layout.addWidget(self.table)
        left_layout.addWidget(self.empty_state_label)

        self.content_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.content_splitter.addWidget(self.list_panel)
        self.content_splitter.addWidget(self.preview_panel)
        # Default to a ~58:42 list/preview split: the list panel stays wide
        # enough for its toolbar (incl. the search box) while the preview
        # still shows covers near full height.
        self.content_splitter.setStretchFactor(0, 7)
        self.content_splitter.setStretchFactor(1, 5)
        self.content_splitter.setSizes([1160, 840])
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setHandleWidth(_rmtool.PANEL_GAP)
        self.preview_panel.setMinimumWidth(360)

        layout = QtWidgets.QHBoxLayout()
        layout.setContentsMargins(
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
        )
        layout.setSpacing(0)
        layout.addWidget(self.content_splitter)
        self.setLayout(layout)

        self.refresh_button.clicked.connect(self.refresh)
        self.upload_button.clicked.connect(self.upload_document)
        self.cleanup_thumbnails_button.clicked.connect(self._cleanup_orphan_thumbnails)
        self.delete_button.clicked.connect(self._delete_document)
        self.export_button.clicked.connect(self._export_as_pdf)
        self.search_edit.textChanged.connect(self._apply_filter)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.set_connection_state(False)
        self._update_results_summary()
        self._update_action_state()
        self._update_empty_state()

    # -- Search / filter -------------------------------------------------------
    def _apply_filter(self, text: str) -> None:
        text = text.strip().lower()
        for row in range(self.table.rowCount()):
            item = self._document_for_row(row)
            visible = bool(item) and (not text or text in item.name.lower())
            self.table.setRowHidden(row, not visible)

        selected = self._selected_document()
        if selected:
            selected_row = self._row_for_document(selected.identifier)
            if selected_row is not None and self.table.isRowHidden(selected_row):
                self.table.clearSelection()

        self._update_results_summary()
        self._update_action_state()
        self._update_empty_state()

    def set_connection_state(self, connected: bool) -> None:
        if self._connected and not connected:
            # Invalidate results of workers started before the disconnect so a
            # stale list from the previous connection is never applied.
            self._connection_generation += 1
        self._connected = connected
        if not connected:
            self._upload_active = False
            for button in (self.upload_button, self.native_transfer_button):
                if hasattr(button, "_rmtool_background_task"):
                    del button._rmtool_background_task
            self._close_progress_dialog()
            self.documents = []
            self._documents_by_id.clear()
            self.table.setRowCount(0)
            self._current_preview_request = None
            self.preview.clear()
            self._set_preview_placeholder("暂无预览")
            self._update_results_summary()
            self.summary_changed.emit(self._build_summary())
        self.refresh_button.setEnabled(connected)
        native_busy = hasattr(self.native_transfer_button, "_rmtool_background_task")
        self.upload_button.setEnabled(connected and not self._upload_active and not native_busy)
        self.native_transfer_button.setEnabled(connected and not self._upload_active and not native_busy)
        self._update_action_state()
        self._update_empty_state()

    def _document_for_row(self, row: int) -> Optional[_rmtool.DocumentItem]:
        item = self.table.item(row, 0)
        if item is None:
            return None
        identifier = item.data(QtCore.Qt.UserRole)
        if not identifier:
            return None
        return self._documents_by_id.get(str(identifier))

    def _row_for_document(self, identifier: str) -> Optional[int]:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(QtCore.Qt.UserRole) == identifier:
                return row
        return None

    def _selected_document(self) -> Optional[_rmtool.DocumentItem]:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        current_row = self.table.currentRow()
        if current_row >= 0 and any(index.row() == current_row for index in indexes):
            return self._document_for_row(current_row)
        return self._document_for_row(sorted(index.row() for index in indexes)[0])

    def _selected_documents(self) -> List[_rmtool.DocumentItem]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        documents = []
        for row in rows:
            item = self._document_for_row(row)
            if item is not None:
                documents.append(item)
        return documents

    def _visible_document_count(self) -> int:
        return sum(0 if self.table.isRowHidden(row) else 1 for row in range(self.table.rowCount()))

    def _update_results_summary(self) -> None:
        visible = self._visible_document_count()
        self.results_summary_label.setText(f"显示 {visible} / {len(self.documents)} 个文档")

    def _update_action_state(self) -> None:
        selected = self._selected_document()
        selected_documents = self._selected_documents()
        has_selection = bool(selected_documents)
        single_selection = len(selected_documents) == 1
        self.delete_button.setEnabled(self._connected and has_selection)
        self.cleanup_thumbnails_button.setEnabled(
            self._connected and not self._thumbnail_cleanup_running
        )
        self.export_button.setEnabled(
            self._connected
            and selected is not None
            and single_selection
            and any(ext in selected.available_assets for ext in ("rm", "note"))
        )

        if not selected_documents:
            self.selection_summary_label.setText("未选择文档")
        elif len(selected_documents) > 1:
            self.selection_summary_label.setText(f"已选择 {len(selected_documents)} 个文档")
        else:
            selected = selected_documents[0]
            asset_text = ", ".join(selected.available_assets) if selected.available_assets else "无资源"
            self.selection_summary_label.setText(f"已选择：{selected.name} · {asset_text}")

    def _update_empty_state(self) -> None:
        if not self._connected:
            self.empty_state_label.setText("连接设备后即可加载文档。")
            self.empty_state_label.show()
            return
        if not self.documents:
            self.empty_state_label.setText("当前设备还没有文档，上传 PDF 或 EPUB 开始使用。")
            self.empty_state_label.show()
            return
        if self._visible_document_count() == 0:
            self.empty_state_label.setText("没有匹配的文档，换个关键词试试。")
            self.empty_state_label.show()
            return
        self.empty_state_label.hide()

    def _set_preview_placeholder(self, text: str) -> None:
        self.preview_image.clear_preview()
        self.preview_image.setText(text)
        self._preview_cover = None

    # -- Progress helpers ------------------------------------------------------
    def _show_progress_dialog(self, title: str, text: str) -> None:
        if self._active_progress:
            self._active_progress.close()
            self._active_progress.deleteLater()
        dialog = QtWidgets.QProgressDialog(text, None, 0, 0, self)
        dialog.setWindowTitle(title)
        dialog.setCancelButton(None)
        dialog.setWindowModality(QtCore.Qt.NonModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setValue(0)
        dialog.show()
        self._active_progress = dialog
        self._progress_label_base = text

    def _update_progress_dialog(self, current: int, total: int) -> None:
        dialog = self._active_progress
        if not dialog:
            return
        if total <= 0:
            dialog.setRange(0, 0)
            dialog.setLabelText(self._progress_label_base)
            return
        dialog.setRange(0, 1000)
        ratio = max(0.0, min(float(current) / float(total), 1.0))
        dialog.setValue(int(ratio * 1000))
        dialog.setLabelText(f"{self._progress_label_base}\n已完成 {ratio * 100:.1f}%")

    def _close_progress_dialog(self) -> None:
        if self._active_progress:
            self._active_progress.close()
            self._active_progress.deleteLater()
            self._active_progress = None
            self._progress_label_base = ""

    @staticmethod
    def _safe_stat_size(sftp: paramiko.SFTPClient, remote_path: str) -> int:
        try:
            return int(sftp.stat(remote_path).st_size)
        except IOError:
            return 0

    def _collect_remote_files(
        self,
        sftp: paramiko.SFTPClient,
        remote_dir: str,
        base_dir: Optional[str] = None,
    ) -> List[Tuple[str, str, int]]:
        if base_dir is None:
            base_dir = remote_dir
        files: List[Tuple[str, str, int]] = []
        entries = sftp.listdir_attr(remote_dir)
        for entry in entries:
            remote_path = f"{remote_dir}/{entry.filename}"
            if stat.S_ISDIR(entry.st_mode):
                files.extend(self._collect_remote_files(sftp, remote_path, base_dir))
            else:
                relative = remote_path[len(base_dir) :].lstrip("/")
                files.append((remote_path, relative, int(entry.st_size)))
        return files

    # -- Refresh ---------------------------------------------------------------
    def refresh(self):
        if not self.ssh_client.is_connected():
            return
        connection_generation = self._connection_generation
        worker = _rmtool.Worker(self._load_documents)

        def on_finished(documents):
            if sip.isdeleted(self):
                return
            if connection_generation == self._connection_generation:
                self._on_documents_loaded(documents)

        def on_error(exc):
            if sip.isdeleted(self):
                return
            if connection_generation == self._connection_generation:
                self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    def refresh_quiet(self, on_done) -> None:
        """Post-connect refresh driven by the MainWindow serial coordinator.

        Loads and applies the document list like ``refresh`` but never pops an
        error dialog: failures go to the log and the status bar only. Calls
        ``on_done`` exactly once, on success and on failure, so the
        coordinator can start the next step. Results produced before a
        disconnect/reconnect are discarded via ``_connection_generation``.
        """
        if not self.ssh_client.is_connected():
            on_done()
            return
        connection_generation = self._connection_generation
        worker = _rmtool.Worker(self._load_documents)

        def on_finished(documents):
            try:
                if sip.isdeleted(self):
                    return
                if connection_generation != self._connection_generation:
                    # Stale result from a previous connection; discard it.
                    logging.info(
                        "Discarding stale document list from a previous connection"
                    )
                    return
                self._on_documents_loaded(documents)
            except Exception:
                # Never let an apply failure stall the serial coordinator.
                logging.exception("Failed to apply post-connect document list")
            finally:
                on_done()

        def on_error(exc: Exception):
            try:
                if sip.isdeleted(self):
                    logging.error(
                        "Post-connect document refresh failed after tab close: %s", exc
                    )
                    return
                if connection_generation != self._connection_generation:
                    return
                logging.error("Post-connect document refresh failed: %s", exc)
                self.status_message.emit("error", f"文档列表刷新失败：{exc}", 4000)
            finally:
                on_done()

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    def _load_documents(self) -> List[_rmtool.DocumentItem]:
        """Load document list using a single SFTP session for efficiency."""
        with self.ssh_client.sftp_session() as sftp:
            return _rmtool.load_document_items(sftp)

    def _on_documents_loaded(self, documents: List[_rmtool.DocumentItem]):
        self.documents = documents
        self._documents_by_id = {item.identifier: item for item in documents}
        sorting_enabled = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(documents))
        for row, item in enumerate(documents):
            name_item = QtWidgets.QTableWidgetItem(item.name)
            name_item.setData(QtCore.Qt.UserRole, item.identifier)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QtWidgets.QTableWidgetItem(item.doc_type))
            updated_text = item.updated.strftime("%Y-%m-%d %H:%M") if item.updated else ""
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(updated_text))
        self.table.setSortingEnabled(sorting_enabled)
        self.table.clearSelection()
        self._current_preview_request = None
        self.preview.clear()
        self._set_preview_placeholder("暂无预览")
        # Re-apply current search filter
        self._apply_filter(self.search_edit.text())
        self._update_action_state()
        self._update_empty_state()
        self.summary_changed.emit(self._build_summary())
        self.status_message.emit("info", f"已加载 {len(documents)} 个文档。", 2500)

    def _on_error(self, exc: Exception):
        show_error(self, _rmtool.APP_NAME, f"操作失败：{exc}")

    def _on_selection_changed(self):
        item = self._selected_document()
        self._update_action_state()
        if not item:
            self._current_preview_request = None
            self.preview.clear()
            self._set_preview_placeholder("暂无预览")
            return
        meta_text = [
            f"ID: {item.identifier}",
            f"名称: {item.name}",
            f"类型: {item.doc_type}",
            f"更新时间: {item.updated.strftime('%Y-%m-%d %H:%M:%S') if item.updated else '未知'}",
            f"可用资源: {', '.join(item.available_assets) if item.available_assets else '无'}",
        ]
        self.preview.setPlainText("\n".join(meta_text))
        self._set_preview_placeholder("加载预览中...")
        self._current_preview_request = item.identifier
        request_id = item.identifier
        connection_generation = self._connection_generation
        worker = _rmtool.Worker(self._fetch_preview_cover, item)

        # Closures with a liveness guard: a functools.partial would still be
        # invoked after the tab is deleted (PyQt only auto-disconnects plain
        # bound methods), raising RuntimeError from the worker pool drain.
        def on_finished(result):
            if sip.isdeleted(self):
                return
            if connection_generation == self._connection_generation:
                self._on_preview_loaded(request_id, result)

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error("Preview load failed after tab close: %s", exc)
                return
            if connection_generation == self._connection_generation:
                self._on_preview_error(request_id, exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    # -- Upload ----------------------------------------------------------------
    @require_connection
    def upload_document(self):
        if self._upload_active or hasattr(self.native_transfer_button, "_rmtool_background_task"):
            return
        generation = self._connection_generation
        with self.ssh_client._state_lock:
            client = self.ssh_client._client
        file_paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "选择要上传的文档", "", "文档文件 (*.pdf *.epub)",
        )
        if not file_paths:
            return

        def is_current():
            with self.ssh_client._state_lock:
                return (generation == self._connection_generation
                        and client is self.ssh_client._client)

        if not is_current():
            return
        service = NativeDocumentImporter(self.ssh_client, _rmtool.DOCUMENT_ROOT, client)
        worker = _rmtool.Worker(service.transfer_batch, file_paths)
        # Normalize before crossing Qt's signed-32-bit progress signal.
        emit_progress = worker.signals.progress.emit
        worker.kwargs["progress_callback"] = lambda sent, total: emit_progress(
            sent * 1000 // max(1, total), 1000
        )
        worker.kwargs["status_callback"] = worker.signals.status.emit

        def on_progress(current, total):
            if sip.isdeleted(self):
                return
            if is_current():
                self._update_progress_dialog(current, total)

        def on_status(text):
            if sip.isdeleted(self):
                return
            if is_current() and self._active_progress:
                self._progress_label_base = text
                self._active_progress.setLabelText(text)
                if text.startswith("等待设备"):
                    self._active_progress.setRange(0, 0)

        def on_finished(count):
            if sip.isdeleted(self):
                return
            if is_current():
                self._upload_active = False
                self.upload_button.setEnabled(self._connected)
                self.native_transfer_button.setEnabled(self._connected)
                self._on_upload_finished(count)

        def on_error(exc):
            if sip.isdeleted(self):
                logging.error("Document upload failed after tab close: %s", exc)
                return
            if is_current():
                self._upload_active = False
                self.upload_button.setEnabled(self._connected)
                self.native_transfer_button.setEnabled(self._connected)
                self._close_progress_dialog()
                self._on_error(exc)
                self.refresh()

        worker.signals.progress.connect(on_progress)
        worker.signals.status.connect(on_status)
        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._upload_active = True
        self.upload_button.setEnabled(False)
        self.native_transfer_button.setEnabled(False)
        self._show_progress_dialog("导入文档", "正在检查设备原生导入服务…")
        self.thread_pool.start(worker)

    def _on_upload_finished(self, count: int) -> None:
        self._close_progress_dialog()
        self.status_message.emit("success", f"设备已接收 {count} 个文档，无需重启；转换由系统完成。", 5000)
        self.refresh()

    @require_connection
    def _initialize_native_transfer(self):
        if self._upload_active or hasattr(self.native_transfer_button, "_rmtool_background_task"):
            return
        generation = self._connection_generation
        with self.ssh_client._state_lock:
            client = self.ssh_client._client
        if not ask_confirmation(self, _rmtool.APP_NAME,
                "将安装或更新用于自动启停原生导入服务的控制组件，保留书籍和其他插件。"
                "首次加载或更新后需要手动重启设备一次；之后上传不需要重启。是否继续？",
                confirm_text="初始化", cancel_text="取消"):
            return
        if generation != self._connection_generation:
            return

        def initialize(ssh):
            if ssh.ensure_client() is not client:
                raise RuntimeError("设备连接已变化，未初始化传输组件。")
            return native_control.initialize(ssh, str(_rmtool.app_state_dir()))

        run_in_background(self, initialize, self.ssh_client,
            on_success=self._native_transfer_initialized,
            error_prefix="原生传输初始化失败", log_message="Native import initialization failed",
            disable=(self.native_transfer_button, self.upload_button))

    def _native_transfer_initialized(self, status):
        if sip.isdeleted(self):
            return
        if status.state is native_control.NativeImportState.ENABLED:
            show_info(self, _rmtool.APP_NAME, "自动传输组件已就绪，可直接上传文档。")
            return
        if status.state is not native_control.NativeImportState.ENABLE_PENDING_REBOOT:
            show_error(self, _rmtool.APP_NAME, status.detail or "原生传输组件状态未确认，请导出诊断日志。")
            return
        self.ssh_client.close()
        self.set_connection_state(False)
        self.disconnect_requested.emit()
        show_info(self, _rmtool.APP_NAME,
            "原生传输组件已部署，SSH 已关闭。请保存文档并手动重启设备一次，"
            "解锁后重新连接 rmtool 上传；不用手动开启 USB 网页界面。")

    @require_connection
    def _remove_native_transfer(self):
        if self._upload_active or hasattr(self.native_transfer_button, "_rmtool_background_task"):
            return
        generation = self._connection_generation
        with self.ssh_client._state_lock:
            client = self.ssh_client._client
        if not ask_confirmation(self, _rmtool.APP_NAME,
                "只移除原生传输控制组件，不删除书籍、扫码传输或其他插件。"
                "完成后请手动重启设备；仍可手动开启系统 USB 网页界面进行原生上传。是否继续？",
                confirm_text="移除", cancel_text="取消", danger=True):
            return
        if generation != self._connection_generation:
            return

        def remove(ssh):
            if ssh.ensure_client() is not client:
                raise RuntimeError("设备连接已变化，未移除传输组件。")
            return native_control.uninstall(ssh, native_control._trusted_catalog())

        run_in_background(self, remove, self.ssh_client,
            on_success=self._native_transfer_removed,
            error_prefix="移除原生传输组件失败", log_message="Native import removal failed",
            disable=(self.native_transfer_button, self.upload_button))

    def _native_transfer_removed(self, status):
        if sip.isdeleted(self):
            return
        if status.detail:
            show_info(self, _rmtool.APP_NAME, status.detail)
            return
        self.ssh_client.close()
        self.set_connection_state(False)
        self.disconnect_requested.emit()
        show_info(self, _rmtool.APP_NAME, "原生传输组件已移除，SSH 已关闭；请手动重启设备。书籍及其他插件已保留。")

    # -- Delete ----------------------------------------------------------------
    @require_connection
    def _delete_document(self):
        items = self._selected_documents()
        if not items:
            show_warning(self, _rmtool.APP_NAME, "请先选择要删除的文档。")
            return
        connection_generation = self._connection_generation
        with self.ssh_client._state_lock:
            client = self.ssh_client._client
        if len(items) == 1:
            confirm_text = f'确定要删除文档「{items[0].name}」吗？此操作不可撤销。'
            progress_text = f'正在删除「{items[0].name}」…'
            success_text = "文档已删除。"
        else:
            confirm_text = f"确定要删除选中的 {len(items)} 个文档吗？此操作不可撤销。"
            progress_text = f"正在删除 {len(items)} 个文档…"
            success_text = f"已删除 {len(items)} 个文档。"
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            confirm_text,
            confirm_text="删除",
            cancel_text="取消",
            danger=True,
        ):
            return
        if (
            connection_generation != self._connection_generation
            or client is not self.ssh_client._client
            or not self.ssh_client.is_connected()
        ):
            return
        worker = _rmtool.Worker(
            self._perform_delete_documents, items, connection_generation, client
        )

        def on_finished(_result):
            if sip.isdeleted(self):
                # Worker outlived the tab; nothing safe left to update.
                return
            if connection_generation != self._connection_generation:
                return
            self._close_progress_dialog()
            self.status_message.emit("success", success_text, 3000)
            show_info(self, _rmtool.APP_NAME, success_text)
            self.refresh()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                # Worker outlived the tab; only log, touching widgets would
                # raise RuntimeError (and abort the process on macOS).
                logging.error("Document deletion failed after tab close: %s", exc)
                return
            if connection_generation != self._connection_generation:
                return
            self._close_progress_dialog()
            self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog("删除文档", progress_text)
        self.thread_pool.start(worker)

    def _perform_delete(self, item: _rmtool.DocumentItem):
        with self.ssh_client._state_lock:
            client = self.ssh_client._client
        self._perform_delete_documents([item], self._connection_generation, client)

    def _perform_delete_documents(
        self, items: List[_rmtool.DocumentItem], connection_generation: int, client
    ):
        if any(not _rmtool.is_document_identifier(item.identifier) for item in items):
            raise RuntimeError("文档标识无效，已停止删除，请刷新文档列表")
        # Hold the transport lock across validation and the entire batch so a
        # queued deletion cannot follow the wrapper onto a different device.
        with self.ssh_client.operation_session():
            if (
                connection_generation != self._connection_generation
                or client is None
                or self.ssh_client.ensure_client() is not client
            ):
                raise RuntimeError("设备连接已改变，请刷新列表后重新选择文档")
            with self.ssh_client.sftp_session() as sftp:
                names = sftp.listdir(_rmtool.DOCUMENT_ROOT)
            identifiers = {item.identifier for item in items}
            for name in names:
                if "/" in name or "\\" in name or "\x00" in name:
                    continue
                if name.split(".", 1)[0] in identifiers:
                    path = shlex.quote(posixpath.join(_rmtool.DOCUMENT_ROOT, name))
                    self.ssh_client.exec_checked(f"rm -rf -- {path}")
            self.ssh_client.exec_checked("systemctl restart xochitl")

    # -- Orphan thumbnail cleanup ---------------------------------------------
    @staticmethod
    def _format_bytes(value: int) -> str:
        size = float(value)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{value} B"

    def _set_thumbnail_cleanup_running(self, running: bool) -> None:
        self._thumbnail_cleanup_running = running
        self._update_action_state()

    @require_connection
    def _cleanup_orphan_thumbnails(self) -> None:
        if self._thumbnail_cleanup_running:
            return
        self._set_thumbnail_cleanup_running(True)
        worker = _rmtool.Worker(self._scan_orphan_thumbnails)

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                # Worker outlived the tab; only log, touching widgets would
                # raise RuntimeError (and abort the process on macOS).
                logging.error("Thumbnail scan failed after tab close: %s", exc)
                return
            self._close_progress_dialog()
            self._set_thumbnail_cleanup_running(False)
            self._on_error(exc)

        worker.signals.finished.connect(self._on_orphan_thumbnail_scan_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog("清理缩略图", "正在扫描失效缩略图…")
        self.thread_pool.start(worker)

    def _scan_orphan_thumbnails(self) -> _OrphanThumbnailScan:
        with self.ssh_client.sftp_session() as sftp:
            active_ids = {
                item.identifier for item in _rmtool.load_document_items(sftp)
            }
            entries = sftp.listdir_attr(_rmtool.DOCUMENT_ROOT)
            candidate_ids = []
            file_count = 0
            total_bytes = 0
            for entry in entries:
                if not entry.filename.endswith(".thumbnails") or not stat.S_ISDIR(
                    entry.st_mode
                ):
                    continue
                identifier = entry.filename[: -len(".thumbnails")]
                if not _rmtool.is_document_identifier(identifier) or identifier in active_ids:
                    continue
                remote_dir = posixpath.join(_rmtool.DOCUMENT_ROOT, entry.filename)
                files = self._collect_remote_files(sftp, remote_dir)
                candidate_ids.append(identifier)
                file_count += len(files)
                total_bytes += sum(size for _path, _relative, size in files)
        return _OrphanThumbnailScan(
            candidate_ids=tuple(sorted(candidate_ids)),
            file_count=file_count,
            total_bytes=total_bytes,
        )

    def _on_orphan_thumbnail_scan_finished(self, scan: _OrphanThumbnailScan) -> None:
        self._close_progress_dialog()
        if not scan.candidate_ids:
            self._set_thumbnail_cleanup_running(False)
            show_info(self, _rmtool.APP_NAME, "没有发现需要清理的失效缩略图。")
            return

        message = (
            f"发现 {scan.directory_count} 个孤立缩略图目录，"
            f"共 {scan.file_count} 个文件，占用 {self._format_bytes(scan.total_bytes)}。"
            "\n\n确定要删除这些失效缩略图吗？"
        )
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            message,
            confirm_text="删除",
            cancel_text="取消",
            danger=True,
        ):
            self._set_thumbnail_cleanup_running(False)
            return

        worker = _rmtool.Worker(self._delete_orphan_thumbnails, scan)

        def on_finished(deleted_count: int):
            if sip.isdeleted(self):
                # Worker outlived the tab; nothing safe left to update.
                return
            self._close_progress_dialog()
            self._set_thumbnail_cleanup_running(False)
            success_text = f"已删除 {deleted_count} 个失效缩略图目录。"
            self.status_message.emit("success", success_text, 3000)
            show_info(self, _rmtool.APP_NAME, success_text)

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                # Worker outlived the tab; only log, touching widgets would
                # raise RuntimeError (and abort the process on macOS).
                logging.error("Thumbnail cleanup failed after tab close: %s", exc)
                return
            self._close_progress_dialog()
            self._set_thumbnail_cleanup_running(False)
            self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog("清理缩略图", "正在删除失效缩略图…")
        self.thread_pool.start(worker)

    def _delete_orphan_thumbnails(self, scan: _OrphanThumbnailScan) -> int:
        deleted_count = 0
        with self.ssh_client.sftp_session() as sftp:
            active_ids = {
                item.identifier for item in _rmtool.load_document_items(sftp)
            }
            entries = sftp.listdir_attr(_rmtool.DOCUMENT_ROOT)
            directory_names = {
                entry.filename
                for entry in entries
                if stat.S_ISDIR(entry.st_mode)
            }
            for identifier in scan.candidate_ids:
                if not _rmtool.is_document_identifier(identifier):
                    continue
                directory_name = f"{identifier}.thumbnails"
                if identifier in active_ids or directory_name not in directory_names:
                    continue
                remote_path = posixpath.join(_rmtool.DOCUMENT_ROOT, directory_name)
                self.ssh_client.exec_checked(f"rm -rf -- {shlex.quote(remote_path)}")
                deleted_count += 1
        return deleted_count

    # -- Export as PDF (rmrl integration) --------------------------------------
    @require_connection
    def _export_as_pdf(self):
        item = self._selected_document()
        if not item:
            show_warning(self, _rmtool.APP_NAME, "请先选择要导出的文档。")
            return
        if not any(ext in item.available_assets for ext in ("rm", "note")):
            show_warning(
                self, _rmtool.APP_NAME, "该文档没有可渲染的笔记数据（需要 rm 或 note 文件）。"
            )
            return
        save_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "保存 PDF", f"{item.name}.pdf", "PDF 文件 (*.pdf)"
        )
        if not save_path:
            return
        worker = _rmtool.Worker(self._perform_export, item, save_path)

        def on_finished(_result):
            if sip.isdeleted(self):
                # Worker outlived the tab; nothing safe left to update.
                return
            self._close_progress_dialog()
            self.status_message.emit("success", f"笔记已导出为 PDF：{save_path}", 4000)
            show_info(
                self, _rmtool.APP_NAME, f"笔记已导出为 PDF：\n{save_path}"
            )

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                # Worker outlived the tab; only log, touching widgets would
                # raise RuntimeError (and abort the process on macOS).
                logging.error("PDF export failed after tab close: %s", exc)
                return
            self._close_progress_dialog()
            self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog("导出 PDF", f'正在渲染「{item.name}」…')
        self.thread_pool.start(worker)

    def _perform_export(self, item: _rmtool.DocumentItem, save_path: str):
        from rmrl import render_notebook_to_pdf

        tmpdir = tempfile.mkdtemp()
        try:
            # Download every source that may contain handwritten note strokes.
            # Newer reMarkable exports can store the pages inside .note/.zip
            # archives rather than the bare uuid directory.
            export_names = {
                item.identifier,
                f"{item.identifier}.content",
                f"{item.identifier}.note",
                f"{item.identifier}.zip",
            }
            archives: List[Path] = []
            with self.ssh_client.sftp_session() as sftp:
                entries = sftp.listdir_attr(_rmtool.DOCUMENT_ROOT)
                for entry in entries:
                    if entry.filename not in export_names:
                        continue
                    remote_path = f"{_rmtool.DOCUMENT_ROOT}/{entry.filename}"
                    local_path = os.path.join(tmpdir, entry.filename)
                    if stat.S_ISDIR(entry.st_mode):
                        self.ssh_client._download_directory_recursive(
                            sftp, remote_path, local_path
                        )
                    else:
                        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
                        sftp.get(remote_path, local_path)
                        if entry.filename.endswith((".note", ".zip")):
                            archives.append(Path(local_path))

            for archive_path in archives:
                _safe_extract_archive(archive_path, Path(tmpdir))

            render_notebook_to_pdf(tmpdir, save_path, workspace=tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # -- Preview ---------------------------------------------------------------
    def _fetch_preview_cover(self, item: _rmtool.DocumentItem) -> Optional[bytes]:
        """Download the first thumbnail (cover) using a single SFTP session."""
        with self.ssh_client.sftp_session() as sftp:
            return _rmtool.read_document_cover(sftp, item)

    def _on_preview_loaded(self, identifier: str, cover: Optional[bytes]):
        if identifier != self._current_preview_request:
            return
        if not cover:
            self._set_preview_placeholder("暂无可用预览")
            return
        self._preview_cover = cover
        image = QtGui.QImage.fromData(cover)
        if image.isNull():
            self.preview_image.clear_preview()
            self.preview_image.setText("无法解析预览图像")
        else:
            self.preview_image.setPixmap(QtGui.QPixmap.fromImage(image))
            self.preview_image.setText("")
        self._preview_stack.setCurrentIndex(1)
        self._image_btn.setChecked(True)

    def _on_preview_error(self, identifier: str, exc: Exception):
        if identifier != self._current_preview_request:
            return
        logging.warning("Preview load failed for %s: %s", identifier, exc)
        self._set_preview_placeholder("暂无可用预览")
        self.status_message.emit("warning", "文档预览加载失败，可继续查看元数据。", 3000)


    def _build_summary(self) -> Dict[str, object]:
        total = len(self.documents)
        pdf_count = sum(1 for item in self.documents if "pdf" in item.available_assets)
        epub_count = sum(1 for item in self.documents if "epub" in item.available_assets)
        note_count = sum(
            1
            for item in self.documents
            if any(ext in item.available_assets for ext in ("note", "rm"))
        )
        last_updated = next(
            (item.updated for item in self.documents if item.updated is not None),
            None,
        )
        return {
            "total": total,
            "pdf": pdf_count,
            "epub": epub_count,
            "notes": note_count,
            "lastUpdated": last_updated.strftime("%Y-%m-%d %H:%M") if last_updated else "",
        }

    def current_summary(self) -> Dict[str, object]:
        return self._build_summary()
