import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import _note_enhancements as note


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "note-enhancements/qmd-src/note-enhancements-3.28.qmd"
READING_SOURCE = ROOT / "reading-enhancements/qmd-src/reading-enhancements-3.28.qmd"


def _configured_path(variable: str) -> Path | None:
    value = os.environ.get(variable)
    return Path(value) if value else None


QMD_TOOL = _configured_path("RMTOOL_QMD_TOOL")
QMLDIFF = _configured_path("RMTOOL_QMLDIFF")
MATRIX_CONFIG = _configured_path("RMTOOL_READING_MATRIX_CONFIG")


def _load_builder():
    path = ROOT / "note-enhancements/build_assets.py"
    spec = importlib.util.spec_from_file_location("note_enhancements_build_assets", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load note-enhancements build helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_reading_builder():
    path = ROOT / "reading-enhancements/build_assets.py"
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
    return tuple(dict(target) for target in document["targets"])


class NoteEnhancementsQmdTests(unittest.TestCase):
    def test_source_has_revision_twelve_press_safe_policy_toggles(self):
        data = SOURCE.read_bytes()
        self.assertNotIn(b"\x00", data)
        self.assertNotIn(b"\r", data)
        source = data.decode("utf-8")
        for marker in (
            "/qml/device/view/settings/Settings.qml",
            "/qt/qml/xofm/libs/toolbar/qml/SettingsMenu.qml",
            "/qml/device/view/documentview/DocumentView.qml",
            "/qml/device/view/documentview/DeviceSceneView.qml",
            "rmtoolNoteEnhancementsSidebarItem",
            "rmtoolNoteEnhancementsPage",
            "rmtoolNoteDelayToggle",
            "rmtoolNoteDelaySelector",
            "rmtoolNoteIdleToggle",
            "rmtoolNotePageOnlyToggle",
            'iconSource: "qrc:/ark/icons/notebook"',
            'readonly property var delayChoices: [5, 10, 30]',
            'Settings.rawValue(group, "masterEnabled")',
            'group, "delayedColorRefreshEnabled")',
            'Settings.rawValue(group, "settlementPolicy")',
            'label: "提笔后延迟刷新"',
            'label: "仅翻页刷新"',
            'label: "空闲等待时间"',
            "document.fileType === Document.Notebook",
            'return "documents/" + rmtoolNoteDocumentId + "/" + key',
            'return "documents/" + root.document.id.toString() + "/" + key',
            "if (!featureKey)",
            "rmtoolNoteReadScopedBool",
            "rmtoolNoteReadScopedDelay",
            "rmtoolNoteReadScopedPolicy",
            "rmtoolSetNoteDocumentDelayEnabled",
            "rmtoolSetNoteDocumentDelaySeconds",
            "rmtoolSetNoteDocumentSettlementPolicy",
            "Component.onCompleted: root.rmtoolReadNoteSettings()",
            "function rmtoolNoteRefreshInterval()",
            "function rmtoolNoteSettlementPolicy()",
            "function rmtoolNoteCanDeferStroke()",
            "const documentFeature = featureKey",
            "const documentSeconds = delayKey",
            "documentViewTools.isWritingTool(documentViewTools.activePen.tool)",
            "property rect rmtoolPendingDirtyRect",
            "property bool rmtoolHasPendingDirty",
            "property bool rmtoolAwaitingStockFinalize",
            "function rmtoolQueueDirty(dirtyRect)",
            "function rmtoolSettlePending()",
            "function rmtoolSubmitPending()",
            "function rmtoolNoteSettingsChanged()",
            "return 1000",
            "return seconds * 1000 - 1000",
            "interval: 1000",
            'rmtoolNoteSettlementPolicy() === "page"',
            "viewportUpdateTimer.stop()",
            "viewportUpdateTimer.restart()",
            "root.viewport?.markDirty(rmtoolPendingDirtyRect)",
            "inputSurface.clearFramebuffer()",
            "REBUILD close",
            "REBUILD goToPageId",
            "REBUILD onInSuspendChanged",
            "strokeHandler.timeSincePenUp() < 100",
        ):
            self.assertIn(marker, source)
        for forbidden in ("60000", "Infinity", "JSON", "daemon", "restart xochitl"):
            self.assertNotIn(forbidden, source)
        for obsolete in (
            "REBUILD onActivePenChanged",
            "rmtoolSettlePendingForToolChange",
            "rmtoolSchedulePenUpSettlement",
            "onCompletedStroke: {",
            "Qt.callLater(function()",
            "rmtoolNoteToolChangeToggle",
            "rmtoolSetNoteDocumentSettleOnToolChange",
            "function rmtoolNoteSettleOnToolChange()",
            "settleOnPenUp",
            "settleOnToolChange",
            "rmtoolNotePenUpToggle",
            "rmtoolNoteDocumentSettleOnPenUp",
            "RMTOOL-NOTE",
            "└",
        ):
            self.assertNotIn(obsolete, source)
        self.assertNotIn('iconSource: "qrc:/ark/icons/ebook"', source)
        menu = source[
            source.index("AFFECT /qt/qml/xofm/libs/toolbar/qml/SettingsMenu.qml") :
            source.index("AFFECT /qml/device/view/documentview/DocumentView.qml")
        ]
        self.assertIn("rmtoolNoteEnhancementsAvailable", menu)
        self.assertNotIn("Document.Pdf", menu)
        self.assertNotIn("Document.Ebook", menu)
        self.assertEqual(menu.count("enabled: checked"), 2)
        page_toggle = menu[
            menu.index('objectName: "rmtoolNotePageOnlyToggle"') :
            menu.index('objectName: "rmtoolNoteIdleToggle"')
        ]
        idle_toggle = menu[
            menu.index('objectName: "rmtoolNoteIdleToggle"') :
            menu.index("Component {\n                id: rmtoolNoteDelayOptions")
        ]
        for toggle in (page_toggle, idle_toggle):
            self.assertIn("onPressed:", toggle)
            self.assertNotIn("onClicked:", toggle)
        self.assertIn(
            'view.rmtoolSetNoteDocumentSettlementPolicy("page")', menu
        )
        self.assertIn(
            'view.rmtoolSetNoteDocumentSettlementPolicy("idle")', menu
        )
        self.assertIn(
            '?.rmtoolNoteDocumentSettlementPolicy !== "page"', menu
        )
        self.assertLess(
            menu.index('objectName: "rmtoolNotePageOnlyToggle"'),
            menu.index('objectName: "rmtoolNoteIdleToggle"'),
        )
        self.assertLess(
            menu.index('objectName: "rmtoolNoteIdleToggle"'),
            menu.index('label: "空闲等待时间"'),
        )
        settings_page = source[
            source.index("Component {\n                id: rmtoolNoteEnhancements") :
            source.index("AFFECT /qt/qml/xofm/libs/toolbar/qml/SettingsMenu.qml")
        ]
        self.assertIn(
            '&& rmtoolNotePage.settlementPolicy === "idle"', settings_page
        )
        self.assertIn(
            '&& rmtoolNotePage.settlementPolicy === "page"', settings_page
        )
        self.assertIn('rmtoolNotePage.writePolicy(\n                                            "page")', settings_page)
        self.assertIn('rmtoolNotePage.writePolicy(\n                                            "idle")', settings_page)
        self.assertLess(
            settings_page.index('label: "仅翻页刷新"'),
            settings_page.index('label: "提笔后延迟刷新"'),
        )
        self.assertLess(
            settings_page.index('label: "提笔后延迟刷新"'),
            settings_page.index('label: "空闲等待时间"'),
        )
        scene = source[source.index("AFFECT /qml/device/view/documentview/DeviceSceneView.qml") :]
        self.assertIn("root.document.fileType !== Document.Notebook", scene)
        self.assertNotIn("SettingsMenu", scene)
        rebuild = scene[
            scene.index("REBUILD markDirtyAndRestart") : scene.index(
                "END REBUILD", scene.index("REBUILD markDirtyAndRestart")
            )
        ]
        queue_start = rebuild.index("viewportUpdateTimer.rmtoolQueueDirty(dirtyRect)")
        queued_return = rebuild.index("return", queue_start)
        fallback_submit = rebuild.index(
            "viewportUpdateTimer.rmtoolSubmitPending()", queued_return
        )
        self.assertLess(queue_start, queued_return)
        self.assertLess(queued_return, fallback_submit)
        self.assertEqual(rebuild.count("viewportUpdateTimer.rmtoolQueueDirty(dirtyRect)"), 1)
        self.assertNotIn("viewportUpdateTimer.restart()", rebuild[:queue_start])
        self.assertFalse(
            any(
                int(value) < 1000
                for value in re.findall(
                    r"viewportUpdateTimer\.interval\s*=\s*(\d+)", source
                )
            )
        )
        settings_start = scene.index(
            "function rmtoolNoteSettingsChanged()",
            scene.index("function rmtoolSettlePending()"),
        )
        settings = scene[
            settings_start : scene.index("REBUILD markDirtyAndRestart", settings_start)
        ]
        settings_order = (
            "!rmtoolNoteFeatureEnabled()",
            "viewportUpdateTimer.interval = 1000",
            "rmtoolSubmitPending()",
            "viewportUpdateTimer.restart()",
            "return",
            "if (!rmtoolHasPendingDirty)",
            'rmtoolNoteSettlementPolicy() === "page"',
        )
        settings_positions = [settings.index(marker) for marker in settings_order]
        self.assertEqual(settings_positions, sorted(settings_positions))
        triggered = scene[
            scene.index("REBUILD onTriggered") : scene.index(
                "END REBUILD", scene.index("REBUILD onTriggered")
            )
        ]
        awaiting = triggered.index("if (viewportUpdateTimer.rmtoolAwaitingStockFinalize")
        clear_awaiting = triggered.index(
            "viewportUpdateTimer.rmtoolAwaitingStockFinalize = false"
        )
        pending = triggered.index("else if (viewportUpdateTimer.rmtoolHasPendingDirty)")
        guard = triggered.index("if (strokeHandler.timeSincePenUp() < 100)")
        guard_restart = triggered.index("viewportUpdateTimer.restart()", guard)
        submit = triggered.index("viewportUpdateTimer.rmtoolSubmitPending()")
        stock_interval = triggered.index("viewportUpdateTimer.interval = 1000", submit)
        submit_restart = triggered.index("viewportUpdateTimer.restart()", stock_interval)
        self.assertEqual(
            [
                awaiting,
                clear_awaiting,
                pending,
                guard,
                guard_restart,
                submit,
                stock_interval,
                submit_restart,
            ],
            sorted(
                [
                    awaiting,
                    clear_awaiting,
                    pending,
                    guard,
                    guard_restart,
                    submit,
                    stock_interval,
                    submit_restart,
                ]
            ),
        )
        self.assertRegex(
            triggered,
            re.compile(
                r"else if \(viewportUpdateTimer\.rmtoolHasPendingDirty\).*?"
                r"rmtoolNoteSettlementPolicy\(\) === \"page\".*?"
                r"viewportUpdateTimer\.stop\(\).*?return.*?"
                r"rmtoolSubmitPending\(\)",
                re.DOTALL,
            ),
        )
        self.assertEqual(
            tuple(seconds * 1000 - 1000 for seconds in (5, 10, 30)),
            (4000, 9000, 29000),
        )
        self.assertNotIn("RMTOOL-NOTE", source)
        settle_start = scene.index("function rmtoolSettlePending()")
        settle = scene[
            settle_start : scene.index(
                "function rmtoolNoteSettingsChanged()", settle_start
            )
        ]
        self.assertLess(
            settle.index("if (!rmtoolHasPendingDirty && !rmtoolAwaitingStockFinalize)"),
            settle.index("viewportUpdateTimer.stop()"),
        )

    def test_327_derivation_changes_only_settings_navigation_boundary(self):
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temporary:
            output = builder._source_for_release(
                SOURCE, "3.27.3.0", Path(temporary)
            ).read_text(encoding="utf-8")
        self.assertIn("TRAVERSE DeviceKeyboardNavigationHandler#settings", output)
        self.assertIn("rmtoolNoteSettingsRoot.highlightedIndex = page", output)
        self.assertIn("return 1000", output)
        self.assertIn('rmtoolNoteSettlementPolicy() === "page"', output)

    @unittest.skipUnless(
        SOURCE.exists()
        and READING_SOURCE.exists()
        and QMD_TOOL is not None
        and QMD_TOOL.exists()
        and QMLDIFF is not None
        and QMLDIFF.exists()
        and MATRIX_CONFIG is not None
        and MATRIX_CONFIG.exists(),
        "combined reading/note QMD matrix is not configured",
    )
    def test_coexists_with_reading_enhancements_in_either_load_order(self):
        note_builder = _load_builder()
        reading_builder = _load_reading_builder()
        targets = _targets()
        self.assertEqual(len(targets), 14)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, target in enumerate(targets):
                release = note.ALLOWED_TARGETS[
                    (
                        target["platform"],
                        target["firmware"],
                        target.get("architecture", "aarch64"),
                        target["xochitl_sha256"],
                    )
                ][0]
                hashtab = Path(target["qrex_root"]) / target["hashtab"]
                qrex = Path(target["qrex_root"]) / "qrex-out"
                note_source = note_builder._source_for_release(
                    SOURCE, release, root / f"note-source-{index}"
                )
                reading_source = reading_builder._source_for_release(
                    READING_SOURCE, release, root / f"reading-source-{index}"
                )
                compiled = []
                for name, source in (
                    ("note", note_source),
                    ("reading", reading_source),
                ):
                    result = subprocess.run(
                        [str(QMD_TOOL), "hash", "-hashtab", str(hashtab), str(source)],
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode,
                        0,
                        result.stderr.decode(errors="replace"),
                    )
                    path = root / f"{index}-{name}.qmd"
                    path.write_bytes(result.stdout)
                    compiled.append(path)

                for order in (compiled, list(reversed(compiled))):
                    compatibility = subprocess.run(
                        [str(QMLDIFF), "check-compatibility", str(hashtab), *map(str, order)],
                        capture_output=True,
                        check=False,
                    )
                    output = compatibility.stdout + compatibility.stderr
                    self.assertEqual(
                        compatibility.returncode,
                        0,
                        output.decode(errors="replace"),
                    )
                    replay = root / f"replay-{index}-{'-'.join(path.stem for path in order)}"
                    applied = subprocess.run(
                        [
                            str(QMLDIFF),
                            "apply-diffs",
                            "--hashtab",
                            str(hashtab),
                            "-c",
                            str(qrex),
                            str(replay),
                            *map(str, order),
                        ],
                        capture_output=True,
                        check=False,
                    )
                    output = applied.stdout + applied.stderr
                    self.assertEqual(applied.returncode, 0, output.decode(errors="replace"))
                    settings = (
                        replay / "qml/device/view/settings/Settings.qml"
                    ).read_text(encoding="utf-8")
                    settings_menu = (
                        replay / "qt/qml/xofm/libs/toolbar/qml/SettingsMenu.qml"
                    ).read_text(encoding="utf-8")
                    document = (
                        replay / "qml/device/view/documentview/DocumentView.qml"
                    ).read_text(encoding="utf-8")
                    scene = (
                        replay / "qml/device/view/documentview/DeviceSceneView.qml"
                    ).read_text(encoding="utf-8")
                    self.assertIn("rmtoolReadingEnhancementsPage", settings)
                    self.assertIn("rmtoolNoteEnhancementsPage", settings)
                    self.assertIn("rmtoolNoteDelayToggle", settings_menu)
                    self.assertIn("rmtoolNoteIdleToggle", settings_menu)
                    self.assertIn("rmtoolNotePageOnlyToggle", settings_menu)
                    self.assertIn("rmtoolCleanupToggle", settings_menu)
                    self.assertIn("rmtoolNoteDocumentView", document)
                    self.assertIn("rmtoolReadingDocumentId", document)
                    self.assertIn("rmtoolNoteDocumentSettlementPolicy", document)
                    self.assertIn("sceneView?.rmtoolSettlePendingColor()", document)
                    self.assertNotIn("rmtoolSettlePendingForToolChange", document)
                    self.assertIn(
                        "Component.onCompleted: root.rmtoolReadNoteSettings()",
                        document,
                    )
                    self.assertIn(
                        "Component.onCompleted: rmtoolReadReadingSettings()",
                        document,
                    )
                    self.assertIn('const group = "RmtoolNoteEnhancements"', scene)
                    self.assertIn("rmtoolNoteCanDeferStroke", scene)
                    self.assertIn("rmtoolQueueDirty", scene)
                    self.assertIn("rmtoolSettlePending", scene)
                    self.assertIn("rmtoolSubmitPending", scene)
                    self.assertIn("rmtoolAwaitingStockFinalize", scene)
                    self.assertIn("return seconds * 1000 - 1000", scene)
                    self.assertNotIn("RMTOOL-NOTE", scene)
                    self.assertNotIn("onCompletedStroke: {", scene)
                    self.assertNotIn("rmtoolSchedulePenUpSettlement", scene)
                    self.assertNotIn("settleOnPenUp", settings)
                    self.assertNotIn("settleOnToolChange", settings)
                    self.assertNotIn("settleOnPenUp", settings_menu)
                    self.assertNotIn("settleOnToolChange", settings_menu)
                    self.assertNotIn("settleOnPenUp", document)
                    self.assertNotIn("settleOnToolChange", document)
                    self.assertNotIn("settleOnPenUp", scene)
                    self.assertNotIn("settleOnToolChange", scene)
                    self.assertEqual(
                        scene.count("strokeHandler.timeSincePenUp() < 100"), 2
                    )
                    timer = scene[scene.index("id: viewportUpdateTimer") :]
                    dirty_start = timer.index("function markDirtyAndRestart(dirtyRect)")
                    dirty = timer[dirty_start : timer.index("onTriggered:", dirty_start)]
                    self.assertRegex(
                        dirty,
                        re.compile(
                            r"if \(viewportUpdateTimer\.rmtoolNoteCanDeferStroke\(\)\)\s*"
                            r"\{\s*viewportUpdateTimer\.rmtoolQueueDirty\(dirtyRect\).*?"
                            r"rmtoolNoteSettlementPolicy\(\) === \"page\".*?"
                            r"viewportUpdateTimer\.stop\(\).*?else.*?"
                            r"rmtoolNoteRefreshInterval\(\).*?"
                            r"viewportUpdateTimer\.restart\(\).*?return",
                            re.DOTALL,
                        ),
                    )
                    self.assertEqual(
                        dirty.count("viewportUpdateTimer.rmtoolQueueDirty(dirtyRect)"),
                        1,
                    )
                    stock_dirty = dirty.index("root.viewport.markDirty(dirtyRect)")
                    self.assertGreater(
                        dirty.find("viewportUpdateTimer.restart()", stock_dirty),
                        stock_dirty,
                    )
                    self.assertFalse(
                        any(
                            int(value) < 1000
                            for value in re.findall(
                                r"viewportUpdateTimer\.interval\s*=\s*(\d+)",
                                scene,
                            )
                        )
                    )
                    self.assertIn(
                        'rmtoolNoteSettlementPolicy() === "page"', scene
                    )
                    triggered_start = timer.index("onTriggered:")
                    triggered = timer[triggered_start:]
                    pending_submit = triggered.index(
                        "viewportUpdateTimer.rmtoolSubmitPending()"
                    )
                    stock_interval = triggered.index(
                        "viewportUpdateTimer.interval = 1000", pending_submit
                    )
                    submit_restart = triggered.index(
                        "viewportUpdateTimer.restart()", stock_interval
                    )
                    stock_finalize = triggered.index(
                        "viewportUpdateTimer.rmtoolAwaitingStockFinalize = false"
                    )
                    stock_guard = triggered.rindex(
                        "if (strokeHandler.timeSincePenUp() < 100)"
                    )
                    stock_clear = triggered.rindex("inputSurface.clearFramebuffer()")
                    stock_repaint = triggered.rindex(
                        "root.viewport?.requestRepaintDirty()"
                    )
                    self.assertLess(pending_submit, stock_interval)
                    self.assertLess(stock_interval, submit_restart)
                    self.assertLess(stock_finalize, stock_guard)
                    self.assertLess(stock_guard, stock_clear)
                    self.assertLess(stock_clear, stock_repaint)


if __name__ == "__main__":
    unittest.main()
