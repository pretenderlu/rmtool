"""Cross-build the narrow aarch64 WeRead QML bridge."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.native_build import check_no_build_paths, prefix_maps, verify_tool


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
    for name, path in (("zig", args.zig), ("moc", args.qt_host / "bin/moc.exe"), ("rcc", args.qt_host / "bin/rcc.exe")):
        verify_tool(name, path)

    here = Path(__file__).resolve().parent
    moc = args.qt_host / "bin" / "moc.exe"
    rcc = args.qt_host / "bin" / "rcc.exe"
    include = args.qt_host / "include"
    temporary = tempfile.TemporaryDirectory()
    work = Path(temporary.name)
    generated = work / "moc_WeReadLauncher.cpp"
    generated_resource = work / "qrc_weread_assets.cpp"
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
            *prefix_maps((work, "weread-generated"), (args.qt_host, "qt"), (args.zig.parent, "zig")),
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
                "-s",
                "-Wl,--allow-shlib-undefined",
                *prefix_maps((work, "weread-generated"), (args.qt_host, "qt"), (args.zig.parent, "zig")),
                f'-DRMTOOL_WEREAD_FAST_SHA256="{shim_sha256}"',
                "-I",
                str(include),
                "-I",
                str(include / "QtCore"),
                "-I",
                str(include / "QtQml"),
                "-I", str(work),
                str(here / "main.cpp"),
                str(generated_resource),
                "-o",
                str(args.output),
            ]
        )
    finally:
        temporary.cleanup()
    check_no_build_paths(args.output, args.qt_host, args.zig.parent)
    check_no_build_paths(args.shim_output, args.qt_host, args.zig.parent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
