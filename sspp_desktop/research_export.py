"""研究派生图表：使用独立 Agg Figure；原始 IQ 与 manifest 永不改写。"""
from pathlib import Path
import numpy as np


def export_report(folder, result, estimates):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = Figure(figsize=(11, 8), layout="constrained")
    FigureCanvasAgg(fig)
    axes = fig.subplots(2, 2)
    windows = [w for w in result.get("windows", []) if w.get("valid", w.get("ok", True))]
    for role, color, title in (("calibration", "#087f85", "Calibration"), ("validation", "#c87530", "Held-out validation")):
        points = [w for w in windows if w.get("role") == role and w.get("distance_mm") is not None]
        xs = [w["distance_mm"] for w in points]
        ys = [w.get("phase_mean_deg", w.get("phase_deg", w.get("phase_median_deg", float("nan")))) for w in points]
        axes[0, 0].scatter(xs, ys, color=color, label=title)
    nodes = (result.get("calibration") or {}).get("nodes", [])
    if nodes:
        axes[0, 0].plot([n["distance_mm"] for n in nodes], [n["phase_deg"] for n in nodes], color="#087f85", linewidth=.8)
    axes[0, 0].set(xlabel="Ruler distance to sensor (mm)", ylabel="Relative phase (deg)", title="Same-run calibration and validation")
    axes[0, 0].legend(loc="best")
    validation = result.get("validation") or {}
    points = validation.get("windows", [])
    for point in points:
        truth = point.get("distance_mm", point.get("true_distance_mm"))
        predicted = point.get("estimated_distance_mm", point.get("predicted_distance_mm"))
        if truth is not None and predicted is not None:
            axes[0, 1].scatter(truth, predicted - truth, color="#c87530")
    tolerance = result.get("tolerance_mm", 2.)
    axes[0, 1].axhline(0, color="#69808a", linewidth=.8)
    for y in (-tolerance, tolerance):
        axes[0, 1].axhline(y, color="#b14040", linestyle="--", linewidth=.8)
    axes[0, 1].set(xlabel="Ruler distance (mm)", ylabel="Estimate minus ruler (mm)", title="Independent interval errors")
    if estimates:
        x = [r.get("t_s") for r in estimates]
        for axis, field, title in ((axes[1, 0], "distance_mm", "Distance to sensor (mm)"),
                                   (axes[1, 1], "displacement_mm", "Displacement from initial (mm)")):
            y = [np.nan if r.get(field) is None else r[field] for r in estimates]
            axis.plot(x, y, color="#087f85", linewidth=.7)
            axis.set(xlabel="Hardware sample time (s)", ylabel=title)
    else:
        for axis in axes[1]:
            axis.text(.5, .5, "Estimate unavailable\nCalibration / validation not accepted", ha="center", va="center", transform=axis.transAxes)
    for axis in axes.flat:
        axis.grid(alpha=.2)
    fig.suptitle("SSPP finger research | " + result.get("run_key", "") + " | " + str(result.get("status", "")))
    fig.savefig(Path(folder) / "research_report.png", dpi=160)
    fig.savefig(Path(folder) / "research_report.pdf")
    fig.clear()
