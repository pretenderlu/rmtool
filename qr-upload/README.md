# QR document upload experiment

Development branch: `codex/qr-document-upload`. **Not a released plugin and not
yet a complete on-device QR feature. Do not merge before hardware validation.**

The final experience is tablet-hosted: start reception on the tablet, scan its
QR code from a phone on the same Wi-Fi, upload PDF/EPUB to the native library,
then stop reception on the tablet. After installation it must not require rmtool,
a computer, a USB cable, an SSH session, AppLoad or a cloud account.

## Current boundary

This directory implements only the independent receiver and phone page. Native
settings integration, lock/suspend coordination, install/update/uninstall and
cable-free native-import activation are NOT implemented or verified yet. There is
no change to rmtool's production UI, support manifests or release packaging.

The receiver refuses to start if the native import endpoint is unreachable. It
does not silently fall back to SFTP, restart xochitl, alter network configuration,
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
The session file contains the QR URL, expiry and TLS certificate fingerprint.
Do not log, publish, or include this file in diagnostic archives. No QR is yet
displayed automatically; the final native UI will own this start/stop flow.

## Security and limits

- HTTPS only. Each experiment creates an ephemeral self-signed certificate. The
  phone must verify its SHA-256 fingerprint before accepting the certificate;
  scanning the URL alone does not establish certificate trust.
- One explicit private IPv4 listener, no wildcard/public bind. Twelve simultaneous
  TCP connections maximum, short header timeout and bounded transfer timeout.
- Independent random 256-bit session token carried in the QR fragment, then only
  an Authorization header. Not in HTTP URLs, logs, localStorage or public HTML.
- Upload-only endpoints: no library listing, downloads, deletion or arbitrary
  reverse proxy. Host/origin checks and no cross-origin access.
- One upload at a time; 100,000,000 bytes per file; content and filename validation.
  EPUB ZIP directories are bounded before parsing; encrypted/symlink/traversal
  entries and declared expansion above 200 MiB are refused. ZIP64 is unsupported.
- Native importer owns full PDF/EPUB parsing. PDF signature/end-marker checks and
  EPUB container checks are format screening, not a complete malware sandbox.
- Staging uses `/home`, not `/data`, checks space before and after reception, keeps
  64 MiB reserve and space for another file copy. Native EPUB conversion may need
  more space than this estimate; low-space hardware behavior still needs testing.
- Expires after 15 minutes; SIGTERM/Ctrl-C cancels reception and invalidates the
  session. Losing the selected IP also stops reception. Lock/suspend hooks are
  still required before this is suitable for end users.
- Normal completion/interruption removes owned temporary files. A crash or power
  loss may leave the private experiment directory; inspect it before removal.
- Uploaded filenames/content/native error bodies are not written to application
  logs. Rejected uploads are not submitted to the native importer.

## Hardware acceptance gate

Before adding this to rmtool, prove these with the cable disconnected and rmtool
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
