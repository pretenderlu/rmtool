**English | [简体中文](README.zh-CN.md)**

<div align="center">

<img src="assets/rmtool-icon.png" alt="rmtool icon" width="120">

# rmtool

A desktop GUI management tool for reMarkable devices

</div>

rmtool manages reMarkable Paper Pro, Paper Pro Move, and reMarkable 2 devices over local root SSH. It provides multi-device connections, a dashboard, wallpaper and document management, font upload, time management, device controls, and native Chinese UI localization. Device operations do not depend on reMarkable cloud services. The computer needs internet access the first time it retrieves a localization manifest or firmware package; a previously validated cache can be reused offline.

> [!WARNING]
> rmtool directly modifies files on the device. Sync or back up important content first, and make sure you accept the data and warranty risks associated with Developer Mode, root SSH, and third-party modifications. This project is not official reMarkable software.

## Download and installation

Most users should download the [latest release](https://github.com/pretenderlu/rmtool/releases/latest). Python is not required.

| Platform | Download | Notes |
| --- | --- | --- |
| Windows x64 | [Portable ZIP](https://github.com/pretenderlu/rmtool/releases/latest/download/rmtool-windows-x64.zip) | Extract it and run `rmtool/rmtool.exe`; recommended for regular use |
| Windows x64 | [Single-file EXE](https://github.com/pretenderlu/rmtool/releases/latest/download/rmtool-windows-x64-onefile.exe) | Run it directly; first launch and each cold start are slower |
| macOS ARM64 | [Apple Silicon app](https://github.com/pretenderlu/rmtool/releases/latest/download/rmtool-macos-arm64.app.zip) | M-series Macs only; extract it and run `rmtool.app` |

The release packages are currently neither Windows code-signed nor Apple-notarized. If SmartScreen or Gatekeeper blocks the app, first verify that the file came from this repository's release page, then use the operating system's one-time approval option. Do not disable system security globally.

The macOS build creates `.rmtool/` next to the `.app`. Place the app in a directory writable by the current user, such as `~/Applications/rmtool/`, instead of running it from a read-only location.

## Connecting a device

### SSH prerequisites

- The device must allow SSH login as `root`, and you must be able to view its current root password.
- Paper Pro and Paper Pro Move must first be put into Developer Mode. Enabling it performs a factory reset, removes local data from the device, and weakens device security, so sync or back up first. See the [official reMarkable documentation](https://developer.remarkable.com/documentation/developer-mode) for the procedure and risks. reMarkable 2 does not have this Developer Mode option, but it still requires working root SSH access.
- The default USB address is `10.11.99.1`. Connect the device to the computer over USB and select USB mode on the device.
- Wi-Fi SSH is disabled by default. Connect over USB first, then choose "Enable Wi-Fi SSH" under "Device Toolbox > Device Control" and change the saved device address to its WLAN address.
- On Paper Pro, the root username and password are available under `General > Help > About > Copyrights and Licenses`. For other models or firmware versions, follow the current device UI.

### First connection

1. Start rmtool, click "Add" in the sidebar, and enter the device name, connection method, address, model, and root password.
2. Click "Connect". The first connection displays the SSH host fingerprint; trust it only after confirming that it belongs to your device.
3. After a successful connection, the wallpaper, document, and toolbox pages are enabled automatically.
4. Multiple devices can have separate saved profiles. Switching to another device or address automatically closes the existing SSH connection.

## Local data and security

rmtool stores runtime state in `.rmtool/` next to the application:

| Run mode | `.rmtool/` location |
| --- | --- |
| From source | Repository root |
| Windows release | Directory containing `rmtool.exe` or the single-file EXE |
| macOS release | Directory containing `rmtool.app` |

The main files are:

- `devices.json`: device profiles, current device, theme, paths, and log-panel settings.
- `known_hosts`: SSH host trust records isolated by device ID.
- `remarkable_tool.log`: rotating runtime log.
- `cache/localization/`: validated localization manifests and firmware-package cache.

> [!CAUTION]
> When "Remember password" is selected, the root password is stored in **plain text** in `.rmtool/devices.json`; it is not stored in the operating system credential manager. Do not share, upload, or sync the entire `.rmtool/` directory to an untrusted location, and do not attach it to an issue. Use "Forget password" in the sidebar to remove a saved password.

## Features

- **Connections and dashboard**: Manage multiple USB/Wi-Fi device profiles and verify SSH host fingerprints. The local HTML dashboard shows connection status, device details, PDF/EPUB/notebook counts, and suggested next steps.
- **Wallpaper management**: Read and preview the device's current startup, suspend, carousel, and shutdown wallpapers. Output is sized for the device and orientation, with fit, crop, and stretch modes plus horizontal and vertical crop offsets.
- **Document center**: Search and inspect document metadata and thumbnails; batch-upload PDF/EPUB files, check free space, and batch-delete documents. Export parseable handwriting from `.rm` or `.note` data in one document to a white-background PDF without merging the original PDF/EPUB pages.
- **Font upload**: Preview and upload TTF/OTF fonts, optionally rename one to `zwzt.ttf`, write it to the user font directory with a fontconfig configuration, refresh the font cache, and prompt for a restart.
- **Time management**: Sync the computer's time, inspect system time, hardware clock, and timezone, or set the timezone to `Asia/Shanghai`.
- **Device control**: Restart the device, enable Wi-Fi SSH, and increase frontlight brightness on devices with the `rm_frontlight` interface while installing a persistence service.
- **Theme and logs**: Light and dark themes are persisted. The bottom log panel supports level filtering, pause, automatic scrolling, clearing, and opening the log file.
- **Third-party application links**: The toolbox links to documentation for vellum, xovi, rm-appload, and KOReader. It does not include one-click installers.

### Wallpaper notes

Before each upload, the target file is copied to `.backup` in the same directory; another upload overwrites that backup. When uploading the suspend wallpaper `suspended.png`, rmtool also replaces existing `carousel/*.png` files with transparent images so firmware 3.27 carousel artwork does not cover the custom wallpaper. Those carousel images are not backed up separately. If you need to preserve them, back them up over SSH first.

### Native Chinese UI localization

Release packages do not embed firmware-specific `.qm` files. After you choose "Device Toolbox > System Localization > Check Status", rmtool:

1. Retrieves the manifest from the fixed `localization-assets` release and falls back to a previously validated local cache when the network is unavailable.
2. Matches the exact 14-digit internal firmware version from `/etc/version`.
3. Calculates the SHA-256 of the device's original French carrier file, `reMarkable_fr.qm`, and uses it to select the correct hardware payload. Platform names such as `chiappa` and `ferrari` are display labels only; they are not used to guess compatibility.
4. Verifies the download size and SHA-256. Nothing is written to the device if the firmware, original French file, or checksum does not match.

The current repository manifest contains `chiappa` (Paper Pro Move) and `ferrari` (Paper Pro) payloads for stable firmware `3.27.1.0` (internal version `20260506100933`) and `3.27.3.0` (internal version `20260612085811`), plus beta firmware `3.28.0.162` (internal version `20260629074044`). On the beta firmware, enable and restore have been verified on a real Paper Pro (`ferrari`). Paper Pro Move (`chiappa`) has only been verified offline against the official firmware and has not yet been deployed to a real Move. The cloud manifest remains the source of truth for actual availability. See the [localization documentation](translations/README.md) and [manifest format](translations/manifest.json).

Localization reuses xochitl's built-in French language slot, so French is unavailable while Chinese is enabled. rmtool first backs up the original configuration and `reMarkable_fr.qm`, then checks whether the current primary font supports Simplified Chinese. If it does not, you can install the bundled Noto Sans CJK SC or select a local TTF/OTF file. After enabling localization, repairing fonts, or restoring the original UI, rmtool closes SSH and **does not restart the device automatically**. Restart the device manually to apply the change.

## Usage recommendations

1. After connecting, confirm the current device and connection method on the dashboard.
2. On the wallpaper page, run "Rescan" first, choose a target that actually exists on the device, then preview and upload.
3. After uploading documents, you can restart xochitl immediately when prompted. If you skip it, new documents may not appear yet.
4. Document deletion cannot be undone. PDF export only works for one document containing `.rm` or `.note` handwriting data, and the result excludes the original PDF/EPUB background and non-handwriting content.
5. Font and localization changes are device-level modifications. Restart the device when prompted after they finish.

## Troubleshooting

- **Connection fails**: Check that the USB network interface appears, the address is `10.11.99.1`, the root password is current, and SSH is allowed on the device. Wi-Fi connections also require Wi-Fi SSH to be enabled over USB first.
- **SSH fingerprint changed**: A system update, device reset, or reuse of the same address by another device can trigger this warning. Verify the device identity before trusting the new fingerprint.
- **Wallpaper target unavailable**: Different firmware versions provide different wallpaper files. Click "Rescan" and choose a target that has a preview and is not marked as missing from the current device.
- **Uploaded document does not appear on the device**: Return to the document center and restart xochitl, or restart the device manually.
- **"Export to PDF" is unavailable**: Select exactly one document containing `.rm` or `.note` handwriting resources. Export renders only parseable handwriting and does not merge original PDF/EPUB pages, typed text, or other non-handwriting content.
- **Localization buttons are disabled**: Click "Check Status" first. The computer needs internet access or a valid cache, and the internal firmware version plus the SHA-256 of the original `reMarkable_fr.qm` must match the same manifest entry.
- **macOS cannot create its configuration**: Move `rmtool.app` to a directory writable by the current user and make sure `.rmtool/` can be created beside it.
- **Diagnostic information is needed**: Click the log button in the lower-left corner, filter by level, or choose "Open Log File". Before sharing a log, check it for private information such as the device address.

## Running from source

Use 64-bit Python 3.12 to match the release workflow. Other Python versions are not covered by the current CI configuration.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python rmtool.py
```

macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python rmtool.py
```

On Windows, after installing dependencies, you can also double-click `rmtool.bat` to launch with `pythonw.exe` without keeping a console window open. See [requirements.txt](requirements.txt) for pinned dependency versions.

## Development and release checks

```bash
python -m compileall -q rmtool.py _dialogs.py _log_viewer.py _rmkit_cn.py _ssh.py _styles.py _tab_connection.py _tab_documents.py _tab_toolbox.py _tab_wallpaper.py rmrl tests
python -m unittest discover -s tests -v
git diff --check
actionlint .github/workflows/release.yml
```

To build Windows x64 packages locally:

```powershell
.\build-portable.ps1
```

The script creates `dist/rmtool-windows-x64.zip` and `dist/rmtool-windows-x64-onefile.exe`. The macOS ARM64 app is built by the [release workflow](.github/workflows/release.yml). After a `v*` tag is pushed, the workflow publishes all three downloads when the Windows and macOS test and build jobs succeed.

## Contributing, license, and credits

Report problems through [Issues](../../issues) or submit [Pull Requests](../../pulls). Do not include device addresses, root passwords, or `.rmtool/` contents in logs, screenshots, or reproduction configurations.

This project is licensed under the [GNU General Public License v3.0](LICENSE). See [NOTICE.md](NOTICE.md) for third-party sources and licenses covering translations and fonts. Major sources include:

- The Chinese translation catalog is adapted from GPL-3.0 content in [boangs/rmkit](https://github.com/boangs/rmkit).
- The bundled handwritten-note renderer is ported from [rschroll/rmrl](https://github.com/rschroll/rmrl) and uses `rmscene` to parse newer handwriting formats.
- The bundled Noto Sans CJK SC comes from [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) and is distributed under the [SIL Open Font License 1.1](assets/fonts/LICENSE).
