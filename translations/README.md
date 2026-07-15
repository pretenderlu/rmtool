# reMarkable Chinese translation

This directory contains the editable Qt Linguist source, the compiled catalog,
and the cloud release manifest:

- `reMarkable_zh_CN.ts`: editable translation source.
- `reMarkable_zh_CN.qm`: compiled catalog published as a versioned GitHub
  Release asset and deployed into xochitl's built-in French translation slot
  (`reMarkable_fr.qm`). It is not bundled into rmtool executables.
- `manifest.json`: release metadata mapping each exact firmware version to its
  user-facing version, stable/beta channel, asset name, byte size, localized
  SHA-256, and stock French SHA-256.

The carrier slot is intentional: this UI-only integration does not inject a
new `zh_CN` language code into xochitl. rmtool backs up and restores the stock
French catalog byte-for-byte.

The current baseline is reMarkable Paper Pro production firmware `3.27.3.0`,
internal version `20260612085811`. The expected stock French carrier
`reMarkable_fr.qm` has SHA-256
`8e0db0f7a2d3116469e1aae4f52657ccc38d0422b5b958ae512554bd018f285e`.

The catalog contains 1847 messages: the 1779-key union of the active messages
in the stock English, French, German, and Spanish catalogs, plus 64 static QML
keys proven by the production xochitl binary and four finite runtime values:
`SettingsModel / Wifi`, `SettingsModel / Developer`,
`SettingsModel / Experimental`, and `PenColorModel / Magenta`.
Other dynamic translation calls are not claimed as covered. The English QM is
intentionally sparse because English is xochitl's source language, so it is
not a complete translation inventory on its own. Message identity uses the
exact `(context, source, comment, numerus)` tuple.

Regenerate the binary with Qt 6 Linguist tools:

```powershell
lrelease -fail-on-unfinished translations/reMarkable_zh_CN.ts `
  -qm translations/reMarkable_zh_CN.qm
```

The TS file must contain no empty or `unfinished` translations before release.

The public assets live in the fixed `localization-assets` GitHub Release. The
tool downloads `manifest.json`, rejects unknown firmware versions, and caches
each verified catalog under `.rmtool/cache/localization/<firmware>/`.
