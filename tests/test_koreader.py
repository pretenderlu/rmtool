import io
import os
import posixpath
import shlex
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtWidgets

import rmtool  # must load before tab modules (see test_rmtool_behaviors.py)
import _koreader
import _tab_koreader


_APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

MTIME = 1700000000


class FakeSFTP:
    def __init__(self, ssh):
        self.ssh = ssh

    def put(self, local_path, remote_path, callback=None):
        data = Path(local_path).read_bytes()
        self.ssh.files[posixpath.normpath(remote_path)] = data
        if callback:
            callback(len(data), len(data))


class FakeSSH:
    """In-memory remote filesystem faking the SSHClientWrapper surface."""

    def __init__(self, *, connected=True):
        self._connected = connected
        self.files = {}
        self.dirs = {"/", "/home", "/home/root"}
        self.commands = []

    # -- helpers -------------------------------------------------------------
    def add_file(self, path, data=b"x"):
        path = posixpath.normpath(path)
        self.files[path] = data
        parent = posixpath.dirname(path)
        while parent and parent not in self.dirs:
            self.dirs.add(parent)
            parent = posixpath.dirname(parent)

    def add_dir(self, path):
        path = posixpath.normpath(path)
        while path and path not in self.dirs:
            self.dirs.add(path)
            path = posixpath.dirname(path)

    def install_official(self):
        self.add_file(posixpath.join(_koreader.OFFICIAL_INSTALL_DIR, "koreader.sh"))

    def install_toltec(self):
        self.add_file(posixpath.join(_koreader.TOLTEC_INSTALL_DIR, "koreader.sh"))

    def install_appload(self):
        self.add_dir(_koreader.APPLOAD_INSTALL_DIR)

    # -- SSHClientWrapper surface ---------------------------------------------
    def is_connected(self):
        return self._connected

    def exec_command(self, command):
        self.commands.append(command)
        parts = shlex.split(command)
        if parts[:2] == ["test", "-f"]:
            return "", "", 0 if posixpath.normpath(parts[2]) in self.files else 1
        if parts[:2] == ["test", "-d"]:
            return "", "", 0 if posixpath.normpath(parts[2]) in self.dirs else 1
        if parts[:3] == ["rm", "-f", "--"]:
            path = posixpath.normpath(parts[3])
            if path not in self.files:
                return "", "no such file", 1
            del self.files[path]
            return "", "", 0
        if parts[:3] == ["rm", "-rf", "--"]:
            path = posixpath.normpath(parts[3])
            if path not in self.dirs and path not in self.files:
                return "", "no such file", 1
            self.files = {
                key: value
                for key, value in self.files.items()
                if key != path and not key.startswith(path + "/")
            }
            self.dirs = {
                key
                for key in self.dirs
                if key != path and not key.startswith(path + "/")
            }
            return "", "", 0
        if parts[:2] == ["mkdir", "--"]:
            path = posixpath.normpath(parts[2])
            if path in self.dirs or path in self.files:
                return "", "exists", 1
            self.add_dir(path)
            return "", "", 0
        raise AssertionError(f"Unexpected command: {command}")

    def exec_checked(self, command):
        stdout, stderr, code = self.exec_command(command)
        if code != 0:
            raise RuntimeError(f"命令执行失败: {stderr.strip() or stdout.strip()}")
        return stdout

    def file_exists(self, path):
        path = posixpath.normpath(path)
        return path in self.files or path in self.dirs

    def listdir_attr(self, path):
        path = posixpath.normpath(path)
        if path not in self.dirs:
            raise IOError(f"No such directory: {path}")
        entries = []
        children = {}
        for directory in self.dirs:
            if directory != path and posixpath.dirname(directory) == path:
                children[posixpath.basename(directory)] = True
        for file_path in self.files:
            if posixpath.dirname(file_path) == path:
                children[posixpath.basename(file_path)] = False
        for name, is_dir in children.items():
            full = posixpath.join(path, name)
            entries.append(
                SimpleNamespace(
                    filename=name,
                    st_mode=(stat.S_IFDIR if is_dir else stat.S_IFREG) | 0o755,
                    st_size=0 if is_dir else len(self.files[full]),
                    st_mtime=MTIME,
                )
            )
        return entries

    @contextmanager
    def sftp_session(self):
        yield FakeSFTP(self)

    def open_remote(self, path, mode="r"):
        @contextmanager
        def _remote_file():
            key = posixpath.normpath(path)
            if key not in self.files:
                raise IOError(f"No such file: {path}")
            yield io.BytesIO(self.files[key])

        return _remote_file()

    def download_file(self, remote_path, local_path, callback=None):
        data = self.files[posixpath.normpath(remote_path)]
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(data)
        if callback:
            callback(len(data), len(data))


