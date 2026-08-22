import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import publish_resources as publisher


class PublishResourcesTests(unittest.TestCase):
    def make_localization(self, root: Path):
        payload = b"localized"
        manifest = json.dumps(
            {
                "schema": 1,
                "firmwares": {
                    "20260806095513": {
                        "asset": "localized.qm",
                        "release_version": "3.28.0.166",
                        "channel": "beta",
                        "platform": "ferrari",
                        "size": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "stock_french_sha256": "1" * 64,
                    }
                },
            },
            separators=(",", ":"),
        ).encode()
        repository_manifest = root / "repository-manifest.json"
        repository_manifest.write_bytes(manifest)
        release_dir = root / "release"
        release_dir.mkdir()
        (release_dir / "manifest.json").write_bytes(manifest)
        (release_dir / "localized.qm").write_bytes(payload)
        resource = publisher.Resource(
            "localization", "localization-assets", repository_manifest, ""
        )
        return resource, release_dir

    def make_feature(self, root: Path):
        payload = b"archive"
        name = "rmtool-tap-page-turn-ferrari-20260806095513.tar.gz"
        manifest = json.dumps(
            {
                "schema_version": 1,
                "packages": [
                    {
                        "firmware": "20260806095513",
                        "release_version": "3.28.0.166",
                        "channel": "beta",
                        "platform": "ferrari",
                        "architecture": "aarch64",
                        "xochitl_sha256": "2" * 64,
                        "asset": name,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "size": len(payload),
                        "files": [
                            {
                                "path": "feature.qmd",
                                "sha256": "3" * 64,
                                "size": 1,
                                "mode": 0o644,
                            }
                        ],
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()
        repository_manifest = root / "repository-feature-manifest.json"
        repository_manifest.write_bytes(manifest)
        release_dir = root / "feature-release"
        release_dir.mkdir()
        (release_dir / "manifest.json").write_bytes(manifest)
        (release_dir / name).write_bytes(payload)
        resource = publisher.Resource(
            "tap-page-turn",
            "tap-page-turn-assets",
            repository_manifest,
            "tap-page-turn",
            "rmtool-tap-page-turn-",
        )
        return resource, release_dir

    def test_validators_require_exact_manifest_and_complete_payload_set(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resource, release_dir = self.make_localization(root)
            bundle = publisher._validate_localization(resource, release_dir)
            self.assertEqual(bundle.assets, ("localized.qm",))

            (release_dir / "extra.qm").write_bytes(b"extra")
            with self.assertRaisesRegex(RuntimeError, "payload set"):
                publisher._validate_localization(resource, release_dir)
            (release_dir / "extra.qm").unlink()
            (release_dir / "manifest.json").write_bytes(b"{}")
            with self.assertRaisesRegex(RuntimeError, "byte-for-byte"):
                publisher._validate_localization(resource, release_dir)

    def test_localization_validator_preserves_identity_and_hash_guards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resource, release_dir = self.make_localization(root)
            document = json.loads(resource.repository_manifest.read_text())
            base = document["firmwares"]["20260806095513"]
            variant = dict(base)
            variant["stock_french_sha256"] = "4" * 64
            base["variants"] = [variant]
            changed = json.dumps(document, separators=(",", ":")).encode()
            resource.repository_manifest.write_bytes(changed)
            (release_dir / "manifest.json").write_bytes(changed)
            with self.assertRaisesRegex(RuntimeError, "Duplicate hardware"):
                publisher._validate_localization(resource, release_dir)

            variant["platform"] = "chiappa"
            variant["stock_french_sha256"] = base["sha256"]
            changed = json.dumps(document, separators=(",", ":")).encode()
            resource.repository_manifest.write_bytes(changed)
            (release_dir / "manifest.json").write_bytes(changed)
            with self.assertRaisesRegex(RuntimeError, "digests conflict"):
                publisher._validate_localization(resource, release_dir)

    def test_feature_validator_rejects_unsafe_nested_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            resource, release_dir = self.make_feature(root)
            publisher._validate_feature(resource, release_dir)

            document = json.loads(resource.repository_manifest.read_text())
            document["packages"][0]["files"][0]["path"] = "../feature.qmd"
            changed = json.dumps(document, separators=(",", ":")).encode()
            resource.repository_manifest.write_bytes(changed)
            (release_dir / "manifest.json").write_bytes(changed)
            with self.assertRaisesRegex(RuntimeError, "nested file metadata"):
                publisher._validate_feature(resource, release_dir)

    def test_reading_enhancements_requires_verification_and_ordered_urls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            resource, release_dir = self.make_feature(Path(temp_dir))
            resource = publisher.Resource(
                "reading-enhancements",
                "reading-enhancements-assets",
                resource.repository_manifest,
                "reading-enhancements",
                "rmtool-reading-enhancements-",
                frozenset(
                    {
                        "offline_verified",
                        "device_verified",
                        "package_revision",
                        "urls",
                    }
                ),
                (
                    f"{publisher.COS_PUBLIC_BASE_URL}/reading-enhancements",
                    "https://github.com/pretenderlu/rmtool/releases/download/reading-enhancements-assets",
                ),
            )
            document = json.loads(resource.repository_manifest.read_text())
            package = document["packages"][0]
            package["asset"] = package["asset"].replace(
                "rmtool-tap-page-turn-", "rmtool-reading-enhancements-"
            )
            package.update(
                offline_verified=True,
                device_verified=False,
                package_revision=1,
                urls=[f"{base}/{package['asset']}" for base in resource.url_bases],
            )

            def write_manifest():
                payload = json.dumps(document, separators=(",", ":")).encode()
                resource.repository_manifest.write_bytes(payload)
                (release_dir / "manifest.json").write_bytes(payload)

            old_asset = next(path for path in release_dir.glob("*.tar.gz"))
            old_asset.rename(release_dir / package["asset"])
            write_manifest()
            publisher._validate_feature(resource, release_dir)

            # Mirror order is no longer significant (GitHub may be listed
            # first); the validator compares the exact URLs as a set.
            package["urls"].reverse()
            write_manifest()
            publisher._validate_feature(resource, release_dir)

            package["urls"][0] = "https://example.invalid/payload"
            write_manifest()
            with self.assertRaisesRegex(RuntimeError, "download URLs"):
                publisher._validate_feature(resource, release_dir)

            package["urls"][0] = f"{resource.url_bases[0]}/{package['asset']}"
            package["offline_verified"] = 1
            write_manifest()
            with self.assertRaisesRegex(RuntimeError, "verification metadata"):
                publisher._validate_feature(resource, release_dir)

    def test_publisher_uploads_changed_payloads_then_all_manifests(self):
        events = []

        class Client:
            def upload_file(self, **kwargs):
                events.append(("upload-payload", kwargs["Key"], kwargs["Bucket"]))

            def put_object(self, **kwargs):
                events.append(("upload-manifest", kwargs["Key"], kwargs["Bucket"]))

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_resource, first_dir = self.make_localization(root)
            second_root = root / "second"
            second_root.mkdir()
            second_resource, second_dir = self.make_feature(second_root)
            bundles = (
                publisher._validate_localization(first_resource, first_dir),
                publisher._validate_feature(second_resource, second_dir),
            )
            client = Client()

            def precheck(_path, key):
                events.append(("precheck-payload", key, publisher.COS_BUCKET))
                return key == "localized.qm"

            def verify(_path, key):
                kind = "manifest" if key.endswith("manifest.json") else "payload"
                events.append((f"verify-{kind}", key, publisher.COS_BUCKET))

            with (
                patch.object(
                    publisher,
                    "_public_matches",
                    side_effect=precheck,
                ),
                patch.object(
                    publisher,
                    "_verify_public",
                    side_effect=verify,
                ),
            ):
                publisher.publish_bundles(bundles, client)

            self.assertEqual(
                events,
                [
                    ("precheck-payload", "localized.qm", publisher.COS_BUCKET),
                    (
                        "precheck-payload",
                        "tap-page-turn/rmtool-tap-page-turn-ferrari-20260806095513.tar.gz",
                        publisher.COS_BUCKET,
                    ),
                    (
                        "upload-payload",
                        "tap-page-turn/rmtool-tap-page-turn-ferrari-20260806095513.tar.gz",
                        publisher.COS_BUCKET,
                    ),
                    ("verify-payload", "localized.qm", publisher.COS_BUCKET),
                    (
                        "verify-payload",
                        "tap-page-turn/rmtool-tap-page-turn-ferrari-20260806095513.tar.gz",
                        publisher.COS_BUCKET,
                    ),
                    ("upload-manifest", "manifest.json", publisher.COS_BUCKET),
                    (
                        "upload-manifest",
                        "tap-page-turn/manifest.json",
                        publisher.COS_BUCKET,
                    ),
                    ("verify-manifest", "manifest.json", publisher.COS_BUCKET),
                    (
                        "verify-manifest",
                        "tap-page-turn/manifest.json",
                        publisher.COS_BUCKET,
                    ),
                ],
            )

    def test_publish_initializes_cos_before_downloading_releases(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with (
                patch.object(
                    publisher,
                    "load_env",
                    return_value={
                        "TENCENT_CLOUD_SECRET_ID": "id",
                        "TENCENT_CLOUD_SECRET_KEY": "key",
                    },
                ),
                patch.object(
                    publisher,
                    "make_cos_client",
                    side_effect=RuntimeError("SDK missing"),
                ) as make_client,
                patch.object(publisher, "download_releases") as download,
            ):
                result = publisher.main(
                    [
                        "publish",
                        "--download-dir",
                        str(root / "downloads"),
                        "--env-file",
                        str(root / ".env"),
                    ]
                )

            self.assertEqual(result, 1)
            make_client.assert_called_once()
            download.assert_not_called()

    def test_env_reader_accepts_only_bucket_publisher_credentials(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "TENCENT_CLOUD_SECRET_ID=id\nTENCENT_CLOUD_SECRET_KEY=key\n",
                encoding="utf-8",
            )
            self.assertEqual(
                publisher.load_env(env_file),
                {
                    "TENCENT_CLOUD_SECRET_ID": "id",
                    "TENCENT_CLOUD_SECRET_KEY": "key",
                },
            )
            env_file.write_text(
                "TENCENT_CLOUD_SECRET_ID=id\nTENCENT_CLOUD_SECRET_KEY=key\nCOS_BUCKET=other\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "Invalid or duplicate"):
                publisher.load_env(env_file)

    def test_network_retry_is_bounded(self):
        attempts = []

        def succeeds_on_third_call():
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("temporary")
            return "ok"

        with patch.object(publisher.time, "sleep") as sleep:
            self.assertEqual(publisher._retry("test", succeeds_on_third_call), "ok")
        self.assertEqual(len(attempts), 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

        with (
            patch.object(publisher.time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "after 3 attempts"),
        ):
            publisher._retry("test", lambda: (_ for _ in ()).throw(OSError()))

    def test_fixed_release_matrix_and_wrapper_cleanup_are_tracked(self):
        self.assertEqual(
            tuple(publisher.RESOURCES),
            (
                "localization",
                "tap-page-turn",
                "fast-mono-reading",
                "native-chinese",
                "pinyin-input",
                "reading-enhancements",
            ),
        )
        reading = publisher.RESOURCES["reading-enhancements"]
        self.assertEqual(reading.tag, "reading-enhancements-assets")
        self.assertEqual(reading.object_prefix, "reading-enhancements")
        self.assertEqual(
            reading.url_bases,
            (
                f"{publisher.COS_PUBLIC_BASE_URL}/reading-enhancements",
                "https://github.com/pretenderlu/rmtool/releases/download/reading-enhancements-assets",
            ),
        )
        wrapper = (publisher.ROOT / "publish-cos.ps1").read_text(encoding="utf-8")
        self.assertIn("finally", wrapper)
        self.assertIn("Remove-Item -LiteralPath $downloadDir -Recurse -Force", wrapper)


if __name__ == "__main__":
    unittest.main()
