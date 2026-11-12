# reMarkable 3.30 beta

Released with rmtool v1.21.7. All five targets passed offline checks. On Move
3.30.0.108, a desktop EPUB upload used the stock importer and restored the
USB web interface to off. This does not validate phone reception, other plugin
functions, or the standalone activation component on hardware.

All five official SWUs use build `20261028121323`. The public release
number, architecture and stock xochitl SHA-256 must still match exactly.
The two beta versions are from the same batch, not interchangeable.

| Platform | Beta | Official SWU SHA-256 |
| --- | --- | --- |
| ferrari | [3.30.0.108](https://dlqathbgqp3nv.cloudfront.net/beta/remarkable-production-image-3.30.0.108-ferrari-public.swu) | `5cd2c255d7e712d2d4a52d215ab8d7c44ef5856480478ada1ddff69c49c05d55` |
| chiappa | [3.30.0.108](https://dlqathbgqp3nv.cloudfront.net/beta/remarkable-production-image-3.30.0.108-chiappa-public.swu) | `40ba6de1eadf51281db714db9096aafbf96ec0c86eeb9ef92f1c7c1ffc031390` |
| tatsu | [3.30.0.108](https://dlqathbgqp3nv.cloudfront.net/beta/remarkable-production-image-3.30.0.108-tatsu-public.swu) | `7b2d6d90b2a30290cc77ba3fa893df1f15bcac99c17121f64eca782459339904` |
| rm1 | [3.30.0.108](https://dlqathbgqp3nv.cloudfront.net/beta/remarkable-production-image-3.30.0.108-rm1-public.swu) | `4cffa907638b5c1ec0f4a655c1e495bfee995c212f376d4fca0cdb006cfb7df6` |
| rm2 | [3.30.0.109](https://dlqathbgqp3nv.cloudfront.net/beta/remarkable-production-image-3.30.0.109-rm2-public.swu) | `14ebbca9c765e8febc0941df55852838b4d83401b13111679fbb40bcb1e04c7b` |

## Offline coverage

- All five: native Simplified Chinese, Pinyin, desktop native import and QR
  reception. The new Chinese supplement covers 96 stock and embedded QML
  identities. The stock French catalog is read only as a translation reference
  and an original-file fingerprint; rmtool does not replace it. QR settings
  use the firmware's QR and power modules and its live settings object.
- Paper Pro and Move: reading and note enhancements, including tap navigation
  and the EPUB font menu. Notes follow the stock timer into `StrokeInput.qml`.
  The Chinese highlight hook has a separate 3.30 build and exact identity
  gate. The new stock word expansion code changes character boundary rules,
  but retains the verified glyph/selection layout and displaced prologue.
  WeRead v1.0.0 and its rmtool launcher are covered on these two devices.
  Tencent's ZIP remains an online download and is not bundled or modified.
- Paper Pure, RM1 and RM2: standalone tap navigation; no color-only packages.
- AppLoad v0.6.0 and KOReader v2026.07.1 are covered on all five targets using
  the unmodified pinned upstream assets. The embedded AppLoad QMD, its own
  resources and the rmtool peers replayed in both load orders. The native
  extension load condition, stock ELF imports and KOReader architecture
  were checked without executing upstream code on a device.

Exact stock-QML replay succeeded in both load orders on all five models,
including the native import and QR peers. The native helpers were rebuilt
against the five verified USB `10.11.99.1/29` contracts and checked against
the stock ABI. RM1 retains its early-home plugin layout. No partition or
firmware installation safety gate has been relaxed.

Firmware Manager discovers the official beta objects for its supported devices
without changing the firmware write matrix. Install the beta through the
device's own update settings where rmtool does not support firmware writing.
