# Audit Remediation, 2026-11-04

Baseline: v1.21.3 / `6c28f92`. Source: `review-2026-11-04.md`.
No device operations, Git commits, push or publication are authorized by this task.
Existing untracked review/handoff files are not part of these changes.

## Status Definitions

- Reproduced: a failing regression or direct source/binary reproduction exists.
- Fixed (offline): the changed behavior has a passing regression. This is not
  hardware validation or complete batch acceptance.
- Hardware pending: requires an actual tablet, firmware or service lifecycle.
- Not established: the report's inference has not been independently confirmed.

## Batch 1: Data Safety and Transactions

| Report | Status | Change / Evidence |
| --- | --- | --- |
| H1 | Fixed (offline) | Hidden rows are deselected and excluded from destructive selection; confirmation names visible documents. Qt regression. |
| H2 | Fixed (offline) | Tap and legacy fast-mono feature actions refuse cross-firmware base deletion and direct users to migration. Peer-preservation tests. |
| H3 | Fixed (offline) | KOReader calls the shared removal API with the correct argument order; absent bridge is skipped. |
| H4 | Fixed (offline) | Independent non-zero signal exits invoke rollback. Real shell test moves user files before HUP/INT/PIPE/TERM. |
| H5 | Fixed (offline), hardware pending | Pinyin retains verified files on disable. Shared transaction stops its known unit, swaps staged payload and marker, and restores files/service on failure. |
| H8 | Fixed (offline), hardware pending | Exact transaction move record bound to boot/PID/start time plus trusted mapped bytes/inode; no backup-name wildcard or skipped checks. |
| M1-M2 | Fixed (offline) | KOReader protects native document/program/state trees and their deletion ancestors; `.sdr` uses the book stem. |
| M3 | Fixed (offline) | Missing official index blocks uninstall before mutation; repair keeps the entire old directory, not only guessed user paths. |
| M4 | Fixed (offline) | Reinstall only removes preserved data it actually used, not unrelated earlier recovery data. |
| M5 | Fixed (offline) | A valid managed marker with a changed version becomes repairable, retaining a full old install during repair. |
| M15 | Fixed (offline) | Drop-in-only residue chooses the device's preferred layout, including RM1 home storage. |
| M16 | Fixed (offline), hardware pending | Effective guards, snapshots before either drop-in deletion, persistent sibling backup, original root mode and failed-unmount handling. Real shell tests for overlay and aliased lower files. |
| M17 | Fixed (offline) | Inert external parent directory does not imply an unknown loader; verified inactive Pinyin payload can be preserved. Active unknown processes still block cleanup. |
| M18-M19 | Fixed (offline), hardware pending | AppLoad reports startup protection; verified controls-only repair does not rebuild external apps. Mapping checks share H8's provenance gate. |
| M26 | Fixed (offline) | Tap disable supplies the current replacement specification for receipt-owned older payloads. |

Additional prerequisites: H7's missing `device_verified` field access is guarded;
the isolation test now creates relative-output fixtures on the caller's drive,
allowing a C: runner to build from an E: repository.

### Validation

- Targeted run: 573 tests, 5 skipped, passed (before final added regressions).
- Real shell cleanup/Pinyin transaction subset: 5 tests passed.
- Ruff passed; release inputs and existing 37-target compatibility matrix passed.
- Initial full regression: 1546 tests passed (12 skips); 63 isolated modules passed.

Pending hardware checks include `/proc/PID/map_files` access on supported kernels,
actual systemd sidecar stop/rollback behavior, both real root-mount layouts, and
disconnect outcomes. Passing modeled scripts does not establish these behaviors.

## Batch 2: Immutable Resources and Builds

| Report | Status | Change / Evidence |
| --- | --- | --- |
| H10 | Fixed (offline) | New resource names bind the full SHA-256; legacy names still parse with strict hashes. Publishing rejects a same-name/different-byte object and uncertain probes rather than replacing it. Workflows fetch only manifest-referenced assets; historical remote assets are retained. |
| M32 | Fixed (offline), documented legacy binary limit | Tool/tree lock and an AArch64 translator builder added. Original build entry points now check Python/zlib and tool hashes; reading/note/WeRead check exact carrier hashtabs; external Pinyin server/hook inputs must match reviewed size/hash. Existing published translator bytes stay pinned; the newly reproducible AArch64 candidate is not silently substituted. |
| M33 | Fixed (offline) | Specified Python/zlib, LF inputs and compiler path maps. Full reading (22) and tap (37) package builds repeated byte-identically; helpers/bridges and translator builds repeated in distinct temporary paths. |
| M34 | Fixed (Windows offline/HTTPS), macOS pending | Shared Windows/macOS inventory and self-test load Qt, 11 plugin manifests plus published fingerprint data, both fonts, five frames, icon, SVG and CA. Directory and one-file executables passed from a C: temporary directory; removing the fingerprint data made the directory check fail. Both HTTPS smoke tests passed. |

