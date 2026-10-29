# Beta 3.29.0.152 Adaptation

Prepared on 2026-10-11 for **rmtool v1.16.16**. All five platforms are offline
verified. Paper Pro reading-enhancement tap navigation passed user hardware
retesting on 2026-10-11; other device and feature hardware checks remain pending.
All five official images report internal build `20261009073202` and Qt 6.10.3.
Exact identities remain mandatory; this is not blanket support for 3.29.

## Coverage

| Feature | Paper Pro / Move | Paper Pure / RM1 / RM2 |
| --- | --- | --- |
| Independent Simplified Chinese | Offline verified | Offline verified |
| Offline Pinyin | Offline verified | Offline verified |
| Tap to turn | Included in reading enhancements | Standalone package |
| Reading enhancements, TOC, refresh preferences | Offline verified | Not ported |
| Custom EPUB font menu | Offline verified | Not ported |
| Note enhancements | Offline verified | Color-only; not applicable |
| Precise Chinese highlighting | Offline verified | Not ported |
| WeRead App / launcher | v1.0.0 and native launcher; offline integration | Not enabled |
| AppLoad / KOReader installation | v0.6.0 offline integration; hardware pending | v0.6.0 offline integration; hardware pending |

There are 19 new packages: five each for Chinese, Pinyin and standalone tap,
plus two each for reading and note enhancements. Normal color-device UI uses
the consolidated reading plugin. Existing package records were not replaced.

## Firmware Evidence

Official downloads use:

```text
https://dlqathbgqp3nv.cloudfront.net/beta/remarkable-production-image-3.29.0.152-{platform}-public.swu
```

| Platform | SWU bytes | Resources extracted | Required stock + QML translation keys | Stock CJK fonts |
| --- | ---: | ---: | ---: | --- |
| ferrari | 232022016 | 199 | 1779 | Noto Sans SC and JP |
| chiappa | 167869952 | 193 | 1748 | Noto Sans SC and JP |
| tatsu | 161291264 | 182 | 1709 | Noto Sans SC and JP |
| rm1 | 87731200 | 166 | 1623 | None |
| rm2 | 99557376 | 177 | 1720 | None |

`tools/build_beta_329.py:TARGETS` pins each complete SWU and xochitl SHA-256.
`tools/validate_build_matrix.py:OFFICIAL_329` independently pins the xochitl,
stock French carrier, and generated Chinese catalog identities. French records
are recovery guards only; the old French-slot installer remains frozen.

Local SWUs, extracted files and download provenance are under
`E:/remarkable/firmware-cache/official-beta/3.29.0.152/<platform>/`.
QREX trees, hashtabs and identity records are under
`E:/remarkable/firmware-cache/work/beta-3.29.0.152/<platform>/`.

## Adaptation Details

- The new LanguageSettings model requires a separate language-menu QMD.
  Both onboarding and normal settings append Simplified Chinese while retaining
  the stock selection callbacks. The old language patches do not apply.
- The translation supplement contains 68 new exact identities: 36 from the
  four stock catalogs and 32 additional embedded-QML strings. Every required
  context/source/comment/plural identity is covered; placeholders and rich-text
  markup are validated. Reviewed older translations remain in the catalogs.
- RM1/RM2 export `QString(QChar const*, int)`, not the AArch64 long-long version.
  A separate Qt32 translator is compiled and its constructor is host-tested for
  both allowed storage layouts. Published old translators remain unchanged.
- IME field offsets were checked in each new QtGui binary: key/unicode/type
  remain 72/80/108 on AArch64 and 40/44/60 on ARM. Register allocation changed,
  so the old instruction-pattern check alone is insufficient.
- Xovi Qt imports resolve against the extracted firmware exports; all packaged
  ELF files match their hardware architecture. Reading/note QMDs pass the full
  existing structural validators and combined forward/reverse replay.
- Precise highlighting has a separately compiled 3.29 hook. The selection object
  now points to the glyph container at offset 0; the glyph pointer/count remain
  at +8/+16, with UTF-16 at glyph +0x30 and stride 0x38. Range records grew from
  8 to 32 bytes, with the first/end glyph indices still at +0/+4. The hook skips
  word expansion for a CJK-starting selection and preserves the stock path for
  other text, disabled settings and invalid/empty inputs. No font or document
  content is changed. Existing 3.28 hook and package bytes remain unchanged.
- RM1 still mounts home before xochitl; RM2 still orders it after xochitl.
  Only the exact new RM1 runtime uses the existing home layout. Font capability,
  fallback, lock-screen storage selection and free-space checks are unchanged.

### Highlighter Evidence

The reviewed word-expansion entry is `0xcadd70` on Move and `0xcc8db0` on
Paper Pro (ELF virtual addresses, before ASLR). Both contain exactly one copy
of the new 40-byte signature. Their complete 0x220-byte function bodies are
instruction-equivalent after normalizing branch addresses. Calls from the
highlight-selection path at `0xcb0174` / `0xccb1d4` still expand words before
trimming whitespace. The five displaced prologue instructions have no
PC-relative operands and remain compatible with the existing trampoline.

