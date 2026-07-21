"""DashboardTab: native Qt Widgets dashboard page.

Replaces the former QWebEngineView-based dashboard (``web/dashboard.html``).
The visual design mirrors the step-3 web dashboard -- flat cards, 1px
translucent borders, large solid metrics with muted captions, a tinted
status badge -- but is rendered entirely with widgets styled through the
global QSS in ``_styles.py`` (tokens from ``_tokens.py``), so theme
switching needs no per-page handling.
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from PyQt5 import QtCore, QtWidgets


_DEVICE_FIELDS = (
    ("name", "名称"),
    ("type", "型号"),
    ("mode", "连接方式"),
    ("host", "地址"),
    ("updated", "状态更新时间"),
)

_DOC_METRICS = (
    ("total", "总文档数"),
    ("pdf", "PDF"),
    ("epub", "EPUB"),
    ("notes", "笔记/NOTE"),
)


def _mode_label(mode: str) -> str:
    return {"wifi": "Wi-Fi", "usb": "USB"}.get(mode, mode)


class DashboardTab(QtWidgets.QWidget):
    """Connection status, device info, document stats and next-step hints.

    Public interface is unchanged from the web-based version:
    ``update_device``, ``update_connection``, ``update_documents`` and
    ``set_theme``.  Styling follows the global QSS, so ``set_theme`` only
    records the theme for compatibility.
    """

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self._theme = "dark"
        self._connected = False
        self._device: Dict[str, str] = {"name": "", "type": "", "mode": "", "host": ""}
        self._documents: Dict[str, object] = {
            "total": 0,
            "pdf": 0,
            "epub": 0,
            "notes": 0,
            "lastUpdated": "",
        }
        self._last_connection_change = ""

        content = QtWidgets.QWidget()
        content.setObjectName("dashboardPage")
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(24, 24, 24, 24)
        content_layout.setSpacing(16)

        # -- Hero: headline + connection badge --
        hero = QtWidgets.QHBoxLayout()
        hero.setSpacing(24)
        headline = QtWidgets.QVBoxLayout()
        headline.setSpacing(8)
        title = QtWidgets.QLabel("reMarkable 控制中心")
        title.setObjectName("dashboardTitle")
        subtitle = QtWidgets.QLabel(
            "集中查看连接状态、设备信息与文档概览，获取下一步操作建议。"
        )
        subtitle.setObjectName("dashboardSubtitle")
        subtitle.setWordWrap(True)
        headline.addWidget(title)
        headline.addWidget(subtitle)
        hero.addLayout(headline, 1)
        self.status_badge = QtWidgets.QLabel()
        self.status_badge.setObjectName("dashboardStatusBadge")
        hero.addWidget(
            self.status_badge, 0, QtCore.Qt.AlignTop | QtCore.Qt.AlignRight
        )
        content_layout.addLayout(hero)

        # -- Cards row: current device + document overview --
        cards = QtWidgets.QHBoxLayout()
        cards.setSpacing(16)
        cards.addWidget(self._build_device_card(), 1)
        cards.addWidget(self._build_documents_card(), 1)
        content_layout.addLayout(cards)

        # -- Next-step suggestions --
        content_layout.addWidget(self._build_tips_card())
        content_layout.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(content)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

        self._apply_state()

    # ------------------------------------------------------------------
    # Card builders
    # ------------------------------------------------------------------
    def _make_field_cell(
        self, caption: str
    ) -> Tuple[QtWidgets.QFrame, QtWidgets.QLabel]:
        cell = QtWidgets.QFrame()
        cell.setObjectName("dashboardFieldCell")
        cell_layout = QtWidgets.QVBoxLayout(cell)
        cell_layout.setContentsMargins(14, 10, 14, 10)
        cell_layout.setSpacing(6)
        caption_label = QtWidgets.QLabel(caption)
        caption_label.setObjectName("dashboardFieldLabel")
        value_label = QtWidgets.QLabel("—")
        value_label.setObjectName("dashboardFieldValue")
        cell_layout.addWidget(caption_label)
        cell_layout.addWidget(value_label)
        return cell, value_label

    def _build_device_card(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("当前设备")
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(12)
        self._device_value_labels: Dict[str, QtWidgets.QLabel] = {}
        for index, (key, caption) in enumerate(_DEVICE_FIELDS):
            cell, value_label = self._make_field_cell(caption)
            self._device_value_labels[key] = value_label
            grid.addWidget(cell, index // 3, index % 3)
        group.setLayout(grid)
        return group

    def _build_documents_card(self) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox("文档概览")
        card_layout = QtWidgets.QVBoxLayout()
        card_layout.setSpacing(12)

        counts = QtWidgets.QHBoxLayout()
        counts.setSpacing(12)
        self._doc_value_labels: Dict[str, QtWidgets.QLabel] = {}
        for key, caption in _DOC_METRICS:
            cell = QtWidgets.QFrame()
            cell.setObjectName("dashboardMetricCell")
            cell_layout = QtWidgets.QVBoxLayout(cell)
            cell_layout.setContentsMargins(12, 14, 12, 14)
            cell_layout.setSpacing(4)
            metric_label = QtWidgets.QLabel("0")
            metric_label.setObjectName("dashboardMetric")
            metric_label.setAlignment(QtCore.Qt.AlignCenter)
            caption_label = QtWidgets.QLabel(caption)
            caption_label.setObjectName("dashboardMetricLabel")
            caption_label.setAlignment(QtCore.Qt.AlignCenter)
            cell_layout.addWidget(metric_label)
            cell_layout.addWidget(caption_label)
            self._doc_value_labels[key] = metric_label
            counts.addWidget(cell, 1)
        card_layout.addLayout(counts)

        self.doc_updated_label = QtWidgets.QLabel()
        self.doc_updated_label.setObjectName("dashboardDocUpdated")
        card_layout.addWidget(self.doc_updated_label)
        card_layout.addStretch(1)
        group.setLayout(card_layout)
        return group

    def _build_tips_card(self) -> QtWidgets.QFrame:
        card = QtWidgets.QFrame()
        card.setObjectName("dashboardTipsCard")
        card_layout = QtWidgets.QVBoxLayout(card)
        card_layout.setContentsMargins(24, 20, 24, 20)
        card_layout.setSpacing(12)
        title = QtWidgets.QLabel("下一步建议")
        title.setObjectName("dashboardTipsTitle")
        self.tips_body = QtWidgets.QLabel()
        self.tips_body.setObjectName("dashboardTipsBody")
        self.tips_body.setWordWrap(True)
        card_layout.addWidget(title)
        card_layout.addWidget(self.tips_body)
        return card

    # ------------------------------------------------------------------
    # Public interface (unchanged from the web-based dashboard)
    # ------------------------------------------------------------------
    def update_device(self, device: Dict):
        self._device = {
            "name": device.get("name", ""),
            "type": device.get("type", ""),
            "mode": device.get("mode", ""),
            "host": device.get("host", ""),
        }
        self._apply_state()

    def update_connection(self, connected: bool, device: Optional[Dict] = None):
        if device:
            self._device = {
                "name": device.get("name", ""),
                "type": device.get("type", ""),
                "mode": device.get("mode", ""),
                "host": device.get("host", ""),
            }
        self._connected = connected
        self._last_connection_change = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._apply_state()

    def update_documents(self, summary: Dict[str, object]):
        self._documents.update(summary)
        self._apply_state()

    def set_theme(self, theme: str):
        """Record the theme; the widgets follow the global QSS automatically."""
        self._theme = theme

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _apply_state(self):
        self.status_badge.setText("已连接" if self._connected else "未连接")
        self.status_badge.setProperty("connected", self._connected)
        self.status_badge.style().unpolish(self.status_badge)
        self.status_badge.style().polish(self.status_badge)

        values = {
            "name": self._device["name"],
            "type": self._device["type"],
            "mode": _mode_label(self._device["mode"]),
            "host": self._device["host"],
            "updated": self._last_connection_change,
        }
        for key, value_label in self._device_value_labels.items():
            value_label.setText(values.get(key) or "—")

        for key, metric_label in self._doc_value_labels.items():
            metric_label.setText(str(self._documents.get(key) or 0))
        updated = self._documents.get("lastUpdated") or "—"
        self.doc_updated_label.setText(f"最近更新：{updated}")

        self.tips_body.setText("\n".join(f"• {item}" for item in self._next_actions()))

    def _next_actions(self) -> List[str]:
        total = self._documents.get("total") or 0
        if not self._connected:
            return [
                "请在左侧连接面板中选择设备并输入 root 密码以建立连接。",
                "连接成功后可一键刷新文档、上传字体以及壁纸。",
            ]
        if not total:
            actions = ["当前设备暂无文档，可在“文档中心”页上传 PDF 或 EPUB 文件。"]
        else:
            actions = ["在“文档中心”页查看文档详情与缩略图预览，必要时继续上传新文件。"]
        actions.append("在“壁纸管理”页选择图片，系统会按设备分辨率自动裁剪并提供预览。")
        return actions
