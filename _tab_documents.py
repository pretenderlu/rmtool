"""DocumentsTab extracted from rmtool.py."""

import json
import logging
import os
import posixpath
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import paramiko
from PIL import Image
from PyQt5 import QtCore, QtGui, QtWidgets

from _dialogs import ask_confirmation, show_error, show_info, show_warning
from _ssh import SSHClientWrapper, require_connection
import rmtool as _rmtool  # late-bound access to avoid circular import


@dataclass
class _PreparedDocumentUpload:
    identifier: str
    tmpdir: str
    remote_dirs: List[str]
    files: List[Tuple[str, str, int]]

    @property
    def total_size(self) -> int:
        return sum(size for _local, _remote, size in self.files)


class _DocumentTransferService:
    def __init__(
        self,
        ssh_client: SSHClientWrapper,
        document_root: str,
        pdf_page_count: Callable[[str], int],
    ):
        self.ssh_client = ssh_client
        self.document_root = document_root.rstrip("/")
        self.pdf_page_count = pdf_page_count

    def transfer_one(
        self,
        file_path: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        self.transfer_batch([file_path], progress_callback=progress_callback)

    def transfer_batch(
        self,
        file_paths: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        packages: List[_PreparedDocumentUpload] = []
        started_identifiers: List[str] = []
        try:
            packages = [self._prepare_upload(file_path) for file_path in file_paths]
            total_size = sum(package.total_size for package in packages)
            self._ensure_device_space(total_size)
            self._upload_packages(packages, started_identifiers, progress_callback)
        except Exception:
            if started_identifiers:
                self._cleanup_remote_uploads(started_identifiers)
            raise
        finally:
            for package in packages:
                shutil.rmtree(package.tmpdir, ignore_errors=True)

    def _prepare_upload(self, file_path: str) -> _PreparedDocumentUpload:
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
                    "pageCount": self.pdf_page_count(file_path),
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
            else:
                content = {"fileType": "epub"}
                with open(os.path.join(tmpdir, f"{uuid_value}.content"), "w", encoding="utf-8") as f:
                    json.dump(content, f, indent=4)

            remote_dirs = set()
            files_to_upload: List[Tuple[str, str, int]] = []
            for root, _dirs, files in os.walk(tmpdir):
                rel_dir = os.path.relpath(root, tmpdir)
                if rel_dir == ".":
                    remote_dir = self.document_root
                else:
                    remote_dir = posixpath.join(
                        self.document_root,
                        rel_dir.replace(os.sep, "/"),
                    )
                    remote_dirs.add(remote_dir)
                for name in files:
                    local_path = os.path.join(root, name)
                    remote_path = posixpath.join(remote_dir, name)
                    file_size = os.path.getsize(local_path)
                    files_to_upload.append((local_path, remote_path, file_size))

            return _PreparedDocumentUpload(
                identifier=uuid_value,
                tmpdir=tmpdir,
                remote_dirs=sorted(remote_dirs, key=lambda value: value.count("/")),
                files=files_to_upload,
            )
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise

    def _ensure_device_space(self, total_size: int) -> None:
        if total_size <= 0:
            return
        available = self._device_available_bytes()
        if available < total_size:
            needed_mb = total_size / (1024 * 1024)
            available_mb = available / (1024 * 1024)
            raise RuntimeError(
                f"设备剩余空间不足：需要约 {needed_mb:.1f} MB，可用约 {available_mb:.1f} MB"
            )

    def _device_available_bytes(self) -> int:
        output = self.ssh_client.exec_checked(f"df -Pk {self.document_root}")
        for line in reversed(output.splitlines()):
            parts = line.split()
            if len(parts) >= 4 and parts[3].isdigit():
                return int(parts[3]) * 1024
        numeric_tokens = [token for token in output.split() if token.isdigit()]
        if numeric_tokens:
            return int(numeric_tokens[-1]) * 1024
        raise RuntimeError("无法读取设备剩余空间")

    def _upload_packages(
        self,
        packages: List[_PreparedDocumentUpload],
        started_identifiers: List[str],
        progress_callback: Optional[Callable[[int, int], None]],
    ) -> None:
        total_size = sum(package.total_size for package in packages)
        total_for_progress = total_size if total_size > 0 else 1
        uploaded = 0
        if progress_callback:
            progress_callback(0, total_for_progress)

        with self.ssh_client.sftp_session() as sftp:
            for package in packages:
                started_identifiers.append(package.identifier)
                for remote_dir in package.remote_dirs:
                    try:
                        sftp.stat(remote_dir)
                    except IOError:
                        sftp.mkdir(remote_dir)

                for local_path, remote_path, size in package.files:
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

    def _cleanup_remote_uploads(self, identifiers: List[str]) -> None:
        for identifier in identifiers:
            try:
                self.ssh_client.exec_checked(
                    f"rm -rf {self.document_root}/{identifier} {self.document_root}/{identifier}.*"
                )
            except Exception:
                logging.exception("Failed to clean partial document upload %s", identifier)


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
            self._on_upload_finished(count)

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

    def _on_upload_finished(self, count: int) -> None:
        self._close_progress_dialog()
        self.status_message.emit("success", f"已上传 {count} 个文档。", 3500)
        if self._confirm_restart_after_upload(count):
            self._start_xochitl_restart_after_upload()
            return
        self.status_message.emit(
            "warning",
            "已跳过重启；设备端可能需要重启 xochitl 后才会显示新文档。",
            5000,
        )
        self.refresh()

    def _confirm_restart_after_upload(self, count: int) -> bool:
        dialog = self._make_restart_confirmation_dialog(count)
        return dialog.exec_() == QtWidgets.QDialog.Accepted

    def _make_restart_confirmation_dialog(self, count: int) -> QtWidgets.QDialog:
        dialog = QtWidgets.QDialog(self)
        dialog.setObjectName("restartConfirmDialog")
        dialog.setWindowTitle("重启文档服务")
        dialog.setModal(True)
        dialog.setFixedWidth(500)
        dialog.setWindowFlags(
            (dialog.windowFlags() | QtCore.Qt.FramelessWindowHint)
            & ~QtCore.Qt.WindowContextHelpButtonHint
        )
        dialog.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)

        root_layout = QtWidgets.QVBoxLayout(dialog)
        root_layout.setContentsMargins(18, 18, 18, 18)

        surface = QtWidgets.QFrame()
        surface.setObjectName("restartConfirmSurface")
        surface.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        surface_layout = QtWidgets.QVBoxLayout(surface)
        surface_layout.setContentsMargins(24, 24, 24, 22)
        surface_layout.setSpacing(18)
        root_layout.addWidget(surface)

        header_layout = QtWidgets.QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(14)

        badge = QtWidgets.QLabel("↻")
        badge.setObjectName("restartConfirmBadge")
        badge.setFixedSize(46, 46)
        badge.setAlignment(QtCore.Qt.AlignCenter)
        header_layout.addWidget(badge, 0, QtCore.Qt.AlignTop)

        title_stack = QtWidgets.QVBoxLayout()
        title_stack.setContentsMargins(0, 0, 0, 0)
        title_stack.setSpacing(5)

        title = QtWidgets.QLabel("让新文档在设备上显示？")
        title.setObjectName("restartConfirmTitle")
        title.setWordWrap(True)
        title_stack.addWidget(title)

        subtitle = QtWidgets.QLabel(f"已上传 {count} 个文档")
        subtitle.setObjectName("restartConfirmSubtitle")
        title_stack.addWidget(subtitle)
        header_layout.addLayout(title_stack, 1)
        surface_layout.addLayout(header_layout)

        body = QtWidgets.QLabel(
            "需要重启 reMarkable 的文档服务 xochitl，设备端文档列表才会立即刷新。"
            "这个操作通常只需要几秒。"
        )
        body.setObjectName("restartConfirmBody")
        body.setWordWrap(True)
        surface_layout.addWidget(body)

        note = QtWidgets.QFrame()
        note.setObjectName("restartConfirmNote")
        note.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        note_layout = QtWidgets.QHBoxLayout(note)
        note_layout.setContentsMargins(14, 12, 14, 12)
        note_layout.setSpacing(10)
        note_text = QtWidgets.QLabel("稍后再说也可以；只是新上传的文档可能不会马上出现在设备端。")
        note_text.setObjectName("restartConfirmNoteText")
        note_text.setWordWrap(True)
        note_layout.addWidget(note_text)
        surface_layout.addWidget(note)

        button_row = QtWidgets.QHBoxLayout()
        button_row.setContentsMargins(0, 2, 0, 0)
        button_row.setSpacing(10)
        button_row.addStretch()

        later_button = QtWidgets.QPushButton("稍后再说")
        later_button.setObjectName("restartConfirmSecondary")
        later_button.setProperty("cssClass", "secondary")
        later_button.clicked.connect(dialog.reject)

        restart_button = QtWidgets.QPushButton("现在重启")
        restart_button.setObjectName("restartConfirmPrimary")
        restart_button.setDefault(True)
        restart_button.clicked.connect(dialog.accept)

        button_row.addWidget(later_button)
        button_row.addWidget(restart_button)
        surface_layout.addLayout(button_row)

        return dialog

    def _start_xochitl_restart_after_upload(self) -> None:
        worker = _rmtool.Worker(self._restart_xochitl)

        def on_finished(_result):
            self._close_progress_dialog()
            self.status_message.emit("success", "xochitl 已重启，正在刷新列表。", 3500)
            self.refresh()

        def on_error(exc: Exception):
            self._close_progress_dialog()
            self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog("重启文档服务", "正在重启 xochitl…")
        self.thread_pool.start(worker)

    def _restart_xochitl(self) -> None:
        self.ssh_client.exec_checked("systemctl restart xochitl")

    def _transfer_documents_batch(
        self,
        file_paths: List[str],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ):
        service = _DocumentTransferService(
            self.ssh_client,
            _rmtool.DOCUMENT_ROOT,
            _rmtool.pdf_page_count,
        )
        service.transfer_batch(file_paths, progress_callback=progress_callback)

    # -- Delete ----------------------------------------------------------------
    @require_connection
    def _delete_document(self):
        items = self._selected_documents()
        if not items:
            show_warning(self, _rmtool.APP_NAME, "请先选择要删除的文档。")
            return
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
        worker = _rmtool.Worker(self._perform_delete_documents, items)

        def on_finished(_result):
            self._close_progress_dialog()
            self.status_message.emit("success", success_text, 3000)
            show_info(self, _rmtool.APP_NAME, success_text)
            self.refresh()

        def on_error(exc: Exception):
            self._close_progress_dialog()
            self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog("删除文档", progress_text)
        self.thread_pool.start(worker)

    def _perform_delete(self, item: _rmtool.DocumentItem):
        self._perform_delete_documents([item])

    def _perform_delete_documents(self, items: List[_rmtool.DocumentItem]):
        # Remove all files belonging to this document (uuid.* and uuid directory)
        for item in items:
            self.ssh_client.exec_checked(
                f"rm -rf {_rmtool.DOCUMENT_ROOT}/{item.identifier} {_rmtool.DOCUMENT_ROOT}/{item.identifier}.*"
            )
        self.ssh_client.exec_checked("systemctl restart xochitl")

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
            self._close_progress_dialog()
            self.status_message.emit("success", f"笔记已导出为 PDF：{save_path}", 4000)
            show_info(
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
        service = _DocumentTransferService(
            self.ssh_client,
            _rmtool.DOCUMENT_ROOT,
            _rmtool.pdf_page_count,
        )
        service.transfer_one(file_path, progress_callback=progress_callback)

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
