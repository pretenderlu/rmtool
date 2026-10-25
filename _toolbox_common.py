"""Shared widgets and dialogs for the toolbox sections."""

import logging
from contextlib import nullcontext
from typing import Optional

from PyQt5 import QtCore, QtGui, QtWidgets, sip

# Toolbox sections call these through this module (``_ui.show_info`` ...),
# so tests and callers have one place to patch.
from _dialogs import ask_confirmation, show_error, show_info, show_warning  # noqa: F401
import _package_download
import rmtool as _rmtool  # late-bound access to avoid circular import


def plugin_operation_busy(ssh_client) -> bool:
    state = vars(ssh_client)
    client = getattr(ssh_client, "_client", None)
    return state.get("_toolbox_task") is not None and state.get("_toolbox_task_client", client) is client


def start_plugin_worker(
    owner, fn, *args, pending, success="", close_connection=False,
    on_done=None, show_errors=True, apply_status=None, retry=None,
    error_hint="设备不会被自动重启。", recovery_intent=None,
):
    """Reserve one connection until the worker and its completion cleanup finish."""
    ssh = owner.ssh_client
    if owner._busy or plugin_operation_busy(ssh):
        if not owner._busy and show_errors:
            owner.status_label.setText("另一个插件操作正在进行，请等待完成后再试。")
        if on_done is not None:
            on_done()
        return
    task = object()
    client = getattr(ssh, "_client", None)
    ssh._toolbox_task = task
    ssh._toolbox_task_client = client
    owner._busy = True
    owner.status_label.setText(pending)
    owner._update_buttons()

    def current():
        return getattr(ssh, "_client", None) is client and ssh.is_connected()

    def execute():
        session = getattr(ssh, "operation_session", nullcontext)
        with session():
            if not current():
                raise RuntimeError("设备连接已变化，请重新检测后操作。")
            return fn(*args)

    completed = False

    def finish(result=None, exc=None):
        nonlocal completed
        if completed:
            return
        completed = True

        def release():
            if vars(ssh).get("_toolbox_task") is task:
                del ssh._toolbox_task
                vars(ssh).pop("_toolbox_task_client", None)

        try:
            valid = current()
            deleted = sip.isdeleted(owner)
            if not deleted and vars(ssh).get("_toolbox_task") is task:
                owner._busy = False
                owner._update_buttons()
            if not valid:
                return
            if exc is None:
                if not deleted:
                    (apply_status or owner._apply_status)(result)
                if close_connection:
                    with getattr(ssh, "_state_lock", nullcontext()):
                        if current():
                            ssh.close()
                release()
                if success and not deleted:
                    show_info(owner, _rmtool.APP_NAME, success)
            else:
                # A preflight failure must not cancel another device operation.
                release()
                logging.error("%s operation failed: %s", type(owner).__name__, exc)
                if deleted:
                    return
                if isinstance(exc, _package_download.PackageDownloadError):
                    owner.status_label.setText("资源包下载失败，可手动加载后重试")
                    if show_errors:
                        _show_package_download_error(owner, exc, retry=retry)
                else:
                    owner._status = None
                    owner._update_buttons()
                    owner.status_label.setText(f"操作失败：{exc}；请重新检测，设备不会自动重启。")
                    if show_errors:
                        show_error(
                            owner,
                            _rmtool.APP_NAME,
                            f"操作失败：{exc}\n{error_hint}",
                            error=exc,
                            recovery_intent=recovery_intent,
                        )
        finally:
            release()
            if on_done is not None:
                on_done()

    worker = _rmtool.Worker(execute)
    worker.signals.finished.connect(lambda result: finish(result=result))
    worker.signals.error.connect(lambda exc: finish(exc=exc))
    try:
        owner.thread_pool.start(worker)
    except Exception as exc:
        finish(exc=exc)
    return worker


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