class DetectionTests(unittest.TestCase):
    def test_detects_official_install(self):
        ssh = FakeSSH()
        ssh.install_official()
        self.assertEqual(
            _koreader.detect_installation(ssh), _koreader.OFFICIAL_INSTALL_DIR
        )

    def test_detects_toltec_install(self):
        ssh = FakeSSH()
        ssh.install_toltec()
        self.assertEqual(
            _koreader.detect_installation(ssh), _koreader.TOLTEC_INSTALL_DIR
        )

    def test_detects_appload_install(self):
        ssh = FakeSSH()
        ssh.install_appload()
        self.assertEqual(
            _koreader.detect_installation(ssh), _koreader.APPLOAD_INSTALL_DIR
        )

    def test_toltec_wins_over_official(self):
        ssh = FakeSSH()
        ssh.install_official()
        ssh.install_toltec()
        self.assertEqual(
            _koreader.detect_installation(ssh), _koreader.TOLTEC_INSTALL_DIR
        )

    def test_not_installed_returns_none_and_require_raises(self):
        ssh = FakeSSH()
        self.assertIsNone(_koreader.detect_installation(ssh))
        with self.assertRaises(RuntimeError) as ctx:
            _koreader.require_installation(ssh)
        self.assertIn("未检测到 KOReader", str(ctx.exception))


class HomeDirParsingTests(unittest.TestCase):
    def test_parses_standard_line(self):
        text = '-- settings\n["home_dir"] = "/home/root/books",\n'
        self.assertEqual(_koreader.parse_home_dir(text), "/home/root/books")

    def test_parses_with_extra_whitespace(self):
        self.assertEqual(
            _koreader.parse_home_dir('[ "home_dir" ]   =   "/mnt/media"'),
            "/mnt/media",
        )

    def test_parses_escaped_characters(self):
        text = '["home_dir"] = "/home/root/my \\"books\\""'
        self.assertEqual(
            _koreader.parse_home_dir(text), '/home/root/my "books"'
        )

    def test_missing_key_returns_none(self):
        self.assertIsNone(_koreader.parse_home_dir('["language"] = "zh_CN"'))

    def test_relative_path_rejected(self):
        self.assertIsNone(_koreader.parse_home_dir('["home_dir"] = "books"'))

    def test_root_and_trailing_slash_normalised(self):
        # A root-only home_dir normalises to empty and falls back.
        self.assertIsNone(_koreader.parse_home_dir('["home_dir"] = "/"'))
        self.assertEqual(
            _koreader.parse_home_dir('["home_dir"] = "/home/root/books/"'),
            "/home/root/books",
        )


