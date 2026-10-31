"""USRP 与 Arduino 独立可用性及批次互锁；所有设备/采集均使用替身。"""

from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock, patch

from sspp_lab.controller import DEFAULTS, LabError
import test_device_environment as fixtures

RF, SERIAL = fixtures.RF, fixtures.SERIAL
TARGETS = {"usrp": RF, "serial": SERIAL}


class MultideviceEnvironmentTests(unittest.TestCase):
    run_fake_process = fixtures.EnvironmentTests.run_fake_process
    initialize = fixtures.EnvironmentTests.initialize
    factory = fixtures.EnvironmentTests.factory

    def setUp(self):
        fixtures.EnvironmentTests.setUp(self)
        self.output = io.StringIO()
        redirect = redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(lambda: redirect.__exit__(None, None, None))

    def batch_factory(self, options=None):
        options = options or {}
        def launch(args, **kwargs):
            kind = args[args.index("--kind") + 1]
            return self.factory(**options.get(kind, {}))(args, **kwargs)
        return launch

    def batch(self, targets=None, options=None, cancel_event=None):
        with patch("sspp_desktop.device_environment.subprocess.Popen", side_effect=self.batch_factory(options)):
            return self.service.initialize_workbench(TARGETS if targets is None else targets, cancel_event=cancel_event)

    def process_kinds(self):
        return [process.args[process.args.index("--kind") + 1] for process in self.processes]

    @staticmethod
    def failure(event):
        return {**event, "status": "failed", "message": "设备未连接"}

    def test_both_devices_ready_have_independent_logs_targets_and_snapshots(self):
        snap = self.batch()
        self.assertEqual(snap["state"], "ready")
        self.assertEqual(self.process_kinds(), ["serial", "usrp"])
        self.assertTrue(self.service.environment_matches(RF))
        self.assertTrue(self.service.environment_matches(SERIAL))
        self.assertNotEqual(snap["devices"]["serial"]["log_path"], snap["devices"]["usrp"]["log_path"])
        self.assertTrue(all(Path(device["log_path"]).is_file() for device in snap["devices"].values()))
        with patch("pathlib.Path.open", side_effect=AssertionError("snapshot must not read disk")):
            copied = self.service.environment_snapshot()
            self.assertEqual(self.service.environment_snapshot("serial")["state"], "ready")
        copied["devices"]["usrp"]["state"] = "failed"
        self.assertEqual(self.service.environment_snapshot("usrp")["state"], "ready")
        self.assertFalse((self.root / "Data").exists())
        self.assertTrue(all(process.waited and process.stdout.closed and process.stderr.closed for process in self.processes))

    def test_serial_failure_still_initializes_usrp_and_allows_rf_start(self):
        snap = self.batch(options={"serial": {"response": self.failure}})
        self.assertEqual(snap["state"], "partial")
        self.assertEqual(snap["devices"]["serial"]["state"], "failed")
        self.assertEqual(snap["devices"]["usrp"]["state"], "ready")
        self.assertIn("设备未连接", Path(snap["devices"]["serial"]["log_path"]).read_text())
        with patch("sspp_lab.controller.LabController.start", return_value={"started": True}) as start:
            self.assertEqual(self.service.start({**DEFAULTS, "profile": "own", "setup_confirmed": True}), {"started": True})
            start.assert_called_once()
        self.assertFalse(self.service.environment_matches(SERIAL))

    def test_usrp_failure_preserves_serial_and_rf_retry_restores_both(self):
        snap = self.batch(options={"usrp": {"response": self.failure}})
        self.assertEqual(snap["state"], "partial")
        self.assertTrue(self.service.environment_matches(SERIAL))
        serial_log = snap["devices"]["serial"]["log_path"]
        with patch("sspp_lab.controller.LabController.start") as start:
            with self.assertRaises(LabError):
                self.service.start({**DEFAULTS, "profile": "own", "setup_confirmed": True})
            start.assert_not_called()
        self.assertEqual(self.initialize(RF)["state"], "ready")
        self.assertEqual(self.service.environment_snapshot()["state"], "ready")
        self.assertEqual(self.service.environment_snapshot("serial")["log_path"], serial_log)

    def test_missing_profile_does_not_prevent_serial_check_and_both_missing_are_nonfatal(self):
        snap = self.batch({"usrp": {"kind": "usrp", "profile": "teacher"}, "serial": SERIAL})
        self.assertEqual(snap["state"], "partial")
        self.assertEqual(self.process_kinds(), ["serial"])
        self.assertTrue(self.service.environment_matches(SERIAL))
        self.assertEqual(snap["devices"]["usrp"]["state"], "failed")
        self.service.invalidate_environment()
        self.processes.clear()
        snap = self.batch({"usrp": None, "serial": None})
        self.assertEqual(snap["state"], "unavailable")
        self.assertTrue(all(device["state"] == "skipped" for device in snap["devices"].values()))
        self.assertEqual(self.process_kinds(), [])

    def test_unselected_device_retains_existing_ready_but_new_one_is_skipped(self):
        self.initialize(RF)
        before = self.service.environment_snapshot("usrp")
        snap = self.batch({"usrp": None, "serial": None})
        self.assertEqual(snap["state"], "partial")
        self.assertEqual(snap["devices"]["usrp"]["state"], "ready")
        self.assertEqual(snap["devices"]["usrp"]["log_path"], before["log_path"])
        self.assertEqual(snap["devices"]["serial"]["state"], "skipped")
        self.assertIn("保留", snap["devices"]["usrp"]["message"])
        self.assertEqual(self.process_kinds(), ["usrp"])

    def test_invalid_serial_configuration_is_isolated_and_logged(self):
        snap = self.batch({"usrp": RF, "serial": {**SERIAL, "port": "/tmp/not-a-device"}})
        self.assertEqual(snap["state"], "partial")
        self.assertEqual(self.process_kinds(), ["usrp"])
        self.assertEqual(snap["devices"]["serial"]["state"], "failed")
        self.assertTrue(Path(snap["devices"]["serial"]["log_path"]).is_file())
        self.assertTrue(self.service.environment_matches(RF))

    def test_invalidation_and_rf_capture_failure_only_change_affected_device(self):
        self.batch()
        self.service.invalidate_environment("serial")
        self.assertTrue(self.service.environment_matches(RF))
        self.assertFalse(self.service.environment_matches(SERIAL))
        self.initialize(SERIAL)
        self.service.invalidate_environment("usrp")
        self.assertTrue(self.service.environment_matches(SERIAL))
        self.initialize(RF)
        with patch("sspp_lab.controller.LabController._collect"):
            self.service._collect(None, {"status": "initialization_failed"})
        self.assertEqual(self.service.environment_snapshot("usrp")["state"], "failed")
        self.assertTrue(self.service.environment_matches(SERIAL))
        self.initialize(RF)
        with patch("sspp_lab.controller.LabController.start", side_effect=LabError("launch failed")):
            with self.assertRaises(LabError):
                self.service.start({**DEFAULTS, "profile": "own", "setup_confirmed": True})
        self.assertTrue(self.service.environment_matches(SERIAL))
        self.assertEqual(self.service.environment_snapshot()["state"], "partial")

    def test_profile_file_change_does_not_invalidate_serial_match(self):
        self.batch()
        self.profile.write_text(self.profile.read_text() + "\n")
        self.assertFalse(self.service.environment_matches(RF))
        self.assertTrue(self.service.environment_matches(SERIAL))

    def test_batch_reservation_blocks_operations_between_devices(self):
        entered, release, values = threading.Event(), threading.Event(), []
        original = self.service._initialize_device
        def pause(target, **kwargs):
            result = original(target, **kwargs)
            if target["kind"] == "serial":
                entered.set()
                release.wait(2)
            return result
        with patch.object(self.service, "_initialize_device", side_effect=pause), \
             patch("sspp_desktop.device_environment.subprocess.Popen", side_effect=self.batch_factory()):
            worker = threading.Thread(target=lambda: values.append(self.service.initialize_workbench(TARGETS)))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.service.preparing)
                self.assertTrue(self.service.environment_matches(SERIAL))
                self.assertEqual(self.service.environment_snapshot()["state"], "initializing")
                for operation in (lambda: self.service.initialize_workbench(TARGETS),
                                  lambda: self.service.initialize_environment(RF),
                                  lambda: self.service.preflight(DEFAULTS),
                                  lambda: self.service.start({**DEFAULTS, "profile": "own", "setup_confirmed": True})):
                    with self.assertRaises(LabError):
                        operation()
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(values[0]["state"], "ready")
        self.assertFalse(self.service.preparing)
        self.assertTrue(self.service._environment_done.is_set())

    def test_cancel_between_checks_is_not_cleared_and_keeps_completed_serial(self):
        original = self.service._initialize_device
        def cancel_after_serial(target, **kwargs):
            result = original(target, **kwargs)
            if target["kind"] == "serial":
                self.service.cancel_environment_initialization()
            return result
        with patch.object(self.service, "_initialize_device", side_effect=cancel_after_serial):
            snap = self.batch()
        self.assertEqual(self.process_kinds(), ["serial"])
        self.assertEqual(snap["state"], "partial")
        self.assertTrue(self.service.environment_matches(SERIAL))
        self.assertEqual(snap["devices"]["usrp"]["state"], "cancelled")

    def test_cancel_during_usrp_reaps_it_without_removing_completed_serial(self):
        values, cancel = [], threading.Event()
        with patch("sspp_desktop.device_environment.subprocess.Popen",
                   side_effect=self.batch_factory({"usrp": {"blocked": True, "resist_terminate": True}})):
            worker = threading.Thread(target=lambda: values.append(self.service.initialize_workbench(TARGETS, cancel_event=cancel)))
            worker.start()
            try:
                deadline = time.monotonic() + 2
                while len(self.processes) < 2 and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertEqual(len(self.processes), 2)
                self.assertTrue(self.processes[1].entered.wait(1))
                self.assertTrue(self.service.environment_matches(SERIAL))
            finally:
                cancel.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(values[0]["state"], "partial")
        self.assertTrue(self.service.environment_matches(SERIAL))
        self.assertEqual(self.service.environment_snapshot("usrp")["state"], "cancelled")
        self.assertTrue(self.processes[1].killed and self.processes[1].waited)
        self.assertFalse(self.service.preparing)

    def test_early_cancel_keeps_previous_ready_and_opens_nothing(self):
        self.batch()
        self.processes.clear()
        cancel = threading.Event()
        cancel.set()
        snap = self.batch(cancel_event=cancel)
        self.assertEqual(snap["state"], "ready")
        self.assertTrue(self.service.environment_matches(RF))
        self.assertTrue(self.service.environment_matches(SERIAL))
        self.assertEqual(self.process_kinds(), [])
        self.assertTrue(cancel.is_set())

    def test_selective_invalidation_cancels_only_that_kind_then_checks_other(self):
        values = []
        with patch("sspp_desktop.device_environment.subprocess.Popen",
                   side_effect=self.batch_factory({"serial": {"blocked": True}})):
            worker = threading.Thread(target=lambda: values.append(self.service.initialize_workbench(TARGETS)))
            worker.start()
            try:
                deadline = time.monotonic() + 2
                while not self.processes and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertTrue(self.processes[0].entered.wait(1))
                self.service.invalidate_environment("serial")
            finally:
                worker.join(2)
                if worker.is_alive():
                    self.service.cancel_environment_initialization()
                    worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(values[0]["state"], "partial")
        self.assertEqual(self.service.environment_snapshot("serial")["state"], "cancelled")
        self.assertTrue(self.service.environment_matches(RF))
        self.assertEqual(self.process_kinds(), ["serial", "usrp"])

    def test_serial_timeout_does_not_use_up_usrp_budget_or_cancel_usrp_check(self):
        with patch("sspp_desktop.device_environment.INITIALIZATION_TIMEOUT_S", .02):
            snap = self.batch(options={"serial": {"blocked": True, "resist_terminate": True}})
        self.assertEqual(snap["state"], "partial")
        self.assertEqual(snap["devices"]["serial"]["state"], "failed")
        self.assertIn("0.02 秒", snap["devices"]["serial"]["message"])
        self.assertTrue(self.service.environment_matches(RF))
        self.assertEqual([call.kwargs["timeout_s"] for call in self.process_runner.call_args_list], [.02, 120.])
        self.assertTrue(self.processes[0].killed and self.processes[0].waited)

    def test_shutdown_cancels_active_usrp_but_preserves_completed_serial(self):
        values = []
        with patch("sspp_desktop.device_environment.subprocess.Popen",
                   side_effect=self.batch_factory({"usrp": {"blocked": True, "resist_terminate": True}})):
            worker = threading.Thread(target=lambda: values.append(self.service.initialize_workbench(TARGETS)))
            worker.start()
            try:
                deadline = time.monotonic() + 2
                while len(self.processes) < 2 and time.monotonic() < deadline:
                    time.sleep(.001)
                self.assertEqual(len(self.processes), 2)
                self.assertTrue(self.processes[1].entered.wait(1))
                self.service.shutdown()
            finally:
                self.service.cancel_environment_initialization()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(values[0]["state"], "partial")
        self.assertEqual(self.service.environment_snapshot("serial")["state"], "ready")
        self.assertEqual(self.service.environment_snapshot("usrp")["state"], "cancelled")
        self.assertTrue(self.processes[1].killed and self.processes[1].waited)
        self.assertTrue(self.service._environment_done.is_set())
        self.assertFalse(self.service.preparing)

    def test_both_repository_names_are_checked_without_blocking_serial(self):
        for repository in ("sspp-usrp", "sspp-usrp-PySide6-PyQtGraph", "multisensor-workbench"):
            with self.subTest(repository=repository):
                self.service.invalidate_environment()
                self.processes.clear()
                self.process_check.return_value = Mock(returncode=0, stdout=f"123 python /repo/{repository}/run_phase.py acquire")
                snap = self.batch()
                self.assertEqual(snap["state"], "partial")
                self.assertTrue(self.service.environment_matches(SERIAL))
                self.assertEqual(snap["devices"]["usrp"]["state"], "failed")
                self.assertEqual(self.process_kinds(), ["serial"])


if __name__ == "__main__":
    unittest.main()
