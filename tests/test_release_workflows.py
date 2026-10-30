import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseWorkflowTests(unittest.TestCase):
    def test_current_release_notes_hide_internal_revision_terms(self):
        source = ast.parse((ROOT / "rmtool.py").read_text(encoding="utf-8"))
        version = next(
            ast.literal_eval(node.value) for node in source.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "APP_VERSION"
                    for target in node.targets)
        )
        tag = f"v{version}"
        for name in ("README.md", "README.en.md"):
            readme = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(f"[{tag}](https://github.com/pretenderlu/rmtool/releases/tag/{tag})", readme)
        notes = (
            ROOT / "docs" / "releases" / f"{tag}.md"
        ).read_text(encoding="utf-8")
        self.assertTrue(notes.startswith(f"# rmtool {tag}\n"))
        self.assertNotIn("revision", notes.casefold())

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
        self.assertIn("note-enhancements/manifest.json", features)
        self.assertIn("weread-launcher/manifest.json", features)
        for name in (
            "tap-page-turn-assets",
            "fast-mono-reading-assets",
            "native-chinese-assets",
            "pinyin-input-assets",
            "reading-enhancements-assets",
            "note-enhancements-assets",
            "weread-launcher-assets",
        ):
            self.assertIn(name, features)
        for name in (
            "tap-page-turn",
            "fast-mono-reading",
            "native-chinese",
            "pinyin-input",
            "reading-enhancements",
            "note-enhancements",
            "weread-launcher",
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
        self.assertIn(
            "*/manifest.json text eol=lf",
            (ROOT / ".gitattributes").read_text(encoding="utf-8"),
        )
        workflow = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )
        windows_build = (ROOT / "build-portable.ps1").read_text(encoding="utf-8")
        matrix_gate = (ROOT / "tools" / "validate_build_matrix.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("tap-page-turn/manifest.json:tap-page-turn", workflow)
        self.assertIn("native-chinese/manifest.json:native-chinese", workflow)
        self.assertIn("pinyin-input/manifest.json:pinyin-input", workflow)
        self.assertIn("reading-enhancements/manifest.json:reading-enhancements", workflow)
        self.assertIn("note-enhancements/manifest.json:note-enhancements", workflow)
        self.assertIn("weread-app/manifest.json:weread-app", workflow)
        self.assertIn("weread-launcher/manifest.json:weread-launcher", workflow)
        self.assertIn("--collect-data certifi", workflow)
        self.assertIn(
            "dist/rmtool.app/Contents/Resources/certifi/cacert.pem", workflow
        )
        self.assertIn(
            "dist/rmtool.app/Contents/MacOS/rmtool --https-smoke-test", workflow
        )
        self.assertLess(
            workflow.index("Contents/MacOS/rmtool --https-smoke-test"),
            workflow.index("ditto -c -k"),
        )
        self.assertIn('"--collect-data", "certifi"', windows_build)
        self.assertIn("tools\\validate_build_matrix.py", windows_build)
        self.assertIn("Validate device and firmware matrix", workflow)
        self.assertIn("3.28.0.172", matrix_gate)
        self.assertIn("OFFICIAL_172", matrix_gate)
        self.assertIn('"certifi\\cacert.pem"', windows_build)
        self.assertIn(
            'weread-app\\manifest.json\');weread-app', windows_build
        )
        self.assertIn(
            'weread-launcher\\manifest.json\');weread-launcher', windows_build
        )
        feature_workflow = (
            ROOT / ".github/workflows/sync-feature-assets.yml"
        ).read_text(encoding="utf-8")
        publisher = (ROOT / "tools/publish_resources.py").read_text(encoding="utf-8")
        for source in (workflow, windows_build, feature_workflow, publisher):
            for payload in (
                "remarkable-weread-v1.0.0-universal-release.zip",
                "remarkable-weread-v1.0.0-universal-aarch64.tar.gz",
                "weread-app-assets",
            ):
                self.assertNotIn(payload, source)
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


class ReleaseGateTests(unittest.TestCase):
    def load_checker(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("check_release", ROOT / "tools" / "check_release.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_current_version_passes_and_mismatched_tag_fails(self):
        checker = self.load_checker()
        version = checker.app_version()
        self.assertEqual(checker.problems(None), [])
        self.assertEqual(checker.problems(f"v{version}"), [])
        self.assertTrue(any("does not match" in item for item in checker.problems("v0.0.1")))

    def test_release_requires_verified_inputs_before_building(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("python tools/check_release.py", workflow)
        self.assertIn("python -m ruff check .", workflow)
        self.assertEqual(workflow.count("needs: verify-release"), 2)
        self.assertNotIn("--generate-notes", workflow)
        self.assertIn("--require-hashes --requirement requirements-build-macos.lock", workflow)

    def test_windows_build_is_hash_locked_and_smoke_tested(self):
        script = (ROOT / "build-portable.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("--require-hashes", script)
        self.assertIn("requirements-build-windows.lock", script)
        self.assertIn("Invoke-HttpsSmokeTest $executable", script)
        self.assertIn("Invoke-HttpsSmokeTest $oneFilePath", script)
        self.assertLess(
            script.index("Invoke-HttpsSmokeTest $executable"),
            script.index("Compress-Archive"),
        )

    def test_build_locks_pin_every_package_with_hashes(self):
        direct = {}
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
            if "==" in line and not line.startswith("#"):
                name, version = line.split("==")
                direct[name.strip().lower()] = version.strip()
        direct["pyinstaller"] = "6.21.0"
        for platform in ("windows", "macos"):
            with self.subTest(platform=platform):
                text = (ROOT / f"requirements-build-{platform}.lock").read_text(encoding="utf-8")
                pins = {}
                for line in text.splitlines():
                    if line and line[0].isalpha():
                        requirement = line.split(";")[0].rstrip(" \\")
                        self.assertIn("==", requirement, line)
                        name, version = requirement.split("==")
                        pins[name.lower()] = version.strip()
                for name, version in direct.items():
                    self.assertEqual(pins.get(name), version, name)
                self.assertIn("cryptography", pins)
                self.assertGreaterEqual(text.count("--hash=sha256:"), len(pins))

    def test_smoke_test_reports_failure_through_exit_status(self):
        import subprocess
        import sys

        code = (
            "import sys, runpy, _https\n"
            "_https.smoke_test = lambda: (_ for _ in ()).throw(OSError('offline'))\n"
            "sys.argv = ['rmtool', '--https-smoke-test']\n"
            "runpy.run_path(sys.argv_path, run_name='__main__')\n"
        ).replace("sys.argv_path", repr(str(ROOT / "rmtool.py")))
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=60,
            env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("HTTPS smoke test failed: offline", result.stderr)


if __name__ == "__main__":
    unittest.main()
