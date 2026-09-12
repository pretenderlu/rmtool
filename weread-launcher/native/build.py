"""Cross-build the narrow aarch64 WeRead QML bridge."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path


def run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qt-host", type=Path, required=True)
    parser.add_argument("--zig", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--shim-output", type=Path, required=True)
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    moc = args.qt_host / "bin" / "moc.exe"
    rcc = args.qt_host / "bin" / "rcc.exe"
    include = args.qt_host / "include"
    generated = here / "moc_WeReadLauncher.cpp"
    generated_resource = here / "qrc_weread_assets.cpp"
    args.shim_output.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            str(args.zig),
            "c++",
            "-target",
            "aarch64-linux-gnu.2.17",
            "-std=c++17",
            "-shared",
            "-fPIC",
            "-O2",
            "-fno-exceptions",
            "-fno-rtti",
            "-ffunction-sections",
            "-fdata-sections",
            "-Wl,--gc-sections",
            "-s",
            "-ldl",
            str(here / "fast_refresh.cpp"),
            "-o",
            str(args.shim_output),
        ]
    )
    shim_sha256 = hashlib.sha256(args.shim_output.read_bytes()).hexdigest()
    run([str(moc), str(here / "WeReadLauncher.h"), "-o", str(generated)])
    run([
        str(rcc),
        "--name",
        "weread_assets",
        "--output",
        str(generated_resource),
        str(here / "weread_assets.qrc"),
    ])
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        run(
            [
                str(args.zig),
                "c++",
                "-target",
                "aarch64-linux-gnu.2.17",
                "-std=c++17",
                "-shared",
                "-fPIC",
                "-O2",
                "-Wl,--allow-shlib-undefined",
                f'-DRMTOOL_WEREAD_FAST_SHA256="{shim_sha256}"',
                "-I",
                str(include),
                "-I",
                str(include / "QtCore"),
                "-I",
                str(include / "QtQml"),
                str(here / "main.cpp"),
                str(generated_resource),
                "-o",
                str(args.output),
            ]
        )
    finally:
        generated.unlink(missing_ok=True)
        generated_resource.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