class StartDirectoryTests(unittest.TestCase):
    def test_home_dir_from_settings_wins(self):
        ssh = FakeSSH()
        ssh.install_official()
        ssh.add_dir("/mnt/books")
        ssh.add_file(
            posixpath.join(_koreader.OFFICIAL_INSTALL_DIR, "settings.reader.lua"),
            b'["home_dir"] = "/mnt/books",\n',
        )
        self.assertEqual(
            _koreader.resolve_start_directory(ssh, _koreader.OFFICIAL_INSTALL_DIR),
            "/mnt/books",
        )

    def test_falls_back_to_default_books_dir(self):
        ssh = FakeSSH()
        ssh.install_official()
        ssh.add_dir(_koreader.DEFAULT_BOOKS_DIR)
        self.assertEqual(
            _koreader.resolve_start_directory(ssh, _koreader.OFFICIAL_INSTALL_DIR),
            _koreader.DEFAULT_BOOKS_DIR,
        )

    def test_falls_back_to_device_home(self):
        ssh = FakeSSH()
        ssh.install_official()
        self.assertEqual(
            _koreader.resolve_start_directory(ssh, _koreader.OFFICIAL_INSTALL_DIR),
            _koreader.FALLBACK_HOME_DIR,
        )

    def test_unreadable_settings_falls_back(self):
        ssh = FakeSSH()
        ssh.install_toltec()
        ssh.add_dir(_koreader.DEFAULT_BOOKS_DIR)
        self.assertEqual(
            _koreader.resolve_start_directory(ssh, _koreader.TOLTEC_INSTALL_DIR),
            _koreader.DEFAULT_BOOKS_DIR,
        )

    def test_home_dir_pointing_at_xochitl_is_skipped(self):
        ssh = FakeSSH()
        ssh.install_official()
        ssh.add_file(
            posixpath.join(_koreader.OFFICIAL_INSTALL_DIR, "settings.reader.lua"),
            f'["home_dir"] = "{_koreader.XOCHITL_ROOT}",\n'.encode(),
        )
        self.assertEqual(
            _koreader.resolve_start_directory(ssh, _koreader.OFFICIAL_INSTALL_DIR),
            _koreader.FALLBACK_HOME_DIR,
        )


class ListDirectoryTests(unittest.TestCase):
    def test_folders_first_and_sidecars_hidden(self):
        ssh = FakeSSH()
        ssh.add_dir("/books/novels")
        ssh.add_file("/books/alpha.epub", b"aaa")
        ssh.add_file("/books/Beta.pdf", b"bb")
        ssh.add_dir("/books/alpha.epub.sdr")
        ssh.add_file("/books/.hidden", b"h")
        entries = _koreader.list_directory(ssh, "/books")
        self.assertEqual(
            [entry.name for entry in entries], ["novels", "alpha.epub", "Beta.pdf"]
        )
        self.assertTrue(entries[0].is_dir)
        self.assertFalse(entries[1].is_dir)
        self.assertEqual(entries[1].size, 3)
        self.assertEqual(entries[1].mtime, float(MTIME))
        self.assertEqual(entries[1].path, "/books/alpha.epub")

    def test_listing_xochitl_is_rejected(self):
        ssh = FakeSSH()
        with self.assertRaises(RuntimeError):
            _koreader.list_directory(ssh, _koreader.XOCHITL_ROOT)


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.local = os.path.join(self.tmpdir.name, "book.epub")
        Path(self.local).write_bytes(b"epub-bytes")

    def test_upload_success(self):
        ssh = FakeSSH()
        ssh.install_official()
        progress = []
        remote = _koreader.upload_file(
            ssh, self.local, "/books", progress_callback=lambda a, b: progress.append((a, b))
        )
        self.assertEqual(remote, "/books/book.epub")
        self.assertEqual(ssh.files["/books/book.epub"], b"epub-bytes")
        self.assertEqual(progress, [(10, 10)])

    def test_upload_existing_without_overwrite_raises(self):
        ssh = FakeSSH()
        ssh.install_official()
        ssh.add_file("/books/book.epub", b"old")
        with self.assertRaises(RuntimeError) as ctx:
            _koreader.upload_file(ssh, self.local, "/books")
        self.assertIn("同名文件", str(ctx.exception))
        self.assertEqual(ssh.files["/books/book.epub"], b"old")

    def test_upload_overwrite_replaces(self):
        ssh = FakeSSH()
        ssh.install_official()
        ssh.add_file("/books/book.epub", b"old")
        _koreader.upload_file(ssh, self.local, "/books", overwrite=True)
        self.assertEqual(ssh.files["/books/book.epub"], b"epub-bytes")

    def test_upload_into_xochitl_rejected(self):
        ssh = FakeSSH()
        ssh.install_official()
        with self.assertRaises(RuntimeError) as ctx:
            _koreader.upload_file(ssh, self.local, _koreader.XOCHITL_ROOT)
        self.assertIn("xochitl", str(ctx.exception))
        self.assertNotIn(
            posixpath.join(_koreader.XOCHITL_ROOT, "book.epub"), ssh.files
        )

    def test_upload_without_installation_rejected(self):
        ssh = FakeSSH()
        with self.assertRaises(RuntimeError) as ctx:
            _koreader.upload_file(ssh, self.local, "/books")
        self.assertIn("未检测到 KOReader", str(ctx.exception))
        self.assertNotIn("/books/book.epub", ssh.files)


