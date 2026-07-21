# Persistent tap-to-turn assets

rmtool downloads firmware-specific tap-to-turn packages from the fixed
`tap-page-turn-assets` GitHub release. Packages are selected by all of:

- hardware platform;
- CPU architecture;
- the 14-digit internal firmware version; and
- the SHA-256 of `/usr/bin/xochitl`.

## Support matrix

| Device | Platform | 3.27.1.0 stable (`20260506100933`) | 3.27.3.0 stable (`20260612085811`) | 3.28.0.162 beta (`20260629074044`) |
| --- | --- | --- | --- | --- |
| Paper Pro | `ferrari` | Offline verified | Offline verified | **Device verified** |
| Paper Pro Move | `chiappa` | Offline verified | Offline verified | Offline verified |
| Paper Pure | `tatsu` | - | Offline verified | - |
| reMarkable 1 | `rm1` | - | Offline verified | - |
| reMarkable 2 | `rm2` | - | Offline verified | - |

Offline validation uses the official firmware image and includes QML resource
recovery, QMLDiff compatibility and replay, binary architecture, archive, and
hash checks. Only the Paper Pro 3.28 package has completed enable, disable,
rollback, and cold-boot validation on physical hardware so far.

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
