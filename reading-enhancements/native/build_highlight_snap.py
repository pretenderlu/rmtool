"""Build and validate the exact-target highlighter Xovi extension."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.native_build import prefix_maps, verify_tool


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zig", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--beta-329", action="store_true")
    parser.add_argument("--release", choices=("3.28.0.172", "3.29.0.152", "3.29.0.153", "3.29.1.0"))
    args = parser.parse_args()
    verify_tool("zig", args.zig)
    if args.beta_329 and args.release not in (None, "3.29.0.152"):
        parser.error("--beta-329 is the legacy .152 selector; use --release alone")
    release = args.release or ("3.29.0.152" if args.beta_329 else "3.28.0.172")
    defines = [] if release == "3.28.0.172" else ["-DRMTOOL_HIGHLIGHT_BETA_329"]
    if release == "3.29.0.153":
        defines.append("-DRMTOOL_HIGHLIGHT_329_153")
    elif release == "3.29.1.0":
        defines.append("-DRMTOOL_HIGHLIGHT_32910")
    source = Path(__file__).with_name("highlight_snap.c")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            str(args.zig), "cc", "-target", "aarch64-linux-gnu", "-shared",
            "-fPIC", "-Os", "-Wall", "-Wextra", "-Werror",
            *defines,
            *prefix_maps(),
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
