"""Stylesheets and palettes extracted from rmtool.py."""

from PyQt5 import QtGui


_DARK_STYLESHEET = """
/* ===== Global ===== */
* {
    outline: none;
}

QMainWindow {
    background: #1A1D27;
}

/* ===== Group Box ===== */
QGroupBox {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 rgba(30, 36, 50, 0.95), stop:1 rgba(22, 26, 38, 0.95));
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: {panel_radius};
    margin-top: 0px;
    padding: 36px {panel_padding} {panel_padding} {panel_padding};
}
QGroupBox::title {
    subcontrol-origin: padding;
    subcontrol-position: top left;
    left: 16px;
    top: 10px;
    color: #A0AACC;
    font-weight: 600;
    letter-spacing: 0.5px;
}

/* ===== Buttons ===== */
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #5B7CF7, stop:1 #3DBBF5);
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    padding: 10px 24px;
    font-weight: 600;
}
QPushButton:hover:!disabled {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #6E8EFF, stop:1 #55CCFF);
}
QPushButton:pressed:!disabled {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #4A6AE0, stop:1 #2EA8E0);
}
QPushButton:disabled {
    background: rgba(255, 255, 255, 0.06);
    color: #5A6380;
}

QToolButton {
    background: rgba(255, 255, 255, 0.06);
    color: #C0C8E0;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 8px;
    padding: 7px 10px;
}
QToolButton:hover {
    background: rgba(255, 255, 255, 0.12);
    border-color: rgba(255, 255, 255, 0.15);
    color: #FFFFFF;
}

/* ===== Input Fields ===== */
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {
    background: rgba(15, 17, 25, 0.6);
    color: #E0E6F0;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: {inner_panel_radius};
    padding: 8px 12px;
    selection-background-color: rgba(91, 124, 247, 0.4);
    selection-color: #FFFFFF;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border-color: rgba(91, 124, 247, 0.6);
}
QLineEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] {
    background: rgba(15, 17, 25, 0.35);
    color: #9AA0B8;
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
    image: url({arrow_dark});
    width: 16px;
    height: 16px;
}
QComboBox::down-arrow:on {
    image: url({arrow_dark_up});
    width: 16px;
    height: 16px;
}
QComboBox QAbstractItemView {
    background: #252836;
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: {inner_panel_radius};
    selection-background-color: rgba(91, 124, 247, 0.3);
    padding: 4px;
    outline: none;
}

/* ===== Checkbox & Radio ===== */
QCheckBox, QRadioButton {
    spacing: 8px;
    color: #C0C8E0;
}
QCheckBox::indicator, QRadioButton::indicator {
    width: 20px;
    height: 20px;
    border: 2px solid rgba(255, 255, 255, 0.2);
    background: rgba(255, 255, 255, 0.04);
}
QCheckBox::indicator {
    border-radius: 5px;
}
QRadioButton::indicator {
    border-radius: 11px;
}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #5B7CF7, stop:1 #3DBBF5);
    border-color: transparent;
}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {
    border-color: rgba(91, 124, 247, 0.5);
}

/* ===== Tab Widget ===== */
QTabWidget::pane {
    background: rgba(28, 31, 42, 0.95);
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: {panel_radius};
    top: -1px;
}
QTabBar {
    qproperty-drawBase: 0;
}
QTabBar::tab {
    background: transparent;
    color: #6B7394;
    padding: 14px 28px;
    margin: 0 1px;
    border-bottom: 3px solid transparent;
    font-weight: 500;
}
QTabBar::tab:selected {
    color: #FFFFFF;
    border-bottom: 3px solid #5B7CF7;
    background: rgba(91, 124, 247, 0.08);
}
QTabBar::tab:hover:!selected {
    color: #A0AACC;
    background: rgba(255, 255, 255, 0.03);
    border-bottom: 3px solid rgba(91, 124, 247, 0.25);
}

/* ===== Table ===== */
QTableWidget {
    background: rgba(15, 17, 25, 0.5);
    alternate-background-color: rgba(255, 255, 255, 0.02);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: {inner_panel_radius};
    gridline-color: rgba(255, 255, 255, 0.04);
    selection-background-color: rgba(91, 124, 247, 0.2);
    selection-color: #FFFFFF;
    padding: 2px;
}
QTableWidget::item {
    padding: 10px 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.03);
}
QTableWidget::item:selected {
    background: rgba(91, 124, 247, 0.18);
    border-radius: 0;
}
QHeaderView::section {
    background: rgba(35, 38, 52, 0.95);
    color: #8890A8;
    padding: 10px 12px;
    border: none;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    font-weight: 600;
}

/* ===== Labels ===== */
QLabel {
    color: #C0C8E0;
}
#previewImage {
    background: rgba(15, 17, 25, 0.4);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: {inner_panel_radius};
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
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: {panel_radius};
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
    background: rgba(255, 255, 255, 0.08);
    border-radius: 3px;
}
QSlider::handle:horizontal {
    width: 20px;
    height: 20px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #5B7CF7, stop:1 #3DBBF5);
    border-radius: 10px;
    margin: -7px 0;
}
QSlider::sub-page:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #5B7CF7, stop:1 #3DBBF5);
    border-radius: 3px;
}

/* ===== Scrollbars ===== */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 4px 2px;
}
QScrollBar::handle:vertical {
    background: rgba(255, 255, 255, 0.12);
    border-radius: 4px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover {
    background: rgba(255, 255, 255, 0.22);
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
    background: rgba(255, 255, 255, 0.12);
    border-radius: 4px;
    min-width: 32px;
}
QScrollBar::handle:horizontal:hover {
    background: rgba(255, 255, 255, 0.22);
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
    background: #252836;
}
QProgressBar {
    background: rgba(255, 255, 255, 0.06);
    border: none;
    border-radius: 6px;
    height: 10px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #5B7CF7, stop:1 #3DBBF5);
    border-radius: 5px;
}

/* ===== Tooltips ===== */
QToolTip {
    background: #2A2E3C;
    color: #E0E6F0;
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 6px;
    padding: 6px 10px;
}

/* ===== Message Box ===== */
QMessageBox {
    background: #1E2130;
}

QStatusBar#appStatusBar {
    background: rgba(17, 19, 27, 0.95);
    color: #C0C8E0;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
}
QStatusBar#appStatusBar[level="success"] {
    color: #6DDC8C;
}
QStatusBar#appStatusBar[level="warning"] {
    color: #F3C76A;
}
QStatusBar#appStatusBar[level="error"] {
    color: #F06470;
}
#appConnectionChip {
    color: #C0C8E0;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 10px;
    padding: 4px 10px;
    margin-left: 8px;
}
#appConnectionChip[connected="true"] {
    color: #6DDC8C;
    border-color: rgba(109, 220, 140, 0.35);
    background: rgba(109, 220, 140, 0.08);
}

/* ===== Sidebar ===== */
#sidebar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #1E2130, stop:1 #171A24);
    border-right: 1px solid rgba(255, 255, 255, 0.06);
}
#sidebarConnection {
    background: transparent;
}
#sidebarSectionLabel {
    color: #8890A8;
    font-size: 20px;
    font-weight: 600;
    letter-spacing: 0.02em;
    padding: 0;
    margin-top: 2px;
}
#sidebarBrand {
    color: #505672;
    font-size: 13px;
    font-weight: 500;
    letter-spacing: 0.04em;
    padding: 12px 0;
}

/* ===== Status dot ===== */
#statusDot {
    border-radius: 6px;
    background: #F06470;
    min-width: 12px;
    min-height: 12px;
}
#statusDot[connected="true"] {
    background: #6DDC8C;
    border: none;
}
#statusText {
    font-size: 18px;
    font-weight: 700;
    color: #C0C8E0;
    line-height: 1.3;
    letter-spacing: 0.03em;
}
#deviceCard {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: {panel_radius};
}
#deviceCardTitle {
    color: #FFFFFF;
    font-size: 22px;
    font-weight: 700;
}
#deviceCardMeta {
    color: #A0AACC;
    font-size: 14px;
}
#deviceCardHost {
    color: #C0C8E0;
    font-size: 14px;
    font-weight: 600;
}

#documentsSummaryLabel {
    color: #A0AACC;
    font-size: 13px;
    font-weight: 600;
}
#panelSectionLabel {
    color: #A0AACC;
    font-size: 18px;
    font-weight: 600;
    padding: 0;
}
#fontTargetName {
    color: #8890A8;
}
#fontPreviewPanel {
    background: rgba(15, 17, 25, 0.34);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: {inner_panel_radius};
}
#fontPreviewTitle {
    color: #A0AACC;
    font-size: 13px;
    font-weight: 600;
}
#fontPreviewSample {
    color: #FFFFFF;
    background: transparent;
    padding: 0;
}
#documentsEmptyState {
    color: #8890A8;
    border: 1px dashed rgba(255, 255, 255, 0.12);
    border-radius: {inner_panel_radius};
    padding: 24px;
    margin-top: 8px;
}

/* ===== Button variants ===== */
QPushButton[cssClass="secondary"] {
    background: rgba(255, 255, 255, 0.05);
    color: #C0C8E0;
    border: 1px solid rgba(255, 255, 255, 0.10);
}
QPushButton[cssClass="secondary"]:hover:!disabled {
    background: rgba(255, 255, 255, 0.09);
    border-color: rgba(255, 255, 255, 0.18);
    color: #FFFFFF;
}
QPushButton[cssClass="secondary"]:pressed:!disabled {
    background: rgba(255, 255, 255, 0.04);
}
QPushButton[cssClass="secondary"]:checked {
    background: rgba(91, 124, 247, 0.15);
    color: #8AB4FF;
    border-color: rgba(91, 124, 247, 0.4);
}
QPushButton[cssClass="secondary"]:disabled {
    background: rgba(255, 255, 255, 0.02);
    color: #3A3F54;
    border-color: rgba(255, 255, 255, 0.04);
}

QPushButton[cssClass="danger"] {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #D04858, stop:1 #B83A4A);
    color: #FFFFFF;
    border: none;
}
QPushButton[cssClass="danger"]:hover:!disabled {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #E05868, stop:1 #C84A5A);
}
QPushButton[cssClass="danger"]:pressed:!disabled {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #B83848, stop:1 #A02838);
}
QPushButton[cssClass="danger"]:disabled {
    background: rgba(208, 72, 88, 0.15);
    color: #5A6380;
}

QToolButton[cssClass="danger"] {
    color: #F06470;
    border-color: rgba(240, 100, 112, 0.25);
}
QToolButton[cssClass="danger"]:hover {
    background: rgba(240, 100, 112, 0.12);
    border-color: rgba(240, 100, 112, 0.4);
    color: #FF7A84;
}

/* ===== Theme toggle ===== */
#themeToggle,
#githubLinkButton {
    background: rgba(255, 255, 255, 0.06);
    color: #8890A8;
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 19px;
    padding: 0;
}
#themeToggle:hover,
#githubLinkButton:hover {
    background: rgba(255, 255, 255, 0.10);
    color: #C0C8E0;
    border-color: rgba(255, 255, 255, 0.18);
}
#themeToggle:pressed,
#githubLinkButton:pressed {
    background: rgba(255, 255, 255, 0.14);
}
"""

