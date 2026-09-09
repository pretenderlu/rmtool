import ast
import ssl
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from unittest import mock

import _https


ROOT = Path(__file__).resolve().parents[1]


class HttpsTests(unittest.TestCase):
    def tearDown(self):
        _https.ssl_context.cache_clear()

    def test_context_uses_certifi_and_keeps_strict_verification(self):
        with mock.patch.object(
            _https.ssl, "create_default_context", return_value=mock.sentinel.context
        ) as create_context:
            self.assertIs(_https.ssl_context(), mock.sentinel.context)
        create_context.assert_called_once_with(cafile=_https.certifi.where())

        _https.ssl_context.cache_clear()
        context = _https.ssl_context()

        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)

    def test_urlopen_supplies_shared_context(self):
        request = mock.sentinel.request
        context = mock.sentinel.context
        with (
            mock.patch.object(_https, "ssl_context", return_value=context),
            mock.patch.object(
                _https.urllib.request, "urlopen", return_value=mock.sentinel.response
            ) as urlopen,
        ):
            response = _https.urlopen(request, timeout=12)

        self.assertIs(response, mock.sentinel.response)
        urlopen.assert_called_once_with(request, timeout=12, context=context)

    def test_custom_opener_gets_the_same_context(self):
        handler = mock.sentinel.redirect_handler
        context = mock.sentinel.context
        opener = mock.Mock()
        with (
            mock.patch.object(_https, "ssl_context", return_value=context),
            mock.patch.object(_https.urllib.request, "HTTPSHandler") as https_handler,
            mock.patch.object(
                _https.urllib.request, "build_opener", return_value=opener
            ) as build_opener,
        ):
            _https.urlopen("https://example.test", timeout=7, handlers=(handler,))

        https_handler.assert_called_once_with(context=context)
        build_opener.assert_called_once_with(https_handler.return_value, handler)
        opener.open.assert_called_once_with("https://example.test", timeout=7)

    def test_smoke_test_covers_all_public_https_sources(self):
        responses = [mock.MagicMock() for _ in _https.SMOKE_URLS]
        with mock.patch.object(_https, "urlopen", side_effect=responses) as urlopen:
            _https.smoke_test()

        self.assertEqual(
            {urlsplit(call.args[0].full_url).netloc for call in urlopen.call_args_list},
            {
                "github.com",
                "rmtool-localization-1254761827.cos.ap-shanghai.myqcloud.com",
                "remarkable-software.s3.us-east-2.amazonaws.com",
            },
        )
        for response in responses:
            response.__enter__.return_value.read.assert_called_once_with(1)

    def test_all_application_urllib_open_calls_are_centralized(self):
        direct_calls = []
        paths = [*ROOT.glob("*.py"), *(ROOT / "rmrl").rglob("*.py")]
        for path in paths:
            if path.name == "_https.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                    centralized = (
                        isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "_https"
                    )
                elif isinstance(node.func, ast.Name):
                    name = node.func.id
                    centralized = False
                else:
                    continue
                if name in {"urlopen", "build_opener", "urlretrieve"} and not centralized:
                    direct_calls.append(f"{path.relative_to(ROOT)}:{node.lineno}:{name}")

        self.assertEqual(direct_calls, [])


if __name__ == "__main__":
    unittest.main()
