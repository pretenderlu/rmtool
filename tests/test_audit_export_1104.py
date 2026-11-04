"""Offline regressions for M29, M30 and atomic PDF/diagnostic exports."""

import gc
import json
import os
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
import weakref
import zipfile
from contextlib import closing
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import UUID

from PIL import Image, ImageDraw, PdfParser
from rmscene import (
    AuthorIdsBlock, CrdtId, CrdtSequenceItem, SceneGroupItemBlock,
    SceneLineItemBlock, SceneTreeBlock, TreeNodeBlock, write_blocks,
)
from rmscene import scene_items as si

import rmtool  # noqa: F401 (entry module must precede tab imports)
import _diagnostics
import _ssh
import rmrl


def v5_page(empty=False, layers=1):
    header = b"reMarkable .lines file, version=5         \n"
    data = header + struct.pack("<BBH", layers, 0, 0)
    for _ in range(layers):
        data += struct.pack("<I", 0 if empty else 1)
        if not empty:
            data += struct.pack("<IIIfII", 0, 0, 0, 4.0, 0, 2)
            for x in (80.0, 120.0):
                data += struct.pack("<ffffff", x, 80.0, 0.0, 0.0, 4.0, 1.0)
    return data


def v6_page(xs=()):
    blocks = [
        AuthorIdsBlock({1: UUID(int=1)}),
        TreeNodeBlock(si.Group(node_id=CrdtId(0, 1))),
        SceneTreeBlock(CrdtId(0, 11), CrdtId(0, 0), True, CrdtId(0, 1)),
        TreeNodeBlock(si.Group(node_id=CrdtId(0, 11))),
        SceneGroupItemBlock(
            parent_id=CrdtId(0, 1),
            item=CrdtSequenceItem(CrdtId(0, 13), CrdtId(0, 0), CrdtId(0, 0), 0, CrdtId(0, 11)),
        ),
    ]
    if xs:
        blocks.append(SceneLineItemBlock(
            parent_id=CrdtId(0, 11),
            item=CrdtSequenceItem(
                CrdtId(0, 14), CrdtId(0, 0), CrdtId(0, 0), 0,
                si.Line(
                    color=si.PenColor.BLACK, tool=si.Pen.BALLPOINT_1,
                    points=[si.Point(x, 80.0, 0, 0, 16, 255) for x in xs],
                    thickness_scale=1.0, starting_length=0.0,
                ),
            ),
        ))
    stream = BytesIO()
    write_blocks(stream, blocks)
    return stream.getvalue()


class ExportAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.notebook = self.root / "notebook"
        self.notebook.mkdir()
        self.output = self.root / "export.pdf"

    def notebook_pages(self, payloads):
        identifiers = [f"page-{i}" for i in range(len(payloads))]
        for identifier, payload in zip(identifiers, payloads):
            (self.notebook / f"{identifier}.rm").write_bytes(payload)
        (self.notebook / "doc.content").write_text(json.dumps({
            "pages": identifiers, "pageDimensions": [800, 240],
        }), encoding="utf-8")
        return {path.name: path.read_bytes() for path in self.notebook.iterdir()}

    def render(self):
        rmrl.render_notebook_to_pdf(str(self.notebook), str(self.output), workspace=str(self.root))

    def assert_pdf_pages(self, count):
        with self.output.open("rb") as handle, PdfParser.PdfParser(f=handle) as pdf:
            self.assertEqual(len(pdf.pages), count)
            for reference in pdf.pages:
                page = pdf.read_indirect(reference)
                self.assertEqual(page[b"MediaBox"], [0, 0, 192, 57.6])

    def test_recursive_download_refuses_cross_platform_path_components(self):
        client = _ssh.SSHClientWrapper()
        invalid = (
            "", ".", "..", "../outside", "nested/page.rm", "/absolute.rm",
            "..\\outside", "nested\\page.rm", "\\rooted.rm", "C:page.rm", "C:/page.rm",
            "C:\\page.rm", "\\\\server\\share\\page.rm", "//server/share/page.rm",
            "page.rm:stream", ".. ", "name.", "NUL", "con.txt", "bad\x00name",
        )
        for name in invalid:
            for mode in (stat.S_IFDIR, stat.S_IFREG):
                with self.subTest(name=name, mode=mode):
                    sftp = mock.Mock()
                    sftp.listdir_attr.side_effect = [
                        [SimpleNamespace(filename=name, st_mode=mode)], [],
                    ]
                    with mock.patch.object(client, "_download_atomic") as download, mock.patch.object(Path, "mkdir"):
                        with self.assertRaises(RuntimeError):
                            client._download_directory_recursive(sftp, "/remote", str(self.notebook))
                    download.assert_not_called()
                    self.assertEqual(list(self.notebook.iterdir()), [])

    def test_recursive_download_checks_nested_entries_and_preserves_remote_data(self):
        client = _ssh.SSHClientWrapper()
        payloads = {"/remote/nested/page.rm": b"unchanged"}

        class Sftp:
            def listdir_attr(self, path):
                if path == "/remote":
                    return [SimpleNamespace(filename="nested", st_mode=stat.S_IFDIR)]
                return [SimpleNamespace(filename="page.rm", st_mode=stat.S_IFREG)]

            def stat(self, path):
                return SimpleNamespace(st_size=len(payloads[path]))

            def get(self, remote, local, callback=None):
                Path(local).write_bytes(payloads[remote])

        client._download_directory_recursive(Sftp(), "/remote", str(self.notebook))
        self.assertEqual((self.notebook / "nested/page.rm").read_bytes(), b"unchanged")
        self.assertEqual(payloads, {"/remote/nested/page.rm": b"unchanged"})
        sftp = mock.Mock()
        sftp.listdir_attr.side_effect = [
            [SimpleNamespace(filename="nested", st_mode=stat.S_IFDIR)],
            [SimpleNamespace(filename="..\\outside", st_mode=stat.S_IFREG)],
        ]
        with mock.patch.object(client, "_download_atomic") as download:
            with self.assertRaises(RuntimeError):
                client._download_directory_recursive(sftp, "/remote", str(self.notebook))
        download.assert_not_called()

    def test_recursive_download_refuses_existing_local_directory_link(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.notebook / "nested"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlink unavailable: {exc}")
        sftp = mock.Mock()
        sftp.listdir_attr.side_effect = [
            [SimpleNamespace(filename="nested", st_mode=stat.S_IFDIR)], [],
        ]
        with self.assertRaises(RuntimeError):
            _ssh.SSHClientWrapper()._download_directory_recursive(sftp, "/remote", str(self.notebook))
        self.assertEqual(list(outside.iterdir()), [])

    def test_v6_center_origin_maps_to_page_center_without_fitting_to_ink(self):
        self.notebook_pages([v6_page((-200.0, 200.0))])
        images = []
        render_page = rmrl._render_page

        def capture(*args):
            image = render_page(*args)
            images.append(image.copy())
            return image

        with mock.patch.object(rmrl, "_render_page", side_effect=capture):
            self.render()
        with images[0] as image:
            self.assertLess(image.getpixel((200, 80)), 80)
            self.assertLess(image.getpixel((400, 80)), 80)
            self.assertLess(image.getpixel((600, 80)), 80)
            self.assertEqual(image.getpixel((80, 80)), 255)

    def test_v6_parser_default_coordinate_contract(self):
        page = self.notebook / "page.rm"
        page.write_bytes(v6_page((0.0, 100.0)))
        layers, bounds = rmrl._parse_rm(page)
        self.assertEqual(layers[0].strokes[0].segments[0].x, 702.0)
        self.assertEqual(bounds, (702.0, 80.0, 802.0, 80.0))

    def test_blank_v6_and_v5_pages_export_and_leave_sources_unchanged(self):
        before = self.notebook_pages([v6_page(), v5_page(), v5_page(empty=True), v5_page(layers=0)])
        self.render()
        self.assert_pdf_pages(4)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.notebook.iterdir()})

    @unittest.skipUnless(shutil.which("pdftoppm"), "Poppler is not installed")
    def test_independent_pdf_rasterizer_confirms_page_order_and_pixels(self):
        self.notebook_pages([v5_page(), v6_page(), v6_page((-200.0, 200.0))])
        self.render()
        result = subprocess.run(
            [shutil.which("pdftoppm"), "-r", "300", "-gray", "-png", str(self.output), str(self.root / "raster")],
            check=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.stderr, b"")
        expected_ink = ((100, 80), None, (400, 80))
        for index, point in enumerate(expected_ink, start=1):
            with Image.open(self.root / f"raster-{index}.png") as raster, closing(raster.convert("L")) as image:
                self.assertEqual(image.size, (800, 240))
                if point is None:
                    self.assertEqual(image.getextrema(), (255, 255))
                else:
                    self.assertLess(image.getpixel(point), 80)

    def test_empty_legacy_page_is_valid_but_truncated_pages_still_fail(self):
        path = self.notebook / "page.rm"
        for raw in (struct.pack("<II", 0, 0), struct.pack("<II", 1, 0)):
            path.write_bytes(raw)
            layers, bounds = rmrl._parse_rm(path)
            with rmrl._render_page(rmrl.PageInfo(path, 800, 240), layers, bounds) as image:
                self.assertEqual(image.getextrema(), (255, 255))
        for raw in (v5_page()[:-2], v5_page(layers=0)[:-1], b"damaged"):
            path.write_bytes(raw)
            with self.assertRaises(rmrl.RmrlError):
                rmrl._parse_rm(path)

    def test_highlighter_blends_without_hiding_underlying_ink(self):
        def stroke(color, brush, y, width):
            return rmrl.Stroke(color, brush, [
                rmrl.Segment(80.0, y, width, 1.0, 0.0),
                rmrl.Segment(200.0, y, width, 1.0, 0.0),
            ])

        page = rmrl.PageInfo(Path("unused.rm"), 300, 200)
        layers = [rmrl.Layer([stroke(0, 0, 100, 6), stroke(3, 6, 100, 30)])]
        with rmrl._render_page(page, layers, (80, 100, 200, 100)) as image:
            ink = image.getpixel((140, 100))
            highlight = image.getpixel((140, 110))
            self.assertGreater(ink, 0)
            self.assertLess(ink, highlight)
            self.assertGreater(highlight, rmrl._COLOR_MAP[3])
            self.assertLess(highlight, 255)

    def test_page_canvases_are_closed_before_rendering_the_next_page(self):
        self.notebook_pages([v5_page()] * 60)
        references = []
        render_page = rmrl._render_page

        def track(*args):
            gc.collect()
            for reference in references:
                previous = reference()
                if previous is not None:
                    with self.assertRaises(ValueError):
                        previous.getpixel((0, 0))
            image = render_page(*args)
            references.append(weakref.ref(image))
            return image

        with mock.patch.object(rmrl, "_render_page", side_effect=track):
            self.render()
        self.assert_pdf_pages(60)

    def test_legacy_grayscale_layer_call_remains_supported(self):
        image = Image.new("L", (240, 160), 255)
        self.addCleanup(image.close)
        layer = rmrl.Layer([rmrl.Stroke(0, 0, [
            rmrl.Segment(80, 80, 4, 1, 0), rmrl.Segment(120, 80, 4, 1, 0),
        ])])
        rmrl._render_layer(ImageDraw.Draw(image), layer, 1.0, 0.0, 0.0)
        self.assertEqual(image.getpixel((100, 80)), 0)

    def test_pdf_refuses_to_overwrite_input_page_or_metadata(self):
        for filename in ("page-0.rm", "doc.content"):
            with self.subTest(filename=filename):
                before = self.notebook_pages([v5_page()])
                with self.assertRaises(rmrl.RmrlError):
                    rmrl.render_notebook_to_pdf(str(self.notebook), str(self.notebook / filename))
                self.assertEqual(before, {path.name: path.read_bytes() for path in self.notebook.iterdir()})

    def test_pdf_refuses_to_overwrite_input_archive(self):
        source = self.root / "source.note"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("page.rm", v5_page())
        before = source.read_bytes()
        with self.assertRaises(rmrl.RmrlError):
            rmrl.render_notebook_to_pdf(str(source), str(source), workspace=str(self.root))
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual({path.name for path in self.root.iterdir()}, {"notebook", "source.note"})

    def test_later_corrupt_page_preserves_old_pdf_and_releases_canvas(self):
        self.notebook_pages([v5_page(), b"corrupt"])
        self.output.write_bytes(b"previous PDF")
        images = []
        render_page = rmrl._render_page

        def capture(*args):
            image = render_page(*args)
            images.append(image)
            return image

        with mock.patch.object(rmrl, "_render_page", side_effect=capture):
            with self.assertRaises(rmrl.RmrlError):
                self.render()
        self.assertEqual(self.output.read_bytes(), b"previous PDF")
        self.assertEqual({path.name for path in self.root.iterdir()}, {"notebook", "export.pdf"})
        self.assertEqual(len(images), 1)
        with self.assertRaises(ValueError):
            images[0].getpixel((0, 0))

    def test_pdf_failure_preserves_old_output_cleans_stage_and_allows_retry(self):
        self.notebook_pages([v5_page()] * 2)
        original_save = Image.Image.save
        writes = []

        def fail_second(image, destination, *args, **kwargs):
            stages = list(self.output.parent.glob(".rmrl-*.pdf"))
            self.assertEqual(len(stages), 1)
            writes.append(stages[0])
            self.assertEqual(self.output.read_bytes(), b"previous PDF")
            if len(writes) == 2:
                with stages[0].open("ab") as handle:
                    handle.write(b"partial PDF")
                raise OSError("injected page write failure")
            return original_save(image, destination, *args, **kwargs)

        self.output.write_bytes(b"previous PDF")
        with mock.patch.object(Image.Image, "save", new=fail_second):
            with self.assertRaisesRegex(OSError, "injected"):
                self.render()
        self.assertEqual(self.output.read_bytes(), b"previous PDF")
        self.assertTrue(all(not path.exists() for path in writes))
        self.render()
        self.assert_pdf_pages(2)
        self.assertEqual({path.name for path in self.root.iterdir()}, {"notebook", "export.pdf"})

    def test_pdf_fsync_or_replace_failure_preserves_previous_output(self):
        self.notebook_pages([v5_page()])
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                self.output.write_bytes(b"previous PDF")
                with mock.patch.object(rmrl.os, operation, side_effect=OSError("injected")):
                    with self.assertRaisesRegex(OSError, "injected"):
                        self.render()
                self.assertEqual(self.output.read_bytes(), b"previous PDF")
                self.assertEqual({path.name for path in self.root.iterdir()}, {"notebook", "export.pdf"})

    def test_zip_partial_write_preserves_old_output_cleans_stage_and_allows_retry(self):
        output = self.root / "diagnostics.zip"
        output.write_bytes(b"previous ZIP")
        results = [_diagnostics.CollectedItem(_diagnostics.DiagItem("pc/test.txt", "test"), "payload")]
        original_write = zipfile.ZipFile.writestr
        stages = []

        def fail_after_manifest(archive, name, *args, **kwargs):
            self.assertEqual(output.read_bytes(), b"previous ZIP")
            self.assertNotEqual(Path(archive.filename), output)
            self.assertEqual(Path(archive.filename).parent, output.parent)
            stages.append(Path(archive.filename))
            if name == "pc/test.txt":
                raise OSError("injected ZIP write failure")
            return original_write(archive, name, *args, **kwargs)

        with mock.patch.object(zipfile.ZipFile, "writestr", new=fail_after_manifest):
            with self.assertRaisesRegex(OSError, "injected"):
                _diagnostics.write_bundle(output, results)
        self.assertEqual(output.read_bytes(), b"previous ZIP")
        self.assertTrue(all(not path.exists() for path in stages))
        self.assertEqual(_diagnostics.write_bundle(output, results), output)
        with zipfile.ZipFile(output) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read("pc/test.txt"), b"payload")

    def test_zip_close_fsync_or_replace_failure_preserves_previous_output(self):
        output = self.root / "diagnostics.zip"
        results = [_diagnostics.CollectedItem(_diagnostics.DiagItem("pc/test.txt", "test"), "payload")]
        original_close = zipfile.ZipFile.close

        def close_then_fail(archive):
            opened = archive.fp is not None
            original_close(archive)
            if opened:
                raise OSError("injected close failure")

        patches = [
            mock.patch.object(zipfile.ZipFile, "close", new=close_then_fail),
            mock.patch.object(os, "fsync", side_effect=OSError("injected")),
            mock.patch.object(os, "replace", side_effect=OSError("injected")),
        ]
        for patch in patches:
            output.write_bytes(b"previous ZIP")
            with patch:
                with self.assertRaisesRegex(OSError, "injected"):
                    _diagnostics.write_bundle(output, results)
            self.assertEqual(output.read_bytes(), b"previous ZIP")
            self.assertEqual({path.name for path in self.root.iterdir()}, {"notebook", "diagnostics.zip"})


if __name__ == "__main__":
    unittest.main()
