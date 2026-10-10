"""手指研究流程的应用层：人工真值、采样窗口和可追溯派生结果。

不初始化硬件，不接触 Qt。实时控制和快照仅操作内存；JSONL/导出由分析工作线程执行。
"""
from copy import deepcopy
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import threading
import time
import uuid

from sspp_lab.controller import LabError, read_json, write_json

BUSY = {"starting", "running", "stopping", "saving"}


def finite(value, name, low=0, high=10000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise LabError(f"{name} 必须为 {low:g}–{high:g} 的有限数值")
    return float(value)


class FingerResearchMixin:
    def _research_reset(self):
        self.finger_annotations = []
        self.finger_actions = []
        self.finger_settings = {}
        self.finger_pending = None
        self.finger_analysis = None
        self.finger_error = ""
        self.finger_last_trace_wall = None
        if not hasattr(self, "finger_analysis_lock"):
            self.finger_analysis_lock = threading.Lock()

    def _finger_key(self):
        output = (self.job or {}).get("output")
        if not output:
            return None
        try:
            return str(Path(output).relative_to("Data"))
        except ValueError:
            return None

    def _research_event(self, event, job):
        kind = event.get("event")
        pending = self.finger_pending
        if kind == "MARK_ACCEPTED":
            for action in reversed(self.finger_actions):
                if action["label"] == event.get("label", "").removeprefix("transition_to_"):
                    action["t_s"] = event["t_s"]
                    break
        if kind == "TRACE" and event.get("points"):
            self.finger_last_trace_wall = time.monotonic()
        if pending and kind == "MARK_ACCEPTED" and event.get("label", "").removeprefix("transition_to_") == pending["label"]:
            pending["start_s"] = float(event["t_s"]) + pending["settle_s"]
            pending["end_s"] = pending["start_s"] + pending["hold_s"]
            pending["state"] = "settling" if pending["settle_s"] else "capturing"
        if pending and kind == "TRACE" and pending.get("start_s") is not None:
            latest = max((p.get("x", 0) for p in event.get("points", [])), default=0)
            if latest >= pending["start_s"]:
                pending["state"] = "capturing"
            # 等到下一个完整数据批次，留出子进程 JSONL flush 的余量。
            if latest >= pending["end_s"] + 0.5:
                item = {key: deepcopy(value) for key, value in pending.items() if key not in ("state", "hold_s", "settle_s")}
                item["state"] = "complete"
                item["source"] = "ruler_manual_confirmation_hardware_time_window"
                self.finger_annotations.append(item)
                if item["role"] in ("calibration", "validation"):
                    self.finger_analysis = None
                self.finger_pending = None
        if pending and kind in ("RF_STOPPED", "COMPLETE", "BASELINE_REJECTED") and self.finger_pending:
            self.finger_error = "采集结束或基线无效，未完成的尺子测量窗口未用于标定。"
            self.finger_pending = None

    def finger_snapshot(self):
        from sspp_experiments.finger_displacement import estimate
        with self.lock:
            key = self._finger_key()
            result = {"run_key": key, "annotations": deepcopy(self.finger_annotations),
                      "settings": deepcopy(self.finger_settings),
                      "actions": deepcopy(self.finger_actions),
                      "pending": deepcopy(self.finger_pending), "analysis": deepcopy(self.finger_analysis),
                      "error": self.finger_error,
                      "estimate": {"ok": False, "distance_mm": None, "displacement_mm": None,
                                   "error": "完成标定与独立验证后显示毫米估算。"}}
            analysis = self.finger_analysis
            if not analysis or not analysis.get("model"):
                return result
            if (self.job or {}).get("status") != "running":
                result["estimate"]["error"] = "当前未在采集；历史估算见研究报告。"
                return result
            if self.finger_last_trace_wall is None or time.monotonic() - self.finger_last_trace_wall > 1.5:
                result["estimate"]["error"] = "实时数据超过 1.5 秒未更新，估算暂停。"
                return result
            point = self.live_points[-1] if self.live_points else {}
            if point.get("gap") or point.get("label", "").startswith("transition_to_") or (self.job or {}).get("errors"):
                result["estimate"]["error"] = "采集或显示数据异常，估算暂停。"
                return result
            result["estimate"] = estimate(analysis["model"], point.get("phase"), run_key=key,
                                          coherent_fraction=point.get("coherent_fraction", 0),
                                          relative_amplitude_dB=point.get("amplitude"))
            result["estimate"]["t_s"] = point.get("x")
            return result

    def finger_begin_window(self, distance_mm, role="calibration", action="hold", settle_s=2., hold_s=2., research_settings=None):
        distance = finite(distance_mm, "尺子距离 mm")
        settle = finite(settle_s, "稳定等待 s", 0, 30)
        hold = finite(hold_s, "测量窗口 s", .5, 30)
        if role not in ("calibration", "validation", "observation"):
            raise LabError("请选择标定、独立验证或观察")
        if not isinstance(action, str) or not action or len(action) > 80:
            raise LabError("动作名称应为 1–80 字")
        with self.lock:
            job = self.job or {}
            if job.get("status") != "running" or job.get("config", {}).get("mode") != "phase" or not job.get("baseline"):
                raise LabError("请先开始固定频率实验，等待静止参考通过")
            if self.finger_analysis_lock.locked():
                raise LabError("研究分析正在运行，请等待完成后采集新窗口")
            if self.finger_pending:
                raise LabError("上一个尺子测量窗口尚未结束，请保持位置或取消窗口")
            live_t = (job.get("live") or {}).get("t_s", 0)
            if live_t + settle + hold + 1 > job["config"]["duration"]:
                raise LabError("本轮剩余时长不足以完成该窗口，请结束后开始新一轮实验")
            if research_settings is not None:
                self.finger_settings = self._validate_finger_settings(research_settings)
            ident = uuid.uuid4().hex[:12]
            pending = {"id": ident, "role": role, "distance_mm": distance, "action": action,
                       "label": "ruler_" + ident, "settle_s": settle, "hold_s": hold,
                       "start_s": None, "end_s": None, "state": "awaiting_ack",
                       "confirmed_utc": datetime.now(timezone.utc).isoformat(), "research_settings": deepcopy(self.finger_settings)}
            self.finger_pending = pending
            self.finger_error = ""
            try:
                self.mark(pending["label"], settle=settle, contact=False)
            except Exception:
                self.finger_pending = None
                raise
            return self.finger_snapshot()

    def finger_mark_action(self, distance_mm=None, action="near", label="", contact=False, settle_s=2., research_settings=None):
        if distance_mm is not None:
            distance_mm = finite(distance_mm, "尺子距离 mm")
        settle_s = finite(settle_s, "过渡等待 s", 0, 30)
        if not isinstance(action, str) or not action or len(action) > 80 or not isinstance(label, str) or len(label) > 80:
            raise LabError("动作与备注应为不超过 80 字的文字")
        if not isinstance(contact, bool):
            raise LabError("接触标记无效")
        with self.lock:
            if self.finger_pending:
                raise LabError("请先完成或取消当前稳定测量窗口，再记录移动动作")
            if research_settings is not None:
                self.finger_settings = self._validate_finger_settings(research_settings)
            marker = "action_" + uuid.uuid4().hex[:12]
            item = {"id": marker, "label": marker, "action": action, "note": label,
                    "distance_mm": distance_mm, "contact_reported": contact,
                    "t_s": None, "settle_s": settle_s,
                    "host_utc": datetime.now(timezone.utc).isoformat(),
                    "source": "operator_action_not_calibration_truth", "research_settings": deepcopy(self.finger_settings)}
            self.finger_actions.append(item)
            try:
                self.mark(marker, settle_s, contact)
            except Exception:
                self.finger_actions.remove(item)
                raise
            return self.finger_snapshot()

    @staticmethod
    def _validate_finger_settings(settings):
        if not isinstance(settings, dict) or settings.get("workflow") not in ("guided", "free"):
            raise LabError("手指研究设置无效")
        return {"initial_distance_mm": finite(settings.get("initial_distance_mm"), "初始尺量距离 mm"),
                "tolerance_mm": finite(settings.get("tolerance_mm"), "验证允许误差 mm", .01, 1000),
                "workflow": settings["workflow"]}

    def finger_cancel_window(self):
        with self.lock:
            self.finger_pending = None
            self.finger_error = "已取消测量窗口；原始采集继续，取消的窗口不参与标定。"
            return self.finger_snapshot()

    def _finger_target(self, key):
        with self.lock:
            key = key or self._finger_key()
        if not key or not key.startswith("phase_runs/"):
            raise LabError("请选择固定频率幅相记录")
        path = self.run_path(key)
        for name in ("amplitude_phase.jsonl", "baseline.json", "manifest.json"):
            if (path / name).is_symlink():
                raise LabError("研究源文件不能是符号链接")
        if not (path / "amplitude_phase.jsonl").is_file():
            raise LabError("该记录没有连续幅相数据")
        return key, path

    def finger_load(self, key):
        key, path = self._finger_target(key)
        rows, content = self._finger_rows(path)
        notes = read_json(path / "finger_annotations.json", {}) or {}
        analysis = None
        directory = path / "finger_research"
        if directory.is_dir() and not directory.is_symlink():
            for file in sorted(directory.glob("*/analysis.json"), reverse=True):
                if file.is_symlink() or file.parent.is_symlink():
                    continue
                saved = read_json(file)
                if isinstance(saved, dict) and saved.get("run_key") == key:
                    analysis = self._finger_compact(saved)
                    provenance = saved.get("provenance") or {}
                    prefix_bytes = provenance.get("series_prefix_bytes")
                    baseline_file = path / "baseline.json"
                    matched = (isinstance(prefix_bytes, int) and 0 <= prefix_bytes <= len(content)
                               and hashlib.sha256(content[:prefix_bytes]).hexdigest() == provenance.get("series_prefix_sha256")
                               and baseline_file.is_file()
                               and hashlib.sha256(baseline_file.read_bytes()).hexdigest() == provenance.get("baseline_sha256"))
                    if not matched:
                        analysis["status"] = "rejected"
                        analysis.setdefault("errors", []).append("保存报告与当前幅相前缀或基线哈希不一致，不能使用旧模型。")
                        if analysis.get("model"):
                            analysis["model"]["active"] = False
                        analysis["observations"] = []
                        analysis["report"] = {"status": "rejected", "errors": analysis["errors"]}
                    notes["annotations"] = saved.get("annotations", [])
                    break
        return {"run_key": key, "settings": notes.get("settings", {}), "annotations": notes.get("annotations", []), "actions": notes.get("actions", []), "analysis": analysis,
                "duration_s": rows[-1]["t_s"] if rows else 0,
                "message": "历史数据仅用于离线分析；尺子真值必须来自当时实际记录，不能由曲线反推。"}

    @staticmethod
    def _finger_rows(path):
        # 读取当前文件的完整 JSONL 前缀；活动采集末尾半行不纳入研究分析。
        content = (path / "amplitude_phase.jsonl").read_bytes()
        if content and not content.endswith(b"\n"):
            content = content[:content.rfind(b"\n") + 1]
        try:
            rows = [json.loads(line) for line in content.splitlines() if line.strip()]
        except ValueError as exc:
            raise LabError("幅相文件中存在损坏记录，不能用于毫米标定") from exc
        return rows, content

    def finger_analyze(self, initial_distance_mm, tolerance_mm=2., key=None, annotations=None):
        from sspp_experiments.finger_displacement import analyze, estimate
        finite(initial_distance_mm, "初始尺子距离 mm")
        finite(tolerance_mm, "最大允许误差 mm", .01, 1000)
        if not self.finger_analysis_lock.acquire(blocking=False):
            raise LabError("研究分析正在运行，请等待完成")
        try:
            with self.lock:
                if self.exporting:
                    raise LabError("实验包正在导出，请等待后再修改研究报告")
            key, path = self._finger_target(key)
            with self.lock:
                current = key == self._finger_key()
                if current and self.finger_pending:
                    raise LabError("请先完成或取消当前稳定窗口，再分析标定与验证")
                actions = deepcopy(self.finger_actions) if current else None
                annotations = deepcopy(self.finger_annotations if annotations is None and current else annotations)
            if annotations is None:
                annotations = self.finger_load(key)["annotations"]
            if not isinstance(annotations, list) or len(annotations) > 500:
                raise LabError("窗口列表无效或超过 500 个")
            rows, content = self._finger_rows(path)
            result = analyze(rows, annotations, initial_distance_mm, tolerance_mm, run_key=key)
            manifest = read_json(path / "manifest.json", {}) or {}
            baseline = read_json(path / "baseline.json", {}) or {}
            from sspp_lab.dsp import baseline_problems
            with self.lock:
                live_valid = (key == self._finger_key() and (self.job or {}).get("status") == "running"
                              and bool((self.job or {}).get("baseline")) and not (self.job or {}).get("errors"))
            telemetry = manifest.get("telemetry", {})
            saved_valid = (manifest.get("status") == "complete" and not manifest.get("errors")
                           and all(telemetry.get(k) for k in ("tx_end_of_burst_sent", "rx_stop_sent", "workers_stopped")))
            baseline_fields = ("min_coherent_fraction", "amplitude_sd_dB", "phase_linear_drift_deg_per_s", "phase_residual_sd_deg")
            baseline_valid = (all(isinstance(baseline.get(k), (int, float)) and math.isfinite(baseline[k]) for k in baseline_fields)
                              and not baseline_problems(baseline))
            if not baseline_valid or not (live_valid or saved_valid):
                result["status"] = "rejected"
                result["results"] = []
                result["observations"] = []
                result.setdefault("errors", []).append("源实验的基线、采集质量或完整停止证据未通过，不能激活毫米估算。")
                if result.get("model"):
                    result["model"]["active"] = False
            result["run_key"] = key
            result["annotations"] = annotations
            result["actions"] = actions if current else (read_json(path / "finger_annotations.json", {}) or {}).get("actions", [])
            result["initial_distance_mm"] = initial_distance_mm
            result["tolerance_mm"] = tolerance_mm
            result["created_utc"] = datetime.now(timezone.utc).isoformat()
            result["provenance"] = {
                "series_file": "amplitude_phase.jsonl", "series_prefix_bytes": len(content),
                "series_prefix_sha256": hashlib.sha256(content).hexdigest(),
                "baseline_sha256": hashlib.sha256((path / "baseline.json").read_bytes()).hexdigest() if baseline else None,
                "source_run_status_at_analysis": manifest.get("status"),
                "scope": "same_continuous_run_only_no_cross_run_phase_reuse",
                "truth_source": "operator_ruler_entry_not_motion_sensor",
                "source_hashes": {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in (self.root / "sspp_experiments/finger_displacement").glob("*.py")},
            }
            research_dir = path / "finger_research"
            if research_dir.is_symlink():
                raise LabError("研究输出目录不能是符号链接")
            export = research_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_" + uuid.uuid4().hex[:6])
            export.mkdir(parents=True, exist_ok=False)
            result["export_path"] = str(export)
            result["report"] = {"status": result.get("status"), "errors": result.get("errors", []),
                                "warnings": result.get("warnings", []), "calibration": result.get("calibration"),
                                "validation": result.get("validation"), "initial_distance_mm": initial_distance_mm,
                                "tolerance_mm": tolerance_mm, "scope": "仅限同一次连续采集；位移正值表示远离传感器。"}
            write_json(export / "analysis.json", result)
            write_json(export / "annotations.json", {"schema_version": 1, "run_key": key, "annotations": annotations})
            self._finger_csv(export / "windows.csv", result.get("windows", []))
            validation = result.get("validation") or {}
            self._finger_csv(export / "validation.csv", validation.get("windows", []))
            estimates = result.get("results", []) if (result.get("model") or {}).get("active") else []
            self._finger_csv(export / "estimated_series.csv", estimates,
                             fields=["t_s", "ok", "distance_mm", "displacement_mm", "error"])
            from .research_export import export_report
            export_report(export, result, estimates)
            with self.lock:
                if key == self._finger_key():
                    self.finger_analysis = self._finger_compact(result)
                    self.finger_annotations = deepcopy(annotations)
            return self._finger_compact(result)
        finally:
            self.finger_analysis_lock.release()

    @staticmethod
    def _finger_compact(result):
        compact = {key: value for key, value in result.items() if key != "results"}
        return compact

    @staticmethod
    def _finger_csv(path, rows, fields=None):
        rows = [row for row in rows if isinstance(row, dict)]
        fields = fields or sorted({key for row in rows for key in row}) or ["status"]
        with path.open("w", encoding="utf-8-sig", newline="") as out:
            writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                                 for key, value in row.items()})

    def _save_finger_context(self, job, output):
        if job.get("config", {}).get("mode") != "phase":
            return
        path = output / "finger_annotations.json"
        if path.is_symlink():
            raise LabError("研究记录不能是符号链接")
        write_json(path, {"schema_version": 1, "experiment": "finger_displacement",
                          "run_key": self._finger_key(), "annotations": self.finger_annotations, "actions": self.finger_actions,
                          "pending_not_used": self.finger_pending, "settings": self.finger_settings,
                          "ruler_entries_are_operator_reported": True})
