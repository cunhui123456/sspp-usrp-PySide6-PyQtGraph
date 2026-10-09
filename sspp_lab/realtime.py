"""Bounded, best-effort display telemetry; recorded measurement data is authoritative.

No hardware or GUI imports. Only the output worker serializes/writes stdout, so a
slow desktop consumer cannot hold the acquisition loop. TRACE and PREVIEW have
independent sequence numbers; a missing sequence must be drawn as a gap. Ordinary
status/log lines use a separate low-volume queue and keep their original order.
"""
from collections import defaultdict
import io
import json
import math
import os
from queue import Empty, Full, Queue, SimpleQueue
import stat
import sys
import threading
import time

import numpy as np


def interval_seconds(name, default_ms):
    """Reject malformed settings before starting a device."""
    value = float(os.environ.get(name, default_ms))
    if not math.isfinite(value) or not 20 <= value <= 5000:
        raise ValueError(f'{name} must be a finite value from 20 to 5000 ms')
    return value / 1000


def trace_point(row):
    """Never turn a missing baseline into a zero relative measurement."""
    point = {'x': float(row['t_s']), 'amplitude': row['relative_amplitude_dB'],
             'phase': row['relative_phase_deg'], 'label': row['label'],
             'absolute_amplitude_dBFS': float(row['amplitude_dBFS_est']),
             'coherent_fraction': float(row['coherent_fraction']),
             'sample_start': int(row['start_sample']), 'sample_count': int(row['count'])}
    for key in ('x', 'amplitude', 'phase', 'absolute_amplitude_dBFS', 'coherent_fraction'):
        if point[key] is not None and not math.isfinite(point[key]):
            raise ValueError(f'Nonfinite trace value: {key}')
    return point


def preview_event(iq, *, rate, carrier_hz, tone_hz, t_s, sample_start, mode):
    """FFT uses contiguous IQ; time display uses the latest 1024 actual samples.

    time_ms is relative to the first displayed IQ sample. sample_start identifies
    that sample in the acquisition's RX index; t_s is the source frame centre.
    spectrum_db is a Hann-window amplitude estimate relative to digital full
    scale, not dBm or a calibrated transfer function.
    """
    rate = float(rate)
    if not all(math.isfinite(float(v)) for v in (rate, carrier_hz, tone_hz, t_s)) or rate <= 0:
        raise ValueError('Invalid preview source metadata')
    values = np.asarray(iq).reshape(-1)
    if len(values) < 8 or not np.isfinite(values).all():
        raise ValueError('Preview requires at least eight finite IQ samples')
    offset = max(0, len(values) - 2048)
    values = values[offset:]
    fft_start = int(sample_start) + offset
    window = np.hanning(len(values))
    magnitude = np.abs(np.fft.fftshift(np.fft.fft(values * window))) / window.sum()
    spectrum = 20 * np.log10(np.maximum(magnitude, 1e-15))
    frequencies = np.fft.fftshift(np.fft.fftfreq(len(values), d=1 / rate)) / 1000
    time_offset = max(0, len(values) - 1024)
    shown = values[time_offset:]
    return {'event': 'PREVIEW', 'mode': mode, 'rate': rate,
            'carrier_hz': float(carrier_hz), 'tone_hz': float(tone_hz), 't_s': float(t_s),
            'sample_start': fft_start + time_offset, 'sample_count': len(shown),
            'fft_sample_start': fft_start, 'fft_sample_count': len(values),
            'source': 'live_rx_contiguous_frame', 'time_origin': 'first_displayed_sample',
            'spectrum_unit': 'dBFS_est',
            'time_ms': (np.arange(len(shown)) * (1000 / rate)).tolist(),
            'I': shown.real.astype(float).tolist(), 'Q': shown.imag.astype(float).tolist(),
            'frequency_khz': frequencies.tolist(), 'spectrum_db': spectrum.tolist()}


