# Move fast-monochrome experiment

This began as an offline-only QMLDiff experiment for reMarkable Paper Pro Move
(Chiappa) running OS 3.27.3.0, internal version `20260612085811`.

The canary adds a native `快速黑白` toggle to the document toolbar's existing
More menu. It is shown only for PDF and EPUB documents on color-capable
hardware. The session-scoped state defaults to off whenever xochitl starts;
enabling or disabling it uses DocumentView's native notification bar. While
enabled, `MainView.qml`'s existing `globalScreenMode` item forces
`Epaper.ScreenModeItem.Mono` only while that reading view is visible. The lock
screen, library, settings, notebooks, and handwriting retain stock behavior.
Turning the toggle off restores the original dynamic global screen-mode logic.
The menu also exposes a session-only native cleanup foldout that shows the
current interval and opens a stock-style 5/10/20/30/never choice page. It
defaults to the stock GhostBuster full clear every 10 real page turns, uses
only existing native controls, and adds no image assets. It does not claim
faster color or full-screen refreshes.

## Offline validation

The helper refuses recovered `MainView.qml`, `DocumentView.qml`,
`SettingsMenu.qml`, or hashtable inputs whose size or SHA-256 differs from the
known 3.27.3.0 Chiappa inputs. It then compiles and checks the QMD, runs QMLDiff
compatibility checks, applies the diff, and asserts the patched QML structure.

```powershell
python refresh-optimization/validate_move_3_27_3.py `
  --qml-root E:\remarkable\firmware-cache\work\tap-matrix\matrix\chiappa-20260612085811\qrex-out `
  --hashtab E:\remarkable\firmware-cache\work\tap-matrix\matrix\chiappa-20260612085811\hashtab `
  --qmldiff E:\remarkable\qmldiff-source\target\release\qmldiff.exe `
  --qmd-tool E:\rmkit-cn-v1.1.1\dist\qmd-tool-windows-amd64.exe
```

Generated files are written below `build/refresh-optimization/`. The canonical
QMD source now lives at
`fast-mono-reading/qmd-src/fast-mono-reading-3.27.qmd`; the validator remains
offline-only and contains no SSH, installer, persistence, service restart, or
device-reboot logic.

## Standalone Vellum canary

After validation, build a deterministic unsigned Vellum APK from the compiled
QMD:

```powershell
python refresh-optimization/build_move_3_27_3_vellum_canary.py `
  --compiled-qmd build/refresh-optimization/qmd/fast-mono-reading-3.27.qmd
```

The APK package is `rmtool-fast-mono-reading-canary` revision
`3.27.3.0-r1`. It requires
`qt-resource-rebuilder>=19.0.0`, `qt-resource-rebuilder<20.0.0`, and
`appload>=0.5.3`, while pinning the target to `remarkable-os=3.27.3.0-r0` and
`rmppmove=1.0.0-r0`. It owns only the compiled QMD at
`/home/root/xovi/exthome/qt-resource-rebuilder/` plus GPL license and source
metadata below its Vellum license directory. The builder inspects both APK
members and verifies the package identity, exact dependency list, installed
QMD path, file whitelist, payload bytes, and deterministic output before
writing under `build/refresh-optimization/`. It does not install or deploy the
APK.
