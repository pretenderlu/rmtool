"""Executable checks for the verified image and SSH dependency updates."""

import unittest
import socket
import threading
from importlib.metadata import version
from io import BytesIO
from pathlib import Path

import paramiko
from packaging.requirements import Requirement
from packaging.version import Version
from PIL import Image, PdfParser


ROOT = Path(__file__).resolve().parents[1]


class DependencyAuditTests(unittest.TestCase):
    def test_installed_versions_match_verified_direct_pins(self):
        requirements = {
            item.name.lower(): item
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
            for item in [Requirement(line)]
        }
        for name, minimum in (("pillow", "13.0.0"), ("paramiko", "5.0.1")):
            with self.subTest(package=name):
                installed = Version(version(name))
                self.assertIn(installed, requirements[name].specifier)
                self.assertGreaterEqual(installed, Version(minimum))

    def test_rsa_default_signature_uses_sha512_and_verifies(self):
        key = paramiko.RSAKey.generate(2048)
        signature = key.sign_ssh_data(b"rmtool dependency audit")
        self.assertEqual(paramiko.Message(signature.asbytes()).get_text(), "rsa-sha2-512")
        self.assertTrue(key.verify_ssh_sig(
            b"rmtool dependency audit", paramiko.Message(signature.asbytes()),
        ))

    def test_rsa_sha256_still_works_but_sha1_is_rejected(self):
        key = paramiko.RSAKey.generate(2048)
        signature = key.sign_ssh_data(b"audit", algorithm="rsa-sha2-256")
        self.assertTrue(key.verify_ssh_sig(b"audit", paramiko.Message(signature.asbytes())))
        with self.assertRaises(KeyError):
            key.sign_ssh_data(b"audit", algorithm="ssh-rsa")
        self.assertEqual(key.get_name(), "ssh-rsa")

    def test_image_roundtrip_and_thumbnail(self):
        for image_format in ("PNG", "JPEG", "BMP", "WEBP"):
            with self.subTest(image_format=image_format):
                stream = BytesIO()
                with Image.new("RGB", (64, 32), "white") as source:
                    source.save(stream, format=image_format)
                stream.seek(0)
                with Image.open(stream, formats=(image_format,)) as decoded:
                    decoded.load()
                    decoded.thumbnail((16, 16))
                    self.assertEqual(decoded.size, (16, 8))

    def test_pdf_file_object_save_and_append(self):
        stream = BytesIO()
        with Image.new("RGB", (32, 16), "white") as page:
            page.save(stream, format="PDF")
            page.save(stream, format="PDF", append=True)
        with PdfParser.PdfParser(buf=stream.getvalue()) as pdf:
            self.assertEqual(len(pdf.pages), 2)

    def test_rsa_host_key_negotiates_sha256_and_password_over_real_transport(self):
        key = paramiko.RSAKey.generate(2048)
        server_socket, client_socket = socket.socketpair()
        server = paramiko.Transport(server_socket)
        server.get_security_options().key_types = ("rsa-sha2-256",)
        server.add_server_key(key)
        finished = threading.Event()
        errors = []

        class PasswordServer(paramiko.ServerInterface):
            def check_auth_password(self, username, password):
                return paramiko.AUTH_SUCCESSFUL if (username, password) == ("root", "test") else paramiko.AUTH_FAILED

        def serve():
            try:
                server.start_server(server=PasswordServer())
                finished.wait(10)
            except Exception as exc:
                errors.append(exc)
            finally:
                server.close()

        worker = threading.Thread(target=serve)
        worker.start()
        client = paramiko.SSHClient()
        client.get_host_keys().add("loopback", "ssh-rsa", key)
        try:
            client.connect(
                "loopback", sock=client_socket, username="root", password="test",
                look_for_keys=False, allow_agent=False, timeout=5,
            )
            self.assertTrue(client.get_transport().is_authenticated())
            self.assertEqual(client.get_transport().host_key_type, "rsa-sha2-256")
        finally:
            client.close()
            client_socket.close()
            finished.set()
            server.close()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
