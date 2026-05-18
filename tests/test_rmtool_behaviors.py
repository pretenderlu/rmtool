import json
import os
import re
import stat
import struct
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import paramiko
from PIL import Image
from PyQt5 import QtCore, QtGui, QtWidgets

import rmrl
import rmtool
import _ssh
import _tab_connection
import _tab_documents
import _tab_wallpaper


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


class FakeWallpaperResourceClient(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self, files):
        super().__init__()
        self.files = files
        self.listdir_calls = []
        self.open_calls = []

    def is_connected(self):
        return True

    def listdir_attr(self, remote_path):
        self.listdir_calls.append(remote_path)
        prefix = remote_path.rstrip("/") + "/"
        children = []
        for path in self.files:
            if not path.startswith(prefix):
                continue
            child = path[len(prefix):]
            if "/" in child:
                continue
            children.append(SimpleNamespace(filename=child, st_mode=stat.S_IFREG | 0o644))
        if remote_path == "/usr/share/remarkable/carousel" and not children:
            raise IOError(remote_path)
        return children

    def open_remote(self, remote_path, _mode="rb"):
        self.open_calls.append(remote_path)
        if remote_path not in self.files:
            raise IOError(remote_path)

        @contextmanager
        def _remote_file():
            yield BytesIO(self.files[remote_path])

        return _remote_file()

    def file_exists(self, _remote_path):
        raise AssertionError("wallpaper scan should use directory listings first")


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
    def __init__(self, fail_on_put=None):
        self.remote_dirs = set()
        self.uploaded_files = {}
        self.fail_on_put = fail_on_put
        self.put_calls = 0

    def stat(self, path):
        if path == rmtool.DOCUMENT_ROOT or path in self.remote_dirs:
            return object()
        raise IOError(path)

    def mkdir(self, path):
        self.remote_dirs.add(path)

    def put(self, local_path, remote_path, callback=None):
        self.put_calls += 1
        if self.fail_on_put == self.put_calls:
            raise IOError(f"simulated upload failure for {remote_path}")
        data = Path(local_path).read_bytes()
        self.uploaded_files[remote_path] = data
        if callback:
            callback(len(data), len(data))


class FakeTransferSSHClient:
    def __init__(self, *, available_kb=1024 * 1024, fail_on_put=None):
        self.sftp = FakeUploadSFTP(fail_on_put=fail_on_put)
        self.available_kb = available_kb
        self.exec_calls = []
        self.restart_calls = []
        self.cleanup_calls = []

    @contextmanager
    def sftp_session(self):
        yield self.sftp

    def exec_checked(self, command):
        self.exec_calls.append(command)
        if command.startswith("df -Pk "):
            return (
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                f"/dev/root 2097152 0 {self.available_kb} 0% /\n"
            )
        if command == "systemctl restart xochitl":
            self.restart_calls.append(command)
            return ""
        if command.startswith("rm -rf "):
            self.cleanup_calls.append(command)
            return ""
        return ""

    def is_connected(self):
        return True


class FakeTransferDocumentsWidget:
    def __init__(self, ssh_client):
        self.ssh_client = ssh_client

    def _transfer_document(self, file_path, progress_callback=None):
        return rmtool.DocumentsTab._transfer_document(self, file_path, progress_callback)


class FakePreviewlessSFTP:
    def open(self, _path, _mode="rb"):
        raise IOError("preview not found")

    def listdir_attr(self, _path):
        raise IOError("preview directory not found")


class FakeExportSFTP:
    def __init__(self, files):
        self.files = files
        self.get_calls = []

    def listdir_attr(self, path):
        if path != rmtool.DOCUMENT_ROOT:
            raise IOError(path)
        return [
            SimpleNamespace(
                filename=remote_path.rsplit("/", 1)[-1],
                st_mode=stat.S_IFREG | 0o644,
            )
            for remote_path in self.files
        ]

    def get(self, remote_path, local_path):
        self.get_calls.append(remote_path)
        Path(local_path).write_bytes(self.files[remote_path])


class FakeExportSSHClient:
    def __init__(self, files):
        self.sftp = FakeExportSFTP(files)

    @contextmanager
    def sftp_session(self):
        yield self.sftp

    def _download_directory_recursive(self, _sftp, _remote_path, _local_path):
        raise AssertionError("directory download should not be used by this fixture")


class FakeRecursiveDownloadSFTP:
    def __init__(self):
        self.files = {
            "/remote/notebook/page-1.rm": b"page one",
            "/remote/notebook/nested/page-2.rm": b"page two",
        }

    def listdir_attr(self, remote_path):
        if remote_path == "/remote/notebook":
            return [
                SimpleNamespace(filename="page-1.rm", st_mode=stat.S_IFREG | 0o644),
                SimpleNamespace(filename="nested", st_mode=stat.S_IFDIR | 0o755),
            ]
        if remote_path == "/remote/notebook/nested":
            return [
                SimpleNamespace(filename="page-2.rm", st_mode=stat.S_IFREG | 0o644),
            ]
        raise IOError(remote_path)

    def get(self, remote_path, local_path):
        Path(local_path).write_bytes(self.files[remote_path])


