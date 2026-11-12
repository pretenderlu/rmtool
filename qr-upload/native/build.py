"""Build and replay-check the QR settings bridge against exact firmware resources."""

import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import _native_import_control as control
import _qr_upload as qr
import _tap_page_turn as tap
from tools.native_build import check_no_build_paths, prefix_maps, verify_tool

QRCODE_MODULE = "xofm.libs.qrcode 1.0"
QRCODE_IMPORT = f"import {QRCODE_MODULE}"
POWER_MODULE = "com.remarkable 1.0"
POWER_ALIAS = "RmtoolQrSystem"
POWER_TARGET = f"{POWER_ALIAS}.PowerManager"


def settings_source_330(text):
    settings_import = "AFFECT /qml/device/view/settings/Settings.qml\n    IMPORT cn.rmtool.QrUpload 1.0\n"
    # The stock SettingsWindow passes in settings; PowerManager is a separate singleton.
    for old, new in (
        (settings_import, settings_import + f"    IMPORT {QRCODE_MODULE}\n    IMPORT {POWER_MODULE} {POWER_ALIAS}\n"),
        ("QrUpload { id: rmtoolQrBridge; settings: Settings }", "QrUpload { id: rmtoolQrBridge; settings: root.settings }"),
        ("target: PowerManager", f"target: {POWER_TARGET}"),
    ):
        if text.count(old) != 1:
            raise RuntimeError(f"Expected one QR settings adaptation anchor: {old.strip()}")
        text = text.replace(old, new)
    return text


def validate_qr_settings(settings, *, beta_330):
    for required in ("import cn.rmtool.QrUpload 1.0", "QRCodeProvider", "rmtoolQrTransferPage", "rmtoolQrBridge.stop()"):
        if required not in settings:
            raise RuntimeError(f"Missing replay marker: {required}")
    if beta_330:
        for name, line in (("QRCodeProvider", QRCODE_IMPORT), ("PowerManager", f"import {POWER_MODULE} as {POWER_ALIAS}")):
            if not re.search(rf"^{re.escape(line)}[ \t]*$", settings, re.MULTILINE):
                raise RuntimeError(f"Missing {name} module in replayed Settings.qml: {line}")
        if not re.search(r"\bQrUpload\s*\{[^}]*\bsettings:\s*root\.settings\b", settings):
            raise RuntimeError("QR bridge must use the SettingsWindow settings object")
        if not re.search(rf"\btarget:\s*{re.escape(POWER_TARGET)}\b", settings):
            raise RuntimeError("QR power button handler must use the imported singleton")
    if "executeCommand" in settings or "systemctl" in settings:
        raise RuntimeError("QML must not expose a shell API")


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
    parser.add_argument("--identity", type=Path, help="Exact manifest identity when the resource tree has no identity.json")
    parser.add_argument("--receiver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    hashtab = args.target / "hashtab"
    record = json.loads((args.identity or args.target / "identity.json").read_text(encoding="utf-8"))
    identity = tap.DeviceIdentity(*(record[k] for k in ("firmware", "platform", "architecture", "xochitl_sha256")))
    key = (identity.platform, identity.firmware, identity.architecture, identity.xochitl_sha256)
    carrier = control.select_package(control._trusted_catalog(), identity)
    if (key not in qr.ALLOWED_TARGETS or carrier is None
            or hashlib.sha256(hashtab.read_bytes()).hexdigest() != carrier.file("exthome/qt-resource-rebuilder/hashtab").sha256):
        raise RuntimeError("QR settings require exact verified firmware resources")
    beta_330 = qr.ALLOWED_TARGETS[key][0].startswith("3.30.")
    target = "aarch64-linux-gnu.2.17" if identity.architecture == "aarch64" else "arm-linux-gnueabihf.2.4"
    machine = 183 if identity.architecture == "aarch64" else 40
    receiver = args.receiver.read_bytes()
    elf_header = b"\x7fELF" + bytes((2 if identity.architecture == "aarch64" else 1, 1))
    if len(receiver) < 20 or receiver[:6] != elf_header or struct.unpack_from("<H", receiver, 18)[0] != machine:
        raise RuntimeError("Receiver architecture differs from the target")
    for name, path in (("zig", args.zig), ("moc", args.qt_host / "bin/moc.exe"),
                       ("qmd-tool", args.qmd_tool), ("qmldiff", args.qmldiff)):
        verify_tool(name, path)
    here = Path(__file__).resolve().parent
    args.output.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(receiver).hexdigest()
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        run([args.qt_host / "bin/moc.exe", here / "QrUpload.h", "-o", work / "moc_QrUpload.cpp"])
        run([args.qt_host / "bin/moc.exe", here / "ImportControl.h", "-o", work / "moc_ImportControl.cpp"])
        include = args.qt_host / "include"
        bridge = args.output / "rmtool-qr-upload.so"
        run([args.zig, "c++", "-target", target, "-std=c++17", "-shared", "-fPIC",
             "-O2", "-s", "-Wl,--allow-shlib-undefined", f'-DRMTOOL_QR_RECEIVER_SHA256="{digest}"',
             *prefix_maps((work, "qr-upload-generated"), (args.qt_host, "qt"), (args.zig.parent, "zig")),
             "-I", include, "-I", include / "QtCore", "-I", include / "QtQml", "-I", include / "QtNetwork", "-I", work,
             here / "main.cpp", here / "ImportControl.cpp", "-o", bridge])
        check_no_build_paths(bridge, work, args.qt_host, args.zig.parent)
        source = here / "qr-upload-3.28.qmd"
        if beta_330:
            text = settings_source_330(source.read_text(encoding="utf-8"))
            source = work / "qr-upload-3.30.qmd"
            source.write_text(text, encoding="utf-8", newline="\n")
        compiled = run([args.qmd_tool, "hash", "-hashtab", hashtab, source])
        qmd = args.output / "rmtool-qr-upload.qmd"
        qmd.write_bytes(compiled)
        (work / "hashtabs").mkdir()
        (work / "qmds").mkdir()
        shutil.copy2(hashtab, work / "hashtabs/hashtab-move")
        shutil.copy2(qmd, work / "qmds/rmtool-qr-upload.qmd")
        if b"ALL OK" not in run([args.qmd_tool, "check", "-hashtabs", work / "hashtabs", "-qmd", work / "qmds"]):
            raise RuntimeError("Incomplete QMD validation")
        run([args.qmldiff, "apply-diffs", "--hashtab", hashtab, "-c", args.target / "qrex-out", work / "replay", qmd])
        settings = (work / "replay/qml/device/view/settings/Settings.qml").read_text(encoding="utf-8")
        main_view = (work / "replay/qml/device/view/main/MainView.qml").read_text(encoding="utf-8")
        if "ImportControl" not in main_view or "settings: Settings" not in main_view:
            raise RuntimeError("Desktop native import control is missing from the main view")
        validate_qr_settings(settings, beta_330=beta_330)
        shutil.copy2(work / "replay/qml/device/view/settings/Settings.qml", args.output / "Settings.replayed.qml")
        for artifact in (bridge, qmd):
            print(artifact.name, artifact.stat().st_size, hashlib.sha256(artifact.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
