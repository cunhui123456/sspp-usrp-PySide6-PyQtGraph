"""初始化子进程的即时日志与结果通道隔离；全部设备均为替身。"""

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import Mock, patch

from sspp_desktop import initialize_device as child


class FlushedLog(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


class InitializeDeviceProgressTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = self.root / "offline.bin"
        self.image.write_bytes(b"offline FPGA fixture")
        self.profile = self.root / "offline.json"
        self.profile.write_text(json.dumps({
            "serial": "FAKE_PRIVATE_ID", "channel": 0, "fpga": str(self.image),
            "fpga_sha256": hashlib.sha256(self.image.read_bytes()).hexdigest(),
        }))
        self.digest = hashlib.sha256(self.profile.read_bytes()).hexdigest()

    def device(self, *, identity="FAKE_PRIVATE_ID"):
        device = Mock(spec=["get_tx_num_channels", "get_rx_num_channels", "get_usrp_rx_info", "get_usrp_tx_info"])
        device.get_tx_num_channels.return_value = device.get_rx_num_channels.return_value = 2
        device.get_usrp_rx_info.return_value = device.get_usrp_tx_info.return_value = {"mboard_serial": identity}
        return device

    def test_usrp_stages_flush_before_open_and_do_not_disclose_identity(self):
        log, output = FlushedLog(), io.StringIO()
        device = self.device()

        def open_device(_):
            self.assertIn("打开指定 USRP", log.getvalue().splitlines()[-1])
            self.assertGreaterEqual(log.flushes, 7)
            return device

        with redirect_stderr(log), redirect_stdout(output):
            self.assertIsNone(child.check_usrp(self.profile, self.digest, open_device))
        text = log.getvalue()
        stages = ["校验设备 profile", "校验 FPGA", "加载 UHD", "打开指定 USRP", "核对收发通道", "释放 USRP"]
        positions = [text.index(stage) for stage in stages]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("FAKE_PRIVATE_ID", text)
        self.assertNotIn(str(self.image), text)
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(text.rstrip().endswith("释放 USRP 设备句柄完成"))

    def test_fpga_error_has_stage_and_exception_before_any_device_open(self):
        self.image.write_bytes(b"tampered")
        factory, log = Mock(), FlushedLog()
        with redirect_stderr(log), self.assertRaisesRegex(ValueError, "FPGA SHA-256"):
            child.check_usrp(self.profile, self.digest, factory)
        factory.assert_not_called()
        self.assertIn("校验 FPGA 镜像与 SHA-256失败：ValueError", log.getvalue())
        self.assertNotIn("打开指定 USRP", log.getvalue())

    def test_driver_import_error_has_explicit_stage(self):
        log = FlushedLog()
        with patch.dict(sys.modules, {"uhd": None}), redirect_stderr(log), self.assertRaises(ModuleNotFoundError):
            child.check_usrp(self.profile, self.digest)
        self.assertIn("加载 UHD 驱动接口失败：ModuleNotFoundError", log.getvalue())

    def test_identity_error_still_reports_device_release(self):
        log = FlushedLog()
        with redirect_stderr(log), self.assertRaisesRegex(ValueError, "序列号"):
            child.check_usrp(self.profile, self.digest, lambda _: self.device(identity="OTHER"))
        text = log.getvalue()
        self.assertIn("核对收发通道与设备身份失败：ValueError", text)
        self.assertGreater(text.index("释放 USRP"), text.index("身份失败"))
        self.assertNotIn("FAKE_PRIVATE_ID", text)

    def test_serial_logs_open_and_close_without_reading_or_sending(self):
        reader = Mock(spec=["close"])
        factory, log = Mock(return_value=reader), FlushedLog()
        with redirect_stderr(log):
            self.assertIsNone(child.check_serial("/dev/cu.FAKE_PRIVATE_ID", "pressure_v1", factory))
        factory.assert_called_once_with("/dev/cu.FAKE_PRIVATE_ID", 9600)
        reader.close.assert_called_once_with()
        text = log.getvalue()
        self.assertIn("不读数据、不发命令", text)
        self.assertIn("关闭并释放串口完成", text)
        self.assertNotIn("FAKE_PRIVATE_ID", text)

    def test_serial_open_and_close_errors_retain_original_exception(self):
        for phase in ("open", "close"):
            with self.subTest(phase=phase):
                failure = OSError("offline " + phase + " failure")
                reader = Mock(spec=["close"])
                factory = Mock(return_value=reader)
                if phase == "open":
                    factory.side_effect = failure
                else:
                    reader.close.side_effect = failure
                log = FlushedLog()
                with redirect_stderr(log), self.assertRaises(OSError) as caught:
                    child.check_serial("/dev/cu.FAKE", "pressure_v1", factory)
                self.assertIs(caught.exception, failure)
                expected = "打开所选串口" if phase == "open" else "关闭并释放串口"
                self.assertTrue(any(expected in line and "失败：OSError" in line for line in log.getvalue().splitlines()))

    def test_direct_main_restores_python_stdout_and_emits_one_failed_result(self):
        stdout, stderr = io.StringIO(), FlushedLog()

        def fail(*_):
            print("fake driver diagnostic")
            raise RuntimeError("offline device open failed")

        with redirect_stdout(stdout), redirect_stderr(stderr), patch.object(child, "check_usrp", side_effect=fail):
            previous = sys.stdout
            code = child.main(["--kind", "usrp", "--request-id", "offline-request"])
            self.assertIs(sys.stdout, previous)
        self.assertEqual(code, 1)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual((result["event"], result["version"], result["status"]), (child.EVENT, child.PROTOCOL_VERSION, "failed"))
        self.assertIn("fake driver diagnostic", stderr.getvalue())
        self.assertIn("初始化检查失败：RuntimeError: offline device open failed", stderr.getvalue())

    def test_cli_keeps_native_and_late_exit_logs_out_of_result_stdout(self):
        program = textwrap.dedent("""
            import atexit, os, threading, time
            from sspp_desktop import initialize_device as child
            from sspp_experiments.temperature_pressure import serial_io

            class OfflineReader:
                def __init__(self, port, baud):
                    print('python driver open log', flush=True)
                    os.write(1, b'native open log\\n')
                def close(self):
                    def late_log():
                        time.sleep(.05)
                        os.write(1, b'native late worker log\\n')
                    threading.Thread(target=late_log).start()
                    atexit.register(lambda: os.write(1, b'native atexit log\\n'))

            serial_io.PosixSerialReader = OfflineReader
            raise SystemExit(child.cli(['--kind', 'serial', '--request-id', 'offline-request',
                                        '--port', '/dev/cu.FAKE', '--protocol', 'pressure_v1']))
        """)
        result = subprocess.run([sys.executable, "-B", "-u", "-c", program],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        event = json.loads(result.stdout)
        self.assertEqual(set(event), {"event", "version", "request_id", "kind", "status", "message", "released"})
        self.assertEqual((event["event"], event["version"], event["status"], event["released"]),
                         (child.EVENT, child.PROTOCOL_VERSION, "ready", True))
        for message in ("python driver open log", "native open log", "native late worker log", "native atexit log"):
            self.assertIn(message, result.stderr)
            self.assertNotIn(message, result.stdout)
        self.assertIn("关闭并释放串口完成", result.stderr)


if __name__ == "__main__":
    unittest.main()
