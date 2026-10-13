"""Rebuild the layout-aware ARMv7 translator with the audited Zig toolchain."""

import argparse
import os
import re
import subprocess
import tempfile
from pathlib import Path


def build_arm_translator(zig, work):
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary)
        # ARM Qt uses int for qsizetype; retain the published AArch64 source ABI.
        for name in ("hook.c", "xovi.c", "xovi.h", "test_hook.c"):
            text = (root / "xovi-src" / name).read_text()
            text = text.replace("_ZN7QStringC1EPK5QCharx", "_ZN7QStringC1EPK5QChari")
            text = text.replace("typedef long long i64;", "")
            text = re.sub(r"\bi64\b", "int", text)
            (source / name).write_text(text)
        executable = source / ("test_hook.exe" if os.name == "nt" else "test_hook")
        subprocess.run([
            str(zig), "cc", "-std=c11", "-DRMTOOL_RUNTIME_CATALOG",
            str(source / "test_hook.c"), "-o", str(executable),
        ], check=True, timeout=180)
        home = "/home/root/.local/share/rmtool/xovi-standalone"
        data = "/data/rmtool/xovi-standalone"
        for configured in (home, data, "", "/untrusted"):
            env = dict(os.environ, XOVI_ROOT=configured)
            selected = home if configured == home else data
            subprocess.run([str(executable), selected + "/native-chinese/reMarkable_zh_CN.qm"],
                           env=env, check=True, timeout=30)
        destination = work / "native-chinese-translator-armv7-qt32.so"
        subprocess.run([
            str(zig), "cc", "-target", "arm-linux-gnueabihf.2.4", "-std=c11",
            "-Oz", "-shared", "-fPIC", "-fno-stack-protector", "-s",
            "-DRMTOOL_RUNTIME_CATALOG", str(source / "hook.c"),
            str(source / "xovi.c"), "-o", str(destination),
        ], check=True, timeout=180)
        payload = destination.read_bytes()
        if b"I_ZN7QStringC1EPK5QChari\0" not in payload or b"_ZN7QStringC1EPK5QCharx" in payload:
            raise RuntimeError("Incorrect ARM QString ABI in generated translator")
        return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zig", type=Path, required=True)
    args = parser.parse_args()
    build_arm_translator(args.zig, Path(__file__).resolve().parent)


if __name__ == "__main__":
    main()
