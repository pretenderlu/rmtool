import hashlib
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import _native_chinese as native
import _note_enhancements as note
import _package_download as downloads
import _pinyin_input as pinyin
import _reading_enhancements as reading


MODULES = (native, reading, note, pinyin)
DATA = b"verified package"


class PackageDownloadAuditTests(unittest.TestCase):
    @contextmanager
    def scenario(self, module):
        package = SimpleNamespace(
            firmware="test-firmware", asset="package.tar.gz", size=len(DATA),
            sha256=hashlib.sha256(DATA).hexdigest(),
            download_urls=("https://github.com/first", "https://mirror.myqcloud.com/second"),
        )
        with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as state:
            destination = Path(state) / "cache" / module.FEATURE_ID / package.firmware / package.asset
            with patch.object(module.tap, "_download_limited") as download, patch.object(
                module.tap, "_write_atomic", wraps=module.tap._write_atomic
            ) as write, patch.object(downloads.logging, "warning"):
                yield package, state, destination, download, write

    def test_verified_cache_skips_download_and_write(self):
        for module in MODULES:
            with self.scenario(module) as (package, state, destination, download, write):
                destination.parent.mkdir(parents=True)
                destination.write_bytes(DATA)
                self.assertEqual(module.download_package(package, state), destination)
                download.assert_not_called()
                write.assert_not_called()

    def test_corrupt_cache_is_replaced_only_with_verified_bytes(self):
        for module in MODULES:
            for cached in (b"short", b"x" * len(DATA)):
                with self.scenario(module) as (package, state, destination, download, write):
                    destination.parent.mkdir(parents=True)
                    destination.write_bytes(cached)
                    download.return_value = DATA
                    self.assertEqual(module.download_package(package, state), destination)
                    download.assert_called_once_with(package.download_urls[0], module.MAX_PACKAGE_BYTES)
                    write.assert_called_once_with(destination, DATA)
                    self.assertEqual(destination.read_bytes(), DATA)
                    self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_mirror_failover_for_network_hash_and_size_errors(self):
        for module in MODULES:
            for first in (OSError("offline"), b"x" * len(DATA), DATA[:-1], DATA + b"x"):
                with self.scenario(module) as (package, state, destination, download, write):
                    download.side_effect = [first, DATA]
                    self.assertEqual(module.download_package(package, state), destination)
                    self.assertEqual(download.call_args_list, [
                        call(url, module.MAX_PACKAGE_BYTES) for url in package.download_urls
                    ])
                    write.assert_called_once_with(destination, DATA)
                    self.assertEqual(destination.read_bytes(), DATA)

    def test_invalid_mirrors_never_replace_cache(self):
        for module in MODULES:
            for invalid in (b"x" * len(DATA), DATA[:-1], DATA + b"x"):
                with self.scenario(module) as (package, state, destination, download, write):
                    destination.parent.mkdir(parents=True)
                    destination.write_bytes(b"old corrupt cache")
                    download.return_value = invalid
                    with self.assertRaises(downloads.PackageDownloadError) as raised:
                        module.download_package(package, state)
                    self.assertIsInstance(raised.exception.__cause__, RuntimeError)
                    self.assertEqual(download.call_args_list, [
                        call(url, module.MAX_PACKAGE_BYTES) for url in package.download_urls
                    ])
                    write.assert_not_called()
                    self.assertEqual(destination.read_bytes(), b"old corrupt cache")

    def test_all_fail_retains_last_cause_and_manual_store_callback(self):
        for module in MODULES:
            with self.scenario(module) as (package, state, destination, download, write):
                last = OSError("last mirror failed")
                download.side_effect = [OSError("first mirror failed"), last]
                with self.assertRaises(downloads.PackageDownloadError) as raised:
                    module.download_package(package, state)
                error = raised.exception
                self.assertIs(error.__cause__, last)
                self.assertEqual((error.asset, error.size, error.sha256, error.urls), (
                    package.asset, package.size, package.sha256, package.download_urls,
                ))
                write.assert_not_called()
                self.assertFalse(destination.exists())
                source = Path(state) / "manual.tar.gz"
                for invalid in (b"short", b"x" * len(DATA)):
                    source.write_bytes(invalid)
                    with self.assertRaises(RuntimeError):
                        error.store(str(source))
                    write.assert_not_called()
                source.write_bytes(DATA)
                with patch.object(module, "load_local_package", wraps=module.load_local_package) as local:
                    self.assertEqual(error.store(str(source)), destination)
                    local.assert_called_once_with(package, str(source), state)
                write.assert_called_once_with(destination, DATA)
                download.reset_mock()
                self.assertEqual(module.download_package(package, state), destination)
                download.assert_not_called()

    def test_atomic_save_failure_tries_next_mirror(self):
        for module in MODULES:
            with self.scenario(module) as (package, state, destination, download, write):
                download.return_value = DATA
                write.side_effect = [OSError("cannot save first attempt"), None]
                self.assertEqual(module.download_package(package, state), destination)
                self.assertEqual(write.call_args_list, [call(destination, DATA)] * 2)
                self.assertEqual(download.call_args_list, [
                    call(url, module.MAX_PACKAGE_BYTES) for url in package.download_urls
                ])


if __name__ == "__main__":
    unittest.main()
