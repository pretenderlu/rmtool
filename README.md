**English | [简体中文](README.zh-CN.md)**

<div align="center">

<img src="assets/rmtool-icon.png" alt="rmtool icon" width="120">

# rmtool

A desktop GUI management tool for reMarkable devices

</div>

rmtool manages reMarkable Paper Pro, Paper Pro Move, Paper Pure, reMarkable 1, and reMarkable 2 devices over local root SSH. It provides multi-device connections, a dashboard, wallpaper and document management, KOReader library management, font upload, time management, device controls, native Chinese UI localization, offline Pinyin input, and exact-build reading and note enhancements for color devices, the
classic offline-verified tap-to-turn plugin for Paper Pure, reMarkable 1,
and reMarkable 2, plus a one-click read-only diagnostic bundle for support. Device operations do not depend on reMarkable cloud services. Release builds include baseline trusted manifests for these firmware-specific features, enabling offline support discovery and verified cache reuse. Payloads are not bundled and still require a network download or an existing validated cache.

> [!WARNING]
> rmtool directly modifies files on the device. Sync or back up important content first, and make sure you accept the data and warranty risks associated with Developer Mode, root SSH, and third-party modifications. This project is not official reMarkable software.

## Screenshots

<table>
  <tr>
    <td width="50%" align="center">
      <a href="assets/screenshots/01-dashboard.png"><img src="assets/screenshots/01-dashboard.png" alt="rmtool device dashboard" width="100%"></a><br>
      <sub><b>Dashboard</b></sub>
    </td>
    <td width="50%" align="center">
      <a href="assets/screenshots/02-wallpaper.png"><img src="assets/screenshots/02-wallpaper.png" alt="rmtool wallpaper management" width="100%"></a><br>
      <sub><b>Wallpaper Management</b></sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <a href="assets/screenshots/03-documents.png"><img src="assets/screenshots/03-documents.png" alt="rmtool document center" width="100%"></a><br>
      <sub><b>Document Center</b></sub>
    </td>
    <td width="50%" align="center">
      <a href="assets/screenshots/04-koreader.png"><img src="assets/screenshots/04-koreader.png" alt="rmtool KOReader library manager" width="100%"></a><br>
      <sub><b>KOReader Library</b></sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <a href="assets/screenshots/05-fonts.png"><img src="assets/screenshots/05-fonts.png" alt="rmtool font management" width="100%"></a><br>
      <sub><b>Font Management</b></sub>
    </td>
    <td width="50%" align="center">
      <a href="assets/screenshots/06-toolbox.png"><img src="assets/screenshots/06-toolbox.png" alt="rmtool device toolbox" width="100%"></a><br>
      <sub><b>Device Toolbox</b></sub>
    </td>
  </tr>
</table>

## Download and installation

Most users should download the latest build below from GitHub Releases. Python is not required.

