# Official RemarkableWeRead payload

`manifest.json` pins Tencent's official RemarkableWeRead v1.0.0 download at
`cdn.weread.qq.com`. rmtool verifies the outer release ZIP, extracts only the
expected universal AArch64 archive, and verifies that inner archive again
before transferring it to the device. Neither archive is bundled in rmtool.

Verify the supplied official release locally with the focused tests:

```powershell
python -m unittest tests.test_weread_app
```

The runtime cache stores verified bytes atomically. Archive validation checks
exact sizes and SHA-256 values, safe member layout, version, source commit,
license, notice, and installer metadata.
