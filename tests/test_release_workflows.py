import hashlib
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def localization_validator_source():
    workflow = (ROOT / ".github/workflows/sync-localization-assets.yml").read_text(
        encoding="utf-8"
    )
    step = workflow.split("      - name: Verify release manifest and payloads", 1)[1]
    script = step.split("          python - <<'PY'\n", 1)[1].split(
        "          PY\n", 1
    )[0]
    return textwrap.dedent(script)


class ReleaseWorkflowTests(unittest.TestCase):
    @staticmethod
    def make_localization_entry(
        *,
        base_platform="Chiappa",
        variant_platform="chiappa",
        base_release="3.28.0.163",
        variant_release="3.28.0.164",
    ):
        payloads = {"old.qm": b"old", "new.qm": b"new"}

        def package(name, platform, release, stock_digest):
            payload = payloads[name]
            return {
                "asset": name,
                "release_version": release,
                "channel": "beta",
                "platform": platform,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "stock_french_sha256": stock_digest,
            }

        entry = package("old.qm", base_platform, base_release, "1" * 64)
        entry["variants"] = [
            package("new.qm", variant_platform, variant_release, "2" * 64)
        ]
        return entry, payloads

    def run_localization_validator(self, entry, payloads):
        manifest = json.dumps(
            {"schema": 1, "firmwares": {"20260702125656": entry}}
        ).encode()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            release_dir = root / "release"
            release_dir.mkdir()
            (root / "translations").mkdir()
            (root / "translations" / "manifest.json").write_bytes(manifest)
            (release_dir / "manifest.json").write_bytes(manifest)
            for name, payload in payloads.items():
                (release_dir / name).write_bytes(payload)
            env = {
                **os.environ,
                "ASSET_LIST_PATH": str(root / "assets.json"),
                "RELEASE_DIR": str(release_dir),
            }
            return subprocess.run(
                [sys.executable, "-c", localization_validator_source()],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_localization_validator_allows_platform_across_distinct_releases(self):
        entry, payloads = self.make_localization_entry()

        result = self.run_localization_validator(entry, payloads)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_localization_validator_rejects_duplicate_exact_release(self):
        entry, payloads = self.make_localization_entry(
            variant_release="3.28.0.163"
        )

        result = self.run_localization_validator(entry, payloads)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "Duplicate hardware platform and release in firmware entry.",
            result.stderr,
        )

    def test_localization_validator_preserves_payload_and_carrier_checks(self):
        def assert_rejected(name, mutate, expected):
            entry, payloads = self.make_localization_entry(
                variant_platform="ferrari"
            )
            mutate(entry)
            result = self.run_localization_validator(entry, payloads)
            with self.subTest(name=name):
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

        assert_rejected(
            "payload-size",
            lambda entry: entry.__setitem__("size", entry["size"] + 1),
            "Release asset validation failed for old.qm.",
        )
        assert_rejected(
            "payload-sha256",
            lambda entry: entry.__setitem__("sha256", "3" * 64),
            "Release asset validation failed for old.qm.",
        )
        assert_rejected(
            "conflicting-asset-metadata",
            lambda entry: entry["variants"][0].__setitem__(
                "asset", entry["asset"]
            ),
            "Conflicting size or SHA-256 metadata for old.qm.",
        )
        assert_rejected(
            "unsafe-metadata",
            lambda entry: entry.__setitem__("asset", "../old.qm"),
            "Invalid localization asset metadata: '../old.qm'",
        )
        assert_rejected(
            "duplicate-stock-carrier",
            lambda entry: entry["variants"][0].__setitem__(
                "stock_french_sha256", entry["stock_french_sha256"]
            ),
            "Duplicate stock carrier digest in firmware entry.",
        )
        assert_rejected(
            "stock-localized-conflict",
            lambda entry: entry.__setitem__(
                "stock_french_sha256", entry["sha256"]
            ),
            "Stock and localized digests conflict.",
        )

    def test_feature_assets_are_strictly_verified_and_published_manifest_last(self):
        workflow = (ROOT / ".github/workflows/sync-feature-assets.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("tap-page-turn-assets", workflow)
        self.assertIn("fast-mono-reading-assets", workflow)
        self.assertIn("native-chinese-assets", workflow)
        self.assertIn("pinyin-input-assets", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("- tap-page-turn/manifest.json", workflow)
        self.assertIn("- fast-mono-reading/manifest.json", workflow)
        self.assertIn("- native-chinese/manifest.json", workflow)
        self.assertIn("- pinyin-input/manifest.json", workflow)
        self.assertIn("release manifest does not match", workflow)
        self.assertIn('document.get("schema_version") != 1', workflow)
        self.assertIn("ASSET_RE.fullmatch(name)", workflow)
        self.assertIn("hashlib.sha256(payload).hexdigest()", workflow)
        self.assertIn("downloaded != set(assets)", workflow)
        self.assertIn('"name": "tap-page-turn"', workflow)
        self.assertIn('"name": "fast-mono-reading"', workflow)
        self.assertIn('"name": "native-chinese"', workflow)
        self.assertIn('"name": "pinyin-input"', workflow)
        self.assertIn('"asset_prefix": "rmtool-native-chinese-"', workflow)
        self.assertIn('"asset_prefix": "rmtool-pinyin-input-"', workflow)
        self.assertIn(
            'feature["name"] in {"fast-mono-reading", "native-chinese", "pinyin-input"}',
            workflow,
        )
        self.assertIn('type(package.get("offline_verified")) is not bool', workflow)
        self.assertIn('type(package.get("device_verified")) is not bool', workflow)
        self.assertIn(
            "https://rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com/native-chinese",
            workflow,
        )
        self.assertIn(
            "https://github.com/pretenderlu/rmtool/releases/download/native-chinese-assets",
            workflow,
        )
        self.assertIn("NATIVE_CHINESE_RELEASE_DIR", workflow)
        self.assertIn("PINYIN_RELEASE_DIR", workflow)
        self.assertIn("Invalid {feature['name']} download URLs.", workflow)
        self.assertIn('key = f"{feature}/{name}"', workflow)
        self.assertIn("client.upload_file(", workflow)
        self.assertIn("LocalFilePath=str(path)", workflow)
        self.assertIn("PartSize=part_size_mb", workflow)
        self.assertIn("MAXThread=max_threads", workflow)
        self.assertIn("part_size_mb = 2", workflow)
        self.assertIn("max_threads = 3", workflow)
        self.assertIn("Timeout=300", workflow)
        self.assertIn('exc.get_error_code() != "AccessDenied"', workflow)
        self.assertIn('upload_state["multipart_available"] = False', workflow)
        self.assertIn('with path.open("rb") as body:', workflow)
        self.assertIn("Body=body", workflow)
        self.assertIn("for attempt in range(3):", workflow)
        self.assertIn("time.sleep(2 ** attempt)", workflow)
        self.assertIn("if not is_retryable_put_error(exc) or attempt == 2:", workflow)
        self.assertIn('"UserNetworkTooSlow"', workflow)
        self.assertIn("def public_object_matches(path, key):", workflow)
        self.assertIn("?precheck={precheck_token}", workflow)
        self.assertIn("while chunk := response.read(1024 * 1024):", workflow)
        self.assertIn("actual_size == expected_size", workflow)
        self.assertIn("actual_digest.hexdigest() == expected_digest", workflow)
        self.assertIn("if public_object_matches(path, key):", workflow)
        self.assertIn("Skipped exact existing feature payload:", workflow)
        self.assertIn("COS precheck failed for", workflow)
        self.assertIn("COS precheck mismatch for", workflow)
        self.assertLess(
            workflow.index("if public_object_matches(path, key):"),
            workflow.index("upload_file_compatible(\n                      path,"),
        )
        self.assertLess(
            workflow.index("Uploaded feature payload:"),
            workflow.index("Uploaded feature manifest last:"),
        )
        manifest_upload = workflow.index("Uploaded feature manifest last:")
        self.assertIn("client.put_object(", workflow[:manifest_upload])
        self.assertIn("Verified public feature object:", workflow)
        self.assertIn("cos-python-sdk-v5==1.9.44", workflow)
        self.assertIn(
            "TENCENT_CLOUD_SECRET_ID: ${{ secrets.TENCENT_CLOUD_SECRET_ID }}",
            workflow,
        )
        self.assertNotIn("client.delete_", workflow)

    def test_application_release_publishes_three_verified_github_artifacts(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("tap-page-turn/manifest.json:tap-page-turn", workflow)
        self.assertIn("native-chinese/manifest.json:native-chinese", workflow)
        self.assertIn("pinyin-input/manifest.json:pinyin-input", workflow)
        for name in (
            "rmtool-windows-x64.zip",
            "rmtool-windows-x64-onefile.exe",
            "rmtool-macos-arm64.app.zip",
        ):
            self.assertGreaterEqual(workflow.count(name), 2)
        self.assertIn("--draft", workflow)
        self.assertIn('existing_draft=$(gh release view "$GITHUB_REF_NAME"', workflow)
        self.assertIn('gh release upload "$GITHUB_REF_NAME" "${assets[@]}" --clobber', workflow)
        self.assertIn("is already public; refusing to replace it", workflow)
        self.assertIn('gh release download "$GITHUB_REF_NAME"', workflow)
        self.assertIn("Verified GitHub release artifact:", workflow)
        self.assertLess(
            workflow.index('gh release create "$GITHUB_REF_NAME"'),
            workflow.index("Verified GitHub release artifact:"),
        )
        self.assertLess(
            workflow.index("Verified GitHub release artifact:"),
            workflow.index('gh release edit "$GITHUB_REF_NAME" --draft=false'),
        )
        self.assertNotIn("Tencent COS", workflow)
        self.assertNotIn("TENCENT_CLOUD_SECRET", workflow)
        self.assertNotIn("qcloud_cos", workflow)


if __name__ == "__main__":
    unittest.main()
