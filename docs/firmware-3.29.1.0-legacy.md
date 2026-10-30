# Stable 3.29.1.0: Paper Pure, reMarkable 1 and reMarkable 2

Updated on 2026-10-30. This document records offline adaptation evidence only. No target is marked as device verified.

## Exact official firmware

| Device | Platform | Internal build | SWU SHA-256 | xochitl SHA-256 |
| --- | --- | --- | --- | --- |
| Paper Pure | tatsu | 20261028104944 | `966d632cd60927ef6f36aef0db8268aeab2285d27b772ea45f292f72bc692cc8` | `c464f2ee5f03468834cb117672ebaaabb1e322cdab8510bbdc2f663886334531` |
| reMarkable 1 | rm1 | 20261028104944 | `215d43ec9d72dd296e729b0d18dea4bdd8946c6f9c8e8e1b4bcbf7278c624a5f` | `2bc210bd4e05b709ea15d7e4f357b816505a40fb1838c02f6b60040ea3c87690` |
| reMarkable 2 | rm2 | 20261028104944 | `7a0af4173d90bae0adf0c5a9e94e7eaf5e86cf741e38bdbec4e3788315c9da6f` | `4ab39bdf42503fe8d9e10a31a415b28b9aed6f25bd32788226c02ee23879fd13` |

Official images were verified by exact size and SHA-256 before extraction. Project-local qrex generated complete resource sets containing every required Settings, MainView, DocumentView, DeviceSceneView, SceneViewGestures and Storage QML source. Fresh hashtabs were generated from each exact tree.

## Plugin coverage

Fifteen new exact archives cover the three devices:

- Native Simplified Chinese and Pinyin input.
- Standalone tap-to-turn navigation.
- Independent native document import and QR reception.
- AppLoad v0.6.0 and KOReader v2026.07.1 on the new exact shared-runtime identities, using unchanged official upstream downloads.

Color-only reading/note enhancements, EPUB font menus, precise highlighting and WeRead are not exposed on these devices. RM1 keeps the early-home runtime layout; RM2 stays on data and does not wait for home before xochitl. Existing storage reserves, ARMv7 hard-float checks and startup protection remain in force.

The initial builds passed exact QMD checks, full QMLDiff compatibility/replay, combined peer replay, archive/member verification and architecture-specific stock ABI checks. Native-import bridges passed 121 strong imports on each ARM target and 126 on tatsu. QR bridges passed their exact Settings/MainView replay. A second independent build produced byte-identical archives and candidate manifests for all 19 core, five native-import and five QR packages.

## Clean installation and migration

Regression fixtures cover clean package selection and migration from .172/.153 on all three devices, including core plugins, native import, QR, AppLoad and KOReader. The independent matrix now covers 37 exact device/firmware identities and 20 localization carriers. New packages have been imported into local rmtool caches through the normal verifier. The final regression suite passed **1427 tests with 6 skipped**; Ruff, Python compilation and `git diff --check` also passed.

All targets remain `device_verified=false`. Use `.trellis/tasks/10-30-stable-32910-legacy/hardware-checklist.md` for separate hardware acceptance on each device. Offline QML/ABI evidence is not hardware evidence.
