# Fast monochrome reading

This feature adds a native `快速黑白` toggle to the PDF/EPUB More menu on
exactly supported color reMarkable builds. The toggle is session-scoped and
defaults off whenever xochitl starts. Enabling it forces monochrome mode only
for the visible PDF/EPUB document view; disabling it restores stock screen-mode
selection. Notebooks, handwriting, the library, settings, and the passcode
screen retain stock behavior.

While fast monochrome is enabled, a native `强制刷新` foldout row shows the
current cleanup interval. Opening it pushes a stock-style choice page with 5,
10, 20, 30, and `从不`; the system toolbar supplies the back action. It defaults
to every 10 page turns for each xochitl session, counts both taps and swipes,
and uses the stock GhostBuster full-clear path 500 ms after the threshold.

## Download and offline behavior

- Tencent COS manifest (preferred in mainland China):
  <https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/fast-mono-reading/manifest.json>
- GitHub manifest fallback:
  <https://github.com/pretenderlu/rmtool/releases/download/fast-mono-reading-assets/manifest.json>

The client tries the COS manifest and payload first, then GitHub. Every
response must match the expected size and SHA-256 before it can replace the
cache or be deployed. If both sources fail, rmtool uses a previously validated
cached manifest and then its bundled baseline trusted manifest. The bundled
manifest contains metadata only: an offline installation still requires the
exact package to be present in the validated local cache. Payload names from
the manifest are resolved below the same COS prefix or fixed GitHub release.

## Support matrix

| Platform | 3.27.1.0 (`20260506100933`) | 3.27.3.0 (`20260612085811`) | 3.28.0.162 beta (`20260629074044`) | 3.28.0.163 beta (`20260702125656`) | 3.28.0.164 beta (`20260702125656`) |
| --- | --- | --- | --- | --- | --- |
| Paper Pro (`ferrari`) | Offline verified | Offline verified | Offline verified | Offline verified | Offline verified |
| Paper Pro Move (`chiappa`) | Offline verified | Offline verified | Offline verified | Offline verified | Offline verified |

All entries require `aarch64` and an exact platform, internal firmware, and
stock xochitl SHA-256 match from `manifest.json`. Offline-verified packages
passed qmd-tool hash checks, QMLDiff compatibility, patch replay, patched-QML
assertions, archive validation, and deterministic rebuild against recovered
official firmware. rmtool allows their installation only after an explicit
not-yet-device-tested warning. The earlier Move 3.27.3 fast-mono behavior was
device tested, but the new r3 cleanup-refresh QMD with its native selector
remains offline verified until the complete package is tested again on-device.

The .163 and .164 beta releases share internal version `20260702125656`, but
their exact stock xochitl hashes and release-qualified `.164` asset names are
different. rmtool also recognizes a completely verified `.163` rmtool/Vellum
installation left behind by a firmware upgrade and offers cleanup before the
`.164` package can be installed. Unknown or modified predecessor state remains
blocked.

## Deployment boundaries

When a standard Vellum-managed Xovi runtime is present, rmtool installs an
independent minimal `rmtool-fast-mono-reading` APK. It depends exactly on the
matching `remarkable-os` version and on `rmpp=1.0.0-r0` for Ferrari or
`rmppmove=1.0.0-r0` for Chiappa, plus the proven QRR/AppLoad runtime range.
A clean device uses rmtool's standalone Xovi/QRR deployment.

On clean devices, tap-to-turn and fast monochrome share rmtool's standalone
Xovi/QRR runtime while retaining separate QMD state. Unmanaged Xovi is rejected
before upload. The earlier `rmtool-fast-mono-reading-canary` package conflicts
with the production package and must be removed first.

Neither installation nor disable restarts xochitl. Wait for rmtool to close
SSH, then restart the device manually.

## Offline validation and build

The reviewed sources are:

- `qmd-src/fast-mono-reading-3.27.qmd`
- `qmd-src/fast-mono-reading-3.28.qmd`

Compile and replay them against recovered QML as described in
`refresh-optimization/README.md`, then build all ten deterministic archives:

```powershell
py -3.14 fast-mono-reading/build_assets.py `
  --cache-root E:\remarkable\firmware-cache\work\tap-matrix\cloud-verify-20260721 `
  --cache-root E:\remarkable\firmware-cache\work\tap-matrix\release-163 `
  --cache-root E:\rmtool-main\build\tap-page-turn-164 `
  --cache-root .rmtool\cache\tap-page-turn `
  --qmd-3-28-164 E:\rmtool-main\build\fast-mono-matrix\results-164\chiappa-20260702125656\qmd\fast-mono-reading.qmd `
  --qmd-tool E:\rmkit-cn-v1.1.1\dist\qmd-tool-windows-amd64.exe `
  --no-download `
  --local-cache .rmtool\cache\fast-mono-reading
```

The builder verifies every base archive against the tap-to-turn manifest,
replaces only the tap QMD, checks the final payload whitelist and packaged QMD
hashtab, builds every output twice, and writes ten archives plus an aggregate
manifest below `build/fast-mono-reading/`. Commit the reviewed manifest and QMD
sources only; publish the large archives as release assets.
