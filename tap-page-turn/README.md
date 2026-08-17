# Persistent tap-to-turn assets

rmtool downloads firmware-specific tap-to-turn packages from Tencent COS or
the fixed `tap-page-turn-assets` GitHub release. Packages are selected by all
of:

- hardware platform;
- CPU architecture;
- the 14-digit internal firmware version; and
- the SHA-256 of `/usr/bin/xochitl`.

## Download and offline behavior

- Tencent COS manifest (preferred in mainland China):
  <https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/tap-page-turn/manifest.json>
- GitHub manifest fallback:
  <https://github.com/pretenderlu/rmtool/releases/download/tap-page-turn-assets/manifest.json>

The client tries the COS manifest and payload first, then GitHub. Every
response must match the expected size and SHA-256 before it can replace the
cache or be deployed. If both sources fail, rmtool uses a previously validated
cached manifest and then its bundled baseline trusted manifest. The bundled
manifest contains metadata only: an offline installation still requires the
exact package to be present in the validated local cache. Payload names from
the manifest are resolved below the same COS prefix or fixed GitHub release.

## Support matrix

| Device | Platform | 3.27.1.0 stable (`20260506100933`) | 3.27.3.0 stable (`20260612085811`) | 3.28.0.162 beta (`20260629074044`) | 3.28.0.163 beta (`20260702125656`) | 3.28.0.164 beta (`20260702125656`) | 3.28.0.166 beta (`20260806095513`) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Paper Pro | `ferrari` | Offline verified | Offline verified | **Device verified** | Offline verified | Offline verified | Offline verified |
| Paper Pro Move | `chiappa` | Offline verified | Offline verified | Offline verified | Offline verified | Offline verified | Offline verified |
| Paper Pure | `tatsu` | - | Offline verified | - | - | - | - |
| reMarkable 1 | `rm1` | - | Offline verified | - | - | - | - |
| reMarkable 2 | `rm2` | - | Offline verified | - | - | - | - |

Offline validation uses the official firmware image and includes QML resource
recovery, QMLDiff compatibility and replay, binary architecture, archive, and
hash checks. Only the Paper Pro 3.28 package has completed enable, disable,
rollback, and cold-boot validation on physical hardware so far.

The .163 and .164 beta releases share internal version `20260702125656`.
Their package names remain unique, and selection additionally requires the
exact stock xochitl SHA-256, so an old package cannot match the new firmware.
An exact rmtool or historical Vellum `.163` installation retained across the upgrade is
offered a verified cleanup path before `.164` can be installed; modified or
unknown state remains blocked.

In PDF and EPUB reading views, a short one-finger tap in the left-middle
region opens the previous page. The right edge and lower region open the next
page. Native swipes, stylus input, menus, zoom, selections, and document links
remain available.

## Runtime design

The package contains unmodified Xovi and qt-resource-rebuilder binaries, a
firmware-specific QMLDiff patch and hashtable, and `qmd-tool`. rmtool validates
the archive and every contained file before upload, then runs `qmd-tool check`
on the device before writing the xochitl systemd drop-in.

The persistent launcher verifies the architecture, platform, internal firmware
version, xochitl SHA-256, and every runtime payload hash on each boot. A
mismatch starts stock xochitl without `LD_PRELOAD`.

New installations always use rmtool's shared Xovi/QRR runtime. rmtool no longer
builds or installs Vellum APKs. It retains strict read-only validation and
targeted `vellum del rmtool-tap-page-turn` removal for its historical package;
Vellum, AppLoader, Xovi, and unrelated packages are never removed. The user
must then follow the [official Vellum CLI uninstall instructions](https://github.com/vellum-dev/vellum-cli#usage)
before rmtool enables shared-Xovi installation. Mixed runtimes are rejected.

Enabling or disabling never restarts xochitl or reboots the tablet. The user
must use the device menu to perform a full restart after the SSH deployment
session has closed. Immediately restarting xochitl from the same SSH session
is intentionally unsupported.

## Source and licenses

- Xovi: <https://github.com/asivery/xovi> (`LGPL-3.0`)
- rm-xovi-extensions / qt-resource-rebuilder:
  <https://github.com/asivery/rm-xovi-extensions> (`GPL-3.0`)
- qmd-tool: <https://github.com/boangs/rmkit> (`GPL-3.0`)

The QMLDiff source maintained by rmtool is under `qmd-src/`. Release archives
include the corresponding upstream license texts.
