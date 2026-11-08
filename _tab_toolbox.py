"""Toolbox page: browser over the device feature sections.

The sections themselves live in the ``_toolbox_*`` modules; their names are
re-exported here so existing ``_tab_toolbox.<Name>`` imports keep working.
"""

import logging
from contextlib import nullcontext
from typing import Dict, Optional

from PyQt5 import QtCore, QtWidgets, sip

import _native_import_control
import _reading_enhancements
import _tap_page_turn
import _toolbox_common as _ui
from _plugin_recovery import RecoveryState
from _weread_app import WeReadAppState
from _ssh import SSHClientWrapper, require_connection
import rmtool as _rmtool  # late-bound access to avoid circular import
from _toolbox_common import (  # noqa: F401 - re-exported for callers and tests
    _show_package_download_error,
    select_font_file,
    load_font_file,
    ToolboxStatusLabel,
)
from _toolbox_fonts import (  # noqa: F401 - re-exported for callers and tests
    FontTab,
    FontPage,
)
from _toolbox_device import (  # noqa: F401 - re-exported for callers and tests
    TimeTab,
    ControlTab,
)
from _toolbox_localization import (  # noqa: F401 - re-exported for callers and tests
    RmkitCnSection,
    NativeChineseSection,
    PinyinInputSection,
)
from _toolbox_reading import (  # noqa: F401 - re-exported for callers and tests
    _reading_enhancements_status,
    _install_reading_enhancements,
    _set_epub_font_with_menu_support,
    _cleanup_reading_enhancements,
    _note_enhancements_status,
    _install_note_enhancements,
    _cleanup_note_enhancements,
    _weread_launcher_status,
    _weread_combined_status,
    _install_weread_app_and_status,
    _install_weread_launcher,
    _cleanup_weread_launcher,
    ReadingEnhancementsSection,
    NoteEnhancementsSection,
    WeReadLauncherSection,
    QrUploadSection,
)
from _toolbox_tap import (  # noqa: F401 - re-exported for callers and tests
    _tap_page_turn_status,
    TapPageTurnSection,
)
from _toolbox_legacy import (  # noqa: F401 - re-exported for callers and tests
    _LEGACY_PLATFORM_LABELS,
    LegacyPluginMigrationSection,
)
from _toolbox_recovery import CapacityRecoverySection
from _toolbox_diagnostics import (  # noqa: F401 - re-exported for callers and tests
    _collect_diagnostics,
    DiagnosticPreviewDialog,
    DiagnosticsSection,
)


_PLUGIN_STATES = {
    "incompatible": ("不兼容", ""),
    "not_installed": ("未安装", "可安装"),
    "installed": ("已安装", ""),
    "installed_disabled": ("已停用", ""),
    "emergency_disabled": ("已停用", ""),
    "enabled": ("已启用", ""),
    "enable_pending_reboot": ("待重启", ""),
    "disable_pending_reboot": ("待重启", ""),
    "outdated": ("旧版", "可更新"),
    "repair_available": ("已安装", "可更新"),
    "migration_available": ("旧版", "可迁移"),
    "firmware_residue": ("固件残留", ""),
    "legacy_vellum": ("旧版", ""),
    "vellum_runtime": ("存在问题", ""),
    "broken": ("存在问题", ""),
}


def _plugin_status(status, *, action_allowed=True):
    if status is None:
        return "待检测", ""
    if getattr(status, "firmware_residue", False):
        return "固件残留", ""
    state, action = _PLUGIN_STATES.get(status.state.value, ("状态未知", ""))
    if status.state in (_tap_page_turn.TapPageTurnState.OUTDATED, _tap_page_turn.TapPageTurnState.LEGACY_VELLUM):
        return state, "可卸载" if status.dropin_present else ""
    if status.state is WeReadAppState.REPAIR_AVAILABLE:
        state, action = "存在问题", "可修复"
    return state, action if action_allowed and status.package is not None else ""


