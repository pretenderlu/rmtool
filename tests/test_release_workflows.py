import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowTests(unittest.TestCase):
    def test_resource_workflows_only_validate_fixed_releases(self):
        localization = (
            ROOT / ".github/workflows/sync-localization-assets.yml"
        ).read_text(encoding="utf-8")
        features = (ROOT / ".github/workflows/sync-feature-assets.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("name: Verify Localization Assets", localization)
        self.assertIn("localization-assets", localization)
        self.assertIn("--resource localization", localization)
        self.assertIn("name: Verify Feature Assets", features)
        self.assertIn("reading-enhancements/manifest.json", features)
        for name in (
            "tap-page-turn-assets",
            "fast-mono-reading-assets",
            "native-chinese-assets",
            "pinyin-input-assets",
            "reading-enhancements-assets",
        ):
            self.assertIn(name, features)
        for name in (
            "tap-page-turn",
            "fast-mono-reading",
            "native-chinese",
            "pinyin-input",
            "reading-enhancements",
        ):
            self.assertIn(f"--resource {name}", features)

        for workflow in (localization, features):
            self.assertIn("tools/publish_resources.py verify", workflow)
            self.assertNotIn("TENCENT_CLOUD", workflow)
            self.assertNotIn("qcloud", workflow.casefold())
            self.assertNotIn("put_object", workflow)
            self.assertNotIn("upload_file", workflow)
            self.assertNotIn("myqcloud.com", workflow)

    def test_application_release_publishes_three_verified_github_artifacts(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("tap-page-turn/manifest.json:tap-page-turn", workflow)
        self.assertIn("native-chinese/manifest.json:native-chinese", workflow)
        self.assertIn("pinyin-input/manifest.json:pinyin-input", workflow)
        self.assertIn("reading-enhancements/manifest.json:reading-enhancements", workflow)
        for name in (
            "rmtool-windows-x64.zip",
            "rmtool-windows-x64-onefile.exe",
            "rmtool-macos-arm64.app.zip",
        ):
            self.assertGreaterEqual(workflow.count(name), 2)
        self.assertIn("--draft", workflow)
        self.assertIn(
            'existing_draft=$(gh release view "$GITHUB_REF_NAME"', workflow
        )
        self.assertIn(
            'gh release upload "$GITHUB_REF_NAME" "${assets[@]}" --clobber',
            workflow,
        )
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
