import importlib.util
import os
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import _native_chinese as native
import _reading_enhancements as reading
import _xovi_standalone as shared
from tools import build_beta_329 as builder


class Beta329Tests(unittest.TestCase):
    def test_tap_calls_new_signals_and_preserves_links_and_inactive_regions(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt5 import QtQml, QtWidgets

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        reading_builder = builder.load(builder.ROOT / 'reading-enhancements/build_assets.py')
        for feature in ('tap-page-turn', 'reading-enhancements'):
            source = (builder.ROOT / feature / f'qmd-src/{feature}-3.28.qmd').read_text(encoding='utf-8')
            with self.subTest(feature=feature):
                for release in ('3.27.3.0', '3.28.0.172'):
                    self.assertEqual(reading_builder._tap_source_for_release(source, release), source)
                adapted = reading_builder._tap_source_for_release(source, builder.RELEASE)
                self.assertEqual(reading_builder._tap_source_for_release(source, '3.29.0.153'), adapted)
                with tempfile.TemporaryDirectory() as temporary:
                    if feature == 'reading-enhancements':
                        generated = reading_builder._source_for_target(
                            builder.ROOT / feature / f'qmd-src/{feature}-3.28.qmd',
                            builder.FIRMWARE, builder.RELEASE, 'ferrari', 'aarch64',
                            builder.TARGETS['ferrari'][2], Path(temporary))
                        adapted = generated.read_text(encoding='utf-8')
                match = re.search(r'const rmtoolDirection =.*?(?=\n\s*}\s*\n\s*END REBUILD)', adapted, re.S)
                self.assertIsNotNone(match)
                engine = QtQml.QJSEngine()
                result = engine.evaluate('''
                    var direction, linkPressed, events;
                    var console = {warn: function() {}};
                    var touchArea = {rmtoolTapPageDirection: function(pos) {return direction;}};
                    var view = {
                        leftPageRequested: function() {events.push("previous");},
                        rightPageRequested: function() {events.push("next");}
                    };
                    function reset() {events.push("reset");}
                    function click(pos) {
                ''' + match.group() + '\nevents.push("stock");\n}')
                self.assertFalse(result.isError(), result.toString())
                for direction, linked, expected in (
                    (-1, False, ['reset', 'previous']), (1, False, ['reset', 'next']),
                    (0, False, ['stock']), (-1, True, ['stock']), (1, True, ['stock']),
                ):
                    result = engine.evaluate(f'direction={direction}; linkPressed={str(linked).lower()}; events=[]; click({{}}); events;')
                    self.assertFalse(result.isError(), result.toString())
                    self.assertEqual(result.toVariant(), expected)
                with self.assertRaisesRegex(RuntimeError, 'tap source'):
                    reading_builder._tap_source_for_release(source.replace('view.moveForward()', ''), builder.RELEASE)
        self.assertIsNotNone(app)

    def test_tap_navigation_contract_rejects_missing_or_stale_interfaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / 'qml/device/view/documentview'
            directory.mkdir(parents=True)
            files = {
                'SceneViewGestures.qml': 'view.leftPageRequested(); view.rightPageRequested();',
                'DeviceSceneView.qml': 'signal leftPageRequested\nsignal rightPageRequested',
                'DocumentView.qml': '''
                    function moveForward() {} function moveBackward() {}
                    onRightPageRequested: { root.moveForward(); }
                    onLeftPageRequested: { root.moveBackward(); }
                ''',
            }
            for name, text in files.items():
                (directory / name).write_text(text)
            gestures = files['SceneViewGestures.qml'] * 2
            builder.validate_tap_navigation(root, gestures)
            with self.assertRaisesRegex(RuntimeError, 'navigation signal'):
                builder.validate_tap_navigation(root, files['SceneViewGestures.qml'])
            for stale in ('view.moveForward()', 'view.moveBackward()'):
                with self.assertRaisesRegex(RuntimeError, 'navigation signal'):
                    builder.validate_tap_navigation(root, gestures + stale)
            for name, text in files.items():
                (directory / name).write_text('')
                with self.assertRaisesRegex(RuntimeError, 'navigation signal'):
                    builder.validate_tap_navigation(root, gestures)
                (directory / name).write_text(text)

    def test_beta_highlighter_matches_reviewed_binary_layout(self):
        libraries = Path('E:/remarkable/firmware-cache/tools/python')
        if not libraries.is_dir():
            self.skipTest('Firmware analysis dependencies are not cached')
        sys.path.insert(0, str(libraries))
        try:
            for release, platform, address in [(builder.RELEASE, 'chiappa', '0xcadd70'),
                                               (builder.RELEASE, 'ferrari', '0xcc8db0'),
                                               ('3.29.0.153', 'chiappa', '0xcaddb0'),
                                               ('3.29.0.153', 'ferrari', '0xcc8db0')]:
                path = Path('E:/remarkable/firmware-cache/official-beta') / release / platform / 'extracted/selected-rootfs/usr/bin/xochitl'
                if not path.is_file():
                    self.skipTest('Exact beta xochitl is not cached')
                result = builder.highlight_abi_check(path, platform, release)
                self.assertEqual(result['address'], address)
                self.assertEqual(result['xochitl_sha256'], builder.digest(path.read_bytes()))
                other = 'ferrari' if platform == 'chiappa' else 'chiappa'
                with self.assertRaisesRegex(RuntimeError, 'identity'):
                    builder.highlight_abi_check(path, other, release)
                if release == '3.29.0.153':
                    with self.assertRaisesRegex(RuntimeError, 'identity'):
                        builder.highlight_abi_check(path, platform, builder.RELEASE)
        finally:
            sys.path.remove(str(libraries))

    def test_exact_beta_packages_are_offline_only_and_have_matching_carriers(self):
        packages = [p for p in native._trusted_catalog() if p.release_version == builder.RELEASE]
        self.assertEqual({p.platform for p in packages}, set(builder.TARGETS))
        for package in packages:
            with self.subTest(platform=package.platform):
                self.assertEqual(package.firmware, builder.FIRMWARE)
                self.assertEqual(package.channel, 'beta')
                self.assertTrue(package.offline_verified)
                self.assertFalse(package.device_verified)
                identity = native.tap.DeviceIdentity(package.firmware, package.platform,
                                                     package.architecture, package.xochitl_sha256)
                carrier = native._bundled_french_slot_package(identity)
                self.assertEqual(carrier.xochitl_sha256, package.xochitl_sha256)
                self.assertEqual(carrier.localized_qm_sha256, package.file(native.CATALOG_PATH).sha256)

    def test_beta_reading_has_own_highlighter_but_no_unpublished_predecessors(self):
        packages = [p for p in reading._trusted_catalog() if p.release_version == builder.RELEASE]
        self.assertEqual({p.platform for p in packages}, {'ferrari', 'chiappa'})
        for package in packages:
            payload = (builder.ROOT / 'reading-enhancements/native/rmtool-highlight-snap-3.29.so').read_bytes()
            self.assertEqual(package.file(reading.HIGHLIGHT_EXTENSION_PATH).sha256, builder.digest(payload))
            self.assertIn(reading.HIGHLIGHT_LICENSE_PATH, {p.path for p in package.files})
            _, feature = reading._shared_specs(package)
            self.assertEqual(reading._known_shared_predecessor_specs(package, feature), ())

    def test_beta_home_layout_is_rm1_only(self):
        for package in native._trusted_catalog():
            if package.release_version not in builder.TARGETS_BY_RELEASE:
                continue
            runtime, _ = native._shared_specs(package)
            layout = shared.preferred_layout(runtime)
            self.assertEqual(layout.remote_base.startswith('/home/'), package.platform == 'rm1')

    def test_qt32_translator_uses_real_32bit_qstring_signature(self):
        payload = (builder.ROOT / 'native-chinese/native-chinese-translator-armv7-qt32.so').read_bytes()
        self.assertEqual(struct.unpack_from('<H', payload, 18)[0], 40)
        self.assertIn(b'I_ZN7QStringC1EPK5QChari\0', payload)
        self.assertNotIn(b'_ZN7QStringC1EPK5QCharx', payload)
        self.assertIn('/home/root/.local/share/rmtool/xovi-standalone'.encode('utf-16le'), payload)

    def test_shared_native_builder_selects_beta_qmd_and_exact_catalog(self):
        spec = importlib.util.spec_from_file_location('native_builder', builder.ROOT / 'native-chinese/build_assets.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module._variant(builder.RELEASE), '3.29')
        self.assertEqual(module._variant('3.29.0.153'), '3.29')
        self.assertEqual(len(module._qmd_bytes()['3.29']), 2035)
        for package in native._trusted_catalog():
            if package.release_version in builder.TARGETS_BY_RELEASE:
                data = module._catalog_bytes(package, module._translation_records())
                self.assertEqual(builder.digest(data), package.file(native.CATALOG_PATH).sha256)

    def test_153_keeps_same_build_but_exact_new_binary_and_catalog(self):
        targets = builder.TARGETS_BY_RELEASE['3.29.0.153']
        self.assertEqual(set(targets), {'ferrari', 'chiappa', 'tatsu', 'rm1', 'rm2'})
        catalog = native._trusted_catalog()
        for platform, target in targets.items():
            with self.subTest(platform=platform):
                previous = next(p for p in catalog if p.platform == platform and p.release_version == builder.RELEASE)
                package = next(p for p in catalog if p.platform == platform and p.release_version == '3.29.0.153')
                self.assertEqual(previous.firmware, package.firmware)
                self.assertNotEqual(previous.xochitl_sha256, package.xochitl_sha256)
                self.assertEqual(package.xochitl_sha256, target[2])
                identity = native.tap.DeviceIdentity(package.firmware, package.platform, package.architecture, package.xochitl_sha256)
                carrier = native._bundled_french_slot_package(identity)
                self.assertEqual(carrier.release_version, '3.29.0.153')
                self.assertEqual(carrier.localized_qm_sha256, package.file(native.CATALOG_PATH).sha256)
                self.assertFalse(package.device_verified)
                beta_reading = reading.select_package(reading._trusted_catalog(), identity)
                color = platform in ('ferrari', 'chiappa')
                self.assertEqual(beta_reading is not None, color)
                self.assertEqual(reading._highlight_snap_supported(package.firmware, package.release_version,
                                 platform, package.architecture, package.xochitl_sha256), color)
                if color:
                    _, feature = reading._shared_specs(beta_reading)
                    self.assertEqual(reading._known_shared_predecessor_specs(beta_reading, feature), ())

    def test_all_five_can_migrate_core_features_to_exact_153(self):
        import _residue_migration as migration
        tap = native.tap
        for platform in builder.TARGETS_BY_RELEASE['3.29.0.153']:
            target = next(p for p in native._trusted_catalog()
                          if p.platform == platform and p.release_version == '3.29.0.153')
            current = tap.DeviceIdentity(target.firmware, platform, target.architecture, target.xochitl_sha256)
            for release in ('3.28.0.172', '3.29.0.152'):
                source = next(p for p in native._trusted_catalog() if p.platform == platform and p.release_version == release)
                old = tap.DeviceIdentity(source.firmware, platform, source.architecture, source.xochitl_sha256)
                _, trusted, _ = tap._trusted_shared_context(old)
                features = {'native-chinese', 'pinyin-input'}
                features.update({'reading-enhancements', 'note-enhancements'} if platform in ('ferrari', 'chiappa') else {'tap-page-turn'})
                states = {name: shared.SharedFeatureState(trusted[name], True, 'old') for name in features}
                with (self.subTest(platform=platform, release=release),
                      patch.object(tap, 'get_device_identity', return_value=current),
                      patch.object(shared, 'has_shared_artifacts', return_value=True),
                      patch.object(shared, 'read_shared_identity', return_value=(old.firmware, old.platform, old.architecture, old.xochitl_sha256)),
                      patch.object(shared, 'inspect_shared_firmware_residue', return_value=shared.SharedInspection(states, False, True))):
                    report = migration.inspect_residue(Mock())
                    self.assertTrue(report.migratable, report)
                    self.assertEqual({f.feature_id for f in report.features if f.target_available}, features)

    def validate_translation(self, source, translation, *, comment='', numerus=False):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            attribute = ' numerus="yes"' if numerus else ''
            prefix = f'<TS><context><name>Screen</name><message{attribute}><source>{source}</source>{comment}'
            suffix = '</message></context></TS>'
            qml = work / 'qml.ts'
            qml.write_text(prefix + suffix)
            catalog = work / 'zh.ts'
            catalog.write_text(prefix + translation + suffix)
            return builder.validate_catalog([], catalog, work, work, qml)

    def test_catalog_checks_qml_only_keys_and_plural_placeholders(self):
        self.assertEqual(self.validate_translation('%n items', '<translation><numerusform>%n items translated</numerusform></translation>', numerus=True), 1)
        with self.assertRaisesRegex(RuntimeError, 'placeholders'):
            self.validate_translation('%1 / %2', '<translation>%1</translation>')
        with self.assertRaisesRegex(RuntimeError, 'markup'):
            self.validate_translation('&lt;b&gt;Text&lt;/b&gt;', '<translation>Text</translation>')
        with self.assertRaisesRegex(RuntimeError, 'Unfinished'):
            self.validate_translation('Text', '<translation type="unfinished">Text</translation>')

    def test_catalog_requires_exact_disambiguation_and_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            text = '<TS><context><name>A</name><message><source>Same</source><comment>{}</comment><translation>Translated</translation></message></context></TS>'
            (work / 'qml.ts').write_text(text.format('noun'))
            (work / 'zh.ts').write_text(text.format('verb'))
            with self.assertRaisesRegex(RuntimeError, 'Untranslated exact'):
                builder.validate_catalog([], work / 'zh.ts', work, work, work / 'qml.ts')
            (work / 'zh.ts').write_text(text.format('noun').replace('</context>', '<message><source>Same</source><comment>noun</comment><translation>Again</translation></message></context>'))
            with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
                builder.ts_messages(work / 'zh.ts')


if __name__ == '__main__':
    unittest.main()