_LIGHT_STYLESHEET = """
/* ===== Global ===== */
* { outline: none; }
QMainWindow { background: #F0F2F5; }

/* ===== Group Box ===== */
QGroupBox {
    background: #FFFFFF;
    border: 1px solid #DDE0E6;
    border-radius: {panel_radius};
    margin-top: 0px;
    padding: 36px {panel_padding} {panel_padding} {panel_padding};
}
QGroupBox::title {
    subcontrol-origin: padding;
    subcontrol-position: top left;
    left: 16px;
    top: 10px;
    color: #5A6070;
    font-weight: 600;
    letter-spacing: 0.5px;
}

/* ===== Buttons ===== */
QPushButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #2563EB, stop:1 #0284C7);
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    padding: 10px 24px;
    font-weight: 600;
}
QPushButton:hover:!disabled {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #3B82F6, stop:1 #0EA5E9);
}
QPushButton:pressed:!disabled {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #1D4ED8, stop:1 #0369A1);
}
QPushButton:disabled {
    background: #E0E3E8;
    color: #A0A6B4;
}

QToolButton {
    background: #F0F2F5;
    color: #3A3F50;
    border: 1px solid #D0D4DC;
    border-radius: 8px;
    padding: 7px 10px;
}
QToolButton:hover {
    background: #E4E7ED;
    border-color: #B0B6C4;
    color: #1A1D27;
}

/* ===== Input Fields ===== */
QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {
    background: #FFFFFF;
    color: #1A1D27;
    border: 1px solid #D0D4DC;
    border-radius: {inner_panel_radius};
    padding: 8px 12px;
    selection-background-color: rgba(74, 108, 247, 0.25);
    selection-color: #1A1D27;
}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border-color: #4A6CF7;
}
QLineEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] {
    background: #F5F6F8;
    color: #6B7080;
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
    image: url({arrow_light});
    width: 16px;
    height: 16px;
}
QComboBox::down-arrow:on {
    image: url({arrow_light_up});
    width: 16px;
    height: 16px;
}
QComboBox QAbstractItemView {
    background: #FFFFFF;
    border: 1px solid #D0D4DC;
    border-radius: {inner_panel_radius};
    selection-background-color: rgba(74, 108, 247, 0.15);
    padding: 4px;
    outline: none;
}

/* ===== Checkbox & Radio ===== */
QCheckBox, QRadioButton { spacing: 8px; color: #3A3F50; }
QCheckBox::indicator, QRadioButton::indicator {
    width: 20px; height: 20px;
    border: 2px solid #B0B6C4;
    background: #FFFFFF;
}
QCheckBox::indicator { border-radius: 5px; }
QRadioButton::indicator { border-radius: 11px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #4A6CF7, stop:1 #2DAAF5);
    border-color: transparent;
}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {
    border-color: #4A6CF7;
}

/* ===== Tab Widget ===== */
QTabWidget::pane {
    background: #FFFFFF;
    border: 1px solid #DDE0E6;
    border-radius: {panel_radius};
    top: -1px;
}
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab {
    background: transparent;
    color: #8890A0;
    padding: 14px 28px;
    margin: 0 1px;
    border-bottom: 3px solid transparent;
    font-weight: 500;
}
QTabBar::tab:selected {
    color: #1A1D27;
    border-bottom: 3px solid #4A6CF7;
    background: rgba(74, 108, 247, 0.06);
}
QTabBar::tab:hover:!selected {
    color: #3A3F50;
    background: rgba(0, 0, 0, 0.03);
    border-bottom: 3px solid rgba(74, 108, 247, 0.2);
}

/* ===== Table ===== */
QTableWidget {
    background: #FFFFFF;
    alternate-background-color: #F8F9FB;
    border: 1px solid #DDE0E6;
    border-radius: {inner_panel_radius};
    gridline-color: #ECEEF2;
    selection-background-color: rgba(74, 108, 247, 0.12);
    selection-color: #1A1D27;
    padding: 2px;
}
QTableWidget::item {
    padding: 10px 12px;
    border-bottom: 1px solid #F0F2F5;
}
QTableWidget::item:selected {
    background: rgba(74, 108, 247, 0.10);
}
QHeaderView::section {
    background: #F5F6F8;
    color: #6B7080;
    padding: 10px 12px;
    border: none;
    border-bottom: 1px solid #DDE0E6;
    font-weight: 600;
}

/* ===== Labels ===== */
QLabel { color: #3A3F50; }
#previewImage {
    background: rgba(0, 0, 0, 0.04);
    border: 1px solid #DDE0E6;
    border-radius: {inner_panel_radius};
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
    background: #F5F6F8;
    border: 1px solid #E3E6EC;
    border-radius: {panel_radius};
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
    background: #DDE0E6;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    width: 20px; height: 20px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #4A6CF7, stop:1 #2DAAF5);
    border-radius: 10px;
    margin: -7px 0;
}
QSlider::sub-page:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4A6CF7, stop:1 #2DAAF5);
    border-radius: 3px;
}

/* ===== Scrollbars ===== */
QScrollBar:vertical {
    background: transparent; width: 10px; margin: 4px 2px;
}
QScrollBar::handle:vertical {
    background: rgba(0, 0, 0, 0.12); border-radius: 4px; min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: rgba(0, 0, 0, 0.22); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none; border: none; height: 0;
}
QScrollBar:horizontal {
    background: transparent; height: 10px; margin: 2px 4px;
}
QScrollBar::handle:horizontal {
    background: rgba(0, 0, 0, 0.12); border-radius: 4px; min-width: 32px;
}
QScrollBar::handle:horizontal:hover { background: rgba(0, 0, 0, 0.22); }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: none; border: none; width: 0;
}

/* ===== Splitter ===== */
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 8px; }
QSplitter::handle:vertical { height: 8px; }

/* ===== Progress ===== */
QProgressDialog { background: #FFFFFF; }
QProgressBar {
    background: #E8EAF0;
    border: none; border-radius: 6px; height: 10px;
    text-align: center; color: transparent;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4A6CF7, stop:1 #2DAAF5);
    border-radius: 5px;
}

/* ===== Tooltips ===== */
QToolTip {
    background: #FFFFFF;
    color: #1A1D27;
    border: 1px solid #D0D4DC;
    border-radius: 6px;
    padding: 6px 10px;
}

/* ===== Sliders ===== */
QSlider::groove:horizontal {
    height: 6px;
    background: #DDE0E6;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    width: 20px; height: 20px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #4A6CF7, stop:1 #2DAAF5);
    border-radius: 10px;
    margin: -7px 0;
}
QSlider::sub-page:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4A6CF7, stop:1 #2DAAF5);
    border-radius: 3px;
}

/* ===== Scrollbars ===== */
QScrollBar:vertical {
    background: transparent; width: 10px; margin: 4px 2px;
}
QScrollBar::handle:vertical {
    background: rgba(0, 0, 0, 0.12); border-radius: 4px; min-height: 32px;
}
QScrollBar::handle:vertical:hover { background: rgba(0, 0, 0, 0.22); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none; border: none; height: 0;
}
QScrollBar:horizontal {
    background: transparent; height: 10px; margin: 2px 4px;
}
QScrollBar::handle:horizontal {
    background: rgba(0, 0, 0, 0.12); border-radius: 4px; min-width: 32px;
}
QScrollBar::handle:horizontal:hover { background: rgba(0, 0, 0, 0.22); }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: none; border: none; width: 0;
}

/* ===== Splitter ===== */
QSplitter::handle { background: transparent; }
QSplitter::handle:horizontal { width: 8px; }
QSplitter::handle:vertical { height: 8px; }

/* ===== Progress ===== */
QProgressDialog { background: #FFFFFF; }
QProgressBar {
    background: #E8EAF0;
    border: none; border-radius: 6px; height: 10px;
    text-align: center; color: transparent;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #4A6CF7, stop:1 #2DAAF5);
    border-radius: 5px;
}

/* ===== Tooltips ===== */
QToolTip {
    background: #FFFFFF;
    color: #1A1D27;
    border: 1px solid #D0D4DC;
    border-radius: 6px;
    padding: 6px 10px;
}

/* ===== Message Box ===== */
QMessageBox { background: #F5F6F8; }

QStatusBar#appStatusBar {
    background: #FFFFFF;
    color: #3A3F50;
    border-top: 1px solid #DDE0E6;
}
QStatusBar#appStatusBar[level="success"] { color: #2E9B61; }
QStatusBar#appStatusBar[level="warning"] { color: #AF7A12; }
QStatusBar#appStatusBar[level="error"] { color: #C84A5A; }
#appConnectionChip {
    color: #3A3F50;
    background: #F5F6F8;
    border: 1px solid #DDE0E6;
    border-radius: 10px;
    padding: 4px 10px;
    margin-left: 8px;
}
#appConnectionChip[connected="true"] {
    color: #2E9B61;
    background: rgba(46, 155, 97, 0.08);
    border-color: rgba(46, 155, 97, 0.25);
}

/* ===== Sidebar ===== */
#sidebar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #F8F9FB, stop:1 #F0F2F5);
    border-right: 1px solid #DDE0E6;
}
#sidebarConnection { background: transparent; }
#sidebarSectionLabel {
    color: #6B7080;
    font-size: 20px;
    font-weight: 600;
    letter-spacing: 0.02em;
    padding: 0;
    margin-top: 2px;
}
#sidebarBrand {
    color: #B0B6C4;
    font-size: 13px;
    font-weight: 500;
    letter-spacing: 0.04em;
    padding: 12px 0;
}

/* ===== Status dot ===== */
#statusDot {
    border-radius: 6px;
    background: #E85B6B;
    min-width: 12px; min-height: 12px;
}
#statusDot[connected="true"] { background: #3CB870; border: none; }
#statusText {
    font-size: 18px;
    font-weight: 700;
    color: #3A3F50;
    line-height: 1.3;
    letter-spacing: 0.03em;
}
#deviceCard {
    background: #FFFFFF;
    border: 1px solid #E3E6EC;
    border-radius: {panel_radius};
}
#deviceCardTitle {
    color: #1A1D27;
    font-size: 22px;
    font-weight: 700;
}
#deviceCardMeta {
    color: #6B7080;
    font-size: 14px;
}
#deviceCardHost {
    color: #3A3F50;
    font-size: 14px;
    font-weight: 600;
}

#documentsSummaryLabel {
    color: #6B7080;
    font-size: 13px;
    font-weight: 600;
}
#panelSectionLabel {
    color: #5A6070;
    font-size: 18px;
    font-weight: 600;
    padding: 0;
}
#fontTargetName {
    color: #7A8092;
}
#fontPreviewPanel {
    background: #F7F8FB;
    border: 1px solid #DDE0E6;
    border-radius: {inner_panel_radius};
}
#fontPreviewTitle {
    color: #6B7080;
    font-size: 13px;
    font-weight: 600;
}
#fontPreviewSample {
    color: #1A1D27;
    background: transparent;
    padding: 0;
}
#documentsEmptyState {
    color: #6B7080;
    border: 1px dashed #D0D4DC;
    border-radius: {inner_panel_radius};
    padding: 24px;
    margin-top: 8px;
}
/* ===== Theme toggle ===== */
#themeToggle,
#githubLinkButton {
    background: #E8EAF0;
    color: #5A6070;
    border: 1px solid #D0D4DC;
    border-radius: 19px;
    padding: 0;
}
#themeToggle:hover,
#githubLinkButton:hover {
    background: #DDE0E6;
    color: #1A1D27;
}
#themeToggle:pressed,
#githubLinkButton:pressed {
    background: #D3D7DE;
}
"""


