"""In-app runtime log viewer.

Pythonw launches without a console, so stdout/stderr disappear.  The root
logger already writes to ``%APPDATA%\\rmtool\\remarkable_tool.log`` via a
RotatingFileHandler; this module exposes the same stream inside a non-modal
Qt dialog so the user does not need to crack open the file.

Pieces
------
* ``_LogBridge``   — QObject carrying the ``record_emitted`` signal.  Living
  on the main thread, it lets log records produced by background workers
  reach the UI safely (Qt queues cross-thread signals automatically).
* ``QtLogHandler`` — ``logging.Handler`` that forwards each formatted record
  through the bridge.
* ``attach_qt_log_handler`` — convenience: instantiate both, register the
  handler on the root logger, return the bridge.
* ``LogViewerDialog`` — the visible window.  Tails the existing log file on
  open, then appends new records live.
"""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path
from typing import Deque, Optional

from PyQt5 import QtCore, QtGui, QtWidgets


_DEFAULT_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class _LogBridge(QtCore.QObject):
    record_emitted = QtCore.pyqtSignal(str, int)  # formatted message, levelno


class QtLogHandler(logging.Handler):
    """Forward records to a Qt signal so the UI can react."""

    def __init__(self, bridge: _LogBridge) -> None:
        super().__init__()
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            msg = self.format(record)
            self._bridge.record_emitted.emit(msg, record.levelno)
        except Exception:
            self.handleError(record)


def attach_qt_log_handler(formatter: Optional[logging.Formatter] = None) -> _LogBridge:
    """Install a QtLogHandler on the root logger and return its bridge."""

    bridge = _LogBridge()
    handler = QtLogHandler(bridge)
    handler.setFormatter(formatter or logging.Formatter(_DEFAULT_FORMAT))
    logging.getLogger().addHandler(handler)
    return bridge


class LogViewerDialog(QtWidgets.QDialog):
    """Non-modal window showing recent log records.

    On open: tail the on-disk log file (last ``tail_bytes`` bytes) so prior
    sessions are visible.  Then append every new record via the bridge.
    """

    def __init__(
        self,
        bridge: _LogBridge,
        log_file: Optional[Path] = None,
        parent: Optional[QtWidgets.QWidget] = None,
        tail_bytes: int = 64 * 1024,
        max_lines: int = 5000,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("logViewerDialog")
        self.setWindowTitle("运行日志")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMinimizeButtonHint)
        self.setMinimumSize(820, 480)
        self.resize(960, 560)

        self._bridge = bridge
        self._log_file = log_file
        self._tail_bytes = tail_bytes
        self._max_lines = max_lines
        self._paused = False
        self._auto_scroll = True
        self._min_level = logging.INFO
        self._buffer: Deque[tuple[str, int]] = deque(maxlen=max_lines)

        self._build_ui()
        self._load_history()
        self._bridge.record_emitted.connect(self._on_record)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setSpacing(8)

        self.level_combo = QtWidgets.QComboBox()
        for name in _LEVELS:
            self.level_combo.addItem(name, getattr(logging, name))
        self.level_combo.setCurrentText("INFO")
        self.level_combo.currentIndexChanged.connect(self._on_level_changed)
        toolbar.addWidget(QtWidgets.QLabel("级别:"))
        toolbar.addWidget(self.level_combo)

        self.auto_scroll_check = QtWidgets.QCheckBox("自动滚动")
        self.auto_scroll_check.setChecked(True)
        self.auto_scroll_check.toggled.connect(self._on_auto_scroll)
        toolbar.addWidget(self.auto_scroll_check)

        self.pause_check = QtWidgets.QCheckBox("暂停")
        self.pause_check.toggled.connect(self._on_pause)
        toolbar.addWidget(self.pause_check)

        toolbar.addStretch()

        self.clear_button = QtWidgets.QPushButton("清屏")
        self.clear_button.clicked.connect(self._on_clear)
        toolbar.addWidget(self.clear_button)

        self.open_file_button = QtWidgets.QPushButton("打开日志文件")
        self.open_file_button.clicked.connect(self._on_open_file)
        if self._log_file is None:
            self.open_file_button.setEnabled(False)
        toolbar.addWidget(self.open_file_button)

        layout.addLayout(toolbar)

        self.text_view = QtWidgets.QPlainTextEdit()
        self.text_view.setObjectName("logViewerText")
        self.text_view.setReadOnly(True)
        self.text_view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        mono = QtGui.QFont("Consolas")
        mono.setStyleHint(QtGui.QFont.Monospace)
        mono.setPointSize(10)
        self.text_view.setFont(mono)
        self.text_view.setMaximumBlockCount(self._max_lines)
        layout.addWidget(self.text_view, 1)

        self.status_label = QtWidgets.QLabel()
        self.status_label.setObjectName("logViewerStatus")
        self._update_status()
        layout.addWidget(self.status_label)

    # -- Slots -------------------------------------------------------------
    def _on_record(self, msg: str, levelno: int) -> None:
        self._buffer.append((msg, levelno))
        if self._paused:
            return
        if levelno < self._min_level:
            return
        self._append_line(msg)

    def _on_level_changed(self, _index: int) -> None:
        self._min_level = self.level_combo.currentData()
        self._redraw_from_buffer()

    def _on_auto_scroll(self, checked: bool) -> None:
        self._auto_scroll = checked
        if checked:
            self._scroll_to_bottom()

    def _on_pause(self, checked: bool) -> None:
        self._paused = checked
        if not checked:
            self._redraw_from_buffer()

    def _on_clear(self) -> None:
        self.text_view.clear()
        self._update_status()

    def _on_open_file(self) -> None:
        if not self._log_file:
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self._log_file)))

    # -- Helpers -----------------------------------------------------------
    def _append_line(self, line: str) -> None:
        self.text_view.appendPlainText(line)
        if self._auto_scroll:
            self._scroll_to_bottom()
        self._update_status()

    def _scroll_to_bottom(self) -> None:
        bar = self.text_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _redraw_from_buffer(self) -> None:
        self.text_view.clear()
        for msg, levelno in self._buffer:
            if levelno >= self._min_level:
                self.text_view.appendPlainText(msg)
        if self._auto_scroll:
            self._scroll_to_bottom()
        self._update_status()

    def _update_status(self) -> None:
        if self._log_file:
            self.status_label.setText(
                f"{self.text_view.blockCount()} 行  ·  日志文件：{self._log_file}"
            )
        else:
            self.status_label.setText(f"{self.text_view.blockCount()} 行")

    def _load_history(self) -> None:
        if not self._log_file or not self._log_file.exists():
            return
        try:
            size = self._log_file.stat().st_size
            with self._log_file.open("rb") as fh:
                if size > self._tail_bytes:
                    fh.seek(size - self._tail_bytes)
                    fh.readline()  # discard partial first line
                data = fh.read()
            text = data.decode("utf-8", errors="replace").rstrip("\n")
            if not text:
                return
            for line in text.splitlines():
                self.text_view.appendPlainText(line)
            self._scroll_to_bottom()
            self._update_status()
        except OSError:
            pass