class DownloadTests(unittest.TestCase):
    def test_download_writes_local_file(self):
        ssh = FakeSSH()
        ssh.add_file("/books/book.epub", b"payload")
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "out", "book.epub")
            progress = []
            _koreader.download_file(
                ssh, "/books/book.epub", local,
                progress_callback=lambda a, b: progress.append((a, b)),
            )
            self.assertEqual(Path(local).read_bytes(), b"payload")
            self.assertEqual(progress, [(7, 7)])


class DeleteTests(unittest.TestCase):
    def test_delete_file_removes_sdr_sidecar(self):
        ssh = FakeSSH()
        ssh.install_official()
        ssh.add_file("/books/book.epub")
        ssh.add_dir("/books/book.epub.sdr")
        ssh.add_file("/books/book.epub.sdr/metadata.lua")
        _koreader.delete_entry(ssh, "/books/book.epub", is_dir=False)
        self.assertNotIn("/books/book.epub", ssh.files)
        self.assertNotIn("/books/book.epub.sdr", ssh.dirs)
        self.assertIn("rm -f -- /books/book.epub", ssh.commands)
        self.assertIn("rm -rf -- /books/book.epub.sdr", ssh.commands)

    def test_delete_file_without_sidecar(self):
        ssh = FakeSSH()
        ssh.install_official()
        ssh.add_file("/books/book.epub")
        _koreader.delete_entry(ssh, "/books/book.epub", is_dir=False)
        self.assertNotIn("/books/book.epub", ssh.files)
        self.assertFalse(any("rm -rf" in cmd for cmd in ssh.commands))

    def test_delete_directory_recursive(self):
        ssh = FakeSSH()
        ssh.install_official()
        ssh.add_dir("/books/novels")
        ssh.add_file("/books/novels/a.epub")
        _koreader.delete_entry(ssh, "/books/novels", is_dir=True)
        self.assertNotIn("/books/novels", ssh.dirs)
        self.assertNotIn("/books/novels/a.epub", ssh.files)

    def test_delete_quotes_special_names(self):
        ssh = FakeSSH()
        ssh.install_official()
        path = "/books/my book's \"best\".epub"
        ssh.add_file(path)
        _koreader.delete_entry(ssh, path, is_dir=False)
        self.assertIn(f"rm -f -- {shlex.quote(path)}", ssh.commands)
        self.assertNotIn(posixpath.normpath(path), ssh.files)

    def test_delete_inside_xochitl_rejected(self):
        ssh = FakeSSH()
        ssh.install_official()
        target = posixpath.join(_koreader.XOCHITL_ROOT, "abc.epub")
        ssh.add_file(target)
        with self.assertRaises(RuntimeError):
            _koreader.delete_entry(ssh, target, is_dir=False)
        self.assertIn(posixpath.normpath(target), ssh.files)

    def test_delete_without_installation_rejected(self):
        ssh = FakeSSH()
        ssh.add_file("/books/book.epub")
        with self.assertRaises(RuntimeError) as ctx:
            _koreader.delete_entry(ssh, "/books/book.epub", is_dir=False)
        self.assertIn("未检测到 KOReader", str(ctx.exception))
        self.assertIn("/books/book.epub", ssh.files)


