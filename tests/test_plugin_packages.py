import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _plugin_packages as pp
import _tap_page_turn as tap


def _archive(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _package(files):
    return SimpleNamespace(
        files=tuple(
            pp.PayloadFile(name, hashlib.sha256(data).hexdigest(), len(data), 0o644)
            for name, data in files.items()
        )
    )


class FieldValidationTests(unittest.TestCase):
    def test_label_is_used_in_messages(self):
        with self.assertRaisesRegex(RuntimeError, "阅读增强清单字段 firmware 无效"):
            pp.required_string({"firmware": "x"}, "firmware", pp.FIRMWARE_RE, "阅读增强")

    def test_unsafe_paths_rejected(self):
        for value in ("/abs", "../up", "a/../b", "a\\b", "", "a//b", None):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                pp.safe_relative_path(value)

    def test_payload_file_rejects_bad_mode_and_size(self):
        base = {"path": "a", "sha256": "0" * 64, "size": 1, "mode": 0o644}
        self.assertEqual(pp.parse_payload_file(base).path, "a")
        for override in ({"mode": 0o777}, {"size": 0}, {"size": True}, {"sha256": "z"}):
            with self.subTest(override=override), self.assertRaises(RuntimeError):
                pp.parse_payload_file({**base, **override})

    def test_tap_aliases_point_at_shared_implementation(self):
        self.assertIs(tap.PayloadFile, pp.PayloadFile)
        self.assertIs(tap._required_string, pp.required_string)
        self.assertIs(tap._write_atomic, pp.write_atomic)
        self.assertIs(tap._SHA256_RE, pp.SHA256_RE)


class CatalogTests(unittest.TestCase):
    def call(self, state, fetch, refresh=True):
        return pp.load_catalog(
            manifest_urls=("https://a/manifest.json", "https://b/manifest.json"),
            cache_path=Path(state) / "manifest.json",
            bundled_path=Path(state) / "bundled.json",
            parse=lambda data: json.loads(data)["ok"],
            max_manifest_bytes=1024,
            refresh=refresh,
            log_label="test",
            error_message="no catalog",
            download=fetch,
        )

    def test_invalid_remote_does_not_overwrite_cache(self):
        with tempfile.TemporaryDirectory() as state, patch.object(pp.logging, "warning"):
            cache = Path(state) / "manifest.json"
            cache.write_bytes(b'{"ok": "cached"}')
            self.assertEqual(self.call(state, lambda *_: b"garbage"), "cached")
            self.assertEqual(cache.read_bytes(), b'{"ok": "cached"}')

    def test_second_mirror_used_and_cached(self):
        responses = iter((OSError("down"), b'{"ok": "remote"}'))

        def fetch(*_):
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item

        with tempfile.TemporaryDirectory() as state, patch.object(pp.logging, "warning"):
            self.assertEqual(self.call(state, fetch), "remote")
            self.assertEqual((Path(state) / "manifest.json").read_bytes(), b'{"ok": "remote"}')

    def test_bundled_fallback_and_error(self):
        with tempfile.TemporaryDirectory() as state, patch.object(pp.logging, "warning"):
            with self.assertRaisesRegex(RuntimeError, "no catalog"):
                self.call(state, None, refresh=False)
            (Path(state) / "bundled.json").write_bytes(b'{"ok": "bundled"}')
            self.assertEqual(self.call(state, None, refresh=False), "bundled")


class ExtractTests(unittest.TestCase):
    def test_extracts_listed_files_with_modes(self):
        files = {"a.txt": b"alpha", "d/b.bin": b"beta"}
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "p.tar.gz"
            archive.write_bytes(_archive(files))
            out = pp.extract_verified_package(archive, _package(files), Path(tmp) / "out", "测试")
            self.assertEqual((out / "d" / "b.bin").read_bytes(), b"beta")

    def test_rejects_unlisted_tampered_and_missing(self):
        files = {"a.txt": b"alpha"}
        cases = {
            "未授权": {"a.txt": b"alpha", "extra": b"x"},
            "SHA-256": {"a.txt": b"ALPHA"},
            "缺少清单文件": {},
        }
        for expected, contents in cases.items():
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "p.tar.gz"
                archive.write_bytes(_archive(contents))
                with self.assertRaisesRegex(RuntimeError, expected):
                    pp.extract_verified_package(archive, _package(files), Path(tmp) / "out", "测试")

    def test_select_by_identity_requires_exact_match(self):
        identity = SimpleNamespace(firmware="f", platform="p", architecture="a", xochitl_sha256="s")
        hit = SimpleNamespace(firmware="f", platform="p", architecture="a", xochitl_sha256="s")
        miss = SimpleNamespace(firmware="f", platform="p", architecture="a", xochitl_sha256="t")
        self.assertIs(pp.select_by_identity((miss, hit), identity), hit)
        self.assertIsNone(pp.select_by_identity((miss,), identity))


if __name__ == "__main__":
    unittest.main()
