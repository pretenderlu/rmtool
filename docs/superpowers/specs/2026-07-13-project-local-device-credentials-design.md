# Project-Local Device Credentials Design

## Problem

rmtool currently stores device profiles under the operating-system application data directory and optionally stores root passwords through `keyring`. In local source-based use this makes the connection state harder to understand and can require users to recreate devices or re-enter passwords. Multiple reMarkable devices also need independent profiles even though they may share the USB address `10.11.99.1`.

## Decision

- Store rmtool runtime state under `.rmtool/` beside `rmtool.py`, resolved from the source file location rather than the process working directory.
- Store the complete existing configuration structure in `.rmtool/devices.json` so device data, active selection, paths, and theme have one source of truth.
- Store a device password as an optional plaintext `password` field only when the user selects “remember password”.
- Keep SSH public-key trust in Paramiko's standard `.rmtool/known_hosts` file.
- Ignore the entire `.rmtool/` directory in Git.
- Remove the `keyring` dependency and all keyring migration or fallback behavior.
- Do not import the old `%APPDATA%/rmtool/config.json`, project-root `config.json`, keyring credentials, or `known_hosts`; leave them untouched for manual rollback.

## Local Files

```text
rmtool-main/
  .rmtool/
    devices.json
    known_hosts
    remarkable_tool.log
```

The log moves with the existing application-state directory because using one local state root is the smallest consistent path change. No file under `.rmtool/` is tracked by Git.

`devices.json` keeps the current configuration shape. A new installation starts with an empty device list:

```json
{
  "active_device_id": "",
  "active_device": "",
  "devices": [],
  "paths": {
    "font": "/home/root/.local/share/fonts/",
    "wallpaper": "/usr/share/remarkable/suspended.png"
  },
  "theme": "dark"
}
```

A remembered device may contain:

```json
{
  "id": "generated-uuid",
  "name": "My Paper Pro",
  "mode": "usb",
  "host": "10.11.99.1",
  "type": "reMarkable Paper Pro",
  "password": "plaintext-root-password"
}
```

When password storage is disabled, the `password` key is absent rather than present with an empty value.

## Startup And Device Selection

- On first startup, create `.rmtool/` and an empty `devices.json`.
- Do not create a placeholder or default device.
- With no saved devices, show an empty device selector and disable connection actions until the user adds a device.
- On later starts, load every saved device into the existing dropdown and restore the active selection when possible.
- Device selection remains explicit. The application does not poll for USB devices or automatically identify hardware.

## Password Flow

- Adding a device stores its name, mode, host, type, and generated UUID immediately.
- If “remember password” is selected, store the entered password in that device record.
- Connecting uses the selected record's password. If the field is absent, show the existing password dialog.
- Editing a device replaces the stored password only when remembering is enabled.
- Disabling “remember password”, clicking “forget password”, or deleting the device removes its stored password from `devices.json`.
- UI copy must say that the password is stored in the local project file, not in the system credential manager.

## SSH Host Trust

- Continue using Paramiko `HostKeys` and `RejectPolicy`; do not embed or manually serialize SSH public keys in JSON.
- Store each trusted key under an alias derived from the immutable device UUID, not the editable device name.
- The actual host/IP remains the network target. This allows different device profiles to share `10.11.99.1` while retaining separate trust records.
- The first connection still shows the fingerprint confirmation dialog.
- A key mismatch for the selected device is rejected. It must never silently use another device's password or replace a trusted key.
- Deleting a device does not clean old `known_hosts` entries in this MVP.

## Persistence And Failure Handling

- Write `devices.json` to a temporary file in `.rmtool/`, flush it, then use standard-library `os.replace()` for atomic replacement.
- If writing fails, preserve the previous valid file and report the failure.
- If `devices.json` contains invalid JSON, do not overwrite or reset it. Abort configuration loading with an error that names the file.
- If the project directory is not writable, fail explicitly; do not fall back to `%APPDATA%`.
- Keep the existing in-memory configuration dictionary so the rest of the application does not gain a second storage abstraction.

## Code Changes

- `rmtool.py`: resolve the project-local state directory, create an empty default configuration, load and atomically save `devices.json`, and remove keyring constants/imports.
- `_tab_connection.py`: read, write, and delete the optional device `password` field; support an empty device list; update credential status text.
- `_ssh.py`: derive trust aliases from device UUID while preserving the current host-key verification flow.
- `requirements.txt`: remove `keyring`.
- `.gitignore`: add `.rmtool/`.
- `README.md`: document project-local plaintext credentials, first-run behavior, and the Git-ignore boundary.
- `tests/test_rmtool_behaviors.py`: replace keyring tests and update configuration/host-trust expectations.

## Testing

- First startup creates an empty `devices.json` and no placeholder device.
- Multiple devices survive save/reload and appear in the dropdown.
- Remembered passwords survive restart; unremembered and forgotten passwords are absent from JSON.
- Deleting a device removes its password with the device record.
- Empty-device UI disables connection until a device is added.
- Configuration, `known_hosts`, and logs resolve under the source-local `.rmtool/` directory even when the process starts from another working directory.
- Device renaming preserves UUID-based SSH trust.
- Two profiles sharing `10.11.99.1` retain separate trusted keys.
- Unknown and mismatched keys still require confirmation or fail closed.
- Invalid JSON remains untouched and reports an error.
- A failed atomic save preserves the previous file.
- The complete existing test suite remains green.

## Out Of Scope

- Encryption, a master password, or operating-system credential storage.
- Automatic device detection or background USB/Wi-Fi polling.
- Importing old application-data, project-root configuration, or keyring credentials.
- SSH private-key authentication.
- Automatic cleanup of stale SSH trust entries.
- Cloud or Git synchronization of credentials.
