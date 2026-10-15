import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets

import _credentials

_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FakeKeyring:
    def __init__(self, fail_set=False):
        self.store = {}
        self.fail_set = fail_set

    def get_password(self, service, account):
        return self.store.get((service, account))

    def set_password(self, service, account, password):
        if self.fail_set:
            raise RuntimeError("locked")
        self.store[(service, account)] = password

    def delete_password(self, service, account):
        if (service, account) not in self.store:
            raise KeyError(account)
        del self.store[(service, account)]


class CredentialsTests(unittest.TestCase):
    def use_backend(self, backend):
        patcher = mock.patch.object(_credentials, "_backend_override", backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disabled_by_default_so_tests_never_touch_real_keyring(self):
        with mock.patch.object(_credentials, "_enabled", False):
            self.assertIsNone(_credentials._keyring())

    def test_store_uses_keyring_and_keeps_secret_out_of_config(self):
        backend = FakeKeyring()
        self.use_backend(backend)
        device = {"id": "dev-1", "name": "A", "password": "old"}
        _credentials.store(device, " s3cret ")
        self.assertNotIn("password", device)
        self.assertEqual(device["password_storage"], "keyring")
        self.assertEqual(backend.store[("rmtool", "dev-1")], " s3cret ")
        self.assertEqual(_credentials.load(device), " s3cret ")
        self.assertEqual(_credentials.status_label(device), _credentials.LABEL_KEYRING)

    def test_falls_back_to_config_when_keyring_write_fails(self):
        self.use_backend(FakeKeyring(fail_set=True))
        device = {"id": "dev-1"}
        _credentials.store(device, "pw")
        self.assertEqual(device["password"], "pw")
        self.assertNotIn("password_storage", device)
        self.assertEqual(_credentials.status_label(device), _credentials.LABEL_FILE)

    def test_falls_back_to_config_without_backend(self):
        self.use_backend(None)
        with mock.patch.object(_credentials, "_enabled", False):
            device = {"id": "dev-1"}
            _credentials.store(device, "pw")
            self.assertEqual(device["password"], "pw")
            self.assertEqual(_credentials.load(device), "pw")

    def test_delete_removes_keyring_entry_and_marker(self):
        backend = FakeKeyring()
        self.use_backend(backend)
        device = {"id": "dev-1"}
        _credentials.store(device, "pw")
        self.assertTrue(_credentials.delete(device))
        self.assertEqual(backend.store, {})
        self.assertEqual(device, {"id": "dev-1"})
        self.assertFalse(_credentials.delete(device))
        self.assertEqual(_credentials.status_label(device), _credentials.LABEL_NONE)

    def test_missing_keyring_entry_reads_as_empty(self):
        self.use_backend(FakeKeyring())
        device = {"id": "dev-1", "password_storage": "keyring"}
        self.assertEqual(_credentials.load(device), "")
        self.assertEqual(_credentials.status_label(device), _credentials.LABEL_NONE)

    def test_migrate_plaintext_moves_all_devices(self):
        backend = FakeKeyring()
        self.use_backend(backend)
        config = {
            "devices": [
                {"id": "a", "password": "pa"},
                {"id": "b"},
                {"id": "c", "password": "pc"},
            ]
        }
        self.assertTrue(_credentials.migrate_plaintext(config))
        self.assertTrue(all("password" not in d for d in config["devices"]))
        self.assertEqual(backend.store, {("rmtool", "a"): "pa", ("rmtool", "c"): "pc"})
        self.assertFalse(_credentials.migrate_plaintext(config))

    def test_migrate_is_noop_without_backend(self):
        self.use_backend(None)
        with mock.patch.object(_credentials, "_enabled", False):
            config = {"devices": [{"id": "a", "password": "pa"}]}
            self.assertFalse(_credentials.migrate_plaintext(config))
            self.assertEqual(config["devices"][0]["password"], "pa")

    def test_connection_tab_shows_keyring_status(self):
        import rmtool
        import _tab_connection

        self.use_backend(FakeKeyring())
        config = rmtool._default_config()
        config["devices"] = [{"id": "dev-1", "name": "A", "mode": "usb", "host": "10.11.99.1"}]
        config["active_device_id"] = "dev-1"
        config["active_device"] = "A"
        class Client(QtCore.QObject):
            connection_changed = QtCore.pyqtSignal(bool)
            connection_info = {}

            def is_connected(self):
                return False

        client = Client()
        with mock.patch.object(rmtool, "save_config"):
            tab = _tab_connection.ConnectionWidget(client, config)
            self.addCleanup(tab.deleteLater)
            device = config["devices"][0]
            self.assertTrue(tab._store_password(device, "pw"))
            self.assertNotIn("password", device)
            self.assertEqual(tab.credential_status_label.text(), _credentials.LABEL_KEYRING)
            self.assertEqual(tab._load_password(device), "pw")
            tab._delete_password(device)
            self.assertEqual(tab._load_password(device), "")


if __name__ == "__main__":
    unittest.main()
