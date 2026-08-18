# Pinyin input package

This package ports the offline Pinyin input components from
[boangs/rmkit](https://github.com/boangs/rmkit) into rmtool's shared Xovi
runtime. The exact matrix covers Paper Pro and Move from 3.27.1.0 through
3.28.0.169. Paper Pro 3.28.0.166 is device verified; the other thirteen
targets are verified offline against official firmware.

The package includes rmkit's `pinyin_interceptor.qmd`, `ime_hook.so`,
`zh_CN.rcc`, and `ime-server` with the upstream GPL-3.0 license and notice.
The RCC registers the Chinese keyboard locale. It is installed as a
feature-owned, immediate file in `exthome/qt-resource-rebuilder`, because QRR
does not recursively scan subdirectories. The Pinyin QMD intentionally does
not patch the keyboard-layout display name. On every supported target the stock
`KeyboardLanguageSelect.qml` resolves `Chinese` with
`qsTranslate("LanguageAndKeyboard", ...)`; the exact native-Chinese catalog
therefore supplies `中文` without either QMD modifying that runtime function.
The embedded dictionary is derived from
[rime-frost](https://github.com/gaboolic/rime-frost), also GPL-3.0.

Build locally with:

```powershell
py -3.13 pinyin-input/build_assets.py --local-cache .rmtool/cache/pinyin-input
```

The build consumes an exact trusted rmtool runtime archive and a local rmkit
checkout. Generated archives stay under `build/` and are not committed. Python
3.13 is pinned because gzip output can differ across zlib versions; rebuilding
with that interpreter reproduces the recorded archive byte for byte.
