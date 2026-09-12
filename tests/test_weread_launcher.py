import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import _tap_page_turn as tap
import _weread_launcher as weread
import _xovi_standalone as shared


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "weread-launcher" / "manifest.json"
QMD = ROOT / "weread-launcher" / "qmd-src" / "weread-launcher-3.28.qmd"
NATIVE = ROOT / "weread-launcher" / "native" / "main.cpp"
HEADER = ROOT / "weread-launcher" / "native" / "WeReadLauncher.h"
SHIM = ROOT / "weread-launcher" / "native" / "fast_refresh.cpp"
QRC = ROOT / "weread-launcher" / "native" / "weread_assets.qrc"


class WeReadLauncherTests(unittest.TestCase):
    def setUp(self):
        self.catalog = weread.parse_manifest(
            MANIFEST.read_bytes(), require_local_match=False
        )
        self.package = self.catalog[0]
        self.identity = tap.DeviceIdentity(
            self.package.firmware,
            self.package.platform,
            self.package.architecture,
            self.package.xochitl_sha256,
        )
        self.runtime, self.feature = weread._shared_specs(self.package)

    def test_manifest_is_exact_172_color_device_matrix_with_two_mirrors(self):
        self.assertEqual(len(self.catalog), 2)
        self.assertEqual({item.platform for item in self.catalog}, {"ferrari", "chiappa"})
        self.assertTrue(all(item.release_version == "3.28.0.172" for item in self.catalog))
        self.assertTrue(all(item.offline_verified for item in self.catalog))
        verification = {item.platform: item.device_verified for item in self.catalog}
        self.assertEqual(verification, {"chiappa": True, "ferrari": False})
        manifest_text = MANIFEST.read_text(encoding="utf-8")
        self.assertNotIn("revision-2", manifest_text)
        self.assertNotIn("revision-3", manifest_text)
        self.assertNotIn("revision-4", manifest_text)
        for package in self.catalog:
            self.assertEqual(
                set(package.urls),
                {f"{base}/{package.asset}" for base in weread.REMOTE_BASE_URLS},
            )
            self.assertEqual({item.path for item in package.files}, weread._PAYLOAD_PATHS)

    def test_manifest_rejects_non_object_and_non_boolean_verification(self):
        with self.assertRaises(RuntimeError):
            weread.parse_manifest(b"[]", require_local_match=False)
        document = json.loads(MANIFEST.read_text(encoding="utf-8"))
        document["packages"][0]["offline_verified"] = 1
        with self.assertRaises(RuntimeError):
            weread.parse_manifest(
                json.dumps(document).encode(), require_local_match=False
            )

    def test_bridge_is_a_strict_xovi_extension_not_a_process_preload(self):
        self.assertEqual(self.feature.preload_paths, ())
        self.assertEqual(
            self.feature.strict_metadata_paths,
            (
                weread.BRIDGE_PAYLOAD_PATH,
                weread.SHIM_PAYLOAD_PATH,
            ),
        )

    def test_device_page_has_three_modes_and_separate_refresh_memory(self):
        source = QMD.read_text(encoding="utf-8")
        self.assertEqual(source.count('objectName: "rmtoolWeReadSidebarItem"'), 1)
        self.assertIn('readonly property bool fastModeAvailable: true', source)
        self.assertIn('Settings.setRawValue(group, "mode"', source)
        self.assertIn('value === "fast" ? "monoFast" : "normal"', source)
        self.assertIn('Settings.setRawValue(group, "colorRefreshPages"', source)
        self.assertIn('Settings.setRawValue(group, "monoRefreshPages"', source)
        self.assertIn("rmtoolWeReadBridge.launch(nativeMode(), refreshPages)", source)
        for label in ('text: "普通"', 'text: "彩色快刷"', 'text: "黑白快刷"'):
            self.assertEqual(source.count(label), 1)
        self.assertIn("保留彩色，交互更跟手；可能积累残影。", source)
        self.assertIn("牺牲彩色，黑白显示更稳定、彩色残影更少。", source)
        self.assertEqual(source.count("手写笔保持最低延迟，可定期完整刷新。"), 2)
        self.assertNotIn("极速黑白", source)
        self.assertNotIn("黑白极速", source)
        for pages in (5, 10, 20, 30, 0):
            self.assertIn(f"setRefreshPages({pages})", source)
        self.assertIn('visible: rmtoolWeReadPage.mode !== "normal"', source)
        self.assertIn('iconSource: "qrc:/rmtool/wereader.png"', source)
        self.assertNotIn("ArkControls.TextLabel", source)
        self.assertNotIn("rmtoolIconBasePadding", source)
        self.assertNotIn("Image {", source)
        self.assertNotIn("contentItem:", source)
        self.assertEqual(source.count("rmtoolWeReadBridge.unavailableReason()"), 1)
        self.assertNotIn("rmtoolWeReadBridge.available()", source)
        for forbidden in ("executeCommand", "systemctl", "LD_PRELOAD"):
            self.assertNotIn(forbidden, source)

    def test_icon_is_a_transparent_black_ark_mask(self):
        from PIL import Image

        self.assertIn(
            '<file alias="wereader.png">../assets/wereader.png</file>',
            QRC.read_text(encoding="utf-8"),
        )
        image = Image.open(ROOT / "weread-launcher" / "assets" / "wereader.png")
        self.assertEqual(image.size, (48, 48))
        self.assertEqual(image.mode, "RGBA")
        pixels = tuple(image.get_flattened_data())
        self.assertTrue(any(alpha == 0 for _, _, _, alpha in pixels))
        self.assertTrue(any(alpha > 0 for _, _, _, alpha in pixels))
        self.assertTrue(all(red == green == blue and red < 128
                            for red, green, blue, alpha in pixels if alpha > 0))

    def test_native_api_has_no_caller_supplied_program_or_arguments(self):
        header = HEADER.read_text(encoding="utf-8")
        source = NATIVE.read_text(encoding="utf-8")
        self.assertIn("Q_INVOKABLE bool launch(int mode, int refreshPages);", header)
        self.assertIn("setProgram(QString::fromUtf8(kLauncher))", source)
        self.assertIn('environment.remove(QStringLiteral("LD_PRELOAD"))', source)
        self.assertIn("90-rmtool-fast.conf", source)
        self.assertIn("Environment=LD_PRELOAD=", source)
        self.assertIn("Environment=RMTOOL_WEREAD_MODE=%d", source)
        self.assertIn("Environment=RMTOOL_WEREAD_REFRESH_PAGES=%d", source)
        self.assertIn("mode != kModeNormal && mode != kModeMono", source)
        self.assertIn("!supportedRefreshPages(refreshPages)", source)
        self.assertIn("mode == kModeNormal && refreshPages != 0", source)
        self.assertIn("kStartupPending", source)
        self.assertIn("S_ISREG", source)
        self.assertIn("metadata.st_size != 0", source)
        self.assertIn("qInitResources_weread_assets();", source)
        self.assertNotIn("Q_INIT_RESOURCE", source)
        self.assertNotIn("QDir", source)
        self.assertNotIn("QSaveFile", source)
        for path, digest in weread.OFFICIAL_FILES.items():
            self.assertIn(path, source)
            self.assertIn(digest, source)

    def test_fast_shim_forces_allowlisted_color_or_mono_modes(self):
        source = SHIM.read_text(encoding="utf-8")
        self.assertIn("constexpr int kModePen = 0;", source)
        self.assertIn("constexpr int kModeMono = 1;", source)
        self.assertIn("constexpr int kModeAnimation = 2;", source)
        self.assertIn('std::strcmp(mode, "2") == 0', source)
        self.assertIn("RTLD_NEXT", source)
        self.assertIn("EPScreenModeItemC1EP10QQuickItem", source)
        self.assertIn("EPScreenModeItem7setMode", source)
        self.assertIn("EPScreenModeMap16setModeForRegion", source)
        self.assertIn("configuration().mode", source)
        self.assertEqual(source.count("requestedMode == kModePen"), 2)
        self.assertIn("effectiveMode = requestedMode", source)
        self.assertIn("(self, region, effectiveMode)", source)
        constructor = source[
            source.index('extern "C" void rmtoolConstructScreenModeItem(void *self, void *parent)\n{'):
            source.index('extern "C" void rmtoolSetModeForRegion', source.index('extern "C" void rmtoolConstructScreenModeItem(void *self, void *parent)\n{'))
        ]
        self.assertNotIn("resolve<SetMode>", constructor)
        self.assertNotIn("ghostControl", source)
        self.assertNotIn("swapBuffers", source)

    def test_fast_shim_counts_verified_page_boundaries(self):
        source = SHIM.read_text(encoding="utf-8")
        self.assertIn("#include <time.h>", source)
        self.assertIn("clock_gettime(CLOCK_MONOTONIC, &now)", source)
        self.assertNotIn("#include <chrono>", source)
        self.assertNotIn("std::chrono", source)
        self.assertIn("constexpr int kRouteChangedSignal = 0;", source)
        self.assertIn("constexpr int kPaginationSignal = 12;", source)
        self.assertIn("constexpr unsigned long long kDuplicateWindowMs = 400;", source)
        self.assertIn("_ZNK7QRegion12boundingRectEv", source)
        self.assertIn("pageState = {};", source)
        self.assertIn("pageState.paginationPending = true;", source)
        self.assertIn("if (!pageState.initialized)", source)
        self.assertIn("now - pageState.lastCandidateMs <= kDuplicateWindowMs", source)
        self.assertIn("if (pageState.pageCount < interval)", source)
        self.assertIn("pageState.pageCount = 0;", source)
        self.assertIn("requestedMode == kModeContent", source)
        self.assertIn("preserveFullRefresh(boundingRect(region))", source)
        self.assertLess(
            source.index("else if (!sameRect(pageState.fullScreen, bounds))"),
            source.index("pageState.paginationPending = false;"),
        )

    def test_fast_shim_prioritizes_pen_signals_with_bounded_atomic_tail(self):
        source = SHIM.read_text(encoding="utf-8")
        for signal, index in (("Down", 4), ("Move", 5), ("Up", 6)):
            self.assertIn(f"constexpr int kPen{signal}Signal = {index};", source)
        self.assertIn("constexpr unsigned long long kPenTailMs = 50;", source)
        self.assertIn(
            "std::atomic<unsigned long long> penDeadlineMs{0};", source
        )
        self.assertIn("penDeadlineMs.compare_exchange_weak(", source)
        self.assertIn("std::memory_order_release", source)
        self.assertIn("penDeadlineMs.load(std::memory_order_acquire)", source)
        self.assertIn("now < penDeadlineMs.load", source)
        self.assertIn('std::strcmp(name, "PenInput") == 0', source)
        self.assertIn("observePenSignal(localSignalIndex);", source)
        self.assertIn(
            "requestedMode == kModePen || penWindowActive()", source
        )

        region_hook = source[
            source.index(
                'extern "C" void rmtoolSetModeForRegion(void *self, const void *region,\n'
                "                                        int requestedMode)\n{"
            ):
            source.index('extern "C" void rmtoolActivate', source.index(
                'extern "C" void rmtoolSetModeForRegion(void *self, const void *region,\n'
                "                                        int requestedMode)\n{"
            ))
        ]
        self.assertLess(
            region_hook.index("preserveFullRefresh(boundingRect(region))"),
            region_hook.index("if (penWindowActive())"),
        )
        self.assertIn("effectiveMode = kModePen;", region_hook)
        self.assertEqual(region_hook.count("preserveFullRefresh("), 1)

    def test_fast_shim_has_only_one_startup_configuration_log(self):
        source = SHIM.read_text(encoding="utf-8")
        self.assertIn("QMetaObject8activateEP7QObjectPKS_iPPv", source)
        self.assertIn("QMetaObject9classNameEv", source)
        self.assertIn('std::strcmp(name, "AppController") == 0', source)
        self.assertEqual(source.count("std::fprintf"), 2)
        self.assertIn("[rmtool-weread-fast] mode=%d refresh_pages=%u", source)
        self.assertNotIn("-diag]", source)
        self.assertIn(
            "activate(sender, metaObject, localSignalIndex, arguments);", source
        )
        self.assertNotIn("arguments[", source)

    def test_revisions_2_through_4_are_exact_shared_predecessors(self):
        predecessors = weread._known_shared_predecessor_specs(
            self.package, self.feature
        )
        self.assertEqual(
            tuple(reason for reason, _predecessor in predecessors),
            ("package-revision-2", "package-revision-3", "package-revision-4"),
        )
        revision_3 = dict(predecessors)["package-revision-3"]
        self.assertNotEqual(revision_3, self.feature)
        self.assertEqual(revision_3.package_id, self.feature.package_id)
        self.assertNotIn(self.package.sha256, self.feature.package_id)
        self.assertEqual(
            {item.archive_path for item in revision_3.extra_files},
            {weread.BRIDGE_PAYLOAD_PATH, weread.SHIM_PAYLOAD_PATH},
        )
        self.assertEqual(
            revision_3.sha256,
            "9bc9f761a2a15c10547912c48b7175c19ed17ab00463e0c6e926633c550dccc7",
        )
        self.assertEqual(revision_3.size, 19360)
        self.assertEqual(
            {item.archive_path: (item.sha256, item.size)
             for item in revision_3.extra_files},
            weread._REVISION_3_EXTRA_FILES,
        )
        self.assertEqual(
            weread._REVISION_3_EXTRA_FILES[weread.BRIDGE_PAYLOAD_PATH][1],
            1925400,
        )
        self.assertEqual(
            weread._REVISION_3_EXTRA_FILES[weread.SHIM_PAYLOAD_PATH][1],
            209952,
        )
        revision_4 = dict(predecessors)["package-revision-4"]
        self.assertNotEqual(revision_4, self.feature)
        self.assertEqual(revision_4.package_id, self.feature.package_id)
        self.assertEqual(
            revision_4.sha256,
            "f0f045f21466aadaa79cd044a50f43d16eea0b63d8ccaed99c8ee4e0b019b59d",
        )
        self.assertEqual(revision_4.size, 19498)
        self.assertEqual(
            {item.archive_path: (item.sha256, item.size)
             for item in revision_4.extra_files},
            weread._REVISION_4_EXTRA_FILES,
        )
        self.assertEqual(
            weread._REVISION_4_EXTRA_FILES[weread.BRIDGE_PAYLOAD_PATH],
            (
                "0d0a834f359eed7aad758670024391606e5613f5e42ab2f758bb7bbc058efe70",
                1925400,
            ),
        )
        self.assertEqual(
            weread._REVISION_4_EXTRA_FILES[weread.SHIM_PAYLOAD_PATH],
            (
                "a01fdfc9fabcc89602861d012592addcb98571874484cf62de9b4f1af2d66044",
                209888,
            ),
        )

    def test_revision_inspection_still_rejects_forged_package_identity(self):
        predecessor = weread._known_shared_predecessor_specs(
            self.package, self.feature
        )[0][1]
        expected = shared.SharedInspection({}, False, False)
        with patch.object(
            shared,
            "inspect_shared",
            side_effect=(RuntimeError("current mismatch"), expected),
        ):
            inspection, installed, selected = shared.inspect_shared_revisions(
                Mock(),
                self.runtime,
                {weread.FEATURE_ID: self.feature},
                {weread.FEATURE_ID: (("package-revision-2", predecessor),)},
            )
        self.assertIs(inspection, expected)
        self.assertIs(installed[weread.FEATURE_ID], predecessor)
        self.assertEqual(selected, {weread.FEATURE_ID: "package-revision-2"})

        forged = replace(predecessor, package_id="forged-package")
        with self.assertRaisesRegex(RuntimeError, "前代功能规格无效"):
            shared.inspect_shared_revisions(
                Mock(),
                self.runtime,
                {weread.FEATURE_ID: self.feature},
                {weread.FEATURE_ID: (("forged", forged),)},
            )

    def test_shared_inspection_registers_known_revisions_for_self(self):
        with patch.object(tap, "_reading_enhancement_revisions", return_value={}):
            revisions = weread._peer_revisions(
                self.identity, {weread.FEATURE_ID: self.feature}
            )
        self.assertEqual(
            tuple(reason for reason, _feature in revisions[weread.FEATURE_ID]),
            ("package-revision-2", "package-revision-3", "package-revision-4"),
        )

    def test_missing_official_install_disables_install_and_launch(self):
        client = Mock()
        client.file_exists.return_value = False
        with (
            patch.object(tap, "get_device_identity", return_value=self.identity),
            patch.object(
                weread,
                "_trusted_context",
                return_value=(
                    self.runtime,
                    {weread.FEATURE_ID: self.feature},
                    (),
                    self.feature,
                ),
            ) as trusted_context,
            patch.object(shared, "has_shared_artifacts", return_value=False),
        ):
            status = weread.get_status(client, self.catalog)
        self.assertEqual(status.state, weread.WeReadLauncherState.NOT_INSTALLED)
        self.assertFalse(status.prerequisite_available)
        trusted_context.assert_called_once()

        with (
            patch.object(tap, "get_device_identity", return_value=self.identity),
            patch.object(weread, "_trusted_catalog", return_value=self.catalog),
            patch.object(tap, "_preflight_device") as preflight,
            self.assertRaisesRegex(RuntimeError, "官方微信读书 v1.0.0"),
        ):
            weread.install(client, self.package, Path("unused.tar.gz"))
        preflight.assert_not_called()

    def test_status_exposes_exact_known_revisions_as_migration_available(self):
        for reason, predecessor in weread._known_shared_predecessor_specs(
            self.package, self.feature
        ):
            with self.subTest(reason=reason):
                inspection = shared.SharedInspection(
                    {
                        weread.FEATURE_ID: shared.SharedFeatureState(
                            predecessor, True, "old-process"
                        )
                    },
                    True,
                    True,
                )
                with (
                    patch.object(tap, "get_device_identity", return_value=self.identity),
                    patch.object(weread, "_official_install_error", return_value=""),
                    patch.object(
                        weread,
                        "_trusted_context",
                        return_value=(
                            self.runtime,
                            {weread.FEATURE_ID: self.feature},
                            (),
                            self.feature,
                        ),
                    ),
                    patch.object(shared, "has_shared_artifacts", return_value=True),
                    patch.object(
                        weread,
                        "_inspect_shared",
                        return_value=(
                            inspection,
                            {weread.FEATURE_ID: predecessor},
                            {weread.FEATURE_ID: reason},
                        ),
                    ),
                    patch.object(tap, "_xochitl_process_token") as process_token,
                ):
                    status = weread.get_status(Mock(), self.catalog)
                self.assertEqual(
                    status.state, weread.WeReadLauncherState.MIGRATION_AVAILABLE
                )
                self.assertEqual(status.detail, "旧版文件身份已精确验证，可安全更新。")
                self.assertTrue(status.recovery_available)
                process_token.assert_not_called()

    def test_status_keeps_unregistered_revision_broken(self):
        unknown = replace(self.feature, sha256="0" * 64)
        inspection = shared.SharedInspection(
            {
                weread.FEATURE_ID: shared.SharedFeatureState(
                    unknown, True, "unknown-process"
                )
            },
            True,
            True,
        )
        with (
            patch.object(tap, "get_device_identity", return_value=self.identity),
            patch.object(weread, "_official_install_error", return_value=""),
            patch.object(
                weread,
                "_trusted_context",
                return_value=(
                    self.runtime,
                    {weread.FEATURE_ID: self.feature},
                    (),
                    self.feature,
                ),
            ),
            patch.object(shared, "has_shared_artifacts", return_value=True),
            patch.object(
                weread,
                "_inspect_shared",
                return_value=(inspection, {weread.FEATURE_ID: unknown}, {}),
            ),
        ):
            status = weread.get_status(Mock(), self.catalog)
        self.assertEqual(status.state, weread.WeReadLauncherState.BROKEN)
        self.assertIn("信任清单不一致", status.detail)

    def test_install_preserves_verified_shared_peer(self):
        client = Mock()
        current_peer = Mock(feature_id="reading-enhancements")
        installed_peer = Mock(feature_id="reading-enhancements")
        trusted = {current_peer.feature_id: current_peer, weread.FEATURE_ID: self.feature}
        installed_trusted = {
            installed_peer.feature_id: installed_peer,
            weread.FEATURE_ID: self.feature,
        }
        inspection = shared.SharedInspection(
            {
                installed_peer.feature_id: shared.SharedFeatureState(
                    installed_peer, True, "token"
                )
            },
            True,
            True,
        )
        final = weread.WeReadLauncherStatus(
            weread.WeReadLauncherState.ENABLE_PENDING_REBOOT,
            self.identity,
            self.package,
            self.catalog,
        )
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(tap, "get_device_identity", return_value=self.identity),
            patch.object(weread, "_trusted_catalog", return_value=self.catalog),
            patch.object(weread, "_official_install_error", return_value=""),
            patch.object(tap, "_vellum_runtime_present", return_value=False),
            patch.object(tap, "_preflight_device"),
            patch.object(
                weread,
                "_trusted_context",
                return_value=(self.runtime, trusted, (), self.feature),
            ),
            patch.object(shared, "has_shared_artifacts", return_value=True),
            patch.object(
                weread,
                "_inspect_shared",
                return_value=(
                    inspection,
                    installed_trusted,
                    {installed_peer.feature_id: "known-release"},
                ),
            ),
            patch.object(
                weread, "extract_verified_package", return_value=Path(temporary)
            ),
            patch.object(shared, "enable_shared") as enable,
            patch.object(weread, "get_status", return_value=final),
        ):
            result = weread.install(client, self.package, Path("package.tar.gz"))
        self.assertIs(result, final)
        enable.assert_called_once_with(
            client,
            self.runtime,
            self.feature,
            Path(temporary),
            installed_trusted,
            (),
        )

    def test_shared_inspection_accepts_registered_reading_revision(self):
        client = Mock()
        trusted = {weread.FEATURE_ID: self.feature}
        revisions = {"reading-enhancements": (("known-release", Mock()),)}
        expected = (Mock(), trusted, {"reading-enhancements": "known-release"})
        with (
            patch.object(weread, "_peer_revisions", return_value=revisions) as revision_loader,
            patch.object(
                shared, "inspect_shared_revisions", return_value=expected
            ) as inspect,
        ):
            result = weread._inspect_shared(
                client,
                self.runtime,
                trusted,
                self.identity,
                check_lower=True,
            )
        self.assertIs(result, expected)
        revision_loader.assert_called_once_with(self.identity, trusted)
        inspect.assert_called_once_with(
            client,
            self.runtime,
            trusted,
            revisions,
            check_lower=True,
        )

    def test_disable_preserves_verified_shared_peer(self):
        client = Mock()
        peer = Mock(feature_id="reading-enhancements")
        trusted = {peer.feature_id: peer, weread.FEATURE_ID: self.feature}
        states = {
            peer.feature_id: shared.SharedFeatureState(peer, True, "token"),
            weread.FEATURE_ID: shared.SharedFeatureState(self.feature, True, "token"),
        }
        status = weread.WeReadLauncherStatus(
            weread.WeReadLauncherState.ENABLED,
            self.identity,
            self.package,
            self.catalog,
        )
        inspection = shared.SharedInspection(states, True, True)
        with (
            patch.object(weread, "get_status", side_effect=(status, status)),
            patch.object(
                weread,
                "_trusted_context",
                return_value=(self.runtime, trusted, (), self.feature),
            ),
            patch.object(
                weread,
                "_inspect_shared",
                return_value=(inspection, trusted, {}),
            ),
            patch.object(shared, "disable_shared") as disable,
        ):
            weread.disable(client, self.catalog)
        disable.assert_called_once_with(
            client,
            self.runtime,
            weread.FEATURE_ID,
            {peer.feature_id: peer, weread.FEATURE_ID: self.feature},
            replacement_spec=None,
        )


if __name__ == "__main__":
    unittest.main()
