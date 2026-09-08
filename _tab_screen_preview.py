"""Live screen preview page for verified reMarkable devices."""

from datetime import datetime
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets, sip

from _dialogs import show_error, show_info
import _screen_preview
import rmtool as _rmtool


class ScreenPreviewTab(QtWidgets.QWidget):
    status_message = QtCore.pyqtSignal(str, str, int)

    def __init__(self, ssh_client, parent=None):
        super().__init__(parent)
        self.ssh_client = ssh_client
        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._capture)
        self._connected = False
        self._page_active = False
        self._busy = False
        self._worker = None
        self._generation = 0
        self._supported = None
        self._latest_png = None

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(12)

        title = QtWidgets.QLabel("屏幕预览")
        title.setObjectName("toolboxBrowserTitle")
        root.addWidget(title)

        self.status_label = QtWidgets.QLabel("设备未连接")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        root.addWidget(self.status_label)

        controls = QtWidgets.QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        self.preview_button = QtWidgets.QPushButton("开启预览")
        self.preview_button.setCheckable(True)
        self.preview_button.setProperty("btnRole", "primary")
        self.refresh_button = QtWidgets.QPushButton("刷新画面")
        self.interval_combo = QtWidgets.QComboBox()
        for seconds in (2, 5, 10):
            self.interval_combo.addItem(f"{seconds} 秒", seconds * 1000)
        self.save_button = QtWidgets.QPushButton("另存为…")
        controls.addWidget(self.preview_button)
        controls.addWidget(self.refresh_button)
        controls.addWidget(QtWidgets.QLabel("刷新间隔"))
        controls.addWidget(self.interval_combo)
        controls.addStretch()
        controls.addWidget(self.save_button)
        root.addLayout(controls)

        self.preview = _rmtool.PreviewImageLabel("暂无画面")
        self.preview.setMinimumSize(320, 420)
        self.preview.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )
        root.addWidget(self.preview, 1)

        self.preview_button.clicked.connect(self._toggle_preview)
        self.refresh_button.clicked.connect(self._capture)
        self.interval_combo.currentIndexChanged.connect(self._interval_changed)
        self.save_button.clicked.connect(self._save_as)
        self._refresh_controls()

    def set_connection_state(self, connected: bool) -> None:
        self._connected = connected
        self._supported = None
        self._stop_preview()
        self.status_label.setText("设备已连接，等待检测" if connected else "设备未连接")
        self._refresh_controls()
        if connected and self._page_active:
            self._detect()

    def set_page_active(self, active: bool) -> None:
        self._page_active = active
        if not active:
            self._stop_preview()
        elif self._connected and self._supported is None:
            self._detect()

    def _refresh_controls(self) -> None:
        available = self._connected and not self._busy and self._supported is True
        self.preview_button.setEnabled(available)
        self.refresh_button.setEnabled(available and not self.preview_button.isChecked())
        self.interval_combo.setEnabled(available and self.preview_button.isChecked())
        self.save_button.setEnabled(self._latest_png is not None and not self._busy)

    def _start_worker(self, operation, on_success, *, error_prefix: str) -> None:
        if self._busy or not self._connected:
            return
        self._busy = True
        self._refresh_controls()
        generation = self._generation
        worker = _rmtool.Worker(operation)
        self._worker = worker

        def finished(result):
            if sip.isdeleted(self) or self._worker is not worker:
                return
            self._busy = False
            self._worker = None
            if generation != self._generation:
                self._refresh_controls()
                return
            on_success(result)

        def failed(exc):
            if sip.isdeleted(self) or self._worker is not worker:
                return
            self._busy = False
            self._worker = None
            if generation != self._generation:
                self._refresh_controls()
                return
            self._stop_preview()
            self.status_label.setText(f"{error_prefix}：{exc}")
            self._refresh_controls()
            show_error(self, _rmtool.APP_NAME, f"{error_prefix}：{exc}")

        worker.signals.finished.connect(finished)
        worker.signals.error.connect(failed)
        self.thread_pool.start(worker)

    def _detect(self) -> None:
        self.status_label.setText("正在检测屏幕预览能力…")
        self._start_worker(
            lambda: _screen_preview.get_status(self.ssh_client),
            self._detected,
            error_prefix="检测屏幕预览失败",
        )

    def _detected(self, status) -> None:
        self._supported = status.supported
        if status.supported:
            self.status_label.setText("Paper Pro Move · 屏幕预览已就绪")
        else:
            device = status.machine or "未知设备"
            self.status_label.setText(f"{device} · 屏幕预览尚未适配")
        self._refresh_controls()

    def _toggle_preview(self, checked: bool) -> None:
        if checked:
            self.preview_button.setText("停止预览")
            self._capture()
        else:
            self._stop_preview()
        self._refresh_controls()

    def _stop_preview(self) -> None:
        self.timer.stop()
        self._generation += 1
        self.preview_button.blockSignals(True)
        self.preview_button.setChecked(False)
        self.preview_button.setText("开启预览")
        self.preview_button.blockSignals(False)
        self._refresh_controls()

    def _capture(self) -> None:
        if self._busy or not self._connected or self._supported is not True:
            return
        automatic = self.preview_button.isChecked()
        self.status_label.setText("正在读取设备画面…")

        def captured(frame):
            self._latest_png = frame.png
            pixmap = QtGui.QPixmap()
            if not pixmap.loadFromData(frame.png, "PNG"):
                raise RuntimeError("无法显示设备预览。")
            self.preview.setPixmap(pixmap)
            self.status_label.setText(f"实时画面 · {frame.width} × {frame.height}")
            self._refresh_controls()
            if automatic and self.preview_button.isChecked() and self._page_active:
                self.timer.start(int(self.interval_combo.currentData()))

        self._start_worker(
            lambda: _screen_preview.capture(self.ssh_client),
            captured,
            error_prefix="屏幕预览失败",
        )

    def _interval_changed(self) -> None:
        if self.timer.isActive():
            self.timer.start(int(self.interval_combo.currentData()))

    def _save_as(self) -> None:
        if self._latest_png is None:
            return
        default_name = datetime.now().strftime("rmtool-screen-%Y%m%d-%H%M%S.png")
        target, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "另存设备画面", default_name, "PNG 图片 (*.png)"
        )
        if not target:
            return
        if Path(target).suffix.casefold() != ".png":
            target += ".png"
        try:
            result = _screen_preview.save_png_atomic(self._latest_png, target)
        except Exception as exc:
            show_error(self, _rmtool.APP_NAME, f"保存设备画面失败：{exc}")
            return
        self.status_message.emit("success", f"画面已保存：{result.path}", 5000)
        show_info(self, _rmtool.APP_NAME, f"画面已保存：\n{result.path}")
