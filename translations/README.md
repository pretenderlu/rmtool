# reMarkable Chinese translation

This directory contains the editable Qt Linguist source, the compiled catalog,
and the cloud release manifest:

- `reMarkable_zh_CN.ts`: editable Chiappa translation source.
- `reMarkable_zh_CN_ferrari_supplement.ts`: the 31 exact keys added by the
  Ferrari firmware payload.
- `reMarkable_zh_CN_legacy_supplement.ts`: the exact 84-key RM2 catalog gap,
  shared by the RM1/RM2 legacy-platform build.
- `reMarkable_zh_CN.qm` and `reMarkable_zh_CN_ferrari.qm`: compiled catalogs
  published as versioned GitHub Release assets and deployed into xochitl's
  built-in French translation slot (`reMarkable_fr.qm`). They are not bundled
  into rmtool executables.
- `reMarkable_zh_CN-20260612085811-legacy.qm`: shared RM1/RM2 stable
  catalog built from the base, Ferrari supplement, and legacy supplement.
- `reMarkable_zh_CN-20260629074044.qm`: shared compiled beta catalog for the
  Chiappa and Ferrari `3.28.0.162` firmware payloads.
- `reMarkable_zh_CN_3_28_0_164_supplement.ts`: 132 exact UTF-8 catalog keys
  newly active in both Chiappa and Ferrari `3.28.0.164`.
- `reMarkable_zh_CN_3_28_0_164_ferrari_supplement.ts`: the two additional
  Ferrari-only `3.28.0.164` keys.
- `reMarkable_zh_CN-3.28.0.164-{chiappa,ferrari}.qm`: separate compiled
  catalogs for the two exact `3.28.0.164` stock inventories.
- `manifest.json`: release metadata mapping each exact firmware version to its
  user-facing version, stable/beta channel, asset name, byte size, localized
  SHA-256, stock French SHA-256, and optional hardware variants.

The carrier slot is intentional: this UI-only integration does not inject a
new `zh_CN` language code into xochitl. rmtool backs up and restores the stock
French catalog byte-for-byte.

The catalog currently supports these production firmware builds:

- `3.27.1.0`, internal version `20260506100933`: Chiappa and Ferrari.
- `3.27.3.0`, internal version `20260612085811`: Chiappa, Ferrari, Tatsu
  (Paper Pure), RM1, and RM2.

The Chiappa and Ferrari payloads have the same stock catalogs across both
builds, so `3.27.1.0` reuses the existing verified assets. Their stock French
SHA-256 values are:

- Chiappa:
  `8e0db0f7a2d3116469e1aae4f52657ccc38d0422b5b958ae512554bd018f285e`
- Ferrari:
  `9f62dc83b150e48b8d4e1688c1b16d22aa09fdd1ba09b772954394ec6c1ab4fb`

Tatsu `3.27.3.0` has this stock French SHA-256:

- `2ee88b18955776e8f6f52949b6c172d50d14f60f3e59d75db7d17881377a7b3a`

Its translation keys are a strict subset of Chiappa, so it reuses the same
175519-byte Chinese asset with SHA-256
`47ba9d8a6f38b3763d013ecc489d44e8742704404b50a5de102b42e33dfebbfb`.

RM1 and RM2 `3.27.3.0` use one shared 188407-byte legacy catalog with SHA-256
`517e70cdf4d862b8ceec57d3238ece72b3799aecdf075c0183668acfc2137c64`.
Their exact stock French SHA-256 values are:

- RM1: `0767babb6d55fc960565568d6af89455ba233194a4d887d70bd1c7987c3898a4`
- RM2: `8080219cb5b3a75a1423ac0cee5bd12d3ee1c9029ff22ecf981cf075559900a7`

The manifest also supports beta `3.28.0.162`, internal version
`20260629074044`, with one shared 178170-byte Chinese asset:

- Localized SHA-256:
  `4f0fa45abdb944f42a44a356ae25d88f283ec2b193a211f59a7030be0342028e`
- Chiappa stock French SHA-256:
  `3d722f4018f33a24c738bfd14f821603c176d06c9d7e81714e2763d3d40eeb12`
- Ferrari stock French SHA-256:
  `24393f00d9edb933933b436ffe5020990dd97d31d7788172907d75ff1d42d3a5`

Beta `3.28.0.163`, internal version `20260702125656`, ships byte-identical
stock English, French, German, and Spanish catalogs on both Chiappa and
Ferrari, so it reuses the same 178170-byte Chinese asset and the same stock
French SHA-256 values as `3.28.0.162`.

Beta `3.28.0.164` retains internal version `20260702125656` but changes both
the stock catalogs and xochitl. It therefore has release-qualified asset names
and exact carrier hashes instead of overwriting or reusing the `.163` entries:

| Platform | Chinese asset | Size | Chinese SHA-256 | Stock French SHA-256 |
| --- | --- | ---: | --- | --- |
| Chiappa | `reMarkable_zh_CN-3.28.0.164-chiappa.qm` | 192220 | `50dc76f758b51fa10bf026269e1788904fe564d00ed6e37b97fed7dc02399348` | `53728fd166e2658363c38c3951c135f59ca1502f2d2e9c43ee6c4cff1ae9871a` |
| Ferrari | `reMarkable_zh_CN-3.28.0.164-ferrari.qm` | 196446 | `18c564eea746a1508343b85197d958b003729b3274f5a6a880285c552dc7348a` | `ef07588e04ade2f19ce2e4545fc9d5e63f7a541b3410803b4727cf82f1b5f946` |

