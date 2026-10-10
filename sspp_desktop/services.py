"""桌面服务边界：复用采集控制，将实时事件保存在有界内存中。"""

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import math
import os
from pathlib import Path

from sspp_lab.controller import LabController, read_json, write_json
from .config import Settings
from .research import FingerResearchMixin
from .bundle import BundleExportMixin
from .comparison_service import ComparisonMixin


class DesktopService(FingerResearchMixin, BundleExportMixin, ComparisonMixin, LabController):
    def __init__(self, settings=None):
        self.settings = settings or Settings.from_env()
        super().__init__(self.settings.root)
        self._reset_realtime()

    def _reset_realtime(self):
        self._research_reset()
        self.live_points = deque(maxlen=self.settings.max_display_points)
        self.preview = None
        self.revision = 0
        self.trace_seq = 0
        self.display_gaps = 0

    def child_env(self):
        result = dict(os.environ)
        result.update({
            "MPLBACKEND": "Agg",
            "SSPP_TRACE_INTERVAL_MS": str(self.settings.trace_interval_ms),
            "SSPP_PREVIEW_INTERVAL_MS": str(self.settings.preview_interval_ms),
        })
        return result

    def refresh_profiles(self):
        candidates = [
            ("own", "自有 B210", self.settings.own_profile),
            ("teacher", "老师 B210", self.settings.teacher_profile),
        ]
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return [{"id": key, "name": item[0]} for key, item in self.profiles.items()]
            self.profiles = {}
            self.redactions = []
            result = []
            for ident, name, location in candidates:
                path = Path(location)
                if not path.is_absolute():
                    path = self.root / path
                value = read_json(path)
                if not isinstance(value, dict) or not all(
                    key in value for key in ("serial", "fpga", "fpga_sha256", "channel")
                ):
                    continue
                self.profiles[ident] = (name, path)
                self.redactions.append(str(value["serial"]))
                result.append({"id": ident, "name": name, "channel": value["channel"]})
            return result

    def _handle_event(self, event, job):
        """由收集线程调用；不接触 Qt，不读取正在增长的数据文件。"""
        kind = event.get("event")
        if kind != "TRACE":
            self._research_event(event, job)
        if kind == "TRACE":
            seq = int(event.get("seq", self.trace_seq + 1))
            if seq <= self.trace_seq:
                return
            self._research_event(event, job)
            points = event.get("points", [])
            if points and seq != self.trace_seq + 1:
                self.display_gaps += seq - self.trace_seq - 1
                self.live_points.append({
                    "x": points[0]["x"], "amplitude": None, "phase": None,
                    "label": "显示数据间断", "gap": True,
                })
            for point in points:
                x = point.get("x")
                if not isinstance(x, (int, float)) or not math.isfinite(x):
                    continue
                if any(value is not None and not math.isfinite(value)
                       for value in (point.get("amplitude"), point.get("phase"))):
                    continue
                self.live_points.append(dict(point))
            self.trace_seq = seq
            if points:
                last = points[-1]
                job["live"] = {
                    "t_s": last["x"], "amplitude_dB": last.get("amplitude"),
                    "phase_deg": last.get("phase"), "label": last.get("label", ""),
                }
            self.revision += 1
        elif kind == "PREVIEW":
            self.preview = dict(event)
            self.revision += 1
        elif kind == "MEASUREMENT":
            self.revision += 1

    def live_snapshot(self):
        with self.lock:
            mode = self.job["config"]["mode"] if self.job else None
            points = list(self.points) if mode == "sweep" else list(self.live_points)
            return {
                "mode": mode, "points": points, "preview": self.preview,
                "revision": self.revision, "job": deepcopy(self.job),
                "display_gaps": self.display_gaps,
            }

    def _save_context(self, job):
        if not job.get("output"):
            return
        output = self.root / job["output"]
        if not output.is_dir():
            return
        self._save_finger_context(job, output)
        names = [
            "sspp_lab/controller.py", "sspp_lab/realtime.py",
            "sspp_desktop/services.py", "sspp_desktop/window.py", "sspp_desktop/plots.py",
            "sspp_desktop/research.py",
        ]
        write_json(output / "desktop_context.json", {
            "schema_version": 2, "source": "local_desktop_manual_experiment",
            "experiment": "material_sweep" if job["config"]["mode"] == "sweep" else "finger_displacement",
            "job_id": job["id"], "condition": job["config"]["condition"],
            "sensor": job["config"]["sensor"], "parameters": job["config"],
            "initial_setup_user_confirmed": job["config"]["setup_confirmed"],
            "manual_events": self.events, "desktop_status": job["status"],
            "stage_timestamps_are_not_measured_motion": True,
            "rf_stopped_confirmed": job.get("rf_stopped", False),
            "conda_environment": self.settings.conda_env,
            "display_gaps": self.display_gaps,
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "source_hashes": {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                              for name in names if (self.root / name).is_file()},
        })
