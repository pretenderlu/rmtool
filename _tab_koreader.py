"""KOReaderTab: KOReader 书籍文件管理页签。"""

import logging
import os
import posixpath
from datetime import datetime
from typing import Callable, List, Optional

from PyQt5 import QtCore, QtWidgets, sip

from _dialogs import ask_confirmation, show_error, show_info, show_warning
from _ssh import SSHClientWrapper, require_connection
import _koreader
import _appload
import rmtool as _rmtool  # late-bound access to avoid circular import


BOOK_FILE_FILTER = (
    "书籍文件 (*.epub *.pdf *.djvu *.mobi *.azw *.azw3 *.fb2 *.cbz *.cbt "
    "*.txt *.rtf *.doc *.docx *.html *.htm *.chm *.xps *.pdb *.md *.zip);;"
    "所有文件 (*)"
)


class KOReaderTab(QtWidgets.QWidget):
    status_message = QtCore.pyqtSignal(str, str, int)

    def __init__(self, ssh_client: SSHClientWrapper, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self.entries: List[_koreader.KOReaderEntry] = []
        self._entries_by_path = {}
        self._install_dir: Optional[str] = None
        self._library_root = ""
        self._current_dir = ""
        self._active_progress: Optional[QtWidgets.QProgressDialog] = None
        self._progress_label_base = ""
        self._connected = False
        self._loading = False
        self._loaded_once = False
        self._management_loading = False
        self._appload_status: Optional[_appload.AppLoadStatus] = None
        self._managed_status: Optional[_koreader.ManagedStatus] = None

        # --- AppLoad / KOReader installation management ---
        self.management_panel = QtWidgets.QFrame()
        self.management_panel.setObjectName("documentsListPanel")
        self.management_panel.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        management_layout = QtWidgets.QVBoxLayout(self.management_panel)
        management_layout.setContentsMargins(
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
        )
        management_layout.setSpacing(_rmtool.SUBSECTION_GAP)

        management_title = QtWidgets.QLabel("AppLoad 与 KOReader")
        management_title.setObjectName("sectionTitle")
        self.management_status_label = QtWidgets.QLabel(
            "连接设备后检测安装状态。"
        )
        self.management_status_label.setWordWrap(True)
        self.detect_management_button = QtWidgets.QPushButton("检测状态")
        self.install_appload_button = QtWidgets.QPushButton("安装 AppLoad")
        self.install_koreader_button = QtWidgets.QPushButton("安装 KOReader")
        self.install_koreader_button.setProperty("btnRole", "primary")
        self.load_official_button = QtWidgets.QPushButton("加载本地官方包…")
        self.purge_legacy_button = QtWidgets.QPushButton("彻底清理旧版残留")
        self.purge_legacy_button.setProperty("btnRole", "danger")
        self.uninstall_koreader_button = QtWidgets.QPushButton("卸载 KOReader")
        self.uninstall_koreader_button.setProperty("btnRole", "danger")
        self.uninstall_appload_button = QtWidgets.QPushButton("停用 AppLoad")
        self.uninstall_appload_button.setProperty("btnRole", "danger")

        install_actions = QtWidgets.QHBoxLayout()
        install_actions.setContentsMargins(0, 0, 0, 0)
        for button in (
            self.detect_management_button,
            self.install_appload_button,
            self.install_koreader_button,
            self.load_official_button,
        ):
            install_actions.addWidget(button)
        install_actions.addStretch()

        cleanup_actions = QtWidgets.QHBoxLayout()
        cleanup_actions.setContentsMargins(0, 0, 0, 0)
        for button in (
            self.purge_legacy_button,
            self.uninstall_koreader_button,
            self.uninstall_appload_button,
        ):
            cleanup_actions.addWidget(button)
        cleanup_actions.addStretch()

        official_links = QtWidgets.QLabel(
            '<a href="https://github.com/asivery/rm-appload/releases">'
            "AppLoad 官方发布</a>  ·  "
            '<a href="https://github.com/koreader/koreader/releases">'
            "KOReader 官方发布</a>"
        )
        official_links.setOpenExternalLinks(True)
        official_links.setWordWrap(True)
        management_layout.addWidget(management_title)
        management_layout.addWidget(self.management_status_label)
        management_layout.addLayout(install_actions)
        management_layout.addLayout(cleanup_actions)
        management_layout.addWidget(official_links)

        # --- Toolbar ---
        self.refresh_button = QtWidgets.QPushButton("刷新列表")
        self.upload_button = QtWidgets.QPushButton("上传书籍")
        self.upload_button.setProperty("btnRole", "primary")
        self.download_button = QtWidgets.QPushButton("下载到本地")
        self.delete_button = QtWidgets.QPushButton("删除")
        self.delete_button.setProperty("btnRole", "danger")
        self.new_folder_button = QtWidgets.QPushButton("新建文件夹")
        self.up_button = QtWidgets.QPushButton("上级目录")
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("搜索名称")
        self.search_edit.setClearButtonEnabled(True)
        # Never let the toolbar squeeze the search box below its placeholder.
        self.search_edit.setMinimumWidth(180)

        # --- Path bar ---
        self.path_edit = QtWidgets.QLineEdit()
        self.path_edit.setPlaceholderText("设备上的目录路径，回车跳转")

        # --- Table ---
        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["名称", "类型", "大小", "修改时间"])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        # Sorting stays disabled: the backend guarantees folders-first order,
        # and re-enabling Qt sorting would re-order rows by the header.
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)

        self.results_summary_label = QtWidgets.QLabel("显示 0 / 0 个项目")
        self.results_summary_label.setObjectName("documentsSummaryLabel")
        self.empty_state_label = QtWidgets.QLabel("连接设备后即可浏览 KOReader 书籍。")
        self.empty_state_label.setObjectName("documentsEmptyState")
        self.empty_state_label.setAlignment(QtCore.Qt.AlignCenter)
        self.empty_state_label.setWordWrap(True)
        self.empty_state_label.hide()

        top_layout = QtWidgets.QHBoxLayout()
        top_layout.addWidget(self.refresh_button)
        top_layout.addWidget(self.upload_button)
        top_layout.addWidget(self.download_button)
        top_layout.addWidget(self.delete_button)
        top_layout.addWidget(self.new_folder_button)
        top_layout.addStretch()
        top_layout.addWidget(self.search_edit)
        top_layout.addSpacing(8)

        path_layout = QtWidgets.QHBoxLayout()
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.up_button)
        path_layout.addWidget(self.path_edit, 1)

        summary_layout = QtWidgets.QHBoxLayout()
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.addWidget(self.results_summary_label)
        summary_layout.addStretch()

        self.list_panel = QtWidgets.QFrame()
        self.list_panel.setObjectName("documentsListPanel")
        self.list_panel.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        panel_layout = QtWidgets.QVBoxLayout(self.list_panel)
        panel_layout.setContentsMargins(
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
            _rmtool.PANEL_PADDING,
        )
        panel_layout.setSpacing(_rmtool.SUBSECTION_GAP)
        panel_layout.addLayout(top_layout)
        panel_layout.addLayout(path_layout)
        panel_layout.addLayout(summary_layout)
        panel_layout.addWidget(self.table)
        panel_layout.addWidget(self.empty_state_label)

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
            _rmtool.TAB_PAGE_MARGIN,
        )
        layout.setSpacing(_rmtool.PANEL_GAP)
        layout.addWidget(self.management_panel)
        layout.addWidget(self.list_panel)
        self.setLayout(layout)

        self.refresh_button.clicked.connect(self.refresh)
        self.upload_button.clicked.connect(self.upload_books)
        self.download_button.clicked.connect(self._download_books)
        self.delete_button.clicked.connect(self._delete_entries)
        self.new_folder_button.clicked.connect(self._create_folder)
        self.up_button.clicked.connect(self._go_up)
        self.path_edit.returnPressed.connect(self._on_path_submitted)
        self.search_edit.textChanged.connect(self._apply_filter)
        self.table.doubleClicked.connect(self._on_row_double_clicked)
        self.table.selectionModel().selectionChanged.connect(self._update_action_state)
        self.detect_management_button.clicked.connect(self.refresh_management)
        self.install_appload_button.clicked.connect(self._install_appload)
        self.install_koreader_button.clicked.connect(self._install_koreader)
        self.load_official_button.clicked.connect(self._load_official_package)
        self.purge_legacy_button.clicked.connect(self._purge_legacy_install)
        self.uninstall_koreader_button.clicked.connect(self._uninstall_koreader)
        self.uninstall_appload_button.clicked.connect(self._uninstall_appload)
        self.set_connection_state(False)
        self._update_results_summary()
        self._update_action_state()
        self._update_empty_state()

    # -- Search / filter -------------------------------------------------------
    def _apply_filter(self, text: str) -> None:
        text = text.strip().lower()
        for row in range(self.table.rowCount()):
            entry = self._entry_for_row(row)
            visible = bool(entry) and (not text or text in entry.name.lower())
            self.table.setRowHidden(row, not visible)

        # Never keep a hidden row selected: the user could delete or download
        # entries they can no longer see (mirrors DocumentsTab._apply_filter).
        for index in self.table.selectionModel().selectedRows():
            if self.table.isRowHidden(index.row()):
                self.table.clearSelection()
                break

        self._update_results_summary()
        self._update_action_state()
        self._update_empty_state()

    # -- Connection state ------------------------------------------------------
    def set_connection_state(self, connected: bool) -> None:
        self._connected = connected
        self.refresh_button.setEnabled(connected)
        if not connected:
            self._install_dir = None
            self._library_root = ""
            self._current_dir = ""
            self._loaded_once = False
            self.entries = []
            self._entries_by_path = {}
            self.table.setRowCount(0)
            self.path_edit.clear()
            self._appload_status = None
            self._managed_status = None
            self.management_status_label.setText("连接设备后检测安装状态。")
            self._update_results_summary()
        self._update_management_actions()
        self._update_action_state()
        self._update_empty_state()

    # -- AppLoad / KOReader management ---------------------------------------
    def _update_management_actions(self) -> None:
        app = self._appload_status
        managed = self._managed_status
        ready = self._connected and not self._management_loading and not self._loading
        self.detect_management_button.setEnabled(ready)
        if not ready or app is None or managed is None:
            for button in (
                self.install_appload_button,
                self.install_koreader_button,
                self.load_official_button,
                self.purge_legacy_button,
                self.uninstall_koreader_button,
                self.uninstall_appload_button,
            ):
                button.setEnabled(False)
            return
        app_installable = app.state in (
            _appload.AppLoadState.NOT_INSTALLED,
            _appload.AppLoadState.INSTALLED_DISABLED,
            _appload.AppLoadState.DISABLE_PENDING_REBOOT,
        )
        app_available = app.state in (
            _appload.AppLoadState.ENABLED,
            _appload.AppLoadState.ENABLE_PENDING_REBOOT,
        )
        managed_installable = managed.state in (
            _koreader.ManagedState.NOT_INSTALLED,
            _koreader.ManagedState.INSTALLED,
            _koreader.ManagedState.REPAIRABLE,
            _koreader.ManagedState.LEGACY_DATA,
            _koreader.ManagedState.EXTERNAL,
        )
        self.install_appload_button.setEnabled(app_installable)
        self.install_koreader_button.setEnabled(
            app_available and managed_installable
        )
        self.install_koreader_button.setText(
            "重新安装 KOReader"
            if managed.state == _koreader.ManagedState.INSTALLED
            else (
                "修复 KOReader"
                if managed.state == _koreader.ManagedState.REPAIRABLE
                else (
                "迁移并安装 KOReader"
                if managed.state
                in (
                    _koreader.ManagedState.LEGACY_DATA,
                    _koreader.ManagedState.EXTERNAL,
                )
                else "安装 KOReader"
                )
            )
        )
        self.load_official_button.setEnabled(
            app_installable or (app_available and managed_installable)
        )
        self.purge_legacy_button.setEnabled(
            managed.state
            in (
                _koreader.ManagedState.LEGACY_DATA,
                _koreader.ManagedState.EXTERNAL,
            )
        )
        self.uninstall_koreader_button.setEnabled(
            managed.state == _koreader.ManagedState.INSTALLED
        )
        self.uninstall_appload_button.setEnabled(
            app.state
            not in (
                _appload.AppLoadState.INCOMPATIBLE,
                _appload.AppLoadState.NOT_INSTALLED,
                _appload.AppLoadState.BROKEN,
            )
            and managed.state == _koreader.ManagedState.NOT_INSTALLED
        )

    @staticmethod
    def _app_status_text(status: _appload.AppLoadStatus) -> str:
        labels = {
            _appload.AppLoadState.INCOMPATIBLE: "不兼容",
            _appload.AppLoadState.NOT_INSTALLED: "未安装",
            _appload.AppLoadState.ENABLE_PENDING_REBOOT: "已安装，等待手动重启",
            _appload.AppLoadState.ENABLED: "已启用",
            _appload.AppLoadState.INSTALLED_DISABLED: "已停用",
            _appload.AppLoadState.DISABLE_PENDING_REBOOT: "等待手动重启后停用",
            _appload.AppLoadState.BROKEN: "状态异常",
        }
        text = labels[status.state]
        return f"{text}（{status.detail}）" if status.detail else text

    @staticmethod
    def _managed_status_text(status: _koreader.ManagedStatus) -> str:
        labels = {
            _koreader.ManagedState.INCOMPATIBLE: "不兼容",
            _koreader.ManagedState.NOT_INSTALLED: "未安装",
            _koreader.ManagedState.INSTALLED: f"已安装 {status.version}",
            _koreader.ManagedState.REPAIRABLE: "需要修复",
            _koreader.ManagedState.LEGACY_DATA: "发现旧版用户数据",
            _koreader.ManagedState.EXTERNAL: "外部安装",
            _koreader.ManagedState.BROKEN: "状态异常",
        }
        text = labels[status.state]
        return f"{text}（{status.detail}）" if status.detail else text

    def _apply_management_status(self, result) -> None:
        self._appload_status, self._managed_status = result
        self.management_status_label.setText(
            f"AppLoad：{self._app_status_text(self._appload_status)}\n"
            f"KOReader：{self._managed_status_text(self._managed_status)}\n"
            "资源均直接来自各自 GitHub 官方发布；操作不会自动重启设备。"
        )
        self._update_management_actions()

    def _read_management_status(self):
        return (
            _appload.get_status(self.ssh_client),
            _koreader.get_managed_status(self.ssh_client),
        )

    def refresh_management(self) -> None:
        if not self._connected or self._management_loading or self._loading:
            return
        self._management_loading = True
        self.management_status_label.setText("正在检测 AppLoad 与 KOReader…")
        self._update_management_actions()
        self._update_action_state()
        worker = _rmtool.Worker(self._read_management_status)

        def on_finished(result):
            if sip.isdeleted(self):
                return
            self._management_loading = False
            self._apply_management_status(result)
            self._update_action_state()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error(
                    "KOReader management refresh failed after tab close: %s", exc
                )
                return
            self._management_loading = False
            self.management_status_label.setText(f"检测失败：{exc}")
            self._update_management_actions()
            self._update_action_state()
            self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    def _run_management_operation(self, title: str, pending: str, operation) -> None:
        if self._management_loading or self._loading:
            return
        self._management_loading = True
        self._update_management_actions()
        self._update_action_state()
        worker = _rmtool.Worker(operation)

        def on_finished(_result):
            if sip.isdeleted(self):
                return
            self._management_loading = False
            self._close_progress_dialog()
            show_info(
                self,
                _rmtool.APP_NAME,
                "操作完成。请等待 SSH 操作完全结束后，从设备菜单手动重启。",
            )
            self.refresh_management()
            self._update_action_state()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error(
                    "KOReader management operation failed after tab close: %s", exc
                )
                return
            self._management_loading = False
            self._close_progress_dialog()
            self._update_management_actions()
            self._update_action_state()
            self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog(title, pending)
        self.thread_pool.start(worker)

    @require_connection
    def _install_appload(self) -> None:
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将从 AppLoad 官方 GitHub Release 下载固定校验版本，并接入 "
            "rmtool 共享 Xovi。操作不会自动重启设备，是否继续？",
            confirm_text="安装",
            cancel_text="取消",
        ):
            return
        self._run_management_operation(
            "安装 AppLoad",
            "正在下载、校验并安装 AppLoad 官方资源…",
            lambda: _appload.enable_cloud(
                self.ssh_client, str(_rmtool.app_state_dir())
            ),
        )

    @require_connection
    def _install_koreader(self) -> None:
        migrate = self._managed_status is not None and self._managed_status.state in (
            _koreader.ManagedState.LEGACY_DATA,
            _koreader.ManagedState.EXTERNAL,
        )
        message = (
            "检测到旧版 KOReader。rmtool 会完整备份原目录，只迁移设置、"
            "历史、统计、截图等用户数据，并从官方包全新安装程序主体。"
            "备份不会自动删除。是否继续？"
            if migrate
            else (
                "将从 KOReader 官方 GitHub Release 下载与设备架构匹配的版本。"
                "重新安装会保留现有用户文件，操作不会自动重启设备，是否继续？"
            )
        )
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            message,
            confirm_text="安装",
            cancel_text="取消",
        ):
            return
        self._run_management_operation(
            "安装 KOReader",
            "正在下载、校验并安装 KOReader 官方资源…",
            lambda: _koreader.install_managed_cloud(
                self.ssh_client,
                str(_rmtool.app_state_dir()),
                migrate_existing=migrate,
            ),
        )

    @require_connection
    def _load_official_package(self) -> None:
        app = self._appload_status
        managed = self._managed_status
        if app is None or managed is None:
            show_warning(self, _rmtool.APP_NAME, "请先检测安装状态。")
            return
        path, _selected = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "选择 AppLoad 或 KOReader 官方 ZIP",
            "",
            "官方 ZIP (*.zip);;所有文件 (*)",
        )
        if not path:
            return
        filename = os.path.basename(path)
        if app.asset is not None and filename == app.asset.name:
            operation = lambda: _appload.enable(
                self.ssh_client, path, str(_rmtool.app_state_dir())
            )
            title = "安装 AppLoad"
            pending = "正在校验并安装本地 AppLoad 官方包…"
        elif managed.asset is not None and filename == managed.asset.name:
            migrate = managed.state in (
                _koreader.ManagedState.LEGACY_DATA,
                _koreader.ManagedState.EXTERNAL,
            )
            operation = lambda: _koreader.install_managed(
                self.ssh_client,
                path,
                str(_rmtool.app_state_dir()),
                migrate_existing=migrate,
            )
            title = "安装 KOReader"
            pending = "正在校验并安装本地 KOReader 官方包…"
        else:
            expected = [
                item.name
                for item in (app.asset, managed.asset)
                if item is not None
            ]
            show_warning(
                self,
                _rmtool.APP_NAME,
                "文件名与当前设备所需的官方包不匹配。\n应为："
                + " 或 ".join(expected),
            )
            return
        self._run_management_operation(title, pending, operation)

    @require_connection
    def _uninstall_koreader(self) -> None:
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将移除 KOReader 启动入口，并把完整安装目录迁移到 rmtool "
            "保留区，以便下次安装恢复设置。操作不会自动重启设备，是否继续？",
            confirm_text="卸载",
            cancel_text="取消",
            danger=True,
        ):
            return
        self._run_management_operation(
            "卸载 KOReader",
            "正在卸载 KOReader 并保留用户数据…",
            lambda: _koreader.uninstall_managed(self.ssh_client),
        )

    @require_connection
    def _purge_legacy_install(self) -> None:
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "这会永久删除旧版 KOReader 目录及其中的全部内容，包括设置、"
            "历史、统计、截图，以及可能直接放在该目录内的书籍，不会创建"
            "备份。设备其他位置的书库、rmtool 共享插件和其他应用不会被"
            "删除。此操作不可撤销，是否继续？",
            confirm_text="彻底清理",
            cancel_text="取消",
            danger=True,
        ):
            return
        self._run_management_operation(
            "清理旧版 KOReader",
            "正在彻底清理旧版 KOReader 残留…",
            lambda: _koreader.purge_legacy_install(self.ssh_client),
        )

    @require_connection
    def _uninstall_appload(self) -> None:
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            "将从 rmtool 共享 Xovi 中停用 AppLoad；其他 rmtool 插件会保留。"
            "操作不会自动重启设备，是否继续？",
            confirm_text="停用",
            cancel_text="取消",
            danger=True,
        ):
            return
        self._run_management_operation(
            "停用 AppLoad",
            "正在安全停用 AppLoad…",
            lambda: _appload.disable(self.ssh_client),
        )

    def _update_action_state(self) -> None:
        available = self._connected and not self._management_loading
        self.refresh_button.setEnabled(available and not self._loading)
        ready = available and self._install_dir is not None
        selected = self._selected_entries()
        has_selection = bool(selected)
        has_file = any(not entry.is_dir for entry in selected)
        self.upload_button.setEnabled(ready)
        self.new_folder_button.setEnabled(ready)
        self.up_button.setEnabled(
            ready
            and bool(self._library_root)
            and self._current_dir != self._library_root
        )
        self.path_edit.setEnabled(ready)
        self.delete_button.setEnabled(ready and has_selection)
        self.download_button.setEnabled(ready and has_file)

    def _update_empty_state(self) -> None:
        if not self._connected:
            self.empty_state_label.setText("连接设备后即可浏览 KOReader 书籍。")
            self.empty_state_label.show()
            return
        if self._install_dir is None:
            self.empty_state_label.setText(
                "设备上未检测到 KOReader 安装。\n"
                "本页签用于管理 KOReader 的书籍文件，请先在设备上安装 KOReader。"
            )
            self.empty_state_label.show()
            return
        if not self.entries:
            self.empty_state_label.setText("当前目录为空，可以上传书籍或新建文件夹。")
            self.empty_state_label.show()
            return
        if self._visible_entry_count() == 0:
            self.empty_state_label.setText("没有匹配的项目，换个关键词试试。")
            self.empty_state_label.show()
            return
        self.empty_state_label.hide()

    # -- Selection helpers -----------------------------------------------------
    def _entry_for_row(self, row: int) -> Optional[_koreader.KOReaderEntry]:
        item = self.table.item(row, 0)
        if item is None:
            return None
        path = item.data(QtCore.Qt.UserRole)
        if not path:
            return None
        return self._entries_by_path.get(str(path))

    def _selected_entries(self) -> List[_koreader.KOReaderEntry]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        entries = []
        for row in rows:
            entry = self._entry_for_row(row)
            if entry is not None:
                entries.append(entry)
        return entries

    def _visible_entry_count(self) -> int:
        return sum(
            0 if self.table.isRowHidden(row) else 1 for row in range(self.table.rowCount())
        )

    def _update_results_summary(self) -> None:
        self.results_summary_label.setText(
            f"显示 {self._visible_entry_count()} / {len(self.entries)} 个项目"
        )

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

    # -- Refresh / navigation ----------------------------------------------------
    def refresh(self):
        if self._loading or self._management_loading:
            return
        self._loading = True
        self._update_management_actions()
        self._update_action_state()
        worker = _rmtool.Worker(self._detect_and_load, self._current_dir)

        def on_finished(result):
            if sip.isdeleted(self):
                return
            self._loading = False
            self._on_listing_loaded(result)
            self._update_management_actions()
            self._update_action_state()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error("KOReader refresh failed after tab close: %s", exc)
                return
            self._loading = False
            self._loaded_once = True
            self._update_management_actions()
            self._update_action_state()
            self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    def ensure_loaded(self) -> None:
        """Lazy initial load, called when the tab becomes visible.

        Refreshing on the ``connected`` signal would add several SSH channels
        to the burst of background tasks that starts right after connect; the
        reMarkable dropbear server has dropped connections under that load.
        Loading on first activation keeps the post-connect burst unchanged.
        """
        if not self._connected or self._loading or self._loaded_once:
            return
        self.refresh()

    def _detect_and_load(self, current_dir: str):
        install_dir = _koreader.detect_installation(self.ssh_client)
        if install_dir is None:
            return None, "", "", []
        library_root = self._library_root or _koreader.resolve_start_directory(
            self.ssh_client, install_dir
        )
        directory = current_dir or library_root
        entries = _koreader.list_directory(
            self.ssh_client, directory, library_root
        )
        return install_dir, library_root, directory, entries

    def _load_dir(self, directory: str):
        entries = _koreader.list_directory(
            self.ssh_client, directory, self._library_root
        )
        return self._install_dir, self._library_root, directory, entries

    def _on_listing_loaded(self, result):
        install_dir, library_root, directory, entries = result
        self._install_dir = install_dir
        self._library_root = library_root
        self._current_dir = directory
        self._loaded_once = True
        self.entries = entries
        self._entries_by_path = {entry.path: entry for entry in entries}
        self.path_edit.setText(directory)
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            name_item = QtWidgets.QTableWidgetItem(entry.name)
            name_item.setData(QtCore.Qt.UserRole, entry.path)
            self.table.setItem(row, 0, name_item)
            self.table.setItem(
                row, 1, QtWidgets.QTableWidgetItem("文件夹" if entry.is_dir else "文件")
            )
            size_text = "" if entry.is_dir else self._format_bytes(entry.size)
            self.table.setItem(row, 2, QtWidgets.QTableWidgetItem(size_text))
            updated_text = (
                datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M")
                if entry.mtime
                else ""
            )
            self.table.setItem(row, 3, QtWidgets.QTableWidgetItem(updated_text))
        self.table.clearSelection()
        self._apply_filter(self.search_edit.text())
        if install_dir is None:
            self.status_message.emit("warning", "设备上未检测到 KOReader 安装。", 4000)
        else:
            self.status_message.emit(
                "info", f"已加载 {len(entries)} 个项目。", 2500
            )

    @require_connection
    def _navigate_to(self, directory: str):
        directory = posixpath.normpath(directory.strip())
        if not directory.startswith("/"):
            show_warning(self, _rmtool.APP_NAME, "请输入设备上的绝对路径。")
            return
        if _koreader.is_forbidden_path(directory):
            show_warning(
                self,
                _rmtool.APP_NAME,
                "xochitl 文档目录不适用于 KOReader 书籍管理，已阻止跳转。",
            )
            self.path_edit.setText(self._current_dir)
            return
        worker = _rmtool.Worker(self._load_dir, directory)

        def on_finished(result):
            if sip.isdeleted(self):
                return
            self._on_listing_loaded(result)

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                logging.error("KOReader navigation failed after tab close: %s", exc)
                return
            self.path_edit.setText(self._current_dir)
            self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self.thread_pool.start(worker)

    def _reload_current(self) -> None:
        if self._current_dir:
            self._navigate_to(self._current_dir)

    def _go_up(self) -> None:
        if not self._current_dir or self._current_dir == self._library_root:
            return
        self._navigate_to(posixpath.dirname(self._current_dir))

    def _on_path_submitted(self) -> None:
        text = self.path_edit.text().strip()
        if text and text != self._current_dir:
            self._navigate_to(text)

    def _on_row_double_clicked(self, index) -> None:
        entry = self._entry_for_row(index.row())
        if entry is not None and entry.is_dir:
            self._navigate_to(entry.path)

    # -- Upload ------------------------------------------------------------------
    @require_connection
    def upload_books(self):
        if self._install_dir is None:
            show_warning(
                self,
                _rmtool.APP_NAME,
                "设备上未检测到 KOReader 安装，无法上传书籍。",
            )
            return
        file_paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            "选择要上传的书籍",
            "",
            BOOK_FILE_FILTER,
        )
        if not file_paths:
            return
        existing_names = {entry.name for entry in self.entries}
        conflicts = [
            os.path.basename(path)
            for path in file_paths
            if os.path.basename(path) in existing_names
        ]
        overwrite = False
        if conflicts:
            preview = "、".join(conflicts[:5])
            if len(conflicts) > 5:
                preview += f" 等 {len(conflicts)} 个文件"
            if not ask_confirmation(
                self,
                _rmtool.APP_NAME,
                f"当前目录已存在同名文件：{preview}。是否覆盖？",
                confirm_text="覆盖",
                cancel_text="取消",
            ):
                return
            overwrite = True

        count = len(file_paths)
        current_dir = self._current_dir
        library_root = self._library_root
        worker = _rmtool.Worker(
            self._perform_upload,
            file_paths,
            current_dir,
            library_root,
            overwrite,
        )
        worker.kwargs["progress_callback"] = worker.signals.progress.emit

        def on_finished(_result):
            if sip.isdeleted(self):
                # Worker outlived the tab; nothing safe left to update.
                return
            self._close_progress_dialog()
            self.status_message.emit("success", f"已上传 {count} 个文件。", 3500)
            self._reload_current()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                # Worker outlived the tab; only log, touching widgets would
                # raise RuntimeError (and abort the process on macOS).
                logging.error("KOReader upload failed after tab close: %s", exc)
                return
            self._close_progress_dialog()
            self._on_error(exc)

        worker.signals.progress.connect(self._update_progress_dialog)
        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog(
            "上传进度",
            f"正在上传 {count} 个文件…" if count > 1 else "正在上传书籍…",
        )
        self.thread_pool.start(worker)

    def _perform_upload(
        self,
        file_paths: List[str],
        remote_dir: str,
        library_root: str,
        overwrite: bool,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        sizes = [max(int(os.path.getsize(path)), 0) for path in file_paths]
        total = sum(sizes)
        completed = 0
        for path, size in zip(file_paths, sizes):
            offset = completed

            def on_file_progress(transferred: int, _total: int, offset=offset):
                if progress_callback:
                    progress_callback(offset + transferred, total)

            _koreader.upload_file(
                self.ssh_client,
                path,
                remote_dir,
                library_root,
                overwrite=overwrite,
                progress_callback=on_file_progress if progress_callback else None,
            )
            completed += size
        if progress_callback and total > 0:
            progress_callback(total, total)

    # -- Download ----------------------------------------------------------------
    @require_connection
    def _download_books(self):
        files = [entry for entry in self._selected_entries() if not entry.is_dir]
        if not files:
            show_warning(
                self, _rmtool.APP_NAME, "请选择要下载的书籍文件（文件夹暂不支持下载）。"
            )
            return
        target_dir = QtWidgets.QFileDialog.getExistingDirectory(
            self, "选择保存位置"
        )
        if not target_dir:
            return
        conflicts = [
            entry.name
            for entry in files
            if os.path.exists(os.path.join(target_dir, entry.name))
        ]
        if conflicts:
            preview = "、".join(conflicts[:5])
            if len(conflicts) > 5:
                preview += f" 等 {len(conflicts)} 个文件"
            if not ask_confirmation(
                self,
                _rmtool.APP_NAME,
                f"本地已存在同名文件：{preview}。是否覆盖？",
                confirm_text="覆盖",
                cancel_text="取消",
            ):
                return

        count = len(files)
        library_root = self._library_root
        worker = _rmtool.Worker(
            self._perform_download, files, target_dir, library_root
        )
        worker.kwargs["progress_callback"] = worker.signals.progress.emit

        def on_finished(_result):
            if sip.isdeleted(self):
                # Worker outlived the tab; nothing safe left to update.
                return
            self._close_progress_dialog()
            self.status_message.emit("success", f"已下载 {count} 个文件。", 3500)
            show_info(self, _rmtool.APP_NAME, f"已下载 {count} 个文件到：{target_dir}")

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                # Worker outlived the tab; only log, touching widgets would
                # raise RuntimeError (and abort the process on macOS).
                logging.error("KOReader download failed after tab close: %s", exc)
                return
            self._close_progress_dialog()
            self._on_error(exc)

        worker.signals.progress.connect(self._update_progress_dialog)
        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog(
            "下载进度",
            f"正在下载 {count} 个文件…" if count > 1 else "正在下载书籍…",
        )
        self.thread_pool.start(worker)

    def _perform_download(
        self,
        files: List[_koreader.KOReaderEntry],
        target_dir: str,
        library_root: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        total = sum(entry.size for entry in files)
        completed = 0
        for entry in files:
            offset = completed

            def on_file_progress(transferred: int, _total: int, offset=offset):
                if progress_callback:
                    progress_callback(offset + transferred, total)

            _koreader.download_file(
                self.ssh_client,
                entry.path,
                os.path.join(target_dir, entry.name),
                library_root,
                progress_callback=on_file_progress if progress_callback else None,
            )
            completed += entry.size
        if progress_callback and total > 0:
            progress_callback(total, total)

    # -- Delete --------------------------------------------------------------------
    @require_connection
    def _delete_entries(self):
        entries = self._selected_entries()
        if not entries:
            show_warning(self, _rmtool.APP_NAME, "请先选择要删除的项目。")
            return
        if len(entries) == 1:
            entry = entries[0]
            if entry.is_dir:
                confirm_text = (
                    f"确定要删除文件夹「{entry.name}」及其全部内容吗？此操作不可撤销。"
                )
            else:
                confirm_text = (
                    f"确定要删除「{entry.name}」吗？"
                    "其阅读进度与批注（.sdr 目录）将一并删除，此操作不可撤销。"
                )
            progress_text = f"正在删除「{entry.name}」…"
            success_text = "已删除 1 个项目。"
        else:
            confirm_text = (
                f"确定要删除选中的 {len(entries)} 个项目吗？"
                "书籍的阅读进度与批注（.sdr 目录）将一并删除，此操作不可撤销。"
            )
            progress_text = f"正在删除 {len(entries)} 个项目…"
            success_text = f"已删除 {len(entries)} 个项目。"
        if not ask_confirmation(
            self,
            _rmtool.APP_NAME,
            confirm_text,
            confirm_text="删除",
            cancel_text="取消",
            danger=True,
        ):
            return
        library_root = self._library_root
        worker = _rmtool.Worker(self._perform_delete, entries, library_root)

        def on_finished(_result):
            if sip.isdeleted(self):
                # Worker outlived the tab; nothing safe left to update.
                return
            self._close_progress_dialog()
            self.status_message.emit("success", success_text, 3000)
            self._reload_current()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                # Worker outlived the tab; only log, touching widgets would
                # raise RuntimeError (and abort the process on macOS).
                logging.error("KOReader deletion failed after tab close: %s", exc)
                return
            self._close_progress_dialog()
            self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog("删除", progress_text)
        self.thread_pool.start(worker)

    def _perform_delete(
        self, entries: List[_koreader.KOReaderEntry], library_root: str
    ) -> None:
        for entry in entries:
            _koreader.delete_entry(
                self.ssh_client, entry.path, entry.is_dir, library_root
            )

    # -- New folder ------------------------------------------------------------------
    @require_connection
    def _create_folder(self):
        if self._install_dir is None:
            show_warning(
                self,
                _rmtool.APP_NAME,
                "设备上未检测到 KOReader 安装，无法新建文件夹。",
            )
            return
        name, ok = QtWidgets.QInputDialog.getText(
            self, "新建文件夹", "文件夹名称："
        )
        name = name.strip()
        if not ok or not name:
            return
        current_dir = self._current_dir
        library_root = self._library_root
        worker = _rmtool.Worker(
            self._perform_create_folder, current_dir, library_root, name
        )

        def on_finished(_result):
            if sip.isdeleted(self):
                # Worker outlived the tab; nothing safe left to update.
                return
            self._close_progress_dialog()
            self.status_message.emit("success", f"已创建文件夹「{name}」。", 3000)
            self._reload_current()

        def on_error(exc: Exception):
            if sip.isdeleted(self):
                # Worker outlived the tab; only log, touching widgets would
                # raise RuntimeError (and abort the process on macOS).
                logging.error("KOReader mkdir failed after tab close: %s", exc)
                return
            self._close_progress_dialog()
            self._on_error(exc)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        self._show_progress_dialog("新建文件夹", f"正在创建「{name}」…")
        self.thread_pool.start(worker)

    def _perform_create_folder(
        self, remote_dir: str, library_root: str, name: str
    ) -> None:
        _koreader.create_folder(self.ssh_client, remote_dir, name, library_root)

    # -- Misc -------------------------------------------------------------------------
    @staticmethod
    def _format_bytes(value: int) -> str:
        size = float(value)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{value} B"

    def _on_error(self, exc: Exception):
        show_error(self, _rmtool.APP_NAME, f"操作失败：{exc}")