Old clients cannot be promised access to every historically overwritten URL.
Hash failure guidance recommends upgrading, never ignoring the mismatch.

## Batch 3: Firmware and Functional Corrections

| Report | Status | Change / Evidence |
| --- | --- | --- |
| H6 | Fixed (offline), hardware pending | Rebuilt all 15 native-import and 11 QR packages. Five exact 3.29.1.0 identities and corresponding official web UI/network files are audited for addresses. |
| H7 | Fixed (offline) | Optional package verification metadata no longer raises during tap enable. Qt regression clicks the action and checks blocked residue actions. |
| H11 | Fixed (offline), hardware pending | Separate .153 and 3.29.1.0 highlighter identities/binaries; executed C acceptance/refusal tests, exact stock signature/layout checks and ABI imports. |
| M20, M22 | Fixed (offline), hardware pending | First install and upgrade use the same external-loader checks. Published predecessor combinations are shared across features, before local receipts; inactive external files are preserved, active unknown loaders refuse. |
| M23-M25 | Fixed (offline) | Firmware residue is diagnosed using marker identity and directed to migration instead of enabling failing repair actions. Native-import install/remove retains the verified installed peer map. |
| M28 | Fixed (offline), hardware pending | Tap sends stock touch notification before page navigation. Real JS state tests cover all three adapters; all 37 tap and 22 reading packages rebuilt/replayed, with repeat archive comparison. |
| M41 | Fixed (offline) | AppLoad v0.5.3 is pinned for both architectures and recognized without this computer's receipts. Schema 1/2 state-model tests preserve peers and reject unknown variants. |

The native matrix now combines manifest/source identities, executed acceptance
and refusal gates, ELF architecture and archive member hashes. Binary strings
alone are not the acceptance criterion. No device has been connected.

## Batch 4: Fonts, Localization and Firmware Recovery

| Report | Status | Change / Evidence |
| --- | --- | --- |
| H9, M10 | Fixed (offline) | Status paths perform no cleanup or language/font writes; stale state is retained for explicit recovery. |
| M7-M9 | Fixed (offline), hardware pending | Font ownership is retained across features; removing localization cannot remove a later user override. Persistent bounded rollback snapshots and an installing phase precede font writes. |
| M11 | Fixed (offline) | Failed localization restores the configuration immediately preceding this operation, not the entire historical xochitl.conf. |
| M12 | Partly fixed (offline), explicit limitation | Damaged/stale backup state exposes explicit recovery and invalid EPUB index references can be repaired. An unknown historical QM without a trusted original backup remains protected; rmtool does not guess its provenance or overwrite it. |
| M13 | Fixed (offline), hardware pending | User-partition sleep wallpaper records recoverable transactions, exposes errors/recovery, and cannot silently retry a failed home upload as a root upload. |
| M14 | Fixed (offline) | Native-Chinese disable/remove completes verified file operations under the lock before changing the language. |
| M6 | Fixed (offline), hardware pending | Uploaded image bytes are not reserved twice. Only provably unsubmitted, operation-owned staging is removed; uncertain commits retain jobs, updater protection and evidence. |
| M39-M40 | Fixed (offline), hardware pending | Failed-task recovery is separate from starting a new write. Official swupdate states were checked; unknown outcomes remain locked. Failed standby partitions are quarantined until a successful reinstall, not merely warned about. |

Font/localization worker validation: 314 combined tests, including real shell
root transactions and 43 readonly/stateful regressions. Firmware: 129 targeted
tests plus fault models for upload, hash, commit acknowledgement and reprobe
failures. These results predate final whole-tree acceptance.

## Batch 5: Interaction, Correctness and Performance

