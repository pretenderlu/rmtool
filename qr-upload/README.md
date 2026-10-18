# QR document upload

The source includes rmtool-managed installation for **Paper Pro Move
3.28.0.172 and 3.29.0.153**. Phone uploads and the device page passed .172
manual-deployment tests. The .153 controller passed USB/Wi-Fi desktop native
imports and restoration checks; .153 phone reception and complete managed
installation/post-reboot acceptance remain separate hardware tests.
Available from rmtool v1.18.0.

The final experience is tablet-hosted: start reception on the tablet, scan its
QR code from a phone on the same Wi-Fi, upload PDF/EPUB to the native library,
then stop reception on the tablet. After installation it must not require rmtool,
a computer, a USB cable, an SSH session, AppLoad or a cloud account.

## Current boundary

This directory implements the independent receiver, phone page and an experimental
Move settings entry under `native/`. Page/QR/phone import has passed on .172 on that
device, including 17.9 MB and 45.9 MB EPUBs that the user confirmed open normally.
`_qr_upload.py` uses rmtool's existing verified shared-Xovi transactions for
installation, detection, disabling and removal. Open **Device tools > QR
transfer** in rmtool, install the exact package, then manually restart the tablet.
Open **Settings > 扫码传输** on the tablet to start reception. There is no
always-listening boot service. Disabling/removing the entry preserves imported
books and all other verified plugins. Stop reception before changing plugins.

The receiver refuses to start if the native import endpoint is unreachable. It
does not silently fall back to SFTP, restart xochitl,
patch the stock executable, or claim a successful upload before native HTTP
acceptance. A 200/201 response means accepted by the native importer, not proven
conversion/rendering success. Ambiguous failures are never automatically retried.

## Build and tests

From this directory, with Go installed:

```text
go test ./...
go vet ./...
python build.py
```

The build script tests first and produces static Linux ARMv7 and AArch64 binaries
under `build/qr-upload/`. They contain no third-party Go dependencies. Cross-build
success does not establish device/firmware compatibility.

Optional phone-page regression checks use an existing development installation of
Playwright: `node test-ui.cjs`. Set `PLAYWRIGHT_MODULE` to its package path and
`PLAYWRIGHT_CHROMIUM_EXECUTABLE` to an existing Chromium/Chrome executable if needed.
This checks mocked uploads at 320/390/1280px and saves screenshots under
`build/qr-upload/screenshots/`; it is not a real-tablet transfer test.

For maintainer-only device experiments after exact device identity and native
import readiness have been checked, the program accepts:

```text
rmtool-qr-upload --listen <device-wifi-ip>:8765
```

It only runs on Linux and requires an already working native local HTTP importer
at `10.11.99.1:80` (or explicitly `127.0.0.1:80/81`). It creates a private session
file at `/run/rmtool-qr-upload/session.json` and temporary uploads under `/home/root`.
The session file contains the HTTP QR URL and expiry.
Do not log, publish, or include this file in diagnostic archives. No QR is yet
displayed by the CLI; the experimental native entry owns its separate session.

## Managed native entry

`native/build.py` requires explicit Qt host tools, Zig, qmd-tool, qmldiff, the
exact Move .172 or .153 extracted resource tree and the AArch64 receiver. It verifies
the hashtable, pins the receiver SHA-256 into the Qt bridge, and replays the QMD.
All binaries/generated moc files stay in build/temporary directories.

`build_assets.py --release 3.29.0.153` packages only that target and preserves
the existing .172 package. The managed receiver verifies the internal build and
stock xochitl hash before preparing the local importer: .152 and .153 share a
build number but are not interchangeable. See [Move .153 checks](../docs/firmware-3.29.0.153-move.md).

The narrow `QrUpload` QML type exposes only `start()` and `stop()`. It reads a
single active private wlan0 IPv4, starts the verified foreground child without
Xovi preload variables, and displays the private session URL via the firmware's
QRCodeProvider. Session data is never written to QML settings or the journal.
The receiver's optional `--parent-pid` invalidates it if the owning UI process
exits. Page hide/destruction and power-button handlers request stop; validate
these on hardware before relying on automatic lock/suspend behavior.

The native entry sits above reading/note enhancements, after the stock sidebar
spacer, regardless of which peer plugins are installed or their loading order.
The native page uses the firmware's import icon and shows the current filename,
bytes actually saved, percentage (when size is known), validation, native import,
acceptance and errors. Progress is atomically published in the private session
file at most once per second; unchanged state does not repaint the e-ink UI.
Browser upload completion is not native import completion. Unknown native
outcomes remain explicit and are never automatically retried.

The phone sends the selected File directly, without a full-file JavaScript read,
Base64 encoding or checksum pass. It shows elapsed time before the first progress
event and byte progress afterward. Cloud-backed file providers can add delay,
but this is not yet confirmed as the cause of the reported iCloud wait. Receiver
logs record request refusal codes, byte counts, receive/validation/import timings
and bounded validation reasons, never names, document content, session URLs or
tokens. Validation failures show the specific format/limit reason on both phone
and device, and do not submit the file to the native importer.