The `.164` supplements were merged from the corrected Qt 6 UTF-8 stock
inventories. The resulting Chiappa and Ferrari catalogs compile with 1979 and
2012 finished messages respectively, with no unfinished or empty entries.

Beta `3.28.0.166` for Ferrari uses internal version `20260806095513` and an
exact release-qualified catalog:

- Asset: `reMarkable_zh_CN-3.28.0.166-ferrari.qm`
- Size: 196567 bytes
- Chinese SHA-256: `6bdca18626173b9fadbd350347afebcab0cae3639f8d206d86b9723cd3dda127`
- Stock French SHA-256: `2b03e8bdf26566d06189604f4678b1929af60b8bef65b662fafc9f04eebed9cc`
- xochitl SHA-256: `8726b4fce55a9154a5014956e5204401ce881d752c1ff3813adb622a68aac2f9`

Paper Pro (Ferrari) enable and restore were validated on a real device.
Paper Pro Move (Chiappa), Paper Pure (Tatsu), RM1, and RM2 `3.27.3.0` were
validated offline against official firmware only and remain pending
real-device validation. RM1 and RM2 do not use Developer Mode, but still
require root SSH. Their official root filesystems contain no CJK font, so
rmtool's font gate and bundled Noto Sans CJK SC fallback are required.

rmtool selects the package by the exact stock carrier hash. Platform names are
display metadata only.

The official `3.27.1.0` SWU files used for verification have these SHA-256
values:

- Chiappa: `786f326b177394d6ce210195034b2b0e0665b377945c72d5882d0ed8d43d9047`
- Ferrari: `2a140a2200c0b770f5e152f32bd8184ca45dab1cc9bae08f0e9d2e9c782d82e3`

The catalog contains 1847 messages: the 1779-key union of the active messages
in the stock English, French, German, and Spanish catalogs, plus 64 static QML
keys proven by the production xochitl binary and four finite runtime values:
`SettingsModel / Wifi`, `SettingsModel / Developer`,
`SettingsModel / Experimental`, and `PenColorModel / Magenta`.
The Ferrari catalog contains those 1847 messages plus 26 exact keys from its
four stock catalogs and five static `SettingsWindow` keys found only in its
embedded QML, for 1878 messages total. The legacy catalog adds exactly the 84
keys missing from the official RM2 four-language union, for 1962 messages total;
RM1's 1671-key union and RM2's 1763-key union are both fully covered. Ferrari
adds no new dynamic translation path. The three RM1/RM2 dynamic sites were
already audited as a subset of the supported Ferrari sites and are not newly
claimed as statically resolved. Other dynamic translation calls are not claimed
as covered. The English
QM is intentionally sparse because English is xochitl's source language, so it
is not a complete translation inventory on its own. Message identity uses the
exact `(context, source, comment, numerus)` tuple.

Regenerate the binary with Qt 6 Linguist tools:

```powershell
lrelease -nounfinished translations/reMarkable_zh_CN.ts `
  -qm translations/reMarkable_zh_CN.qm

lconvert -sort-contexts -locations none `
  translations/reMarkable_zh_CN.ts `
  translations/reMarkable_zh_CN_ferrari_supplement.ts `
  -o "$env:TEMP/reMarkable_zh_CN_ferrari.ts"
lrelease -nounfinished "$env:TEMP/reMarkable_zh_CN_ferrari.ts" `
  -qm translations/reMarkable_zh_CN_ferrari.qm

lconvert -sort-contexts -locations none `
  translations/reMarkable_zh_CN.ts `
  translations/reMarkable_zh_CN_ferrari_supplement.ts `
  translations/reMarkable_zh_CN_legacy_supplement.ts `
  -o "$env:TEMP/reMarkable_zh_CN_legacy.ts"
lrelease -nounfinished "$env:TEMP/reMarkable_zh_CN_legacy.ts" `
  -qm translations/reMarkable_zh_CN-20260612085811-legacy.qm

lconvert -sort-contexts -locations none `
  translations/reMarkable_zh_CN.ts `
  translations/reMarkable_zh_CN_3_28_0_164_supplement.ts `
  -o "$env:TEMP/reMarkable_zh_CN-3.28.0.164-chiappa.ts"
lrelease -nounfinished "$env:TEMP/reMarkable_zh_CN-3.28.0.164-chiappa.ts" `
  -qm translations/reMarkable_zh_CN-3.28.0.164-chiappa.qm

lconvert -sort-contexts -locations none `
  translations/reMarkable_zh_CN.ts `
  translations/reMarkable_zh_CN_ferrari_supplement.ts `
  translations/reMarkable_zh_CN_3_28_0_164_supplement.ts `
  translations/reMarkable_zh_CN_3_28_0_164_ferrari_supplement.ts `
  -o "$env:TEMP/reMarkable_zh_CN-3.28.0.164-ferrari.ts"
lrelease -nounfinished "$env:TEMP/reMarkable_zh_CN-3.28.0.164-ferrari.ts" `
  -qm translations/reMarkable_zh_CN-3.28.0.164-ferrari.qm
```

The TS file must contain no empty or `unfinished` translations before release.

The public assets live in the fixed `localization-assets` GitHub Release. The
tool downloads `manifest.json`, rejects unknown firmware versions, and caches
each verified catalog under `.rmtool/cache/localization/<firmware>/`.
