# WeRead launcher

This rmtool-owned shared-Xovi feature adds one device Settings entry for the
official WeRead v1.0.0 installation on Paper Pro and Move 3.28.0.172.

It does not bundle or modify WeRead. The native QML bridge accepts only the
three fixed launch modes and an allowlisted refresh interval, then starts the
verified official launcher. Color-fast and monochrome-fast modes load a
firmware-specific process-local shim; normal mode keeps the official display
behavior.
