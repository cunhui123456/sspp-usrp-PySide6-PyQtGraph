"""后台串口与封存验证：注入 fake transport，无真实串口打开。"""

import csv
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from sspp_experiments.temperature_pressure import CaptureConfig, SerialCaptureSession


class FakeReader:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False
        self.calls = 0

    def read(self, timeout=0.1):
        self.calls += 1
        time.sleep(.003)
        if self.chunks:
            item = self.chunks.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return None

    def close(self):
        self.closed = True


def wait_until(predicate, timeout=3):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("fake capture timed out")


class TemperatureCaptureTests(unittest.TestCase):
    def config(self, protocol="pressure_v1"):
        return CaptureConfig("/dev/cu.FAKE", protocol, "offline sample")

    def test_complete_capture_is_sealed_raw_bytes_hashes_and_no_commands(self):
        chunks = [b"100,2.", b"71\r\n150,2.72\n"]
        reader = FakeReader(chunks)
        with tempfile.TemporaryDirectory() as temporary:
            session = SerialCaptureSession(temporary, lambda port, baud: reader)
            key = session.start(self.config())
            wait_until(lambda: session.snapshot()["rows"] == 2)
            self.assertTrue(session.stop())
            path = Path(temporary) / "Data" / key
            self.assertEqual((path / "raw_serial.bin").read_bytes(), b"".join(chunks))
            manifest = json.loads((path / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["acquisition_schema"], "sspp.temperature_pressure.v1")
            self.assertTrue(reader.closed)
            self.assertTrue(all(manifest[name] for name in ("serial_closed", "workers_stopped", "data_files_closed")))
            for item in manifest["files"]:
                contents = (path / item["path"]).read_bytes()
                self.assertEqual(item["bytes"], len(contents))
                self.assertEqual(item["sha256"], hashlib.sha256(contents).hexdigest())
            with (path / "samples.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["mcu_time_ms"] for row in rows], ["100", "150"])
            self.assertEqual(rows[0]["adc"], "")
            self.assertEqual(manifest["stop_reason"], "operator_stop")

    def test_temperature_two_channel_rows_are_saved_with_actual_times(self):
        reader = FakeReader([b"# firmware v1\n100,600,2.933,140,650,3.177\n"])
        with tempfile.TemporaryDirectory() as temporary:
            session = SerialCaptureSession(temporary, lambda port, baud: reader)
            session.start(self.config("temperature_v1"))
            wait_until(lambda: session.snapshot()["rows"] == 2)
            self.assertTrue(session.stop())
            with (session.run_path / "samples.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["channel"] for row in rows], ["S6", "S7"])
            self.assertEqual([row["mcu_time_ms"] for row in rows], ["100", "140"])

    def test_disconnect_retains_valid_data_and_does_not_report_success(self):
        reader = FakeReader([b"100,2.71\n", OSError("fake cable disconnected")])
        with tempfile.TemporaryDirectory() as temporary:
            session = SerialCaptureSession(temporary, lambda port, baud: reader)
            session.start(self.config())
            wait_until(lambda: not session.is_running)
            manifest = json.loads((session.run_path / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed_or_incomplete")
            self.assertEqual(manifest["rows"], 1)
            self.assertTrue(manifest["data_files_closed"])
            self.assertTrue(reader.closed)
            self.assertIn("capture_error", manifest["event_counts"])

    def test_bad_protocol_and_partial_tail_are_identifiable(self):
        for data, kind in [(b"1,2,3\n", "malformed_line"), (b"100,2.7\n105,", "truncated_line"), (b"# ERROR: I2C failed\n", "firmware_error")]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                reader = FakeReader([data])
                session = SerialCaptureSession(temporary, lambda port, baud: reader)
                session.start(self.config())
                wait_until(lambda: not reader.chunks and reader.calls > 1)
                self.assertTrue(session.stop())
                manifest = json.loads((session.run_path / "manifest.json").read_text())
                self.assertEqual(manifest["status"], "complete" if kind == "truncated_line" else "failed_or_incomplete")
                if kind == "truncated_line":
                    self.assertTrue(manifest["warnings"])
                self.assertEqual(manifest["event_counts"][kind], 1)
                self.assertEqual((session.run_path / "raw_serial.bin").read_bytes(), data)

    def test_open_error_does_not_claim_any_sample_or_leak_thread(self):
        def failure(port, baud):
            raise OSError("fake port busy")
        with tempfile.TemporaryDirectory() as temporary:
            session = SerialCaptureSession(temporary, failure)
            session.start(self.config())
            wait_until(lambda: not session.is_running)
            manifest = json.loads((session.run_path / "manifest.json").read_text())
            self.assertFalse(manifest["serial_opened"])
            self.assertEqual(manifest["status"], "failed_or_incomplete")
            self.assertTrue(session.stop(.1))

    def test_start_requires_metadata_and_session_is_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            reader = FakeReader([])
            session = SerialCaptureSession(temporary, lambda port, baud: reader)
            with self.assertRaises(ValueError):
                session.start(CaptureConfig("/private/tmp/file", "pressure_v1", "sample"))
            with self.assertRaises(ValueError):
                session.start(CaptureConfig("/dev/cu.FAKE", "pressure_v1", ""))
            session.start(self.config())
            try:
                with self.assertRaises(RuntimeError):
                    session.start(self.config())
            finally:
                self.assertTrue(session.stop())

    def test_bounded_stop_returns_false_until_reader_can_close(self):
        release = threading.Event()
        entered = threading.Event()
        class BlockedReader(FakeReader):
            def read(self, timeout=0.1):
                entered.set()
                release.wait(2)
                return None
        with tempfile.TemporaryDirectory() as temporary:
            reader = BlockedReader([])
            session = SerialCaptureSession(temporary, lambda port, baud: reader)
            session.start(self.config())
            entered.wait(2)
            try:
                start = time.monotonic()
                self.assertFalse(session.stop(.01))
                self.assertLess(time.monotonic() - start, .2)
                self.assertFalse(reader.closed)
                manifest = json.loads((session.run_path / "manifest.json").read_text())
                self.assertFalse(manifest["workers_stopped"])
            finally:
                release.set()
                self.assertTrue(session.stop())
            self.assertTrue(reader.closed)

    def test_no_bytes_times_out_and_seals_distinct_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            reader = FakeReader([])
            session = SerialCaptureSession(temporary, lambda port, baud: reader, first_sample_timeout_s=.08)
            session.start(self.config())
            wait_until(lambda: session.snapshot()["status"] == "running")
            self.assertIn("尚未收到任何字节", session.snapshot()["diagnostic"])
            wait_until(lambda: not session.is_running)
            manifest = json.loads((session.run_path / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed_or_incomplete")
            self.assertEqual(manifest["raw_bytes"], 0)
            self.assertEqual(manifest["event_counts"]["no_serial_data"], 1)
            self.assertTrue(reader.closed)

    def test_wrong_protocol_keeps_original_bytes_and_reports_protocol_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            raw = b"100,2.71\n150,2.72\n"
            reader = FakeReader([raw])
            session = SerialCaptureSession(temporary, lambda port, baud: reader, first_sample_timeout_s=.08)
            session.start(self.config("temperature_v1"))
            wait_until(lambda: session.snapshot()["raw_bytes"] == len(raw))
            self.assertIn("尚无匹配样本", session.snapshot()["diagnostic"])
            wait_until(lambda: not session.is_running)
            self.assertEqual((session.run_path / "raw_serial.bin").read_bytes(), raw)
            self.assertEqual(session.snapshot()["rows"], 0)
            self.assertEqual(session.snapshot()["event_counts"]["protocol_timeout"], 1)

    def test_received_samples_then_stall_preserves_curve_until_operator_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            reader = FakeReader([b"100,2.71\n"])
            session = SerialCaptureSession(temporary, lambda port, baud: reader,
                                           first_sample_timeout_s=.06, stalled_warning_s=.06)
            session.start(self.config())
            wait_until(lambda: "数据已停更" in session.snapshot()["diagnostic"])
            self.assertTrue(session.is_running)
            self.assertEqual(session.snapshot()["rows"], 1)
            self.assertEqual(session.snapshot()["points"][0]["voltage_v"], 2.71)
            self.assertTrue(session.stop())

    def test_preparation_provenance_must_match_target_and_is_saved_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            reader = FakeReader([b"100,2.71\n"])
            session = SerialCaptureSession(temporary, lambda port, baud: reader)
            evidence = {"port": "/dev/cu.FAKE", "protocol": "temperature_v1", "preparation_key": "device_preparations/test"}
            with self.assertRaisesRegex(ValueError, "准备记录"):
                session.start(CaptureConfig("/dev/cu.FAKE", "pressure_v1", "sample", preparation=evidence))
            self.assertIsNone(session.thread)
            evidence["protocol"] = "pressure_v1"
            session.start(CaptureConfig("/dev/cu.FAKE", "pressure_v1", "sample", preparation=evidence))
            wait_until(lambda: session.snapshot()["rows"] == 1)
            self.assertTrue(session.stop())
            manifest = json.loads((session.run_path / "manifest.json").read_text())
            self.assertEqual(manifest["preparation"], evidence)
            self.assertNotIn("preparation", manifest["serial"])


if __name__ == "__main__":
    unittest.main()
