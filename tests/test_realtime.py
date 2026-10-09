"""Offline transport tests: no device discovery, initialization, or RF streaming."""
import io
import json
import os
from pathlib import Path
from queue import Queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from sspp_lab.realtime import RealtimePublisher, interval_seconds, preview_event, trace_point


def row(index, baseline=True):
    return {'t_s': index * .02, 'relative_amplitude_dB': .5 if baseline else None,
            'relative_phase_deg': 12. if baseline else None, 'label': 'static',
            'amplitude_dBFS_est': -20., 'coherent_fraction': .99,
            'start_sample': index * 5000, 'count': 5000}


def metadata(**changes):
    return {'rate': 256000., 'carrier_hz': 2.4e9, 'tone_hz': 10000.,
            't_s': .03, 'sample_start': 5000, 'mode': 'phase', **changes}


class RealtimeTests(unittest.TestCase):
    def test_preview_uses_true_frequency_samples_and_digital_amplitude(self):
        rate = 256000.
        samples = .2 * np.exp(2j * np.pi * 10000 * np.arange(5000) / rate)
        original = samples.copy()
        event = preview_event(samples, **metadata())
        self.assertEqual(len(event['I']), 1024)
        self.assertEqual(len(event['frequency_khz']), 2048)
        self.assertEqual(event['sample_start'], 5000 + 5000 - 1024)
        self.assertEqual(event['fft_sample_start'], 5000 + 5000 - 2048)
        self.assertAlmostEqual(event['time_ms'][-1], 1023 * 1000 / rate)
        peak = int(np.argmax(event['spectrum_db']))
        self.assertAlmostEqual(event['frequency_khz'][peak], 10.)
        self.assertAlmostEqual(event['spectrum_db'][peak], 20 * np.log10(.2), places=8)
        np.testing.assert_array_equal(samples, original)
        np.testing.assert_allclose(event['I'], samples[-1024:].real)
        json.dumps(event, allow_nan=False)

    def test_relative_trace_preserves_unknown_baseline_and_source(self):
        original = row(0, baseline=False)
        result = trace_point(original)
        self.assertIsNone(result['amplitude'])
        self.assertIsNone(result['phase'])
        self.assertEqual(result['absolute_amplitude_dBFS'], -20.)
        self.assertEqual(result['sample_count'], 5000)
        self.assertEqual(original, row(0, baseline=False))
        with self.assertRaises(ValueError):
            trace_point({**original, 'relative_phase_deg': float('nan')})

    def test_normal_trace_delivery_keeps_every_processed_point(self):
        now = [0.]
        output = io.StringIO()
        publisher = RealtimePublisher(output=output, clock=lambda: now[0],
                                      trace_interval=.1, preview_interval=.25)
        for index in range(18):
            now[0] = index * .02
            publisher.trace(row(index, baseline=index >= 5))
        publisher.start(capture_stdout=False)
        self.assertTrue(publisher.close())
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        points = [point for event in events for point in event['points']]
        self.assertEqual([point['x'] for point in points], [i * .02 for i in range(18)])
        self.assertEqual([event['seq'] for event in events], list(range(1, len(events) + 1)))
        self.assertFalse(any(event['gap_before'] for event in events))
        self.assertTrue(all(point['phase'] is None for point in points[:5]))

    def test_overloaded_trace_reports_missing_batches(self):
        output = io.StringIO()
        publisher = RealtimePublisher(output=output, queue_size=2)
        for index in range(5):
            publisher.trace(row(index))
            publisher.flush_trace()
        self.assertEqual(publisher.trace_queue.qsize(), 2)
        publisher.start(capture_stdout=False)
        self.assertTrue(publisher.close())
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([event['seq'] for event in events], [4, 5])
        self.assertTrue(events[0]['gap_before'])
        self.assertEqual(events[0]['dropped_since_last'], 3)
        self.assertEqual(publisher.stats()['trace_dropped'], 3)

    def test_slow_consumer_does_not_hold_acquisition_producer(self):
        class SlowStream(io.StringIO):
            def __init__(self):
                super().__init__()
                self.entered, self.release = threading.Event(), threading.Event()

            def write(self, value):
                self.entered.set()
                self.release.wait(2)
                return super().write(value)

        output = SlowStream()
        publisher = RealtimePublisher(output=output, queue_size=2, preview_interval=0)
        publisher.start(capture_stdout=False)
        publisher.status_line('{"event":"ACQUIRING"}')
        self.assertTrue(output.entered.wait(1))
        samples = np.ones(5000, dtype=np.complex64) * .1
        started = time.monotonic()
        try:
            for index in range(100):
                publisher.trace(row(index))
                publisher.flush_trace()
                publisher.preview(samples, **metadata(sample_start=index * 5000))
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, .5)
            self.assertEqual(publisher.trace_queue.qsize(), 2)
            self.assertEqual(publisher.preview_queue.qsize(), 1)
            self.assertGreater(publisher.stats()['trace_dropped'], 0)
            self.assertGreater(publisher.stats()['preview_dropped'], 0)
            np.testing.assert_array_equal(samples, np.ones(5000, dtype=np.complex64) * .1)
        finally:
            output.release.set()
            self.assertTrue(publisher.close())
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[0]['event'], 'ACQUIRING')
        self.assertTrue(any(event.get('gap_before') for event in events))

    def test_stdout_print_fragments_do_not_interleave_json(self):
        output = io.StringIO()
        publisher = RealtimePublisher(output=output).start()
        def worker(ident):
            for index in range(30):
                print(json.dumps({'event': 'LOG', 'thread': ident, 'index': index}), flush=True)
        try:
            threads = [threading.Thread(target=worker, args=(n,)) for n in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            self.assertTrue(publisher.close())
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(events), 90)
        for ident in range(3):
            self.assertEqual([event['index'] for event in events if event['thread'] == ident], list(range(30)))

    def test_full_os_pipe_can_close_without_a_blocked_output_thread(self):
        reader, writer = os.pipe()
        with os.fdopen(writer, 'w', buffering=1) as output:
            publisher = RealtimePublisher(output=output).start(capture_stdout=False)
            try:
                publisher.status_line('x' * 1000000)
                started = time.monotonic()
                self.assertTrue(publisher.close(timeout=.05))
                self.assertLess(time.monotonic() - started, .3)
                self.assertEqual(publisher.output_error, 'TimeoutError')
                self.assertTrue(os.get_blocking(writer))
            finally:
                os.close(reader)

    def test_preview_throttle_and_invalid_values_do_not_forge_data(self):
        now = [0.]
        output = io.StringIO()
        publisher = RealtimePublisher(output=output, clock=lambda: now[0], preview_interval=.25)
        samples = np.ones(64, dtype=np.complex64)
        publisher.preview(samples, **metadata())
        now[0] = .1
        publisher.preview(samples, **metadata())
        self.assertEqual(publisher.sequence['PREVIEW'], 1)
        now[0] = .3
        publisher.preview(samples * np.nan, **metadata())
        publisher.start(capture_stdout=False)
        self.assertTrue(publisher.close())
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(publisher.stats()['preview_errors'], 1)

    def test_sweep_current_frame_is_available_before_point_file(self):
        from sspp_lab.acquire_sweep import make_runner
        from sspp_lab.sweep_logic import SweepConfig
        samples = np.ones(1000, dtype=np.complex64) * .1
        frames = Queue()
        frames.put_nowait((7000, samples))
        captured = []
        publisher = SimpleNamespace(preview=lambda iq, **meta: captured.append((iq.copy(), meta)))
        args = SimpleNamespace(rx_rate_actual=250000.)
        with tempfile.TemporaryDirectory() as folder:
            runner = make_runner(args, SweepConfig(), None, frames, threading.Event(), None,
                                 Path(folder), SimpleNamespace(point_timeout=8), [], publisher)
            runner.deadline = time.monotonic() + 5
            runner.point_info = {'rx_actual_carrier_hz': 2.4e9, 'tone_hz': 10000.}
            result, latest = runner._fetch_available_frames()
            self.assertEqual(len(result), 1)
            self.assertIs(latest, samples)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0][1]['sample_start'], 7000)
            self.assertEqual(list(Path(folder).iterdir()), [])
            np.testing.assert_array_equal(captured[0][0], samples)
            np.testing.assert_array_equal(runner.frames[0], samples)

    def test_environment_interval_rejects_invalid_values(self):
        with patch.dict(os.environ, {'SSPP_TEST_INTERVAL_MS': '100'}):
            self.assertEqual(interval_seconds('SSPP_TEST_INTERVAL_MS', 250), .1)
        for value in ('nan', '0', '5001', 'invalid'):
            with patch.dict(os.environ, {'SSPP_TEST_INTERVAL_MS': value}):
                with self.assertRaises(ValueError):
                    interval_seconds('SSPP_TEST_INTERVAL_MS', 250)


if __name__ == '__main__':
    unittest.main()
