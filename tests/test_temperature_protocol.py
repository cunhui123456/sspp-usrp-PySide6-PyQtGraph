"""已有 Arduino 协议：碎片、两路时钟、错误和复位，不使用设备。"""

import unittest

from sspp_experiments.temperature_pressure.protocol import LineParser, TEMPERATURE_HEADER

STAMP = {"host_time_utc": "2026-10-13T00:00:00+00:00", "host_elapsed_s": 1.0}


class TemperatureProtocolTests(unittest.TestCase):
    def test_fragmented_pressure_keeps_voltage_and_mcu_time_separate(self):
        parser = LineParser("pressure_v1", "A2-P2")
        self.assertEqual(parser.feed(b"1000,2.", **STAMP), ([], []))
        rows, events = parser.feed(b"71\r\n1050,2.72\n", **STAMP)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["mcu_time_ms"], 1000)
        self.assertEqual(rows[0]["voltage_v"], 2.71)
        self.assertEqual(rows[0]["channel"], "A2-P2")
        self.assertEqual(rows[0]["adc"], "")
        self.assertEqual(rows[0]["host_elapsed_s"], 1.0)
        self.assertEqual(rows[1]["mcu_elapsed_s"], .05)
        self.assertEqual(events, [])

    def test_temperature_six_columns_header_and_firmware_error(self):
        parser = LineParser("temperature_v1")
        data = ("# Temperature_Channel_Check v1\n" + TEMPERATURE_HEADER + "\n100,600.5,2.935,140,650,3.177\n# ERROR: S6 failed; row omitted\n").encode()
        rows, events = parser.feed(data, **STAMP)
        self.assertEqual([row["channel"] for row in rows], ["S6", "S7"])
        self.assertEqual([row["mcu_time_ms"] for row in rows], [100, 140])
        self.assertEqual(rows[0]["adc"], 600.5)
        self.assertEqual([event["kind"] for event in events], ["firmware_status", "protocol_header", "firmware_error"])

    def test_reset_starts_segment_and_rollover_preserves_segment(self):
        parser = LineParser("pressure_v1")
        rows, events = parser.feed(b"4294967290,2.1\n5,2.2\n1,2.3\n", **STAMP)
        self.assertEqual([row["time_segment"] for row in rows], [0, 0, 1])
        self.assertAlmostEqual(rows[1]["mcu_elapsed_s"], .011)
        self.assertEqual(rows[2]["mcu_elapsed_s"], 0)
        self.assertEqual([event["kind"] for event in events], ["mcu_wrap", "mcu_reset"])

    def test_temperature_reset_is_one_new_segment_for_both_channels(self):
        parser = LineParser("temperature_v1")
        rows, events = parser.feed(b"1000,600,2.9,1040,650,3.1\n100,600,2.9,140,650,3.1\n", **STAMP)
        self.assertEqual([row["time_segment"] for row in rows], [0, 0, 1, 1])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "mcu_reset")

    def test_temperature_restart_banner_segments_even_if_new_millis_is_larger(self):
        parser = LineParser("temperature_v1")
        rows, _ = parser.feed(b"100,600,2.9,140,650,3.1\n", **STAMP)
        self.assertEqual(rows[0]["time_segment"], 0)
        rows, events = parser.feed(b"# Temperature_Channel_Check v1; A1\n200,600,2.9,240,650,3.1\n", **STAMP)
        self.assertEqual([row["time_segment"] for row in rows], [1, 1])
        self.assertEqual([event["kind"] for event in events], ["firmware_status", "mcu_reset"])

    def test_invalid_rows_and_encoding_never_become_samples(self):
        parser = LineParser("pressure_v1")
        rows, events = parser.feed(b"nan,2.7\n100,nan\n100,inf\n100,-1\n100,5.2\n1,2,3\n\xff\n", **STAMP)
        self.assertEqual(rows, [])
        self.assertEqual(len(events), 7)
        self.assertEqual(events[-1]["kind"], "invalid_encoding")

    def test_overlong_and_partial_tail_are_reported(self):
        parser = LineParser("pressure_v1", max_line_bytes=10)
        rows, events = parser.feed(b"x" * 100 + b"\n10,2.7\n12,", **STAMP)
        self.assertEqual(len(rows), 1)
        self.assertEqual(events[0]["kind"], "line_too_long")
        self.assertEqual(parser.finish(**STAMP)[0]["kind"], "truncated_line")


if __name__ == "__main__":
    unittest.main()
