# Third-party notices

## Screen preview references

The Paper Pro and reMarkable 2 framebuffer layouts used by `_screen_preview.py`
were informed by the screenshot implementation in
[yangg1224/smart_remarkable](https://github.com/yangg1224/smart_remarkable)
at commit `cb787065281b7211b012bd5e5d9be751fe5adaef`, licensed under the MIT
License (Copyright 2024-2025 Brock Wilcox). rmtool independently performs the
read over its existing SSH transport, validates exact device families, and
does not install the upstream application on the device.

## Firmware management references

`_firmware.py` follows the official image discovery and detached native
SWUpdate approach in [rmitchellscott/reManager](https://github.com/rmitchellscott/reManager)
(`app_os.go`, `app_swupdate.go`) and the A/B inspection approach in
[remarkable-go v0.4.1](https://github.com/rmitchellscott/remarkable-go/tree/v0.4.1/partition).
Both reference projects are licensed under the GNU General Public License v3.0;
rmtool retains that license. The implementation adds independent validation,
persistent recovery records and explicit confirmation boundaries. It does not
inherit force bypasses, counter resets or writer cancellation.

Official firmware is downloaded directly from reMarkable's distribution
infrastructure, not redistributed by rmtool. Native SWUpdate is provided by
the device firmware, not bundled here. Its implementation was consulted to
verify check-only and staging semantics; see `docs/firmware-management.md`.

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

## Optional AppLoad and KOReader downloads

The optional installer downloads unmodified release archives directly from
[asivery/rm-appload](https://github.com/asivery/rm-appload), licensed under the
GNU General Public License v3.0, and
[koreader/koreader](https://github.com/koreader/koreader), licensed under the
GNU Affero General Public License v3.0. These application archives are verified
on the user's computer and are not redistributed by the rmtool repository,
release builds, or Tencent COS.

## Offline Pinyin input

Firmware-specific Pinyin input archives redistribute `ime_hook.so`,
`zh_CN.rcc`, and `ime-server` from, and an rmtool-adapted
`pinyin_interceptor.qmd` based on,
[boangs/rmkit](https://github.com/boangs/rmkit), licensed under the GNU General
Public License v3.0. The server embeds the
[rime-frost](https://github.com/gaboolic/rime-frost) dictionary, also licensed
under the GNU General Public License v3.0. Each archive includes the upstream
rmkit notice and license.
