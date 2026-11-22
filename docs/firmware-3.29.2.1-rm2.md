# reMarkable 2 stable 3.29.2.1

## Exact target

On 2026-11-22 the [official inventory](https://remarkable-software.s3.us-east-2.amazonaws.com/?list-type=2&prefix=remarkable-production-image-3.29.2.1-)
listed only RM2 for this release. The four other model-specific URLs returned
404. No other platform was added.

- [Official RM2 SWU](https://dlqathbgqp3nv.cloudfront.net/remarkable-production-image-3.29.2.1-rm2-public.swu)
  (100,275,712 bytes): SHA-256 `e0e5c17c0ba8af673265d937085cb40a8e1391b363a2d02261008e54d5be28c7`.
  Its compressed root image matches the SHA-256 in the SWU description.
- Internal build `20261110104847`, ARMv7 hard float. Stock xochitl:
  `dec257e07b443685a612a058119a88ede6dcba5365b81e265180785851ada6cc`.
- qrex recovered 625 QML files. The new hashtab SHA-256 is
  `d6ac1d3e39403cfb865bcc4f8ef353bb95020f524cd774b356f74d1850ff3ec2`.
  All 1,765 required stock and embedded QML translation identities are
  covered. Recompilation yielded the exact RM2 3.29.1.0 catalog bytes, so the
  existing immutable asset is referenced by the new firmware identity.
  No French-slot write path is enabled.

## Offline coverage

Five new pinned archives cover native Simplified Chinese, Pinyin, tap
navigation, native desktop import and QR reception. The normal local cache
verifies each archive against the manifest. The five patches replayed against
stock QML in both orders, including with the unmodified official AppLoad v0.6.0
patch. Native import, QR and AppLoad resolved 121, 185 and 287 strong stock
ABI imports respectively. The pinned KOReader v2026.07.1 archive and ARM
launcher were verified. Stock USB uses `10.11.99.1/29`; the importer still
posts the multipart field `file` to `/upload`.

All five archives were byte-identical across independent builds. The QR
bridge produced the same bytes with absolute and relative Qt host paths after
the build normalized the path before prefix mapping. The executable Go gate
accepts the exact RM2 identity and rejects cross-firmware/architecture inputs.

All checks are offline. RM2 still starts stock xochitl before mounting
`/home`. The shared runtime stays on root-backed `/data` and all reserve,
firmware write, home/data font and startup-protection rules are unchanged.

## Clean-install capacity

The stock ext4 directory listing has only two entries directly under
`/usr/share/fonts/ttf`: the directories `ebgaramond` and `noto`.
Root-font recovery considers only an active custom TTF/OTF file directly
in that directory on this exact firmware. It does not scan the stock
subdirectories or use a family-name or upstream font hash allowlist.

Replaying the reported RM2's read-only capacity probe with rmtool gives
18.67 MiB of privileged usable space before the 8 MiB safety reserve.
There is no existing verified Xovi runtime to reuse. Each row is a separate
first installation using the exact new packages:

| Plugin | Required, including reserve | Shortfall in the report |
| --- | ---: | ---: |
| Native Simplified Chinese | 19.68 MiB | 1.01 MiB |
| Pinyin | 19.59 MiB | 0.92 MiB |
| Tap navigation | 19.46 MiB | 0.79 MiB |
| Native desktop import | 22.13 MiB | 3.46 MiB |
| QR reception | 26.89 MiB | 8.22 MiB |
| AppLoad | 30.14 MiB | 11.47 MiB |

The new firmware matches the installer, but free space still needs to be
checked on-device. None of these numbers authorizes removing unknown files,
relaxing the reserve, or inferring a successful hardware installation.
