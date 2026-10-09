"""用真实历史记录驱动子进程显示通道；不是新实验，不初始化硬件。"""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService


def test_recorded_data_arrives_before_child_finishes():
    root = Settings().root
    files = sorted((root / "Data/phase_runs").glob("*/amplitude_phase.jsonl"))
    if not files:
        import pytest
        pytest.skip("No copied historical phase records")
    code = r'''
import json,sys,time
from pathlib import Path
import numpy as np
from sspp_lab.realtime import RealtimePublisher
path=Path(sys.argv[1])
rows=[json.loads(line) for line in path.read_text().splitlines()]
rows=[r for r in rows if r.get('relative_phase_deg') is not None][:60]
manifest=json.loads((path.parent/'manifest.json').read_text())
sdr=manifest.get('actual_sdr') or manifest['requested_sdr']
rate=sdr.get('rx_rate_actual') or sdr['rate']
raw=np.memmap(path.parent/'rx_iq.cf32',dtype=np.complex64,mode='r')
publisher=RealtimePublisher(trace_interval=.1,preview_interval=.25).start()
for row in rows:
    publisher.trace(row)
    first=row['start_sample'];end=first+row['count']
    publisher.preview(raw[first:end],rate=rate,carrier_hz=sdr['freq'],tone_hz=sdr['tone'],
                      t_s=row['t_s'],sample_start=first,mode='phase')
    time.sleep(.02)
publisher.close()
'''
    service = DesktopService()
    job = {"config": {"mode": "phase"}, "output": None, "errors": [], "rf_stopped": False}
    service.job = job
    arrivals = []
    original = service._handle_event

    def observe(event, current):
        if event.get("event") == "TRACE":
            arrivals.append(time.monotonic())
        original(event, current)

    service._handle_event = observe
    proc = subprocess.Popen([sys.executable, "-B", "-u", "-c", code, str(files[-1])],
                            cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    collector = threading.Thread(target=service._collect, args=(proc, job))
    started = time.monotonic()
    collector.start()
    try:
        deadline = time.monotonic() + 5
        while not arrivals and time.monotonic() < deadline:
            time.sleep(.01)
        assert arrivals, "No real-time packets received"
        assert proc.poll() is None, "Data was delayed until subprocess exit"
        assert arrivals[0] - started < 3
        proc.wait(timeout=6)
        collector.join(timeout=2)
        assert not collector.is_alive()
        assert proc.returncode == 0, proc.stderr.read()
        snap = service.live_snapshot()
        assert len(snap["points"]) == 60
        assert snap["preview"]["source"] == "live_rx_contiguous_frame"
        assert snap["display_gaps"] == 0
        # 核实链路输送的幅相值仍是原文件，不是另造的示例波形。
        rows = [json.loads(line) for line in files[-1].read_text().splitlines()]
        rows = [r for r in rows if r.get("relative_phase_deg") is not None][:60]
        assert [p["phase"] for p in snap["points"]] == [r["relative_phase_deg"] for r in rows]
        assert [p["amplitude"] for p in snap["points"]] == [r["relative_amplitude_dB"] for r in rows]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)
        collector.join(timeout=2)
        proc.stdout.close()
        proc.stderr.close()
