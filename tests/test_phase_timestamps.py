"""UHD TimeSpec and fake-stream regressions; never discover or create a USRP."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
from queue import Queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import uhd

from sspp_lab.dsp import HardwareTimeContinuity, SampleContinuity, TimestampDiscontinuity
from sspp_lab.acquire_phase import receive, request_stop, record_failure, transmit


RATE = 250000.
CLOCK = 32000000.
TICKS_PER_SAMPLE = 128
BASE = uhd.types.TimeSpec.from_ticks(661081 * TICKS_PER_SAMPLE + 64, CLOCK)


def continuous_times(count=12):
    # This is the fractional packet-offset addition used by UHD. Independently
    # rounding these absolute times at RX rate really does yield +/-1 jumps.
    return [BASE + uhd.types.TimeSpec(i * 5000 / RATE) for i in range(count)]


def telemetry():
    return {'rx_samples': 0, 'rx_frames': 0, 'rx_timeouts': 0, 'peak_component': 0.,
            'tx_samples': 0, 'tx_partial_sends': 0, 'tx_end_of_burst_sent': False}


class FakeReceiver:
    def __init__(self, timestamps, stop):
        self.timestamps, self.stop = iter(timestamps), stop

    def recv(self, buffer, metadata, timeout):
        try:
            timestamp = next(self.timestamps)
        except StopIteration:
            request_stop(self.stop, {}, 'test_input_finished')
            return 0
        metadata.error_code = uhd.types.RXMetadataErrorCode.none
        metadata.out_of_sequence = False
        metadata.has_time_spec = True
        metadata.time_spec = timestamp
        buffer[0, :5000] = .001 + .0005j
        return 5000


class NativeClockTests(unittest.TestCase):
    def test_half_sample_origin_reproduces_old_error_and_passes_native_clock(self):
        times = continuous_times()
        legacy = SampleContinuity()
        with self.assertRaises(RuntimeError):
            for timestamp in times:
                legacy.check(timestamp.to_ticks(RATE), 5000)
        timeline = HardwareTimeContinuity(CLOCK, RATE)
        frames = [timeline.check(timestamp, 5000) for timestamp in times]
        self.assertEqual(timeline.samples, 12 * 5000)
        self.assertTrue(all(frame['delta_master_ticks'] == 0 for frame in frames[1:]))
        self.assertEqual(frames[0]['device_tick_at_master_rate'] % 128, 64)
        self.assertTrue(any(frames[i]['device_tick_at_rx_rate'] -
                            frames[i-1]['device_tick_at_rx_rate'] != 5000
                            for i in range(1, len(frames))))

    def test_packet_fragments_and_variable_frame_lengths_keep_exact_clock_origin(self):
        timeline = HardwareTimeContinuity(CLOCK, RATE)
        position = 0
        for count in [137, 4096, 767, 5000, 213, 1, 64, 4999] * 40:
            # Independently construct a packet timestamp and its fragment offset.
            fragment = position % 1020
            packet = BASE + uhd.types.TimeSpec((position - fragment) / RATE)
            timestamp = packet + uhd.types.TimeSpec(fragment / RATE)
            frame = timeline.check(timestamp, count)
            self.assertEqual(frame['start_sample'], position)
            self.assertIn(frame['delta_master_ticks'], (None, 0))
            position += count
        self.assertEqual(timeline.samples, position)

    def test_long_uptime_and_long_capture_do_not_collapse_full_fraction_seconds(self):
        for whole in (0, 86400, 1000000000):
            with self.subTest(whole=whole):
                timeline = HardwareTimeContinuity(CLOCK, RATE)
                base = uhd.types.TimeSpec(whole, BASE.get_frac_secs())
                # Several hours of device time; no arrays or actual reception.
                for index in range(4000):
                    frame = timeline.check(base + uhd.types.TimeSpec(index * 1.0), 250000)
                    self.assertIn(frame['delta_master_ticks'], (None, 0))
                self.assertEqual(timeline.samples, 1000000000)

    def test_real_sample_loss_and_overlap_and_single_clock_tick_are_rejected(self):
        for delta in (-128, 128, -1, 1):
            with self.subTest(delta=delta):
                timeline = HardwareTimeContinuity(CLOCK, RATE)
                first = timeline.check(BASE, 5000)
                expected = first['device_tick_at_master_rate'] + 5000 * 128
                wrong = uhd.types.TimeSpec.from_ticks(expected + delta, CLOCK)
                with self.assertRaises(TimestampDiscontinuity) as raised:
                    timeline.check(wrong, 5000)
                evidence = raised.exception.evidence
                self.assertEqual(evidence['delta_master_ticks'], delta)
                self.assertEqual(evidence['delta_samples'], delta / 128)
                self.assertEqual(evidence['time_spec_full_secs'], wrong.get_full_secs())
                self.assertEqual(evidence['time_spec_frac_secs'], wrong.get_frac_secs())
                self.assertEqual(timeline.samples, 5000)
                self.assertEqual(timeline.next_tick, expected)

    def test_other_integer_decimation_and_float_representation_are_supported(self):
        for factor in (8, 16, 37, 61, 128, 160):
            with self.subTest(factor=factor):
                timeline = HardwareTimeContinuity(CLOCK, CLOCK / factor)
                self.assertEqual(timeline.ticks_per_sample, factor)
                for start in range(0, 10000, 1000):
                    timestamp = uhd.types.TimeSpec.from_ticks(700003 + start * factor, CLOCK)
                    timeline.check(timestamp, 1000)

    def test_fractional_ratio_is_explicitly_rejected(self):
        for rate in (441000., 3e7, 64e6, float('nan'), 0):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                HardwareTimeContinuity(CLOCK, rate)


class ReceiveIntegrationTests(unittest.TestCase):
    def run_receiver(self, timestamps, folder):
        stop = threading.Event()
        info, errors, queue = telemetry(), [], Queue(maxsize=128)
        # UHD metadata exposes read-only fields to Python; only the native recv
        # normally populates them. Replace that container for our fake recv.
        with patch.object(uhd.types, 'RXMetadata', side_effect=SimpleNamespace):
            receive(FakeReceiver(timestamps, stop), RATE, Path(folder), queue, stop, info, errors, CLOCK)
        return info, errors, queue

    def test_fake_receive_keeps_all_half_origin_frames_and_legacy_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            info, errors, queue = self.run_receiver(continuous_times(), folder)
            self.assertEqual(errors, [])
            self.assertEqual(info['rx_samples'], 60000)
            self.assertEqual(queue.qsize(), 12)
            raw = np.fromfile(Path(folder)/'rx_iq.cf32', dtype=np.complex64)
            np.testing.assert_array_equal(raw, np.full(60000, .001+.0005j, np.complex64))
            frames = [json.loads(line) for line in (Path(folder)/'rx_frames.jsonl').read_text().splitlines()]
            self.assertEqual(len(frames), 12)
            self.assertTrue(all(row['delta_master_ticks'] == 0 for row in frames[1:]))
            self.assertFalse((Path(folder)/'rx_timestamp_failure.json').exists())

    def test_fake_receive_stops_at_real_gap_and_saves_rejected_timestamp(self):
        for delta in (-128, 128):
            with self.subTest(delta=delta), tempfile.TemporaryDirectory() as folder:
                times = continuous_times(5)
                expected = BASE.to_ticks(CLOCK) + 25000 * 128
                times.append(uhd.types.TimeSpec.from_ticks(expected + delta, CLOCK))
                info, errors, queue = self.run_receiver(times, folder)
                self.assertEqual(info['stop_reason'], 'rx_timestamp_discontinuity')
                self.assertEqual(info['rx_samples'], 25000)
                self.assertEqual(queue.qsize(), 5)
                self.assertEqual((Path(folder)/'rx_iq.cf32').stat().st_size, 25000 * 8)
                self.assertEqual(len(errors), 1)
                evidence = json.loads((Path(folder)/'rx_timestamp_failure.json').read_text())
                self.assertEqual(evidence, info['timestamp_failure'])
                self.assertEqual(evidence['delta_master_ticks'], delta)
                self.assertEqual(evidence['expected_device_tick_at_master_rate'], expected)
                self.assertFalse(evidence['accepted_into_iq'])

    def test_tx_worker_failure_preserves_cause_during_shutdown(self):
        stream = Mock()
        stream.send.side_effect = RuntimeError('fake disconnected stream')
        stop, info, errors = threading.Event(), telemetry(), []
        sdr = SimpleNamespace(amp=.1, tone=10000., tx_rate_actual=RATE)
        transmit(stream, sdr, uhd.types.TimeSpec(.7), stop, info, errors)
        self.assertTrue(stop.is_set())
        self.assertEqual(info['stop_reason'], 'tx_error')
        self.assertEqual(len(errors), 2)

    def test_first_stop_cause_is_not_replaced_by_shutdown_failure(self):
        for reason in ('operator_stop', 'duration_limit', 'signal_2', 'wall_clock_watchdog',
                       'initialization_error', 'acquisition_error'):
            with self.subTest(reason=reason):
                stop, info, errors = threading.Event(), {}, []
                request_stop(stop, info, reason)
                record_failure(stop, info, errors, 'rx_shutdown_error', 'RX shutdown: test error')
                self.assertEqual(info['stop_reason'], reason)
                self.assertEqual(errors, ['RX shutdown: test error'])

    def test_unsupported_ratio_fails_before_stream_creation_and_records_initialization_cause(self):
        manager, manifest = self.run_fake_main(441000.)
        manager.create_streamers.assert_not_called()
        manager.stop_rx_stream.assert_not_called()
        self.assertEqual(manifest['stop_reason'], 'initialization_error')
        self.assertEqual(manifest['status'], 'failed_or_incomplete')
        self.assertTrue(any('integer master-clock ticks' in error for error in manifest['errors']))

    def test_wall_clock_watchdog_is_recorded_instead_of_duration_limit(self):
        manager, manifest = self.run_fake_main(RATE, watchdog=True)
        manager.stop_rx_stream.assert_called_once()
        self.assertEqual(manifest['stop_reason'], 'wall_clock_watchdog')
        self.assertEqual(manifest['processed_samples'], 0)
        self.assertTrue(manifest['telemetry']['tx_end_of_burst_sent'])
        self.assertTrue(manifest['telemetry']['workers_stopped'])
        self.assertEqual(manifest['status'], 'failed_or_incomplete')

    def test_main_processing_failure_records_acquisition_error_before_rf_stopped(self):
        manager, manifest = self.run_fake_main(RATE, acquisition_error=True)
        manager.stop_rx_stream.assert_called_once()
        self.assertEqual(manifest['stop_reason'], 'acquisition_error')
        self.assertTrue(any('fake DSP failure' in error for error in manifest['errors']))
        self.assertTrue(manifest['telemetry']['workers_stopped'])

    def run_fake_main(self, rate, watchdog=False, acquisition_error=False):
        import sspp_lab.acquire_phase as phase
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = root/'fake_fpga.bin'
            image.write_bytes(b'not a hardware image')
            profile = root/'profile.json'
            profile.write_text(json.dumps({'serial': 'offline_test', 'fpga': str(image),
                                          'fpga_sha256': hashlib.sha256(image.read_bytes()).hexdigest(),
                                          'channel': 0}))
            opts = SimpleNamespace(command='acquire', device_config=profile, gui=False,
                                   output_root=root/'runs', duration=10., initial_label='test',
                                   carrier=2.4e9, tone=10000., rate=rate, amplitude=.1,
                                   tx_gain=20., rx_gain=30.)
            manager = Mock()
            manager.usrp.get_master_clock_rate.return_value = CLOCK
            manager.usrp.get_tx_gain.return_value = 20.
            manager.usrp.get_rx_gain.return_value = 30.
            manager.usrp.get_tx_antenna.return_value = 'TX/RX'
            manager.usrp.get_rx_antenna.return_value = 'RX2'
            manager.usrp.get_time_now.return_value = uhd.types.TimeSpec(2.)
            tx, rx = Mock(), Mock()
            tx.send.side_effect = lambda samples, metadata, timeout: len(samples)
            tx.recv_async_msg.return_value = False
            manager.create_streamers.return_value = (tx, rx)
            def build_manager(sdr):
                sdr.rx_rate_actual = sdr.tx_rate_actual = rate
                return manager
            def timer_factory(interval, operation):
                timer = Mock()
                if watchdog:
                    timer.start.side_effect = operation
                return timer
            original_receive = phase.receive
            def receive_fixture(stream, actual_rate, output, queue, stop, info, errors, clock):
                if acquisition_error:
                    queue.put_nowait((0, np.full(5000, .001+.0005j, np.complex64)))
                    stop.wait(2)
                else:
                    original_receive(stream, actual_rate, output, queue, stop, info, errors, clock)
            original_process = phase.RelativeMonitor.process
            def process_fixture(monitor, *args):
                if acquisition_error:
                    raise RuntimeError('fake DSP failure')
                return original_process(monitor, *args)
            publisher = Mock()
            publisher.stats.return_value = {}
            with patch.object(phase, 'options', return_value=opts), \
                 patch('sspp_lab.hardware.USRPManager', side_effect=build_manager), \
                 patch.object(phase, 'RealtimePublisher', return_value=publisher), \
                 patch.object(phase, 'receive', side_effect=receive_fixture), \
                 patch.object(phase.RelativeMonitor, 'process', process_fixture), \
                 patch.object(phase.select, 'select', return_value=([], [], [])), \
                 patch.object(phase.threading, 'Timer', side_effect=timer_factory), \
                 patch.object(phase.signal, 'signal'), redirect_stdout(io.StringIO()):
                self.assertEqual(phase.main(), 2)
            manifest = json.loads(next((root/'runs').glob('*/manifest.json')).read_text())
            return manager, manifest


if __name__ == '__main__':
    unittest.main()
