# Native Simplified Chinese

Packages are gated to the exact firmware identity and stock xochitl SHA-256
recorded in `manifest.json`.

| Device | 3.27.1.0 stable | 3.27.3.0 stable | 3.28.0.162 beta | 3.28.0.163 beta | 3.28.0.164 beta | 3.28.0.166 beta |
| --- | --- | --- | --- | --- | --- | --- |
| Paper Pro (`ferrari`) | Offline verified | Offline verified | Offline verified | Offline verified | Offline verified | **Device verified** |
| Paper Pro Move (`chiappa`) | Offline verified | **Device verified** | Offline verified | Offline verified | Offline verified | Offline verified |

It preserves the existing French-slot localization feature. Before enabling
this plugin, fully restore any managed French-slot localization, including one
that is installed but not selected. Installation and removal only stage
persistent files; rmtool never restarts xochitl or the device. Reboot manually
after either operation. If `zh_CN` is selected during removal, rmtool first
changes `[General] language` to `en`.

The bold identities have passed real-device language switching and reboot
checks; Ferrari 3.28.0.166 also passed Chinese input, the `中文` keyboard label,
passcode cold boot, unlock, emergency fail-open, and normal reboot checks. Every
other package passed exact firmware, archive, QMD/hashtab, and shared-runtime
checks offline, including Move 3.28.0.166 against the official 3.28.0.166
firmware image.
Because stable Chiappa firmware has no stock CJK font, rmtool refuses
deployment until the active sans-serif font has Simplified Chinese coverage.
The selected system font can be supplied through rmtool's
`/data/rmtool/fonts` active mirror; the plugin itself does not carry a font.

## Local build

The builder verifies every exact input hash, creates the archive twice to
prove deterministic output, validates the archive against the generated
manifest, and can seed rmtool's local cache:

```powershell
py -3.13 native-chinese/build_assets.py `
  --qmd-tool E:\path\to\qmd-tool-windows-amd64.exe `
  --local-cache .rmtool/cache/native-chinese
```

The builder consumes only exact tap-to-turn base archives already pinned by
their manifest, verifies the corresponding Chinese catalog, and uses the
reviewable QMD sources under `native-chinese/qmd/`. It checks the native QMD
alone and together with tap-to-turn in both orders. The manifest records
Tencent COS first and GitHub second, but this step does not upload either file.
Python 3.13 is pinned above because gzip output can differ across zlib versions;
rebuilding with that interpreter reproduces the recorded archive byte for byte.

## Local test

1. Start rmtool from source.
2. Connect one exact supported device and firmware.
3. In Device Toolbox, detect **Native Simplified Chinese**.
4. Enable it, close SSH, and reboot the device manually.
5. Select **Simplified Chinese** in the device language settings.

The emergency sentinel is `/data/rmtool/disable-xovi`; when present, the shared
launcher starts stock xochitl without loading any rmtool Xovi feature. rmtool
also recognizes the legacy `/home/root/.local/share/rmtool/disable-xovi` marker
for cleanup. It can create or clear the root-owned marker atomically without
remounting `/` or restarting the device. After a firmware upgrade, this section
also recognizes its own verified old shared-Xovi state and can remove that
obsolete shared runtime.
