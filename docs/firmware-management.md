# Firmware management

## Scope and confirmation

The native Qt page supports official inventory/download/cache, local SWU
inspection, Paper Pro/Move new-schema upgrades and downgrades, and inspected
A/B switching. No automatic reboot, force mode, counter reset, cancellation of
a writer, resource publication, or device operation is performed by the tests.
Older devices and legacy A/B layouts remain unsupported for mutation.

Connect and query device state before preparing an installation or switch.
After final confirmation rmtool pauses the idle automatic updater itself. The
pause uses a runtime-only service mask and records the original active state in
`pause.json`; it is restored automatically if the operation fails before being
committed. The runtime mask also disappears on reboot. Permanent enable/disable
policy is not changed, and unknown service changes are never overwritten.

Installing and switching require confirmation after preflight. A downgrade
requires an additional confirmation: shared user data is NOT rolled back and
older firmware may be unable to read newer notes/settings. Back up first.
Reboot is a separate action available only after durable success and native
state checks. Download/structure/hash work reports progress; native installation
is queried as a state rather than displaying an invented flash percentage.

## Validation and native engine

Inventory and images come only from the official S3 bucket and CloudFront
distribution, including redirect validation. Public availability does not
imply stable-channel membership. The .172 stable label is user-confirmed.
Local CPIO checks reject traversal, links, special files, duplicate entries,
invalid sizes/types, extra payloads and payload hash disagreement. Descriptor
selection includes Ferrari's identical `CT-PCBA-IMX8MM`/`ferrari` board aliases.
Local validation is not a signature claim. Each staged image is checked using
the device's native `swupdate -c`, installed public key, hardware revision and
inactive-slot selection before running the native installer.

Both current and inactive version probes read `IMG_VERSION` from
`/usr/lib/os-release`, without sourcing shell files; missing/empty reads fail.
Boot flow, A/B mappings, counters, power, staging capacity and inactive-device
users are validated. An ordinary idle SWUpdate daemon is NOT a writer. Normal
rmtool operations are not blocked solely by battery, platform or daemon state.
The transport guard establishes firmware state while connecting and updates it
when rmtool starts a firmware transaction. Actual or uncertain updates block
competing operations immediately, including queued writes and reboots.

The .172 `09-swupdate-args` resets `swu_status`; therefore rmtool does **not**
source it or call `swupdate-from-image-file`. It invokes the same native engine
with explicit arguments and a minimal configuration, inside a private `/tmp`
mount. This also isolates fixed extraction/socket names from stale shared files.
The image's signed native scripts remain responsible for flashing and bootloader
handling. rmtool does not implement a raw partition writer.

Offline inspection of the actual .172 image's SWUpdate recipe reports
`2022.05-rm+git`. In the corresponding upstream
[install_from_file.c](https://github.com/sbabic/swupdate/blob/2022.05/core/install_from_file.c),
`-c` selects `RUN_DRYRUN`; the
[installer](https://github.com/sbabic/swupdate/blob/2022.05/core/installer.c)
skips pre/post scripts and substitutes a dummy install handler. Native payload
staging uses archive filenames before preinstall, so `imx-boot` is staged at
`/tmp/imx-boot` independently of its final file-handler path
`/tmp/imx-boot-tmp`. No redundant custom extraction is used. This is upstream
protocol evidence, not execution of the vendor-patched binary on hardware.

## Durable state and plugins

Jobs live in `/home/root/.rmtool-firmware/<job-id>/`: `job.json`, `result`,
`target`, the script, and (for installations) `install.log`. `current` is an
atomic, synced pointer. A unique detached systemd unit runs each job. Disconnect
or a missing unit never implies success. Same-boot success requires both a
synced success record and successful exited unit. After reboot, reconciliation
uses the changed boot ID plus target slot, version, xochitl hash and clean native
state; it does not depend on a surviving transient unit. Missing/conflicting
evidence remains locked for manual diagnosis. Logs and images are retained;
there is no automatic cleanup/retry of uncertain jobs.

Official firmware installation deliberately does not inspect, modify, migrate,
or reject third-party applications and plugins. This matches the stock updater:
firmware validation is limited to the official image, device identity, power,
native updater and inactive target. The shared operation lock still serializes
rmtool's own writers while the firmware job is committed.

After the new firmware boots, rmtool detects the retained trusted shared-plugin
marker in `/data`. When exact packages exist for the new firmware, the firmware
page offers **Restore pre-update plugins**. Restoration downloads and verifies
the new packages, rebuilds the complete shared runtime in one transaction, and
preserves each feature's enabled or disabled state. Missing or untrusted targets
are reported and never forced into the new firmware. A restoration failure does
not roll back or damage the completed firmware installation. Third-party apps
outside rmtool ownership are left untouched and are never automatically injected.

Inactive-slot switching is different from installing an image: it boots existing
contents without replacing them. It therefore retains the clean read-only
filesystem check and `ro,noload` inspection; unknown old root-local xochitl
drop-ins still block switching.

## Verification and limits

Run `python -m unittest discover -s tests -p 'test_firmware*.py'` and the existing
SSH/GUI regression suite in `test_rmtool_behaviors.py`. External real SWU fixtures
can be selected with `RMTOOL_FIRMWARE_FIXTURES`; none are committed. Set
`RMTOOL_FIRMWARE_SCREENSHOTS` to keep offscreen Qt screenshots.

Both real .172 containers passed structure/platform/hash checks in this work.
The coordinating task additionally reported RSA PKCS1v15/SHA256 verification
of both descriptors against the installed device public key, using only a
read-only key retrieval. The application still validates each staged image
natively. No device connection, upload, native check invocation, flash, switch,
reboot or hardware integration test was performed by this implementation task.
Native execution, vendor-specific behavior and power-loss recovery still require
separately authorized hardware testing. External actors with root/device access
cannot be serialized by an application-local lock; do not run other device
management tools during firmware operations.
