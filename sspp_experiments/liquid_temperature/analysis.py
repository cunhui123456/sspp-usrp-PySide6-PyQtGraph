"""同次连续采集内温度平台的相位—位移描述性分析。

参考温度、距离和平台由操作者记录；首尾温度检查不能证明全程热平衡。
拟合只报告各平台内的响应斜率，不产生毫米模型，也不把时间漂移归因为温度。
"""
from __future__ import annotations

from collections import defaultdict
import math
from statistics import mean, median

from sspp_experiments.finger_displacement.analysis import _finite, _window
from sspp_lab.dsp import baseline_problems

ALGORITHM_VERSION = "liquid-temperature-phase-v1"
POLICY = {
    "min_samples": 5, "min_window_duration_s": .5,
    "min_coherent_fraction": .8, "max_phase_std_deg": 3.,
    "max_phase_drift_deg_per_s": 1., "max_amplitude_std_dB": .5,
}


def _fit_block(name, windows, tolerance):
    """使用窗口中位数做带截距 OLS；返回拟合误差而非测量精度。"""
    result = {"block": name, "status": "rejected", "errors": [], "points": [],
              "temperature_mean_c": None, "temperature_range_c": None,
              "reference_id": None, "reference_distance_mm": None, "reference_phase_deg": None,
              "sensitivity_deg_per_mm": None, "intercept_deg": None,
              "fit_rmse_deg": None, "r_squared": None, "return_drift_deg": []}
    errors = result["errors"]
    valid = [w for w in windows if w["valid"]]
    references = sorted([w for w in valid if w["role"] == "reference"], key=lambda w: w["start_s"])
    if not valid:
        errors.append("该温度平台没有通过质量检查的窗口")
        return result
    temperatures = [w[key] for w in valid for key in ("temperature_c", "temperature_end_c")]
    result.update(temperature_mean_c=float(mean(temperatures)),
                  temperature_range_c=[float(min(temperatures)), float(max(temperatures))])
    if max(temperatures)-min(temperatures) > tolerance+1e-9:
        errors.append("同一温度平台的参考温度范围超过容差；不能把不同温度混合拟合")
    if not references:
        errors.append("该平台缺少有效 reference 初始参考窗口")
        return result
    reference = references[0]
    result.update(reference_id=reference["id"], reference_distance_mm=reference["distance_mm"],
                  reference_phase_deg=reference["phase_deg"], reference_start_s=reference["start_s"],
                  reference_temperature_c=reference["temperature_mean_c"])
    if any(w["start_s"] < reference["start_s"] for w in valid):
        errors.append("reference 必须是该平台最早的有效窗口")
    if any(not math.isclose(w["distance_mm"], reference["distance_mm"], abs_tol=1e-9, rel_tol=0.)
           for w in references):
        errors.append("同一平台的 reference 必须使用同一几何距离，其他位置请标为 displacement")
    for w in valid:
        result["points"].append({"window_id": w["id"], "role": w["role"],
                                "displacement_mm": float(w["distance_mm"]-reference["distance_mm"]),
                                "delta_phase_deg": float(w["phase_deg"]-reference["phase_deg"])})
    result["return_drift_deg"] = [{"window_id": w["id"],
        "elapsed_s": float(w["start_s"]-reference["start_s"]),
        "phase_difference_deg": float(w["phase_deg"]-reference["phase_deg"])} for w in references[1:]]
    # 返程参考只用于报告回零漂移；不重复加权同一个零位进入斜率拟合。
    fit = [reference] + [w for w in valid if w["role"] == "displacement"]
    distances = {w["distance_mm"] for w in fit}
    if len(distances) < 3:
        errors.append("每个平台需要至少 3 个不同位置（含 reference）才能计算相位—位移斜率")
    if errors:
        return result
    x = [w["distance_mm"]-reference["distance_mm"] for w in fit]
    y = [w["phase_deg"]-reference["phase_deg"] for w in fit]
    x_mean, y_mean = mean(x), mean(y)
    denominator = sum((value-x_mean)**2 for value in x)
    slope = sum((a-x_mean)*(b-y_mean) for a, b in zip(x, y))/denominator
    intercept = y_mean-slope*x_mean
    residual_ss = sum((b-(intercept+slope*a))**2 for a, b in zip(x, y))
    total_ss = sum((b-y_mean)**2 for b in y)
    result.update(status="ready", sensitivity_deg_per_mm=float(slope), intercept_deg=float(intercept),
                  fit_rmse_deg=float(math.sqrt(residual_ss/len(x))),
                  r_squared=float(1.-residual_ss/total_ss) if total_ss > 1e-20 else None,
                  fitted_window_count=len(fit), distinct_position_count=len(distances))
    return result