class FakeDocumentsSSHClient(QtCore.QObject):
    connection_changed = QtCore.pyqtSignal(bool)

    def __init__(self, connected=True):
        super().__init__()
        self._connected = connected
        self.sftp = FakePreviewlessSFTP()
        self.exec_calls = []

    def is_connected(self):
        return self._connected

    @contextmanager
    def sftp_session(self):
        yield self.sftp

    def exec_checked(self, _command):
        self.exec_calls.append(_command)
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
            _tab_connection, "show_info"
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
    def test_connection_password_prompt_allows_remember_choice_without_keyring(self):
        config = rmtool._default_config()
        ssh_client = FakeConnectionClient()

        with mock.patch.object(rmtool, "save_config"), mock.patch.object(
            rmtool, "keyring", None
        ):
            widget = rmtool.ConnectionWidget(ssh_client, config)
            self.addCleanup(widget.deleteLater)
            dialog, controls = widget._make_password_dialog(config["devices"][0])

        self.assertTrue(controls["remember"].isEnabled())
        self.assertFalse(controls["remember"].isChecked())

    def test_edit_device_stores_password_when_remember_is_checked(self):
        config = rmtool._default_config()
        ssh_client = FakeConnectionClient()
        fake_keyring = mock.Mock()
        fake_keyring.get_password.return_value = ""
        credential_key = f"device:{config['devices'][0]['id']}"

        with mock.patch.object(rmtool, "save_config"), mock.patch.object(
            rmtool, "keyring", fake_keyring
        ), mock.patch.object(
            rmtool.ConnectionWidget,
            "_request_edit_device",
            return_value={
                "name": "默认设备",
                "mode": "usb",
                "host": "10.11.99.1",
                "type": "reMarkable Paper Pro",
                "password": "secret",
                "remember_password": True,
            },
        ):
            widget = rmtool.ConnectionWidget(ssh_client, config)
            self.addCleanup(widget.deleteLater)

            widget._edit_device()

        fake_keyring.set_password.assert_called_once_with(
            rmtool.KEYRING_SERVICE, credential_key, "secret"
        )

    def test_missing_keyring_warning_unchecks_remember_preference(self):
        config = rmtool._default_config()
        ssh_client = FakeConnectionClient()

        with mock.patch.object(rmtool, "save_config"), mock.patch.object(
            rmtool, "keyring", None
        ), mock.patch.object(_tab_connection, "show_warning") as warning:
            widget = rmtool.ConnectionWidget(ssh_client, config)
            self.addCleanup(widget.deleteLater)

            self.assertFalse(widget._sync_password_preference(config["devices"][0], "secret", True))

        warning.assert_called_once()
        self.assertIn("不支持", widget.credential_status_label.text())

    def test_disabling_remember_password_deletes_stored_secret(self):
        config = rmtool._default_config()
        ssh_client = FakeConnectionClient()
        fake_keyring = mock.Mock()
        fake_keyring.get_password.return_value = "old-secret"
        credential_key = f"device:{config['devices'][0]['id']}"

        with mock.patch.object(rmtool, "save_config"), mock.patch.object(
            rmtool, "keyring", fake_keyring
        ):
            widget = rmtool.ConnectionWidget(ssh_client, config)
            widget._sync_password_preference(config["devices"][0], "secret", False)

        fake_keyring.delete_password.assert_called_once_with(
            rmtool.KEYRING_SERVICE, credential_key
        )
        fake_keyring.set_password.assert_not_called()

    def test_legacy_name_password_is_migrated_to_device_credential_key(self):
        config = rmtool._default_config()
        device = config["devices"][0]
        credential_key = f"device:{device['id']}"
        fake_keyring = mock.Mock()
        fake_keyring.get_password.side_effect = lambda _service, account: (
            "old-secret" if account == device["name"] else None
        )

        with mock.patch.object(rmtool, "save_config"), mock.patch.object(
            rmtool, "keyring", fake_keyring
        ):
            widget = rmtool.ConnectionWidget(FakeConnectionClient(), config)
            self.addCleanup(widget.deleteLater)

        self.assertIn("已保存", widget.credential_status_label.text())
        fake_keyring.set_password.assert_called_once_with(
            rmtool.KEYRING_SERVICE, credential_key, "old-secret"
        )
        fake_keyring.delete_password.assert_called_once_with(
            rmtool.KEYRING_SERVICE, device["name"]
        )

    def test_saved_password_status_is_visible_and_forgettable(self):
        config = rmtool._default_config()
        device = config["devices"][0]
        credential_key = f"device:{device['id']}"
        fake_keyring = mock.Mock()
        fake_keyring.get_password.side_effect = lambda _service, account: (
            "secret" if account == credential_key else None
        )

        with mock.patch.object(rmtool, "save_config"), mock.patch.object(
            rmtool, "keyring", fake_keyring
        ):
            widget = rmtool.ConnectionWidget(FakeConnectionClient(), config)
            self.addCleanup(widget.deleteLater)

            self.assertIn("已保存", widget.credential_status_label.text())
            self.assertTrue(widget.forget_password_button.isEnabled())
            widget.forget_password_button.click()

        fake_keyring.delete_password.assert_called_once_with(
            rmtool.KEYRING_SERVICE, credential_key
        )
        self.assertIn("未保存", widget.credential_status_label.text())
        self.assertFalse(widget.forget_password_button.isEnabled())

    def test_missing_keyring_status_explains_unavailable_password_storage(self):
        config = rmtool._default_config()

        with mock.patch.object(rmtool, "save_config"), mock.patch.object(
            rmtool, "keyring", None
        ):
            widget = rmtool.ConnectionWidget(FakeConnectionClient(), config)
            self.addCleanup(widget.deleteLater)

        self.assertIn("不支持", widget.credential_status_label.text())
        self.assertFalse(widget.forget_password_button.isEnabled())


