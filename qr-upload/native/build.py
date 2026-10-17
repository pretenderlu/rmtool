"""Build and replay-check the isolated Move .172 QR settings canary."""

import argparse
import hashlib
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path


def run(command):
    result = subprocess.run([str(arg) for arg in command], capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError((result.stdout + result.stderr).decode(errors="replace"))
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qt-host", type=Path, required=True)
    parser.add_argument("--zig", type=Path, required=True)
    parser.add_argument("--qmd-tool", type=Path, required=True)
    parser.add_argument("--qmldiff", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--receiver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    hashtab = args.target / "hashtab"
    if hashlib.sha256(hashtab.read_bytes()).hexdigest() != "d947afca520b16a06efc0e5462d9c4db68928f2b4591ac67d59bc2e1f519c27d":
        raise RuntimeError("This UI canary is pinned to Move 3.28.0.172")
    receiver = args.receiver.read_bytes()
    if receiver[:4] != b"\x7fELF" or struct.unpack_from("<H", receiver, 18)[0] != 183:
        raise RuntimeError("An AArch64 receiver is required")
    here = Path(__file__).resolve().parent
    args.output.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(receiver).hexdigest()
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        run([args.qt_host / "bin/moc.exe", here / "QrUpload.h", "-o", work / "moc_QrUpload.cpp"])
        include = args.qt_host / "include"
        bridge = args.output / "rmtool-qr-upload.so"
        run([args.zig, "c++", "-target", "aarch64-linux-gnu.2.17", "-std=c++17", "-shared", "-fPIC",
             "-O2", "-Wl,--allow-shlib-undefined", f'-DRMTOOL_QR_RECEIVER_SHA256="{digest}"',
             "-I", include, "-I", include / "QtCore", "-I", include / "QtQml", "-I", work,
             here / "main.cpp", "-o", bridge])
        compiled = run([args.qmd_tool, "hash", "-hashtab", hashtab, here / "qr-upload-3.28.qmd"])
        qmd = args.output / "rmtool-qr-upload.qmd"
        qmd.write_bytes(compiled)
        (work / "hashtabs").mkdir()
        (work / "qmds").mkdir()
        shutil.copy2(hashtab, work / "hashtabs/hashtab-move-172")
        shutil.copy2(qmd, work / "qmds/rmtool-qr-upload.qmd")
        if b"ALL OK" not in run([args.qmd_tool, "check", "-hashtabs", work / "hashtabs", "-qmd", work / "qmds"]):
            raise RuntimeError("Incomplete QMD validation")
        run([args.qmldiff, "apply-diffs", "--hashtab", hashtab, "-c", args.target / "qrex-out", work / "replay", qmd])
        settings = (work / "replay/qml/device/view/settings/Settings.qml").read_text(encoding="utf-8")
        for required in ("import cn.rmtool.QrUpload 1.0", "QRCodeProvider", "rmtoolQrTransferPage", "rmtoolQrBridge.stop()"):
            if required not in settings:
                raise RuntimeError(f"Missing replay marker: {required}")
        if "executeCommand" in settings or "systemctl" in settings:
            raise RuntimeError("QML must not expose a shell API")
        shutil.copy2(work / "replay/qml/device/view/settings/Settings.qml", args.output / "Settings.replayed.qml")
        for artifact in (bridge, qmd):
            print(artifact.name, artifact.stat().st_size, hashlib.sha256(artifact.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