class CreateFolderTests(unittest.TestCase):
    def test_create_folder_success(self):
        ssh = FakeSSH()
        ssh.install_official()
        path = _koreader.create_folder(ssh, "/books", "新 书")
        self.assertEqual(path, "/books/新 书")
        self.assertIn("/books/新 书", ssh.dirs)
        self.assertIn(f"mkdir -- {shlex.quote('/books/新 书')}", ssh.commands)

    def test_create_folder_existing_raises(self):
        ssh = FakeSSH()
        ssh.install_official()
        ssh.add_file("/books/novels")
        with self.assertRaises(RuntimeError) as ctx:
            _koreader.create_folder(ssh, "/books", "novels")
        self.assertIn("已存在", str(ctx.exception))

    def test_create_folder_invalid_names(self):
        ssh = FakeSSH()
        ssh.install_official()
        for bad in ("", "  ", ".", "..", "a/b", "a\\b"):
            with self.assertRaises(RuntimeError, msg=bad):
                _koreader.create_folder(ssh, "/books", bad)

    def test_create_folder_inside_xochitl_rejected(self):
        ssh = FakeSSH()
        ssh.install_official()
        with self.assertRaises(RuntimeError):
            _koreader.create_folder(ssh, _koreader.XOCHITL_ROOT, "books")
        self.assertNotIn(posixpath.join(_koreader.XOCHITL_ROOT, "books"), ssh.dirs)


