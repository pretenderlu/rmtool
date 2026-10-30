"""Closing the main window while device work runs."""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtGui, QtWidgets

import rmtool
import _dialogs

APP = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class CloseWindowTests(unittest.TestCase):
    def setUp(self):
        self.window = rmtool.MainWindow()
        self.addCleanup(self.window.deleteLater)

    def close(self, *, running, connected, answer):
        event = QtGui.QCloseEvent()
        with mock.patch.object(rmtool.Worker, "running_count", return_value=running), \
                mock.patch.object(self.window.ssh_client, "is_connected", return_value=connected), \
                mock.patch.object(self.window.ssh_client, "close") as close, \
                mock.patch.object(_dialogs, "ask_confirmation", return_value=answer) as ask:
            self.window.closeEvent(event)
        return event, close, ask

    def test_running_work_asks_and_cancel_keeps_the_window_and_connection(self):
        event, close, ask = self.close(running=1, connected=True, answer=False)
        ask.assert_called_once()
        self.assertTrue(ask.call_args.kwargs["danger"])
        self.assertFalse(event.isAccepted())
        close.assert_not_called()

    def test_confirmed_exit_cancels_the_connection(self):
        event, close, _ask = self.close(running=1, connected=True, answer=True)
        self.assertTrue(event.isAccepted())
        close.assert_called_once_with()

    def test_idle_exit_does_not_ask(self):
        event, close, ask = self.close(running=0, connected=True, answer=False)
        ask.assert_not_called()
        self.assertTrue(event.isAccepted())
        close.assert_called_once_with()

    def test_worker_counter_tracks_running_tasks(self):
        seen = []
        worker = rmtool.Worker(lambda: seen.append(rmtool.Worker.running_count()))
        worker.run()
        self.assertEqual(seen, [1])
        self.assertEqual(rmtool.Worker.running_count(), 0)
        failing = rmtool.Worker(lambda: 1 / 0)
        with self.assertLogs(level="ERROR"):
            failing.run()
        self.assertEqual(rmtool.Worker.running_count(), 0)


class ImportSideEffectTests(unittest.TestCase):
    def test_import_does_not_attach_the_application_log(self):
        import logging

        self.assertFalse(any(
            getattr(handler, "_rmtool_app_log", False) for handler in logging.getLogger().handlers
        ))


if __name__ == "__main__":
    unittest.main()
