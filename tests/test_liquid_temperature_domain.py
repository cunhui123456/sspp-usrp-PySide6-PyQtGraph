"""合成温度—位移数据验证；不接触硬件或真实实验数据。"""
import copy
import json
import math
import unittest

from sspp_experiments.liquid_temperature import analyze_phase


def make_case():
    baseline = {"end_s": .5, "min_coherent_fraction": .99, "amplitude_sd_dB": .01,
                "phase_linear_drift_deg_per_s": .01, "phase_residual_sd_deg": .01}
    stages = [(1., "cool", 20., 10., "reference"), (3., "cool", 20., 20., "displacement"),
              (5., "cool", 20., 30., "displacement"), (7., "cool", 20., 10., "reference"),
              (10., "warm", 40., 10., "reference"), (12., "warm", 40., 20., "displacement"),
              (14., "warm", 40., 30., "displacement")]
    annotations = [{"id": f"w{i}", "start_s": t, "end_s": t+1, "block": block,
                    "temperature_c": temperature, "temperature_end_c": temperature,
                    "distance_mm": distance, "role": role}
                   for i, (t, block, temperature, distance, role) in enumerate(stages)]
    rows = []
    for i in range(320):
        t = .025+i*.05
        stage = next((a for a in annotations if a["start_s"] <= t < a["end_s"]), None)
        if stage:
            slope, offset = (2., 0.) if stage["block"] == "cool" else (3., 7.)
            phase = offset+slope*(stage["distance_mm"]-10.)
        else:
            phase = 0.
        rows.append({"t_s": t, "start_sample": i*50, "count": 50,
                     "relative_phase_deg": phase+.02*math.sin(i*math.pi/2),
                     "relative_amplitude_dB": -.1+.01*math.cos(i*math.pi/2),
                     "coherent_fraction": .98, "label": stage["id"] if stage else "transition_move"})
    return rows, annotations, baseline


