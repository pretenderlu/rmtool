# Move 3.29.0.153 Adaptation

Prepared on 2026-10-17; included in v1.18.0. **Offline verified, partial Move hardware acceptance.**
Only Paper Pro Move (Chiappa) is added for .153. Published .152/.172 peer
packages are unchanged; local QR packages for Move .172/.153 also include the
new desktop native-import control.

## Coverage

- QR transfer: device settings entry, temporary HTTP receiver, native PDF/EPUB
  import, transfer status, 200 MB limit. Trusted same-Wi-Fi use only.
- Native Simplified Chinese and Pinyin input.
- Reading enhancements: tap navigation, refresh options, TOC shortcut,
  custom EPUB fonts and precise Chinese highlighting.
- Note enhancements.
- WeRead App/launcher and AppLoad/KOReader are **not** enabled for .153.
  Firmware-residue migration skips an incompatible WeRead launcher with an
  explicit status, preserving application data and migrating compatible peers.
  Skipping QR/WeRead alone does not change AppLoad shim links.

## Exact Firmware

[Official beta SWU](https://dlqathbgqp3nv.cloudfront.net/beta/remarkable-production-image-3.29.0.153-chiappa-public.swu)

| Field | Value |
| --- | --- |
| SWU bytes | 168664064 |
| SWU SHA-256 | `0b58feec10c046ed8d1d093316bfbfa4f4549511b3a9458cba29570fd8f81094` |
| Internal build | `20261009073202` |
| xochitl SHA-256 | `0d4bfba6daf8f61138996af95b494d70da11654d4861a98bffe894a425b8caba` |
| Resource count | 193 |
| Hashtab SHA-256 | `dcc74e46b6b306ffe5df1d7c6c6929bfa353e4fcc6c909de36d17bfe8d24ac0c` |

.152 reports the same internal build. Desktop package selection and the new
managed QR receiver check the exact xochitl hash, not the build number alone.
No generic 3.29 allowlist or test-history fingerprints are added.

## Offline Checks

- Compiled QMD hashes and full application against extracted stock resources.
  Chinese/Pinyin/reading/note/QR pass both forward and reverse combined replay.
- All 131 Qt imports from the QR bridge resolve in the extracted Qt 6.10.3.
  The native settings property, upload route, multipart support and USB interface
  indicators remain present. This does not prove cable-free runtime activation.
- Pinyin's AArch64 key/unicode/type offsets remain 72/80/108.
- The highlighter entry is now `0xcaddb0`; its unique signature, displaced
  prologue and entire normalized 0x220-byte function match the reviewed .152
  implementation. The same 3.29 hook can be used.
- The .153 translation supplement adds 59 exact identities. All 1793 required
  stock-catalog and embedded-QML keys pass placeholder and markup validation.
- Six packages (including the standalone tap compatibility carrier) are built
  into local caches. The consolidated reading plugin remains the normal UI.

Local evidence: `build/beta-3.29.0.153/validation.json` and
`build/qr-upload/validation-153.json`. Official image and resource provenance:
`E:/remarkable/firmware-cache/official-beta/3.29.0.153/chiappa/` and
`E:/remarkable/firmware-cache/work/beta-3.29.0.153/chiappa/`.
Use `tools/build_beta_329.py --release 3.29.0.153` with the same explicit
tool/cache arguments as the .152 builder; `qr-upload/native/build.py` verifies
the exact resource hash, and `qr-upload/build_assets.py --release 3.29.0.153`
registers that package without replacing .172.

## First Hardware Finding

On 2026-10-17, the exact .153 Move loaded the managed QR entry, but every start
exited with `native interface has a conflicting IPv4 configuration`. Read-only
inspection found `10.11.99.1/29` on `usb1`; its device networking units also
configure `/29`. The prototype wrongly required its temporary `/27` address.

The receiver now reuses existing `/29` or canary `/27` without changing it, adds
`/29` only when IPv4 is absent, and still refuses other addresses/prefixes and
mixed conflicting configurations. The `/29` regression failed before the fix;
Go tests/vet, ARM cross-builds, exact QMD replay and 17 Python QR/matrix tests
pass afterward. The .153 package/cache is rebuilt, with .172 unchanged. Against
the connected device and this computer's managed receipt store, normal rmtool
detection reports `repair_available`. No device mutation or restart was done;
installation and reception of this corrected package still need testing.

## Hardware Acceptance

On 2026-10-18, the updated component was installed through the normal verified
transaction and loaded after the user's manual reboot. MainView's root-private
control socket is present, with no QML/type/symbol errors. A desktop USB test
started with `WebInterfaceEnabled=false`, automatically activated native import,
submitted a generated PDF, found its native-created library entry, and verified
the setting returned to false with all active/restore markers cleared. No reader
restart occurred during import.

With USB unplugged (both carriers zero), the same desktop importer submitted a
small EPUB over the pinned Wi-Fi connection. Its native-created library entry,
byte-identical EPUB and generated PDF were verified, with the original false
setting and clean control markers restored. The system uses the EPUB metadata
title, not necessarily the uploaded filename; the hardware script now accounts
for this and still refuses duplicate test uploads.

An interruption check exposed a suspend-clock limitation: the kernel suspend
counter increased from 11 to 12 during 17 seconds without heartbeats, while Go's
monotonic timeout paused. Active polling subsequently let the old watchdog expire
and restore the setting. The helper now periodically compares elapsed wall time,
including suspend, and rejects a late heartbeat or backward clock jump. Both
packages were rebuilt; Go tests/vet, ARM builds, 42 Python tests (including all
26 sidebar replays) and 151 exact Qt imports pass. The updated .153 component is
installed through the normal managed transaction, without an automatic restart.
Full Python regression subsequently passed: 1279 tests, with four existing
resource/environment-dependent skips.

After the user's next manual reboot, the exact updated helper and root-private
control socket were verified, with managed status `enabled` and both USB carriers
zero. The desktop session automatically prepared the native HTTP service over
Wi-Fi and restored the original false preference, free control state and original
USB interface addresses. During 17 seconds without heartbeats, suspend count
increased from 2 to 3; the resumed watchdog restored settings and addresses by
18.53 elapsed wall seconds and exited with the expected heartbeat-expired error.
An SSH stdin-EOF test also returned `restored` and exit zero. The reader process
token remained unchanged; no documents were added or removed by these checks.
Latest desktop session, suspend-watchdog and EOF recovery acceptance now pass.
This is not hardware acceptance for other device/firmware targets.

1. Save work, manually reboot to finish the pending official update, and confirm
   Move reports 3.29.0.153. Do not bypass the pending-OTA guard.
2. Relaunch local rmtool, reconnect and use verified migration/repair for peers.
   Install QR transfer from Device tools. Follow the restart confirmation.
3. Without USB or rmtool running, start reception from Settings > 扫码传输.
   Upload PDF and EPUB from a phone on the same trusted Wi-Fi. Confirm progress,
   library appearance and that each book opens, including the prior large EPUBs.
4. Stop reception, lock/unlock, restart, reopen and stop reception again. Check
   that disabling/uninstalling QR leaves books and other plugins intact.
5. Retest Chinese, Pinyin, both tap directions, EPUB fonts, highlighting and
   note refresh. Other .153 platforms remain outside this adaptation.
