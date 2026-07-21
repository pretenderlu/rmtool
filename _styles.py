"""Stylesheets and palettes extracted from rmtool.py.

Both themes share a single QSS template (``_STYLESHEET_TEMPLATE``).  Colors
and radii come from the design tokens in ``_tokens.py``; the template is
rendered once per theme via :func:`_render_stylesheet`, producing
``_DARK_STYLESHEET`` and ``_LIGHT_STYLESHEET``.

The rendered stylesheets still contain ``{arrow_*}`` placeholders for the
runtime-generated combo box arrow icons; those are substituted by
``rmtool._resolve_stylesheet``.

Buttons follow a three-level hierarchy selected with the ``btnRole``
dynamic property: ``primary`` (one emphasized action per region, solid
accent), ``danger`` (destructive actions) and the default ``secondary``
look (every QPushButton without a ``btnRole``).
"""

from PyQt5 import QtGui

from _tokens import DARK_TOKENS, LIGHT_TOKENS


_STYLESHEET_TEMPLATE = """
/* ===== Global ===== */
* {
    outline: none;
}

QMainWindow {
    background: {bg_base};
}

/* ===== Group Box ===== */
QGroupBox {
    background: {bg_surface};
    border: 1px solid {border};
    border-radius: {radius_panel};
    margin-top: 0px;
    padding: 36px {panel_padding} {panel_padding} {panel_padding};
}
QGroupBox::title {
    subcontrol-origin: padding;
    subcontrol-position: top left;
    left: 16px;
    top: 10px;
    color: {text_muted};
    font-weight: 600;
    letter-spacing: 0.5px;
}

/* ===== Buttons: default is secondary ===== */
QPushButton {
    background: {btn_secondary_bg};
    color: {text_secondary};
    border: 1px solid {border_popup};
    border-radius: {radius_control};
    padding: 10px 24px;
    font-weight: 600;
}
QPushButton:hover:!disabled {
    background: {btn_secondary_hover_bg};
    border-color: {btn_secondary_hover_border};
    color: {text_primary};
}
QPushButton:pressed:!disabled {
    background: {btn_secondary_pressed_bg};
}
QPushButton:checked {
    background: {btn_secondary_checked_bg};
    color: {btn_secondary_checked_text};
    border-color: {btn_secondary_checked_border};
}
QPushButton:disabled {
    background: {btn_secondary_disabled_bg};
    color: {btn_secondary_disabled_text};
    border-color: {btn_secondary_disabled_border};
}

/* Primary: the single emphasized action per region, solid accent */
QPushButton[btnRole="primary"] {
    background: {accent};
    color: {text_on_accent};
    border: none;
}
QPushButton[btnRole="primary"]:hover:!disabled {
    background: {accent_hover};
}
QPushButton[btnRole="primary"]:pressed:!disabled {
    background: {accent_pressed};
}
QPushButton[btnRole="primary"]:disabled {
    background: {btn_disabled_bg};
    color: {text_disabled};
}

/* Danger: destructive actions stay quiet until hovered */
QPushButton[btnRole="danger"] {
    color: {danger};
    border-color: {danger_border};
}
QPushButton[btnRole="danger"]:hover:!disabled {
    background: {danger_hover_bg};
    border-color: {danger_hover_border};
    color: {danger_hover_text};
}

QToolButton {
    background: {tool_bg};
    color: {text_secondary};
    border: 1px solid {border_control};
    border-radius: {radius_control};
    padding: 7px 10px;
}
QToolButton:hover {
    background: {tool_bg_hover};
    border-color: {tool_border_hover};
    color: {text_primary};
}
QToolButton[btnRole="danger"] {
    color: {danger};
    border-color: {danger_border};
}
QToolButton[btnRole="danger"]:hover {
    background: {danger_hover_bg};
    border-color: {danger_hover_border};
    color: {danger_hover_text};
}

/* ===== Input Fields ===== */
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {
    background: {bg_inner};
    color: {text_bright};
    border: 1px solid {border_control};
    border-radius: {radius_inner};
    padding: 8px 12px;
    selection-background-color: {selection_bg};
    selection-color: {text_primary};
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border-color: {accent_focus};
}
QLineEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] {
    background: {bg_inner_readonly};
    color: {text_readonly};
}

QComboBox {
    padding-right: 32px;
}
QComboBox::drop-down {
    border: none;
    width: 30px;
    padding-right: 4px;
}
QComboBox::down-arrow {
    image: url({combo_arrow});
    width: 16px;
    height: 16px;
}
QComboBox::down-arrow:on {
    image: url({combo_arrow_up});
    width: 16px;
    height: 16px;
}
QComboBox QAbstractItemView {
    background: {bg_popup};
    border: 1px solid {border_popup};
    border-radius: {radius_inner};
    selection-background-color: {selection_bg_popup};
    padding: 4px;
    outline: none;
}

/* ===== Checkbox & Radio ===== */
QCheckBox, QRadioButton {
    spacing: 8px;
    color: {text_secondary};
}
QCheckBox::indicator, QRadioButton::indicator {
    width: 20px;
    height: 20px;
    border: 2px solid {indicator_border};
    background: {indicator_bg};
}
QCheckBox::indicator {
    border-radius: 5px;
}
QRadioButton::indicator {
    border-radius: 11px;
}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: {control_accent};
    border-color: transparent;
}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {
    border-color: {indicator_hover_border};
}

/* ===== Tab Widget ===== */
QTabWidget::pane {
    background: {bg_pane};
    border: 1px solid {border_pane};
    border-radius: {radius_panel};
    top: -1px;
}
QTabBar {
    qproperty-drawBase: 0;
}
QTabBar::tab {
    background: transparent;
    color: {text_tab};
    padding: 14px 28px;
    margin: 0 1px;
    border-bottom: 3px solid transparent;
    font-weight: 500;
}
QTabBar::tab:selected {
    color: {text_primary};
    border-bottom: 3px solid {control_accent};
    background: {tab_selected_bg};
}
QTabBar::tab:hover:!selected {
    color: {text_tab_hover};
    background: {tab_hover_bg};
    border-bottom: 3px solid {tab_hover_border};
}

/* ===== Table ===== */
QTableWidget {
    background: {bg_table};
    alternate-background-color: {bg_table_alt};
    border: 1px solid {border_subtle};
    border-radius: {radius_inner};
    gridline-color: {gridline};
    selection-background-color: {table_selection_bg};
    selection-color: {text_primary};
    padding: 2px;
}
QTableWidget::item {
    padding: 10px 12px;
    border-bottom: 1px solid {table_row_border};
}
QTableWidget::item:selected {
    background: {table_item_selected};
    border-radius: 0;
}
QHeaderView::section {
    background: {bg_header};
    color: {text_faded};
    padding: 10px 12px;
    border: none;
    border-bottom: 1px solid {border_subtle};
    font-weight: 600;
}

/* ===== Labels ===== */
QLabel {
    color: {text_secondary};
}
#previewImage {
    background: {bg_preview};
    border: 1px solid {border_subtle};
    border-radius: {radius_inner};
}
#wallpaperWorkspace {
    background: transparent;
}
#wallpaperControlScroll,
#wallpaperControlViewport,
#wallpaperControlInner {
    background: transparent;
    border: none;
}
#wallpaperControlPanel,
#wallpaperPreviewPanel,
#documentsListPanel,
#documentsPreviewPanel {
    background: {bg_panel};
    border: 1px solid {border_panel};
    border-radius: {radius_panel};
}
#connectionStatusLabel {
    font-size: 16pt;
    font-weight: 700;
    padding: 10px 0;
    letter-spacing: 0.5px;
}

/* ===== Sliders ===== */
QSlider::groove:horizontal {
    height: 6px;
    background: {border};
    border-radius: 3px;
}
QSlider::handle:horizontal {
    width: 20px;
    height: 20px;
    background: {control_accent};
    border-radius: 10px;
    margin: -7px 0;
}
QSlider::sub-page:horizontal {
    background: {control_accent};
    border-radius: 3px;
}

/* ===== Scrollbars ===== */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 4px 2px;
}
QScrollBar::handle:vertical {
    background: {scrollbar_handle};
    border-radius: 4px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover {
    background: {scrollbar_handle_hover};
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
    border: none;
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px 4px;
}
QScrollBar::handle:horizontal {
    background: {scrollbar_handle};
    border-radius: 4px;
    min-width: 32px;
}
QScrollBar::handle:horizontal:hover {
    background: {scrollbar_handle_hover};
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: none;
    border: none;
    width: 0;
}

/* ===== Splitter ===== */
QSplitter::handle {
    background: transparent;
}
QSplitter::handle:horizontal { width: 8px; }
QSplitter::handle:vertical { height: 8px; }

/* ===== Progress Dialog ===== */
QProgressDialog {
    background: {bg_popup};
}
QProgressBar {
    background: {progress_bg};
    border: none;
    border-radius: 6px;
    height: 10px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background: {control_accent};
    border-radius: 5px;
}

/* ===== Tooltips ===== */
QToolTip {
    background: {bg_tooltip};
    color: {text_bright};
    border: 1px solid {border_popup};
    border-radius: 6px;
    padding: 6px 10px;
}

/* ===== Message Box ===== */
QMessageBox {
    background: {bg_messagebox};
}

QStatusBar#appStatusBar {
    background: {bg_statusbar};
    color: {text_secondary};
    border-top: 1px solid {border_subtle};
}
QStatusBar#appStatusBar[level="success"] {
    color: {success};
}
QStatusBar#appStatusBar[level="warning"] {
    color: {warning};
}
QStatusBar#appStatusBar[level="error"] {
    color: {danger};
}
#appConnectionChip {
    color: {text_secondary};
    background: {chip_bg};
    border: 1px solid {border};
    border-radius: 10px;
    padding: 4px 10px;
    margin-left: 8px;
}
#appConnectionChip[connected="true"] {
    color: {success};
    border-color: {success_border};
    background: {success_bg};
}

/* ===== Sidebar ===== */
#sidebar {
    background: {sidebar_bg};
    border-right: 1px solid {border_subtle};
}
#sidebarConnection {
    background: transparent;
}
#sidebarSectionLabel {
    color: {text_faded};
    font-size: 20px;
    font-weight: 600;
    letter-spacing: 0.02em;
    padding: 0;
    margin-top: 2px;
}
#sidebarBrand {
    color: {text_brand};
    font-size: 13px;
    font-weight: 500;
    letter-spacing: 0.04em;
    padding: 12px 0;
}

/* ===== Sidebar navigation ===== */
#sidebarNavSection {
    background: transparent;
}
#sidebarNav {
    background: transparent;
    border: none;
    outline: none;
}
#sidebarNav::item {
    color: {text_secondary};
    padding: 9px 12px;
    border-left: 3px solid transparent;
    border-radius: {radius_control};
}
#sidebarNav::item:hover:!selected {
    background: {tab_hover_bg};
    color: {text_primary};
}
#sidebarNav::item:selected {
    background: {tab_selected_bg};
    color: {text_primary};
    border-left: 3px solid {control_accent};
    font-weight: 600;
}
#sidebarNav::item:disabled {
    color: {text_disabled};
}

/* ===== Status dot ===== */
#statusDot {
    border-radius: 6px;
    background: {status_off};
    min-width: 12px;
    min-height: 12px;
}
#statusDot[connected="true"] {
    background: {status_on};
    border: none;
}
#statusText {
    font-size: 18px;
    font-weight: 700;
    color: {text_secondary};
    line-height: 1.3;
    letter-spacing: 0.03em;
}
#deviceCard {
    background: {bg_card};
    border: 1px solid {border_panel};
    border-radius: {radius_panel};
}
#deviceCardTitle {
    color: {text_primary};
    font-size: 22px;
    font-weight: 700;
}
#deviceCardMeta {
    color: {text_soft};
    font-size: 14px;
}
#deviceCardHost {
    color: {text_secondary};
    font-size: 14px;
    font-weight: 600;
}
#credentialStatusLabel {
    color: {text_faded};
    font-size: 13px;
    font-weight: 600;
}
#forgetPasswordButton {
    padding: 5px 8px;
    font-size: 12px;
    border-radius: 7px;
}

#documentsSummaryLabel {
    color: {text_soft};
    font-size: 13px;
    font-weight: 600;
}
#panelSectionLabel {
    color: {text_muted};
    font-size: 18px;
    font-weight: 600;
    padding: 0;
}
#fontTargetName {
    color: {text_dim};
}
#fontPreviewPanel {
    background: {bg_font_preview};
    border: 1px solid {border};
    border-radius: {radius_inner};
}
#fontPreviewTitle {
    color: {text_soft};
    font-size: 13px;
    font-weight: 600;
}
#fontPreviewSample {
    color: {text_primary};
    background: transparent;
    padding: 0;
}
#documentsEmptyState {
    color: {text_faded};
    border: 1px dashed {border_dashed};
    border-radius: {radius_inner};
    padding: 24px;
    margin-top: 8px;
}

/* ===== Theme toggle ===== */
#themeToggle,
#logViewerButton,
#githubLinkButton {
    background: {pill_bg};
    color: {text_pill};
    border: 1px solid {border_popup};
    border-radius: 19px;
    padding: 0;
}
#themeToggle:hover,
#logViewerButton:hover,
#githubLinkButton:hover {
    background: {pill_bg_hover};
    color: {text_contrast};
    border-color: {pill_border_hover};
}
#themeToggle:pressed,
#logViewerButton:pressed,
#githubLinkButton:pressed {
    background: {pill_bg_pressed};
}

/* ===== Log viewer panel ===== */
#logViewerPanel {
    background: {bg_log_panel};
    border-top: 1px solid {border_control};
}
#logViewerTitle {
    color: {text_contrast};
    font-weight: 700;
    font-size: 18px;
    letter-spacing: 0.02em;
}
#logViewerText {
    background: {bg_log_text};
    color: {text_contrast};
    border: 1px solid {border_popup};
    border-radius: {radius_control};
    padding: 8px;
    selection-background-color: {selection_log};
}
#logViewerStatus {
    color: {text_log_status};
    font-size: 12px;
}
#logViewerClose {
    background: {pill_bg};
    color: {text_pill};
    border: 1px solid {border_popup};
    border-radius: 19px;
    font-size: 22px;
    font-weight: 600;
    padding: 0;
    padding-bottom: 4px;
}
#logViewerClose:hover {
    background: {pill_bg_hover};
    color: {text_contrast};
    border-color: {pill_border_hover};
}
#logViewerClose:pressed {
    background: {pill_bg_pressed};
}
QSplitter#mainSplitter::handle {
    background: {splitter_handle};
}
QSplitter#mainSplitter::handle:hover {
    background: {splitter_handle_hover};
}

/* ===== App dialog ===== */
#appDialog {
    background: transparent;
}
#appDialogSurface {
    background: {dialog_bg};
    border: 1px solid {border_dialog};
    border-radius: {radius_panel};
}
#appDialogBadge {
    background: {badge_bg};
    color: {badge_text};
    border: 1px solid {badge_border};
    border-radius: 23px;
    font-size: 24px;
    font-weight: 800;
}
#appDialogBadge[kind="warning"],
#appDialogBadge[kind="confirm"] {
    background: {badge_warning_bg};
    color: {badge_warning_text};
    border-color: {badge_warning_border};
}
#appDialogBadge[kind="error"] {
    background: {badge_error_bg};
    color: {badge_error_text};
    border-color: {badge_error_border};
}
#appDialogTitle {
    color: {text_primary};
    font-size: 21px;
    font-weight: 800;
}
#appDialogBody {
    color: {text_secondary};
    font-size: 16px;
}
#appDialogNote {
    background: {note_bg};
    border: 1px solid {note_border};
    border-radius: {radius_inner};
}
#appDialogNoteText {
    color: {note_text};
    font-size: 15px;
}
#appDialogPrimary,
#appDialogSecondary {
    min-width: 108px;
    min-height: 38px;
    padding: 0 18px;
}

/* ===== Restart confirmation dialog ===== */
#restartConfirmDialog {
    background: transparent;
}
#restartConfirmSurface {
    background: {dialog_bg};
    border: 1px solid {border_dialog};
    border-radius: {radius_panel};
}
#restartConfirmBadge {
    background: {badge_bg};
    color: {badge_text};
    border: 1px solid {badge_border};
    border-radius: 23px;
    font-size: 24px;
    font-weight: 700;
}
#restartConfirmTitle {
    color: {text_primary};
    font-size: 21px;
    font-weight: 800;
}
#restartConfirmSubtitle {
    color: {text_subtitle};
    font-size: 14px;
    font-weight: 600;
}
#restartConfirmBody {
    color: {text_secondary};
    font-size: 16px;
}
#restartConfirmNote {
    background: {note_bg};
    border: 1px solid {note_border};
    border-radius: {radius_inner};
}
#restartConfirmNoteText {
    color: {note_text};
    font-size: 15px;
}
#restartConfirmPrimary,
#restartConfirmSecondary {
    min-width: 108px;
    min-height: 38px;
    padding: 0 18px;
}
"""