class NativeTransferStatusSection(QtWidgets.QWidget):
    open_documents_requested = QtCore.pyqtSignal()

    def __init__(self, ssh_client):
        super().__init__()
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self._status = None
        self._busy = False
        self.status_label = ToolboxStatusLabel()
        self.status_label.setTextFormat(QtCore.Qt.PlainText)
        self.status_label.setWordWrap(True)
        self.detect_button = QtWidgets.QPushButton("检测状态")
        self.documents_button = QtWidgets.QPushButton("前往文档中心")

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(_rmtool.SUBSECTION_GAP)
        buttons.addWidget(self.detect_button)
        buttons.addWidget(self.documents_button)
        buttons.addStretch()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(_rmtool.SUBSECTION_GAP)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.detect_button.clicked.connect(self._detect_status)
        self.documents_button.clicked.connect(self.open_documents_requested)
        ssh_client.connection_changed.connect(self._on_connection_changed)
        self._on_connection_changed(ssh_client.is_connected())

    def _update_buttons(self):
        connected = self.ssh_client.is_connected()
        self.detect_button.setEnabled(connected and not self._busy)
        self.documents_button.setEnabled(connected)

    def _on_connection_changed(self, connected):
        if not connected:
            self._status = None
            self.status_label.setText("设备未连接")
        elif self._status is None:
            self.status_label.setText("设备已连接，尚未检测")
        self._update_buttons()

    def _apply_status(self, status):
        self._status = status
        self.status_label.setText("\n".join(filter(None, (*_plugin_status(status), status.detail))))

    @require_connection
    def _detect_status(self):
        self._start_status_detection()

    def _start_status_detection(self, *, on_done=None, show_errors=True):
        def probe():
            return _native_import_control.get_status(self.ssh_client, _native_import_control._trusted_catalog())

        return _ui.start_plugin_worker(
            self, probe, pending="正在检测原生文档传输状态…",
            on_done=on_done, show_errors=show_errors, detection=True,
        )


