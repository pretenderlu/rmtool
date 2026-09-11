"""Build and validate the exact-target highlighter Xovi extension."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zig", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).with_name("highlight_snap.c")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            str(args.zig), "cc", "-target", "aarch64-linux-gnu", "-shared",
            "-fPIC", "-Os", "-Wall", "-Wextra", "-Werror",
            str(source), "-o", str(args.output),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    data = args.output.read_bytes()
    if not data.startswith(b"\x7fELF") or data[4] != 2 or data[5] != 1:
        raise RuntimeError("highlight extension is not a little-endian ELF64 file")
    if int.from_bytes(data[18:20], "little") != 183:
        raise RuntimeError("highlight extension is not AArch64")
    for symbol in (b"_xovi_shouldLoad", b"_xovi_construct", b"_xovi_depconstruct"):
        if symbol not in data:
            raise RuntimeError(f"missing exported symbol: {symbol.decode()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
