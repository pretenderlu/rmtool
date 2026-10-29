# WeRead launcher

This rmtool-owned shared-Xovi feature adds one device Settings entry for the
official WeRead v1.0.0 installation on Paper Pro and Move 3.28.0.172,
3.29.0.152, 3.29.0.153 and stable 3.29.1.0. The 3.29 packages completed full
stock-QML replay. The user confirmed the stable launcher on Move; other targets
still require hardware acceptance.

It does not bundle or modify WeRead. The native QML bridge accepts only the
three fixed launch modes and an allowlisted refresh interval, then starts the
verified official launcher. Color-fast and monochrome-fast modes load a
firmware-specific process-local shim; normal mode keeps the official display
behavior.