class _LineCapture(io.TextIOBase):
    """Collect print fragments per thread before handing complete lines to worker."""
    def __init__(self, publisher, original):
        self.publisher, self.original = publisher, original
        self.buffers = defaultdict(str)
        self.lock = threading.Lock()

    @property
    def encoding(self):
        return getattr(self.original, 'encoding', None) or 'utf-8'

    def writable(self):
        return True

    def isatty(self):
        return False

    def fileno(self):
        return self.original.fileno()

    def write(self, value):
        with self.lock:
            ident = threading.get_ident()
            parts = (self.buffers[ident] + value).split('\n')
            self.buffers[ident] = parts.pop()
            for line in parts:
                self.publisher.status_line(line)
        return len(value)

    def flush(self):
        with self.lock:
            ident = threading.get_ident()
            value = self.buffers.pop(ident, '')
            if value:
                self.publisher.status_line(value)

    def flush_all(self):
        with self.lock:
            for value in self.buffers.values():
                if value:
                    self.publisher.status_line(value)
            self.buffers.clear()


class RealtimePublisher:
    """Bounded display queues and one stdout writer, independent of GUI speed.

    Preview FFT work runs on the output worker, never the receive/processing
    threads. When overloaded, only display packets are evicted. Exact IQ and
    amplitude_phase.jsonl writing stay in their original acquisition code.
    """
    def __init__(self, *, trace_interval=None, preview_interval=None, queue_size=16,
                 output=None, clock=time.monotonic):
        self.trace_interval = (interval_seconds('SSPP_TRACE_INTERVAL_MS', 100)
                               if trace_interval is None else trace_interval)
        self.preview_interval = (interval_seconds('SSPP_PREVIEW_INTERVAL_MS', 250)
                                 if preview_interval is None else preview_interval)
        self.output = output if output is not None else sys.stdout
        self.clock = clock
        self.trace_queue = Queue(maxsize=max(1, queue_size))
        self.preview_queue = Queue(maxsize=1)
        self.status_queue = SimpleQueue()
        self.sequence = {'TRACE': 0, 'PREVIEW': 0}
        self.dropped = {'TRACE': 0, 'PREVIEW': 0}
        self.delivered = {'TRACE': 0, 'PREVIEW': 0}
        self.last_sequence = {'TRACE': 0, 'PREVIEW': 0}
        self.output_error = None
        self.preview_errors = 0
        self.pending = []
        self.last_trace = clock()
        self.last_preview = -math.inf
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread = None
        self.capture = None
        self.output_fd = None
        self.output_was_blocking = None
        self.close_deadline = None

    def start(self, *, capture_stdout=True):
        if self.thread is not None:
            raise RuntimeError('Publisher already started')
        # Use cancellable pipe writes instead of holding TextIO's writer lock.
        # Otherwise a daemon thread blocked by a disconnected desktop can also
        # block Python's final stdout flush after RF has safely stopped.
        try:
            fd = self.output.fileno()
            if stat.S_ISFIFO(os.fstat(fd).st_mode):
                self.output.flush()
                self.output_was_blocking = os.get_blocking(fd)
                os.set_blocking(fd, False)
                self.output_fd = fd
        except (AttributeError, OSError, io.UnsupportedOperation):
            pass
        self.thread = threading.Thread(target=self._run, name='display-output', daemon=True)
        self.thread.start()
        if capture_stdout:
            self.capture = _LineCapture(self, sys.stdout)
            sys.stdout = self.capture
        return self

    def status_line(self, line):
        # Low-volume status and legacy print output retain their exact text/order.
        self.status_queue.put(line)
        self.wake.set()

    def _enqueue(self, kind, payload):
        queue = self.trace_queue if kind == 'TRACE' else self.preview_queue
        self.sequence[kind] += 1
        packet = (self.sequence[kind], payload)
        try:
            queue.put_nowait(packet)
        except Full:
            try:
                queue.get_nowait()
                self.dropped[kind] += 1
            except Empty:
                pass
            try:
                queue.put_nowait(packet)
            except Full:
                self.dropped[kind] += 1
        self.wake.set()

    def trace(self, row):
        self.pending.append(trace_point(row))
        # A frame burst cannot grow a display batch without bound.
        if self.clock() - self.last_trace >= self.trace_interval or len(self.pending) >= 256:
            self.flush_trace()

    def flush_trace(self):
        if self.pending:
            self._enqueue('TRACE', {'event': 'TRACE', 'mode': 'phase', 'points': self.pending})
            self.pending = []
            self.last_trace = self.clock()

    def preview(self, iq, **metadata):
        now = self.clock()
        if now - self.last_preview < self.preview_interval:
            return
        self.last_preview = now
        values = np.asarray(iq).reshape(-1)
        offset = max(0, len(values) - 2048)
        metadata = dict(metadata)
        metadata['sample_start'] += offset
        # This bounded copy is the only preview work done by the producer.
        self._enqueue('PREVIEW', (values[offset:].copy(), metadata))

    def _write(self, line):
        if self.output_fd is not None:
            remaining = memoryview((line + '\n').encode(self.output.encoding or 'utf-8'))
            while remaining:
                if self.close_deadline is not None and time.monotonic() >= self.close_deadline:
                    raise TimeoutError('Display output drain deadline exceeded')
                try:
                    count = os.write(self.output_fd, remaining)
                    if not count:
                        raise BrokenPipeError('Display output closed')
                    remaining = remaining[count:]
                except BlockingIOError:
                    # No acquisition thread waits here; close() can cancel this
                    # bounded wait even when the consumer never reads again.
                    time.sleep(.005)
            return
        self.output.write(line + '\n')
        self.output.flush()

    def _display(self, kind, packet):
        sequence, payload = packet
        if kind == 'PREVIEW':
            values, metadata = payload
            try:
                event = preview_event(values, **metadata)
            except (ValueError, TypeError, FloatingPointError):
                self.preview_errors += 1
                self.dropped[kind] += 1
                return
        else:
            event = payload
        missing = sequence - self.last_sequence[kind] - 1
        event.update(seq=sequence, dropped_total=self.dropped[kind],
                     dropped_since_last=missing, gap_before=missing > 0)
        self._write(json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
        self.last_sequence[kind] = sequence
        self.delivered[kind] += 1

    def _run(self):
        try:
            while True:
                self.wake.clear()
                worked = False
                # Limit a log burst so traces/previews are not starved.
                for _ in range(32):
                    try:
                        line = self.status_queue.get_nowait()
                    except Empty:
                        break
                    self._write(line)
                    worked = True
                for kind, queue in (('TRACE', self.trace_queue), ('PREVIEW', self.preview_queue)):
                    try:
                        packet = queue.get_nowait()
                    except Empty:
                        continue
                    self._display(kind, packet)
                    worked = True
                if self.stopping.is_set() and not worked:
                    return
                if not worked:
                    self.wake.wait(.1)
        except (BrokenPipeError, OSError, ValueError) as exc:
            # An unavailable display must not stop an otherwise valid recording.
            self.output_error = type(exc).__name__

    def stats(self):
        return {'display_only': True, 'trace_packets': self.sequence['TRACE'],
                'preview_packets': self.sequence['PREVIEW'],
                'trace_dropped': self.dropped['TRACE'], 'preview_dropped': self.dropped['PREVIEW'],
                'trace_delivered': self.delivered['TRACE'], 'preview_delivered': self.delivered['PREVIEW'],
                'preview_errors': self.preview_errors, 'output_error': self.output_error}

    def close(self, timeout=2):
        self.flush_trace()
        if self.capture:
            self.capture.flush_all()
        self.stopping.set()
        self.close_deadline = time.monotonic() + timeout
        self.wake.set()
        if self.thread:
            self.thread.join(timeout + .05)
        stopped = self.thread is None or not self.thread.is_alive()
        if stopped and self.output_fd is not None:
            try:
                os.set_blocking(self.output_fd, self.output_was_blocking)
            except OSError:
                pass
        if self.capture and sys.stdout is self.capture:
            sys.stdout = self.capture.original
        return stopped
