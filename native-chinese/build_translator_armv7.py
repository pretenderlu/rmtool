"""Rebuild the layout-aware ARMv7 translator with the audited Zig toolchain."""

import argparse
import os
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zig", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as temporary:
        executable = Path(temporary) / ("test_hook.exe" if os.name == "nt" else "test_hook")
        subprocess.run([
            str(args.zig), "cc", "-std=c11", "-DRMTOOL_RUNTIME_CATALOG",
            str(root / "xovi-src/test_hook.c"), "-o", str(executable),
        ], check=True)
        home = "/home/root/.local/share/rmtool/xovi-standalone"
        data = "/data/rmtool/xovi-standalone"
        for configured in (home, data, "", "/untrusted"):
            env = dict(os.environ, XOVI_ROOT=configured)
            selected = home if configured == home else data
            subprocess.run([str(executable), selected + "/native-chinese/reMarkable_zh_CN.qm"],
                           env=env, check=True)
    subprocess.run([
        str(args.zig), "cc", "-target", "arm-linux-gnueabihf.2.4", "-std=c11",
        "-Oz", "-shared", "-fPIC", "-fno-stack-protector", "-s",
        "-DRMTOOL_RUNTIME_CATALOG", str(root / "xovi-src/hook.c"),
        str(root / "xovi-src/xovi.c"), "-o", str(root / "native-chinese-translator-armv7.so"),
    ], check=True)


if __name__ == "__main__":
    main()
