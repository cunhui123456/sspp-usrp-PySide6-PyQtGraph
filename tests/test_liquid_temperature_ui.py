"""液体温度工作区离线 GUI 验收；服务为 fake，不打开任何设备。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import patch

from PySide6 import QtCore, QtWidgets
from sspp_desktop.liquid_temperature_widget import LiquidTemperatureWidget


class FakeService:
    def __init__(self):
        self.calls = []
        self.annotations = []
        self.end = None
        self.gate = None
        self.raise_load = False

    def _call(self, name, *args):
        self.calls.append((name, args, threading.get_ident()))

    def liquid_history(self):
        self._call("history")
        return [{"key": "runs/cold", "mode": "sweep", "label": "冷水", "temperature_c": 20.0},
                {"key": "runs/warm", "mode": "sweep", "label": "温水", "temperature_c": 30.0},
                {"key": "phase_runs/one", "mode": "phase", "label": "位移", "temperature_c": 20.0},
                {"key": "phase_runs/two", "mode": "phase", "label": "温水位移", "temperature_c": 30.0}]

    def liquid_load(self, key):
        self._call("load", key)
        if self.gate:
            self.gate.wait(3)
        if self.raise_load:
            raise ValueError("源记录停止状态未确认")
        return {"key": key, "mode": "phase" if key.startswith("phase_runs/") else "sweep",
                "metadata": {}, "temperature_end_c": self.end, "annotations": deepcopy(self.annotations)}

    def liquid_save_annotations(self, key, annotations, temperature_end_c=None):
        self._call("save", key, annotations, temperature_end_c)
        self.annotations = deepcopy(annotations)
        self.end = temperature_end_c
        return {"saved": True}

    def liquid_analyze_phase(self, key, annotations, temperature_tolerance_c=.5):
        self._call("phase", key, annotations, temperature_tolerance_c)
        return {"status": "ready", "export_path": "/private/tmp/example_report", "errors": [], "warnings": [],
                "blocks": [{"block": "T20-forward", "sensitivity_deg_per_mm": 3,
                            "points": [{"displacement_mm": 0, "delta_phase_deg": 0},
                                       {"displacement_mm": 1, "delta_phase_deg": 3}]}]}

    def liquid_compare_sweeps(self, keys, reference_key, temperature_tolerance_c=.5):
        self._call("sweeps", keys, reference_key, temperature_tolerance_c)
        return {"status": "ready", "export_path": "/private/tmp/example_sweep_report", "charts": [], "warnings": []}

    def liquid_compare_phase_runs(self, keys, temperature_tolerance_c=.5):
        self._call("phase_runs", keys, temperature_tolerance_c)
        return {"status": "ready", "export_path": "/private/tmp/example_phase_comparison", "charts": []}

    def liquid_export(self, path, destination):
        self._call("export", path, destination)
        return {"destination": destination}


class LiquidTemperatureUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.service = FakeService()
        self.widget = LiquidTemperatureWidget(self.service)
        self.errors = []
        self.widget.error.connect(self.errors.append)
        self.main_thread = threading.get_ident()

    def tearDown(self):
        if self.service.gate:
            self.service.gate.set()
        self.pump(lambda: not self.widget.is_busy)
        self.widget.close()
        self.widget.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def pump(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Qt 工作区后台任务未按时完成")

    def load_phase(self):
        self.widget.set_current("phase_runs/one")
        self.pump(lambda: not self.widget.is_busy)
        self.assertEqual(self.widget.current_mode, "phase")

    def add_window(self, start="10", end="12", block="T20-forward"):
        values = {"start_s": start, "end_s": end, "temperature_c": "20.1", "temperature_end_c": "20.2",
                  "distance_mm": "10", "block": block}
        for key, value in values.items():
            self.widget.window_fields[key].setText(value)
        self.widget.recorded.setChecked(True)
        self.widget.add_window()

    def test_preparation_requires_readings_and_only_emits_conditions(self):
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.widget.fields["temperature_c"].text(), "")
        emitted = []
        self.widget.prepare_requested.connect(emitted.append)
        self.widget.prepare()
        self.assertFalse(emitted)
        values = {"liquid": "水", "sample_id": "sample-A", "container": "glass-A", "volume_ml": "10",
                  "geometry": "瓶底平行，线缆固定", "thermometer": "参考温度计A", "target_temperature_c": "30",
                  "temperature_c": "29.8", "distance_mm": "10", "repeat_id": "r1"}
        for key, value in values.items():
            self.widget.fields[key].setText(value)
        self.widget.prepare()
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["mode"], "sweep")
        self.assertEqual(emitted[0]["liquid_temperature"]["temperature_c"], 29.8)
        self.assertEqual(self.service.calls, [])
        self.widget.set_external_busy(True)
        self.widget.prepare()
        self.assertEqual(len(emitted), 1)

    def test_history_and_sweep_reference_are_explicit_and_background(self):
        busy = []
        self.widget.busy_changed.connect(busy.append)
        self.widget.refresh_history()
        self.pump(lambda: not self.widget.is_busy)
        self.assertEqual(self.widget.history.count(), 4)
        self.widget.compare_records()
        self.assertIn("2–8", self.errors[-1])
        for index in (0, 1):
            self.widget.history.item(index).setCheckState(QtCore.Qt.CheckState.Checked)
        self.widget.compare_records()
        self.pump(lambda: not self.widget.is_busy)
        call = next(c for c in self.service.calls if c[0] == "sweeps")
        self.assertEqual(call[1], (["runs/cold", "runs/warm"], "runs/cold", .5))
        self.assertTrue(all(c[2] != self.main_thread for c in self.service.calls))
        self.assertEqual(busy, [True, False, True, False])
        self.assertEqual(self.widget.tabs.currentIndex(), 2)

    def test_phase_comparison_routes_by_mode_and_refuses_unsaved_or_mixed_records(self):
        self.widget.refresh_history()
        self.pump(lambda: not self.widget.is_busy)
        self.widget.history.item(0).setCheckState(QtCore.Qt.CheckState.Checked)
        self.widget.history.item(2).setCheckState(QtCore.Qt.CheckState.Checked)
        self.widget.compare_records()
        self.assertIn("不能混选", self.errors[-1])
        self.widget.history.item(0).setCheckState(QtCore.Qt.CheckState.Unchecked)
        self.widget.history.item(3).setCheckState(QtCore.Qt.CheckState.Checked)
        self.load_phase()
        self.add_window()
        self.widget.compare_records()
        self.assertIn("尚未保存", self.errors[-1])
        self.widget.save_annotations()
        self.pump(lambda: not self.widget.is_busy)
        self.widget.compare_records()
        self.pump(lambda: not self.widget.is_busy)
        call = self.service.calls[-1]
        self.assertEqual(call[0], "phase_runs")
        self.assertEqual(call[1], (["phase_runs/one", "phase_runs/two"], .5))

    def test_phase_annotation_validation_and_versioned_service_flow(self):
        self.load_phase()
        self.widget.add_window()
        self.assertIn("实际记录", self.errors[-1])
        self.add_window()
        self.assertEqual(len(self.widget.annotations), 1)
        self.assertFalse(self.widget.recorded.isChecked())
        self.add_window(start="11", end="13")
        self.assertEqual(len(self.widget.annotations), 1)
        self.assertIn("重叠", self.errors[-1])
        self.widget.set_current("runs/cold")
        self.assertEqual(self.widget.current_key, "phase_runs/one")
        self.assertIn("尚未保存", self.errors[-1])
        self.widget.save_annotations()
        self.pump(lambda: not self.widget.is_busy)
        self.assertFalse(self.widget._dirty)
        self.assertEqual(len(self.service.annotations), 1)
        self.widget.analyze_phase()
        self.pump(lambda: not self.widget.is_busy)
        self.assertEqual(self.widget.chart_choice.count(), 1)
        self.assertEqual(self.widget.plot.model["series"][0]["data"], [[0, 0], [1, 3]])
        self.assertTrue(self.widget.export_button.isEnabled())

    def test_nonfinite_annotation_is_rejected(self):
        self.load_phase()
        self.add_window(start="nan")
        self.assertFalse(self.widget.annotations)
        self.assertIn("有效范围", self.errors[-1])

    def test_sweep_end_temperature_is_required_and_saved(self):
        self.widget.set_current("runs/cold")
        self.pump(lambda: not self.widget.is_busy)
        self.assertFalse(self.widget.phase_group.isEnabled())
        self.widget.save_annotations()
        self.assertIn("实际数值", self.errors[-1])
        self.widget.end_temperature.setText("20.3")
        self.widget.save_annotations()
        self.pump(lambda: not self.widget.is_busy)
        self.assertEqual(self.service.end, 20.3)
        self.assertEqual(self.service.annotations, [])

    def test_export_uses_chosen_destination_in_background(self):
        self.widget._show_report({"status": "ready", "export_path": "/private/tmp/example_report"})
        with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=("/private/tmp/chosen-temperature", "ZIP (*.zip)")):
            self.widget.export_report()
        self.pump(lambda: not self.widget.is_busy)
        call = self.service.calls[-1]
        self.assertEqual(call[1], ("/private/tmp/example_report", "/private/tmp/chosen-temperature.zip"))
        self.assertNotEqual(call[2], self.main_thread)

    def test_failed_load_preserves_current_record_and_recovers_controls(self):
        self.load_phase()
        self.service.raise_load = True
        self.widget.set_current("runs/cold")
        self.pump(lambda: not self.widget.is_busy)
        self.assertEqual(self.widget.current_key, "phase_runs/one")
        self.assertIn("停止状态未确认", self.errors[-1])
        self.assertTrue(self.widget.refresh_button.isEnabled())

    def test_close_waits_for_existing_background_task(self):
        self.service.gate = threading.Event()
        self.widget.set_current("phase_runs/one")
        self.assertFalse(self.widget.request_close())
        self.assertFalse(self.widget.refresh_button.isEnabled())
        self.service.gate.set()
        self.pump(lambda: not self.widget.is_busy)
        self.assertTrue(self.widget.request_close())
        self.assertFalse(self.widget.prepare_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