def _render_stylesheet(template: str, tokens: dict) -> str:
    """Render ``{token}`` placeholders in *template* via ``str.format``.

    QSS rules use literal braces heavily, so all braces are escaped first and
    the known token fields are then un-escaped, leaving a plain
    ``str.format``-able template.  Token values are inserted verbatim (no
    recursive substitution), which keeps placeholders such as ``{arrow_dark}``
    inside token values intact for ``rmtool._resolve_stylesheet``.
    """
    escaped = template.replace("{", "{{").replace("}", "}}")
    for name in tokens:
        escaped = escaped.replace("{{" + name + "}}", "{" + name + "}")
    return escaped.format(**tokens)


_DARK_STYLESHEET = _render_stylesheet(_STYLESHEET_TEMPLATE, DARK_TOKENS)
_LIGHT_STYLESHEET = _render_stylesheet(_STYLESHEET_TEMPLATE, LIGHT_TOKENS)


def _build_palette(tokens: dict) -> QtGui.QPalette:
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Window, QtGui.QColor(tokens["bg_base"]))
    palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor(tokens["palette_window_text"]))
    palette.setColor(QtGui.QPalette.Base, QtGui.QColor(tokens["palette_base"]))
    palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(tokens["palette_alt_base"]))
    palette.setColor(QtGui.QPalette.Text, QtGui.QColor(tokens["text_bright"]))
    palette.setColor(QtGui.QPalette.Button, QtGui.QColor(tokens["palette_button"]))
    palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor(tokens["text_bright"]))
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(tokens["control_accent"]))
    palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor(tokens["text_on_accent"]))
    palette.setColor(QtGui.QPalette.ToolTipBase, QtGui.QColor(tokens["bg_tooltip"]))
    palette.setColor(QtGui.QPalette.ToolTipText, QtGui.QColor(tokens["text_bright"]))
    palette.setColor(QtGui.QPalette.Link, QtGui.QColor(tokens["palette_link"]))
    palette.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, QtGui.QColor(tokens["palette_disabled"]))
    palette.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.ButtonText, QtGui.QColor(tokens["palette_disabled"]))
    return palette


def _dark_palette() -> QtGui.QPalette:
    return _build_palette(DARK_TOKENS)


def _light_palette() -> QtGui.QPalette:
    return _build_palette(LIGHT_TOKENS)