class ConnectionSidebarUiTests(unittest.TestCase):
    def test_sidebar_uses_dialogs_instead_of_inline_device_form(self):
        widget = rmtool.ConnectionWidget(FakeConnectionClient(), rmtool._default_config())
        self.addCleanup(widget.deleteLater)

        labels = {label.text() for label in widget.findChildren(QtWidgets.QLabel)}
        checkboxes = {checkbox.text() for checkbox in widget.findChildren(QtWidgets.QCheckBox)}
        radios = {radio.text() for radio in widget.findChildren(QtWidgets.QRadioButton)}
        buttons = {button.text() for button in widget.findChildren(QtWidgets.QToolButton)}

        self.assertNotIn("地址", labels)
        self.assertNotIn("设备类型", labels)
        self.assertNotIn("密码", labels)
        self.assertNotIn("记住密码", checkboxes)
        self.assertNotIn("USB", radios)
        self.assertNotIn("WiFi", radios)
        self.assertIn("编辑", buttons)
        self.assertNotIn("保存", buttons)

    def test_device_dialog_button_row_has_visual_margins(self):
        widget = rmtool.ConnectionWidget(FakeConnectionClient(), rmtool._default_config())
        self.addCleanup(widget.deleteLater)

        dialog, _controls = widget._make_device_details_dialog("新增设备")
        button_frame = dialog.findChild(QtWidgets.QWidget, "deviceDialogButtonFrame")
        margins = button_frame.layout().contentsMargins()

        self.assertGreaterEqual(margins.left(), 16)
        self.assertGreaterEqual(margins.right(), 16)
        self.assertGreaterEqual(margins.bottom(), 16)

    def test_add_device_dialog_result_creates_full_device_and_stores_password(self):
        config = rmtool._default_config()
        fake_keyring = mock.Mock()
        fake_keyring.get_password.return_value = ""

        with mock.patch.object(rmtool.uuid, "uuid4", return_value="new-device-id"), mock.patch.object(
            rmtool, "save_config"
        ) as save_config, mock.patch.object(rmtool, "keyring", fake_keyring), mock.patch.object(
            rmtool.ConnectionWidget,
            "_request_new_device",
            return_value={
                "name": "Paper Pro",
                "mode": "wifi",
                "host": "192.168.1.88",
                "type": "reMarkable Paper Pro",
                "password": "secret",
                "remember_password": True,
            },
        ):
            widget = rmtool.ConnectionWidget(FakeConnectionClient(), config)
            self.addCleanup(widget.deleteLater)

            widget._add_device()

        created = config["devices"][-1]
        self.assertEqual(created["id"], "new-device-id")
        self.assertEqual(created["name"], "Paper Pro")
        self.assertEqual(created["mode"], "wifi")
        self.assertEqual(created["host"], "192.168.1.88")
        self.assertEqual(config["active_device_id"], "new-device-id")
        self.assertEqual(widget.device_combo.currentText(), "Paper Pro")
        fake_keyring.set_password.assert_called_once_with(
            rmtool.KEYRING_SERVICE,
            "device:new-device-id",
            "secret",
        )
        self.assertGreaterEqual(save_config.call_count, 1)

    def test_edit_device_dialog_updates_current_device(self):
        config = rmtool._default_config()
        fake_keyring = mock.Mock()
        fake_keyring.get_password.return_value = ""

        with mock.patch.object(rmtool, "save_config") as save_config, mock.patch.object(
            rmtool, "keyring", fake_keyring
        ), mock.patch.object(
            rmtool.ConnectionWidget,
            "_request_edit_device",
            return_value={
                "name": "Updated",
                "mode": "wifi",
                "host": "192.168.1.99",
                "type": "reMarkable 2",
                "password": "",
                "remember_password": False,
            },
        ):
            widget = rmtool.ConnectionWidget(FakeConnectionClient(), config)
            self.addCleanup(widget.deleteLater)

            widget._edit_device()

        device = config["devices"][0]
        self.assertEqual(device["name"], "Updated")
        self.assertEqual(device["mode"], "wifi")
        self.assertEqual(device["host"], "192.168.1.99")
        self.assertEqual(device["type"], "reMarkable 2")
        self.assertEqual(config["active_device"], "Updated")
        self.assertGreaterEqual(save_config.call_count, 1)

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
        self.assertLessEqual(widget.minimumSizeHint().width(), 320)

    def test_edit_device_emits_non_modal_status_message(self):
        widget = rmtool.ConnectionWidget(FakeConnectionClient(), rmtool._default_config())
        received = []
        widget.status_message.connect(lambda level, text, timeout: received.append((level, text, timeout)))

        with mock.patch.object(
            widget,
            "_request_edit_device",
            return_value={
                "name": "默认设备",
                "mode": "usb",
                "host": "10.11.99.9",
                "type": "reMarkable Paper Pro",
                "password": "",
                "remember_password": False,
            },
        ):
            widget._edit_device()

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

    def test_wallpaper_page_scans_resources_when_shown_connected(self):
        widget = rmtool.WallpaperTab(FakeConnectionClient(connected=True), rmtool._default_config())
        self.addCleanup(widget.deleteLater)

        with mock.patch.object(widget, "_refresh_variant_previews") as refresh:
            widget.showEvent(QtGui.QShowEvent())

        refresh.assert_called_once_with()

    def test_wallpaper_variants_keep_legacy_sleeping_path_but_not_removed_hibernate_path(self):
        paths = [path for _key, _label, path in rmtool.WALLPAPER_VARIANTS]

        self.assertIn("/usr/share/remarkable/suspended.png", paths)
        self.assertNotIn("/usr/share/remarkable/hibernate.png", paths)
        self.assertIn("/usr/share/remarkable/sleeping.png", paths)

    def test_wallpaper_variants_include_paper_pro_sleep_carousel_resources(self):
        paths = [path for _key, _label, path in rmtool.WALLPAPER_VARIANTS]

        self.assertIn("/usr/share/remarkable/carousel/sleep_Illustration_01.png", paths)
        self.assertIn("/usr/share/remarkable/carousel/sleep_Illustration_02.png", paths)
        self.assertIn("/usr/share/remarkable/carousel/sleep_Illustration_03.png", paths)

    def test_wallpaper_legacy_hibernate_path_migrates_to_suspended(self):
        config = rmtool._default_config()
        config["paths"]["wallpaper"] = "/usr/share/remarkable/hibernate.png"

        widget = rmtool.WallpaperTab(FakeConnectionClient(), config)
        self.addCleanup(widget.deleteLater)

        self.assertEqual(config["paths"]["wallpaper"], "/usr/share/remarkable/suspended.png")
        self.assertTrue(widget.variant_buttons["suspended"].isChecked())
        self.assertIn("/usr/share/remarkable/suspended.png", widget.target_label.text())

    def test_wallpaper_legacy_sleeping_path_is_retained_for_old_firmware(self):
        config = rmtool._default_config()
        config["paths"]["wallpaper"] = "/usr/share/remarkable/sleeping.png"

        widget = rmtool.WallpaperTab(FakeConnectionClient(), config)
        self.addCleanup(widget.deleteLater)

        self.assertEqual(config["paths"]["wallpaper"], "/usr/share/remarkable/sleeping.png")
        self.assertTrue(widget.variant_buttons["sleeping"].isChecked())
        self.assertIn("/usr/share/remarkable/sleeping.png", widget.target_label.text())

    def test_missing_wallpaper_variant_is_marked_unavailable_without_removing_it(self):
        config = rmtool._default_config()
        config["paths"]["wallpaper"] = "/usr/share/remarkable/sleeping.png"
        widget = rmtool.WallpaperTab(FakeConnectionClient(connected=True), config)
        self.addCleanup(widget.deleteLater)
        widget._cached_source_image = Image.new("RGB", (10, 10), "white")
        widget._update_upload_button_state()

        widget._apply_variant_previews({
            "sleeping": _tab_wallpaper._WallpaperPreviewResult(missing=True),
        })

        self.assertEqual(widget.variant_previews["sleeping"].text(), "当前设备不存在")
        self.assertFalse(widget.variant_buttons["sleeping"].isEnabled())
        self.assertFalse(widget.upload_button.isEnabled())
        self.assertIn("当前设备不存在", widget.target_label.text())

    def test_wallpaper_preview_loading_is_driven_by_fast_device_scan(self):
        png = BytesIO()
        Image.new("RGBA", (2, 2), (255, 255, 255, 255)).save(png, format="PNG")
        files = {
            "/usr/share/remarkable/suspended.png": png.getvalue(),
            "/usr/share/remarkable/carousel/sleep_Illustration_01.png": png.getvalue(),
        }
        client = FakeWallpaperResourceClient(files)
        widget = rmtool.WallpaperTab(client, rmtool._default_config())
        self.addCleanup(widget.deleteLater)

        results = widget._download_all_variant_previews()

        self.assertIn("/usr/share/remarkable", client.listdir_calls)
        self.assertIn("/usr/share/remarkable/carousel", client.listdir_calls)
        self.assertFalse(results["suspended"].missing)
        self.assertFalse(results["sleep_carousel_1"].missing)
        self.assertTrue(results["sleeping"].missing)
        self.assertNotIn("/usr/share/remarkable/sleeping.png", client.open_calls)


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
    def test_default_config_assigns_stable_device_id(self):
        config = rmtool._default_config()

        self.assertTrue(config["devices"][0]["id"])
        self.assertEqual(config["active_device_id"], config["devices"][0]["id"])

    def test_load_config_adds_missing_device_ids_and_active_device_id(self):
        with tempfile.TemporaryDirectory() as temp_root:
            temp_root = Path(temp_root)
            app_state = temp_root / "appstate"
            app_state.mkdir()
            config = {
                "active_device": "Device B",
                "devices": [
                    {"name": "Device A", "mode": "usb", "host": "10.11.99.1", "type": "reMarkable 2"},
                    {"name": "Device B", "mode": "wifi", "host": "192.168.1.23", "type": "reMarkable Paper Pro"},
                ],
                "paths": {
                    "font": rmtool.DEFAULT_FONT_DIR,
                    "wallpaper": "/usr/share/remarkable/suspended.png",
                },
            }
            (app_state / "config.json").write_text(
                json.dumps(config, ensure_ascii=False),
                encoding="utf-8",
            )

            ids = iter(["device-a", "device-b"])
            with mock.patch.object(rmtool, "app_state_dir", return_value=app_state), mock.patch.object(
                rmtool.uuid, "uuid4", side_effect=lambda: next(ids)
            ):
                loaded = rmtool.load_config()

        self.assertEqual([device["id"] for device in loaded["devices"]], ["device-a", "device-b"])
        self.assertEqual(loaded["active_device_id"], "device-b")
        self.assertEqual(loaded["active_device"], "Device B")

    def test_load_config_merges_legacy_devices_when_app_state_has_only_default(self):
        with tempfile.TemporaryDirectory() as temp_root:
            temp_root = Path(temp_root)
            app_state = temp_root / "appstate"
            legacy_dir = temp_root / "legacy"
            app_state.mkdir()
            legacy_dir.mkdir()
            app_config = rmtool._default_config()
            legacy_config = {
                "active_device": "Paper Pro",
                "devices": [
                    {"name": "默认设备", "mode": "usb", "host": "10.11.99.1", "type": "reMarkable Paper Pro"},
                    {"name": "Paper Pro", "mode": "wifi", "host": "192.168.1.88", "type": "reMarkable Paper Pro"},
                    {"name": "RM2", "mode": "wifi", "host": "192.168.1.89", "type": "reMarkable 2"},
                ],
                "paths": {
                    "font": rmtool.DEFAULT_FONT_DIR,
                    "wallpaper": "/usr/share/remarkable/suspended.png",
                },
            }
            (app_state / "config.json").write_text(
                json.dumps(app_config, ensure_ascii=False),
                encoding="utf-8",
            )
            (legacy_dir / "config.json").write_text(
                json.dumps(legacy_config, ensure_ascii=False),
                encoding="utf-8",
            )

            with mock.patch.object(rmtool, "app_state_dir", return_value=app_state):
                with temporary_cwd(legacy_dir):
                    loaded = rmtool.load_config()

            names = [device["name"] for device in loaded["devices"]]
            self.assertEqual(names, ["默认设备", "Paper Pro", "RM2"])
            self.assertEqual(loaded["active_device"], "Paper Pro")
            self.assertTrue(loaded["legacy_devices_imported"])
            saved = json.loads((app_state / "config.json").read_text(encoding="utf-8"))
            self.assertEqual([device["name"] for device in saved["devices"]], names)

    def test_load_config_does_not_reimport_legacy_devices_after_import_marker(self):
        with tempfile.TemporaryDirectory() as temp_root:
            temp_root = Path(temp_root)
            app_state = temp_root / "appstate"
            legacy_dir = temp_root / "legacy"
            app_state.mkdir()
            legacy_dir.mkdir()
            app_config = rmtool._default_config()
            app_config["legacy_devices_imported"] = True
            legacy_config = {
                "active_device": "Paper Pro",
                "devices": [
                    {"name": "默认设备", "mode": "usb", "host": "10.11.99.1", "type": "reMarkable Paper Pro"},
                    {"name": "Paper Pro", "mode": "wifi", "host": "192.168.1.88", "type": "reMarkable Paper Pro"},
                ],
                "paths": {
                    "font": rmtool.DEFAULT_FONT_DIR,
                    "wallpaper": "/usr/share/remarkable/suspended.png",
                },
            }
            (app_state / "config.json").write_text(
                json.dumps(app_config, ensure_ascii=False),
                encoding="utf-8",
            )
            (legacy_dir / "config.json").write_text(
                json.dumps(legacy_config, ensure_ascii=False),
                encoding="utf-8",
            )

            with mock.patch.object(rmtool, "app_state_dir", return_value=app_state):
                with temporary_cwd(legacy_dir):
                    loaded = rmtool.load_config()

        self.assertEqual([device["name"] for device in loaded["devices"]], ["默认设备"])

    def test_load_config_prefers_app_state_file_over_legacy_file(self):
        with tempfile.TemporaryDirectory() as temp_root:
            temp_root = Path(temp_root)
            app_state = temp_root / "appstate"
            legacy_dir = temp_root / "legacy"
            app_state.mkdir()
            legacy_dir.mkdir()

            preferred_config = rmtool._default_config()
            preferred_config["active_device"] = "App Device"
            preferred_config["devices"][0]["name"] = "App Device"
            legacy_config = rmtool._default_config()
            legacy_config["active_device"] = "Legacy Device"
            legacy_config["devices"][0]["name"] = "Legacy Device"

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
            legacy_config["devices"][0]["name"] = "Migrated Device"
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

    def test_document_table_allows_multi_select_rows(self):
        widget = self._make_widget()

        self.assertEqual(
            widget.table.selectionMode(),
            QtWidgets.QAbstractItemView.ExtendedSelection,
        )

    def test_multi_selection_updates_summary_and_single_item_actions(self):
        widget = self._make_widget()
        widget.set_connection_state(True)
        documents = [
            self._make_document("Meeting Notes", ["pdf", "rm"], datetime(2026, 4, 15, 9, 0)),
            self._make_document("Book", ["epub"], datetime(2026, 4, 14, 8, 0)),
        ]

        widget._on_documents_loaded(documents)
        widget.table.selectRow(0)
        widget.table.selectionModel().select(
            widget.table.model().index(1, 0),
            QtCore.QItemSelectionModel.Select | QtCore.QItemSelectionModel.Rows,
        )
        self._drain_background_tasks(widget)

        self.assertEqual(
            [item.name for item in widget._selected_documents()],
            ["Meeting Notes", "Book"],
        )
        self.assertEqual(widget.selection_summary_label.text(), "已选择 2 个文档")
        self.assertTrue(widget.delete_button.isEnabled())
        self.assertFalse(widget.export_button.isEnabled())

    def test_batch_delete_removes_all_selected_documents_with_one_restart(self):
        widget = self._make_widget()
        widget.set_connection_state(True)
        documents = [
            self._make_document("Meeting Notes", ["pdf", "rm"], datetime(2026, 4, 15, 9, 0)),
            self._make_document("Book", ["epub"], datetime(2026, 4, 14, 8, 0)),
        ]

        widget._on_documents_loaded(documents)
        widget.table.selectRow(0)
        widget.table.selectionModel().select(
            widget.table.model().index(1, 0),
            QtCore.QItemSelectionModel.Select | QtCore.QItemSelectionModel.Rows,
        )

        with mock.patch.object(
            _tab_documents, "ask_confirmation", return_value=True
        ), mock.patch.object(_tab_documents, "show_info"), mock.patch.object(widget, "refresh"):
            widget._delete_document()
            self._drain_background_tasks(widget)

        delete_calls = [command for command in widget.ssh_client.exec_calls if command.startswith("rm -rf ")]
        self.assertEqual(len(delete_calls), 2)
        self.assertTrue(any("id-Meeting Notes" in command for command in delete_calls))
        self.assertTrue(any("id-Book" in command for command in delete_calls))
        self.assertEqual(
            [command for command in widget.ssh_client.exec_calls if command == "systemctl restart xochitl"],
            ["systemctl restart xochitl"],
        )

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

    def test_upload_finished_asks_before_restarting_xochitl(self):
        widget = self._make_widget()

        with mock.patch.object(widget, "_confirm_restart_after_upload", return_value=True) as confirm, mock.patch.object(
            widget, "_start_xochitl_restart_after_upload"
        ) as restart, mock.patch.object(widget, "refresh") as refresh:
            widget._on_upload_finished(2)

        confirm.assert_called_once_with(2)
        restart.assert_called_once_with()
        refresh.assert_not_called()

    def test_upload_finished_skips_restart_when_user_declines(self):
        widget = self._make_widget()

        with mock.patch.object(widget, "_confirm_restart_after_upload", return_value=False), mock.patch.object(
            widget, "_start_xochitl_restart_after_upload"
        ) as restart, mock.patch.object(widget, "refresh") as refresh:
            widget._on_upload_finished(1)

        restart.assert_not_called()
        refresh.assert_called_once_with()

    def test_restart_confirmation_uses_custom_dialog_and_requires_acceptance(self):
        widget = self._make_widget()
        fake_dialog = mock.Mock()
        fake_dialog.exec_.return_value = QtWidgets.QDialog.Accepted

        with mock.patch.object(widget, "_make_restart_confirmation_dialog", return_value=fake_dialog) as make_dialog, mock.patch.object(
            QtWidgets.QMessageBox, "question"
        ) as question:
            self.assertTrue(widget._confirm_restart_after_upload(3))

        make_dialog.assert_called_once_with(3)
        question.assert_not_called()

    def test_restart_confirmation_dialog_uses_app_styled_controls(self):
        widget = self._make_widget()

        dialog = widget._make_restart_confirmation_dialog(3)

        self.assertEqual(dialog.objectName(), "restartConfirmDialog")
        self.assertIsNotNone(dialog.findChild(QtWidgets.QFrame, "restartConfirmSurface"))
        self.assertIsNotNone(dialog.findChild(QtWidgets.QFrame, "restartConfirmNote"))
        title = dialog.findChild(QtWidgets.QLabel, "restartConfirmTitle")
        subtitle = dialog.findChild(QtWidgets.QLabel, "restartConfirmSubtitle")
        primary = dialog.findChild(QtWidgets.QPushButton, "restartConfirmPrimary")
        secondary = dialog.findChild(QtWidgets.QPushButton, "restartConfirmSecondary")
        self.assertIn("文档", title.text())
        self.assertIn("3", subtitle.text())
        self.assertEqual(primary.text(), "现在重启")
        self.assertTrue(primary.isDefault())
        self.assertEqual(secondary.text(), "稍后再说")

    def test_restart_confirmation_text_uses_readable_scale(self):
        def font_size(stylesheet, selector):
            match = re.search(
                rf"{re.escape(selector)}\s*\{{[^}}]*font-size:\s*(\d+)px;",
                stylesheet,
                re.DOTALL,
            )
            self.assertIsNotNone(match, selector)
            return int(match.group(1))

        for stylesheet in (rmtool._DARK_STYLESHEET, rmtool._LIGHT_STYLESHEET):
            self.assertGreaterEqual(font_size(stylesheet, "#restartConfirmSubtitle"), 14)
            self.assertGreaterEqual(font_size(stylesheet, "#restartConfirmBody"), 16)
            self.assertGreaterEqual(font_size(stylesheet, "#restartConfirmNoteText"), 15)

    def test_shared_dialog_text_uses_restart_dialog_readable_scale(self):
        def font_size(stylesheet, selector):
            match = re.search(
                rf"{re.escape(selector)}\s*\{{[^}}]*font-size:\s*(\d+)px;",
                stylesheet,
                re.DOTALL,
            )
            self.assertIsNotNone(match, selector)
            return int(match.group(1))

        for stylesheet in (rmtool._DARK_STYLESHEET, rmtool._LIGHT_STYLESHEET):
            self.assertGreaterEqual(font_size(stylesheet, "#appDialogTitle"), 21)
            self.assertGreaterEqual(font_size(stylesheet, "#appDialogBody"), 16)
            self.assertGreaterEqual(font_size(stylesheet, "#appDialogNoteText"), 15)

    def test_export_without_selection_shows_warning_instead_of_crashing(self):
        widget = self._make_widget()
        widget.set_connection_state(True)
        documents = [self._make_document("Meeting Notes", ["pdf", "rm"], datetime(2026, 4, 15, 9, 0))]

        widget._on_documents_loaded(documents)

        with mock.patch.object(_tab_documents, "show_warning") as warning:
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
    def test_ssh_recursive_download_creates_local_directory_tree(self):
        client = _ssh.SSHClientWrapper()
        sftp = FakeRecursiveDownloadSFTP()

        with tempfile.TemporaryDirectory() as temp_root:
            local_root = Path(temp_root) / "notebook"

            client._download_directory_recursive(sftp, "/remote/notebook", str(local_root))

            self.assertEqual((local_root / "page-1.rm").read_bytes(), b"page one")
            self.assertEqual((local_root / "nested" / "page-2.rm").read_bytes(), b"page two")

    def test_pdf_upload_uses_real_page_count_without_restarting_xochitl(self):
        ssh_client = FakeTransferSSHClient()
        widget = FakeTransferDocumentsWidget(ssh_client)

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
        self.assertEqual(ssh_client.restart_calls, [])

    def test_batch_upload_transfers_all_files_without_restarting_xochitl(self):
        ssh_client = FakeTransferSSHClient()
        widget = FakeTransferDocumentsWidget(ssh_client)

        with tempfile.TemporaryDirectory() as temp_root:
            first_path = Path(temp_root) / "first.pdf"
            second_path = Path(temp_root) / "second.pdf"
            first_path.write_bytes(TWO_PAGE_PDF)
            second_path.write_bytes(TWO_PAGE_PDF)

            with mock.patch.object(
                rmtool.uuid,
                "uuid4",
                side_effect=[
                    "11111111-1111-1111-1111-111111111111",
                    "22222222-2222-2222-2222-222222222222",
                ],
            ):
                rmtool.DocumentsTab._transfer_documents_batch(
                    widget, [str(first_path), str(second_path)]
                )

        self.assertEqual(ssh_client.restart_calls, [])
        self.assertTrue(
            any(path.endswith("11111111-1111-1111-1111-111111111111.pdf") for path in ssh_client.sftp.uploaded_files)
        )
        self.assertTrue(
            any(path.endswith("22222222-2222-2222-2222-222222222222.pdf") for path in ssh_client.sftp.uploaded_files)
        )

    def test_batch_upload_reports_aggregate_byte_progress(self):
        ssh_client = FakeTransferSSHClient()
        widget = FakeTransferDocumentsWidget(ssh_client)
        progress = []

        with tempfile.TemporaryDirectory() as temp_root:
            first_path = Path(temp_root) / "first.pdf"
            second_path = Path(temp_root) / "second.epub"
            first_path.write_bytes(TWO_PAGE_PDF)
            second_path.write_bytes(b"fake epub bytes")

            with mock.patch.object(
                rmtool.uuid,
                "uuid4",
                side_effect=[
                    "11111111-1111-1111-1111-111111111111",
                    "22222222-2222-2222-2222-222222222222",
                ],
            ):
                rmtool.DocumentsTab._transfer_documents_batch(
                    widget,
                    [str(first_path), str(second_path)],
                    progress_callback=lambda current, total: progress.append((current, total)),
                )

        self.assertGreater(progress[-1][1], 2)
        self.assertEqual(progress[-1][0], progress[-1][1])
        self.assertEqual(progress[-1][1], max(total for _current, total in progress))
        self.assertTrue(all(0 <= current <= total for current, total in progress))

    def test_upload_checks_device_space_before_transfer(self):
        ssh_client = FakeTransferSSHClient(available_kb=0)
        widget = FakeTransferDocumentsWidget(ssh_client)

        with tempfile.TemporaryDirectory() as temp_root:
            pdf_path = Path(temp_root) / "too-large.pdf"
            pdf_path.write_bytes(TWO_PAGE_PDF)

            with self.assertRaisesRegex(RuntimeError, "空间不足"):
                rmtool.DocumentsTab._transfer_document(widget, str(pdf_path))

        self.assertEqual(ssh_client.sftp.uploaded_files, {})
        self.assertEqual(ssh_client.restart_calls, [])

    def test_failed_upload_cleans_remote_partial_document_without_restart(self):
        ssh_client = FakeTransferSSHClient(fail_on_put=2)
        widget = FakeTransferDocumentsWidget(ssh_client)

        with tempfile.TemporaryDirectory() as temp_root:
            pdf_path = Path(temp_root) / "partial.pdf"
            pdf_path.write_bytes(TWO_PAGE_PDF)

            with mock.patch.object(
                rmtool.uuid,
                "uuid4",
                return_value="11111111-1111-1111-1111-111111111111",
            ):
                with self.assertRaises(IOError):
                    rmtool.DocumentsTab._transfer_document(widget, str(pdf_path))

        self.assertEqual(ssh_client.restart_calls, [])
        self.assertTrue(
            any(
                command.startswith("rm -rf ")
                and "11111111-1111-1111-1111-111111111111" in command
                for command in ssh_client.cleanup_calls
            )
        )

    def test_export_downloads_note_archive_and_renders_all_pages(self):
        identifier = "note-doc"
        archive_buffer = BytesIO()
        with zipfile.ZipFile(archive_buffer, "w") as archive:
            archive.writestr("pages/page-1.rm", build_rm_v5_page())
            archive.writestr("pages/page-2.rm", build_rm_v5_page())

        files = {
            f"{rmtool.DOCUMENT_ROOT}/{identifier}.content": json.dumps(
                {"pages": ["page-1", "page-2"], "pageDimensions": [1404, 1872]}
            ).encode("utf-8"),
            f"{rmtool.DOCUMENT_ROOT}/{identifier}.note": archive_buffer.getvalue(),
        }
        ssh_client = FakeExportSSHClient(files)
        widget = mock.Mock()
        widget.ssh_client = ssh_client
        item = rmtool.DocumentItem(
            identifier=identifier,
            name="Notebook",
            doc_type="DocumentType",
            updated=datetime(2026, 4, 15, 9, 0),
            available_assets=["note"],
        )

        with tempfile.TemporaryDirectory() as temp_root:
            output_pdf = Path(temp_root) / "export.pdf"
            rmtool.DocumentsTab._perform_export(widget, item, str(output_pdf))

            self.assertTrue(output_pdf.exists())
            self.assertEqual(rmtool.pdf_page_count(str(output_pdf)), 2)

        self.assertIn(f"{rmtool.DOCUMENT_ROOT}/{identifier}.note", ssh_client.sftp.get_calls)

    def test_embedded_rmrl_finds_nested_pages_from_content_order(self):
        with tempfile.TemporaryDirectory() as temp_root:
            notebook_root = Path(temp_root) / "notebook"
            nested = notebook_root / "nested"
            nested.mkdir(parents=True)
            (notebook_root / "page-1.rm").write_bytes(build_rm_v5_page())
            (nested / "page-2.rm").write_bytes(build_rm_v5_page())
            (notebook_root / "doc.content").write_text(
                json.dumps({"pages": ["page-1", "page-2"], "pageDimensions": [1404, 1872]}),
                encoding="utf-8",
            )
            output_pdf = Path(temp_root) / "export.pdf"

            rmrl.render_notebook_to_pdf(str(notebook_root), str(output_pdf), workspace=temp_root)

            self.assertTrue(output_pdf.exists())
            self.assertEqual(rmtool.pdf_page_count(str(output_pdf)), 2)

    def test_embedded_rmrl_uses_cpages_page_order(self):
        with tempfile.TemporaryDirectory() as temp_root:
            notebook_root = Path(temp_root) / "notebook"
            notebook_root.mkdir()
            first = notebook_root / "first-page.rm"
            second = notebook_root / "second-page.rm"
            first.write_bytes(build_rm_v5_page())
            second.write_bytes(build_rm_v5_page())
            (notebook_root / "doc.content").write_text(
                json.dumps(
                    {
                        "cPages": {
                            "pages": [
                                {"id": "second-page"},
                                {"id": "first-page"},
                            ]
                        },
                        "pageDimensions": [1404, 1872],
                    }
                ),
                encoding="utf-8",
            )

            pages = rmrl._collect_pages(notebook_root)

            self.assertEqual([page.path.name for page in pages], ["second-page.rm", "first-page.rm"])

    def test_embedded_rmrl_does_not_upscale_small_notebook_content(self):
        page = rmrl.PageInfo(path=Path("page.rm"), width=1404, height=1872)
        layers = [
            rmrl.Layer(
                [
                    rmrl.Stroke(
                        color=0,
                        brush=0,
                        segments=[
                            rmrl.Segment(100.0, 100.0, 4.0, 1.0, 0.0),
                            rmrl.Segment(300.0, 300.0, 4.0, 1.0, 0.0),
                        ],
                    )
                ]
            )
        ]

        image = rmrl._render_page(page, layers, (100.0, 100.0, 300.0, 300.0))
        bbox = image.point(lambda value: 0 if value > 245 else 255).getbbox()

        self.assertIsNotNone(bbox)
        left, top, right, bottom = bbox
        self.assertLess(right - left, 500)
        self.assertLess(bottom - top, 500)

    def test_embedded_rmrl_draws_each_stroke_as_one_polyline(self):
        class FakeDraw:
            def __init__(self):
                self.lines = []
                self.ellipses = []

            def line(self, points, fill=None, width=1, joint=None):
                self.lines.append((points, fill, width, joint))

            def ellipse(self, bounds, fill=None):
                self.ellipses.append((bounds, fill))

        draw = FakeDraw()
        layer = rmrl.Layer(
            [
                rmrl.Stroke(
                    color=0,
                    brush=0,
                    segments=[
                        rmrl.Segment(10.0, 10.0, 4.0, 1.0, 0.0),
                        rmrl.Segment(30.0, 30.0, 8.0, 1.0, 0.0),
                        rmrl.Segment(60.0, 10.0, 4.0, 1.0, 0.0),
                    ],
                )
            ]
        )

        rmrl._render_layer(draw, layer, 1.0, 0.0, 0.0)

        self.assertEqual(len(draw.lines), 1)
        self.assertEqual(len(draw.lines[0][0]), 3)
        self.assertEqual(draw.lines[0][3], "curve")

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
