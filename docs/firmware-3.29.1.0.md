# Stable 3.29.1.0: Paper Pro and Move

Updated on 2026-10-29. Offline adaptation evidence is recorded below. The user subsequently confirmed plugin migration and the WeRead launcher on Move; only that launcher is marked as device verified.

## Exact official firmware

| Device | Platform | Internal build | SWU SHA-256 | xochitl SHA-256 |
| --- | --- | --- | --- | --- |
| Paper Pro | ferrari | 20261028104944 | `b94772f860ba8273a35b0dea24d1b74c28b6e8af718f7767c7417687721e2401` | `35e940d8acb3de0dfaf61f1a421d450e61dbaa915f885c818a2a3cc5c94c8ecd` |
| Paper Pro Move | chiappa | 20261028104944 | `ce3b42e6178dc26fe5658c505aa97b54c062fe30597be26661c01ecd803e62df` | `9a1391c4a2d26c1c364de078d37773f0464da296039097e88e475ecf807bb6b4` |

The official production images were downloaded from reMarkable's public firmware bucket and checked by exact size and SHA-256 before extraction. Complete Qt resources and fresh hashtabs were generated from each exact binary. Other models and other 3.29.x releases remain unsupported unless separately catalogued.

## Plugin coverage

Sixteen new exact archives cover both color devices:

- Native Chinese and Pinyin input.
- Reading enhancements, including EPUB font menus, tap navigation, refresh controls and precise Chinese highlighting.
- Note enhancements and the trusted tap-to-turn migration package.
- Independent native document import and QR reception.
- The rmtool WeRead launcher; official RemarkableWeRead v1.0.0 remains unchanged and accepts these exact targets.
- AppLoad v0.6.0 and KOReader v2026.07.1 remain official upstream downloads and become installable on the new exact shared-runtime identities; no upstream archive is mirrored or modified.

The initial candidate builds passed exact QMD checks, QMLDiff compatibility and complete stock-QML replay, combined peer replay in both load orders, archive/member verification, AArch64 checks and stock ABI import checks. The stable highlighter signature, prologue and normalized expansion-loop layout match the reviewed .153 implementation on both devices.

Native-import packages validated the stock `/upload` indicators, web importer, USB address configuration and 126 strong bridge imports per target. QR packages use the same verified native-import carrier and passed their own Settings/MainView replay. WeRead launcher packages completed full QML replay rather than the earlier hash-only fallback. A second independent build of all 16 archives produced byte-identical archives and candidate manifests.

## Clean installation and firmware-upgrade migration

The independent matrix now covers 34 exact device/firmware identities and 17 localization carriers. Regression fixtures cover clean package selection and migration from 3.28.0.172 and 3.29.0.153 on both devices, including native Chinese, Pinyin, reading/note enhancements, native import, QR, WeRead, AppLoad and KOReader. The migration path retains enabled states and external application data and still requires one verified atomic transaction with rollback.

All newly generated archives have been imported into local rmtool caches through the normal package verifier. No resource has been uploaded to GitHub or COS and no device has been modified by the build process. The final regression suite passed **1425 tests with 6 skipped**; Ruff, Python compilation, the independent matrix and `git diff --check` also passed.

## Hardware acceptance

The user confirmed on Move that plugin migration succeeds and the WeRead launcher works correctly. `weread-launcher` on chiappa is therefore marked `device_verified=true`; this does not certify every feature or Paper Pro. Continue the remaining checklist in `.trellis/tasks/10-29-stable-32910-plugins/hardware-checklist.md` separately on both devices, including cold boot, fresh installation, PDF/EPUB navigation and refresh, precise highlighting, native import, QR reception, AppLoad/KOReader, disable/uninstall and stock recovery. Offline QML/ABI evidence is not hardware evidence; all other new targets remain `device_verified=false`.