| Report | Status | Change / Evidence |
| --- | --- | --- |
| M21 | Fixed (offline) | Current disconnect failures remain visible; callbacks from a replaced connection are discarded. Progress windows are owned by their individual task. |
| M27 | Fixed (offline), hardware pending | Generation-bound async results, serialized commits and retained typed text on failure. 23 real QJSEngine/QML tests; all 34 packages rebuilt and replayed. |
| M29 | Fixed (offline) | Export identifiers and every recursively downloaded entry are validated; Windows traversal, reserved names, ADS, links and special files refuse before local writes. |
| M30 | Fixed (offline) | Blank pages, v6 horizontal origin and highlighter alpha corrected. PDF/diagnostic outputs replace atomically; PDF rendering retains only one page canvas. Pixel and failure tests included. |
| M31 | Fixed dependencies and decoder paths (offline), residual Qt risk | Official advisories checked before pinning Pillow 13.0.0 and Paramiko 5.0.1 in both hash locks. Images use bounded approved Pillow formats/raw pixels rather than Qt auto-detection. Supported Qt5 Windows wheels still contain an old runtime; this is not a claim that Qt itself has been patched. |
| M35 | Fixed (offline) | Each detection reuses a fresh readonly file/metadata/hash snapshot across predecessor combinations. Process/loader observations stay live; mutation paths revalidate. No persistent trust cache. |
| M36 | Fixed (offline) | Font, catalog and managed wallpaper verification use device-side SHA-256 instead of repeatedly downloading files. Required rollback snapshots remain. |
| M37 | Fixed (offline) | Downsampled/debounced image interactions, full-resolution output only when saving, EXIF orientation and released cover dialogs. |
| M38 | Fixed (offline) | Firmware page captures connection state without waiting for the transport lock on the GUI thread; worker-side validation remains. |

Pinyin cursor changes without reliable text evidence preserve composition rather
than guessing backspace/enter. A zero-width placeholder may remain in that
ambiguous case; actual Qt/device input behavior still needs hardware testing.

### Dependency Evidence