class NoRestartTests(unittest.TestCase):
    def test_no_service_restart_commands_anywhere(self):
        ssh = FakeSSH()
        ssh.install_official()
        ssh.add_dir("/books")
        ssh.add_file("/books/book.epub")
        ssh.add_dir("/books/book.epub.sdr")
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "new.epub")
            Path(local).write_bytes(b"data")
            _koreader.upload_file(ssh, local, "/books")
            _koreader.delete_entry(ssh, "/books/book.epub", is_dir=False)
            _koreader.create_folder(ssh, "/books", "folder")
        joined = "\n".join(ssh.commands)
        self.assertNotIn("systemctl", joined)
        self.assertNotIn("reboot", joined)
        self.assertNotIn("restart", joined)

    def test_module_sources_contain_no_restart(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("_koreader.py", "_tab_koreader.py"):
            source = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("systemctl", source, name)
            self.assertNotIn("reboot", source, name)


class TabTestBase(unittest.TestCase):
    def setUp(self):
        self.ssh = FakeSSH()
        self.tab = _tab_koreader.KOReaderTab(self.ssh)
        self.addCleanup(self.tab.deleteLater)
        self.addCleanup(QtWidgets.QApplication.processEvents)

    def load(self, install_dir=_koreader.OFFICIAL_INSTALL_DIR, directory="/books",
             entries=()):
        self.tab.set_connection_state(True)
        self.tab._on_listing_loaded((install_dir, directory, list(entries)))


class TabStateTests(TabTestBase):
    def entry(self, name, is_dir=False, directory="/books"):
        return _koreader.KOReaderEntry(
            name=name,
            path=posixpath.join(directory, name),
            size=12,
            mtime=float(MTIME),
            is_dir=is_dir,
        )

    def test_disconnected_disables_actions(self):
        self.assertFalse(self.tab.refresh_button.isEnabled())
        self.assertFalse(self.tab.upload_button.isEnabled())
        self.assertFalse(self.tab.delete_button.isEnabled())
        # Offscreen: the tab itself is never shown, so check the hidden flag.
        self.assertFalse(self.tab.empty_state_label.isHidden())

    def test_not_installed_shows_notice_and_blocks_writes(self):
        self.load(install_dir=None, directory="")
        self.assertIn("未检测到 KOReader", self.tab.empty_state_label.text())
        self.assertTrue(self.tab.refresh_button.isEnabled())
        self.assertFalse(self.tab.upload_button.isEnabled())
        self.assertFalse(self.tab.new_folder_button.isEnabled())
        with mock.patch.object(_tab_koreader, "show_warning") as warning:
            self.tab.upload_books()
            warning.assert_called_once()
        with mock.patch.object(_tab_koreader, "show_warning") as warning:
            self.tab._create_folder()
            warning.assert_called_once()

    def test_listing_fills_table_folders_first(self):
        entries = [
            self.entry("alpha.epub"),
            self.entry("novels", is_dir=True),
            self.entry("Beta.pdf"),
        ]
        # Backend sorts folders first; UI must preserve that order.
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        self.load(entries=entries)
        self.assertEqual(self.tab.table.rowCount(), 3)
        self.assertEqual(self.tab.table.item(0, 0).text(), "novels")
        self.assertEqual(self.tab.table.item(0, 1).text(), "文件夹")
        self.assertEqual(self.tab.path_edit.text(), "/books")

    def test_disconnect_clears_state(self):
        self.load(entries=[self.entry("a.epub")])
        self.tab.set_connection_state(False)
        self.assertEqual(self.tab.table.rowCount(), 0)
        self.assertIsNone(self.tab._install_dir)
        self.assertEqual(self.tab.path_edit.text(), "")

    def test_double_click_folder_navigates(self):
        self.load(entries=[self.entry("novels", is_dir=True)])
        with mock.patch.object(self.tab, "_navigate_to") as navigate:
            self.tab._on_row_double_clicked(self.tab.table.model().index(0, 0))
            navigate.assert_called_once_with("/books/novels")

    def test_navigation_into_xochitl_blocked(self):
        self.load()
        with mock.patch.object(_tab_koreader, "show_warning") as warning, \
                mock.patch.object(self.tab.thread_pool, "start") as start:
            self.tab._navigate_to(_koreader.XOCHITL_ROOT)
            warning.assert_called_once()
            start.assert_not_called()
        self.assertEqual(self.tab.path_edit.text(), "/books")

    def test_relative_manual_path_rejected(self):
        self.load()
        with mock.patch.object(_tab_koreader, "show_warning") as warning, \
                mock.patch.object(self.tab.thread_pool, "start") as start:
            self.tab._navigate_to("books")
            warning.assert_called_once()
            start.assert_not_called()

    def test_selection_enables_actions(self):
        self.load(entries=[self.entry("a.epub"), self.entry("novels", is_dir=True)])
        self.assertFalse(self.tab.delete_button.isEnabled())
        self.assertFalse(self.tab.download_button.isEnabled())
        self.tab.table.selectRow(0)
        self.assertTrue(self.tab.delete_button.isEnabled())
        self.assertTrue(self.tab.download_button.isEnabled())
        self.tab.table.clearSelection()
        self.tab.table.selectRow(1)  # folder only: download stays disabled
        self.assertTrue(self.tab.delete_button.isEnabled())
        self.assertFalse(self.tab.download_button.isEnabled())

    def test_filter_clears_hidden_selection(self):
        self.load(entries=[self.entry("a.epub"), self.entry("b.epub")])
        self.tab.table.selectRow(0)
        self.assertTrue(self.tab.delete_button.isEnabled())
        self.tab.search_edit.setText("b.epub")
        self.assertFalse(self.tab._selected_entries())
        self.assertFalse(self.tab.delete_button.isEnabled())
        self.assertFalse(self.tab.download_button.isEnabled())


class TabDeleteTests(TabTestBase):
    def entry(self, name):
        return _koreader.KOReaderEntry(
            name=name, path=f"/books/{name}", size=1, mtime=None, is_dir=False
        )

    def test_delete_requires_selection(self):
        self.load()
        with mock.patch.object(_tab_koreader, "show_warning") as warning:
            self.tab._delete_entries()
            warning.assert_called_once()

    def test_delete_cancel_runs_no_worker(self):
        self.load(entries=[self.entry("a.epub")])
        self.tab.table.selectRow(0)
        with mock.patch.object(
            _tab_koreader, "ask_confirmation", return_value=False
        ) as confirm, mock.patch.object(self.tab.thread_pool, "start") as start:
            self.tab._delete_entries()
            confirm.assert_called_once()
            self.assertTrue(confirm.call_args.kwargs["danger"])
            start.assert_not_called()

    def test_delete_confirm_starts_worker(self):
        self.load(entries=[self.entry("a.epub")])
        self.tab.table.selectRow(0)
        with mock.patch.object(
            _tab_koreader, "ask_confirmation", return_value=True
        ), mock.patch.object(self.tab.thread_pool, "start") as start:
            self.tab._delete_entries()
            start.assert_called_once()

    def test_perform_delete_calls_backend(self):
        self.ssh.install_official()
        self.ssh.add_file("/books/a.epub")
        self.ssh.add_dir("/books/a.epub.sdr")
        self.tab._perform_delete([self.entry("a.epub")])
        self.assertNotIn("/books/a.epub", self.ssh.files)
        self.assertNotIn("/books/a.epub.sdr", self.ssh.dirs)


class TabUploadTests(TabTestBase):
    def entry(self, name):
        return _koreader.KOReaderEntry(
            name=name, path=f"/books/{name}", size=1, mtime=None, is_dir=False
        )

    def pick_files(self, paths):
        return mock.patch.object(
            QtWidgets.QFileDialog, "getOpenFileNames", return_value=(paths, "")
        )

    def test_upload_no_conflict_skips_confirmation(self):
        self.load()
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "new.epub")
            Path(local).write_bytes(b"data")
            with self.pick_files([local]), mock.patch.object(
                _tab_koreader, "ask_confirmation"
            ) as confirm, mock.patch.object(self.tab.thread_pool, "start") as start:
                self.tab.upload_books()
                confirm.assert_not_called()
                start.assert_called_once()

    def test_upload_conflict_cancel_aborts(self):
        self.load(entries=[self.entry("book.epub")])
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "book.epub")
            Path(local).write_bytes(b"data")
            with self.pick_files([local]), mock.patch.object(
                _tab_koreader, "ask_confirmation", return_value=False
            ), mock.patch.object(self.tab.thread_pool, "start") as start:
                self.tab.upload_books()
                start.assert_not_called()

    def test_upload_conflict_confirm_overwrites(self):
        self.ssh.install_official()
        self.ssh.add_file("/books/book.epub", b"old")
        self.load(entries=[self.entry("book.epub")])
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "book.epub")
            Path(local).write_bytes(b"new-bytes")
            with self.pick_files([local]), mock.patch.object(
                _tab_koreader, "ask_confirmation", return_value=True
            ) as confirm:
                self.tab.upload_books()
                confirm.assert_called_once()
                self.tab.thread_pool.waitForDone(5000)
        QtWidgets.QApplication.processEvents()
        self.assertEqual(self.ssh.files["/books/book.epub"], b"new-bytes")

    def test_perform_upload_rejects_existing_without_overwrite(self):
        self.ssh.install_official()
        self.ssh.add_file("/books/book.epub", b"old")
        with tempfile.TemporaryDirectory() as tmpdir:
            local = os.path.join(tmpdir, "book.epub")
            Path(local).write_bytes(b"data")
            with self.assertRaises(RuntimeError):
                self.tab._perform_upload([local], "/books", False)


