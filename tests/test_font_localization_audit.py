import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import _rmkit_cn as cn
import _sleep_wallpaper as sleep
import _native_chinese as native
from test_rmkit_cn_localization import FakeSSH
from test_sleep_wallpaper import FakeSleepSSH


class FontLocalizationAuditTests(unittest.TestCase):
    def setUp(self):
        self.stock = b"stock"
        self.localized = b"localized"
        self.package = cn.TranslationPackage(
            firmware=cn.SUPPORTED_FIRMWARE,
            asset="test.qm", size=len(self.localized),
            stock_french_sha256=hashlib.sha256(self.stock).hexdigest(),
            localized_qm_sha256=hashlib.sha256(self.localized).hexdigest(),
        )

    def stale_device(self):
        return FakeSSH({
            cn.CONFIG_PATH: b"[General]\nlanguage=fr_FR\nNewSetting=keep\n",
            cn.QM_PATH: self.stock,
            cn.BACKUP_READY_PATH: b"",
            cn.BACKUP_CONFIG_PATH: b"[General]\nlanguage=en_US\nOldSetting=old\n",
            cn.BACKUP_QM_PATH: b"old firmware",
        })

    def test_stale_status_is_read_only_and_offers_recovery(self):
        ssh = self.stale_device()
        before = dict(ssh.files)
        status = cn.get_localization_status(ssh, self.package)
        self.assertEqual(ssh.files, before)
        self.assertEqual(status.state.value, "recovery_required")
        self.assertTrue(status.detail)

    def test_cloud_status_uses_exact_identity_when_backup_cannot_select_variant(self):
        ssh = self.stale_device()
        ssh.files[cn.QM_PATH] = b"historical unknown carrier"
        package = replace(self.package, platform="chiappa", xochitl_sha256="a" * 64)
        identity = native.tap.DeviceIdentity(package.firmware, package.platform, "aarch64", package.xochitl_sha256)
        before = dict(ssh.files)
        with patch.object(cn, "load_translation_catalog", return_value={package.firmware: package}), patch.object(
            native.tap, "get_device_identity", return_value=identity
        ):
            status = cn.get_cloud_localization_status(ssh, "unused")
        self.assertEqual(status.state, cn.LocalizationState.RECOVERY_REQUIRED)
        self.assertEqual(status.package, package)
        self.assertEqual(ssh.files, before)

    def test_font_failure_preserves_concurrent_user_config_and_evidence(self):
        ssh = FakeSSH({cn.FONTCONFIG_FILE: b"original"})

        def change_and_fail(*_args):
            ssh.files[cn.FONTCONFIG_FILE] = b"new external user config"
            raise RuntimeError("cache failed")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "font.ttf"
            path.write_bytes(b"font")
            with patch.object(cn, "refresh_font_cache", side_effect=change_and_fail):
                with self.assertRaisesRegex(RuntimeError, "保留事务证据"):
                    cn._install_managed_font(ssh, str(path), "Font", lock_screen_support=False)
        self.assertEqual(ssh.files[cn.FONTCONFIG_FILE], b"new external user config")
        self.assertEqual(json.loads(ssh.files[cn.FONT_MARKER_PATH])["phase"], "installing")

    def test_explicit_stale_restore_preserves_settings_and_evidence(self):
        ssh = self.stale_device()
        cn.restore_localization(ssh, self.package)
        self.assertEqual(ssh.files[cn.QM_PATH], self.stock)
        self.assertEqual(ssh.files[cn.CONFIG_PATH], b"[General]\nlanguage=en_US\nNewSetting=keep\n")
        self.assertTrue(any("recovery-" in path for path in ssh.files))

    def test_install_cannot_take_another_features_marker(self):
        target = cn.CUSTOM_FONT_PATHS[".ttf"]
        data = b"native font"
        ssh = FakeSSH({
            target: data,
            cn.FONT_MARKER_PATH: json.dumps({"path": target,
                "sha256": hashlib.sha256(data).hexdigest(), "had_fontconfig": False,
                "owner": cn.FONT_OWNER_NATIVE_CHINESE}).encode(),
        })
        before = dict(ssh.files)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "font.ttf"
            path.write_bytes(b"replacement")
            with self.assertRaisesRegex(RuntimeError, "归属|其他功能"):
                cn._install_managed_font(ssh, str(path), "Font", lock_screen_support=False)
        self.assertEqual(ssh.files, before)

    def test_restore_keeps_later_user_fontconfig(self):
        target = cn.CUSTOM_FONT_PATHS[".ttf"]
        data = b"font"
        later = cn.fontconfig_override("User", cn.USER_FONT_REPOSITORY + "/user.ttf").encode()
        ssh = FakeSSH({target: data, cn.FONTCONFIG_FILE: later,
            cn.FONT_MARKER_PATH: json.dumps({"path": target,
                "sha256": hashlib.sha256(data).hexdigest(), "had_fontconfig": False,
                "owner": cn.FONT_OWNER_RMKIT}).encode()})
        cn._remove_own_managed_font(ssh)
        self.assertEqual(ssh.files[cn.FONTCONFIG_FILE], later)

    def test_failed_enable_restores_current_not_historical_settings(self):
        ssh = self.stale_device()
        ssh.files[cn.BACKUP_QM_PATH] = self.stock
        ssh.files[cn.CONFIG_PATH] = b"[General]\nlanguage=en_US\nNewSetting=keep\n"
        before = ssh.files[cn.CONFIG_PATH]
        ssh.fail_exec_once_contains = [f"mv -f {cn.CONFIG_PATH}.tmp"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "translation.qm"
            path.write_bytes(self.localized)
            with self.assertRaises(IOError):
                cn.enable_localization(ssh, str(path), package=self.package)
        self.assertEqual(ssh.files[cn.CONFIG_PATH], before)

    def test_stale_epub_target_does_not_block_unrelated_delete(self):
        directory = cn.USER_FONT_REPOSITORY
        target = directory + "/other.ttf"
        ssh = FakeSSH({target: b"other", cn.EPUB_FONT_INDEX_PATH:
            cn._epub_font_index_data((cn.EpubFontSlot(1, directory + "/missing.ttf", "Missing"),))})
        cn.delete_user_font(ssh, directory, "other.ttf")
        self.assertNotIn(target, ssh.files)

    def test_remote_hash_does_not_download_font(self):
        ssh = FakeSSH({"/font.ttf": b"large font"})
        with patch.object(cn, "_read_bytes", side_effect=AssertionError("font downloaded")):
            self.assertEqual(cn._remote_sha256(ssh, "/font.ttf"), hashlib.sha256(b"large font").hexdigest())

    def test_failed_native_disable_does_not_change_language(self):
        package = native._trusted_catalog()[0]
        identity = native._package_identity(package)
        runtime, feature = native._shared_specs(package)
        ssh = FakeSSH({cn.CONFIG_PATH: b"[General]\nlanguage=zh_CN\nKeep=1\n"})
        before = dict(ssh.files)
        with patch.object(native.tap, "get_device_identity", return_value=identity), patch.object(
            native._xovi_standalone, "read_shared_identity", return_value=tuple(vars(identity).values())
        ), patch.object(native, "_trusted_shared_context", return_value=(runtime, {native.FEATURE_ID: feature}, ())), patch.object(
            native, "_inspect_shared_revision", return_value=(SimpleNamespace(states={native.FEATURE_ID: True}), {native.FEATURE_ID: feature}, {})
        ), patch.object(native._xovi_standalone, "_operation_lock", return_value=nullcontext()), patch.object(
            native._xovi_standalone, "_disable_shared_locked", side_effect=RuntimeError("revalidation failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "revalidation failed"):
                native.disable(ssh, (package,))
        self.assertEqual(ssh.files, before)

    def test_failed_native_uninstall_does_not_change_language(self):
        package = native._trusted_catalog()[0]
        identity = native._package_identity(package)
        runtime, feature = native._shared_specs(package)
        ssh = FakeSSH({cn.CONFIG_PATH: b"[General]\nlanguage=zh_CN\nKeep=1\n"})
        before = dict(ssh.files)
        status = native.NativeChineseStatus(native.NativeChineseState.ENABLED, identity, package, installed=True)
        with patch.object(native, "get_status", return_value=status), patch.object(
            native, "_trusted_shared_context", return_value=(runtime, {native.FEATURE_ID: feature}, ())
        ), patch.object(native, "_inspect_shared_revision", return_value=(
            SimpleNamespace(states={native.FEATURE_ID: True}), {native.FEATURE_ID: feature}, {}
        )), patch.object(native._xovi_standalone, "remove_shared_features", side_effect=RuntimeError("revalidation failed")):
            with self.assertRaisesRegex(RuntimeError, "revalidation failed"):
                native.uninstall(ssh, (package,))
        self.assertEqual(ssh.files, before)

    def test_interrupted_font_install_has_explicit_owner_bound_recovery(self):
        class LostConnection(BaseException):
            pass

        class InterruptedSSH(FakeSSH):
            def exec_checked(self, command):
                if command.startswith("fc-cache"):
                    raise LostConnection()
                return super().exec_checked(command)

        original = b"previous user config"
        ssh = InterruptedSSH({cn.FONTCONFIG_FILE: original})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "font.ttf"
            path.write_bytes(b"new font")
            with self.assertRaises(LostConnection):
                cn._install_managed_font(ssh, str(path), "New", lock_screen_support=False)
        marker = json.loads(ssh.files[cn.FONT_MARKER_PATH])
        self.assertEqual(marker["phase"], "installing")
        ssh.exec_checked = FakeSSH.exec_checked.__get__(ssh)
        before = dict(ssh.files)
        with self.assertRaisesRegex(RuntimeError, "确认后发生变化"):
            cn.recover_managed_font(ssh, expected_owner=cn.FONT_OWNER_RMKIT, expected_fingerprint="0" * 64)
        self.assertEqual(ssh.files, before)
        cn.recover_managed_font(ssh, expected_owner=cn.FONT_OWNER_RMKIT)
        self.assertEqual(ssh.files[cn.FONTCONFIG_FILE], original)
        self.assertNotIn(cn.CUSTOM_FONT_PATHS[".ttf"], ssh.files)

    def test_interrupted_font_replacement_restores_exact_previous_font_and_marker(self):
        class LostConnection(BaseException):
            pass

        data = b"replacement CJK"
        old = b"old font"
        target = cn.CUSTOM_FONT_PATHS[".ttf"]
        marker = json.dumps({"path": target, "sha256": hashlib.sha256(old).hexdigest(),
            "had_fontconfig": False, "owner": cn.FONT_OWNER_RMKIT}).encode()
        original = {target: old, cn.FONTCONFIG_FILE: b"old exact config", cn.FONT_MARKER_PATH: marker}
        ssh = FakeSSH(original, cjk_font_data=(data,))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "font.ttf"
            path.write_bytes(data)
            with patch.object(cn, "refresh_font_cache", side_effect=LostConnection):
                with self.assertRaises(LostConnection):
                    cn._install_managed_font(ssh, str(path), "New", lock_screen_support=False)
        with self.assertRaisesRegex(RuntimeError, "归属"):
            cn.recover_managed_font(ssh, expected_owner=cn.FONT_OWNER_NATIVE_CHINESE)
        cn.recover_managed_font(ssh, expected_owner=cn.FONT_OWNER_RMKIT)
        for key, value in original.items():
            self.assertEqual(ssh.files[key], value)

    def test_interrupted_system_mirror_recovers_exact_prior_bytes(self):
        class LostConnection(BaseException):
            pass

        data = b"new CJK"
        ssh = FakeSSH({cn.FONTCONFIG_FILE: b"old config"}, cjk_font_data=(data,))
        original = dict(ssh.files)
        install = cn._install_system_font_mirror

        def lose_response(*args, **kwargs):
            install(*args, **kwargs)
            raise LostConnection()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "font.ttf"
            path.write_bytes(data)
            with patch.object(cn, "_install_system_font_mirror", side_effect=lose_response):
                with self.assertRaises(LostConnection):
                    cn._install_managed_font(ssh, str(path), "New")
        cn.recover_managed_font(ssh, expected_owner=cn.FONT_OWNER_RMKIT)
        self.assertEqual(ssh.files[cn.FONTCONFIG_FILE], original[cn.FONTCONFIG_FILE])
        self.assertTrue(all(path not in ssh.files for path in cn.SYSTEM_FONT_STATE_PATHS))

    def test_interrupted_home_only_switch_recovers_prior_mirror(self):
        class LostConnection(BaseException):
            pass

        old, new = b"old CJK", b"new CJK"
        ssh = FakeSSH(cjk_font_data=(old, new))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "font.ttf"
            path.write_bytes(old)
            cn._install_managed_font(ssh, str(path), "Old")
            before = dict(ssh.files)
            path.write_bytes(new)
            write = cn._write_font_marker

            def interrupt_commit(*args, **kwargs):
                if kwargs.get("install_before") is None:
                    raise LostConnection()
                return write(*args, **kwargs)

            with patch.object(cn, "_write_font_marker", side_effect=interrupt_commit):
                with self.assertRaises(LostConnection):
                    cn._install_managed_font(ssh, str(path), "New", lock_screen_support=False)
        cn.recover_managed_font(ssh, expected_owner=cn.FONT_OWNER_RMKIT)
        for remote in (*cn.SYSTEM_FONT_STATE_PATHS, cn.FONTCONFIG_FILE, cn.FONT_MARKER_PATH,
                       cn.CUSTOM_FONT_PATHS[".ttf"]):
            self.assertEqual(ssh.files.get(remote), before.get(remote))

    def test_epub_repair_preserves_valid_entries_and_archives_original(self):
        directory = cn.USER_FONT_REPOSITORY
        present, missing = directory + "/present.ttf", directory + "/missing.ttf"
        index = cn._epub_font_index_data((cn.EpubFontSlot(1, missing, "Gone"), cn.EpubFontSlot(2, present, "Present")))
        ssh = FakeSSH({present: b"present", cn.EPUB_FONT_INDEX_PATH: index})
        identity = next(item for item in native._trusted_catalog()
                        if item.release_version == "3.29.1.0" and item.platform == "chiappa")
        with patch.object(native.tap, "get_device_identity", return_value=native._package_identity(identity)):
            status = cn.repair_epub_font_index(ssh)
        self.assertEqual(status.target_paths, (present,))
        self.assertTrue(any(value == index and ".recovery-" in path for path, value in ssh.files.items()))

    def test_status_does_not_delete_missing_backup_config_or_native_font(self):
        ssh = self.stale_device()
        del ssh.files[cn.BACKUP_CONFIG_PATH]
        target = cn.CUSTOM_FONT_PATHS[".ttf"]
        ssh.files[target] = b"native font"
        ssh.files[cn.FONT_MARKER_PATH] = json.dumps({"path": target,
            "sha256": hashlib.sha256(b"native font").hexdigest(), "had_fontconfig": False,
            "owner": cn.FONT_OWNER_NATIVE_CHINESE}).encode()
        before = dict(ssh.files)
        cn.get_localization_status(ssh, self.package)
        self.assertEqual(ssh.files, before)
        cn.restore_localization(ssh, self.package)
        self.assertEqual(ssh.files[target], b"native font")
        self.assertEqual(ssh.files[cn.CONFIG_PATH], before[cn.CONFIG_PATH])

    def test_interrupted_first_wallpaper_enable_retains_recoverable_marker(self):
        class LostConnection(BaseException):
            pass

        class InterruptedSSH(FakeSleepSSH):
            def exec_checked(self, command):
                if "mv" in command and sleep.CONFIG_PATH in command:
                    raise LostConnection()
                return super().exec_checked(command)

        ssh = InterruptedSSH({sleep.CONFIG_PATH: b"[General]\nFoo=keep\n"})
        with patch.object(sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY):
            with self.assertRaises(LostConnection):
                sleep.enable(ssh, b"image")
            self.assertIn(sleep.MARKER_PATH, ssh.files)
            ssh.exec_checked = FakeSleepSSH.exec_checked.__get__(ssh)
            sleep.recover(ssh)
        self.assertNotIn(sleep.MANAGED_IMAGE_PATH, ssh.files)
        self.assertEqual(ssh.files[sleep.CONFIG_PATH], b"[General]\nFoo=keep\n")

    def test_wallpaper_recovery_after_verified_ota_preserves_other_settings(self):
        original = b"[General]\nFoo=keep\n"
        ssh = FakeSleepSSH({sleep.CONFIG_PATH: original})
        with patch.object(sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY):
            sleep.enable(ssh, b"image")
        current = next(item for item in native._trusted_catalog()
                       if item.release_version == "3.29.1.0" and item.platform == "chiappa")
        with patch.object(sleep.tap, "get_device_identity", return_value=native._package_identity(current)):
            status = sleep.get_status(ssh)
            self.assertTrue(status.broken)
            self.assertFalse(status.supported)
            sleep.recover(ssh)
        self.assertEqual(ssh.files, {sleep.CONFIG_PATH: original})

    def test_wallpaper_recovery_never_deletes_foreign_image(self):
        ssh = FakeSleepSSH({sleep.CONFIG_PATH: b"[General]\n"})
        with patch.object(sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY):
            sleep.enable(ssh, b"ours")
            ssh.files[sleep.MANAGED_IMAGE_PATH] = b"foreign"
            before = dict(ssh.files)
            with self.assertRaisesRegex(RuntimeError, "不一致"):
                sleep.recover(ssh)
        self.assertEqual(ssh.files, before)

    def test_wallpaper_status_and_post_write_hashes_do_not_download_owned_image(self):
        class ReadCountingSSH(FakeSleepSSH):
            image_reads = 0

            def open_remote(self, path, mode="rb"):
                if path == sleep.MANAGED_IMAGE_PATH:
                    self.image_reads += 1
                return super().open_remote(path, mode)

        ssh = ReadCountingSSH({sleep.CONFIG_PATH: b"[General]\n"})
        with patch.object(sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY):
            sleep.enable(ssh, b"image")
            self.assertTrue(sleep.get_status(ssh).enabled)
            self.assertEqual(ssh.image_reads, 0)
            sleep.disable(ssh)
            self.assertGreaterEqual(ssh.image_reads, 1)

    def test_interrupted_wallpaper_disable_remains_recoverable(self):
        class LostConnection(BaseException):
            pass

        ssh = FakeSleepSSH({sleep.CONFIG_PATH: b"[General]\nKeep=1\n"})
        with patch.object(sleep.tap, "get_device_identity", return_value=sleep.SUPPORTED_IDENTITY):
            sleep.enable(ssh, b"ours")
            execute = ssh.exec_checked

            def lose_after_image_remove(command):
                result = execute(command)
                if command == f"rm -f {sleep.MANAGED_IMAGE_PATH}":
                    raise LostConnection()
                return result

            ssh.exec_checked = lose_after_image_remove
            with self.assertRaises(LostConnection):
                sleep.disable(ssh)
            self.assertIn(sleep.MARKER_PATH, ssh.files)
            ssh.exec_checked = execute
            sleep.recover(ssh)
        self.assertEqual(ssh.files, {sleep.CONFIG_PATH: b"[General]\nKeep=1\n"})


if __name__ == "__main__":
    unittest.main()
