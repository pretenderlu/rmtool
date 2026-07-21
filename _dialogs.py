"""Shared styled dialogs for the desktop UI."""

from typing import Optional

from PyQt5 import QtCore, QtWidgets


_KIND_HEADINGS = {
    "info": "提示",
    "warning": "需要注意",
    "error": "操作失败",
    "confirm": "请确认",
}

_KIND_BADGES = {
    "info": "i",
    "warning": "!",
    "error": "!",
    "confirm": "?",
}


def _build_dialog(
    parent: Optional[QtWidgets.QWidget],
    window_title: str,
    heading: str,
    message: str,
    *,
    kind: str,
    detail: Optional[str] = None,
    primary_text: str = "知道了",
    secondary_text: Optional[str] = None,
    primary_role: str = "primary",
) -> QtWidgets.QDialog:
    dialog = QtWidgets.QDialog(parent)
    dialog.setObjectName("appDialog")
    dialog.setWindowTitle(window_title)
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
    surface.setObjectName("appDialogSurface")
    surface.setAttribute(QtCore.Qt.WA_StyledBackground, True)
    surface_layout = QtWidgets.QVBoxLayout(surface)
    surface_layout.setContentsMargins(24, 24, 24, 22)
    surface_layout.setSpacing(18)
    root_layout.addWidget(surface)

    header_layout = QtWidgets.QHBoxLayout()
    header_layout.setContentsMargins(0, 0, 0, 0)
    header_layout.setSpacing(14)

    badge = QtWidgets.QLabel(_KIND_BADGES.get(kind, "i"))
    badge.setObjectName("appDialogBadge")
    badge.setProperty("kind", kind)
    badge.setFixedSize(46, 46)
    badge.setAlignment(QtCore.Qt.AlignCenter)
    header_layout.addWidget(badge, 0, QtCore.Qt.AlignTop)

    text_stack = QtWidgets.QVBoxLayout()
    text_stack.setContentsMargins(0, 0, 0, 0)
    text_stack.setSpacing(8)

    title = QtWidgets.QLabel(heading)
    title.setObjectName("appDialogTitle")
    title.setWordWrap(True)
    text_stack.addWidget(title)

    body = QtWidgets.QLabel(message)
    body.setObjectName("appDialogBody")
    body.setWordWrap(True)
    body.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    text_stack.addWidget(body)

    header_layout.addLayout(text_stack, 1)
    surface_layout.addLayout(header_layout)

    if detail:
        note = QtWidgets.QFrame()
        note.setObjectName("appDialogNote")
        note.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        note_layout = QtWidgets.QVBoxLayout(note)
        note_layout.setContentsMargins(14, 12, 14, 12)
        note_text = QtWidgets.QLabel(detail)
        note_text.setObjectName("appDialogNoteText")
        note_text.setWordWrap(True)
        note_text.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        note_layout.addWidget(note_text)
        surface_layout.addWidget(note)

    button_row = QtWidgets.QHBoxLayout()
    button_row.setContentsMargins(0, 2, 0, 0)
    button_row.setSpacing(10)
    button_row.addStretch()

    if secondary_text:
        secondary_button = QtWidgets.QPushButton(secondary_text)
        secondary_button.setObjectName("appDialogSecondary")
        secondary_button.clicked.connect(dialog.reject)
        button_row.addWidget(secondary_button)

    primary_button = QtWidgets.QPushButton(primary_text)
    primary_button.setObjectName("appDialogPrimary")
    primary_button.setProperty(
        "btnRole", "danger" if primary_role == "danger" else "primary"
    )
    primary_button.setDefault(True)
    primary_button.clicked.connect(dialog.accept)
    button_row.addWidget(primary_button)
    surface_layout.addLayout(button_row)

    return dialog


def show_info(parent: Optional[QtWidgets.QWidget], title: str, message: str) -> None:
    _show_message(parent, title, message, kind="info")


def show_warning(parent: Optional[QtWidgets.QWidget], title: str, message: str) -> None:
    _show_message(parent, title, message, kind="warning")


def show_error(parent: Optional[QtWidgets.QWidget], title: str, message: str) -> None:
    _show_message(parent, title, message, kind="error")


def _show_message(
    parent: Optional[QtWidgets.QWidget],
    title: str,
    message: str,
    *,
    kind: str,
) -> None:
    dialog = _build_dialog(
        parent,
        title,
        _KIND_HEADINGS.get(kind, "提示"),
        message,
        kind=kind,
        primary_text="知道了",
    )
    dialog.exec_()


def ask_confirmation(
    parent: Optional[QtWidgets.QWidget],
    title: str,
    message: str,
    *,
    confirm_text: str = "确认",
    cancel_text: str = "取消",
    detail: Optional[str] = None,
    danger: bool = False,
) -> bool:
    dialog = _build_dialog(
        parent,
        title,
        _KIND_HEADINGS["confirm"],
        message,
        kind="confirm",
        detail=detail,
        primary_text=confirm_text,
        secondary_text=cancel_text,
        primary_role="danger" if danger else "primary",
    )
    return dialog.exec_() == QtWidgets.QDialog.Accepted
