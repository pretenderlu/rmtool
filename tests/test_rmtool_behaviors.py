import json
import os
import struct
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import paramiko
from PyQt5 import QtCore, QtGui, QtWidgets

import rmrl
import rmtool


_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FakeConnectionClient(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self, *, connected=False, device_name="", host=""):
        super().__init__()
        self._connected = connected
        self.close_calls = 0
        self.connection_info = {"device_name": device_name, "host": host}

    def is_connected(self):
        return self._connected

    def close(self):
        self.close_calls += 1
        self._connected = False
        self.connection_changed.emit(False)


class FakeHostKey:
    def __init__(self, fingerprint=b"\x01\x23\x45\x67"):
        self._fingerprint = fingerprint

    def get_fingerprint(self):
        return self._fingerprint

    def get_name(self):
        return "ssh-ed25519"

    def get_base64(self):
        return "ZmFrZS1rZXk="


class FakeTransport:
    def __init__(self):
        self.keepalive = None

    def set_keepalive(self, value):
        self.keepalive = value

    def is_active(self):
        return True

    def send_ignore(self):
        return None


class FakeSSHClient:
    def __init__(self, exc=None):
        self.exc = exc
        self.transport = FakeTransport()
        self.policy = None
        self.loaded_host_keys = []
        self.connected_with = None
        self.host_keys = paramiko.HostKeys()

    def load_system_host_keys(self):
        return None

    def load_host_keys(self, path):
        self.loaded_host_keys.append(path)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def get_host_keys(self):
        return self.host_keys

    def connect(
        self,
        hostname,
        port=22,
        username=None,
        password=None,
        pkey=None,
        key_filename=None,
        timeout=None,
        allow_agent=True,
        look_for_keys=True,
        compress=False,
        sock=None,
        gss_auth=False,
        gss_kex=False,
        gss_deleg_creds=True,
        gss_host=None,
        banner_timeout=None,
        auth_timeout=None,
        channel_timeout=None,
        gss_trust_dns=True,
        passphrase=None,
        disabled_algorithms=None,
        transport_factory=None,
        auth_strategy=None,
    ):
        if self.exc:
            raise self.exc
        self.connected_with = {
            "hostname": hostname,
            "port": port,
            "username": username,
            "password": password,
            "timeout": timeout,
            "allow_agent": allow_agent,
            "look_for_keys": look_for_keys,
        }

    def get_transport(self):
        return self.transport

    def close(self):
        return None


class FakeUploadSFTP:
    def __init__(self):
        self.remote_dirs = set()
        self.uploaded_files = {}

    def stat(self, path):
        if path == rmtool.DOCUMENT_ROOT or path in self.remote_dirs:
            return object()
        raise IOError(path)

    def mkdir(self, path):
        self.remote_dirs.add(path)

    def put(self, local_path, remote_path, callback=None):
        data = Path(local_path).read_bytes()
        self.uploaded_files[remote_path] = data
        if callback:
            callback(len(data), len(data))


class FakeTransferSSHClient:
    def __init__(self):
        self.sftp = FakeUploadSFTP()
        self.exec_calls = []

    @contextmanager
    def sftp_session(self):
        yield self.sftp

    def exec_checked(self, command):
        self.exec_calls.append(command)

    def is_connected(self):
        return True


class FakePreviewlessSFTP:
    def open(self, _path, _mode="rb"):
        raise IOError("preview not found")

    def listdir_attr(self, _path):
        raise IOError("preview directory not found")


class FakeDocumentsSSHClient(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self, connected=True):
        super().__init__()
        self._connected = connected
        self.sftp = FakePreviewlessSFTP()

    def is_connected(self):
        return self._connected

    @contextmanager
    def sftp_session(self):
        yield self.sftp

    def exec_checked(self, _command):
        return None


class FakeDashboardTab(QtWidgets.QWidget):
    def update_device(self, _device):
        return None

    def update_documents(self, _summary):
        return None

    def update_connection(self, _connected, _device=None):
        return None

    def set_theme(self, _theme):
        return None


class FakeWallpaperTab(QtWidgets.QWidget):
    def __init__(self, *_args, **_kwargs):
        super().__init__()

    def update_device(self, _device):
        return None


class FakeToolboxTab(QtWidgets.QWidget):
    def __init__(self, *_args, **_kwargs):
        super().__init__()
        self.font_section = QtWidgets.QWidget()
        self.time_section = QtWidgets.QWidget()
        self.control_section = QtWidgets.QWidget()