The managed bridge requests `--activate-native`. On the exact verified build,
the receiver prepares only the device-local `10.11.99.1/29` address on `usb1` if
absent. Existing stock `/29` and earlier canary `/27` addresses are reused without
replacement; other IPv4 configurations are refused. A private readiness marker
then lets the bridge temporarily enable `Settings.webInterfaceEnabled`. No
cable, SSH session or desktop process is required for reception. The original
setting is preserved; an owned restore marker survives a UI crash and is
recovered when the page is reopened. A leftover marker blocks removal until
recovery. Only an address created by this session is removed, and only while no
USB cable is connected. Normal stop closes the listener and restores the setting.
Reboot persistence and crash/lock recovery still require hardware acceptance.

The updated component also exposes a root-only Unix control socket for desktop
native import, initialized from MainView rather than the QR settings page. The
verified helper acquires an exclusive session, prepares the local address, then
requests the live Settings property change. Desktop uploads keep the helper
alive over their captured SSH connection; EOF, cancellation or 15 seconds without
a heartbeat releases the session. Completion waits for settings and address
restoration, and does not report success when restoration is unconfirmed.
Originally enabled native service is reused without changing its setting. QR
reception and desktop-controlled activation do not take over each other's active
session. This automatic path currently has the same exact Move .172/.153 scope;
it is not a generic firmware activation method. Loading a new component requires
one manual restart after installation; uploads themselves do not restart anything.

After building the receiver and bridge, `build_assets.py --receiver ...
--native-dir ... --carrier ... --output-dir ...` creates a deterministic verified
package, updates the bundled manifest and seeds the local `.rmtool` cache. The
carrier is the exact current reading-enhancement archive, supplying the same
shared runtime. This does not publish assets or deploy to a device.

## Security and limits

- HTTP on trusted Wi-Fi only, explicitly chosen to avoid self-signed certificate
  interstitials without installing phone certificates or adding cloud services.
  Documents and authorization headers are unencrypted: session tokens do not
  prevent network eavesdropping or tampering. Do not use on public/untrusted
  networks or forward this port to the internet. The phone page displays a warning.
- There is no certificate interstitial, but a browser may still label HTTP as
  insecure or warn under an HTTPS-only policy. Do not disable browser security
  globally. No automatic redirect or fallback to another host/protocol is used.
- One explicit private IPv4 listener, no wildcard/public bind. Twelve simultaneous
  TCP connections maximum, short header timeout and bounded transfer timeout.
- Independent random 256-bit session token carried in the QR fragment, then only
  an Authorization header. Not in HTTP URLs, logs, localStorage or public HTML.
- Upload-only endpoints: no library listing, downloads, deletion or arbitrary
  reverse proxy. Host/origin checks and no cross-origin access.
- One upload at a time; 200,000,000 bytes (200 MB) per file; larger files should use
  USB transfer. Content and filename validation still apply.
  EPUB ZIP directories are bounded before parsing; encrypted/symlink/traversal
  entries and declared expansion above 200 MiB are refused. ZIP64 is unsupported.
- Outer filename edge spaces are trimmed before native import; control characters
  are still rejected. The phone checks invalid names before sending the body.
  Nested EPUB resource names may contain colons on the Linux target; drive/URI
  prefixes, backslashes, NULs and traversal remain blocked. Archive contents and
  internal resource names are never rewritten. This is compatibility screening,
  not EPUBCheck.
- Native importer owns full PDF/EPUB parsing. PDF signature/end-marker checks and
  EPUB container checks are format screening, not a complete malware sandbox.
- Staging uses `/home`, not `/data`, checks space before and after reception, keeps
  64 MiB reserve and space for another file copy. Native EPUB conversion may need
  more space than this estimate; low-space hardware behavior still needs testing.
- Expires after 15 minutes; SIGTERM/Ctrl-C cancels reception and invalidates the
  session. Losing the selected IP also stops reception. Lock/suspend hooks are
  present but still need hardware acceptance.
- Normal completion/interruption removes owned temporary files. A crash or power
  loss may leave the private experiment directory; inspect it before removal.
- Uploaded filenames/content/native error bodies are not written to application
  logs. Rejected uploads are not submitted to the native importer.

## Hardware acceptance gate

Before releasing this feature, prove these with the cable disconnected and rmtool
closed: native PDF/EPUB import, library appearance without a reader restart, QR
start/stop from the device, reboot persistence, sleep/lock handling, phone Safari
and Chrome, full/interrupted transfers, and preservation of USB/SSH and existing
plugins. Test exact firmware targets; do not infer all-device support from ARM
cross-compilation or the presence of QRCodeProvider.

## References

- [webinterface-rmpp](https://github.com/t3rminalV/webinterface-rmpp): MIT, native
  web importer over Wi-Fi with cable-free network configuration on Paper Pro.
- [webserver-remote](https://github.com/asivery/rm-xovi-extensions/tree/master/webserver-remote):
  GPL-3.0, Xovi UI approval and native-server routing reference.
- [webinterface-upload-button](https://github.com/rM-self-serve/webinterface-upload-button):
  native `POST /upload` multipart field `file` reference.

No upstream code is vendored here. This implementation uses Go's standard library
and is covered by rmtool's repository license.