| Platform | Download | Notes |
| --- | --- | --- |
| Windows x64 | [Portable ZIP](https://github.com/pretenderlu/rmtool/releases/latest/download/rmtool-windows-x64.zip) | Extract it and run `rmtool/rmtool.exe`; recommended for regular use |
| Windows x64 | [Single-file EXE](https://github.com/pretenderlu/rmtool/releases/latest/download/rmtool-windows-x64-onefile.exe) | Run it directly; first launch and each cold start are slower |
| macOS ARM64 | [Apple Silicon app](https://github.com/pretenderlu/rmtool/releases/latest/download/rmtool-macos-arm64.app.zip) | M-series Macs only; extract it and run `rmtool.app` |

The release packages are currently neither Windows code-signed nor Apple-notarized. If SmartScreen or Gatekeeper blocks the app, first verify that the file came from this repository's release page, then use the operating system's one-time approval option. Do not disable system security globally.

The macOS build stores its runtime state in `~/Library/Application Support/rmtool/`, so the app can run normally even when its bundle is in a read-only or translocated location.

### Hosted resource sources

All firmware-specific resources managed by rmtool use two fixed sources. The client tries GitHub Releases first and automatically falls back to the Tencent COS mirror (useful when GitHub is hard to reach from mainland China), and accepts a manifest or payload only after its expected size and SHA-256 match. An invalid response never replaces a validated cache. If both sources fail, rmtool uses a previously validated cached manifest and then the baseline trusted manifest bundled with the application; installation still requires the matching payload to exist in the validated cache. When a payload cannot be downloaded from either mirror, the error dialog lists the exact GitHub and COS download URLs, offers to copy them, and can load a manually downloaded archive after the same size and SHA-256 verification.

| Resource | GitHub (default) | Tencent COS fallback (mainland China) |
| --- | --- | --- |
| Chinese localization | [`localization-assets`](https://github.com/pretenderlu/rmtool/releases/tag/localization-assets) | [COS root](https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/) |
| Native Simplified Chinese | [`native-chinese-assets`](https://github.com/pretenderlu/rmtool/releases/tag/native-chinese-assets) | [`native-chinese/`](https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/native-chinese/) |
| Pinyin input | [`pinyin-input-assets`](https://github.com/pretenderlu/rmtool/releases/tag/pinyin-input-assets) | [`pinyin-input/`](https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/pinyin-input/) |
| Reading enhancements | [`reading-enhancements-assets`](https://github.com/pretenderlu/rmtool/releases/tag/reading-enhancements-assets) | [`reading-enhancements/`](https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/reading-enhancements/) |
| Note enhancements | [`note-enhancements-assets`](https://github.com/pretenderlu/rmtool/releases/tag/note-enhancements-assets) | [`note-enhancements/`](https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/note-enhancements/) |

## Connecting a device

### SSH prerequisites

- The device must allow SSH login as `root`, and you must be able to view its current root password.
- Paper Pro, Paper Pro Move, and Paper Pure must first be put into Developer Mode. Enabling it performs a factory reset, removes local data from the device, and weakens device security, so sync or back up first. See the [official reMarkable documentation](https://developer.remarkable.com/documentation/developer-mode) for the procedure and risks. reMarkable 1 and reMarkable 2 do not use Developer Mode, but they still require working root SSH access.
- The default USB address is `10.11.99.1`. Connect the device to the computer over USB and select USB mode on the device.
- Wi-Fi SSH is disabled by default. Connect over USB first, then choose "Enable Wi-Fi SSH" under "Device Toolbox > Device Control" and change the saved device address to its WLAN address.
- On Paper Pro, the root username and password are available under `General > Help > About > Copyrights and Licenses`. For other models or firmware versions, follow the current device UI.

### First connection

1. Start rmtool, click "Add" in the sidebar, and enter the device name, connection method, address, model, and root password.
2. Click "Connect". The first connection displays the SSH host fingerprint; trust it only after confirming that it belongs to your device.
3. After a successful connection, the wallpaper, document, KOReader, and toolbox pages are enabled automatically.
4. Multiple devices can have separate saved profiles. Switching to another device or address automatically closes the existing SSH connection.

## Local data and security

rmtool stores runtime state in the following platform-specific directory:

| Run mode | State directory |
| --- | --- |
| From source | `.rmtool/` in the repository root |
| Windows release | `.rmtool/` beside `rmtool.exe` or the single-file EXE |
| macOS release | `~/Library/Application Support/rmtool/` |

The main files are:

- `devices.json`: device profiles, current device, theme, paths, and log-panel settings.
- `known_hosts`: SSH host trust records isolated by device ID.
- `remarkable_tool.log`: rotating runtime log.
- `cache/localization/`: validated localization manifests and firmware-package cache.
- `cache/reading-enhancements/`: validated reading-enhancements manifest and package cache.
- `cache/note-enhancements/`: validated note-enhancements manifest and package cache.
- `cache/pinyin-input/`: validated offline Pinyin package cache.
- `cache/official/`: verified AppLoad and KOReader archives downloaded directly from their official GitHub Releases.

> [!CAUTION]
> When "Remember password" is selected, the root password is stored in **plain text** in `devices.json` under the state directory above; it is not stored in the operating system credential manager. Do not share, upload, or sync the entire state directory to an untrusted location, and do not attach it to an issue. Use "Forget password" in the sidebar to remove a saved password.

## Features

- **Connections and dashboard**: Manage multiple USB/Wi-Fi device profiles and verify SSH host fingerprints. The native Qt dashboard shows connection status, device details, PDF/EPUB/notebook counts, and suggested next steps.
- **Wallpaper management**: Read and preview the device's current startup, suspend, carousel, and shutdown wallpapers. The current UI produces portrait wallpapers at the selected device's native resolution, with fit, crop, and stretch modes plus horizontal and vertical crop offsets. The cover-wall generator arranges selected document thumbnails with optional text into a local poster wallpaper; no document data is sent to a cloud service.
- **Document center**: Search and inspect document metadata and thumbnails; batch-upload PDF/EPUB files, check free space, and batch-delete documents. Export parseable handwriting from `.rm` or `.note` data in one document to a white-background PDF without merging the original PDF/EPUB pages.
- **AppLoad and KOReader**: On exact supported production firmware, install AppLoad and KOReader directly from their official GitHub Releases without Vellum. rmtool verifies the pinned filename, size, and SHA-256, accepts the same official ZIP files for offline import, and never restarts the device automatically. A previous Vellum/AppLoad KOReader directory is fully backed up before rmtool migrates its settings, history, statistics, screenshots, and other allowlisted user data into a clean official install; unrecognized program files are not mixed into the new version. Firmware 3.28 beta builds are intentionally unsupported. The existing KOReader library manager can then search folders, transfer books, create folders, and delete entries without leaving the detected library root.
- **Font manager**: Preview and upload multiple TTF/OTF fonts, optionally rename an upload to `zwzt.ttf`, inspect the inactive repository at `/home/root/.local/share/rmtool/fonts`, switch the exact font file used by the system UI, and delete inactive fonts. Uploads stay outside Fontconfig's default scan paths until explicitly selected; only the selected font is atomically mirrored to `/data/rmtool/fonts` for the pre-unlock UI. Root stores only a small, persistent Fontconfig file. Existing custom font directories remain supported and are not moved automatically. A one-click migration is offered only when the old root mirror, rmtool-generated Fontconfig files, metadata, and one `/home` original match the exact legacy layout; no font is re-uploaded. Activation uses the complete font and is allowed when the post-swap state keeps at least 24 MiB free on `/data`; tight-space replacements first preserve and verify the previous mirror under `/home`. The UI labels exact device/firmware identities as device verified, pending validation, or unverified. Uploading does not change the active font or reboot the device; restart is a separate confirmed action.
- **Time management**: Sync the computer's time, inspect system time, hardware clock, and timezone, or set the timezone to `Asia/Shanghai`.
- **Device control**: Restart the device, enable Wi-Fi SSH, and increase frontlight brightness on devices with the `rm_frontlight` interface while installing a persistence service.
- **Reading enhancements**: On exact supported Paper Pro and Paper Pro Move 3.27/3.28 builds, add one native Settings page for global tap-to-turn, fast monochrome, and forced-refresh controls. Each PDF/EPUB also keeps independent switches and a per-book refresh strategy in its reading menu.
- **Note enhancements**: On exact supported Paper Pro and Paper Pro Move 3.27/3.28 builds, control color-settlement refreshes while handwriting. Device Settings provide the global default, while each notebook can independently choose a 5/10/30-second pen-up delay or page-turn-only settlement.
- **Offline Pinyin input**: Adds an on-device Pinyin candidate bar for the system soft keyboard and physical keyboard. Prediction stays local and shares rmtool's existing Xovi runtime with other plugins.
- **Theme and logs**: Light and dark themes are persisted. The bottom log panel supports level filtering, pause, automatic scrolling, clearing, and opening the log file.

### AppLoad and KOReader installation

Open the KOReader page, connect the device, and click **Check Status**. Installation is enabled only when the model, internal firmware version, architecture, and stock xochitl hash exactly match a supported production-firmware entry. Install AppLoad first, then KOReader, and restart the device manually after rmtool closes SSH. If legacy KOReader files are detected, choose either **Migrate and Install KOReader** or **Permanently Remove Legacy Files**. Migration keeps the untouched old directory at `/home/root/.local/share/rmtool/koreader-legacy-backup`; permanent removal deletes the fixed legacy KOReader application directory and everything inside it, creates no backup, and requires a separate confirmation before a clean installation. Online installation downloads AppLoad from [asivery/rm-appload Releases](https://github.com/asivery/rm-appload/releases) and KOReader from [koreader/koreader Releases](https://github.com/koreader/koreader/releases); these two application archives are not bundled in rmtool, copied to Tencent COS, or served from the rmtool repository. For an unreliable connection, download the exact official ZIP yourself and choose **Load Local Official Package**.

### Wallpaper notes

Before each upload, the target file is copied to `.backup` in the same directory; another upload overwrites that backup. When uploading the suspend wallpaper `suspended.png`, rmtool can replace existing `carousel/*.png` files with transparent images so firmware 3.27 carousel artwork does not cover the custom wallpaper. The original carousel images are preserved once in `carousel/.backup/`, a subdirectory ignored by the firmware, and disabling the option restores them. Legacy adjacent backups are migrated into that subdirectory.

### Native Chinese UI localization

> [!IMPORTANT]
> This feature (the French-slot replacement method) is **frozen**: the matrix
> below is the final support list and no newer firmware will be added to it.
> Localization for newer firmware (`3.28.0.166` betas and future stable
> releases) is provided exclusively by the [Independent Simplified Chinese
> plugin](#independent-simplified-chinese-plugin), which uses a genuine
> `zh_CN` slot and keeps French fully usable. This feature remains available
> on the firmware listed below.

Release packages do not embed firmware-specific `.qm` files. After you choose "Device Toolbox > System Localization > Check Status", rmtool:

1. Retrieves the manifest from the fixed GitHub [`localization-assets`](https://github.com/pretenderlu/rmtool/releases/download/localization-assets/manifest.json) release first, then the [Tencent COS mainland mirror](https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/manifest.json). If both are unavailable or invalid, it uses a previously validated cache and finally the baseline manifest bundled with the application.
2. Matches the exact 14-digit internal firmware version from `/etc/version`.
3. Calculates the SHA-256 of the device's original French carrier file, `reMarkable_fr.qm`, and uses it to select the correct hardware payload. Platform names such as `chiappa`, `ferrari`, `tatsu`, `rm1`, and `rm2` are display labels only; they are not used to guess compatibility.
4. Verifies the download size and SHA-256. Nothing is written to the device if the firmware, original French file, or checksum does not match.

The normal workflow is to click "Enable Chinese" and let rmtool download and install the exact matching package automatically. Package downloads try GitHub first and fall back to the Tencent COS mirror, and every response must match the manifest's exact size and SHA-256 before it can replace the cache. If the network is unreliable, use "Get Localization Package" to save the matching file or copy a direct URL, then import it with "Load Local Localization Package". Local files must pass the same checks for the connected device. A verified import only enters the computer-side cache; "Enable Chinese" still performs the existing guarded deployment. Firmware-specific `.qm` payloads are never bundled in rmtool releases.

#### Current localization support matrix

The platform code is the hardware identifier used inside official firmware packages. It is separate from the 14-digit internal firmware version shown in each column.

| Device model | Platform code | 3.27.1.0 stable (`20260506100933`) | 3.27.3.0 stable (`20260612085811`) | 3.28.0.162 beta (`20260629074044`) | 3.28.0.163 beta (`20260702125656`) | 3.28.0.164 beta (`20260702125656`) | 3.28.0.166 beta (`20260806095513`) | 3.28.0.169 beta (`20260806095513`) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| reMarkable Paper Pro | `ferrari` | Supported | Supported | Supported | Supported | Supported | Supported | Supported |
| reMarkable Paper Pro Move | `chiappa` | Supported | Supported | Supported | Supported | Supported | Supported | Supported |
| reMarkable Paper Pure | `tatsu` | Not available | Supported | Not available | Not available | Not available | Not available | Not supported |
| reMarkable 1 | `rm1` | Not available | Supported | Not available | Not available | Not available | Not available | Not supported |
| reMarkable 2 | `rm2` | Not available | Supported | Not available | Not available | Not available | Not available | Not supported |

Enable and restore have been verified on a real Paper Pro (`ferrari`) for 3.28.0.162 and 3.28.0.163. The 3.28.0.164 packages, Paper Pro Move (`chiappa`) beta support (including 3.28.0.166 and 3.28.0.169), and the listed 3.27.3 packages for Paper Pro Move, Paper Pure (`tatsu`), reMarkable 1 (`rm1`), and reMarkable 2 (`rm2`) have been validated offline against official firmware but not yet deployed to those devices. Versions 3.28.0.163 and 3.28.0.164, like 3.28.0.166 and 3.28.0.169, share the same internal version, so rmtool distinguishes them by the exact stock French catalog hash instead of the version string alone; 3.28.0.169 also ships the byte-identical stock carrier, so the same localization assets apply. The cloud manifest remains the source of truth for actual availability. See the [localization documentation](translations/README.md) and [manifest format](translations/manifest.json).

The established localization path reuses xochitl's built-in French language slot, so French is unavailable while Chinese is enabled. rmtool first backs up the original configuration and `reMarkable_fr.qm`, then checks whether the current primary font supports Simplified Chinese. The official reMarkable 1 and reMarkable 2 firmware images contain no CJK fonts, so this fallback is required. If the current primary font does not support Chinese, you can install the bundled Noto Sans CJK SC or select a local TTF/OTF file. The selected UI font remains managed under `/home`; one verified active copy is stored under `/data/rmtool/fonts`, while only `/etc/fonts/conf.d/99-rmtool-ui-font.conf` is persisted on the root filesystem. This keeps the same font available on the passcode screen before encrypted `/home` is unlocked without consuming root space with a full font. After enabling localization, applying or repairing fonts, or restoring the original UI, rmtool closes SSH and **does not restart the device automatically**. After enabling Chinese, restart the device manually, then open Settings > Language and select French to activate the Chinese UI. Restoring the original UI only requires the prompted restart.

#### Independent Simplified Chinese plugin

Exact-build plugins add an independent **Simplified Chinese** language option while preserving French:

| Device | 3.27.1 stable | 3.27.3 stable | 3.28.162 beta | 3.28.163 beta | 3.28.164 beta | 3.28.166 beta | 3.28.169 beta |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Paper Pro (`ferrari`) | Offline verified | Offline verified | Offline verified | Offline verified | Offline verified | **Device verified** | Offline verified |
| Paper Pro Move (`chiappa`) | Offline verified | **Device verified** | Offline verified | Offline verified | Offline verified | Offline verified | Offline verified |

The Move stable package has passed real-device installation, language switching, reboot, and removal tests. It remains gated to the exact official xochitl hash. Its stock firmware has no CJK font, so rmtool requires a Chinese-capable active system font before deployment and refuses without writing when coverage is missing.

The independent plugin and French-slot localization cannot be active together. To migrate safely, first restore French-slot localization and manually reboot. Then reconnect, enable the independent plugin, and reboot manually again before selecting Simplified Chinese. rmtool deliberately keeps this as two explicit stages so a failed second stage leaves the device on the stock language path.

### Offline Pinyin input

Exact packages cover Paper Pro and Paper Pro Move `3.27.1.0`, `3.27.3.0`, `3.28.0.162`, `3.28.0.163`, `3.28.0.164`, `3.28.0.166`, and `3.28.0.169`. Every package is gated by hardware, architecture, internal firmware, and xochitl SHA-256. Paper Pro `3.28.0.166` is device verified; the other thirteen targets are offline verified against official firmware.

The GPL-3.0 components ported from [boangs/rmkit](https://github.com/boangs/rmkit) comprise a QMLDiff candidate bar, a small input hook, the `zh_CN` keyboard-layout resource, and a local `rime-frost` dictionary server. The hook and validated keyboard resource join the rmtool shared-Xovi runtime only while Pinyin is enabled. Every exact native-Chinese catalog resolves the stock `LanguageAndKeyboard / Chinese` label as `中文`; keyboard-label ownership stays out of all QMDs. The dictionary server is kept under `/home/root/.local/share/rmtool/pinyin-input`, and installation preserves every peer feature without restarting xochitl or the device. Only the previously installed Paper Pro `3.28.0.166` revisions are accepted for bounded repair; newly supported targets do not inherit those predecessor rules.

### Reading enhancements

Reading enhancements remain one rmtool plugin and one exact-firmware package. The native device Settings page provides a master switch plus independent global gates for tap-to-turn, fast monochrome, and forced refresh. Each PDF/EPUB reading menu then exposes per-book switches; forced refresh can use a 5/10/15/20/25/30-page interval or chapter boundaries for that document. The same menu also provides a direct **Table of contents** entry: reMarkable already supplies the TOC data, hierarchy, navigation, and native screen, while rmtool only exposes that existing screen outside the deeper stock menu. Global gates always win over per-book state, while disabling a global feature preserves the saved book preferences. rmtool requires the hardware platform, CPU architecture, internal firmware version, and stock `/usr/bin/xochitl` SHA-256 to match; other or modified builds are rejected rather than guessed. The older tap-to-turn and fast-monochrome packages remain private compatibility inputs for safe migration and cleanup only.

Paper Pure, reMarkable 1, and reMarkable 2 are not covered by the unified reading-enhancements packages. On those devices the toolbox shows a device-scoped **Tap to turn** entry (it appears only when the connected device has an exact tap package and no reading-enhancements package) that manages the classic tap-to-turn plugin with install, disable, firmware-residue cleanup, and a local-package loader. Every tap target on these devices is offline verified only and has not been deployed to a real device; the UI states this explicitly before every install.

For every package in the support matrix, rmtool can identify exact earlier Reading Enhancements revisions and verified historical tap-to-turn/fast-monochrome layouts. It offers migration or repair as the primary path and a separate **Clean legacy** action before a fresh install. Cleanup validates the complete old installation before changing the device, preserves other verified rmtool plugins even when they are disabled, and refuses unknown, modified, or mixed layouts.

| Device model | Platform | 3.27.1.0 stable (`20260506100933`) | 3.27.3.0 stable (`20260612085811`) | 3.28.0.162 beta (`20260629074044`) | 3.28.0.163 beta (`20260702125656`) | 3.28.0.164 beta (`20260702125656`) | 3.28.0.166 beta (`20260806095513`) | 3.28.0.169 beta (`20260806095513`) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| reMarkable Paper Pro | `ferrari` | Offline verified | Offline verified | Offline verified | Offline verified | Offline verified | Offline verified | Offline verified |
| reMarkable Paper Pro Move | `chiappa` | Offline verified | **Device verified** | Offline verified | Offline verified | Offline verified | Offline verified | Offline verified |

Move 3.27.3 has passed real-device installation, restart, Settings navigation, and feature tests. The other thirteen targets are offline verified against official firmware with qmd-tool hash checks, QMLDiff compatibility, patch replay, patched-QML assertions, archive validation, and deterministic rebuild.

After installation and a manual device restart, first authorize the required features in `Settings > Reading enhancements`, then control them independently from each PDF/EPUB reading menu. Per-book state is keyed by document ID and survives restarts. Tap-to-turn keeps native swipes, stylus input, menus, zooming, selections, and document links available. Fast monochrome applies only to PDF/EPUB reading on color devices; forced-refresh controls do not appear in notebooks, inserted note pages, or non-reading views.

Installation, migration, repair, and removal are intentionally separated from activation. rmtool writes and validates the persistent configuration, closes SSH, and never restarts xochitl or reboots the device automatically. Use the device menu to perform a full restart afterward. The launcher checks the device and every runtime file on each boot and falls back to stock xochitl if any check fails.

Reading enhancements, note enhancements, native Simplified Chinese, and Pinyin input share one rmtool-owned Xovi/QRR runtime while retaining separate feature state. Vellum/AppLoader and unmanaged Xovi layouts block installation to prevent mixed runtimes. Firmware-specific resources are fetched from the fixed GitHub release first and the Tencent COS mirror second, with exact size and SHA-256 verification plus validated-cache fallback. The legacy migration/cleanup action can replace verified historical tap-to-turn and fast-monochrome packages with the current exact package while preserving peer features. rmtool does not uninstall Vellum itself; follow the [official Vellum CLI instructions](https://github.com/vellum-dev/vellum-cli#usage) after verified legacy package cleanup.

### Note enhancements

Note Enhancements targets color handwriting on Paper Pro and Paper Pro Move. After installation, `Settings > Note enhancements` provides a master switch and global defaults; each notebook can override them from its own settings menu. **Delayed refresh after pen-up** settles color 5, 10, or 30 seconds after writing stops. **Refresh on page turn only** keeps fast writing feedback on the current page and settles when the page or document changes. The two enhanced policies are mutually exclusive, and disabling the enhancement restores the stock approximately one-second pen-up refresh.

The exact-package matrix covers the fourteen currently supported Paper Pro and Paper Pro Move 3.27 stable and 3.28 beta targets. Move 3.27.3 has passed real-device installation, reboot, global settings, per-notebook settings, and policy-switching tests; the remaining targets are verified offline against official firmware. Installation, update, disable, and cleanup preserve the other verified shared-Xovi features and never reboot the device automatically.

## Usage recommendations

1. After connecting, confirm the current device and connection method on the dashboard.
2. On the wallpaper page, run "Rescan" first, choose a target that actually exists on the device, then preview and upload.
3. After uploading documents, you can restart xochitl immediately when prompted. If you skip it, new documents may not appear yet.
4. Document deletion cannot be undone. PDF export only works for one document containing `.rm` or `.note` handwriting data, and the result excludes the original PDF/EPUB background and non-handwriting content.
5. Font and localization changes are device-level modifications. Restart the device when prompted after they finish.
6. After installing, migrating, repairing, or disabling reading or note enhancements, wait for rmtool to close SSH, then restart from the device menu. Do not combine deployment with an immediate remote xochitl restart.

## Troubleshooting

- **Connection fails**: Check that the USB network interface appears, the address is `10.11.99.1`, the root password is current, and SSH is allowed on the device. Wi-Fi connections also require Wi-Fi SSH to be enabled over USB first.
- **SSH fingerprint changed**: A system update, device reset, or reuse of the same address by another device can trigger this warning. Verify the device identity before trusting the new fingerprint.
- **Wallpaper target unavailable**: Different firmware versions provide different wallpaper files. Click "Rescan" and choose a target that has a preview and is not marked as missing from the current device.
- **Uploaded document does not appear on the device**: Return to the document center and restart xochitl, or restart the device manually.
- **"Export to PDF" is unavailable**: Select exactly one document containing `.rm` or `.note` handwriting resources. Export renders only parseable handwriting and does not merge original PDF/EPUB pages, typed text, or other non-handwriting content.
- **Localization buttons are disabled**: Click "Check Status" first. rmtool can use GitHub, Tencent COS, a validated cache, or its bundled baseline catalog, but the internal firmware version plus the SHA-256 of the original `reMarkable_fr.qm` must match the same manifest entry. Installing without network access also requires a validated cached package or a matching package imported from disk.
- **Reading enhancements cannot be installed**: Click "Check Status" first. The model, firmware, architecture, and stock xochitl hash must match one exact row above. Modified or mixed Xovi layouts are blocked. If Vellum is detected, first let rmtool remove only its verified historical feature packages, then follow the official Vellum uninstall instructions and detect again.
- **Reading enhancements still work immediately after disabling**: This is expected because rmtool does not kill the running xochitl process. Restart the tablet from its device menu to return to the stock interface.
- **Note enhancements cannot be installed**: Click "Check Status" first. Only exact supported Paper Pro and Paper Pro Move firmware builds are accepted; unknown, modified, or mixed Xovi layouts are rejected. Restart from the device menu after installation, update, or disable.
- **AppLoad/KOReader installation is unavailable**: Click "Check Status" on the KOReader page. Only exact production-firmware matches are accepted; all 3.28 beta builds are excluded. A previous KOReader directory can be migrated, but mixed Vellum/Xovi runtimes or an already existing legacy backup still block mutation for safety.
- **macOS cannot create its configuration**: Make sure the current user can create and write `~/Library/Application Support/rmtool/`.
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
python -m compileall -q rmtool.py _dialogs.py _diagnostics.py _fast_mono_reading.py _log_viewer.py _note_enhancements.py _package_download.py _pinyin_input.py _residue_migration.py _reading_enhancements.py _rmkit_cn.py _ssh.py _styles.py _tab_connection.py _tab_documents.py _tab_toolbox.py _tab_wallpaper.py _tap_page_turn.py _xovi_standalone.py rmrl tools tests
python -m unittest discover -s tests -v
git diff --check
actionlint .github/workflows/release.yml .github/workflows/sync-localization-assets.yml .github/workflows/sync-feature-assets.yml
```

To build Windows x64 packages locally:

```powershell
.\build-portable.ps1
```

The script creates `dist/rmtool-windows-x64.zip` and `dist/rmtool-windows-x64-onefile.exe`. The macOS ARM64 app is built by the [release workflow](.github/workflows/release.yml). After a `v*` tag is pushed, the workflow publishes all three downloads when the Windows and macOS test and build jobs succeed.

Fixed resource Releases are validated by GitHub Actions but are published to the Tencent COS mirror from a maintainer's Windows computer. Install `cos-python-sdk-v5==1.9.44`, copy [.env.example](.env.example) to the gitignored `.env`, add the bucket-scoped CAM credentials, then run:

```powershell
.\publish-cos.ps1
```

The command downloads all seven fixed Releases into a temporary directory, applies the same strict manifest and payload checks used by Actions, uploads only changed payloads, publishes every manifest last, and verifies every object through the public COS endpoint. The temporary directory is removed even when publishing fails.

## Contributing, license, and credits

Report problems through [Issues](../../issues) or submit [Pull Requests](../../pulls). Do not include device addresses, root passwords, or `.rmtool/` contents in logs, screenshots, or reproduction configurations.

This project is licensed under the [GNU General Public License v3.0](LICENSE). See [NOTICE.md](NOTICE.md) for third-party sources and licenses covering translations and fonts. Major sources include:

- The Chinese translation catalog is adapted from GPL-3.0 content in [boangs/rmkit](https://github.com/boangs/rmkit).
- The bundled handwritten-note renderer is ported from [rschroll/rmrl](https://github.com/rschroll/rmrl) and uses `rmscene` to parse newer handwriting formats.
- The bundled Noto Sans CJK SC comes from [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk) and is distributed under the [SIL Open Font License 1.1](assets/fonts/LICENSE).
