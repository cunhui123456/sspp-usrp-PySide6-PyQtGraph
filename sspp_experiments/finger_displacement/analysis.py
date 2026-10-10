"""以独立静止区间验证的单调分段线性相位—尺量距离标定。

这是单次实验内的经验映射，不是电磁传播距离公式。门槛是明确的工程
质量策略；验证误差针对区间相位中位数，不声称每一瞬时读数均达该精度。
函数只生成派生字典，不修改输入行或标注，不读写原始数据。
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, stdev

ALGORITHM_VERSION = "finger-monotonic-same-run-v1"


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _sha(value):
    # NaN 可进入输入指纹，但不会进入数值结果；对应区间会被质量门槛拒绝。
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sd(values):
    return stdev(values) if len(values) > 1 else 0.0


def _model_hash(model):
    return _sha({k: v for k, v in model.items() if k != "binding_sha256"})


def _failure(message):
    return {"ok": False, "distance_mm": None, "displacement_mm": None, "error": message}


def _interpolate(nodes, phase):
    ordered = sorted(nodes, key=lambda n: n["phase_deg"])
    if phase < ordered[0]["phase_deg"] or phase > ordered[-1]["phase_deg"]:
        return None
    for left, right in zip(ordered, ordered[1:]):
        if left["phase_deg"] <= phase <= right["phase_deg"]:
            weight = (phase-left["phase_deg"])/(right["phase_deg"]-left["phase_deg"])
            return left["distance_mm"] + weight*(right["distance_mm"]-left["distance_mm"])
    return None


def _estimate_validated_model(model, phase_deg, coherent_fraction, relative_amplitude_dB):
    if not _finite(phase_deg):
        return _failure("缺少有限的相对解缠相位；基线尚未建立的行不能估距")
    if not _finite(coherent_fraction) or not model["policy"]["min_coherent_fraction"] <= coherent_fraction <= 1:
        return _failure("相干占比缺失或低于模型质量门槛")
    if not _finite(relative_amplitude_dB):
        return _failure("缺少有限相对幅度，无法执行幅度变化检查")
    low, high = model["amplitude_guard_dB"]
    if not low <= relative_amplitude_dB <= high:
        return _failure("相对幅度超出标定及验证范围的容许余量，可能出现未建模条件变化")
    distance = _interpolate(model["nodes"], phase_deg)
    if distance is None:
        return _failure("相位超出标定范围，禁止外推")
    return {"ok": True, "distance_mm": float(distance),
            "displacement_mm": float(distance-model["initial_distance_mm"]), "error": None}


def estimate(model, phase_deg, *, run_key, coherent_fraction=None, relative_amplitude_dB=None):
    """估计传感器到手指的尺量距离；位移 = 距离 − 初始尺量距离（远离为正）。

    模型只允许在同 run_key/同次相位基线使用。相干占比与相对幅度必须显式
    提供，禁止缺失质量数据时自动假定良好。返回字典，无效点保留 None 缺口。
    """
    if not isinstance(model, dict) or not model.get("active"):
        return _failure("模型未通过独立验证，不能输出距离")
    if not isinstance(run_key, str) or not run_key or model.get("run_key") != run_key:
        return _failure("模型仅绑定同一次采集与相位基线，禁止跨 run 使用")
    try:
        if model.get("algorithm_version") != ALGORITHM_VERSION:
            return _failure("不支持的标定算法版本")
        if model.get("binding_sha256") != _model_hash(model):
            return _failure("模型内容与来源绑定校验不一致")
        return _estimate_validated_model(model, phase_deg, coherent_fraction, relative_amplitude_dB)
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return _failure("模型结构无效")


def _window(rows, indexed, annotation, cadence, policy):
    start, end = annotation["start_s"], annotation["end_s"]
    selected = [(i, row) for i, row in indexed if start <= row["t_s"] < end]
    result = dict(annotation)
    errors = []
    result.update({"valid": False, "errors": errors, "sample_count": len(selected),
                   "row_indices": [i for i, _ in selected], "sample_spans": []})
    if end-start < policy["min_window_duration_s"]:
        errors.append("静止区间短于最小持续时间")
    if len(selected) < policy["min_samples"]:
        errors.append("区间内有效记录数量不足")
    if not selected:
        return result
    times = [row["t_s"] for _, row in selected]
    result["first_t_s"], result["last_t_s"] = times[0], times[-1]
    # 相位行的时间是采集窗口中心；允许边缘半个正常帧周期的量化误差。
    edge_slack = cadence * 1.05 + 1e-9
    if times[0]-start > edge_slack or end-times[-1] > edge_slack:
        errors.append("记录未覆盖所选区间首尾，不能用少数点代表整个区间")
    if any(b-a > cadence*1.5+1e-9 for a, b in zip(times, times[1:])):
        errors.append("区间存在时间记录缺口")
    has_samples = [("start_sample" in row or "count" in row) for _, row in selected]
    if any(has_samples):
        previous_end = None
        for index, row in selected:
            first, count = row.get("start_sample"), row.get("count")
            if (not isinstance(first, int) or isinstance(first, bool) or first < 0 or
                    not isinstance(count, int) or isinstance(count, bool) or count <= 0):
                errors.append("样本起点或计数缺失/无效")
                break
            result["sample_spans"].append({"row_index": index, "start_sample": first, "count": count})
            if previous_end is not None and first != previous_end:
                errors.append("区间存在实际样本索引缺口或重复")
                break
            previous_end = first+count
    for _, row in selected:
        if str(row.get("label", "")).lower().startswith("transition"):
            errors.append("区间包含 transition 过渡记录，请只选择静止段")
            break
    keys = ("relative_phase_deg", "relative_amplitude_dB", "coherent_fraction")
    if any(not _finite(row.get(key)) for _, row in selected for key in keys):
        errors.append("区间存在缺失/非有限幅相或相干数据，可能尚处于基线建立阶段")
    if errors:
        return result
    phases = [float(row["relative_phase_deg"]) for _, row in selected]
    amplitudes = [float(row["relative_amplitude_dB"]) for _, row in selected]
    coherent = [float(row["coherent_fraction"]) for _, row in selected]
    time_mean = mean(times)
    centered_t = [t-time_mean for t in times]
    phase_mean = mean(phases)
    denominator = sum(t*t for t in centered_t)
    slope = sum(t*(p-phase_mean) for t, p in zip(centered_t, phases))/denominator if denominator else 0.
    residual = [p-phase_mean-slope*t for p, t in zip(phases, centered_t)]
    result.update({"phase_deg": float(median(phases)), "phase_std_deg": float(_sd(phases)),
                   "phase_residual_std_deg": float(_sd(residual)),
                   "phase_drift_deg_per_s": float(slope),
                   "relative_amplitude_dB": float(median(amplitudes)),
                   "amplitude_std_dB": float(_sd(amplitudes)),
                   "min_coherent_fraction": min(coherent),
                   "labels": sorted({str(row.get("label", "")) for _, row in selected})})
    if min(coherent) < policy["min_coherent_fraction"] or max(coherent) > 1:
        errors.append("静止区间相干占比不满足门槛")
    if result["phase_std_deg"] > policy["max_phase_std_deg"]:
        errors.append("静止区间相位波动过大")
    if abs(slope) > policy["max_phase_drift_deg_per_s"]:
        errors.append("静止区间相位持续漂移，不能视为稳定位置")
    if result["amplitude_std_dB"] > policy["max_amplitude_std_dB"]:
        errors.append("静止区间幅度波动过大")
    result["valid"] = not errors
    return result


def _independence(rows, calibration, validation, policy):
    """记录时段独立性，不能据此声称独立会话/样本的科学泛化。"""
    evidence, errors = [], []
    for cal in calibration:
        for val in validation:
            left, right = sorted((cal, val), key=lambda w: w["start_s"])
            gap = right["start_s"]-left["end_s"]
            entry = {"calibration_id": cal["id"], "validation_id": val["id"],
                     "gap_s": float(gap), "basis": None}
            if gap < policy["min_validation_gap_s"]-1e-9:
                errors.append(f"{cal['id']} / {val['id']}: 标定与验证时段至少间隔 {policy['min_validation_gap_s']:g} 秒")
                evidence.append(entry)
                continue
            between = rows[left["row_indices"][-1]+1:right["row_indices"][0]]
            left_labels, right_labels = set(left["labels"]), set(right["labels"])
            recorded_stage_change = (left_labels != right_labels or
                any(str(r.get("label", "")) not in left_labels for r in between))
            if recorded_stage_change:
                entry["basis"] = "distinct_recorded_stage"
            else:
                phases = [r["relative_phase_deg"] for r in between if _finite(r.get("relative_phase_deg"))]
                phases += [left["phase_deg"], right["phase_deg"]]
                threshold = max(policy["min_phase_step_deg"], policy["significance_sigma"]*
                                math.hypot(left["phase_std_deg"], right["phase_std_deg"]))
                if left["action"].strip() and right["action"].strip() and max(phases)-min(phases) > threshold:
                    entry["basis"] = "operator_annotated_motion"
                else:
                    errors.append(f"{cal['id']} / {val['id']}: 不能把同一连续静止阶段拆成标定和独立验证；需新的动作/阶段证据")
            evidence.append(entry)
    return evidence, errors


def analyze(rows, annotations, initial_distance_mm, tolerance_mm=2.0, run_key=None, *,
            min_coherent_fraction=0.8, min_samples=5, min_window_duration_s=0.5,
            max_phase_std_deg=3.0, max_phase_drift_deg_per_s=1.0,
            max_amplitude_std_dB=0.5, significance_sigma=3.0,
            min_phase_step_deg=0.5, amplitude_margin_dB=3.0, min_validation_gap_s=1.0):
    """分析同 run 的相位行与静止区间标注，返回可 JSON 保存的派生结果。

    annotations: {id?, start_s, end_s, role, distance_mm?, action?}；时间区间
    均为 [start_s, end_s)，calibration/validation 必须有人工尺量 distance_mm。
    observation 的 distance_mm 可省略。所有标注禁止互相重叠，防止数据泄漏。
    至少 3 个不同标定距离、2 个不同验证距离；验证只评价独立区间中位数误差。
    """
    report = {"algorithm_version": ALGORITHM_VERSION, "status": "invalid", "errors": [],
              "warnings": [], "windows": [], "calibration": {}, "validation": {},
              "model": None, "results": [], "observations": []}
    errors = report["errors"]
    try:
        rows, annotations = list(rows), list(annotations)
    except TypeError:
        errors.append("记录和区间标注必须是可迭代列表")
        return report
    policy = {"min_coherent_fraction": min_coherent_fraction, "min_samples": min_samples,
              "min_window_duration_s": min_window_duration_s, "max_phase_std_deg": max_phase_std_deg,
              "max_phase_drift_deg_per_s": max_phase_drift_deg_per_s,
              "max_amplitude_std_dB": max_amplitude_std_dB, "significance_sigma": significance_sigma,
              "min_phase_step_deg": min_phase_step_deg, "amplitude_margin_dB": amplitude_margin_dB,
              "min_validation_gap_s": min_validation_gap_s,
              "gap_factor": 1.5, "interpolation": "piecewise_linear_no_extrapolation",
              "validation_metric": "window_median_absolute_error_mm"}
    if not isinstance(run_key, str) or not run_key.strip():
        errors.append("必须提供绑定此次连续采集的 run_key")
    if not _finite(initial_distance_mm) or initial_distance_mm < 0:
        errors.append("初始尺量距离必须是非负有限毫米数")
    if not _finite(tolerance_mm) or tolerance_mm <= 0:
        errors.append("最大允许误差必须是正有限毫米数")
    if (not isinstance(min_samples, int) or isinstance(min_samples, bool) or min_samples < 3 or
            not _finite(min_coherent_fraction) or not 0 < min_coherent_fraction <= 1 or
            any(not _finite(v) or v <= 0 for v in (min_window_duration_s, max_phase_std_deg,
                max_phase_drift_deg_per_s, max_amplitude_std_dB, significance_sigma, min_phase_step_deg,
                min_validation_gap_s)) or
            not _finite(amplitude_margin_dB) or amplitude_margin_dB < 0):
        errors.append("质量策略参数无效")
    if len(rows) < 2 or any(not isinstance(r, dict) or not _finite(r.get("t_s")) for r in rows):
        errors.append("需要按时间排列且时间有效的相位记录")
    else:
        if any(b["t_s"] <= a["t_s"] for a, b in zip(rows, rows[1:])):
            errors.append("相位记录时间必须严格递增，重复/倒序记录不能参与分析")
        else:
            intervals = [b["t_s"]-a["t_s"] for a, b in zip(rows, rows[1:])]
            nominal_interval = median(intervals)
            if any(delta > nominal_interval*1.5+1e-9 for delta in intervals):
                errors.append("源相位记录存在时间缺口，不能跨缺口沿用同次相位标定")
        if any(r.get("run_key", run_key) != run_key for r in rows):
            errors.append("输入记录包含其他 run，不能共享相位基线")
        previous_end = None
        for row in rows:
            start, count = row.get("start_sample"), row.get("count")
            if isinstance(start, int) and isinstance(count, int) and count > 0:
                if previous_end is not None and start < previous_end:
                    errors.append("输入记录存在重复/重叠的源样本，不能作为独立数据使用")
                    break
                if previous_end is not None and start > previous_end:
                    errors.append("输入记录存在源样本缺口，不能跨缺口沿用相位标定")
                    break
                previous_end = start+count
    normalized = []
    ids = set()
    for index, annotation in enumerate(annotations):
        if not isinstance(annotation, dict):
            errors.append(f"标注 {index+1} 结构无效")
            continue
        a = {"id": str(annotation.get("id", f"window_{index+1}")),
             "start_s": annotation.get("start_s"), "end_s": annotation.get("end_s"),
             "role": annotation.get("role"), "distance_mm": annotation.get("distance_mm"),
             "action": str(annotation.get("action", ""))}
        if a["id"] in ids:
            errors.append("区间 id 重复")
        ids.add(a["id"])
        if (a["role"] not in ("calibration", "validation", "observation") or
                not _finite(a["start_s"]) or not _finite(a["end_s"]) or
                a["start_s"] < 0 or a["end_s"] <= a["start_s"]):
            errors.append(f"区间 {a['id']} 的角色或时间范围无效")
            continue
        if a["role"] != "observation" or a["distance_mm"] is not None:
            if not _finite(a["distance_mm"]) or a["distance_mm"] < 0:
                errors.append(f"区间 {a['id']} 缺少非负有限的尺量距离")
        normalized.append(a)
    ordered = sorted(normalized, key=lambda a: a["start_s"])
    if any(b["start_s"] < a["end_s"] for a, b in zip(ordered, ordered[1:])):
        errors.append("标注区间重叠：标定、验证和观察必须使用互不重叠的样本")
    if errors:
        return report
    cadence = float(median([b["t_s"]-a["t_s"] for a, b in zip(rows, rows[1:])]))
    indexed = list(enumerate(rows))
    windows = [_window(rows, indexed, a, cadence, policy) for a in normalized]
    report["windows"] = windows
    # 无原始样点身份时仍保留来源行号，明确仅检查 JSONL 时间连续性。
    if any(not w["sample_spans"] for w in windows):
        report["warnings"].append("部分区间没有 start_sample/count，仅能检查记录时间连续性")
    for w in windows:
        if not w["valid"]:
            target = report["warnings"] if w["role"] == "observation" else errors
            target.extend(f"{w['id']}: {e}" for e in w["errors"])
    if errors:
        return report
    calibration = [w for w in windows if w["role"] == "calibration"]
    validation = [w for w in windows if w["role"] == "validation"]
    groups = defaultdict(list)
    for w in calibration:
        groups[w["distance_mm"]].append(w)
    if len(groups) < 3:
        errors.append("至少需要 3 个不同尺量距离的标定静止区间")
        return report
    nodes = []
    for distance, group in sorted(groups.items()):
        phases = [w["phase_deg"] for w in group]
        for i, left in enumerate(group):
            for right in group[i+1:]:
                limit = max(min_phase_step_deg, significance_sigma*math.hypot(left["phase_std_deg"], right["phase_std_deg"]))
                if abs(left["phase_deg"]-right["phase_deg"]) > limit:
                    errors.append(f"距离 {distance:g} mm 的重复标定相位不一致，可能存在迟滞/漂移")
        nodes.append({"distance_mm": float(distance), "phase_deg": float(median(phases)),
                      "phase_noise_deg": float(max([_sd(phases)] + [w["phase_std_deg"] for w in group])),
                      "window_ids": [w["id"] for w in group]})
    delta = nodes[-1]["phase_deg"]-nodes[0]["phase_deg"]
    direction = 1 if delta > 0 else -1
    for left, right in zip(nodes, nodes[1:]):
        threshold = max(min_phase_step_deg, significance_sigma*math.hypot(left["phase_noise_deg"], right["phase_noise_deg"]))
        if direction*(right["phase_deg"]-left["phase_deg"]) <= threshold:
            errors.append("标定相位未严格单调，或相邻距离的相位差不足以超过噪声门槛")
    report["calibration"] = {"passed": not errors, "nodes": nodes,
                              "direction": "phase_increases_with_distance" if direction > 0 else "phase_decreases_with_distance",
                              "distance_range_mm": [nodes[0]["distance_mm"], nodes[-1]["distance_mm"]]}
    if errors:
        return report
    val_errors = []
    if len({w["distance_mm"] for w in validation}) < 2:
        val_errors.append("至少需要 2 个不同尺量距离的独立验证区间")
    independence, independence_errors = _independence(rows, calibration, validation, policy)
    val_errors.extend(independence_errors)
    checked = []
    for w in validation:
        distance = w["distance_mm"]
        reason = None
        if not nodes[0]["distance_mm"] <= distance <= nodes[-1]["distance_mm"]:
            reason = "验证尺量距离超出标定范围"
        predicted = _interpolate(nodes, w["phase_deg"])
        if predicted is None:
            reason = reason or "验证相位超出标定范围，禁止外推"
        error = None if predicted is None else float(predicted-distance)
        if error is not None and abs(error) > tolerance_mm:
            reason = reason or "独立验证误差超过允许值"
        checked.append({"window_id": w["id"], "distance_mm": distance,
                        "estimated_distance_mm": predicted, "error_mm": error,
                        "absolute_error_mm": None if error is None else abs(error),
                        "passed": reason is None, "error": reason})
        if reason:
            val_errors.append(f"{w['id']}: {reason}")
    absolute = [c["absolute_error_mm"] for c in checked if c["absolute_error_mm"] is not None]
    val_report = {"passed": not val_errors, "tolerance_mm": float(tolerance_mm),
                  "metric": policy["validation_metric"], "windows": checked,
                  "max_error_mm": max(absolute) if absolute else None,
                  "rmse_mm": math.sqrt(mean([x*x for x in absolute])) if absolute else None,
                  "independence": independence, "scope": "within_run_held_out_intervals_only",
                  "errors": val_errors}
    report["validation"] = val_report
    source = {"rows_sha256": _sha(rows), "annotations_sha256": _sha(normalized),
              "row_count": len(rows), "calibration_window_ids": [w["id"] for w in calibration],
              "validation_window_ids": [w["id"] for w in validation],
              "analysis_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "phase_field": "relative_phase_deg", "window_selection": "start_s <= t_s < end_s"}
    measured_amplitudes = [w["relative_amplitude_dB"] for w in calibration+validation]
    model = {"algorithm_version": ALGORITHM_VERSION, "active": val_report["passed"],
             "run_key": run_key, "same_run_only": True, "initial_distance_mm": float(initial_distance_mm),
             "distance_definition": "ruler_sensor_to_finger_mm", "displacement_positive": "away_from_sensor",
             "nodes": nodes, "distance_range_mm": report["calibration"]["distance_range_mm"],
             "phase_range_deg": sorted([nodes[0]["phase_deg"], nodes[-1]["phase_deg"]]),
             "amplitude_guard_dB": [min(measured_amplitudes)-amplitude_margin_dB,
                                    max(measured_amplitudes)+amplitude_margin_dB],
             "policy": policy, "validation": val_report, "source": source}
    model["binding_sha256"] = _model_hash(model)
    report["model"] = model
    if val_errors:
        errors.extend(val_errors)
        report["status"] = "validation_failed"
        return report
    report["status"] = "ready"
    report["warnings"].append("验证仅针对本次采集的独立静止区间中位数；同次时间分离不自动证明物理取放独立，也不保证逐帧精度")
    for index, row in indexed:
        value = _estimate_validated_model(model, row.get("relative_phase_deg"), row.get("coherent_fraction"), row.get("relative_amplitude_dB"))
        if str(row.get("label", "")).lower().startswith("transition"):
            value = _failure("transition 过渡记录不输出距离")
        report["results"].append({"row_index": index, "t_s": float(row["t_s"]),
                                  "label": str(row.get("label", "")), **value})
    for w in windows:
        if w["role"] == "observation":
            value = (_estimate_validated_model(model, w["phase_deg"], w["min_coherent_fraction"], w["relative_amplitude_dB"])
                     if w["valid"] else _failure("；".join(w["errors"])))
            report["observations"].append({"window_id": w["id"], "start_s": w["start_s"], "end_s": w["end_s"],
                                          **value})
    return report
