import io
import json
import logging
import unittest
from unittest import mock

import _app_update


class Response:
    def __init__(self, payload):
        self.payload = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        return self.payload.read(*args)


class AppUpdateTests(unittest.TestCase):
    def test_returns_newer_public_release(self):
        with mock.patch.object(
            _app_update._https,
            "urlopen",
            return_value=Response({"tag_name": "v1.16.5", "draft": False, "prerelease": False}),
        ):
            self.assertEqual(_app_update.check_for_update("1.16.4"), "v1.16.5")

    def test_ignores_same_old_draft_prerelease_and_malformed_releases(self):
        for payload in (
            {"tag_name": "v1.16.4", "draft": False, "prerelease": False},
            {"tag_name": "v1.16.3", "draft": False, "prerelease": False},
            {"tag_name": "v1.16.5", "draft": True, "prerelease": False},
            {"tag_name": "v1.16.5", "draft": False, "prerelease": True},
            {"tag_name": "revision-7", "draft": False, "prerelease": False},
        ):
            with self.subTest(payload=payload), mock.patch.object(
                _app_update._https, "urlopen", return_value=Response(payload)
            ):
                self.assertIsNone(_app_update.check_for_update("1.16.4"))

    def test_network_failure_is_silent_to_user_and_logged(self):
        with mock.patch.object(
            _app_update._https, "urlopen", side_effect=OSError("offline")
        ), self.assertLogs(level=logging.WARNING) as logs:
            self.assertIsNone(_app_update.check_for_update("1.16.4"))
        self.assertIn("Could not check rmtool updates", "\n".join(logs.output))