@contextmanager
def temporary_cwd(path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


TWO_PAGE_PDF = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 5 0 R >>
endobj
4 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 6 0 R >>
endobj
5 0 obj
<< /Length 33 >>
stream
BT /F1 12 Tf (Page 1) Tj ET
endstream
endobj
6 0 obj
<< /Length 33 >>
stream
BT /F1 12 Tf (Page 2) Tj ET
endstream
endobj
trailer
<< /Root 1 0 R >>
%%EOF
"""


def build_rm_v5_page() -> bytes:
    header = b"reMarkable .lines file, version=" + b"5" + (b" " * 9) + b"\n"
    page = struct.pack("<BBH", 1, 0, 0)
    layer = struct.pack("<I", 1)
    stroke = struct.pack("<IIIfII", 0, 0, 0, 2.0, 0, 2)
    segments = b"".join(
        [
            struct.pack("<ffffff", 10.0, 10.0, 0.0, 0.0, 2.0, 1.0),
            struct.pack("<ffffff", 120.0, 160.0, 0.0, 0.0, 2.0, 1.0),
        ]
    )
    return header + page + layer + stroke + segments


def build_rm_v6_page() -> bytes:
    from io import BytesIO
    from uuid import uuid4

    import rmscene
    from rmscene import scene_items as si
    from rmscene import (
        AuthorIdsBlock,
        CrdtId,
        CrdtSequenceItem,
        LwwValue,
        MigrationInfoBlock,
        PageInfoBlock,
        SceneGroupItemBlock,
        SceneLineItemBlock,
        SceneTreeBlock,
        TreeNodeBlock,
        write_blocks,
    )

    blocks = [
        AuthorIdsBlock({1: uuid4()}),
        MigrationInfoBlock(CrdtId(1, 1), True),
        PageInfoBlock(1, 0, 0, 0),
        TreeNodeBlock(si.Group(node_id=CrdtId(0, 1))),
        SceneTreeBlock(CrdtId(0, 11), CrdtId(0, 0), True, CrdtId(0, 1)),
        TreeNodeBlock(si.Group(node_id=CrdtId(0, 11), label=LwwValue(CrdtId(0, 12), "Layer 1"))),
        SceneGroupItemBlock(
            parent_id=CrdtId(0, 1),
            item=CrdtSequenceItem(CrdtId(0, 13), CrdtId(0, 0), CrdtId(0, 0), 0, CrdtId(0, 11)),
        ),
        SceneLineItemBlock(
            parent_id=CrdtId(0, 11),
            item=CrdtSequenceItem(
                CrdtId(0, 14),
                CrdtId(0, 0),
                CrdtId(0, 0),
                0,
                si.Line(
                    color=si.PenColor.BLACK,
                    tool=si.Pen.BALLPOINT_1,
                    points=[
                        si.Point(10.0, 10.0, 0, 0, 8, 255),
                        si.Point(120.0, 160.0, 0, 0, 8, 255),
                    ],
                    thickness_scale=1.0,
                    starting_length=0.0,
                ),
            ),
        ),
    ]

    buf = BytesIO()
    write_blocks(buf, blocks)
    return buf.getvalue()


class DeviceSwitchTests(unittest.TestCase):
    def test_switching_devices_disconnects_existing_connection(self):
        config = {
            "active_device": "Device A",
            "devices": [
                {
                    "name": "Device A",
                    "mode": "usb",
                    "host": "10.11.99.1",
                    "type": "reMarkable Paper Pro",
                },
                {
                    "name": "Device B",
                    "mode": "wifi",
                    "host": "192.168.1.50",
                    "type": "reMarkable 2",
                },
            ],
            "paths": {
                "font": rmtool.DEFAULT_FONT_DIR,
                "wallpaper": "/usr/share/remarkable/suspended.png",
            },
        }
        ssh_client = FakeConnectionClient(
            connected=True,
            device_name="Device A",
            host="10.11.99.1",
        )

        with mock.patch.object(rmtool, "save_config"), mock.patch.object(
            QtWidgets.QMessageBox, "information"
        ) as info_box:
            widget = rmtool.ConnectionWidget(ssh_client, config)
            target_index = widget.device_combo.findText("Device B")
            widget.device_combo.setCurrentIndex(target_index)

        self.assertEqual(ssh_client.close_calls, 1)
        self.assertEqual(config["active_device"], "Device B")
        info_box.assert_called_once()


class RequireConnectionDecoratorTests(unittest.TestCase):
    def test_no_arg_slot_ignores_qt_clicked_bool(self):
        calls = []

        class Receiver:
            def __init__(self):
                self.ssh_client = mock.Mock()
                self.ssh_client.is_connected.return_value = True

            @rmtool.require_connection
            def action(self):
                calls.append("called")

        receiver = Receiver()

        receiver.action(False)

        self.assertEqual(calls, ["called"])


class PasswordPreferenceTests(unittest.TestCase):
    def test_disabling_remember_password_deletes_stored_secret(self):
        config = rmtool._default_config()
        ssh_client = FakeConnectionClient()
        fake_keyring = mock.Mock()
        fake_keyring.get_password.return_value = "old-secret"

        with mock.patch.object(rmtool, "save_config"), mock.patch.object(
            rmtool, "keyring", fake_keyring
        ):
            widget = rmtool.ConnectionWidget(ssh_client, config)
            widget._sync_password_preference("Device A", "secret", False)

        fake_keyring.delete_password.assert_called_once_with(
            rmtool.KEYRING_SERVICE, "Device A"
        )
        fake_keyring.set_password.assert_not_called()


class ConnectionSidebarUiTests(unittest.TestCase):
    def test_selecting_device_updates_summary_card_and_primary_action_text(self):
        config = {
            "active_device": "Device A",
            "devices": [
                {
                    "name": "Device A",
                    "mode": "usb",
                    "host": "10.11.99.1",
                    "type": "reMarkable Paper Pro",
                },
                {
                    "name": "Device B",
                    "mode": "wifi",
                    "host": "192.168.1.23",
                    "type": "reMarkable 2",
                },
            ],
            "paths": {
                "font": rmtool.DEFAULT_FONT_DIR,
                "wallpaper": "/usr/share/remarkable/suspended.png",
            },
        }
        widget = rmtool.ConnectionWidget(FakeConnectionClient(), config)

        widget.device_combo.setCurrentIndex(widget.device_combo.findText("Device B"))

        self.assertEqual(widget.device_title_label.text(), "Device B")
        self.assertIn("reMarkable 2", widget.device_meta_label.text())
        self.assertIn("Wi-Fi", widget.device_meta_label.text())
        self.assertIn("192.168.1.23", widget.device_host_label.text())
        self.assertIn("Device B", widget.connect_button.text())

    def test_narrow_sidebar_uses_compact_connect_label_and_shrinks_device_type_combo(self):
        app = QtWidgets.QApplication.instance()
        original_stylesheet = app.styleSheet()
        self.addCleanup(app.setStyleSheet, original_stylesheet)
        app.setStyleSheet(rmtool._resolve_stylesheet(rmtool._LIGHT_STYLESHEET))

        widget = rmtool.ConnectionWidget(FakeConnectionClient(), rmtool._default_config())
        widget.setFixedWidth(288)
        widget.resize(288, 900)
        widget.show()
        QtWidgets.QApplication.processEvents()

        self.assertTrue(widget.device_meta_label.wordWrap())
        self.assertTrue(widget.device_host_label.wordWrap())
        self.assertEqual(widget.connect_button.text(), "连接设备")
        self.assertEqual(widget.device_type_combo.currentText(), "Paper Pro")
        self.assertLessEqual(widget.device_type_combo.minimumSizeHint().width(), 220)
        self.assertLessEqual(widget.minimumSizeHint().width(), 320)

    def test_save_device_emits_non_modal_status_message(self):
        widget = rmtool.ConnectionWidget(FakeConnectionClient(), rmtool._default_config())
        received = []
        widget.status_message.connect(lambda level, text, timeout: received.append((level, text, timeout)))

        widget.host_edit.setText("10.11.99.9")
        widget._save_device()

        self.assertEqual(received, [("info", "已保存“默认设备”的连接配置。", 3000)])

    def test_sidebar_footer_uses_icon_actions_and_opens_github_repo(self):
        widget = rmtool.ConnectionWidget(FakeConnectionClient(), rmtool._default_config())
        self.addCleanup(widget.deleteLater)

        self.assertIsInstance(widget.theme_button, QtWidgets.QToolButton)
        self.assertEqual(widget.theme_button.text(), "")
        self.assertFalse(widget.theme_button.icon().isNull())

        self.assertIsInstance(widget.github_button, QtWidgets.QToolButton)
        self.assertEqual(widget.github_button.text(), "")
        self.assertFalse(widget.github_button.icon().isNull())
        self.assertEqual(widget.github_button.toolTip(), "打开 GitHub 仓库")

        with mock.patch.object(QtGui.QDesktopServices, "openUrl") as open_url:
            widget.github_button.click()

        open_url.assert_called_once()
        self.assertEqual(open_url.call_args.args[0].toString(), rmtool.GITHUB_REPO_URL)

    def test_sidebar_status_text_uses_readable_primary_scale(self):
        light = rmtool._resolve_stylesheet(rmtool._LIGHT_STYLESHEET)
        status_text_rule = light.split("#statusText {", 1)[1].split("}", 1)[0]

        self.assertIn("font-size: 18px;", status_text_rule)
        self.assertIn("line-height: 1.3;", status_text_rule)


class WallpaperUiTests(unittest.TestCase):
    def test_native_tab_pages_share_same_outer_content_inset(self):
        wallpaper = rmtool.WallpaperTab(FakeConnectionClient(), rmtool._default_config())
        documents = rmtool.DocumentsTab(FakeConnectionClient())
        toolbox = rmtool.ToolboxTab(FakeConnectionClient(), rmtool._default_config())
        self.addCleanup(wallpaper.deleteLater)
        self.addCleanup(documents.deleteLater)
        self.addCleanup(toolbox.deleteLater)

        expected = (rmtool.TAB_PAGE_MARGIN,) * 4

        wallpaper_margins = wallpaper.layout().contentsMargins()
        self.assertEqual(
            (
                wallpaper_margins.left(),
                wallpaper_margins.top(),
                wallpaper_margins.right(),
                wallpaper_margins.bottom(),
            ),
            expected,
        )

        documents_margins = documents.layout().contentsMargins()
        self.assertEqual(
            (
                documents_margins.left(),
                documents_margins.top(),
                documents_margins.right(),
                documents_margins.bottom(),
            ),
            expected,
        )

        toolbox_content = toolbox.findChild(QtWidgets.QScrollArea).widget()
        toolbox_margins = toolbox_content.layout().contentsMargins()
        self.assertEqual(
            (
                toolbox_margins.left(),
                toolbox_margins.top(),
                toolbox_margins.right(),
                toolbox_margins.bottom(),
            ),
            expected,
        )
        self.assertEqual(toolbox_content.layout().spacing(), rmtool.PANEL_GAP)

    def test_toolbox_group_boxes_leave_consistent_space_below_titles(self):
        toolbox = rmtool.ToolboxTab(FakeConnectionClient(), rmtool._default_config())
        self.addCleanup(toolbox.deleteLater)

        time_group = toolbox.time_section.parentWidget()
        control_group = toolbox.control_section.parentWidget()

        self.assertIsInstance(time_group, QtWidgets.QGroupBox)
        self.assertIsInstance(control_group, QtWidgets.QGroupBox)

        for group in (time_group, control_group):
            margins = group.layout().contentsMargins()
            self.assertEqual(
                (
                    margins.left(),
                    margins.top(),
                    margins.right(),
                    margins.bottom(),
                ),
                (0, rmtool.SUBSECTION_GAP, 0, 0),
            )

    def test_wallpaper_previews_use_rounded_content_corners(self):
        widget = rmtool.WallpaperTab(FakeConnectionClient(), rmtool._default_config())
        self.addCleanup(widget.deleteLater)

        self.assertEqual(widget.preview_label.corner_radius(), float(rmtool.INNER_PANEL_RADIUS))
        self.assertTrue(
            all(
                preview.corner_radius() == float(rmtool.INNER_PANEL_RADIUS)
                for preview in widget.variant_previews.values()
            )
        )

    def test_wallpaper_preview_area_uses_rounded_panel_container(self):
        widget = rmtool.WallpaperTab(FakeConnectionClient(), rmtool._default_config())
        self.addCleanup(widget.deleteLater)

        self.assertEqual(widget.objectName(), "wallpaperWorkspace")
        self.assertEqual(widget.control_panel.objectName(), "wallpaperControlPanel")
        self.assertEqual(widget.preview_panel.objectName(), "wallpaperPreviewPanel")
        self.assertIsInstance(widget.control_panel, QtWidgets.QFrame)
        self.assertIsInstance(widget.preview_panel, QtWidgets.QFrame)
        self.assertFalse(widget.findChildren(QtWidgets.QGroupBox))
        self.assertEqual(widget.variants_section_label.objectName(), "panelSectionLabel")
        control_margins = widget.control_panel.layout().contentsMargins()
        self.assertEqual(
            (control_margins.left(), control_margins.top(), control_margins.right(), control_margins.bottom()),
            (rmtool.PANEL_PADDING, rmtool.PANEL_PADDING, rmtool.PANEL_PADDING, rmtool.PANEL_PADDING),
        )
        margins = widget.preview_panel.layout().contentsMargins()
        self.assertEqual(
            (margins.left(), margins.top(), margins.right(), margins.bottom()),
            (rmtool.PANEL_PADDING, rmtool.PANEL_PADDING, rmtool.PANEL_PADDING, rmtool.PANEL_PADDING),
        )
        self.assertEqual(widget.control_inner.objectName(), "wallpaperControlInner")
        self.assertEqual(widget.control_scroll.viewport().objectName(), "wallpaperControlViewport")
        self.assertEqual(widget.main_splitter.handleWidth(), rmtool.PANEL_GAP)

    def test_wallpaper_workspace_biases_left_panel_width_on_startup(self):
        widget = rmtool.WallpaperTab(FakeConnectionClient(), rmtool._default_config())
        self.addCleanup(widget.deleteLater)

        widget.resize(1440, 900)
        widget.show()
        QtWidgets.QApplication.processEvents()

        left_size, right_size = widget.main_splitter.sizes()
        self.assertGreaterEqual(left_size, 780)
        self.assertGreater(left_size, right_size)


class FontUiTests(unittest.TestCase):
    def test_font_selection_previews_before_upload(self):
        widget = rmtool.FontTab(FakeConnectionClient(connected=True), rmtool._default_config())
        self.addCleanup(widget.deleteLater)

        with tempfile.TemporaryDirectory() as temp_root:
            font_path = Path(temp_root) / "preview-font.ttf"
            font_path.write_bytes(b"fake-font")

            with mock.patch.object(
                QtWidgets.QFileDialog,
                "getOpenFileName",
                return_value=(str(font_path), "字体文件 (*.ttf *.otf)"),
            ), mock.patch.object(
                QtGui.QFontDatabase,
                "addApplicationFont",
                return_value=7,
            ), mock.patch.object(
                QtGui.QFontDatabase,
                "applicationFontFamilies",
                return_value=["Preview Family"],
            ), mock.patch.object(widget.thread_pool, "start") as start_worker:
                widget.select_button.click()

        self.assertEqual(widget.font_path_label.text(), str(font_path))
        self.assertEqual(widget.preview_sample_label.font().family(), "Preview Family")
        self.assertTrue(widget.upload_button.isEnabled())
        self.assertFalse(start_worker.called)

    def test_font_upload_starts_only_after_confirm_click(self):
        widget = rmtool.FontTab(FakeConnectionClient(connected=True), rmtool._default_config())
        self.addCleanup(widget.deleteLater)

        with tempfile.TemporaryDirectory() as temp_root:
            font_path = Path(temp_root) / "preview-font.ttf"
            font_path.write_bytes(b"fake-font")

            with mock.patch.object(
                QtWidgets.QFileDialog,
                "getOpenFileName",
                return_value=(str(font_path), "字体文件 (*.ttf *.otf)"),
            ), mock.patch.object(
                QtGui.QFontDatabase,
                "addApplicationFont",
                return_value=7,
            ), mock.patch.object(
                QtGui.QFontDatabase,
                "applicationFontFamilies",
                return_value=["Preview Family"],
            ):
                widget.select_button.click()

        worker_instance = mock.Mock()
        worker_instance.signals = mock.Mock()
        with mock.patch.object(QtWidgets, "QProgressDialog") as progress_dialog, mock.patch.object(
            rmtool, "Worker", return_value=worker_instance
        ) as worker_cls, mock.patch.object(widget.thread_pool, "start") as start_worker:
            widget._upload_selected_font()

        progress_dialog.assert_called_once()
        worker_cls.assert_called_once_with(widget._upload_font, str(font_path), rmtool.DEFAULT_FONT_NAME)
        start_worker.assert_called_once_with(worker_instance)


class MainWindowUiTests(unittest.TestCase):
    def test_main_window_opens_wider_by_default(self):
        window = rmtool.MainWindow()
        self.addCleanup(window.deleteLater)

        self.assertGreaterEqual(window.size().width(), 1760)


class DashboardDesignTokenTests(unittest.TestCase):
    def test_dashboard_css_uses_shared_gap_and_radius_tokens(self):
        css = rmtool.resource_path("web", "dashboard.css").read_text(encoding="utf-8")

        self.assertIn(f"--panel-gap: {rmtool.PANEL_GAP}px;", css)
        self.assertIn(f"--radius-panel: {rmtool.PANEL_RADIUS}px;", css)
        self.assertIn(f"--radius-inner: {rmtool.INNER_PANEL_RADIUS}px;", css)

    def test_qt_stylesheets_use_shared_inner_radius_for_form_controls(self):
        expected_radius = f"border-radius: {rmtool.INNER_PANEL_RADIUS}px;"

        dark = rmtool._resolve_stylesheet(rmtool._DARK_STYLESHEET)
        dark_inputs = dark.split("QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {", 1)[1].split("}", 1)[0]
        dark_dropdown = dark.split("QComboBox QAbstractItemView {", 1)[1].split("}", 1)[0]

        light = rmtool._resolve_stylesheet(rmtool._LIGHT_STYLESHEET)
        light_inputs = light.split("QLineEdit, QComboBox, QPlainTextEdit, QTextEdit {", 1)[1].split("}", 1)[0]
        light_dropdown = light.split("QComboBox QAbstractItemView {", 1)[1].split("}", 1)[0]

        self.assertIn(expected_radius, dark_inputs)
        self.assertIn(expected_radius, dark_dropdown)
        self.assertIn(expected_radius, light_inputs)
        self.assertIn(expected_radius, light_dropdown)

    def test_dashboard_hero_uses_relaxed_spacing_and_line_height(self):
        css = rmtool.resource_path("web", "dashboard.css").read_text(encoding="utf-8")

        self.assertIn(".headline {", css)
        self.assertIn("gap: 12px;", css)
        self.assertIn("line-height: 1.75;", css)
        self.assertIn("margin-bottom: 8px;", css)

    def test_github_icon_uses_official_mark_path(self):
        self.assertEqual(
            rmtool.GITHUB_MARK_PATH,
            "M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z",
        )


class HostKeyVerificationTests(unittest.TestCase):
    def test_trust_identity_prefers_same_device_alias_for_usb_and_wifi(self):
        wrapper = rmtool.SSHClientWrapper()

        usb_identity = wrapper._trust_identity("10.11.99.1", "usb", "Device A")
        wifi_identity = wrapper._trust_identity("192.168.0.8", "wifi", "Device A")

        self.assertEqual(usb_identity, wifi_identity)
        self.assertNotEqual(usb_identity, "10.11.99.1")
        self.assertNotEqual(wifi_identity, "192.168.0.8")

    def test_connect_raises_unknown_host_key_error_for_untrusted_host(self):
        wrapper = rmtool.SSHClientWrapper()
        host_key = FakeHostKey(b"\x01\x23\x45\x67")
        unknown_host = paramiko.SSHException(
            "Server '10.11.99.1' not found in known_hosts"
        )

        with mock.patch.object(
            wrapper, "_build_client", return_value=FakeSSHClient(exc=unknown_host)
        ), mock.patch.object(
            wrapper, "_fetch_remote_host_key", return_value=host_key
        ):
            with self.assertRaises(rmtool.UnknownHostKeyError) as ctx:
                wrapper.connect("10.11.99.1", "secret")

        self.assertEqual(ctx.exception.host, "10.11.99.1")
        self.assertEqual(ctx.exception.fingerprint, "01:23:45:67")

    def test_system_known_hosts_entries_do_not_override_app_host_trust_flow(self):
        wrapper = rmtool.SSHClientWrapper()
        host_key = FakeHostKey(b"\x11\x22\x33\x44")
        legacy_key = FakeHostKey(b"\xaa\xbb\xcc\xdd")
        unknown_host = paramiko.SSHException(
            "Server '10.11.99.1' not found in known_hosts"
        )
        mismatch = paramiko.BadHostKeyException("10.11.99.1", host_key, legacy_key)
        client = FakeSSHClient(exc=unknown_host)
        system_host_keys_loaded = False

        def fake_load_system_host_keys():
            nonlocal system_host_keys_loaded
            system_host_keys_loaded = True
            client.exc = mismatch

        client.load_system_host_keys = fake_load_system_host_keys

        with mock.patch.object(
            rmtool.paramiko, "SSHClient", return_value=client
        ), mock.patch.object(
            wrapper, "_fetch_remote_host_key", return_value=host_key
        ):
            with self.assertRaises(rmtool.UnknownHostKeyError) as ctx:
                wrapper.connect(
                    "10.11.99.1",
                    "secret",
                    device_name="Device B",
                    connection_mode="usb",
                )

        self.assertEqual(ctx.exception.host, "10.11.99.1")
        self.assertEqual(ctx.exception.fingerprint, "11:22:33:44")
        self.assertFalse(system_host_keys_loaded)

    def test_connect_can_trust_and_persist_unknown_host(self):
        wrapper = rmtool.SSHClientWrapper()
        host_key = FakeHostKey(b"\xaa\xbb\xcc\xdd")
        unknown_host = paramiko.SSHException(
            "Server '10.11.99.1' not found in known_hosts"
        )
        first_client = FakeSSHClient(exc=unknown_host)
        second_client = FakeSSHClient()

        with mock.patch.object(
            wrapper, "_build_client", side_effect=[first_client, second_client]
        ), mock.patch.object(
            wrapper, "_fetch_remote_host_key", return_value=host_key
        ), mock.patch.object(
            wrapper, "_trust_host_key"
        ) as trust_host_key:
            wrapper.connect(
                "10.11.99.1",
                "secret",
                trust_unknown_host=True,
                device_name="Device A",
            )

        expected_identity = wrapper._trust_identity("10.11.99.1", "wifi", "Device A")
        trust_host_key.assert_called_once_with(expected_identity, host_key)
        self.assertIs(wrapper._client, second_client)
        self.assertEqual(wrapper.connection_info["device_name"], "Device A")
        self.assertEqual(second_client.transport.keepalive, 30)
        injected = second_client.get_host_keys().lookup("10.11.99.1")
        self.assertIsNotNone(injected)
        self.assertEqual(
            rmtool.host_key_fingerprint(next(iter(injected.values()))),
            rmtool.host_key_fingerprint(host_key),
        )

    def test_connect_treats_legacy_host_mismatch_as_retrust_for_selected_device(self):
        wrapper = rmtool.SSHClientWrapper()
        legacy_key = FakeHostKey(b"\xaa\xbb\xcc\xdd")
        actual_key = FakeHostKey(b"\x11\x22\x33\x44")
        mismatch = paramiko.BadHostKeyException("10.11.99.1", actual_key, legacy_key)

        with mock.patch.object(
            wrapper, "_lookup_trusted_host_key", return_value=("10.11.99.1", legacy_key)
        ), mock.patch.object(
            wrapper, "_build_client", return_value=FakeSSHClient(exc=mismatch)
        ), mock.patch.object(
            wrapper, "_fetch_remote_host_key", return_value=actual_key
        ):
            with self.assertRaises(rmtool.UnknownHostKeyError) as ctx:
                wrapper.connect(
                    "10.11.99.1",
                    "secret",
                    device_name="Device B",
                    connection_mode="usb",
                )

        self.assertEqual(ctx.exception.host, "10.11.99.1")
        self.assertEqual(ctx.exception.fingerprint, "11:22:33:44")

    def test_connect_can_retrust_legacy_host_mismatch_for_selected_device(self):
        wrapper = rmtool.SSHClientWrapper()
        legacy_key = FakeHostKey(b"\xaa\xbb\xcc\xdd")
        actual_key = FakeHostKey(b"\x11\x22\x33\x44")
        mismatch = paramiko.BadHostKeyException("10.11.99.1", actual_key, legacy_key)
        first_client = FakeSSHClient(exc=mismatch)
        second_client = FakeSSHClient()

        with mock.patch.object(
            wrapper, "_lookup_trusted_host_key", return_value=("10.11.99.1", legacy_key)
        ), mock.patch.object(
            wrapper, "_build_client", side_effect=[first_client, second_client]
        ), mock.patch.object(
            wrapper, "_fetch_remote_host_key", return_value=actual_key
        ), mock.patch.object(
            wrapper, "_trust_host_key"
        ) as trust_host_key:
            wrapper.connect(
                "10.11.99.1",
                "secret",
                trust_unknown_host=True,
                device_name="Device B",
                connection_mode="usb",
            )

        expected_identity = wrapper._trust_identity("10.11.99.1", "usb", "Device B")
        trust_host_key.assert_called_once_with(expected_identity, actual_key)
        self.assertIs(wrapper._client, second_client)
        self.assertEqual(wrapper.connection_info["device_name"], "Device B")
        injected = second_client.get_host_keys().lookup("10.11.99.1")
        self.assertIsNotNone(injected)
        self.assertEqual(
            rmtool.host_key_fingerprint(next(iter(injected.values()))),
            rmtool.host_key_fingerprint(actual_key),
        )

    def test_usb_connect_trusts_host_key_by_device_name(self):
        wrapper = rmtool.SSHClientWrapper()
        host_key = FakeHostKey(b"\x10\x20\x30\x40")
        unknown_host = paramiko.SSHException(
            "Server '10.11.99.1' not found in known_hosts"
        )
        first_client = FakeSSHClient(exc=unknown_host)
        second_client = FakeSSHClient()

        with mock.patch.object(
            wrapper, "_build_client", side_effect=[first_client, second_client]
        ), mock.patch.object(
            wrapper, "_fetch_remote_host_key", return_value=host_key
        ), mock.patch.object(
            wrapper, "_trust_host_key"
        ) as trust_host_key:
            wrapper.connect(
                "10.11.99.1",
                "secret",
                trust_unknown_host=True,
                device_name="Device A",
                connection_mode="usb",
            )

        expected_identity = wrapper._trust_identity("10.11.99.1", "usb", "Device A")
        trust_host_key.assert_called_once_with(expected_identity, host_key)
        injected = second_client.get_host_keys().lookup("10.11.99.1")
        self.assertIsNotNone(injected)
        self.assertEqual(
            rmtool.host_key_fingerprint(next(iter(injected.values()))),
            rmtool.host_key_fingerprint(host_key),
        )


class ExecCheckedContractTests(unittest.TestCase):
    def _make_wrapper_with_exit(self, exit_code, stderr_text="boom"):
        wrapper = rmtool.SSHClientWrapper()
        wrapper._client = object()
        wrapper.ensure_client = lambda: wrapper._client  # type: ignore[assignment]

        def fake_exec_command(_command):
            return "out", stderr_text, exit_code

        wrapper.exec_command = fake_exec_command  # type: ignore[assignment]
        return wrapper

    def test_exec_checked_returns_stdout_on_zero_exit(self):
        wrapper = self._make_wrapper_with_exit(0, stderr_text="")
        self.assertEqual(wrapper.exec_checked("true"), "out")

    def test_exec_checked_raises_runtime_error_on_nonzero_exit(self):
        wrapper = self._make_wrapper_with_exit(2, stderr_text="mount failed")
        with self.assertRaises(RuntimeError) as ctx:
            wrapper.exec_checked("mount -o remount,rw /")
        self.assertIn("mount failed", str(ctx.exception))


class ConfigMigrationTests(unittest.TestCase):
    def test_load_config_prefers_app_state_file_over_legacy_file(self):
        with tempfile.TemporaryDirectory() as temp_root:
            temp_root = Path(temp_root)
            app_state = temp_root / "appstate"
            legacy_dir = temp_root / "legacy"
            app_state.mkdir()
            legacy_dir.mkdir()

            preferred_config = rmtool._default_config()
            preferred_config["active_device"] = "App Device"
            legacy_config = rmtool._default_config()
            legacy_config["active_device"] = "Legacy Device"

            (app_state / "config.json").write_text(
                json.dumps(preferred_config, ensure_ascii=False),
                encoding="utf-8",
            )
            (legacy_dir / "config.json").write_text(
                json.dumps(legacy_config, ensure_ascii=False),
                encoding="utf-8",
            )

            with mock.patch.object(rmtool, "app_state_dir", return_value=app_state):
                with temporary_cwd(legacy_dir):
                    loaded = rmtool.load_config()

        self.assertEqual(loaded["active_device"], "App Device")

    def test_load_config_migrates_legacy_file_into_app_state(self):
        with tempfile.TemporaryDirectory() as temp_root:
            temp_root = Path(temp_root)
            app_state = temp_root / "appstate"
            legacy_dir = temp_root / "legacy"
            app_state.mkdir()
            legacy_dir.mkdir()

            legacy_config = rmtool._default_config()
            legacy_config["active_device"] = "Migrated Device"
            legacy_config["theme"] = "light"
            (legacy_dir / "config.json").write_text(
                json.dumps(legacy_config, ensure_ascii=False),
                encoding="utf-8",
            )

            with mock.patch.object(rmtool, "app_state_dir", return_value=app_state):
                with temporary_cwd(legacy_dir):
                    loaded = rmtool.load_config()

            migrated_path = app_state / "config.json"
            self.assertEqual(loaded["active_device"], "Migrated Device")
            self.assertTrue(migrated_path.exists())
            migrated = json.loads(migrated_path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["active_device"], "Migrated Device")
            self.assertEqual(migrated["theme"], "light")

    def test_save_config_writes_to_app_state_directory(self):
        config = rmtool._default_config()
        config["active_device"] = "Saved Device"

        with tempfile.TemporaryDirectory() as temp_root:
            temp_root = Path(temp_root)
            app_state = temp_root / "appstate"
            legacy_dir = temp_root / "legacy"
            app_state.mkdir()
            legacy_dir.mkdir()

            with mock.patch.object(rmtool, "app_state_dir", return_value=app_state):
                with temporary_cwd(legacy_dir):
                    rmtool.save_config(config)

            app_state_path = app_state / "config.json"
            legacy_path = legacy_dir / "config.json"
            self.assertTrue(app_state_path.exists())
            self.assertFalse(legacy_path.exists())
            saved = json.loads(app_state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["active_device"], "Saved Device")


class DocumentWorkspaceUiTests(unittest.TestCase):
    def _make_widget(self):
        widget = rmtool.DocumentsTab(FakeDocumentsSSHClient())
        self.addCleanup(QtWidgets.QApplication.processEvents)
        self.addCleanup(widget.deleteLater)
        self.addCleanup(lambda: widget.thread_pool.waitForDone(1000))
        self.addCleanup(QtWidgets.QApplication.processEvents)
        return widget

    def _drain_background_tasks(self, widget):
        QtWidgets.QApplication.processEvents()
        widget.thread_pool.waitForDone(1000)
        QtWidgets.QApplication.processEvents()

    def _make_document(self, name, assets, updated):
        return rmtool.DocumentItem(
            identifier=f"id-{name}",
            name=name,
            doc_type="DocumentType",
            updated=updated,
            available_assets=assets,
        )

    def test_documents_workspace_uses_card_panels_with_shared_gap(self):
        widget = self._make_widget()

        self.assertEqual(widget.list_panel.objectName(), "documentsListPanel")
        self.assertEqual(widget.preview_panel.objectName(), "documentsPreviewPanel")
        self.assertEqual(widget.content_splitter.handleWidth(), rmtool.PANEL_GAP)
        self.assertEqual(widget.preview_image.corner_radius(), float(rmtool.INNER_PANEL_RADIUS))

        list_margins = widget.list_panel.layout().contentsMargins()
        self.assertEqual(
            (list_margins.left(), list_margins.top(), list_margins.right(), list_margins.bottom()),
            (rmtool.PANEL_PADDING, rmtool.PANEL_PADDING, rmtool.PANEL_PADDING, rmtool.PANEL_PADDING),
        )

        preview_margins = widget.preview_panel.layout().contentsMargins()
        self.assertEqual(
            (
                preview_margins.left(),
                preview_margins.top(),
                preview_margins.right(),
                preview_margins.bottom(),
            ),
            (rmtool.PANEL_PADDING, rmtool.PANEL_PADDING, rmtool.PANEL_PADDING, rmtool.PANEL_PADDING),
        )

    def test_loaded_documents_update_counts_and_action_state(self):
        widget = self._make_widget()
        widget.set_connection_state(True)
        documents = [
            self._make_document("Meeting Notes", ["pdf", "rm"], datetime(2026, 4, 15, 9, 0)),
            self._make_document("Book", ["epub"], datetime(2026, 4, 14, 8, 0)),
        ]

        widget._on_documents_loaded(documents)

        self.assertEqual(widget.results_summary_label.text(), "显示 2 / 2 个文档")
        self.assertEqual(widget.selection_summary_label.text(), "未选择文档")
        self.assertFalse(widget.empty_state_label.isVisible())
        self.assertTrue(widget.upload_button.isEnabled())
        self.assertFalse(widget.delete_button.isEnabled())
        self.assertFalse(widget.export_button.isEnabled())

        widget.table.selectRow(0)
        self._drain_background_tasks(widget)

        self.assertTrue(widget.delete_button.isEnabled())
        self.assertTrue(widget.export_button.isEnabled())
        self.assertIn("Meeting Notes", widget.selection_summary_label.text())

    def test_filter_and_sort_keep_selected_document_mapping(self):
        widget = self._make_widget()
        widget.set_connection_state(True)
        documents = [
            self._make_document("Zulu", ["epub"], datetime(2026, 4, 15, 9, 0)),
            self._make_document("Alpha", ["pdf", "note"], datetime(2026, 4, 14, 8, 0)),
        ]

        widget._on_documents_loaded(documents)
        widget.table.sortItems(0, QtCore.Qt.AscendingOrder)
        widget.table.selectRow(0)
        self._drain_background_tasks(widget)

        self.assertEqual(widget._selected_document().name, "Alpha")

        widget._apply_filter("missing")

        self.assertEqual(widget.results_summary_label.text(), "显示 0 / 2 个文档")
        self.assertFalse(widget.empty_state_label.isHidden())
        self.assertEqual(widget.empty_state_label.text(), "没有匹配的文档，换个关键词试试。")

    def test_preview_failure_uses_non_modal_status_feedback(self):
        widget = self._make_widget()
        widget.set_connection_state(True)
        documents = [self._make_document("Broken Preview", ["pdf", "rm"], datetime(2026, 4, 15, 9, 0))]
        messages = []
        widget.status_message.connect(lambda level, text, timeout: messages.append((level, text, timeout)))

        widget._on_documents_loaded(documents)

        with mock.patch.object(widget, "_fetch_preview_cover", side_effect=RuntimeError("preview boom")), mock.patch.object(
            QtWidgets.QMessageBox, "critical"
        ) as critical:
            widget.table.selectRow(0)
            self._drain_background_tasks(widget)

        self.assertFalse(critical.called)
        self.assertEqual(widget.preview_image.text(), "暂无可用预览")
        self.assertIn(("warning", "文档预览加载失败，可继续查看元数据。", 3000), messages)

    def test_export_without_selection_shows_warning_instead_of_crashing(self):
        widget = self._make_widget()
        widget.set_connection_state(True)
        documents = [self._make_document("Meeting Notes", ["pdf", "rm"], datetime(2026, 4, 15, 9, 0))]

        widget._on_documents_loaded(documents)

        with mock.patch.object(QtWidgets.QMessageBox, "warning") as warning:
            widget._export_as_pdf()

        warning.assert_called_once_with(widget, rmtool.APP_NAME, "请先选择要导出的文档。")


class MainWindowUiTests(unittest.TestCase):
    def test_status_bar_receives_widget_status_messages(self):
        with mock.patch.object(rmtool, "DashboardTab", FakeDashboardTab), mock.patch.object(
            rmtool, "WallpaperTab", FakeWallpaperTab
        ), mock.patch.object(rmtool, "ToolboxTab", FakeToolboxTab), mock.patch.object(
            rmtool, "load_config", return_value=rmtool._default_config()
        ):
            window = rmtool.MainWindow()

        window.connection_widget.status_message.emit("info", "配置已保存", 0)
        QtWidgets.QApplication.processEvents()

        self.assertEqual(window.statusBar().currentMessage(), "配置已保存")

    def test_theme_toggle_updates_icon_tooltip_by_target_theme(self):
        config = rmtool._default_config()
        config["theme"] = "dark"

        with mock.patch.object(rmtool, "DashboardTab", FakeDashboardTab), mock.patch.object(
            rmtool, "WallpaperTab", FakeWallpaperTab
        ), mock.patch.object(rmtool, "ToolboxTab", FakeToolboxTab), mock.patch.object(
            rmtool, "load_config", return_value=config
        ):
            window = rmtool.MainWindow()
        self.addCleanup(window.deleteLater)

        self.assertEqual(window.connection_widget.theme_button.toolTip(), "切换到亮色主题")
        self.assertFalse(window.connection_widget.theme_button.icon().isNull())

        window._toggle_theme()

        self.assertEqual(window.connection_widget.theme_button.toolTip(), "切换到暗色主题")
        self.assertFalse(window.connection_widget.theme_button.icon().isNull())


class DocumentTransferTests(unittest.TestCase):
    def test_pdf_upload_uses_real_page_count(self):
        ssh_client = FakeTransferSSHClient()
        widget = type("FakeDocumentsWidget", (), {"ssh_client": ssh_client})()

        with tempfile.TemporaryDirectory() as temp_root:
            pdf_path = Path(temp_root) / "two-pages.pdf"
            pdf_path.write_bytes(TWO_PAGE_PDF)

            with mock.patch.object(
                rmtool.uuid,
                "uuid4",
                return_value="11111111-1111-1111-1111-111111111111",
            ):
                rmtool.DocumentsTab._transfer_document(widget, str(pdf_path))

        content_path = next(
            path for path in ssh_client.sftp.uploaded_files if path.endswith(".content")
        )
        content = json.loads(ssh_client.sftp.uploaded_files[content_path].decode("utf-8"))

        self.assertEqual(content["pageCount"], 2)
        self.assertEqual(ssh_client.exec_calls, ["systemctl restart xochitl"])

    def test_embedded_rmrl_can_render_valid_v5_rm_page(self):
        with tempfile.TemporaryDirectory() as temp_root:
            notebook_root = Path(temp_root) / "notebook"
            notebook_root.mkdir()
            (notebook_root / "page-1.rm").write_bytes(build_rm_v5_page())
            (notebook_root / "doc.content").write_text(
                json.dumps({"pages": ["page-1"], "pageDimensions": [1404, 1872]}),
                encoding="utf-8",
            )
            output_pdf = Path(temp_root) / "export.pdf"

            rmrl.render_notebook_to_pdf(str(notebook_root), str(output_pdf), workspace=temp_root)

            self.assertTrue(output_pdf.exists())
            self.assertGreater(output_pdf.stat().st_size, 0)

    def test_embedded_rmrl_can_render_valid_v6_rm_page(self):
        with tempfile.TemporaryDirectory() as temp_root:
            notebook_root = Path(temp_root) / "notebook"
            notebook_root.mkdir()
            (notebook_root / "page-1.rm").write_bytes(build_rm_v6_page())
            (notebook_root / "doc.content").write_text(
                json.dumps({"pages": ["page-1"], "pageDimensions": [1404, 1872]}),
                encoding="utf-8",
            )
            output_pdf = Path(temp_root) / "export.pdf"

            rmrl.render_notebook_to_pdf(str(notebook_root), str(output_pdf), workspace=temp_root)

            self.assertTrue(output_pdf.exists())
            self.assertGreater(output_pdf.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