def _dark_palette() -> QtGui.QPalette:
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#1A1D27"))
    palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#E8ECF4"))
    palette.setColor(QtGui.QPalette.Base, QtGui.QColor("#0F1119"))
    palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#1E2130"))
    palette.setColor(QtGui.QPalette.Text, QtGui.QColor("#E0E6F0"))
    palette.setColor(QtGui.QPalette.Button, QtGui.QColor("#2A2E3C"))
    palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor("#E0E6F0"))
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#5B7CF7"))
    palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#FFFFFF"))
    palette.setColor(QtGui.QPalette.ToolTipBase, QtGui.QColor("#2A2E3C"))
    palette.setColor(QtGui.QPalette.ToolTipText, QtGui.QColor("#E0E6F0"))
    palette.setColor(QtGui.QPalette.Link, QtGui.QColor("#5B9CF7"))
    palette.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, QtGui.QColor("#5A6380"))
    palette.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.ButtonText, QtGui.QColor("#5A6380"))
    return palette


def _light_palette() -> QtGui.QPalette:
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#F0F2F5"))
    palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#1A1D27"))
    palette.setColor(QtGui.QPalette.Base, QtGui.QColor("#FFFFFF"))
    palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#F8F9FB"))
    palette.setColor(QtGui.QPalette.Text, QtGui.QColor("#1A1D27"))
    palette.setColor(QtGui.QPalette.Button, QtGui.QColor("#E8EAF0"))
    palette.setColor(QtGui.QPalette.ButtonText, QtGui.QColor("#1A1D27"))
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#4A6CF7"))
    palette.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("#FFFFFF"))
    palette.setColor(QtGui.QPalette.ToolTipBase, QtGui.QColor("#FFFFFF"))
    palette.setColor(QtGui.QPalette.ToolTipText, QtGui.QColor("#1A1D27"))
    palette.setColor(QtGui.QPalette.Link, QtGui.QColor("#4A6CF7"))
    palette.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, QtGui.QColor("#B0B6C4"))
    palette.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.ButtonText, QtGui.QColor("#B0B6C4"))
    return palette
