# reMarkable Chinese translation

This directory contains the editable Qt Linguist source and the compiled
catalog deployed by rmtool:

- `reMarkable_zh_CN.ts`: editable translation source.
- `reMarkable_zh_CN.qm`: compiled catalog deployed by rmtool into xochitl's
  built-in French translation slot (`reMarkable_fr.qm`).

The carrier slot is intentional: this UI-only integration does not inject a
new `zh_CN` language code into xochitl. rmtool backs up and restores the stock
French catalog byte-for-byte.

The current baseline is reMarkable Paper Pro firmware `20260629074044`,
xochitl `3.28-tentacruel` commit `8bee0a4`. Its device-provided
`reMarkable_en.qm` has SHA-256
`2235293230987a790c5524dd46ff5ee03d4f4a090b905d62208361663da1a71d`.

Regenerate the binary with Qt 6 Linguist tools:

```powershell
lrelease -fail-on-unfinished -fail-on-invalid translations/reMarkable_zh_CN.ts `
  -qm translations/reMarkable_zh_CN.qm
```

The TS file must contain no empty or `unfinished` translations before release.
