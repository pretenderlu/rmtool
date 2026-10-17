"""Source contracts for the isolated Move QR UI; hardware acceptance is separate."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "qr-upload"


class QrUploadNativeTests(unittest.TestCase):
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

    def test_canary_build_is_exact_target_and_keeps_artifacts_out_of_source(self):
        build = (ROOT / "native/build.py").read_text(encoding="utf-8")
        self.assertIn("d947afca520b16a06efc0e5462d9c4db68928f2b4591ac67d59bc2e1f519c27d", build)
        self.assertIn('"apply-diffs"', build)
        self.assertIn('"ALL OK"', build)
        self.assertIn("TemporaryDirectory", build)
        self.assertFalse((ROOT / "native/moc_QrUpload.cpp").exists())
        self.assertFalse((ROOT / "native/rmtool-qr-upload.so").exists())

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
        self.assertIn('"/data/rmtool/xovi-standalone/helpers/rmtool-qr-upload"', source)
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
