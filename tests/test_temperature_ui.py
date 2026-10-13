"""Qt 温压页面：明确开始、异步保存、互斥和生命周期；仅 fake reader。"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from PySide6 import QtCore, QtWidgets
from sspp_desktop.temperature_widget import TemperaturePressureWidget
from sspp_experiments.temperature_pressure import SerialCaptureSession
from test_temperature_capture import FakeReader


class TemperatureUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def pump(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Qt fake serial condition timed out")

    def dispose(self, widget):
        widget.close()
        widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_requires_confirmation_then_saves_and_emits_once(self):
        with tempfile.TemporaryDirectory() as temporary, patch("sspp_desktop.temperature_widget.list_serial_ports", return_value=["/dev/cu.FAKE"]):
            reader = FakeReader([b"100,2.7\n150,2.8\n"])
            session = SerialCaptureSession(temporary, lambda port, baud: reader)
            widget = TemperaturePressureWidget(Path(temporary), session=session)
            completed, busy = [], []
            widget.run_completed.connect(completed.append)
            widget.busy_changed.connect(busy.append)
            try:
                widget.sample_name.setText("sample")
                widget.start_capture()
                self.assertFalse(widget.is_running)
                widget.confirm.setChecked(True)
                widget.set_external_busy(True)
                widget.start_capture()
                self.assertFalse(widget.is_running)
                widget.set_external_busy(False)
                widget.start_capture()
                self.assertTrue(widget.is_running)
                self.assertFalse(widget.config_group.isEnabled())
                self.assertTrue(widget.stop_button.isEnabled())
                self.pump(lambda: session.snapshot()["rows"] == 2)
                widget.stop_capture()
                self.pump(lambda: bool(completed))
                widget._poll()
                self.assertEqual(len(completed), 1)
                self.assertEqual(busy, [True, False])
                self.assertTrue(widget.config_group.isEnabled())
                self.assertIn("A2", widget.lines)
                self.assertIn("已停止并保存", widget.status.text())
                self.assertTrue((Path(temporary) / "Data" / completed[0] / "manifest.json").is_file())
            finally:
                self.dispose(widget)
            self.assertTrue(reader.closed)
            self.assertFalse(session.is_running)

    def test_close_during_capture_stops_worker_and_releases_plots(self):
        from shiboken6 import isValid
        with tempfile.TemporaryDirectory() as temporary, patch("sspp_desktop.temperature_widget.list_serial_ports", return_value=["/dev/cu.FAKE"]):
            reader = FakeReader([b"100,2.7\n"])
            session = SerialCaptureSession(temporary, lambda port, baud: reader)
            widget = TemperaturePressureWidget(Path(temporary), session=session)
            widget.sample_name.setText("sample")
            widget.confirm.setChecked(True)
            widget.start_capture()
            self.pump(lambda: session.snapshot()["rows"] == 1)
            plot = widget.voltage_plot
            self.dispose(widget)
            self.assertFalse(session.is_running)
            self.assertTrue(reader.closed)
            self.assertFalse(isValid(plot))

    def test_qt_process_exits_cleanly_after_temperature_widget_cycles(self):
        program = textwrap.dedent("""
            import tempfile
            from pathlib import Path
            from unittest.mock import patch
            from PySide6 import QtCore, QtWidgets
            from sspp_desktop.temperature_widget import TemperaturePressureWidget
            app = QtWidgets.QApplication([])
            with tempfile.TemporaryDirectory() as root, patch('sspp_desktop.temperature_widget.list_serial_ports', return_value=[]):
                for _ in range(3):
                    widget = TemperaturePressureWidget(Path(root))
                    widget.close()
                    widget.deleteLater()
                    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                    app.processEvents()
            print('temperature Qt cleanup passed')
        """)
        result = subprocess.run([sys.executable, "-B", "-X", "faulthandler", "-c", program],
                                cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("temperature Qt cleanup passed", result.stdout)
        self.assertNotIn("shared QObject was deleted", result.stderr)


if __name__ == "__main__":
    unittest.main()
