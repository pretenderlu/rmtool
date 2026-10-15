"""Run blocking device work off the Qt main thread.

Slots that talk to the device should call :func:`run_in_background` instead
of invoking SSH directly, so a slow or stalled link never freezes the window.
"""

import logging
from typing import Callable, Iterable, Optional

from PyQt5 import QtCore, QtWidgets, sip

from _dialogs import show_error
from _ssh import SSHClientWrapper


def run_in_background(
    owner: QtWidgets.QWidget,
    fn: Callable,
    ssh_client: SSHClientWrapper,
    *args,
    on_success: Optional[Callable[[object], None]] = None,
    error_prefix: str = "操作失败",
    log_message: str = "Background device action failed",
    disable: Iterable[QtWidgets.QWidget] = (),
):
    """Run ``fn(ssh_client, *args)`` on the current device in the thread pool.

    ``disable`` widgets are disabled while the task runs and re-enabled when it
    ends.  Callbacks are delivered on the main thread via queued signals and
    are skipped if ``owner`` was destroyed or the connection was replaced.
    The operation lock covers every command, including remount cleanup.
    """
    import rmtool as _rmtool  # late-bound to avoid a circular import

    # Snapshot without waiting for the transport lock on the UI thread.
    with ssh_client._state_lock:
        session = ssh_client._client

    def session_current():
        with ssh_client._state_lock:
            return session is not None and ssh_client._client is session

    def execute():
        with ssh_client.operation_session():
            if not session_current():
                raise RuntimeError("设备连接已变化，请重新操作。")
            return fn(ssh_client, *args)

    task = object()
    widgets = [w for w in disable if w is not None]
    for widget in widgets:
        widget._rmtool_background_task = task
        widget.setEnabled(False)

    def restore():
        for widget in widgets:
            if not sip.isdeleted(widget) and getattr(widget, "_rmtool_background_task", None) is task:
                del widget._rmtool_background_task
                widget.setEnabled(True)

    def finished(result):
        if sip.isdeleted(owner):
            return
        restore()
        if session_current() and on_success is not None:
            on_success(result)

    def failed(exc: Exception):
        if sip.isdeleted(owner):
            logging.error("%s after widget close: %s", log_message, exc)
            return
        restore()
        logging.error("%s: %s", log_message, exc)
        if session_current():
            show_error(owner, _rmtool.APP_NAME, f"{error_prefix}：{exc}")

    worker = _rmtool.Worker(execute)
    worker.signals.finished.connect(finished)
    worker.signals.error.connect(failed)
    QtCore.QThreadPool.globalInstance().start(worker)
    return worker