class LiquidTemperatureDomainTests(unittest.TestCase):
    def test_injected_temperature_dependent_slopes_and_same_position_phase_drift(self):
        args = make_case()
        original = copy.deepcopy(args)
        report = analyze_phase(*args)
        self.assertEqual(report["status"], "ready", report["errors"])
        self.assertEqual(args, original)
        self.assertAlmostEqual(report["blocks"][0]["sensitivity_deg_per_mm"], 2.)
        self.assertAlmostEqual(report["blocks"][1]["sensitivity_deg_per_mm"], 3.)
        self.assertAlmostEqual(report["blocks"][0]["fit_rmse_deg"], 0.)
        self.assertAlmostEqual(report["blocks"][1]["r_squared"], 1.)
        self.assertEqual(report["blocks"][0]["temperature_range_c"], [20., 20.])
        self.assertAlmostEqual(report["thermal_drift"][0]["phase_difference_deg"], 7.)
        self.assertEqual(report["thermal_drift"][0]["temperature_difference_c"], 20.)
        self.assertEqual(report["thermal_drift"][0]["elapsed_s"], 9.)
        self.assertIn("时间", " ".join(report["warnings"]))
        self.assertNotIn("model", report)
        json.dumps(report, allow_nan=False)

    def test_return_reference_drift_reported_without_reweighting_slope(self):
        rows, annotations, baseline = make_case()
        for row in rows:
            if 7 <= row["t_s"] < 8:
                row["relative_phase_deg"] += 6.
        report = analyze_phase(rows, annotations, baseline)
        self.assertAlmostEqual(report["blocks"][0]["sensitivity_deg_per_mm"], 2.)
        self.assertAlmostEqual(report["blocks"][0]["return_drift_deg"][0]["phase_difference_deg"], 6.)
        self.assertEqual(report["blocks"][0]["fitted_window_count"], 3)

    def test_window_temperature_end_drift_excluded_and_block_range_enforced(self):
        rows, annotations, baseline = make_case()
        annotations[1]["temperature_end_c"] = 21.
        report = analyze_phase(rows, annotations, baseline)
        self.assertFalse(report["windows"][1]["valid"])
        self.assertIsNone(report["blocks"][0]["sensitivity_deg_per_mm"])
        self.assertEqual(report["status"], "partial")
        rows, annotations, baseline = make_case()
        annotations[1].update(temperature_c=21., temperature_end_c=21.)
        report = analyze_phase(rows, annotations, baseline)
        self.assertTrue(report["windows"][1]["valid"])
        self.assertEqual(report["blocks"][0]["status"], "rejected")
        self.assertIn("温度平台", " ".join(report["blocks"][0]["errors"]))

    def test_missing_reference_temperature_or_distance_is_not_imputed(self):
        for field, value in (("temperature_c", None), ("temperature_end_c", float("nan")),
                             ("temperature_c", -1000.), ("temperature_c", -273.15),
                             ("distance_mm", float("inf")), ("distance_mm", -1.)):
            with self.subTest(field=field, value=value):
                rows, annotations, baseline = make_case()
                annotations[0][field] = value
                report = analyze_phase(rows, annotations, baseline)
                self.assertFalse(report["windows"][0]["valid"])
                json.dumps(report, allow_nan=False)

    def test_missing_bad_baseline_and_prebaseline_windows_rejected(self):
        rows, annotations, baseline = make_case()
        for bad in (None, {}, dict(baseline, end_s=float("nan")),
                    dict(baseline, phase_linear_drift_deg_per_s=2.),
                    dict(baseline, min_coherent_fraction=2.), dict(baseline, amplitude_sd_dB=-.5)):
            self.assertEqual(analyze_phase(rows, annotations, bad)["status"], "rejected")
        baseline["end_s"] = 1.5
        report = analyze_phase(rows, annotations, baseline)
        self.assertFalse(report["windows"][0]["valid"])
        self.assertIn("基线", " ".join(report["windows"][0]["errors"]))

    def test_overlap_and_duplicate_annotations_rejected_globally(self):
        for mutation in (lambda a: a[1].update(start_s=1.5), lambda a: a[1].update(id="w0")):
            rows, annotations, baseline = make_case()
            mutation(annotations)
            report = analyze_phase(rows, annotations, baseline)
            self.assertEqual(report["status"], "rejected")
            self.assertEqual(report["blocks"], [])

    def test_global_time_or_sample_gap_even_outside_windows_rejects_phase_reference(self):
        for mutation in (lambda rows: rows.pop(50), lambda rows: rows[50].update(start_sample=2499),
                         lambda rows: rows[50].pop("count"),
                         lambda rows: rows[50].update(t_s=rows[49]["t_s"]),
                         lambda rows: rows[50].update(t_s=float("nan"))):
            rows, annotations, baseline = make_case()
            mutation(rows)
            report = analyze_phase(rows, annotations, baseline)
            self.assertEqual(report["status"], "rejected")
            self.assertEqual(report["thermal_drift"], [])

    def test_incomplete_window_and_low_quality_excluded(self):
        for field, value in (("relative_phase_deg", float("nan")),
                             ("coherent_fraction", .5), ("label", "transition:still_moving"),
                             ("relative_amplitude_dB", float("inf"))):
            with self.subTest(field=field):
                rows, annotations, baseline = make_case()
                for row in rows:
                    if 3 <= row["t_s"] < 4:
                        row[field] = value
                report = analyze_phase(rows, annotations, baseline)
                self.assertFalse(report["windows"][1]["valid"])
                self.assertIsNone(report["blocks"][0]["sensitivity_deg_per_mm"])
                self.assertEqual(report["status"], "partial")
                json.dumps(report, allow_nan=False)
        rows, annotations, baseline = make_case()
        annotations[-1]["end_s"] = 17.
        report = analyze_phase(rows, annotations, baseline)
        self.assertFalse(report["windows"][-1]["valid"])

    def test_phase_drift_within_window_is_rejected(self):
        rows, annotations, baseline = make_case()
        for row in rows:
            if 3 <= row["t_s"] < 4:
                row["relative_phase_deg"] += 5*(row["t_s"]-3)
        report = analyze_phase(rows, annotations, baseline)
        self.assertFalse(report["windows"][1]["valid"])
        self.assertIn("持续漂移", " ".join(report["windows"][1]["errors"]))

    def test_reference_missing_different_distance_and_wrong_order(self):
        rows, annotations, baseline = make_case()
        annotations[4]["distance_mm"] = 12.
        report = analyze_phase(rows, annotations, baseline)
        self.assertEqual(report["thermal_drift"], [])
        self.assertIn("共同距离", " ".join(report["warnings"]))
        rows, annotations, baseline = make_case()
        annotations[0]["role"] = "displacement"
        report = analyze_phase(rows, annotations, baseline)
        self.assertEqual(report["blocks"][0]["status"], "rejected")
        self.assertIn("最早", " ".join(report["blocks"][0]["errors"]))
        annotations[3]["role"] = "displacement"
        report = analyze_phase(rows, annotations, baseline)
        self.assertIn("缺少", " ".join(report["blocks"][0]["errors"]))

    def test_multiple_runs_and_invalid_tolerance_rejected(self):
        rows, annotations, baseline = make_case()
        rows[0]["run_key"], rows[1]["run_key"] = "a", "b"
        report = analyze_phase(rows, annotations, baseline)
        self.assertEqual(report["status"], "rejected")
        self.assertIn("跨 run", " ".join(report["errors"]))
        for value in (0, -1, float("nan"), True):
            self.assertEqual(analyze_phase(*make_case(), temperature_tolerance_c=value)["status"], "rejected")

    def test_temperature_only_reference_windows_retain_descriptive_drift(self):
        rows, annotations, baseline = make_case()
        selected = [a for a in annotations if a["role"] == "reference"]
        report = analyze_phase(rows, selected, baseline)
        self.assertEqual(report["status"], "partial")
        self.assertAlmostEqual(report["thermal_drift"][0]["phase_difference_deg"], 7.)
        self.assertTrue(all(b["sensitivity_deg_per_mm"] is None for b in report["blocks"]))


if __name__ == "__main__":
    unittest.main()
