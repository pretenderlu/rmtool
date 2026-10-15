"""Run blocking device work off the Qt main thread.

Slots that talk to the device should call :func:`run_in_background` instead
of invoking SSH directly, so a slow or stalled link never freezes the window.
"""

import logging
from typing import Callable, Iterable, Optional

from PyQt5 import QtCore, QtWidgets, sip

from _dialogs import show_error


def run_in_background(
    owner: QtWidgets.QWidget,
    fn: Callable,
    *args,
    on_success: Optional[Callable[[object], None]] = None,
    error_prefix: str = "操作失败",
    log_message: str = "Background device action failed",
    disable: Iterable[QtWidgets.QWidget] = (),
):
    """Run ``fn(*args)`` in the global thread pool.

    ``disable`` widgets are disabled while the task runs and re-enabled when it
    ends.  Callbacks are delivered on the main thread via queued signals and
    are skipped if ``owner`` was destroyed in the meantime.
    """
    import rmtool as _rmtool  # late-bound to avoid a circular import

    widgets = [w for w in disable if w is not None]
    for widget in widgets:
        widget.setEnabled(False)

    def restore():
        for widget in widgets:
            if not sip.isdeleted(widget):
                widget.setEnabled(True)

    def finished(result):
        if sip.isdeleted(owner):
            return
        restore()
        if on_success is not None:
            on_success(result)

    def failed(exc: Exception):
        if sip.isdeleted(owner):
            logging.error("%s after widget close: %s", log_message, exc)
            return
        restore()
        logging.error("%s: %s", log_message, exc)
        show_error(owner, _rmtool.APP_NAME, f"{error_prefix}：{exc}")

    worker = _rmtool.Worker(fn, *args)
    worker.signals.finished.connect(finished)
    worker.signals.error.connect(failed)
    QtCore.QThreadPool.globalInstance().start(worker)
    return worker
