# 3.28.0.172 offline support

Both official images identify `/etc/version` as `20260827113527`. Stable channel
is user-confirmed, not inferred from the public download URL. No device was
accessed, rebooted, flashed or tested. Nothing was uploaded or published.

## Resource matrix

| Feature | Ferrari | Chiappa | Application installation |
| --- | --- | --- | --- |
| Native Chinese | Offline passed | Offline passed | Local trust/cache integrated |
| Pinyin input | Offline passed | Offline passed | Local trust/cache integrated |
| Reading enhancements | Offline passed | Offline passed | Local trust/cache integrated |
| Note enhancements | Offline passed | Offline passed | Local trust/cache integrated |
| Tap page turn | Offline passed | Offline passed | Local trust/cache integrated |
| Fast mono reading | Offline passed | Offline passed | Local trust/cache integrated |

Stock EN/FR/DE/ES catalogs are byte-identical to .169. Chinese catalogs rebuilt
twice with Qt 6.10.3 reproduce the published bytes: Ferrari 2015 translations,
196626 bytes, SHA-256 `49cf09fc23ef3fcacb956d426915e3f80b85a02fa7e597a8b5fc8013a2bdb931`;
Chiappa 1982 translations, 192400 bytes, SHA-256
`2e501a66c30addbecada68b6af262ea506440547b478b4e02e7d2a56889446a1`.
The prior catalog audit reports zero missing keys and zero placeholder issues.
This does not extend the deprecated French-slot replacement support matrix.

Exact xochitl SHA-256:

| Platform | SHA-256 |
| --- | --- |
| ferrari | `b1816408cf90b19e448c70082625c4d6a36060368706eb7a9b35425428a9a021` |
| chiappa | `5ba79d1b5656df1a771217d29a8d3938c40256be53361b10a0d17cd4752807f4` |

Reading and note QMDs are compiled from current source and checked with the
existing structural assertions against each exact recovered resource tree.
Other payloads come from checksum-verified current .169 packages; only the
hashtab changes, and Chinese catalog bytes are independently rebuilt. All six
features pass individual QMD checks and QML replay. Both supported families
pass combined replay in forward/reverse order: native + pinyin + reading +
note; native + pinyin + tap + fast + note. Reading is not combined with its
legacy tap/fast alternatives. Shared runtime members match across all peers.

`pinyin-input/check_172_abi.py` verifies the official Qt 6.10.3 export symbols
and disassembly evidence for IME KeyEvent offsets 72/80/108 on both platforms.
Native translator has no undefined dynamic imports; its three ELF relocations
are internal/relative AArch64 ABS64/GLOB_DAT/RELATIVE. The Xovi Qt imports are
present. This is static evidence, not proof of runtime behavior or cold boot.
The audited existing translator `.so` is reused unchanged, not recompiled.

## Exact local outputs

Repository: `E:/rmtool-main`. Each of the six feature names in the builder's
`FEATURES` tuple has these two archives (substitute the same feature twice):

```text
E:/rmtool-main/build/resources-172/<feature>/rmtool-<feature>-ferrari-20260827113527-3.28.0.172.tar.gz
E:/rmtool-main/build/resources-172/<feature>/rmtool-<feature>-chiappa-20260827113527-3.28.0.172.tar.gz
E:/rmtool-main/build/resources-172/<feature>/manifest.candidate.json
```

Each candidate manifest contains the actual archive and every member's size,
mode and SHA-256. `build/resources-172/validation.json` records the eight
combined checks; `abi-validation.json` records symbols, offsets and library
hashes. `integration.json` records local trust integration, and
`unittest-final.log` records the post-integration full regression run.

Rebuild from the repository directory, without network or device operations:

```powershell
build/.venv/Scripts/python.exe native-chinese/build_172.py --research .trellis/tasks/09-07-firmware-management-172/research --firmware-cache E:/remarkable/firmware-cache/official/3.28.0.172 --output-dir build/resources-172 --qmd-tool build/reading-enhancements-qmd/qmd-tool.exe --qmldiff E:/remarkable/qmldiff-source/target/release/qmldiff.exe --qt-bin E:/remarkable/firmware-cache/tools/qt-6.10.3/6.10.3/msvc2022_64/bin
build/.venv/Scripts/python.exe native-chinese/stage_172.py
build/.venv/Scripts/python.exe native-chinese/stage_172.py --integrate
```

Both staging and integration have been run. Staging verifies all archive/member
hashes and modes before writing, refuses collisions, and is repeatable. It
adds the 12 archives to `E:/rmtool-main/.rmtool/cache/<feature>/20260827113527/`
and puts metadata in `<feature>/manifest.172.candidate.json`. Staging alone
never replaces active manifests. `--integrate` requires ABI evidence and the
updated application gates, validates all six merged manifests through the real
parsers, then appends the .172 records to bundled and active cache manifests.
Existing rows must remain identical; differing cache records are refused and
previous active cache manifests are backed up as `manifest.before-172.json`.

## Local testing and publication boundary

Application allowlists and bundled manifests now admit both exact .172
identities. Native/Pinyin retain complete-target checks; reading/note/fast
retain bundled trust checks. All .172 entries are offline verified and NOT
device verified. There are no fabricated .172 predecessor revisions.

The application package selectors, shared-runtime trust context, and all 12
local download/cache paths have been tested with network calls prohibited.
Manual archive import can use the exact build paths above after selecting the
.172 target. Normal manifest download URL fields are present for schema
compatibility but are NOT evidence of publication; rely on the verified local
cache before testing. No device installation has been performed or authorized
by this offline validation.

Native/Pinyin use bundled trust directly. Reading/note/fast reject an old
incomplete remote manifest and fall back to the local/bundled complete matrix.
Tap status now selects the exact bundled target when a lagging remote manifest
omits it. This preserves .172 discovery without manufacturing remote records.
Use offline mode to avoid unnecessary remote lookup delays; local archives
remain available even if the remote manifest does not yet contain .172.

The deprecated French-slot matrix is unchanged: native .172 deployment only
uses the byte-identical stock catalog identity for its read-only conflict
guard. It does not enable the old French-slot installer for .172.
Firmware UI, firmware tests and flashing remain outside this resource change.
