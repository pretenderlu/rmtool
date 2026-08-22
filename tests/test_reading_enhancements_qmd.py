import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import _reading_enhancements as reading


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "reading-enhancements" / "qmd-src" / "reading-enhancements-3.28.qmd"


def _configured_path(variable: str) -> Path | None:
    value = os.environ.get(variable)
    return Path(value) if value else None


QMD_TOOL = _configured_path("RMTOOL_QMD_TOOL")
QMLDIFF = _configured_path("RMTOOL_QMLDIFF")
MATRIX_CONFIG = _configured_path("RMTOOL_READING_MATRIX_CONFIG")


def _load_builder():
    path = ROOT / "reading-enhancements" / "build_assets.py"
    spec = importlib.util.spec_from_file_location("reading_enhancements_build_assets", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load reading-enhancements build helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _targets() -> tuple[dict, ...]:
    if MATRIX_CONFIG is None:
        return ()
    document = json.loads(MATRIX_CONFIG.read_text(encoding="utf-8"))
    targets = []
    for target in document["targets"]:
        target = dict(target)
        target["id"] = (
            f"{target['platform']}-{target['firmware']}-"
            f"{target['xochitl_sha256'][:12]}"
        )
        targets.append(target)
    return tuple(targets)


class ReadingEnhancementsQmdTests(unittest.TestCase):
    def test_source_is_utf8_lf_text_without_nul_bytes(self):
        data = SOURCE.read_bytes()
        self.assertNotIn(b"\x00", data)
        self.assertNotIn(b"\r", data)
        SOURCE.read_text(encoding="utf-8")

    def test_source_contains_shared_contract(self):
        source = SOURCE.read_text(encoding="utf-8")

        for target in (
            "/qml/device/view/settings/Settings.qml",
            "/qml/device/view/documentview/SceneViewGestures.qml",
            "/qml/device/view/documentview/DocumentView.qml",
            "/qt/qml/xofm/libs/toolbar/qml/SettingsMenu.qml",
        ):
            self.assertIn(target, source)

        for marker in (
            "rmtoolReadingEnhancementsPage",
            "privatePage: 1001",
            "masterEnabled",
            "rmtoolGlobalTapPageTurnEnabled",
            "rmtoolGlobalFastMonoEnabled",
            "rmtoolGlobalCleanupEnabled",
            "rmtoolDocumentKey",
            "function rmtoolNormalizedInterval(value)",
            "documents/",
            "rmtoolTapPageTurnEnabled",
            "rmtoolCleanupEffective",
            "Settings.rawValue",
            "Settings.setRawValue",
            '"tapPageTurnEnabled"',
            '"fastMonoEnabled"',
            '"cleanupEnabled"',
            '"cleanupByChapter"',
            '"cleanupInterval"',
            "[5, 10, 15, 20, 25, 30]",
            "forceClearNow",
            "interval: 500",
            "tocModel",
            "onCurrentPageChanged",
            "onRmtoolReadingDocumentIdChanged",
            "const documentId = root.rmtoolReadingDocumentId",
            "rmtoolHasUsableToc",
            "const resetCleanup =",
            "documentInterval !== rmtoolCleanupInterval",
            "rmtoolCleanupLastBoundary = rmtoolTocBoundaryForPage(currentPage)",
            "notePage",
            "textMode",
            "itemSelectionMode",
            "textSelectionMode",
            "ArkControls.FoldoutToggle",
            "ArkControls.FoldoutItem",
            "stackView.push(rmtoolCleanupOptions)",
            "每 15 次翻页",
            "按章节",
            # Conditional settings panels must gray out when disabled
            # (opacity follows the stock DisplayVisibleContent pattern).
            "opacity: enabled ? 1 : 0.5",
        ):
            self.assertIn(marker, source, marker)
        self.assertEqual(source.count("opacity: enabled ? 1 : 0.5"), 5)

        self.assertIn("Settings.rawValue(\"RmtoolReadingEnhancements\",", source)
        self.assertIn("TRAVERSE Item#root", source)
        self.assertIn("LOCATE BEFORE Component#general", source)
        self.assertNotIn("TRAVERSE ?#general", source)
        self.assertIn("SettingsMenu.qml", source)
        self.assertNotIn("AFFECT /qml/device/view/main/MainView.qml", source)
        self.assertNotIn(
            'Settings.setRawValue("RmtoolReadingEnhancements", "fastMonoEnabled", false)',
            source,
        )
        self.assertIn(
            'description: "作为全局授权；开启后可在每本 PDF/EPUB 的阅读菜单中独立开关。"',
            source,
        )
        self.assertNotIn("重启后默认关闭", source)
        self.assertIn("readonly property bool rmtoolReadingAvailable: !!document\n                && !notePage", source)
        self.assertNotIn("function onDocumentChanged()", source)
        document_source = source.split(
            "AFFECT /qml/device/view/documentview/DocumentView.qml", 1
        )[1]
        self.assertNotIn("normalizedInterval(", document_source)
        self.assertNotIn("E:\\", source)

    def test_release_source_uses_the_exact_settings_root_selector(self):
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_327 = builder._source_for_release(SOURCE, "3.27.3.0", root / "327")
            source_328 = builder._source_for_release(SOURCE, "3.28.0.169", root / "328")
            text_327 = source_327.read_text(encoding="utf-8")
            text_328 = source_328.read_text(encoding="utf-8")
            bytes_328 = source_328.read_bytes()
        self.assertIn("TRAVERSE DeviceKeyboardNavigationHandler#settings", text_327)
        self.assertNotIn("TRAVERSE Item#root", text_327)
        self.assertIn("rmtoolSettingsRoot._selectedIndex = page", text_327)
        self.assertIn("rmtoolSettingsRoot.highlightedIndex = page", text_327)
        self.assertIn("TRAVERSE Item#root", text_328)
        self.assertNotIn("TRAVERSE DeviceKeyboardNavigationHandler#settings", text_328)
        self.assertIn("rmtoolSettingsRoot._selectedPage = page", text_328)
        self.assertNotIn("rmtoolSettingsRoot.highlightedIndex = page", text_328)
        self.assertEqual(bytes_328, SOURCE.read_bytes())
        for source in (text_327, text_328):
            self.assertIn("LOCATE BEFORE Component#general", source)
            self.assertNotIn("TRAVERSE ?#general", source)

    @unittest.skipUnless(
        SOURCE.exists()
        and QMD_TOOL is not None
        and QMD_TOOL.exists()
        and QMLDIFF is not None
        and QMLDIFF.exists()
        and MATRIX_CONFIG is not None
        and MATRIX_CONFIG.exists(),
        "full offline reading-enhancements matrix is not configured",
    )
    def test_matrix_is_exactly_fourteen_targets(self):
        targets = _targets()
        self.assertEqual(len(targets), 14)
        identities = {
            (target["platform"], target["firmware"], target["xochitl_sha256"])
            for target in targets
        }
        self.assertEqual(
            identities,
            {(key[0], key[1], key[3]) for key in reading.ALLOWED_TARGETS},
        )

    @unittest.skipUnless(
        SOURCE.exists()
        and QMD_TOOL is not None
        and QMD_TOOL.exists()
        and QMLDIFF is not None
        and QMLDIFF.exists()
        and MATRIX_CONFIG is not None
        and MATRIX_CONFIG.exists(),
        "full offline reading-enhancements matrix is not configured",
    )
    def test_compiles_checks_replays_and_asserts_all_targets(self):
        builder = _load_builder()
        targets = _targets()
        self.assertEqual(len(targets), 14)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for target in targets:
                target_id = target["id"]
                release = reading.ALLOWED_TARGETS[
                    (target["platform"], target["firmware"], "aarch64", target["xochitl_sha256"])
                ][0]
                source = builder._source_for_release(SOURCE, release, root / "sources")
                hashtab = Path(target["qrex_root"]) / target["hashtab"]
                qrex = Path(target["qrex_root"]) / "qrex-out"
                compiled = root / f"{target_id}.qmd"
                result = subprocess.run(
                    [str(QMD_TOOL), "hash", "-hashtab", str(hashtab), str(source)],
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                compiled.write_bytes(result.stdout)
                self.assertGreater(compiled.stat().st_size, 0)

                hashtabs = root / target_id / "hashtabs"
                qmds = root / target_id / "qmd"
                replay = root / target_id / "replay"
                hashtabs.mkdir(parents=True)
                qmds.mkdir()
                hashtabs.joinpath(f"hashtab-{target['platform']}-{target['firmware']}").write_bytes(
                    hashtab.read_bytes()
                )
                qmds.joinpath("reading-enhancements.qmd").write_bytes(compiled.read_bytes())

                check = subprocess.run(
                    [str(QMD_TOOL), "check", "-hashtabs", str(hashtabs), "-qmd", str(qmds)],
                    capture_output=True,
                    check=False,
                )
                check_output = check.stdout + check.stderr
                self.assertEqual(check.returncode, 0, check_output.decode(errors="replace"))
                self.assertIn(b"ALL OK", check_output)

                compatibility = subprocess.run(
                    [str(QMLDIFF), "check-compatibility", str(hashtab), str(compiled)],
                    capture_output=True,
                    check=False,
                )
                compatibility_output = compatibility.stdout + compatibility.stderr
                self.assertEqual(
                    compatibility.returncode,
                    0,
                    compatibility_output.decode(errors="replace"),
                )
                self.assertIn(b"No compatibility errors found", compatibility_output)

                replay_result = subprocess.run(
                    [
                        str(QMLDIFF),
                        "apply-diffs",
                        "--hashtab",
                        str(hashtab),
                        "-c",
                        str(qrex),
                        str(replay),
                        str(compiled),
                    ],
                    capture_output=True,
                    check=False,
                )
                replay_output = replay_result.stdout + replay_result.stderr
                self.assertEqual(
                    replay_result.returncode,
                    0,
                    replay_output.decode(errors="replace"),
                )
                settings = (replay / "qml/device/view/settings/Settings.qml").read_text(
                    encoding="utf-8"
                )
                gestures = (
                    replay / "qml/device/view/documentview/SceneViewGestures.qml"
                ).read_text(encoding="utf-8")
                document = (replay / "qml/device/view/documentview/DocumentView.qml").read_text(
                    encoding="utf-8"
                )
                menu = (
                    replay / "qt/qml/xofm/libs/toolbar/qml/SettingsMenu.qml"
                ).read_text(encoding="utf-8")
                self.assertIn("rmtoolReadingEnhancementsPage", settings)
                self.assertIsNone(
                    re.search(r"Component\s*\{\s*Component\s*\{", settings),
                    target_id,
                )
                self.assertEqual(
                    builder._component_indent(settings, "general"),
                    builder._component_indent(settings, "rmtoolReadingEnhancements"),
                    target_id,
                )
                self.assertIn("rmtoolTapPageDirection", gestures)
                self.assertIn("rmtoolHasUsableToc", document)
                self.assertIn("forceClearNow", document)
                self.assertIn("onRmtoolReadingDocumentIdChanged", document)
                self.assertRegex(
                    document,
                    r"readonly property\s+bool\s+rmtoolReadingAvailable:\s*!!document\s*&&\s*!notePage",
                )
                self.assertIn("rmtoolTapPageTurnToggle", menu)
                self.assertIn("rmtoolFastMonoToggle", menu)
                self.assertIn("rmtoolCleanupSelector", menu)
                if release.startswith("3.27."):
                    self.assertIn("rmtoolSettingsRoot._selectedIndex = page", settings)
                    self.assertIn("rmtoolSettingsRoot.highlightedIndex = page", settings)
                    self.assertIn(
                        "settings.sideBarItemClicked(settings.highlightedIndex);",
                        settings,
                    )
                    main = (replay / "qml/device/view/main/MainView.qml").read_text(
                        encoding="utf-8"
                    )
                    self.assertIn("rmtoolFastMonoReadingEnabled", main)
                else:
                    self.assertIn("rmtoolSettingsRoot._selectedPage = page", settings)
                    self.assertIn("root.sideBarItemClicked(root._selectedPage);", settings)
                    self.assertNotIn(
                        "rmtoolSettingsRoot.highlightedIndex = page", settings
                    )


if __name__ == "__main__":
    unittest.main()