def analyze_phase(rows, annotations, baseline, *, temperature_tolerance_c=.5):
    """分析一个 run 的完整相位序列与非重叠稳定区间；输入保持不变。

    annotations: id, start_s, end_s, temperature_c, temperature_end_c,
    distance_mm, block, role (reference/displacement)。时间采用 [start, end)。
    返回 ready/partial/rejected；partial 表示仅部分平台/窗口有效，必须保留错误。
    服务层仍须核验原始记录 complete、停止证据以及各输入的来源哈希。
    """
    report = {"algorithm_version": ALGORITHM_VERSION, "status": "rejected", "errors": [],
              "warnings": ["温度与时间、装置热漂移可能共同变化；相位差不能单独证明液体温度的因果影响。",
                           "参考温度是人工首尾读数；首尾满足容差不等于已验证全程热稳定。",
                           "斜率和拟合 RMSE 是本次窗口的描述性结果，不是毫米测量精度或跨 run 标定。"],
              "windows": [], "blocks": [], "thermal_drift": [],
              "scope": "same_continuous_run_only", "policy": dict(POLICY)}
    errors = report["errors"]
    if not _finite(temperature_tolerance_c) or temperature_tolerance_c <= 0:
        errors.append("温度容差必须为正有限摄氏度数值")
        return report
    report["policy"]["temperature_tolerance_c"] = float(temperature_tolerance_c)
    try:
        rows, annotations = list(rows), list(annotations)
    except TypeError:
        errors.append("相位记录和区间标注必须是列表")
        return report
    fields = ("end_s", "min_coherent_fraction", "amplitude_sd_dB",
              "phase_linear_drift_deg_per_s", "phase_residual_sd_deg")
    if (not isinstance(baseline, dict) or any(not _finite(baseline.get(k)) for k in fields)
            or baseline.get("end_s", -1) < 0):
        errors.append("缺少有效基线及基线结束时间，不能分析相对相位")
        return report
    if (not 0 <= baseline["min_coherent_fraction"] <= 1 or baseline["amplitude_sd_dB"] < 0
            or baseline["phase_residual_sd_deg"] < 0 or baseline_problems(baseline)):
        errors.append("静止基线质量未通过，不能分析相对相位")
        return report
    if len(rows) < 2 or any(not isinstance(r, dict) or not _finite(r.get("t_s")) for r in rows):
        errors.append("需要时间有效且严格递增的相位记录")
        return report
    intervals = [b["t_s"]-a["t_s"] for a, b in zip(rows, rows[1:])]
    if min(intervals) <= 0:
        errors.append("相位记录时间必须严格递增，不能包含重复或倒序")
        return report
    cadence = float(median(intervals))
    if any(delta > 1.5*cadence+1e-9 for delta in intervals):
        errors.append("完整相位记录存在时间缺口，不能跨缺口沿用相位参考")
    run_keys = {str(r["run_key"]) for r in rows if r.get("run_key") is not None}
    if len(run_keys) > 1:
        errors.append("输入含多个 run，禁止跨 run 相减相位")
    has_sample_identity = any("start_sample" in r or "count" in r for r in rows)
    if has_sample_identity:
        previous_end = None
        for row in rows:
            start, count = row.get("start_sample"), row.get("count")
            if (not isinstance(start, int) or isinstance(start, bool) or start < 0 or
                    not isinstance(count, int) or isinstance(count, bool) or count <= 0):
                errors.append("源样本起点或计数缺失/无效，不能确认相位连续性")
                break
            if previous_end is not None and start != previous_end:
                errors.append("源样本存在缺口、重复或重叠，不能沿用相位参考")
                break
            previous_end = start+count
    else:
        report["warnings"].append("没有 start_sample/count，仅能检查记录时间连续性。")
    if not annotations:
        errors.append("请添加带参考温度和距离的稳定区间")
    normalized, ids = [], set()
    for i, item in enumerate(annotations):
        if not isinstance(item, dict):
            errors.append(f"标注 {i+1} 结构无效")
            continue
        ident = item.get("id", f"window_{i+1}")
        if not isinstance(ident, str) or not ident.strip() or ident in ids:
            errors.append(f"标注 {i+1} 的 id 为空或重复")
            continue
        ids.add(ident)
        block = item.get("block")
        if not isinstance(block, str) or not block.strip():
            errors.append(f"{ident}: 缺少明确温度平台 block")
            continue
        a = {"id": ident, "block": block.strip(), "role": item.get("role"),
             **{k: item.get(k) for k in ("start_s", "end_s", "temperature_c",
                                         "temperature_end_c", "distance_mm")}}
        if (a["role"] not in ("reference", "displacement") or
                any(not _finite(a[k]) for k in ("start_s", "end_s")) or
                a["start_s"] < 0 or a["end_s"] <= a["start_s"]):
            errors.append(f"{ident}: 区间角色或时间范围无效")
            continue
        normalized.append(a)
    ordered = sorted(normalized, key=lambda a: a["start_s"])
    if any(b["start_s"] < a["end_s"] for a, b in zip(ordered, ordered[1:])):
        errors.append("标注区间重叠，不能重复使用同一批样本")
    if errors:
        return report
    indexed = list(enumerate(rows))
    for a in normalized:
        extra_errors = []
        if a["start_s"] < baseline["end_s"]:
            extra_errors.append("区间早于基线结束时间")
        if not _finite(a["distance_mm"]) or not 0 <= a["distance_mm"] <= 1e6:
            extra_errors.append("距离必须是 0–1000000 范围内的有限毫米数")
            a["distance_mm"] = None
        if any(not _finite(a[k]) or not -273.15 < a[k] <= 1e6
               for k in ("temperature_c", "temperature_end_c")):
            extra_errors.append("缺少有效的窗口首尾参考温度（必须高于绝对零度）；不能由设定温度代替")
            for key in ("temperature_c", "temperature_end_c"):
                if not _finite(a[key]) or not -273.15 < a[key] <= 1e6:
                    a[key] = None
            a["temperature_mean_c"] = None
        else:
            a["temperature_mean_c"] = float((a["temperature_c"]+a["temperature_end_c"])/2.)
            if abs(a["temperature_end_c"]-a["temperature_c"]) > temperature_tolerance_c+1e-9:
                extra_errors.append("窗口首尾参考温差超过容差")
        w = _window(rows, indexed, a, cadence, POLICY)
        w["errors"].extend(extra_errors)
        w["valid"] = not w["errors"]
        # 行号和样本区间已经保存在原始源哈希中；保留窗口统计，避免巨大GUI报告。
        w.pop("row_indices", None)
        w.pop("sample_spans", None)
        report["windows"].append(w)
        errors.extend(f"{w['id']}: {e}" for e in w["errors"])
    groups = defaultdict(list)
    for w in report["windows"]:
        groups[w["block"]].append(w)
    report["blocks"] = [_fit_block(name, group, temperature_tolerance_c) for name, group in groups.items()]
    if len(groups) < 2:
        report["warnings"].append("目前仅有一个温度平台；可描述该温度下的响应，尚不能比较温度影响。")
    for block in report["blocks"]:
        errors.extend(f"{block['block']}: {e}" for e in block["errors"])
        if any(abs(item["phase_difference_deg"]) > .5 for item in block["return_drift_deg"]):
            report["warnings"].append(
                f"{block['block']}: 返程参考相位偏离初始参考超过 0.5°；存在回零漂移/迟滞，"
                "请结合往返顺序解释斜率，不能将其直接归因为温度。")
    # 固定位置温漂独立于斜率是否可拟合：单reference平台也可描述温漂。
    references = [w for w in report["windows"] if w["valid"] and w["role"] == "reference"]
    first_by_distance = {}
    for w in sorted(references, key=lambda value: value["start_s"]):
        distance = w["distance_mm"]
        if distance not in first_by_distance:
            first_by_distance[distance] = w
            continue
        first = first_by_distance[distance]
        if first["block"] == w["block"]:
            continue
        report["thermal_drift"].append({"reference_block": first["block"], "target_block": w["block"],
            "reference_window_id": first["id"], "target_window_id": w["id"], "distance_mm": float(distance),
            "temperature_difference_c": float(w["temperature_mean_c"]-first["temperature_mean_c"]),
            "phase_difference_deg": float(w["phase_deg"]-first["phase_deg"]),
            "elapsed_s": float(w["start_s"]-first["start_s"]),
            "scope": "same_run_same_distance_temperature_and_time_confounded"})
    if len(groups) > 1 and not report["thermal_drift"]:
        report["warnings"].append("温度平台间缺少共同距离的有效 reference，未计算温度相位差。")
    ready = any(b["status"] == "ready" for b in report["blocks"])
    if ready or report["thermal_drift"]:
        report["status"] = "partial" if errors else "ready"
    return report
