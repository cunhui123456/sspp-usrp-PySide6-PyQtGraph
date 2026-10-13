"""POSIX 接收端的纯模拟 syscall 验证；不打开真实设备。"""

import os
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sspp_experiments.temperature_pressure.serial_io import PosixSerialReader, list_serial_ports


class TemperatureSerialIoTests(unittest.TestCase):
    def test_port_inventory_only_stats_names_without_open(self):
        with patch("sspp_experiments.temperature_pressure.serial_io.glob.glob", return_value=["/dev/cu.A", "/dev/cu.B"]), \
             patch("sspp_experiments.temperature_pressure.serial_io.os.stat", side_effect=[SimpleNamespace(st_mode=stat.S_IFCHR), SimpleNamespace(st_mode=stat.S_IFREG)]), \
             patch("sspp_experiments.temperature_pressure.serial_io.os.open") as opened:
            self.assertEqual(list_serial_ports(), ["/dev/cu.A"])
            opened.assert_not_called()

    def test_read_only_port_and_settings_close_without_serial_commands(self):
        attrs = [0, 0, 0, 0, 0, 0, [0] * 32]
        with patch("sspp_experiments.temperature_pressure.serial_io.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR)), \
             patch("sspp_experiments.temperature_pressure.serial_io.os.open", return_value=999) as opened, \
             patch("sspp_experiments.temperature_pressure.serial_io.fcntl.flock"), \
             patch("sspp_experiments.temperature_pressure.serial_io.fcntl.ioctl"), \
             patch("sspp_experiments.temperature_pressure.serial_io.termios.tcgetattr", side_effect=lambda fd: [*attrs[:6], list(attrs[6])]), \
             patch("sspp_experiments.temperature_pressure.serial_io.termios.tcsetattr") as setting, \
             patch("sspp_experiments.temperature_pressure.serial_io.select.select", side_effect=[([], [], []), ([999], [], [])]), \
             patch("sspp_experiments.temperature_pressure.serial_io.os.read", return_value=b"100,2.7\n"), \
             patch("sspp_experiments.temperature_pressure.serial_io.os.write") as writing, \
             patch("sspp_experiments.temperature_pressure.serial_io.os.close") as closing:
            reader = PosixSerialReader("/dev/cu.FAKE")
            self.assertEqual(opened.call_args.args, ("/dev/cu.FAKE", os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK))
            self.assertIsNone(reader.read())
            self.assertEqual(reader.read(), b"100,2.7\n")
            reader.close()
            reader.close()
            closing.assert_called_once_with(999)
            self.assertEqual(setting.call_count, 2)
            writing.assert_not_called()

    def test_setup_failure_closes_fd(self):
        with patch("sspp_experiments.temperature_pressure.serial_io.os.stat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR)), \
             patch("sspp_experiments.temperature_pressure.serial_io.os.open", return_value=999), \
             patch("sspp_experiments.temperature_pressure.serial_io.fcntl.flock", side_effect=OSError("busy")), \
             patch("sspp_experiments.temperature_pressure.serial_io.os.close") as closing:
            with self.assertRaises(OSError):
                PosixSerialReader("/dev/cu.FAKE")
            closing.assert_called_once_with(999)


if __name__ == "__main__":
    unittest.main()
