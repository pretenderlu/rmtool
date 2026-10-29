# 3.29.0.153 Five-Device Adaptation

Local verification on 2026-10-22, included in v1.18.2. The user subsequently
confirmed Paper Pro plugin migration and installation. Other feature/model
hardware checks remain separate. Existing Move package records remain unchanged.

## Scope

| Device | Native Chinese / Pinyin / tap pages | Reading / EPUB menu / Chinese highlighting | Note enhancements | QR reception / native desktop import |
| --- | --- | --- | --- | --- |
| Paper Pro (ferrari) | Yes | Yes | Yes | Yes |
| Move (chiappa) | Yes | Yes | Yes | Yes |
| Pure (tatsu) | Yes | No port | Not applicable | Yes |
| RM1 | Yes | No port | Not applicable | Yes |
| RM2 | Yes | No port | Not applicable | Yes |

Tap navigation is part of reading enhancements on the two color models and a
standalone feature on the monochrome models. WeRead v1.0.0 installation and the
rmtool native launcher now have exact offline integration on Paper Pro and Move;
firmware partition-writing permissions are unchanged. AppLoad v0.6.0/KOReader now
has exact offline integration on all five targets. Hardware acceptance remains
pending. This is not a port of color-only features to monochrome screens.

Stock RM1/RM2 .153 images still lack CJK fonts. The existing Native Chinese
fallback-font flow applies; QR installation now checks Chinese font coverage
before deployment and directs missing-font users to that flow or Font management.
It does not silently change the system font or the home/data lock-screen choice.

## Exact Firmware

All five official beta SWUs were size/SHA-256 checked before inspecting their
rootfs, Qt libraries, QML and stock translation catalogs. Internal build
`20261009073202` is shared with .152; it is never sufficient for selecting a package.

| Platform | Architecture | Stock xochitl SHA-256 | Stock/QML translation keys checked |
| --- | --- | --- | --- |
| ferrari | aarch64 | `9d6d844d793b6242345c7c25667451620c48b2cc28986a21049fa8d534042dd4` | 1824 |
| chiappa | aarch64 | `0d4bfba6daf8f61138996af95b494d70da11654d4861a98bffe894a425b8caba` | 1793 |
| tatsu | aarch64 | `3735105b7e6b8a1d8ea42c9eabe6079c0bd1c99a546ab1a9fc7870263909ce39` | 1754 |
| rm1 | armv7l | `0c68cf7b049fb87929b5978494a1205bc558f239f77758821c67524133a3f54b` | 1668 |
| rm2 | armv7l | `53547bc30584b9057d5b05ada6c5fb2df3561c3a097c1c2b1aae527d5ddd2e26` | 1765 |

The exact SWU digests live in `tools/build_beta_329.py`; independent translation
carrier expectations live in `tools/validate_build_matrix.py`. Stock language
catalog unions and static QML identities are checked with comments, plurals,
placeholders and markup, not merely message counts.

## Build Evidence

- 19 core archives and five Chinese catalogs were rebuilt twice, byte-identical.
  Fourteen archives and four catalogs are new; the five Move archives/catalog
  retain their previously registered bytes.
- Paper Pro's Chinese highlighting function at `0xcc8db0` passed the reviewed
  complete instruction-layout check. Move's .153 address is `0xcaddb0`.
- Four new QR packages were built twice. Bridge/QMD bytes match between runs;
  all 195 AArch64 or 185 ARMv7 strong imports resolve against the corresponding
  stock libraries. ARMv7 bridges use the stock hard-float ABI.
- RM1's QR helper uses its managed home runtime; RM2 and the other models use
  the data runtime. Root, ownership, permissions and pinned helper SHA checks
  remain enforced. The fixed ARM translator stack-argument handling is retained.
- Exact QMD replay passed for all 20 reading/note targets, six QR targets and
  ten native-import targets, including peer subsets and both combined load orders.
  QR stays above reading/note in the settings sidebar.
- Migration tests cover .172 and .152 core features on every model, while
  unsupported launchers remain excluded. New .153 ARM packages do not carry
  unpublished failing predecessors.
- The build matrix independently checks all device/release pairs; removing a
  supported target from both a backend and its manifest still fails validation.
- All 29 cached .153 plugin archives passed outer archive and per-file checks.
  Every previously published package/carrier record remains unchanged.
- Full regression: 1,319 passed and one skipped (1,320 total). The skipped .172
  candidate test lacks its local build report; no .153 or configured QMD checks
  were skipped. Repository Ruff checks and changed Python compilation passed.

Local artifacts: `build/beta-3.29.0.153-all/validation.json`,
`build/beta-3.29.0.153-all-repeat/`, `build/qr-153-all/validation.json` and
`build/beta-153-full-tests-final.log`. Verified archives are also in the normal
`.rmtool/cache/<feature>/20261009073202/` folders, ready for local rmtool testing.
The adaptation is shipped with application release v1.18.2.

## Reproduction and Hardware Gate

Use `tools/build_beta_329.py --release 3.29.0.153` with the exact SWU/resource
cache and existing Qt, Zig, qmd-tool and qmldiff tools. QR uses
`qr-upload/native/build.py` and `qr-upload/build_assets.py --platform <codename>
--release 3.29.0.153 --candidate-only`; pass the exact native-import carrier.
See the [QR build contract](../qr-upload/README.md) and
[native-import ABI checker](../native-import/build.py).

Run `python tools/validate_build_matrix.py`, `python -m unittest discover -s tests`
and the optional QMD suites with `RMTOOL_QMD_TOOL`, `RMTOOL_QMLDIFF`,
`RMTOOL_READING_MATRIX_CONFIG`, `RMTOOL_QR_RESOURCE_TARGETS` and
`RMTOOL_NATIVE_IMPORT_RESOURCE_TARGETS`. The two resource maps use
`<platform>/<release>` keys so one device cannot substitute for another.

The adaptation session did not install plugins or restart the connected Paper
Pro. Before publication, the user tested and confirmed plugin migration and
installation through rmtool on Paper Pro. This does not establish phone-upload
acceptance on the newly supported models. Continue feature-specific tests of
Chinese text/Pinyin, PDF/EPUB navigation and highlighting, notebook settings,
QR reception and native import/restoration on each model. Offline checks do not
replace this hardware gate.
