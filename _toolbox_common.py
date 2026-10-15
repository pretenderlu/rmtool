"""Shared widgets and dialogs for the toolbox sections."""

import logging
from typing import Optional

from PyQt5 import QtCore, QtGui, QtWidgets

# Toolbox sections call these through this module (``_ui.show_info`` ...),
# so tests and callers have one place to patch.
from _dialogs import ask_confirmation, show_error, show_info, show_warning  # noqa: F401
import _package_download
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
        (
            "资源包 (*.zip);;所有文件 (*)"
            if exc.asset.casefold().endswith(".zip")
            else "资源包 (*.tar.gz);;所有文件 (*)"
        ),
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


class ToolboxStatusLabel(QtWidgets.QLabel):
    text_changed = QtCore.pyqtSignal(str)

    def setText(self, text: str):
        changed = text != self.text()
        super().setText(text)
        if changed:
            self.text_changed.emit(text)
