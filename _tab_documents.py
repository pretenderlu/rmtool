"""DocumentsTab extracted from rmtool.py."""

import json
import logging
import os
import posixpath
import shutil
import stat
import tempfile
import uuid
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import paramiko
from PIL import Image
from PyQt5 import QtCore, QtGui, QtWidgets

from _ssh import SSHClientWrapper, require_connection
import rmtool as _rmtool  # late-bound access to avoid circular import


class DocumentsTab(QtWidgets.QWidget):
    summary_changed = QtCore.pyqtSignal(dict)
    status_message = QtCore.pyqtSignal(str, str, int)

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

        # --- Toolbar ---
        self.refresh_button = QtWidgets.QPushButton("刷新列表")
        self.refresh_button.setProperty("cssClass", "secondary")
        self.upload_button = QtWidgets.QPushButton("上传文档")
        self.delete_button = QtWidgets.QPushButton("删除文档")
        self.delete_button.setProperty("cssClass", "danger")
        self.export_button = QtWidgets.QPushButton("导出为 PDF")
        self.export_button.setToolTip("将笔记渲染并导出为 PDF 文件（需要 rm/note 数据）")
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("搜索文档名称")
        self.search_edit.setPlaceholderText("🔍 搜索文档…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setPlaceholderText("搜索文档名称")

        self.preview = QtWidgets.QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview_image = _rmtool.PreviewImageLabel("暂无预览")
        self.preview_image.setWordWrap(True)
        self.preview_image.set_corner_radius(_rmtool.INNER_PANEL_RADIUS)

        self.table = QtWidgets.QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["名称", "类型", "更新时间"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
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
        self._meta_btn.setProperty("cssClass", "secondary")
        self._meta_btn.setCheckable(True)
        self._meta_btn.setChecked(True)
        self._image_btn = QtWidgets.QPushButton("图像预览")
        self._image_btn.setProperty("cssClass", "secondary")
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
        self.content_splitter.setStretchFactor(0, 1)
        self.content_splitter.setStretchFactor(1, 1)
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
        self._connected = connected
        self.refresh_button.setEnabled(connected)
        self.upload_button.setEnabled(connected)
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
        return self._document_for_row(indexes[0].row())

    def _visible_document_count(self) -> int:
        return sum(0 if self.table.isRowHidden(row) else 1 for row in range(self.table.rowCount()))

    def _update_results_summary(self) -> None:
        visible = self._visible_document_count()
        self.results_summary_label.setText(f"显示 {visible} / {len(self.documents)} 个文档")

    def _update_action_state(self) -> None:
        selected = self._selected_document()
        has_selection = selected is not None
        self.delete_button.setEnabled(self._connected and has_selection)
        self.export_button.setEnabled(
            self._connected
            and selected is not None
            and any(ext in selected.available_assets for ext in ("rm", "note"))
        )

        if selected is None:
            self.selection_summary_label.setText("未选择文档")
        else:
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
        try:
            entries = sftp.listdir_attr(remote_dir)
        except IOError:
            return files
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
        worker = _rmtool.Worker(self._load_documents)
        worker.signals.finished.connect(self._on_documents_loaded)
        worker.signals.error.connect(self._on_error)
        self.thread_pool.start(worker)

    def _load_documents(self) -> List[_rmtool.DocumentItem]:
        """Load document list using a single SFTP session for efficiency."""
        items: List[_rmtool.DocumentItem] = []
        with self.ssh_client.sftp_session() as sftp:
            try:
                entries = sftp.listdir_attr(_rmtool.DOCUMENT_ROOT)
            except IOError:
                return items

            filenames = {e.filename for e in entries}
            metadata_files = [e for e in entries if e.filename.endswith(".metadata")]
            for entry in metadata_files:
                identifier = entry.filename[:-9]
                metadata_path = f"{_rmtool.DOCUMENT_ROOT}/{entry.filename}"
                try:
                    with sftp.open(metadata_path, "r") as fh:
                        metadata = json.load(fh)
                except Exception:
                    metadata = {}
                visible_name = metadata.get("visibleName", identifier)
                doc_type = metadata.get("type", "document")
                available_assets = []
                for ext in ("pdf", "epub", "zip", "note"):
                    remote_name = f"{identifier}.{ext}"
                    if remote_name in filenames:
                        available_assets.append(ext)
                # .rm page files live inside the bare identifier directory,
                # not as a top-level uuid.rm file.
                if identifier in filenames:
                    available_assets.append("rm")
                updated = None
                if entry.st_mtime:
                    updated = datetime.fromtimestamp(entry.st_mtime)
                items.append(_rmtool.DocumentItem(identifier, visible_name, doc_type, updated, available_assets))
        items.sort(key=lambda item: item.updated or datetime.min, reverse=True)
        return items

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
        QtWidgets.QMessageBox.critical(self, _rmtool.APP_NAME, f"操作失败：{exc}")

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
        worker = _rmtool.Worker(self._fetch_preview_cover, item)
        worker.signals.finished.connect(partial(self._on_preview_loaded, item.identifier))
        worker.signals.error.connect(partial(self._on_preview_error, item.identifier))
        self.thread_pool.start(worker)

    # -- Upload ----------------------------------------------------------------
    def upload_document(self):
        file_paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "选择要上传的文档",
            "",
            "文档文件 (*.pdf *.epub)",
        )
        if not file_paths:
            return

        count = len(file_paths)
        worker = _rmtool.Worker(self._transfer_documents_batch, file_paths)
        worker.kwargs["progress_callback"] = worker.signals.progress.emit

        def on_finished(_result):
            self._close_progress_dialog()
            self.status_message.emit("success", f"已上传 {count} 个文档，正在刷新列表。", 3500)
            QtWidgets.QMessageBox.information(
                self, _rmtool.APP_NAME, f"已上传 {count} 个文档，正在刷新列表。"
            )
            self.refresh()

        def on_error(exc: Exception):
            self._close_progress_dialog()
            self._on_error(exc)

        worker.signals.progress.connect(self._update_progress_dialog)
        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog(
            "上传进度",
            f"正在上传 {count} 个文档…" if count > 1 else "正在上传文档…",
        )
        self.thread_pool.start(worker)

    def _transfer_documents_batch(
        self,
        file_paths: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ):
        total = len(file_paths)
        for i, file_path in enumerate(file_paths):
            if progress_callback:
                progress_callback(i, total)
            self._transfer_document(file_path)
        if progress_callback:
            progress_callback(total, total)

    # -- Delete ----------------------------------------------------------------
    @require_connection
    def _delete_document(self):
        item = self._selected_document()
        if not item:
            QtWidgets.QMessageBox.warning(self, _rmtool.APP_NAME, "请先选择要删除的文档。")
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            _rmtool.APP_NAME,
            f'确定要删除文档「{item.name}」吗？此操作不可撤销。',
        )
        if confirm != QtWidgets.QMessageBox.Yes:
            return
        worker = _rmtool.Worker(self._perform_delete, item)

        def on_finished(_result):
            self._close_progress_dialog()
            self.status_message.emit("success", "文档已删除。", 3000)
            QtWidgets.QMessageBox.information(self, _rmtool.APP_NAME, "文档已删除。")
            self.refresh()

        def on_error(exc: Exception):
            self._close_progress_dialog()
            self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog("删除文档", f'正在删除「{item.name}」…')
        self.thread_pool.start(worker)

    def _perform_delete(self, item: _rmtool.DocumentItem):
        # Remove all files belonging to this document (uuid.* and uuid directory)
        self.ssh_client.exec_checked(
            f"rm -rf {_rmtool.DOCUMENT_ROOT}/{item.identifier} {_rmtool.DOCUMENT_ROOT}/{item.identifier}.*"
        )
        self.ssh_client.exec_checked("systemctl restart xochitl")

    # -- Export as PDF (rmrl integration) --------------------------------------
    @require_connection
    def _export_as_pdf(self):
        item = self._selected_document()
        if not item:
            QtWidgets.QMessageBox.warning(self, _rmtool.APP_NAME, "请先选择要导出的文档。")
            return
        if not any(ext in item.available_assets for ext in ("rm", "note")):
            QtWidgets.QMessageBox.warning(
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
            self._close_progress_dialog()
            self.status_message.emit("success", f"笔记已导出为 PDF：{save_path}", 4000)
            QtWidgets.QMessageBox.information(
                self, _rmtool.APP_NAME, f"笔记已导出为 PDF：\n{save_path}"
            )

        def on_error(exc: Exception):
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
            # Only download files needed for rendering: the content directory
            # (contains .rm pages) and the .content metadata file.  Skip
            # thumbnails, cache, and the original pdf/epub to save bandwidth.
            _EXPORT_SUFFIXES = (
                item.identifier,           # bare uuid directory (contains .rm pages)
                f"{item.identifier}.content",
            )
            with self.ssh_client.sftp_session() as sftp:
                entries = sftp.listdir_attr(_rmtool.DOCUMENT_ROOT)
                for entry in entries:
                    if entry.filename not in _EXPORT_SUFFIXES:
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

            render_notebook_to_pdf(tmpdir, save_path, workspace=tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # -- Preview ---------------------------------------------------------------
    def _fetch_preview_cover(self, item: _rmtool.DocumentItem) -> Optional[bytes]:
        """Download the first thumbnail (cover) using a single SFTP session."""
        thumbnail_dir = f"{_rmtool.DOCUMENT_ROOT}/{item.identifier}.thumbnails"
        with self.ssh_client.sftp_session() as sftp:
            try:
                entries = sftp.listdir_attr(thumbnail_dir)
            except IOError:
                return None

            image_entries = [
                e for e in entries
                if e.filename.lower().endswith((".png", ".jpg", ".jpeg", ".thumbnail"))
            ]
            if not image_entries:
                return None
            image_entries.sort(key=lambda e: e.filename)

            remote_path = f"{thumbnail_dir}/{image_entries[0].filename}"
            try:
                with sftp.open(remote_path, "rb") as fh:
                    return fh.read() or None
            except IOError:
                return None

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

    def _on_preview_error(self, identifier: str, exc: Exception):
        if identifier != self._current_preview_request:
            return
        logging.warning("Preview load failed for %s: %s", identifier, exc)
        self._set_preview_placeholder("暂无可用预览")
        self.status_message.emit("warning", "文档预览加载失败，可继续查看元数据。", 3000)

    # -- Document transfer -----------------------------------------------------
    def _transfer_document(
        self, file_path: str, progress_callback: Optional[Callable[[int, int], None]] = None
    ):
        extension = os.path.splitext(file_path)[1][1:].lower()
        if extension not in {"pdf", "epub"}:
            raise RuntimeError("仅支持上传 PDF 或 EPUB 文件")

        tmpdir = tempfile.mkdtemp()
        uuid_value = str(uuid.uuid4()).lower()
        try:
            shutil.copy(file_path, os.path.join(tmpdir, f"{uuid_value}.{extension}"))

            metadata = {
                "deleted": False,
                "lastModified": f"{int(datetime.now().timestamp())}000",
                "metadatamodified": False,
                "modified": False,
                "parent": "",
                "pinned": False,
                "synced": False,
                "type": "DocumentType",
                "version": 1,
                "visibleName": os.path.splitext(os.path.basename(file_path))[0],
            }
            with open(os.path.join(tmpdir, f"{uuid_value}.metadata"), "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=4, ensure_ascii=False)

            content: Dict[str, object]
            if extension == "pdf":
                content = {
                    "extraMetadata": {},
                    "fileType": "pdf",
                    "fontName": "",
                    "lastOpenedPage": 0,
                    "lineHeight": -1,
                    "margins": 100,
                    "pageCount": _rmtool.pdf_page_count(file_path),
                    "textScale": 1,
                    "transform": {
                        "m11": 1,
                        "m12": 1,
                        "m13": 1,
                        "m21": 1,
                        "m22": 1,
                        "m23": 1,
                        "m31": 1,
                        "m32": 1,
                        "m33": 1,
                    },
                }
                with open(os.path.join(tmpdir, f"{uuid_value}.content"), "w", encoding="utf-8") as f:
                    json.dump(content, f, indent=4)
                os.makedirs(os.path.join(tmpdir, f"{uuid_value}.cache"), exist_ok=True)
                os.makedirs(os.path.join(tmpdir, f"{uuid_value}.highlights"), exist_ok=True)
                os.makedirs(os.path.join(tmpdir, f"{uuid_value}.thumbnails"), exist_ok=True)
            else:  # epub
                content = {"fileType": "epub"}
                with open(os.path.join(tmpdir, f"{uuid_value}.content"), "w", encoding="utf-8") as f:
                    json.dump(content, f, indent=4)

            with self.ssh_client.sftp_session() as sftp:
                remote_dirs = set()
                files_to_upload: List[Tuple[str, str, int]] = []
                for root, _dirs, files in os.walk(tmpdir):
                    rel_dir = os.path.relpath(root, tmpdir)
                    if rel_dir == ".":
                        remote_dir = _rmtool.DOCUMENT_ROOT
                    else:
                        remote_dir = posixpath.join(
                            _rmtool.DOCUMENT_ROOT,
                            rel_dir.replace(os.sep, "/"),
                        )
                        remote_dirs.add(remote_dir)
                    for name in files:
                        local_path = os.path.join(root, name)
                        remote_path = posixpath.join(remote_dir, name)
                        file_size = os.path.getsize(local_path)
                        files_to_upload.append((local_path, remote_path, file_size))

                for remote_dir in sorted(remote_dirs, key=lambda value: value.count("/")):
                    try:
                        sftp.stat(remote_dir)
                    except IOError:
                        sftp.mkdir(remote_dir)

                total_size = sum(size for _local, _remote, size in files_to_upload)
                total_for_progress = total_size if total_size > 0 else 1
                if progress_callback:
                    progress_callback(0, total_for_progress)

                uploaded = 0
                for local_path, remote_path, size in files_to_upload:
                    offset = uploaded

                    def _put_callback(transferred, _total, offset=offset, size=size):
                        if progress_callback:
                            progress_callback(
                                offset + min(transferred, size), total_for_progress
                            )

                    sftp.put(local_path, remote_path, callback=_put_callback)
                    uploaded += size
                    if progress_callback:
                        progress_callback(uploaded, total_for_progress)

            if progress_callback:
                progress_callback(total_for_progress, total_for_progress)

            self.ssh_client.exec_checked("systemctl restart xochitl")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

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