- Pillow: official advisories [FPX/JPEG](https://github.com/python-pillow/Pillow/security/advisories/GHSA-5x8w-8jj5-q52h), [TIFF](https://github.com/python-pillow/Pillow/security/advisories/GHSA-462x-pgg6-55fp), [PDF](https://github.com/python-pillow/Pillow/security/advisories/GHSA-p4wg-p73j-c528).
- Paramiko: [official changelog](https://www.paramiko.org/changelog.html); RSA SHA-256 host-key/password negotiation passed using real local transports, with no SHA-1 fallback. Cached RM1/RM2 3.29.1.0 Dropbear binaries advertise RSA SHA-256 and Ed25519. Older device handshakes remain untested.
- Qt: [official WebP advisory](https://www.qt.io/blog/two-qt-security-advisorys-gdi-font-engine-webp-image-format). No compatible newer official Windows Qt5 wheel was found. Decoder-path mitigation does not replace a supported Qt runtime.

### Low Findings

| Finding | Status / handling |
| --- | --- |
| Carousel shell quoting, lower drop-in guards, AppLoad shim guard, KOReader guard | Quoted paths and effective failing guards; shell/state regressions. |
| Atomic ZIP/PDF output, plain-text dialogs | Fixed; old destinations survive injected failures; remote text cannot trigger rich-text rendering. |
| Overlapping progress, recovery feature intent, case-colliding KOReader downloads | Fixed with ownership/intent checks and refusal before download. |
| Non-UTF-8 KOReader names | Read-only encoded-name fallback lists valid names and skips unrepresentable names without renaming/deleting them. |
| Reboot timeout / BusyBox writer match / firmware guard presentation | Bounded commands; checked official shell semantics and explicit failure/unknown guidance. Actual disconnect completion remains hardware pending. |
| Capacity recovery on the wrong partition or insufficient reclaim | Refused before any cleanup; state-model tests. |
| Pinyin retry, cursor, visible digits and plaintext input logs | Fixed source with real JS/QML tests; bounded retry, visible five-candidate selection and redacted logs. |
| WeRead synchronous binary hashing on page open | Process-local successful hash cache keyed by device/inode/size/timestamps/mode/owner. Changes invalidate it; launch always forces full SHA. Native cache state tests and all eight rebuilt packages' ABI/QMD checks pass. |
| Hashable Pinyin package URLs | Stored as tuple, not set. |
| Draft release notes / local bat environment | Draft reruns update notes before uploading. Bat prefers the project venv, then explicit Python 3.12. |
| Cross-drive isolation / platform CI | Relative fixture output stays on its caller's drive; CI now covers Linux, Windows and macOS, with an extra mapped-drive Windows run. Hosted runs have not been triggered. |
| SSH test timing | Worker exceptions are asserted, barrier/join deadlines bounded with more scheduling headroom; no claim that every load-induced race is eliminated. |
| Wrong wallpaper device profile | Upload verifies the actual machine before root/home mutation; mismatch refuses with reselection guidance. |
| Firmware labels / backup documentation | 3.29 support text corrected; README now accurately says the first wallpaper backup is retained. |
| Alleged dead code | `find_device_by_name` and `is_active_document_metadata` have production callers, so those claims are not established. Compatibility/test helpers and the old ARM translator trust record are retained; deleting an old trust anchor would regress installed devices. |
| Large status functions / private session access / broad catches | Structural splitting remains deferred as agreed. Failure suppression in carousel migration now propagates instead of pretending no backup exists. Other broad catches are not removed indiscriminately. |
| Embedded native build paths | New QR/WeRead/native builds use prefix maps and byte scans; repeated builds match across temporary directories. Legacy, unchanged pinned ELF bytes are not claimed to have been rebuilt. |

### Published Baseline and Hardware Labels

`assets/published-plugin-features.json` records only the published v1.21.3
payloads, checked against tag `aa50e52265681bcdb413479f4de7817d3e1e23f1`.
It covers the 127 changed tap/reading/Pinyin/WeRead/QR/native-import records;
no intermediate local build was added as a published predecessor. Existing
older explicit trust anchors remain. Full file fingerprints, not a common
"done" tag, authorize recognition. New payloads are marked offline-verified
and hardware-unverified; previous model testing does not verify these builds.

The final integration reproduced a Windows Qt use-after-free in KOReader's
progress callback. Task ownership now uses plain identity tokens rather than
capturing an auto-deleted QRunnable. The previously crashing four-upload test
sequence and complete 91-test KOReader module pass. This was fixed before the
final frozen builds; it is not dismissed as a mock-only failure.

## Final Acceptance

Local offline acceptance passed, subject to M12's protected unknown-history
case and the host/device limits below. Earlier failing integration snapshots
are retained as debugging evidence, not counted as passing tests.

| Check | Actual result |
| --- | --- |
| Upgraded dependency full suite | 1699 tests, 12 skipped, passed in 207.498 s; `build/audit1104-full-accepted.log`. |
| Isolated modules, C: temp / E: repository | 73 modules passed; `build/audit1104-isolation-accepted.log`. |
| Published predecessor model | All 127 changed targets recognized without local receipts; eight focused tests passed after expanding tap coverage. |
| Compatibility / native binary matrix | 37 device/firmware targets, nine plugin matrices, 20 localization carriers; 34 native archives verified, with executed Go/C identity refusals. |
| Native toolchain lock | Executables and Zig/Go/Qt tree hashes passed; `build/audit1104-toolchain-final.log`. |
| Native/Qr QMD combinations | All 15 native-import and 11 QR targets replayed with peers in both orders/sidebar subsets; four tests passed, `build/audit1104-qmd-peers.log`. |
| Reading/note QMD combinations | All 22 color-device/firmware targets compiled/replayed and note/reading load orders checked; 12 configured tests passed, `build/audit1104-reading-note-qmd-accepted.log`. Replaced stale hardcoded 20-target assertions with the supported identity set. |
| Ruff / compilation / diff | Passed; all 54 root Python modules and tools/tests compiled; release inputs remain valid for unchanged v1.21.3. |
| Windows frozen directory and one-file | Both built with the upgraded hash-locked environment; both resource and HTTPS self-tests exited 0 outside the repository. Missing fingerprint resource refused with exit 1 and was restored. |
| macOS | 19 locked wheel downloads/hashes verified only. No macOS runtime/app build or hosted workflow executed. |

The 12 default skips comprise POSIX permission/symlink checks and optional
local firmware/QMD fixtures. Native/QR peer-order and reading/note replay were
separately enabled and passed; seven default skipped cases remain unexecuted
on this host. The default full-suite result alone does not establish them.

Generated test builds are below `build/audit1104-final-frozen/`; rebuilt resource
packages and detailed identities are below `build/audit1104-native/`. They are
local artifacts, not public download locations. The AArch64 translator recipe
produces a repeatable candidate, while the existing published ELF stays pinned
pending separate ABI/hardware acceptance.

At the offline checkpoint, no hardware connection, push, publication, version
bump or Git commit had occurred. All seven remediation subagents were explicitly
closed. Task closure and macOS/hardware acceptance were not implied.

## Release Follow-up

The maintainer subsequently authorized committing and publishing this maintenance
as v1.21.4. Release metadata and both READMEs are updated. The six rebuilt feature
releases comprise 127 manifest-referenced packages; publication retains historical
assets and verifies new public bytes before the application release. Windows
builds and hosted Windows/macOS release checks are rerun for the versioned source.
Hardware validation remains pending; no device operations are part of release.
