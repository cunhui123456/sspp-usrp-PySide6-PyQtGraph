"""真实临时 Python 子进程验证初始化日志 IO；不导入或打开设备。"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService

REAL_POPEN = subprocess.Popen
SERIAL = {"kind": "serial", "port": "/dev/cu.OFFLINE", "protocol": "temperature_v1"}
SECRET = "PRIVATE-IDENTITY"
PRELUDE = '''import json, os, signal, sys, time
result = {"event": "DEVICE_ENVIRONMENT_RESULT", "version": 1,
          "request_id": sys.argv[1], "kind": sys.argv[2],
          "status": "ready", "message": "检查完成", "released": True}
'''
SUCCESS = 'print(json.dumps(result), flush=True)\n'


class RecordingTerminal(io.StringIO):
    def __init__(self):
        super().__init__()
        self.observed = threading.Event()
        self.flushed = 0

    def write(self, text):
        result = super().write(text)
        if "驱动检查第一阶段" in text:
            self.observed.set()
        return result

    def flush(self):
        self.flushed += 1
        super().flush()


class InitializationLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = DesktopService(Settings(root=self.root))
        self.service.redactions = [SECRET]
        self.terminal = RecordingTerminal()
        self.processes = []

    def launcher(self, source):
        def launch(args, **kwargs):
            request = args[args.index("--request-id") + 1]
            kind = args[args.index("--kind") + 1]
            process = REAL_POPEN([sys.executable, "-B", "-u", "-c", PRELUDE + source, request, kind], **kwargs)
            self.processes.append(process)
            return process
        return launch

    def run_child(self, source, *, cancel_event=None):
        with redirect_stdout(self.terminal), \
             patch("sspp_desktop.device_environment.subprocess.Popen", side_effect=self.launcher(source)):
            result = self.service.initialize_environment(SERIAL, cancel_event=cancel_event)
        self.assertFalse((self.root / "Data").exists())
        self.assertTrue(all(proc.poll() is not None for proc in self.processes))
        self.assertTrue(all(proc.stdout.closed and proc.stderr.closed for proc in self.processes))
        self.assertFalse(any(thread.name.startswith("sspp-initialize-") for thread in threading.enumerate()))
        return result

    def log_text(self, result):
        path = Path(result["log_path"])
        self.assertTrue(path.is_relative_to(self.root / ".cache" / "initialization_logs"))
        return path.read_text(encoding="utf-8")

    def test_stderr_reaches_terminal_and_file_before_child_completes(self):
        source = f'print("驱动检查第一阶段 {SECRET}", file=sys.stderr, flush=True)\ntime.sleep(.5)\n' + SUCCESS
        results = []
        with redirect_stdout(self.terminal), \
             patch("sspp_desktop.device_environment.subprocess.Popen", side_effect=self.launcher(source)):
            worker = threading.Thread(target=lambda: results.append(self.service.initialize_environment(SERIAL)))
            worker.start()
            try:
                self.assertTrue(self.terminal.observed.wait(2), "driver log was buffered until child completion")
                self.assertTrue(worker.is_alive())
                self.assertIsNone(self.processes[-1].poll())
                snap = self.service.environment_snapshot("serial")
                self.assertEqual(snap["state"], "initializing")
                self.assertIn("驱动检查第一阶段", self.log_text(snap))
                self.assertNotIn(SECRET, self.terminal.getvalue())
                self.assertNotIn(SECRET, self.log_text(snap))
                self.assertIn("[设备标识]", self.log_text(snap))
                self.assertGreater(self.terminal.flushed, 0)
            finally:
                worker.join(3)
                self.service.cancel_environment_initialization()
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["state"], "ready")
        self.assertIn("子进程退出码=0", self.log_text(results[0]))

    def test_split_utf8_and_invalid_stderr_bytes_do_not_corrupt_success(self):
        source = ('message = "中文驱动状态 PRIVATE-IDENTITY\\n".encode("utf-8")\n'
                  'os.write(2, message[:2]); time.sleep(.03); os.write(2, message[2:])\n'
                  'os.write(2, b"invalid byte: \\xff\\n")\n' + SUCCESS)
        snap = self.run_child(source)
        self.assertEqual(snap["state"], "ready")
        self.assertIn("中文驱动状态 [设备标识]", self.log_text(snap))
        self.assertIn("invalid byte: \ufffd", self.log_text(snap))
        self.assertNotIn(SECRET, self.terminal.getvalue())

    def test_nonzero_exit_retains_redacted_driver_summary_and_returncode(self):
        source = ('print("USB open failed PRIVATE-IDENTITY", file=sys.stderr, flush=True)\n'
                  'result.update(status="failed", message="driver initialization failed PRIVATE-IDENTITY")\n'
                  + SUCCESS + 'sys.exit(7)\n')
        snap = self.run_child(source)
        self.assertEqual(snap["state"], "failed")
        self.assertIn("USB open failed [设备标识]", snap["message"])
        self.assertIn("子进程退出码=7", self.log_text(snap))
        self.assertNotIn(SECRET, json.dumps(snap) + self.log_text(snap) + self.terminal.getvalue())

    def test_missing_or_malformed_stdout_exposes_stderr_without_printing_stdout(self):
        for stdout in ('print("not a result PRIVATE-IDENTITY", flush=True)\n', '',
                       SUCCESS + 'print("extra trailing stdout PRIVATE-IDENTITY", flush=True)\n'):
            with self.subTest(stdout=stdout):
                snap = self.run_child('print("driver reason PRIVATE-IDENTITY", file=sys.stderr, flush=True)\n' + stdout)
                self.assertEqual(snap["state"], "failed")
                self.assertIn("driver reason [设备标识]", snap["message"])
                self.assertNotIn(SECRET, self.log_text(snap) + self.terminal.getvalue())
                self.assertNotIn("not a result", self.terminal.getvalue())
                self.assertNotIn("extra trailing stdout", self.terminal.getvalue())

    def test_oversize_stdout_cannot_be_truncated_to_valid_success(self):
        snap = self.run_child('print("driver output before overflow", file=sys.stderr, flush=True)\n'
                              + SUCCESS + 'sys.stdout.write(" " * 100000); sys.stdout.flush()\n')
        self.assertEqual(snap["state"], "failed")
        self.assertIn("超过允许大小", snap["message"])
        self.assertIn("driver output before overflow", snap["message"])

    def test_oversize_stderr_line_is_bounded_and_never_leaks_split_identity(self):
        source = ('os.write(2, b"x" * 8185 + b"PRIVATE-"); time.sleep(.02)\n'
                  'os.write(2, b"IDENTITY" + b"x" * 20000 + b"\\n")\n'
                  'print("driver tail", file=sys.stderr, flush=True)\n' + SUCCESS)
        snap = self.run_child(source)
        self.assertEqual(snap["state"], "ready")
        text = self.log_text(snap)
        self.assertIn("过长驱动日志行已省略", text)
        self.assertIn("driver tail", text)
        self.assertNotIn("PRIVATE-", text)
        self.assertNotIn("IDENTITY", text)
        self.assertLess(len(text), 10000)

    def test_cancel_drains_signal_handler_diagnostics_and_reaps(self):
        cancel, results = threading.Event(), []
        source = ('def terminated(signum, frame):\n'
                  '    print("cancel tail PRIVATE-IDENTITY", file=sys.stderr, flush=True)\n'
                  '    sys.exit(3)\n'
                  'signal.signal(signal.SIGTERM, terminated)\n'
                  'print("驱动检查第一阶段", file=sys.stderr, flush=True)\n'
                  'time.sleep(10)\n')
        with redirect_stdout(self.terminal), \
             patch("sspp_desktop.device_environment.subprocess.Popen", side_effect=self.launcher(source)):
            worker = threading.Thread(target=lambda: results.append(self.service.initialize_environment(SERIAL, cancel_event=cancel)))
            worker.start()
            try:
                self.assertTrue(self.terminal.observed.wait(2))
                cancel.set()
            finally:
                cancel.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["state"], "cancelled")
        self.assertIn("cancel tail [设备标识]", self.log_text(results[0]))
        self.assertIn("收到取消请求", self.terminal.getvalue())
        self.assertEqual(self.processes[-1].poll(), 3)
        self.assertTrue(self.processes[-1].stdout.closed and self.processes[-1].stderr.closed)

    def test_silent_driver_emits_heartbeat_before_completion(self):
        observed, results = threading.Event(), []
        original_write = self.terminal.write
        def write(text):
            if "仍在等待设备驱动" in text:
                observed.set()
            return original_write(text)
        source = 'time.sleep(.4)\n' + SUCCESS
        with redirect_stdout(self.terminal), \
             patch.object(self.terminal, "write", side_effect=write), \
             patch("sspp_desktop.device_environment.INITIALIZATION_PROGRESS_INTERVAL_S", .05), \
             patch("sspp_desktop.device_environment.subprocess.Popen", side_effect=self.launcher(source)):
            worker = threading.Thread(target=lambda: results.append(self.service.initialize_environment(SERIAL)))
            worker.start()
            try:
                self.assertTrue(observed.wait(2))
                self.assertTrue(worker.is_alive())
                self.assertIsNone(self.processes[-1].poll())
                self.assertIn("最多 30 秒", self.terminal.getvalue())
            finally:
                worker.join(3)
                self.service.cancel_environment_initialization()
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["state"], "ready")
        self.assertIn("仍在等待设备驱动", self.log_text(results[0]))

    def test_timeout_drains_final_diagnostics_and_does_not_leave_readers(self):
        source = ('def terminated(signum, frame):\n'
                  '    print("timeout tail PRIVATE-IDENTITY", file=sys.stderr, flush=True)\n'
                  '    sys.exit(4)\n'
                  'signal.signal(signal.SIGTERM, terminated)\n'
                  'print("waiting for device", file=sys.stderr, flush=True)\n'
                  'time.sleep(10)\n')
        with patch("sspp_desktop.device_environment.INITIALIZATION_TIMEOUT_S", .2):
            snap = self.run_child(source)
        self.assertEqual(snap["state"], "failed")
        self.assertIn("超过 0.2 秒", snap["message"])
        self.assertIn("timeout tail [设备标识]", snap["message"])
        self.assertIn("子进程退出码=4", self.log_text(snap))

    def test_failed_log_directory_keeps_terminal_and_allows_later_ready(self):
        (self.root / ".cache").write_text("blocked directory")
        first = self.run_child('print("driver first", file=sys.stderr, flush=True)\n' + SUCCESS)
        self.assertEqual(first["state"], "ready")
        self.assertIsNone(first["log_path"])
        self.assertIn("诊断日志未完整保存", first["message"])
        self.assertIn("driver first", self.terminal.getvalue())
        (self.root / ".cache").unlink()
        second = self.run_child('print("driver second", file=sys.stderr, flush=True)\n' + SUCCESS)
        self.assertEqual(second["state"], "ready")
        self.assertIn("driver second", self.log_text(second))
        self.assertNotIn("日志未完整保存", second["message"])

    def test_shutdown_joins_readers_and_preserves_final_driver_output(self):
        results = []
        source = ('def terminated(signum, frame):\n'
                  '    os.write(2, b"shutdown tail PRIVATE-IDENTITY")\n'
                  '    sys.exit(5)\n'
                  'signal.signal(signal.SIGTERM, terminated)\n'
                  'print("驱动检查第一阶段", file=sys.stderr, flush=True)\n'
                  'time.sleep(10)\n')
        with redirect_stdout(self.terminal), \
             patch("sspp_desktop.device_environment.subprocess.Popen", side_effect=self.launcher(source)):
            worker = threading.Thread(target=lambda: results.append(self.service.initialize_environment(SERIAL)))
            worker.start()
            try:
                self.assertTrue(self.terminal.observed.wait(2))
                self.service.shutdown()
            finally:
                self.service.cancel_environment_initialization()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["state"], "cancelled")
        self.assertIn("shutdown tail [设备标识]", self.log_text(results[0]))
        self.assertTrue(self.processes[-1].stdout.closed and self.processes[-1].stderr.closed)
        self.assertFalse(any(thread.name.startswith("sspp-initialize-") for thread in threading.enumerate()))

    def test_broken_terminal_does_not_prevent_ready_or_file_diagnostics(self):
        class BrokenTerminal:
            def write(self, text):
                raise BrokenPipeError("closed terminal")
            def flush(self):
                raise BrokenPipeError("closed terminal")
        self.terminal = BrokenTerminal()
        snap = self.run_child('print("file still works", file=sys.stderr, flush=True)\n' + SUCCESS)
        self.assertEqual(snap["state"], "ready")
        self.assertIn("file still works", self.log_text(snap))

    def test_early_cancel_is_logged_without_launching_subprocess(self):
        cancel = threading.Event()
        cancel.set()
        with redirect_stdout(self.terminal), patch("sspp_desktop.device_environment.subprocess.Popen") as launch:
            snap = self.service.initialize_environment(SERIAL, cancel_event=cancel)
            launch.assert_not_called()
        self.assertEqual(snap["state"], "cancelled")
        self.assertIn("收到取消请求", self.log_text(snap))
        self.assertIn("子进程退出码=未启动", self.log_text(snap))
        self.assertTrue(cancel.is_set())


if __name__ == "__main__":
    unittest.main()
