# Native Document Import Control

Support from rmtool v1.18.0 for **desktop document-center upload**, not the
phone QR receiver. PDF/EPUB bytes are streamed over the selected tablet's SSH
connection to its own HTTP `/upload` endpoint, using multipart field `file`.
The system creates UUIDs and metadata and performs EPUB conversion. No direct
library writes, SFTP fallback, automatic retry, or upload-triggered restart.

## Exact Coverage

| Device | Stable firmware | Beta firmware | Architecture |
| --- | --- | --- | --- |
| reMarkable 1 | 3.28.0.172, 3.29.1.0 | 3.29.0.153, 3.30.0.108 | ARMv7 |
| reMarkable 2 | 3.28.0.172, 3.29.1.0 | 3.29.0.153, 3.30.0.109 | ARMv7 |
| Paper Pure | 3.28.0.172, 3.29.1.0 | 3.29.0.153, 3.30.0.108 | AArch64 |
| Paper Pro | 3.28.0.172, 3.29.1.0 | 3.29.0.153, 3.30.0.108 | AArch64 |
| Paper Pro Move | 3.28.0.172, 3.29.1.0 | 3.29.0.153, 3.30.0.108 | AArch64 |

Every target requires its exact internal
build and stock xochitl hash, not just a matching public version. All packages
have offline ABI and resource-replay verification. The Move .153 QR-based controller
has passed USB/Wi-Fi native imports and restoration tests. On Move 3.30.0.108,
desktop native import and restoration of the disabled USB web interface also passed.
Independent-component hardware
acceptance remains outstanding. Native transfer coverage does not authorize
localization, QR reception or other plugins on these new beta targets; their
individual exact-package gates still apply. Old unsupported plugin residue
must be handled by device maintenance before a new shared component is installed.

## Desktop Workflow

Use **Upload Documents** when the tablet's native USB web interface is already
reachable. No component is needed. To enable the interface manually, connect
via USB and use the tablet's Settings > Storage page.

If the interface is off and no control component is ready on supported firmware,
**Upload Documents** offers optional automatic activation before sending files.
First load requires a manual reboot; retry the upload after reconnecting. With
the component ready and the interface off, rmtool opens an exclusive control session,
prepares the device-local address and temporarily enables the native service.
It restores the original setting when done. Imported EPUBs may appear under
their internal book title while the system converts them.

A verified, already loaded QR controller is reused without requiring the new
component. If both are installed, distinct QML types avoid ambiguity and only
the QR controller owns the shared control socket. Removing either preserves
the other feature. Old QR experiments without native control must be updated
before initializing this component. Missing/unknown components never trigger
a direct-library fallback. Other firmware can use its manually enabled native
USB web interface if available.

The helper is a minimal static build of the same protocol, address and expiry
code used by QR transfer; it does not include a phone server. The root-private
Unix control socket accepts root peers only. EOF, signals or missing heartbeats
end a session; the 15-second deadline includes CPU-suspend wall time. Persistent
restore markers cover UI crashes. Component changes are blocked during active
transfers. The initial firmware hash check allows 30 seconds for older ARM CPUs.

## Offline Build

Use `build.py --help` for explicit Go, Zig, Qt moc, qmd-tool, qmldiff, 7-Zip and
cached firmware/resource paths. Output must remain under the repository's
`build/` directory. The builder verifies cached SWU bytes, exact xochitl/build
identities, native web-upload framing, stock network prefixes, all strong ELF
imports, architecture and QMD/hashtab replay. Stable .172 uses `/27`; beta .153
uses `/29`. Existing `/27` or `/29` addresses are never replaced.

`--beta-resources` points to the version directory containing one resource tree
per platform. Where no exact localization carrier exists, only the previously
verified architecture-matched .152 shared core is reused; its ELF imports are
checked against the new stock libraries and its hashtab is replaced by the
new firmware's own provenance-checked resource map. No language plugin is installed.

The complete ten-target manifest is written only after all checks succeed and
the archives are imported into normal local caches. Nothing is published or
deployed by this build. Repeat it and compare manifest/archive hashes before
publication. Windows/macOS packaging includes only the manifest, not payloads.

Run `python -m unittest tests.test_native_import_control
tests.test_native_document_import tests.test_native_import_ui
tests.test_native_import_control_qmd`. Exact peer replay additionally requires
`RMTOOL_QMLDIFF` and `RMTOOL_NATIVE_IMPORT_RESOURCE_TARGETS`, a JSON map from
`platform/release` to each exact resource directory containing `hashtab` and
`qrex-out`. Hardware acceptance must separately check initialization/reboot,
USB and cable-free Wi-Fi, QR co-installation, restoration on failure/sleep, and
removal without affecting books or peer plugins.
