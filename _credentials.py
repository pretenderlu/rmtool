"""Remembered root passwords.

When the OS provides a secure credential store (Windows Credential Manager,
macOS Keychain, Secret Service on Linux) passwords are kept there, keyed by
the stable device id, and the device entry in ``devices.json`` only records
``"password_storage": "keyring"``.  Without a usable backend the previous
behaviour is kept: the password is stored in the local config file.
"""

import logging
import os
from typing import Dict, Optional

SERVICE_NAME = "rmtool"
STORAGE_KEY = "password_storage"
KEYRING_STORAGE = "keyring"
DISABLE_ENV = "RMTOOL_DISABLE_KEYRING"

LABEL_KEYRING = "已保存到系统凭据管理器"
LABEL_FILE = "已保存到项目本地文件"
LABEL_NONE = "未保存"

_backend_override = None  # tests may inject an object with keyring's API
# Off until the application entry point opts in, so unit tests and scripts
# that import the UI never touch the developer's real credential store.
_enabled = False


def enable() -> None:
    global _enabled
    _enabled = True


def _keyring():
    """Return a usable keyring module/backend, or None."""
    if _backend_override is not None:
        return _backend_override
    if not _enabled or os.environ.get(DISABLE_ENV):
        return None
    try:
        import keyring
        from keyring.backends import fail
    except Exception:  # missing dependency or broken install
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:
        logging.exception("Could not initialise the system keyring")
        return None
    if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 0) <= 0:
        return None
    # keyrings.alt plaintext/"encrypted" file backends are no better than
    # our own file, so treat them as unavailable.
    if type(backend).__module__.startswith("keyrings.alt"):
        return None
    return keyring


def secure_storage_available() -> bool:
    return _keyring() is not None


def _account(device: Dict) -> str:
    device_id = str(device.get("id") or "")
    if not device_id:
        raise ValueError("device id is required to store a password")
    return device_id


def load(device: Dict) -> str:
    if device.get(STORAGE_KEY) == KEYRING_STORAGE:
        backend = _keyring()
        if backend is None:
            logging.warning("Saved password is in the system keyring, which is unavailable")
            return ""
        try:
            return backend.get_password(SERVICE_NAME, _account(device)) or ""
        except Exception:
            logging.exception("Could not read password from the system keyring")
            return ""
    return str(device.get("password", ""))


def store(device: Dict, password: str) -> None:
    """Remember ``password`` for ``device`` (caller persists the config)."""
    backend = _keyring()
    if backend is not None and device.get("id"):
        try:
            backend.set_password(SERVICE_NAME, _account(device), password)
        except Exception:
            logging.exception("Could not write to the system keyring; using the config file")
        else:
            device.pop("password", None)
            device[STORAGE_KEY] = KEYRING_STORAGE
            return
    device.pop(STORAGE_KEY, None)
    device["password"] = password


def delete(device: Dict) -> bool:
    """Forget the password; return True if the device entry changed."""
    changed = False
    if device.pop(STORAGE_KEY, None) == KEYRING_STORAGE:
        changed = True
        backend = _keyring()
        if backend is not None:
            try:
                backend.delete_password(SERVICE_NAME, _account(device))
            except Exception:
                # Already absent, or backend failure: nothing else to do.
                logging.info("No keyring entry removed for device %s", device.get("id"))
    if "password" in device:
        device.pop("password")
        changed = True
    return changed


def has_saved(device: Dict) -> bool:
    return bool(load(device))


def status_label(device: Optional[Dict]) -> str:
    if not device or not has_saved(device):
        return LABEL_NONE
    if device.get(STORAGE_KEY) == KEYRING_STORAGE:
        return LABEL_KEYRING
    return LABEL_FILE


def migrate_plaintext(config: Dict) -> bool:
    """Move plaintext passwords from the config into the keyring.

    Returns True if the config changed and should be saved.
    """
    if _keyring() is None:
        return False
    changed = False
    for device in config.get("devices") or []:
        password = device.get("password")
        if password and device.get("id"):
            store(device, str(password))
            changed = changed or device.get(STORAGE_KEY) == KEYRING_STORAGE
    return changed
