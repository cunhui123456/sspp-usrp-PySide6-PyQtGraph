"""离线工作台：真实 QProcess 协议/取消、标注请求与 Qt 生命周期。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from PySide6 import QtCore, QtWidgets

from sspp_desktop.analysis_bridge import AnalysisBridge, validate_payload
from sspp_desktop.analysis_workbench_widget import AnalysisWorkbenchWidget


class QtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def wait(self, condition, timeout=4):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if condition():
                return
            time.sleep(.005)
        self.fail("Qt 条件超时")

    def flush(self):
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents()


class BridgeTests(QtTests):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sspp_bridge_")
        self.project = Path(self.temp.name)
        worker = self.project / "rf_analysis/workbench"
        worker.mkdir(parents=True)
        (worker.parent / "__init__.py").write_text("")
        (worker / "__init__.py").write_text("")
        self.worker = worker / "worker.py"
        self.bridge = AnalysisBridge(self.project, sys.executable)
        self.values, self.errors, self.busy = [], [], []
        self.bridge.succeeded.connect(self.values.append)
        self.bridge.failed.connect(self.errors.append)
        self.bridge.busy_changed.connect(self.busy.append)
        self.request = {"action": "inspect", "captures": [str(self.project)], "parameters": {}}

    def tearDown(self):
        self.assertTrue(self.bridge.shutdown())
        self.bridge.deleteLater()
        self.flush()
        self.temp.cleanup()

    def test_real_json_roundtrip_environment_and_no_main_thread_block(self):
        self.worker.write_text("import json,sys,time,os\nr=json.load(sys.stdin)\ntime.sleep(.15)\nprint(json.dumps({'ok':True,'action':r['action'],'result':{'captures':r['captures'],'python':sys.executable,'path':os.environ['PYTHONPATH']}}))\n")
        ticked = []
        QtCore.QTimer.singleShot(10, lambda: ticked.append(True))
        self.assertTrue(self.bridge.start(self.request))
        self.assertFalse(self.bridge.start(self.request))
        self.wait(lambda: bool(self.values))
        self.assertTrue(ticked)
        self.assertEqual(self.values[0]["result"]["captures"], self.request["captures"])
        self.assertEqual(self.values[0]["result"]["python"], sys.executable)
        self.assertEqual(self.values[0]["result"]["path"], str(self.project))
        self.assertEqual(self.busy, [True, False])
        self.assertFalse(self.errors)

    def test_structured_failure_and_bad_stdout(self):
        self.worker.write_text("import json,sys\njson.load(sys.stdin)\nprint(json.dumps({'ok':False,'error':{'code':'no_labels','message':'缺少标签','hint':'补充实测标签'}}))\nsys.exit(2)\n")
        self.bridge.start(self.request)
        self.wait(lambda: bool(self.errors))
        self.assertIn("补充实测标签", self.errors[-1])
        self.worker.write_text("import json,sys\njson.load(sys.stdin)\nprint('diagnostic noise')\nprint(json.dumps({'ok':True}))\n")
        self.bridge.start(self.request)
        self.wait(lambda: len(self.errors) == 2)
        self.assertIn("有效 JSON", self.errors[-1])
        self.assertFalse(self.values)

    def test_cancel_timeout_and_shutdown_are_bounded(self):
        self.worker.write_text("import json,sys,time\njson.load(sys.stdin)\ntime.sleep(20)\n")
        self.bridge.start(self.request, timeout_ms=80)
        self.wait(lambda: bool(self.errors))
        self.assertIn("超时", self.errors[-1])
        self.bridge.start(self.request)
        QtCore.QTimer.singleShot(40, self.bridge.cancel)
        self.wait(lambda: len(self.errors) == 2)
        self.assertIn("取消", self.errors[-1])
        self.bridge.start(self.request)
        started = time.monotonic()
        self.assertTrue(self.bridge.shutdown())
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(self.bridge.is_running)
        self.assertFalse(self.bridge.start(self.request))

    def test_payload_and_environment_validation(self):
        for value in ({"action": "unknown"}, {"action": "inspect", "captures": ["relative/run"]},
                      {"action": "inspect", "captures": [str(self.project)], "parameters": {"x": float("nan")}}):
            with self.assertRaises(ValueError):
                validate_payload(value)
        self.bridge.configure(self.project / "missing", sys.executable)
        self.assertFalse(self.bridge.start(self.request))
        self.assertIn("分析项目", self.errors[-1])
        self.assertFalse(self.bridge.is_running)

    def test_output_limit_stops_worker(self):
        self.bridge.max_output_bytes = 1000
        self.worker.write_text("import json,sys,time\njson.load(sys.stdin)\nprint('a'*2000,flush=True)\ntime.sleep(10)\n")
        self.bridge.start(self.request)
        self.wait(lambda: bool(self.errors))
        self.assertIn("输出超过", self.errors[-1])


class FakeBridge(QtCore.QObject):
    succeeded = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    busy_changed = QtCore.Signal(bool)

    def __init__(self):
        super().__init__()
        self.project = Path("/private/tmp/rf-analysis")
        self.python_executable = Path(sys.executable)
        self.is_running = False
        self.requests = []
        self.inventory = {"sources": [], "models": []}
        self.closed = False

    def configure(self, project, python):
        self.project, self.python_executable = Path(project), Path(python)

    def start(self, request, timeout_ms=None):
        if self.closed or self.is_running:
            return False
        self.requests.append(request)
        self.is_running = True
        self.busy_changed.emit(True)
        if request["action"] == "list":
            QtCore.QTimer.singleShot(0, lambda: self.complete({"ok": True, "action": "list", "result": self.inventory}))
        return True

    def complete(self, value):
        self.is_running = False
        self.busy_changed.emit(False)
        self.succeeded.emit(value)

    def cancel(self):
        self.is_running = False
        self.busy_changed.emit(False)
        self.failed.emit("已取消分析")

    def shutdown(self):
        self.closed = True
        self.is_running = False
        return True


def source(name, kind="sweep", eligible=True, **extra):
    folder = {"sweep": "runs", "phase": "phase_runs", "sensor": "temperature_pressure_runs"}[kind]
    return {"capture": f"/private/tmp/desktop/Data/{folder}/{name}", "label": name, "kind": kind,
            "status": "complete" if eligible else "failed", "eligible": eligible,
            "reason": None if eligible else "尚未收口", **extra}


class WorkbenchUiTests(QtTests):
    def setUp(self):
        self.bridge = FakeBridge()
        self.widget = AnalysisWorkbenchWidget(Path("/private/tmp/desktop"), bridge=self.bridge)
        self.widget.show()
        self.wait(lambda: bool(self.bridge.requests) and not self.bridge.is_running)

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        self.bridge.deleteLater()
        self.flush()

    def inventory(self, sources, models=()):
        self.bridge.inventory = {"sources": sources, "models": list(models)}
        self.widget.refresh_sources()
        self.wait(lambda: not self.bridge.is_running)

    def test_no_data_guidance_and_incomplete_sources_disabled(self):
        self.assertIn("先在对应实验页采集", self.widget.source_notice.text())
        self.assertFalse(self.widget.run_button.isEnabled())
        self.inventory([source("failed", eligible=False)])
        item = self.widget.source_table.item(0, 0)
        self.assertFalse(item.flags() & QtCore.Qt.ItemFlag.ItemIsEnabled)
        self.widget.select_source(item.data(QtCore.Qt.ItemDataRole.UserRole))
        self.assertFalse(self.widget.selected)
        self.assertIn("收口", self.widget.status.text())

    def test_material_labels_and_run_level_validation_request(self):
        sources = [source(f"sample_{i}") for i in range(4)]
        self.inventory(sources)
        self.widget.selected = {r["capture"] for r in sources}
        with self.assertRaisesRegex(ValueError, "训练数据"):
            self.widget.build_request("material_train")
        for i, row in enumerate(sources):
            self.widget._labels[row["capture"]] = "water" if i < 2 else "empty"
            self.widget._roles[row["capture"]] = "training" if i % 2 == 0 else "validation"
        payload = self.widget.build_request("material_train")
        self.assertEqual(set(payload["parameters"]["labels"].values()), {"water", "empty"})
        self.assertEqual(len(payload["parameters"]["validation_runs"]), 2)
        self.widget.advanced.setPlainText('{"labels": {}}')
        with self.assertRaisesRegex(ValueError, "表格"):
            self.widget.build_request("material_train")

    def test_annotations_follow_source_and_finger_model_validation_filters_roles(self):
        a, b = source("finger_a", "phase"), source("finger_b", "phase")
        self.inventory([a, b])
        self.widget.select_source(a["capture"])
        self.widget.initial_distance.setText("10")
        self.widget.add_annotation("finger", {"start_s": 1, "end_s": 2, "role": "calibration", "distance_mm": 10})
        self.widget.add_annotation("finger", {"start_s": 3, "end_s": 4, "role": "validation", "distance_mm": 8})
        payload = self.widget.build_request("finger_calibrate")
        self.assertEqual(payload["parameters"]["initial_distance_mm"], 10)
        self.assertEqual(len(payload["parameters"]["annotations"]), 2)
        self.widget.model.addItem("model", "/private/tmp/model.json")
        self.widget.model.setCurrentIndex(1)
        validation = self.widget.build_request("finger_validate")
        self.assertEqual([r["role"] for r in validation["parameters"]["annotations"]], ["validation"])
        self.widget.select_source(b["capture"])
        self.assertEqual(self.widget.finger_annotations.rowCount(), 0)
        self.widget.select_source(a["capture"])
        self.assertEqual(self.widget.finger_annotations.rowCount(), 2)

    def test_sensor_channels_units_and_reference_rows(self):
        capture = source("sensor", "sensor", channels=["S6", "S7"])
        self.inventory([capture])
        self.widget.select_source(capture["capture"])
        self.assertEqual(self.widget.sensor_channel.currentText(), "S6")
        self.assertIsNone(self.widget.source_table.cellWidget(0, 4))
        self.widget.add_annotation("sensor", {"start_s": 1, "end_s": 2, "role": "calibration", "reference": 20})
        self.widget.add_annotation("sensor", {"start_s": 3, "end_s": 4, "role": "validation", "reference": 25})
        with self.assertRaisesRegex(ValueError, "单位"):
            self.widget.build_request("sensor_calibrate")
        self.widget.reference_unit.setText("degC")
        payload = self.widget.build_request("sensor_calibrate")
        self.assertEqual(payload["parameters"]["channel"], "S6")
        self.assertEqual(payload["parameters"]["reference_unit"], "degC")
        self.assertEqual(payload["parameters"]["annotations"][0]["capture"], capture["capture"])
        self.assertEqual(self.widget.build_request("response")["parameters"]["input_field"], "adc")
        self.assertEqual(payload["parameters"]["time_field"], "mcu_elapsed_s")
        self.assertEqual(payload["parameters"]["annotations"][0]["time_segment"], 0)

    def test_run_switch_restores_initial_distance_and_sensor_unit(self):
        a = source("finger_a", "phase", annotation_settings={"initial_distance_mm": 10})
        b = source("finger_b", "phase", annotation_settings={"initial_distance_mm": 25})
        s1 = source("s1", "sensor", channels=["A2"], annotation_draft={"parameters": {"reference_unit": "kPa"}})
        s2 = source("s2", "sensor", channels=["S6"], annotation_draft={"parameters": {"reference_unit": "degC"}})
        self.inventory([a, b, s1, s2])
        self.widget.select_source(a["capture"])
        self.assertEqual(self.widget.initial_distance.text(), "10")
        self.widget.initial_distance.setText("12")
        self.widget.select_source(b["capture"])
        self.assertEqual(self.widget.initial_distance.text(), "25")
        self.widget.select_source(a["capture"])
        self.assertEqual(self.widget.initial_distance.text(), "12")
        self.widget.select_source(s1["capture"])
        self.assertEqual(self.widget.reference_unit.text(), "kPa")
        self.widget.select_source(s2["capture"])
        self.assertEqual(self.widget.reference_unit.text(), "degC")

    def test_sensor_validation_model_unit_channel_and_field_bound(self):
        capture = source("sensor", "sensor", channels=["S6"])
        model = {"path": "/private/tmp/sensor/model.json", "kind": "sensor", "active": True,
                 "channel": "S6", "reference_unit": "degC", "input_field": "voltage_v"}
        self.inventory([capture], [model])
        self.widget.select_source(capture["capture"])
        self.widget.model.setCurrentIndex(1)
        self.assertEqual(self.widget.reference_unit.text(), "degC")
        self.assertEqual(self.widget.input_field.currentData(), "voltage_v")
        self.widget.add_annotation("sensor", {"start_s": 1, "end_s": 2, "role": "validation", "reference": 25})
        self.widget.reference_unit.setText("kPa")
        with self.assertRaisesRegex(ValueError, "单位必须与模型一致"):
            self.widget.build_request("sensor_validate")

    def test_derived_draft_restored_and_missing_reference_can_save(self):
        capture = source("sensor", "sensor", channels=["A2"], annotation_draft={"parameters": {
            "reference_unit": "kPa", "annotations": [{"start_s": 1, "end_s": 2, "role": "calibration", "reference": None}]}})
        self.inventory([capture])
        self.widget.select_source(capture["capture"])
        self.assertEqual(self.widget.sensor_annotations.rowCount(), 1)
        self.assertEqual(self.widget.reference_unit.text(), "kPa")
        payload = self.widget.build_request("save_annotations")
        self.assertIsNone(payload["parameters"]["annotations"][0]["reference"])
        with self.assertRaisesRegex(ValueError, "参考值"):
            self.widget.build_request("sensor_calibrate")

    def test_request_failure_busy_and_shutdown_do_not_restart(self):
        capture = source("one")
        self.inventory([capture])
        self.widget.select_source(capture["capture"])
        self.widget.run_action("response")
        self.assertTrue(self.widget.is_running)
        self.assertFalse(self.widget.source_table.isEnabled())
        self.assertTrue(self.widget.cancel_button.isEnabled())
        self.widget.refresh_sources()
        self.assertTrue(self.widget.shutdown())
        before = len(self.bridge.requests)
        self.bridge.complete({"ok": True, "action": "response", "result": {"charts": []}})
        self.widget.refresh_sources()
        self.app.processEvents()
        self.assertEqual(len(self.bridge.requests), before)

    def test_chart_results_and_model_active_state_are_shown(self):
        self.bridge.complete({"ok": True, "action": "response", "analysis_dir": "/private/tmp/results", "result": {
            "status": "complete", "charts": [{"id": "voltage", "title": "原始电压", "kind": "line", "xlabel": "s", "ylabel": "V",
                "series": [{"label": "A2", "data": [[0, .1], [1, .2]]}]}]}, "artifacts": [{"path": "/private/tmp/results/result.json", "kind": "json"}]})
        self.assertEqual(self.widget.chart_choice.count(), 1)
        self.assertEqual(self.widget.plot.model["ylabel"], "V")
        self.assertTrue(self.widget.open_result_button.isEnabled())
        self.assertEqual(self.widget.artifacts.count(), 1)
        self.bridge.complete({"ok": True, "action": "material_train", "model_dir": "/private/tmp/model_ready", "result": {"status": "ready"}})
        self.assertIn("已激活", self.widget.model.currentText())
        self.bridge.complete({"ok": True, "action": "material_train", "model_dir": "/private/tmp/model_failed", "result": {"status": "validation_failed"}})
        self.assertIn("未激活", self.widget.model.currentText())
        self.assertIn("未通过", self.widget.status.text())


if __name__ == "__main__":
    unittest.main()
