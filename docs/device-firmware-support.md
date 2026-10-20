# Device, Firmware and Feature Support

Updated on 2026-10-20 for v1.18.1. This describes **rmtool's installation gates**,
not everything an upstream application might run on. A package requires the
exact platform, architecture, internal build and stock xochitl SHA-256.
3.29.0.152 resources are included in v1.16.16. All five platforms passed offline
checks; Paper Pro reading-enhancement tap navigation passed hardware retesting.
Other device and feature hardware checks remain separately recorded.

## Known Firmware

| Public release | Internal build | Platforms | Recorded channel |
| --- | --- | --- | --- |
| 3.27.1.0 | 20260506100933 | ferrari, chiappa | stable |
| 3.27.3.0 | 20260612085811 | ferrari, chiappa, tatsu, rm1, rm2 | stable |
| 3.28.0.162 | 20260629074044 | ferrari, chiappa | beta |
| 3.28.0.163 | 20260702125656 | ferrari, chiappa | beta |
| 3.28.0.164 | 20260702125656 | ferrari, chiappa | beta |
| 3.28.0.166 | 20260806095513 | ferrari, chiappa | beta |
| 3.28.0.169 | 20260806095513 | ferrari, chiappa | beta |
| 3.28.0.172 | 20260827113527 | ferrari, chiappa, tatsu, rm1, rm2 | stable |
| 3.29.0.152 | 20261009073202 | ferrari, chiappa, tatsu, rm1, rm2 | beta |
| 3.29.0.153 | 20261009073202 | ferrari, chiappa, tatsu, rm1, rm2 | beta; native-transfer coverage below |

Paper Pro = ferrari; Move = chiappa; Paper Pure = tatsu. RM1/RM2 use armv7l;
the other three use aarch64. Internal builds are not unique firmware identities:
.163/.164 and .166/.169 must still be distinguished by the exact binary hash.
Unlisted device/release combinations are not inferred from a neighboring model.

