# reMarkable Chinese translation

This directory contains the editable Qt Linguist source, the compiled catalog,
and the cloud release manifest:

- `reMarkable_zh_CN.ts`: editable Chiappa translation source.
- `reMarkable_zh_CN_ferrari_supplement.ts`: the 31 exact keys added by the
  Ferrari firmware payload.
- `reMarkable_zh_CN.qm` and `reMarkable_zh_CN_ferrari.qm`: compiled catalogs
  published as versioned GitHub Release assets and deployed into xochitl's
  built-in French translation slot (`reMarkable_fr.qm`). They are not bundled
  into rmtool executables.
- `reMarkable_zh_CN-20260629074044.qm`: shared compiled beta catalog for the
  Chiappa and Ferrari `3.28.0.162` firmware payloads.
- `manifest.json`: release metadata mapping each exact firmware version to its
  user-facing version, stable/beta channel, asset name, byte size, localized
  SHA-256, stock French SHA-256, and optional hardware variants.

The carrier slot is intentional: this UI-only integration does not inject a
new `zh_CN` language code into xochitl. rmtool backs up and restores the stock
French catalog byte-for-byte.

The catalog currently supports these production firmware builds:

- `3.27.1.0`, internal version `20260506100933`.
- `3.27.3.0`, internal version `20260612085811`.

Both builds have the same four stock translation catalogs on each hardware
platform, so `3.27.1.0` intentionally reuses the already verified Chinese QM
assets instead of publishing byte-identical copies. Their hardware payloads are:

- Chiappa stock French SHA-256:
  `8e0db0f7a2d3116469e1aae4f52657ccc38d0422b5b958ae512554bd018f285e`
- Ferrari stock French SHA-256:
  `9f62dc83b150e48b8d4e1688c1b16d22aa09fdd1ba09b772954394ec6c1ab4fb`

The manifest also supports beta `3.28.0.162`, internal version
`20260629074044`, with one shared 178170-byte Chinese asset:

- Localized SHA-256:
  `4f0fa45abdb944f42a44a356ae25d88f283ec2b193a211f59a7030be0342028e`
- Chiappa stock French SHA-256:
  `3d722f4018f33a24c738bfd14f821603c176d06c9d7e81714e2763d3d40eeb12`
- Ferrari stock French SHA-256:
  `24393f00d9edb933933b436ffe5020990dd97d31d7788172907d75ff1d42d3a5`

Paper Pro (Ferrari) enable and restore were validated on a real device.
Paper Pro Move (Chiappa) was validated offline against the official firmware
only and remains pending real-device validation.

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
embedded QML, for 1878 messages total. Ferrari adds no new dynamic translation
path. Other dynamic translation calls are not claimed as covered. The English
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
```

The TS file must contain no empty or `unfinished` translations before release.

The public assets live in the fixed `localization-assets` GitHub Release. The
tool downloads `manifest.json`, rejects unknown firmware versions, and caches
each verified catalog under `.rmtool/cache/localization/<firmware>/`.
