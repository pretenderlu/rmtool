# 3.28.0.172 five-device offline support

The official stable images for Paper Pro (`ferrari`), Paper Pro Move
(`chiappa`), Paper Pure (`tatsu`), reMarkable 1 (`rm1`), and reMarkable 2
(`rm2`) all report internal version `20260827113527`. No device was accessed,
rebooted, flashed, or tested during this offline validation.

## Resource matrix

| Feature | Ferrari | Chiappa | Tatsu | RM1 | RM2 |
| --- | --- | --- | --- | --- | --- |
| Native Chinese | Offline passed | Offline passed | Offline passed | Offline passed | Offline passed |
| Pinyin input | Offline passed | Offline passed | Offline passed | Offline passed | Offline passed |
| Tap to turn | Offline passed | Offline passed | Offline passed | Offline passed | Offline passed |
| Reading enhancements | Offline passed | Offline passed | Not exposed | Not exposed | Not exposed |
| Note enhancements | Offline passed | Offline passed | Not exposed | Not exposed | Not exposed |
| Fast mono reading | Offline passed | Offline passed | Not exposed | Not exposed | Not exposed |

Ferrari and Chiappa retain their existing `.172` resources byte for byte.
Tatsu uses AArch64 runtime and input assets. RM1 and RM2 use ARMv7 runtime,
input, and native-Chinese translator assets. Reading Enhancements, Note
Enhancements, and Fast Mono remain color-device-only.

## Exact identities

| Platform | Architecture | SWU SHA-256 | xochitl SHA-256 |
| --- | --- | --- | --- |
| `ferrari` | AArch64 | recorded in the prior `.172` audit | `b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021` |
| `chiappa` | AArch64 | recorded in the prior `.172` audit | `5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4` |
| `tatsu` | AArch64 | `a9372f222abd12f7c7aeb36a3667eee6f0e8841ee885a23797c41019bcd848e0` | `fa674d2ca3d8002602ce4b1b92280b96bcdf2cf32b6a42ec62ae178a1fad1fe3` |
| `rm1` | ARMv7 | `4e572f72ae17ce1f1e8a2bbb7f3e9cddc6b0ba7d168bc75328216aaeee9eb910` | `1f4fbb6e14650704b5b036e482da9948e73553178f6116ad464ab072f7b90117` |
| `rm2` | ARMv7 | `9b91cfe303d1c7c3458f2e511a635341653a2f90535240cb5374fb93fa0f0ad6` | `071d85beef3ef2d4cc0e11002140b27b82a2cc04a2ed740a5669f591069b77df` |

Every package selector remains fail-closed on platform, architecture, internal
version, and stock xochitl SHA-256.

## Chinese catalogs and ARM translator

Tatsu uses the exact proven Pure catalog: 192400 bytes, SHA-256
`2e501a66c30addbecada68b6af262ea506440547b478b4e02e7d2a56889446a1`.
RM1 and RM2 use the deterministic merged legacy catalog with 2099 entries:
205621 bytes, SHA-256
`0f1de519ab4ac1998f432dab014d40fb0cdae2fe528ab30ca47c7a507df82485`.
The committed ARMv7 translator is built from the same architecture-neutral
source under `native-chinese/xovi-src` with the reviewed Zig toolchain: 2888
bytes, SHA-256
`9569d723d4057f741fcb70522b90a69e11aa5c75998cee8a6dcb69ad668be722`.
Builders reject any byte mismatch before packaging.

## Deterministic outputs

The five-device builder creates 21 archives under:

```text
E:/rmtool-main/build/resources-172-five-device/<feature>/
```

Each feature directory contains its applicable device archives and
`manifest.candidate.json`. `validation.json` records all 21 packages and six
forward/reverse combined-QMD checks. `abi-validation.json` records both
translator architectures and the static input ABI evidence. Candidate archives
and every member are checked by exact size, mode, and SHA-256.

Rebuild from the repository directory without network or device operations:

```powershell
build/.venv/Scripts/python.exe native-chinese/build_172.py --research .trellis/tasks/09-08-172-five-device-support/research --firmware-cache E:/remarkable/firmware-cache/official/3.28.0.172 --output-dir build/resources-172-five-device --qmd-tool build/reading-enhancements-qmd/qmd-tool.exe --qmldiff E:/remarkable/qmldiff-source/target/release/qmldiff.exe --qt-bin E:/remarkable/firmware-cache/tools/qt-6.10.3/6.10.3/msvc2022_64/bin
build/.venv/Scripts/python.exe native-chinese/stage_172.py --source build/resources-172-five-device --cache build/stage-172-five-device-cache
build/.venv/Scripts/python.exe native-chinese/stage_172.py --source build/resources-172-five-device --integrate
```

Staging verifies every candidate before writing and refuses collisions.
Integration keeps all existing rows byte-equivalent, validates merged manifests
through the application parsers, and adds only the exact applicable `.172`
records. Native Chinese, Pinyin, and Tap to Turn receive five entries; Reading
Enhancements, Note Enhancements, and Fast Mono retain only Ferrari and Chiappa.

## Validation boundary

All new Tatsu, RM1, and RM2 packages are offline verified and explicitly not
device verified. The tests prohibit network fallback while selecting staged
archives. Publication is verified separately from package construction and does
not change the device-verification level. Firmware flashing and device
operations are outside this resource pipeline.
