"""Recreate the original AArch64 translator, including its catalog-path ABI."""

import argparse
import subprocess
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.native_build import prefix_maps, verify_tool


def build_translator(zig, output):
    zig = verify_tool("zig", zig)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    source = ROOT / "native-chinese/xovi-src"
    with tempfile.TemporaryDirectory() as temporary:
        for name in ("test_metadata", "test_hook"):
            executable = Path(temporary) / (name + (".exe" if os.name == "nt" else ""))
            subprocess.run([str(zig), "cc", "-std=c11", "-include", "string.h",
                            str(source / (name + ".c")), "-o", str(executable)], check=True, timeout=180)
            arguments = [] if name == "test_metadata" else ["/data/rmtool/xovi-standalone/native-chinese/" + "./" * 9 + "reMarkable_zh_CN.qm"]
            subprocess.run([str(executable), *arguments], check=True, timeout=30)
    command = [str(zig), "cc", "-target", "aarch64-linux-gnu.2.17", "-std=c11",
               "-Oz", "-shared", "-fPIC", "-fno-stack-protector", "-s", *prefix_maps(),
               str(source / "hook.c"), str(source / "xovi.c"), "-o", str(output)]
    subprocess.run(command, check=True, timeout=180)
    first = output.read_bytes()
    subprocess.run(command, check=True, timeout=180)
    if first != output.read_bytes():
        raise RuntimeError("Non-deterministic AArch64 translator")
    if (first[:6] != b"\x7fELF\x02\x01" or int.from_bytes(first[18:20], "little") != 183
            or b"I_ZN7QStringC1EPK5QCharx\0" not in first or b"_ZN7QStringC1EPK5QChari" in first):
        raise RuntimeError("Incorrect AArch64 translator ABI")
    return first


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zig", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build_translator(args.zig, args.output)


if __name__ == "__main__":
    main()
