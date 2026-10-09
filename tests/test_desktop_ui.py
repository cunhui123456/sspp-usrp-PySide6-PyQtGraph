"""离线 Qt 验收：线程响应、参数锁、阶段标记、完整诊断与回放展示。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PySide6 import QtCore, QtWidgets
from sspp_desktop.plots import DiagnosticPlot, LivePlots
from sspp_desktop.window import MainWindow
from sspp_desktop.workers import TaskRunner


class FakeService:
    def __init__(self):
        self.settings = SimpleNamespace(root=Path(__file__).resolve().parents[1], ui_interval_ms=30,
                                        preview_interval_ms=100, default_profile="own")
        self.job = None
        self.marked = None
        self.started = None
        self.stopped = False
        self.closed = False
        self.preflight_release = threading.Event()

    def refresh_profiles(self):
        return [{"id": "own", "name": "测试配置", "channel": 0}]

    def history(self):
        return []

    def snapshot(self):
        return {"job": self.job, "logs": [], "events": []}

    def live_snapshot(self):
        return {"mode": None, "points": [], "preview": None, "revision": 0}

    def preflight(self, config):
        self.preflight_release.wait(2)
        return {"valid": True, "summary": "离线参数检查"}

    def start(self, config):
        self.started = config
        self.job = {"status": "running", "config": config, "baseline": {"end_s": 7}}
        return self.snapshot()

    def stop(self):
        self.stopped = True
        if self.job:
            self.job["status"] = "stopped"
            self.job["rf_stopped"] = True
        return self.snapshot()

    def mark(self, label, settle, contact):
        self.marked = (label, settle, contact)
        return {"accepted": True}

    def shutdown(self):
        self.closed = True
        self.preflight_release.set()


class DesktopUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def pump_until(self, predicate, timeout=3):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Qt condition timed out")

    def test_worker_is_bounded_and_callbacks_arrive_on_gui_thread(self):
        runner = TaskRunner()
        release = threading.Event()
        values = []
        gui_thread = threading.get_ident()
        self.assertTrue(runner.submit("one", lambda: (release.wait(2), threading.get_ident()),
                                      lambda result: values.append((result, threading.get_ident()))))
        self.assertFalse(runner.submit("one", lambda: None))
        release.set()
        self.pump_until(lambda: bool(values))
        self.assertNotEqual(values[0][0][1], gui_thread)
        self.assertEqual(values[0][1], gui_thread)
        self.assertFalse(runner.pending)

    def test_urgent_stop_remains_available_when_control_pool_is_full(self):
        runner = TaskRunner()
        release = threading.Event()
        stopped = []
        try:
            for key in ("preflight", "start"):
                self.assertTrue(runner.submit(key, lambda: release.wait(2), lane="control"))
            self.assertFalse(runner.submit("mark", lambda: None, lane="control"))
            self.assertTrue(runner.submit("stop", lambda: stopped.append(True), lane="urgent"))
            self.pump_until(lambda: bool(stopped), .5)
        finally:
            release.set()
            self.pump_until(lambda: not runner.pending)

    def test_parameter_lock_mark_stop_and_nonblocking_preflight(self):
        service = FakeService()
        window = MainWindow(service)
        window.show()
        try:
            self.pump_until(lambda: window.profile.count() == 1)
            self.assertEqual(window.config()["mode"], "phase")
            self.assertEqual(set(window.config()), set(__import__("sspp_lab.controller", fromlist=["DEFAULTS"]).DEFAULTS) | {"profile"})
            window.preflight()
            ticked = []
            QtCore.QTimer.singleShot(10, lambda: ticked.append(True))
            self.pump_until(lambda: bool(ticked), .5)
            self.assertIn("preflight", window.runner.pending)
            service.preflight_release.set()
            self.pump_until(lambda: "preflight" not in window.runner.pending)
            window.start_experiment()
            self.assertIsNone(service.started)  # 接线确认不能跳过。
            window.confirm.setChecked(True)
            window.start_experiment()
            self.pump_until(lambda: service.started is not None and "start" not in window.runner.pending)
            self.assertFalse(window.config_container.isEnabled())
            self.assertTrue(window.stop_button.isEnabled())
            self.assertTrue(window.mark_group.isEnabled())
            window.settle.setValue(2.5)
            window.contact.setChecked(True)
            window.mark("finger_near")
            self.pump_until(lambda: service.marked is not None)
            self.assertEqual(service.marked, ("finger_near", 2.5, True))
            window.stop_experiment()
            self.pump_until(lambda: service.stopped and "stop" not in window.runner.pending)
            self.assertTrue(window.config_container.isEnabled())
        finally:
            service.preflight_release.set()
            service.stop()
            window._apply_status(service.snapshot())
            window.close()
            self.pump_until(lambda: service.closed and not window.isVisible())

    def test_phase_gaps_and_rewind_do_not_bridge_or_leave_future_rounds(self):
        plots = LivePlots()
        plots.update_snapshot({"mode": "phase", "points": [
            {"x": 1, "amplitude": 1, "phase": 10}, {"x": 2, "amplitude": None, "phase": None},
            {"x": 3, "amplitude": 2, "phase": 20}], "display_gaps": 1})
        self.assertTrue(np.isnan(plots.amplitude_line.yData[1]))
        self.assertEqual(plots.amplitude_line.opts["connect"], "finite")
        points = [{"carrier_hz": 2.4e9, "amplitude_dB": -40, "repeat": 1, "point": 1},
                  {"carrier_hz": 2.4e9, "amplitude_dB": -39, "repeat": 2, "point": 1}]
        plots.update_snapshot({"mode": "sweep", "points": points})
        self.assertEqual(len(plots.sweep_items), 2)
        plots.update_snapshot({"mode": "sweep", "points": points[:1]})
        self.assertEqual(set(plots.sweep_items), {1})
        plots.close()

    def test_every_diagnostic_model_type_renders_and_switches(self):
        plot = DiagnosticPlot()
        common = {"title": "诊断验证", "xlabel": "时间", "ylabel": "幅度", "note": "真实数据格式",
                  "series": [{"label": "测试", "data": [[0, 1], [1, 2]], "errors": [.1, .2]}]}
        models = [dict(common, kind=kind) for kind in ("line", "bar", "scatter")]
        models += [dict(common, kind="heatmap", x=[0, 1], y=[1, 2], z=[[1, None], [2, 3]], colorbar="PSD"),
                   dict(common, kind="segments", segments=[{"start": 0, "end": 2, "label": "参考", "contact": False}]),
                   dict(common, kind="line", vertical=[{"x": .5, "label": "单音"}], horizontal=[{"y": 1, "label": "门槛"}])]
        for model in models:
            plot.display(model)
            self.app.processEvents()
            self.assertEqual(plot.model["kind"], model["kind"])
        plot.close()

    def test_close_requires_active_confirmation_and_waits_without_freezing(self):
        service = FakeService()
        window = MainWindow(service)
        window.show()
        self.pump_until(lambda: window.profile.count() == 1)
        service.job = {"status": "running", "config": {"mode": "phase"}}
        window._apply_status(service.snapshot())
        with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.No):
            window.close()
        self.assertTrue(window.isVisible())
        self.assertFalse(service.closed)
        release = threading.Event()
        window.runner.submit("analysis_in_progress", lambda: release.wait(2))
        with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.StandardButton.Yes):
            window.close()
        self.pump_until(lambda: service.closed)
        self.assertTrue(window.isVisible())
        ticked = []
        QtCore.QTimer.singleShot(10, lambda: ticked.append(True))
        self.pump_until(lambda: bool(ticked), .5)
        release.set()
        self.pump_until(lambda: not window.isVisible())

    def test_real_replay_seek_force_refreshes_preview_and_shows_timing_note(self):
        from sspp_desktop.config import Settings
        from sspp_desktop.services import DesktopService
        service = DesktopService(Settings.from_env(Path(__file__).resolve().parents[1]))
        window = MainWindow(service)
        window.show()
        try:
            self.pump_until(lambda: bool(window.history_rows))
            for mode in ("phase", "sweep"):
                index = next(i for i, row in enumerate(window.history_rows) if row["mode"] == mode and row["status"] == "complete")
                window.history_table.selectRow(index)
                self.pump_until(lambda: window.selected_details is not None)
                window.play_replay()
                self.pump_until(lambda: window.replay is not None)
                window.pause_replay()
                window.seek_replay(700)
                self.assertGreater(len(window.preview_plots.i_line.xData), 0)
                self.assertFalse(window.replay_running)
                self.assertEqual(window.replay_note.text(), window.replay.timing_note)
                self.assertTrue(window.replay_note.isVisible())
                self.assertIn("历史回放", window.view_banner.text())
                self.assertIsNone(service.proc)
            window.return_live()
            self.assertIsNone(window.replay)
        finally:
            window.close()
            self.pump_until(lambda: not window.isVisible())

    def test_real_history_diagnostic_coverage(self):
        from sspp_desktop.config import Settings
        from sspp_desktop.services import DesktopService
        service = DesktopService(Settings.from_env(Path(__file__).resolve().parents[1]))
        plot = DiagnosticPlot()
        covered = set()
        for mode in ("phase", "sweep"):
            candidates = [row for row in service.history() if row["mode"] == mode and row["status"] == "complete"]
            if not candidates:
                continue
            model = service.diagnostic_view(candidates[0]["key"])
            for charts in model["groups"].values():
                for chart in charts:
                    plot.display(chart)
                    self.app.processEvents()
                    covered.add(chart["kind"])
        self.assertTrue({"line", "scatter", "heatmap", "bar", "segments"}.issubset(covered), covered)
        plot.close()
        service.shutdown()


if __name__ == "__main__":
    unittest.main()
