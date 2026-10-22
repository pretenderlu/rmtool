"""QR bridge trust boundaries; hardware acceptance is separate."""

import hashlib
import importlib.util
import json
import os
import re
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1] / "qr-upload"


class QrUploadNativeTests(unittest.TestCase):
    def test_receiver_builder_resolves_relative_output_before_changing_cwd(self):
        spec = importlib.util.spec_from_file_location("qr_receiver_builder", ROOT / "build.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=ROOT.parent / "build") as temporary:
            output = Path(temporary) / "receiver"
            def run(command, **kwargs):
                if "build" in command:
                    path = Path(command[command.index("-o") + 1])
                    self.assertTrue(path.is_absolute())
                    self.assertEqual(path.parent, output)
                    data = bytearray(20)
                    data[:4], data[5] = b"\x7fELF", 1
                    struct.pack_into("<H", data, 18, 40 if kwargs['env']['GOARCH'] == 'arm' else 183)
                    path.write_bytes(data)
            argv = ["build.py", "--go", "go", "--output", os.path.relpath(output)]
            with patch("sys.argv", argv), patch.object(module.subprocess, "run", side_effect=run), patch("builtins.print"):
                module.main()
            self.assertEqual(len(list(output.iterdir())), 2)

    def test_bridge_exposes_only_start_and_stop(self):
        header = (ROOT / "native/QrUpload.h").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"Q_INVOKABLE void (\w+)\((.*?)\);", header),
                         [("start", ""), ("stop", "")])
        source = (ROOT / "native/main.cpp").read_text(encoding="utf-8")
        self.assertIn("RMTOOL_QR_RECEIVER_SHA256", source)
        self.assertIn("if (!trustedReceiver())", source)
        self.assertIn("info.isSymLink()", source)
        self.assertIn("info.ownerId() != 0", source)
        self.assertIn("QProcessEnvironment::systemEnvironment()", source)
        self.assertIn('"LD_PRELOAD", "XOVI_ROOT"', source)
        self.assertNotIn("startDetached", source)
        self.assertNotIn("/bin/sh", source)
        self.assertNotIn("systemctl", source)

    def test_close_and_owner_exit_stop_receiving(self):
        qmd = (ROOT / "native/qr-upload-3.28.qmd").read_text(encoding="utf-8")
        self.assertIn("onVisibleChanged: { if (!visible) rmtoolQrBridge.stop() }", qmd)
        self.assertIn("Component.onDestruction: rmtoolQrBridge.stop()", qmd)
        self.assertIn("onPowerButtonShortPress() { rmtoolQrBridge.stop() }", qmd)
        source = (ROOT / "native/main.cpp").read_text(encoding="utf-8")
        self.assertIn('QStringLiteral("--parent-pid")', source)
        self.assertIn("m_process->terminate()", source)
        self.assertIn("process->kill()", source)
        self.assertNotIn("waitForFinished", source)
        self.assertNotIn("QTimer::singleShot", source)
        self.assertIn("deadline->setInterval(4000)", source)

    def test_qr_entry_is_session_scoped_and_does_not_change_stock_settings(self):
        qmd = (ROOT / "native/qr-upload-3.28.qmd").read_text(encoding="utf-8")
        self.assertEqual(qmd.count('text: "开启接收"'), 1)
        self.assertEqual(qmd.count('text: "停止接收"'), 1)
        self.assertIn("QRCodeProvider", qmd)
        self.assertIn("text: rmtoolQrBridge.url", qmd)
        self.assertIn("smooth: false", qmd)
        self.assertIn("传输未加密，仅限可信 Wi-Fi", qmd)
        self.assertNotIn("Settings.webInterfaceEnabled =", qmd)
        self.assertNotIn("executeCommand", qmd)

    def test_build_keeps_artifacts_out_of_source(self):
        build = (ROOT / "native/build.py").read_text(encoding="utf-8")
        self.assertIn('"apply-diffs"', build)
        self.assertIn('"ALL OK"', build)
        self.assertIn("TemporaryDirectory", build)
        self.assertFalse((ROOT / "native/moc_QrUpload.cpp").exists())
        self.assertFalse((ROOT / "native/rmtool-qr-upload.so").exists())

    def test_builder_rejects_wrong_identity_resources_and_receiver_before_compiling(self):
        import _native_import_control as control
        spec = importlib.util.spec_from_file_location("qr_native_builder", ROOT / "native/build.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for platform in ("ferrari", "rm1"):
            package = next(p for p in control._trusted_catalog()
                           if p.platform == platform and p.release_version == "3.29.0.153")
            record = {key: getattr(package, key) for key in ("firmware", "platform", "architecture", "xochitl_sha256")}
            elf = bytearray(20)
            elf[:6] = b"\x7fELF" + bytes((1 if platform == "rm1" else 2, 1))
            struct.pack_into("<H", elf, 18, 40 if platform == "rm1" else 183)
            fake_carrier = SimpleNamespace(file=lambda path: SimpleNamespace(sha256=hashlib.sha256(b"exact").hexdigest()))
            for case in ("valid", "identity", "resources", "architecture", "class", "endian", "truncated"):
                with self.subTest(platform=platform, case=case), tempfile.TemporaryDirectory() as temporary:
                    work = Path(temporary)
                    current = dict(record)
                    if case == "identity":
                        current["xochitl_sha256"] = "0" * 64
                    (work / "identity.json").write_text(json.dumps(current))
                    (work / "hashtab").write_bytes(b"wrong" if case == "resources" else b"exact")
                    data = bytearray(elf)
                    if case == "architecture":
                        struct.pack_into("<H", data, 18, 183 if platform == "rm1" else 40)
                    if case in ("class", "endian"):
                        data[4 if case == "class" else 5] = 0
                    (work / "receiver").write_bytes(b"\x7fELF" if case == "truncated" else data)
                    argv = ["build.py", "--target", str(work), "--receiver", str(work / "receiver"),
                            "--output", str(work / "output"), "--qt-host", str(work), "--zig", "zig",
                            "--qmd-tool", "qmd-tool", "--qmldiff", "qmldiff"]
                    message = "compiler boundary" if case == "valid" else "exact verified|architecture"
                    with (patch("sys.argv", argv), patch.object(module.control, "select_package", return_value=fake_carrier),
                          patch.object(module, "run", side_effect=RuntimeError("compiler boundary")) as compile_step):
                        with self.assertRaisesRegex(RuntimeError, message):
                            module.main()
                        self.assertEqual(compile_step.call_count, 1 if case == "valid" else 0)

    def test_packager_rejects_unported_device_release_before_reading_files(self):
        spec = importlib.util.spec_from_file_location("qr_asset_builder", ROOT / "build_assets.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        argv = ["build_assets.py", "--platform", "rm1", "--release", "3.28.0.172", "--receiver", "missing",
                "--native-dir", "missing", "--carrier", "missing", "--output-dir", "missing"]
        with patch("sys.argv", argv), patch("sys.stderr"), self.assertRaises(SystemExit) as error:
            module.main()
        self.assertEqual(error.exception.code, 2)

    def test_device_transfer_progress_is_bounded_plain_text(self):
        qmd = (ROOT / "native/qr-upload-3.28.qmd").read_text(encoding="utf-8")
        self.assertIn('iconSource: "qrc:/ark/icons/import"', qmd)
        self.assertNotIn('qrc:/ark/icons/share', qmd)
        self.assertIn("ArkControls.ProgressBar", qmd)
        self.assertIn("elide: Text.ElideMiddle", qmd)
        self.assertIn("textFormat: Text.PlainText", qmd)
        source = (ROOT / "native/main.cpp").read_text(encoding="utf-8")
        self.assertIn("m_poll.setInterval(1000)", source)
        self.assertIn("info.size() > 4096", source)
        self.assertIn("received > 200000000", source)
        self.assertIn("total > 200000000", source)
        self.assertIn("filename.toUtf8().size() > 240", source)
        self.assertNotIn("m_stopping || !m_url.isEmpty()", source)
        self.assertIn("m_transferDetail == detail && m_progress == progress", source)

    def test_managed_importer_waits_for_address_and_restores_owned_preference(self):
        source = (ROOT / "native/main.cpp").read_text(encoding="utf-8")
        qmd = (ROOT / "native/qr-upload-3.28.qmd").read_text(encoding="utf-8")
        self.assertIn("settings: Settings", qmd)
        self.assertIn('qgetenv("XOVI_ROOT")', source)
        self.assertIn('root != QStringLiteral("/data/rmtool/xovi-standalone")', source)
        self.assertIn('root != QStringLiteral("/home/root/.local/share/rmtool/xovi-standalone")', source)
        self.assertIn('QStringLiteral("/helpers/rmtool-qr-upload")', source)
        self.assertIn('QStringLiteral("--activate-native")', source)
        self.assertIn("QSaveFile", source)
        self.assertIn("file.setDirectWriteFallback(false)", source)
        self.assertIn('m_settings->setProperty("webInterfaceEnabled", false)', source)
        start = source.split("void QrUpload::start()", 1)[1].split("void QrUpload::stop()", 1)[0]
        self.assertNotIn("enableImportPreference()", start)
        read = source.split("void QrUpload::readSession()", 1)[1]
        self.assertLess(read.index("native-ready"), read.index("enableImportPreference()"))
        self.assertIn("restoreImportPreference();", source.split("void QrUpload::stop()", 1)[1])


if __name__ == "__main__":
    unittest.main()