class TabDownloadTests(TabTestBase):
    def entry(self, name, is_dir=False):
        return _koreader.KOReaderEntry(
            name=name, path=f"/books/{name}", size=3, mtime=None, is_dir=is_dir
        )

    def test_download_folder_selection_warns(self):
        self.load(entries=[self.entry("novels", is_dir=True)])
        self.tab.table.selectRow(0)
        with mock.patch.object(_tab_koreader, "show_warning") as warning:
            self.tab._download_books()
            warning.assert_called_once()

    def test_download_writes_files(self):
        self.ssh.install_official()
        self.ssh.add_file("/books/a.epub", b"abc")
        self.load(entries=[self.entry("a.epub")])
        self.tab.table.selectRow(0)
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(
                QtWidgets.QFileDialog, "getExistingDirectory", return_value=tmpdir
            ), mock.patch.object(_tab_koreader, "show_info"):
                self.tab._download_books()
                self.tab.thread_pool.waitForDone(5000)
                QtWidgets.QApplication.processEvents()
            self.assertEqual(Path(tmpdir, "a.epub").read_bytes(), b"abc")


class TabConnectionGuardTests(TabTestBase):
    def test_require_connection_blocks_slots(self):
        self.ssh._connected = False
        # require_connection shows its warning through _ssh's own import.
        with mock.patch("_ssh.show_warning") as warning:
            self.assertIsNone(self.tab._delete_entries())
            warning.assert_called_once()
            self.assertIn("请先连接设备", warning.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
