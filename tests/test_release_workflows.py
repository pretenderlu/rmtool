import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowTests(unittest.TestCase):
    def test_feature_assets_are_strictly_verified_and_published_manifest_last(self):
        workflow = (ROOT / ".github/workflows/sync-feature-assets.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("tap-page-turn-assets", workflow)
        self.assertIn("fast-mono-reading-assets", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("- tap-page-turn/manifest.json", workflow)
        self.assertIn("- fast-mono-reading/manifest.json", workflow)
        self.assertIn("release manifest does not match", workflow)
        self.assertIn('document.get("schema_version") != 1', workflow)
        self.assertIn("ASSET_RE.fullmatch(name)", workflow)
        self.assertIn("hashlib.sha256(payload).hexdigest()", workflow)
        self.assertIn("downloaded != set(assets)", workflow)
        self.assertIn('"name": "tap-page-turn"', workflow)
        self.assertIn('"name": "fast-mono-reading"', workflow)
        self.assertIn('key = f"{feature}/{name}"', workflow)
        self.assertIn("client.upload_file(", workflow)
        self.assertIn(
            "LocalFilePath=str(release_dirs[feature] / name)", workflow
        )
        self.assertIn("PartSize=part_size_mb", workflow)
        self.assertIn("MAXThread=max_threads", workflow)
        self.assertIn("part_size_mb = 2", workflow)
        self.assertIn("max_threads = 3", workflow)
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

    def test_application_release_publishes_three_artifacts_versioned_before_latest(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("tap-page-turn/manifest.json:tap-page-turn", workflow)
        for name in (
            "rmtool-windows-x64.zip",
            "rmtool-windows-x64-onefile.exe",
            "rmtool-macos-arm64.app.zip",
        ):
            self.assertGreaterEqual(workflow.count(name), 2)
        self.assertIn("cos-python-sdk-v5==1.9.44", workflow)
        self.assertIn("--draft", workflow)
        self.assertIn('existing_draft=$(gh release view "$GITHUB_REF_NAME"', workflow)
        self.assertIn('gh release upload "$GITHUB_REF_NAME" "${assets[@]}" --clobber', workflow)
        self.assertIn("is already public; refusing to replace it", workflow)
        self.assertIn('gh release download "$GITHUB_REF_NAME"', workflow)
        self.assertIn("Verified GitHub release artifact:", workflow)
        self.assertIn("client.upload_file(", workflow)
        self.assertIn("LocalFilePath=str(path)", workflow)
        self.assertIn("PartSize=part_size_mb", workflow)
        self.assertIn("MAXThread=max_threads", workflow)
        self.assertIn("part_size_mb = 2", workflow)
        self.assertIn("max_threads = 3", workflow)
        self.assertIn("def file_sha256(path):", workflow)
        self.assertIn("actual_digest.update(chunk)", workflow)
        self.assertNotIn(
            'artifacts = {name: (Path("dist") / name).read_bytes()', workflow
        )
        self.assertLess(
            workflow.index('gh release create "$GITHUB_REF_NAME"'),
            workflow.index("Publish application artifacts to Tencent COS"),
        )
        versioned = 'versioned_prefix = f"releases/{os.environ[\'GITHUB_REF_NAME\']}"'
        upload_versioned = "upload_prefix(versioned_prefix)"
        verify_versioned = "verify_prefix(versioned_prefix)"
        upload_latest = 'upload_prefix("releases/latest")'
        verify_latest = 'verify_prefix("releases/latest")'
        self.assertIn(versioned, workflow)
        self.assertLess(workflow.index(upload_versioned), workflow.index(verify_versioned))
        self.assertLess(workflow.index(verify_versioned), workflow.index(upload_latest))
        self.assertLess(workflow.index(upload_latest), workflow.index(verify_latest))
        self.assertLess(
            workflow.index(verify_latest),
            workflow.index('gh release edit "$GITHUB_REF_NAME" --draft=false'),
        )
        self.assertIn("Verified public application artifact:", workflow)
        self.assertIn(
            "TENCENT_CLOUD_SECRET_KEY: ${{ secrets.TENCENT_CLOUD_SECRET_KEY }}",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
