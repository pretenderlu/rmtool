# Third-party notices

## rmkit Chinese translation

`translations/reMarkable_zh_CN.ts` and the compiled
`translations/reMarkable_zh_CN.qm` are derived from the Chinese translation
catalog in [boangs/rmkit](https://github.com/boangs/rmkit), licensed under the
GNU General Public License v3.0.

The catalog was rebased for rmtool against the stock English, French, German,
and Spanish catalogs shipped with reMarkable Paper Pro production firmware
`3.27.3.0` (internal version `20260612085811`). The original device catalogs
are not redistributed by this repository.

## Noto Sans CJK SC

`assets/fonts/NotoSansCJKsc-Regular.otf` is the unmodified regular-weight
Simplified Chinese font from [notofonts/noto-cjk](https://github.com/notofonts/noto-cjk).
It is distributed under the SIL Open Font License 1.1; the complete upstream
license is included at `assets/fonts/LICENSE`.

## Persistent tap-to-turn dependencies

Firmware-specific tap-to-turn release archives redistribute unmodified builds
of [asivery/xovi](https://github.com/asivery/xovi), licensed under the GNU
Lesser General Public License v3.0, and the qt-resource-rebuilder extension
from [asivery/rm-xovi-extensions](https://github.com/asivery/rm-xovi-extensions),
licensed under the GNU General Public License v3.0. They also include the
`qmd-tool` validator from [boangs/rmkit](https://github.com/boangs/rmkit),
licensed under the GNU General Public License v3.0.

Each asset archive includes the corresponding license texts. Source code and
installation information are available from the linked upstream repositories;
rmtool's QMLDiff source is maintained under `tap-page-turn/qmd-src/`.
