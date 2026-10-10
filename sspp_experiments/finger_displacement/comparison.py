"""跨记录仅叠加各自基线后的相对响应，禁止复用相位原点或毫米模型。"""
import bisect
import json
import math
from statistics import median

from ..comparison import chart, finite, read_json_source, read_source, series, table

MAX_PLOT_POINTS = 2500


def _plot_points(points, limit=MAX_PLOT_POINTS):
    """每桶保留极值；任何被省略的无效段仍令两侧曲线断开。"""
    if len(points) <= limit:
        return points
    bucket_count = max(1, (limit-5)//4)
    width = math.ceil(len(points)/bucket_count)
    selected = {0, len(points)-1}
    gaps = [i for i, (_, y) in enumerate(points) if y is None]
    for start in range(0, len(points), width):
        valid = [i for i in range(start, min(start+width, len(points))) if points[i][1] is not None]
        if valid:
            selected.add(min(valid, key=lambda i: points[i][1]))
            selected.add(max(valid, key=lambda i: points[i][1]))
    result, previous = [], None
    for index in sorted(selected):
        if previous is not None:
            position = bisect.bisect_right(gaps, previous)
            if position < len(gaps) and gaps[position] < index:
                result.append(points[gaps[position]])
        result.append(points[index])
        previous = index
    return result


def _load(record):
    baseline = read_json_source(record, "baseline.json")
    required = ("start_s", "end_s", "reference_amplitude", "reference_phase_unwrapped_deg",
                "min_coherent_fraction", "amplitude_sd_dB", "phase_linear_drift_deg_per_s", "phase_residual_sd_deg")
    if (not all(finite(baseline.get(k)) for k in required) or
            baseline["end_s"] <= baseline["start_s"] or baseline["reference_amplitude"] <= 0 or
            not 0 <= baseline["min_coherent_fraction"] <= 1 or
            baseline["amplitude_sd_dB"] < 0 or baseline["phase_residual_sd_deg"] < 0):
        raise ValueError("幅相记录缺少有效基线，不能比较相对响应")
    from sspp_lab.dsp import baseline_problems
    if baseline_problems(baseline):
        raise ValueError("幅相记录的基线未通过原工程质量门槛")
    try:
        raw = [json.loads(line) for line in read_source(record, "amplitude_phase.jsonl").splitlines() if line.strip()]
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("幅相 JSONL 存在损坏记录") from exc
    rows, previous_t = [], None
    for row in raw:
        if not isinstance(row, dict) or not finite(row.get("t_s")) or (previous_t is not None and row["t_s"] <= previous_t):
            raise ValueError("幅相时间必须有限且严格递增")
        previous_t = row["t_s"]
        for key in ("relative_amplitude_dB", "relative_phase_deg", "coherent_fraction"):
            if row.get(key) is not None and not finite(row[key]):
                raise ValueError("幅相记录中存在非有限读数")
        coherence = row.get("coherent_fraction")
        if coherence is not None and not 0 <= coherence <= 1:
            raise ValueError("幅相相干占比超出 0–1")
        if row["t_s"] <= baseline["end_s"]:
            continue
        start, count = row.get("start_sample"), row.get("count")
        if ((start is not None and (isinstance(start, bool) or not isinstance(start, int) or start < 0)) or
                (count is not None and (isinstance(count, bool) or not isinstance(count, int) or count <= 0))):
            raise ValueError("幅相样本位置/长度无效")
        rows.append({"key": record["key"], "label": record["label"], "t_s": row["t_s"],
                     "time_after_baseline_s": row["t_s"]-baseline["end_s"],
                     "relative_amplitude_dB": row.get("relative_amplitude_dB"),
                     "relative_phase_deg": row.get("relative_phase_deg"), "coherent_fraction": coherence,
                     "stage_label": str(row.get("label", "")), "start_sample": start, "count": count,
                     "gap_before": False})
    if not rows or not any(r["relative_phase_deg"] is not None and r["relative_amplitude_dB"] is not None for r in rows):
        raise ValueError("幅相记录没有可比较的基线后相对幅相数据")
    intervals = [b["t_s"]-a["t_s"] for a, b in zip(rows, rows[1:])]
    typical_interval = median(intervals) if intervals else None
    for previous, current in zip(rows, rows[1:]):
        if previous["start_sample"] is not None and previous["count"] is not None and current["start_sample"] is not None:
            expected = previous["start_sample"]+previous["count"]
            if current["start_sample"] < expected:
                raise ValueError("幅相样本窗口存在重叠或倒退")
            current["gap_before"] = current["start_sample"] != expected
        elif typical_interval:
            current["gap_before"] = current["t_s"]-previous["t_s"] > 1.5*typical_interval
    return baseline, raw, rows


def _span(rows, key):
    values = [r[key] for r in rows if r[key] is not None]
    return max(values)-min(values) if values else None


def compare_phase(records, reference_index, report):
    curves = {"relative_amplitude_dB": [], "relative_phase_deg": [], "coherent_fraction": []}
    all_rows = []
    for record in records:
        baseline, raw, rows = _load(record)
        all_rows.extend(rows)
        for field, items in curves.items():
            points = []
            for row in rows:
                t = row["time_after_baseline_s"]
                if row["gap_before"]:
                    points.append([t, None])
                points.append([t, row[field]])
            items.append(series(record, _plot_points(points)))
        coherent = [r["coherent_fraction"] for r in rows if r["coherent_fraction"] is not None]
        missing = sum(r["relative_phase_deg"] is None or r["relative_amplitude_dB"] is None for r in rows)
        gaps = sum(r["gap_before"] for r in rows)
        report["metrics"].append({"key": record["key"], "label": record["label"],
            "processed_windows": len(raw), "post_baseline_windows": len(rows),
            "baseline_end_s": baseline["end_s"], "response_end_s": rows[-1]["time_after_baseline_s"],
            "baseline_amplitude_sd_dB": baseline["amplitude_sd_dB"],
            "baseline_phase_residual_sd_deg": baseline["phase_residual_sd_deg"],
            "baseline_phase_drift_deg_per_s": baseline["phase_linear_drift_deg_per_s"],
            "min_coherent_fraction": min(coherent) if coherent else None,
            "low_coherence_windows": sum(c < .8 for c in coherent),
            "missing_relative_windows": missing, "sample_gaps": gaps,
            "transition_windows": sum(r["stage_label"].lower().startswith("transition") for r in rows),
            "amplitude_peak_to_peak_dB": _span(rows, "relative_amplitude_dB"),
            "phase_peak_to_peak_deg": _span(rows, "relative_phase_deg")})
        if gaps or missing:
            report["warnings"].append(f"{record['label']}：存在 {gaps} 处样本/时间缺口、{missing} 个相对值缺失窗口，曲线断线保留。")
        if any(c < .8 for c in coherent):
            report["warnings"].append(f"{record['label']}：部分窗口相干占比低于 0.8；仍展示保存读数，不能把该处相位当作可靠位移。")
    note = "横轴为本记录 t_s − baseline.end_s；纵轴直接使用已保存的本记录基线相对值，不再次归零或解缠。各记录动作发生时间并未对齐。图最多2500点/序列，保留桶内极值及缺口；CSV保留全部基线后窗口。"
    report["charts"] = [
        chart("phase_relative_amplitude", "各记录自身基线后的相对幅度", "距各自基线结束（s）", "相对幅度（dB）", curves["relative_amplitude_dB"], note),
        chart("phase_relative_phase", "各记录自身基线后的相对相位", "距各自基线结束（s）", "相对相位（°）", curves["relative_phase_deg"], note),
        chart("phase_coherence", "相干单音占比与信号质量", "距各自基线结束（s）", "相干占比", curves["coherent_fraction"], "低相干读数保留用于诊断；0.8仅为当前手指估算质量参考门槛，不是统计显著性门槛。")]
    report["charts"][-1]["horizontal"] = [{"y": .8, "label": "质量参考 0.8"}]
    report["quantitative_difference_allowed"] = False
    report["warnings"].extend([
        "每次采集有独立相位原点：只比较相对响应形状，不跨记录相减相位，不复用毫米模型或输出跨会话绝对位移。",
        "各动作/过渡窗口保留为实际响应；全程峰峰值含运动与漂移，不是静止段噪声或可重复性。仅按基线结束对齐，不能推断动作时延差。"])
    report["tables"] = {
        "phase_summary": table(list(report["metrics"][0]), list(report["metrics"])),
        "phase_curves": table(["key", "label", "t_s", "time_after_baseline_s", "relative_amplitude_dB",
                               "relative_phase_deg", "coherent_fraction", "stage_label", "start_sample", "count", "gap_before"], all_rows)}