The [official public inventory](https://remarkable-software.s3.us-east-2.amazonaws.com/?list-type=2)
currently lists 3.28.0.172 and 3.27.3.0 for all five models. Inventory presence
does not establish the stable/beta channel; the table retains the project's
previously verified channel records. It also does not promise an OTA rollout.

## Plugin Matrix

`Yes` means an exact installable package exists, not that this audit ran it on
hardware. `No` means no current rmtool port. `N/A` means a color-only feature
does not apply to a monochrome screen.

| Device group | Firmware | Native Chinese | Pinyin | Tap pages | Reading enhancements | Note enhancements | EPUB font menu | Chinese precise highlighting | WeRead App | WeRead launcher | AppLoad / KOReader install |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Paper Pro, Move | 3.27.1.0, 3.27.3.0 | Yes | Yes | In reading | Yes | Yes | No | No | No | No | Yes |
| Paper Pro, Move | 3.28.0.162, .163, .164, .166, .169 | Yes | Yes | In reading | Yes | Yes | Yes | No | Yes | No | No |
| Paper Pro, Move | 3.28.0.172 | Yes | Yes | In reading | Yes | Yes | Yes | Yes | Yes | Yes | No |
| Pure, RM1, RM2 | 3.27.3.0 | No (legacy French-slot resource only) | No | Yes | No | N/A | No | No | No | No | Yes |
| Pure, RM1, RM2 | 3.28.0.172 | Yes | Yes | Yes | No | N/A | No | No | No | No | No |
| Paper Pro, Move | 3.29.0.152 beta (offline) | Yes | Yes | In reading | Yes | Yes | Yes | Yes | No | No | No |
| Pure, RM1, RM2 | 3.29.0.152 beta (offline) | Yes | Yes | Yes | No | N/A | No | No | No | No | No |
| Move | 3.29.0.153 beta (offline) | Yes | Yes | In reading | Yes | Yes | Yes | Yes | No | No | No |
| Paper Pro, Pure, RM1, RM2 | 3.29.0.153 beta | No | No | No | No | N/A on monochrome | No | No | No | No | No |

- The standalone tap/fast-mono packages remain trusted for migration. On color
  devices their normal UI entry is consolidated into reading enhancements;
  they are not additional recommended installations.
- Reading enhancements includes the native table-of-contents shortcut, per-book
  refresh preferences and forced refresh. Its color/mono refresh modes are
  restricted to color-device PDF/EPUB reading. The Chinese highlighting hook
  exists for Paper Pro/Move 3.28.0.172 and 3.29.0.152, plus Move 3.29.0.153
  (beta hook packages are offline verified).
  Reading-specific features on Pure/RM1/RM2
  are missing ports, not intrinsically impossible on those screens.
- Note enhancements specifically delays color settlement after pen lift or
  until a page change. Do not advertise it on monochrome devices.
- EPUB menu injection depends on the reading package and a listed 3.28 build or
  exact 3.29.0.152, plus Move 3.29.0.153. It is separate
  from generic font upload/system-font selection; those remain available on all
  five devices subject to file, glyph, storage and mount checks.
- The WeRead App installer accepts the 3.28 series on Paper Pro/Move. Only .172
  has an rmtool launcher package; an App install is not proof of launcher support.
- AppLoad is pinned to v0.5.3 and KOReader to v2026.07.1. The rmtool installer
  accepts only the listed 3.27 builds. A trusted historical AppLoad peer on .172
  exists for preservation/recovery, **not** installation permission.
  [Upstream has published v0.6.0](https://github.com/asivery/rm-appload/releases/tag/v0.6.0),
  but it is not integrated or validated by this audit. Do not describe current
  rmtool restrictions as proof that no newer upstream solution exists.
- The former French-slot path is frozen and is not exposed as a new toolbox
  installation. Its 3.27.3 legacy resources do not provide the independent
  native-Chinese plugin on Pure/RM1/RM2. .172 carrier records are recovery guards.
- v1.18.1 updates native-Chinese startup compatibility for RM1/RM2 on stable
  3.28.0.172 and beta 3.29.0.152. The rebuilt ARM extension and old-package repair
  paths passed local checks; the reported RM1 startup crash still awaits a
  device backtrace or hardware retest. Keep existing startup protection until
  the diagnostic follow-up in [Issue 20](https://github.com/pretenderlu/rmtool/issues/20).

## Other Tools

### Native Desktop Import (v1.18.0)

Document-center PDF/EPUB uploads use the tablet's own `/upload` importer over
the selected SSH connection. UUIDs, metadata and EPUB conversion belong to the
system; rmtool does not fall back to direct library writes or restart the reader.

Automatic preparation has independent exact-target packages for **RM1, RM2,
Paper Pure, Paper Pro and Move on stable 3.28.0.172 and beta 3.29.0.153**.
The control component can be initialized/removed from the document center;
its first load needs one manual reboot. QR reception is not a prerequisite and
its separate Move-only support is unchanged. An authenticated, already loaded
QR control component remains usable. Existing enabled native services are left
unchanged; temporary activation restores the original setting and only removes
addresses owned by that session.

All ten packages passed exact stock ABI and peer-QMD replay checks. The current
Move .153 QR-based control path has hardware evidence; the new independent
component on each device/firmware still requires hardware acceptance. This
does not expand localization, QR or other plugin support on the four additional
.153 targets. Existing incompatible plugin residue remains guarded. See
[build and test details](../native-import/README.md). Do not infer automatic
activation support on other builds from the shared public version number.

| Feature | Paper Pro / Move | Pure | RM1 / RM2 |
| --- | --- | --- | --- |
| SSH, documents, wallpapers, time, diagnostics | Runtime checks | Runtime checks | Runtime checks |
| Font upload and system-font selection | Runtime checks | Runtime checks | Runtime checks; small /data |
| Screen preview | DRM mapping probe | DRM mapping probe | Legacy framebuffer probe |
| Phone QR reception | Move .172 / .153 only | Not ported | Not ported |
| Firmware catalog / download in the UI | Available | Not exposed | Not exposed |
| Firmware write / A-B switching | Validated new A-B layout only | Not enabled | Not enabled |

The backend catalog parser recognizes five devices, but the UI exposes only
Paper Pro/Move. Pure/RM1/RM2 get a clear unsupported status before partition
probing. `parse_state()` and the writer
allow only Paper Pro/Move with the expected partition map and boot schema.
Root access, sufficient storage, charger state and current transaction guards
remain mandatory. Generic tools are not a promise of every unknown firmware.

The lock-screen font option chooses the storage path: enabled mirrors one active
font to /data; disabled uses /home and skips the /data capacity requirement.
EPUB-only font files stay under /home. Never infer working Chinese fallback just
from a 3.28 version string or from a font file being present; the live font match
and glyph checks remain authoritative.

## Evidence and Reproduction

The entries below record the 2026-10-07 baseline. For the five additional beta
targets, 19 new archives, Qt ABI checks, translation coverage and hardware test
checklist, see [3.29.0.152 evidence](firmware-3.29.0.152.md). Existing package
records and device-verification flags were preserved.

- All 22 known target xochitl hashes and `/etc/version` values matched locally
  extracted cached firmware. Ferrari .166 also has a device-derived baseline;
  this does not create new real-device verification claims.
- All 110 current plugin archives passed outer size/SHA-256 and exact inner-file
  validation with the existing package verifier. Their 393 ELF payloads matched
  the target ARM architecture. Fourteen missing current reading packages were
  downloaded into an isolated build verification directory; old user cache
  files were not replaced or deleted.
- `python tools/validate_build_matrix.py` checks all seven plugin coverage sets,
  exact identity agreement, common runtime files, shared feature layouts,
  AppLoad/KOReader and WeRead installation boundaries, screen-preview profiles,
  and the five .172 French-carrier records. Missing a target from both backend
  code and its manifest must still fail the independent coverage check.
- `python -m unittest discover -s tests -p test_build_matrix.py -v` checks every
  selector against every known identity and rejects unknown hashes/architectures;
  it also checks that generic font support cannot imply EPUB menu support.
- The 16-target reading/note offline suite was run with `RMTOOL_QMD_TOOL`,
  `RMTOOL_QMLDIFF` and `RMTOOL_READING_MATRIX_CONFIG` pointing to the existing
  local tools and `build/reading-enhancements-matrix-16.json`. All 12 tests
  passed, including compilation/replay and both plugin load orders.
- With those same offline fixtures configured, the complete regression suite
  passed 1101 tests with no skips in 77.656 seconds. Log:
  `build/coverage-full-test.log`. Compilation and `git diff --check` also passed.
- Hardware verification is feature-specific. Current manifests mark native
  Chinese on Move 3.27.3 and Pro .166; Pinyin on Pro .166; reading on Move 3.27.3/.172;
  WeRead launcher on Move .172. Other package flags remain offline-only.
  This audit did not connect to, install on, or reboot any device.

Exact source contracts: `_tap_page_turn.py`, `_native_chinese.py`,
`_pinyin_input.py`, `_reading_enhancements.py`, `_note_enhancements.py`,
`_rmkit_cn.py:get_epub_font_slot_status`, `_appload.py:app_asset`,
`_weread_app.py:_supported`, `_weread_launcher.py`, `_screen_preview.py`,
`_firmware.py:parse_state`, and each feature's bundled `manifest.json`.
