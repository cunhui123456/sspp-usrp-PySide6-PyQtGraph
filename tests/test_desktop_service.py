"""桌面服务离线契约：取消启动、无文件实时更新、真实记录只读回放。"""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_desktop.replay import ReplaySession
from sspp_lab.controller import DEFAULTS, LabError


class DesktopContracts(unittest.TestCase):
    def test_old_and_renamed_project_acquisitions_both_block_new_rf_start(self):
        for folder in ("sspp-usrp-PySide6-PyQtGraph", "multisensor-workbench"):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as temporary:
                service = DesktopService(Settings(root=Path(temporary)))
                with patch.object(service, "environment_matches", return_value=True), \
                     patch.object(service, "preflight"), \
                     patch("sspp_lab.controller.subprocess.run") as processes, \
                     patch("sspp_lab.controller.subprocess.Popen") as launch:
                    processes.return_value.stdout = f"123 python /workspace/{folder}/run_phase.py acquire\n"
                    with self.assertRaisesRegex(LabError, "另一个.*采集进程"):
                        service.start({**DEFAULTS, "setup_confirmed": True})
                    launch.assert_not_called()

    def test_root_env_loaded_without_modifying_process_environment(self):
        import os
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text("SSPP_UI_INTERVAL_MS=75\nSSPP_CONDA_ENV=rf-gnuradio\n")
            with patch.dict(os.environ, {}, clear=True):
                settings = Settings.from_env(root)
                self.assertEqual(settings.ui_interval_ms, 75)
                self.assertNotIn("SSPP_UI_INTERVAL_MS", os.environ)
            (root / ".env").write_text("SSPP_CONDA_ENV=another-env\n")
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
                Settings.from_env(root)

    def test_trace_requires_neither_disk_nor_baseline_file_and_marks_gaps(self):
        service = DesktopService(Settings(root=Path("/tmp/nonexistent-sspp-test")))
        service.job = {"config": {"mode": "phase"}}
        first = {"x": 0.01, "amplitude": None, "phase": None, "label": "baseline"}
        second = {"x": 7.1, "amplitude": -2.5, "phase": 3.2, "label": "near"}
        with patch("pathlib.Path.open", side_effect=AssertionError("must not read files")):
            service._handle_event({"event": "TRACE", "seq": 1, "points": [first]}, service.job)
            service._handle_event({"event": "TRACE", "seq": 3, "points": [second]}, service.job)
            snap = service.live_snapshot()
        self.assertIsNone(snap["points"][0]["phase"])
        self.assertTrue(snap["points"][1]["gap"])
        self.assertEqual(snap["points"][-1]["phase"], 3.2)
        self.assertEqual(snap["display_gaps"], 1)

    def test_slow_preflight_does_not_block_snapshot_and_cancel_prevents_launch(self):
        service = DesktopService()
        entered, release = threading.Event(), threading.Event()
        failures = []

        def prepare(request):
            entered.set()
            release.wait(2)

        def start():
            try:
                service.start({**DEFAULTS, "setup_confirmed": True})
            except LabError as exc:
                failures.append(str(exc))

        with patch.object(service, "environment_matches", return_value=True), \
             patch.object(service, "preflight", side_effect=prepare), \
             patch("sspp_lab.controller.subprocess.run") as process_list, \
             patch("sspp_lab.controller.subprocess.Popen") as launch:
            process_list.return_value.stdout = ""
            worker = threading.Thread(target=start)
            worker.start()
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            self.assertTrue(service.snapshot()["preparing"])
            service.stop()
            self.assertLess(time.monotonic() - before, 0.2)
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            launch.assert_not_called()
        self.assertIn("启动已取消", failures[0])

    def test_collector_consumes_display_without_bloating_logs(self):
        class Process:
            stdout = io.StringIO(json.dumps({
                "event": "TRACE", "seq": 1,
                "points": [{"x": 8, "amplitude": 1, "phase": 2, "label": "near"}],
            }) + "\n")

            def wait(self):
                return 0

            def poll(self):
                return 0

        service = DesktopService()
        service.job = {"config": {"mode": "phase"}, "output": None, "errors": [], "rf_stopped": False}
        service._collect(Process(), service.job)
        self.assertEqual(service.live_snapshot()["points"][-1]["amplitude"], 1)
        self.assertEqual(list(service.logs), [])
        self.assertEqual(service.job["status"], "stop_unconfirmed")

    def test_new_desktop_metadata_and_old_web_metadata_both_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            service = DesktopService(Settings(root=Path(folder)))
            run = Path(folder) / "Data/runs/example"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text('{"status":"complete"}')
            (run / "desktop_context.json").write_text(json.dumps({
                "source": "local_desktop_manual_experiment", "desktop_status": "stopped", "sensor": "test"
            }))
            item = service.history()[0]
            self.assertEqual(item["source"], "desktop")
            self.assertEqual(item["status"], "stopped")
            self.assertIn("desktop_context.json", service.details("runs/example")["files"])

    def test_full_control_pipe_does_not_block_stop_or_snapshot(self):
        import os
        from types import SimpleNamespace
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        stream = os.fdopen(write_fd, "w")
        signals = []
        proc = SimpleNamespace(stdin=stream, poll=lambda: None, send_signal=signals.append)
        service = DesktopService()
        service.proc = proc
        service.job = {"config": {"mode": "phase"}, "status": "running", "rf_stopped": False}
        try:
            while True:
                try:
                    os.write(write_fd, b"x" * 4096)
                except BlockingIOError:
                    break
            with patch.object(service, "_end_process"):
                start = time.monotonic()
                service.stop()
                self.assertLess(time.monotonic() - start, 0.2)
                self.assertEqual(service.snapshot()["job"]["status"], "stopping")
                self.assertEqual(len(signals), 1)
        finally:
            stream.close()
            os.close(read_fd)

    def test_shutdown_waits_for_final_context_collector(self):
        service = DesktopService()
        finalized = threading.Event()

        def finalize():
            time.sleep(0.05)
            finalized.set()

        service.collector_thread = threading.Thread(target=finalize)
        service.collector_thread.start()
        service.shutdown()
        self.assertTrue(finalized.is_set())

    def test_real_phase_replay_uses_recorded_values_and_raw_iq(self):
        root = Settings().root
        paths = sorted((root / "Data/phase_runs").glob("*/amplitude_phase.jsonl"))
        if not paths:
            self.skipTest("No migrated real records")
        session = ReplaySession(paths[-1].parent, "phase")
        row = next(row for row in session.rows if row.get("relative_phase_deg") is not None)
        snap = session.snapshot(row["t_s"])
        self.assertEqual(snap["points"][-1]["phase"], row["relative_phase_deg"])
        self.assertEqual(snap["points"][-1]["amplitude"], row["relative_amplitude_dB"])
        preview = snap["preview"]
        start, count = preview["sample_start"], preview["sample_count"]
        np.testing.assert_array_equal(preview["I"], session.raw[start:start + count].real)
        np.testing.assert_array_equal(preview["Q"], session.raw[start:start + count].imag)
        self.assertIsNone(session.snapshot(0)["preview"])


if __name__ == "__main__":
    unittest.main()