`tools/build_beta_329.py:highlight_abi_check` checks the exact binary hash,
unique executable signature, prologue and full normalized function before
packaging. Disassembly is retained in `build/beta-3.29.0.152/validation.json`.
`test_highlight_329.c` executes the new pointer/range reader on the host, testing
CJK block boundaries, Latin text, punctuation, null/empty input and bad indices.
The separate native artifact is `rmtool-highlight-snap-3.29.so`, installed under
the same managed extension path; no extra plugin or migration fingerprint is added.

### Tap Navigation Follow-Up

Paper Pro hardware testing exposed a runtime API change despite successful QMD
replay: every recognized tap logged `view.moveForward is not a function` from
`SceneViewGestures.qml`. The plugin and its global/document switches were active.
In 3.29, `DeviceSceneView` exposes `leftPageRequested` / `rightPageRequested`
signals; `DocumentView` handles them and calls its own bounded navigation methods.

The exact beta source adapter now uses these stock swipe signals in both the
standalone tap plugin (all five devices) and reading enhancements (Pro/Move).
Tap zones, settings and link handling are unchanged. Published older sources
and manifest records are preserved. The build validates the stock signal and
handler chain plus injected calls after each combined QMD replay. Host JS tests
exercise both directions, links and inactive regions with no legacy methods.
The user confirmed that the corrected reading package works on Paper Pro on
2026-10-11. This corrected payload is the only 3.29 tap implementation published;
failed local candidates and their fingerprints are not added to release history.

## Build and Verify

Using the already extracted exact firmware/resource caches:

```powershell
build/.venv/Scripts/python.exe tools/build_beta_329.py `
  --firmware-cache E:/remarkable/firmware-cache/official-beta/3.29.0.152 `
  --resources E:/remarkable/firmware-cache/work/beta-3.29.0.152 `
  --qt-bin E:/remarkable/firmware-cache/tools/qt-6.10.3/6.10.3/msvc2022_64/bin `
  --qmd-tool build/reading-enhancements-qmd/qmd-tool.exe `
  --qmldiff E:/remarkable/qmldiff-source/target/release/qmldiff.exe `
  --python-libs E:/remarkable/firmware-cache/tools/python `
  --zig E:/remarkable/tools/zig-0.16.0/zig.exe `
  --output-dir build/beta-3.29.0.152
```

This builds candidates without editing production manifests, installing on a
device, or uploading resources. Outputs include per-feature candidate manifests,
carriers and validation evidence. Both independent builds produced byte-identical
archives for all 19 packages. Local packages were imported through rmtool's
normal verifier into `.rmtool/cache/<feature>/20261009073202/`; subsequent package
loads succeeded with network access mocked out.

`tools/validate_build_matrix.py` passes all 27 device/firmware targets. The full
suite passed **1143 tests, zero skips**, using the local QMD tools and
`build/reading-enhancements-matrix-18.json`. Log:
`build/beta-3.29.0.152/highlight-tests.log`. The resource-only builder does not build a
desktop executable or change the application release version.

Tap follow-up verification: **1145 tests passed, zero skips**, recorded in
`build/beta-3.29.0.152/tap-fix-tests.log`. Candidates in `tap-fix/` and
`tap-fix-repeat/` are byte-identical across all 19 packages; only the seven tap
QMD payloads changed. All published manifest records are unchanged. The seven
updated archives passed normal cache import, per-member verification and
offline loading. Read-only detection on the connected Paper Pro reports
`repair_available` through its existing managed-install receipt, without adding
temporary test fingerprints to the public catalog.

## Before Publication

The user installed the corrected package through rmtool and confirmed working
taps on Paper Pro. The adaptation tools only inspected the device read-only.
Package-level hardware flags remain conservative because a tap retest does not
establish every feature on every platform. Complete these checks on each model
before setting its `device_verified` flag:

1. Install via source rmtool, reboot manually, select Chinese, and reopen settings.
2. Test soft/physical keyboard Pinyin and Chinese labels, then another reboot.
3. Test passcode cold boot and unlock, including the RM1/RM2 fallback-font choice.
4. Test tap regions; on color models also test TOC, custom EPUB fonts, three
   reading refresh modes, color pen input and notebook delay/page-only refresh.
5. Test individual disable/uninstall, peer preservation and firmware migration.
6. On Paper Pro/Move, test precise highlighting in both EPUB and searchable PDF:
   short Chinese phrases, mixed Chinese/English, punctuation and multiple lines.
   Verify English word expansion, the global/off switch, erasing/undo and saved
   highlights after reopening. A scanned PDF without a text layer is not a target.

Offline QMD replay and ABI checks cannot establish e-ink behavior, UI stability,
or cold-boot safety on real hardware. Do not generalize the tap retest to all
devices or features.