class ToolboxTab(QtWidgets.QWidget):
    open_documents_requested = QtCore.pyqtSignal()

    def __init__(
        self,
        ssh_client: SSHClientWrapper,
        config: Dict,
        parent: Optional[QtWidgets.QWidget] = None,
    ):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self._detect_all_busy = False
        self._scan_prompt_handled = False
        self._scan_prompt_open = False
        self._maintenance_visit = False
        self.time_section = TimeTab(ssh_client)
        self.control_section = ControlTab(ssh_client)
        self.native_chinese_section = NativeChineseSection(ssh_client, config=config)
        self.pinyin_input_section = PinyinInputSection(ssh_client)
        self.reading_enhancements_section = ReadingEnhancementsSection(ssh_client)
        self.note_enhancements_section = NoteEnhancementsSection(ssh_client)
        self.weread_launcher_section = WeReadLauncherSection(ssh_client)
        self.qr_upload_section = QrUploadSection(ssh_client)
        self.native_transfer_section = NativeTransferStatusSection(ssh_client)
        self.native_transfer_section.open_documents_requested.connect(self.open_documents_requested)
        self.tap_page_turn_section = TapPageTurnSection(ssh_client)
        self.diagnostics_section = DiagnosticsSection(ssh_client)
        self.legacy_plugin_section = LegacyPluginMigrationSection(ssh_client)
        self.capacity_recovery_section = CapacityRecoverySection(ssh_client)
        self.capacity_recovery_section.diagnostics_handler = self.show_diagnostics
        self._detectable_sections = (
            self.native_chinese_section,
            self.pinyin_input_section,
            self.reading_enhancements_section,
            self.note_enhancements_section,
            self.weread_launcher_section,
            self.qr_upload_section,
            self.native_transfer_section,
            self.tap_page_turn_section,
            self.legacy_plugin_section,
        )
        self._device_identity = None
        self._identity_probe_running = False
        self._identity_connection_state: Optional[bool] = None
        self._connection_client = None
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
                "title": "笔记增强",
                "category": "笔记增强",
                "keywords": "笔记 书写 彩色 提笔 刷新 闪烁 延迟 Paper Pro Move",
                "section": self.note_enhancements_section,
                "status": self.note_enhancements_section.status_label,
            },
            {
                "title": "微信读书",
                "category": "阅读增强",
                "keywords": "微信读书 WeRead 启动 普通 黑白快刷 3.28",
                "section": self.weread_launcher_section,
                "status": self.weread_launcher_section.status_label,
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
                "title": "扫码传输",
                "category": "设备维护",
                "keywords": "扫码 手机 Wi-Fi PDF EPUB 传书 上传",
                "section": self.qr_upload_section,
                "status": self.qr_upload_section.status_label,
            },
            {
                "title": "原生文档传输",
                "category": "设备维护",
                "keywords": "文档中心 原生传输服务 USB Wi-Fi PDF EPUB 传书 上传",
                "section": self.native_transfer_section,
                "status": self.native_transfer_section.status_label,
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
                "title": "设备修复与清理",
                "category": "设备维护",
                "keywords": "容量 空间 修复 清理 字体 继续安装",
                "section": self.capacity_recovery_section,
                "status": self.capacity_recovery_section.status_label,
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
                "keywords": "Vellum AppLoader Xovi 卸载 残留 迁移 固件升级 修复 重装 恢复",
                "section": self.legacy_plugin_section,
                "status": self.legacy_plugin_section.status_label,
            },
            {
                "title": "共享插件恢复",
                "category": "设备维护",
                "keywords": "共享 Xovi 修复 重装 清理 事务锁",
                "section": self.legacy_plugin_section,
                "status": self.legacy_plugin_section.status_label,
                "shared_recovery": True,
            },
        )

        self.search_input = QtWidgets.QLineEdit()
        self.search_input.setObjectName("toolboxSearchInput")
        self.search_input.setPlaceholderText("搜索插件与工具")
        self.search_input.setClearButtonEnabled(True)

        self.category_combo = QtWidgets.QComboBox()
        self.category_combo.setObjectName("toolboxCategoryFilter")
        self.category_combo.addItems(
            ("全部分类", "中文与输入", "阅读增强", "笔记增强", "设备维护")
        )

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
        self._detail_pages = {}
        for row, entry in enumerate(self._tool_entries):
            section = entry["section"]
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
            lines = 3 if section is self.weread_launcher_section else 2
            self.tool_table.setRowHeight(row, max(48, lines * self.tool_table.fontMetrics().lineSpacing() + 18))
            if section not in self._detail_pages:
                title = "旧版插件迁移与共享插件恢复" if section is self.legacy_plugin_section else entry["title"]
                page = self._detail_page(title, entry["category"], section)
                self.detail_stack.addWidget(page)
                self._detail_pages[section] = page
            status_label = entry["status"]
            if status_label is not None:
                status_label.text_changed.connect(
                    lambda _text, current_row=row: self._refresh_row_status(current_row)
                )
            if section is self.weread_launcher_section:
                section.app_status_label.text_changed.connect(
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
            if section is not self.weread_launcher_section or old_title is section.title_label:
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
        if section in self._detectable_sections and section is not self.legacy_plugin_section:
            recovery_link = QtWidgets.QPushButton("查看共享插件恢复")
            recovery_link.setObjectName("pluginRecoveryLink")
            recovery_link.clicked.connect(self._show_plugin_recovery)

            def update_recovery_link(_text):
                if sip.isdeleted(self) or sip.isdeleted(recovery_link):
                    return
                status = section._status
                recovery_link.setVisible(
                    section.status_label.detection_failed or (
                        status is not None and (
                            status.state.value in ("broken", "firmware_residue")
                            or getattr(status, "firmware_residue", False)
                        )
                    )
                )

            section.status_label.text_changed.connect(update_recovery_link)
            update_recovery_link(section.status_label.text())
            content_layout.addWidget(recovery_link)
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
        if "等待完整重启" in text:
            return "待重启"
        if text.startswith("可修复并重装"):
            return "可修复"
        if text.startswith("可清理不完整状态") or "可清理不完整状态" in text:
            return "可清理"
        if text.startswith("无法安全自动修复"):
            return "已阻止"
        if text.startswith("当前固件不支持自动修复"):
            return "不支持"
        if text.startswith("无需修复"):
            return "无需修复"
        if "可直接修复更新" in text:
            return "可更新"
        if any(word in text for word in ("失败", "不完整", "被修改", "需要修复", "残留")):
            return "需处理"
        if any(word in text for word in ("可安全更新", "可安全修复", "修复并更新")):
            return "可更新"
        if "可迁移" in text:
            return "可迁移"
        if any(word in text for word in ("不兼容", "不支持", "没有精确匹配", "当前设备没有")):
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

    def show_diagnostics(self):
        """Select the diagnostics entry, clearing filters that could hide it."""
        self.search_input.clear()
        self.category_combo.setCurrentIndex(0)
        row = next(
            row for row, entry in enumerate(self._tool_entries)
            if entry["section"] is self.diagnostics_section
        )
        self.tool_table.setCurrentCell(row, 0)
        self._maintenance_visit = True

    def show_capacity_recovery(self, intent=None, error=None):
        self.search_input.clear()
        self.category_combo.setCurrentIndex(0)
        row = next(
            row for row, entry in enumerate(self._tool_entries)
            if entry["section"] is self.capacity_recovery_section
        )
        self.tool_table.setCurrentCell(row, 0)
        self._maintenance_visit = True
        if intent is not None:
            self.capacity_recovery_section.open_intent(intent, error)

    def _show_plugin_recovery(self):
        self.search_input.clear()
        self.category_combo.setCurrentIndex(0)
        row = next(
            row for row, entry in enumerate(self._tool_entries)
            if entry.get("shared_recovery")
        )
        self.tool_table.setCurrentCell(row, 0)
        self.legacy_plugin_section._start_status_detection(show_errors=False)

    def _refresh_row_status(self, row: int):
        if sip.isdeleted(self):
            return
        entry = self._tool_entries[row]
        section = entry["section"]
        status_label = entry["status"]
        full_status = status_label.text() if status_label is not None else "设备维护工具"
        status_item = self.tool_table.item(row, 1)
        if section not in self._detectable_sections:
            text = "工具"
        elif not self.ssh_client.is_connected():
            text = "未连接"
        elif section is self.legacy_plugin_section:
            if entry.get("shared_recovery"):
                text, full_status = self._shared_recovery_status(section)
            else:
                report = section._report
                if section._residue_detection_failed:
                    text = "检测失败"
                elif section._report_session is None:
                    text = "待检测"
                elif report is None:
                    text, full_status = "无固件残留", section._report_text(None)
                else:
                    text = "固件残留"
                    if report.migratable and not getattr(section._recovery_report, "operation_locked", False):
                        text += "\n可迁移"
                    full_status = section._report_text(report)
        elif section is self.weread_launcher_section:
            if status_label.detection_failed:
                text = "App 检测失败\n入口检测失败"
            else:
                app = _plugin_status(section.app_status)
                entry_status = _plugin_status(
                    section._status,
                    action_allowed=(
                        section.app_status is not None
                        and section.app_status.state is WeReadAppState.INSTALLED
                        and getattr(section._status, "prerequisite_available", True)
                    ),
                )
                text = "\n".join(filter(None, (f"App {app[0]}", app[1], f"入口 {entry_status[0]}", entry_status[1])))
                full_status = f"App：{section.app_status_label.text()}\n入口：{full_status}"
        elif status_label.detection_failed:
            text = "检测失败"
        else:
            text = "\n".join(filter(None, _plugin_status(section._status)))
        status_item.setText(text)
        status_item.setToolTip(f"{text}\n{full_status}")

    @staticmethod
    def _shared_recovery_status(section):
        if section.status_label.detection_failed:
            return "检测失败", "共享插件恢复检测失败，请重新检测。"
        report = section._recovery_report
        if report is None:
            return "待检测", "尚未检测共享插件恢复状态"
        incomplete = section._incomplete_report
        if getattr(report, "operation_locked", False):
            return "已阻止", report.detail
        if not report.can_repair and getattr(incomplete, "can_cleanup", False):
            return "可清理", incomplete.detail
        if report.state is RecoveryState.BLOCKED and (next_step := getattr(report, "next_step", "")):
            return "已阻止", "\n".join(filter(None, (report.detail, next_step)))
        if incomplete is not None and incomplete.state is RecoveryState.BLOCKED:
            return "已阻止", incomplete.detail
        return {
            RecoveryState.NOT_NEEDED: "无需修复",
            RecoveryState.REPAIR_AVAILABLE: "可修复",
            RecoveryState.CLEANUP_AVAILABLE: "可清理",
            RecoveryState.UNSUPPORTED: "不兼容",
            RecoveryState.BLOCKED: "已阻止",
        }[report.state], report.detail

    def _show_current_tool(self, current_row: int, _column: int, _old_row: int, _old_column: int):
        if current_row >= 0 and not self.tool_table.isRowHidden(current_row):
            section = self._tool_entries[current_row]["section"]
            self.detail_stack.setCurrentWidget(self._detail_pages[section])
            if self._maintenance_visit and section not in (self.diagnostics_section, self.capacity_recovery_section):
                self._maintenance_visit = False
                QtCore.QTimer.singleShot(0, self._maybe_offer_scan)
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
        client = getattr(self.ssh_client, "_client", None) if connected else None
        if connected == self._identity_connection_state and client is self._connection_client:
            return
        replaced = connected and self._identity_connection_state is True
        self._identity_connection_state = connected
        self._connection_client = client
        self._identity_connection_generation += 1
        self._identity_probe_running = False
        self._device_identity = None
        self._scan_prompt_handled = False
        self._reset_scan()
        for section in self._detectable_sections:
            if section is not self.legacy_plugin_section:
                _ui.reset_plugin_detection(section)
            if replaced:
                section._on_connection_changed(False)
                section._on_connection_changed(True)
        self._update_device_scoped_entries()
        for row in range(len(self._tool_entries)):
            self._refresh_row_status(row)
        if connected:
            self._probe_device_identity()
            if self.isVisible():
                QtCore.QTimer.singleShot(0, self._maybe_offer_scan)

    def _session_current(self, session):
        generation, client = session
        return (
            generation == self._identity_connection_generation
            and client is getattr(self.ssh_client, "_client", None)
            and self.ssh_client.is_connected()
        )

    def _session_token(self):
        return self._identity_connection_generation, self._connection_client

    def showEvent(self, event):
        super().showEvent(event)
        # Direct diagnostic and recovery navigation selects its row after showing this page.
        QtCore.QTimer.singleShot(0, self._maybe_offer_scan)

    def hideEvent(self, event):
        self._maintenance_visit = False
        super().hideEvent(event)

    def _maybe_offer_scan(self):
        if sip.isdeleted(self):
            return
        session = self._session_token()
        if (
            self._scan_prompt_handled or self._scan_prompt_open or self._maintenance_visit
            or not self.isVisible() or not self._session_current(session)
        ):
            return
        self._scan_prompt_open = True
        try:
            accepted = _ui.ask_confirmation(
                self, "检测设备插件",
                "是否检测当前设备的插件状态？检测只读取设备状态，不会安装、更新或清理插件。",
                confirm_text="检测全部插件", cancel_text="稍后",
            )
        finally:
            if not sip.isdeleted(self):
                self._scan_prompt_open = False
        if sip.isdeleted(self):
            return
        if not self._session_current(session):
            if self.isVisible() and self.ssh_client.is_connected():
                QtCore.QTimer.singleShot(0, self._maybe_offer_scan)
        elif not self._scan_prompt_handled:
            self._scan_prompt_handled = True
            if accepted and self.isVisible() and not self._maintenance_visit:
                self._detect_all_statuses()

    def _probe_device_identity(self):
        """Quietly learn the device identity to scope device-specific entries."""
        if (
            self._identity_probe_running
            or not self.ssh_client.is_connected()
            or sip.isdeleted(self)
        ):
            return
        self._identity_probe_running = True
        session = self._session_token()
        ssh = self.ssh_client

        def inspect():
            with getattr(ssh, "operation_session", nullcontext)():
                if not self._session_current(session):
                    raise RuntimeError("设备连接已变化，请重新检测。")
                return _tap_page_turn.get_device_identity(ssh)

        worker = _rmtool.Worker(inspect)

        def on_finished(identity):
            if sip.isdeleted(self):
                return
            if not self._session_current(session) or not self._identity_probe_running:
                return
            self._identity_probe_running = False
            self._device_identity = identity
            self._update_device_scoped_entries()
            if self._detect_all_busy:
                self._begin_scan(session)

        def on_error(_exc):
            if sip.isdeleted(self):
                return
            on_finished(None)

        worker.signals.finished.connect(on_finished)
        worker.signals.error.connect(on_error)
        try:
            self.thread_pool.start(worker)
        except Exception as exc:
            logging.exception("Could not start device identity probe")
            on_error(exc)

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
        self._scan_prompt_handled = True
        self._detect_all_busy = True
        self.detect_all_button.setEnabled(False)
        self.detect_all_button.setToolTip("")
        if self._identity_probe_running:
            self.detect_all_button.setText("正在识别设备…")
        else:
            self._begin_scan(self._session_token())

    def _reset_scan(self):
        self._detect_all_busy = False
        self.detect_all_button.setText("检测全部插件")
        self.detect_all_button.setEnabled(self.ssh_client.is_connected())
        self.detect_all_button.setToolTip("")

    def _begin_scan(self, session):
        steps = []
        for section in self._detectable_sections:
            if section is self.tap_page_turn_section and self._tap_entry_hidden():
                continue
            if section is self.legacy_plugin_section:
                steps.append((section, section._start_residue_detection))
            steps.append((section, section._start_status_detection))
        self._run_next_status_detection(session, steps, 0, 0, 0)

    def _run_next_status_detection(self, session, steps, index, completed, skipped):
        if sip.isdeleted(self):
            return
        if not self._detect_all_busy or not self._session_current(session):
            return
        total = len(steps)
        while index < total:
            section, start = steps[index]
            if not section._busy and not _ui.plugin_operation_busy(self.ssh_client):
                break
            index += 1
            skipped += 1
        if index == total:
            self._reset_scan()
            suffix = f"，忙碌跳过 {skipped} 项" if skipped else ""
            self.detect_all_button.setToolTip(f"已完成检测 {completed}/{total}{suffix}")
            return

        self.detect_all_button.setText(f"正在检测 {completed}/{total}")
        done_called = False
        starting = True

        def advance(ran):
            self._run_next_status_detection(
                session, steps, index + 1, completed + int(ran), skipped + int(not ran)
            )

        def on_done():
            if sip.isdeleted(self):
                return
            nonlocal done_called
            if done_called:
                return
            done_called = True
            if not starting:
                advance(True)

        worker = start(on_done=on_done, show_errors=False)
        starting = False
        if done_called:
            # A worker that loses the reservation race calls on_done but returns no worker.
            advance(worker is not None)

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
