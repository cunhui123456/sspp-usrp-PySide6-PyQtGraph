"""离线合成数据覆盖标定、留出验证与无效距离的拒绝；不导入采集模块。"""
import copy
import json
import math
import unittest

from sspp_experiments.finger_displacement import analyze, estimate


def make_case(direction=1):
    stages = [(1., 10., "calibration"), (4., 20., "calibration"), (7., 30., "calibration"),
              (10., 15., "validation"), (13., 25., "validation"), (16., 23., "observation")]
    annotations = [{"id": f"w{i}", "start_s": start, "end_s": start+1,
                    "distance_mm": distance, "role": role, "action": f"尺量后静止 {i}"}
                   for i, (start, distance, role) in enumerate(stages)]
    rows = []
    for i in range(360):
        t = .025+i*.05
        stage = next((a for a in annotations if a["start_s"] <= t < a["end_s"]), None)
        distance = stage["distance_mm"] if stage else 20.
        rows.append({"t_s": t, "start_sample": i*50, "count": 50,
                     "relative_phase_deg": direction*(30+2*distance)+.02*math.sin(i*math.pi/2),
                     "relative_amplitude_dB": -.1+.01*math.cos(i*math.pi/2),
                     "coherent_fraction": .98, "label": stage["id"] if stage else "transition:move"})
    return rows, annotations


def change_window(rows, annotation, **fields):
    for row in rows:
        if annotation["start_s"] <= row["t_s"] < annotation["end_s"]:
            row.update(fields)


class FingerDomainTests(unittest.TestCase):
    def report(self, rows, annotations, **kwargs):
        return analyze(rows, annotations, initial_distance_mm=20., run_key="run-a", **kwargs)

    def test_increasing_and_decreasing_mapping_same_run_json_and_inputs_immutable(self):
        for direction in (1, -1):
            with self.subTest(direction=direction):
                rows, annotations = make_case(direction)
                original = copy.deepcopy((rows, annotations))
                result = self.report(rows, annotations)
                self.assertEqual(result["status"], "ready", result["errors"])
                self.assertEqual((rows, annotations), original)
                json.dumps(result, allow_nan=False)
                model = json.loads(json.dumps(result["model"]))
                value = estimate(model, direction*76., run_key="run-a", coherent_fraction=.98, relative_amplitude_dB=-.1)
                self.assertTrue(value["ok"])
                self.assertAlmostEqual(value["distance_mm"], 23.)
                self.assertAlmostEqual(value["displacement_mm"], 3.)
                self.assertEqual(len(result["results"]), len(rows))
                self.assertFalse(result["results"][0]["ok"])
                self.assertTrue(result["observations"][0]["ok"])
                self.assertEqual(result["windows"][0]["sample_spans"][0]["start_sample"], 1000)
                self.assertEqual(result["validation"]["scope"], "within_run_held_out_intervals_only")

    def test_requires_three_calibration_and_two_distinct_validation_distances(self):
        rows, annotations = make_case()
        result = self.report(rows, [a for a in annotations if a["id"] != "w2"])
        self.assertIn("3 个不同", " ".join(result["errors"]))
        annotations[4]["distance_mm"] = annotations[3]["distance_mm"]
        result = self.report(rows, annotations)
        self.assertEqual(result["status"], "validation_failed")
        self.assertIn("2 个不同", " ".join(result["errors"]))
        self.assertFalse(result["model"]["active"])
        self.assertEqual(result["results"], [])

    def test_validation_tolerance_is_editable_and_error_is_held_out(self):
        rows, annotations = make_case()
        for a in annotations:
            if a["role"] == "validation":
                for row in rows:
                    if a["start_s"] <= row["t_s"] < a["end_s"]:
                        row["relative_phase_deg"] += 5.
        result = self.report(rows, annotations)
        self.assertEqual(result["status"], "validation_failed")
        self.assertAlmostEqual(result["validation"]["max_error_mm"], 2.5)
        self.assertEqual(self.report(rows, annotations, tolerance_mm=3.)["status"], "ready")
        self.assertEqual(result["calibration"]["nodes"][1]["phase_deg"], 70.)

    def test_cross_run_missing_quality_outside_range_and_model_tamper_rejected(self):
        rows, annotations = make_case()
        model = self.report(rows, annotations)["model"]
        for params in ({"run_key": "another-run", "coherent_fraction": .98, "relative_amplitude_dB": -.1},
                       {"run_key": "run-a", "relative_amplitude_dB": -.1},
                       {"run_key": "run-a", "coherent_fraction": .98},
                       {"run_key": "run-a", "coherent_fraction": .4, "relative_amplitude_dB": -.1},
                       {"run_key": "run-a", "coherent_fraction": .98, "relative_amplitude_dB": 8.}):
            with self.subTest(params=params):
                self.assertFalse(estimate(model, 76., **params)["ok"])
        for phase in (49., 91., None, float("nan")):
            self.assertFalse(estimate(model, phase, run_key="run-a", coherent_fraction=.98, relative_amplitude_dB=-.1)["ok"])
        model["initial_distance_mm"] = 1.
        self.assertFalse(estimate(model, 76., run_key="run-a", coherent_fraction=.98, relative_amplitude_dB=-.1)["ok"])

    def test_annotation_overlap_and_duplicate_raw_samples_are_leakage(self):
        rows, annotations = make_case()
        annotations[3]["start_s"] = 1.5
        self.assertIn("重叠", " ".join(self.report(rows, annotations)["errors"]))
        rows, annotations = make_case()
        for row in rows:
            if 10 <= row["t_s"] < 11:
                row["start_sample"] -= 9000
        self.assertIn("源样本", " ".join(self.report(rows, annotations)["errors"]))

    def test_same_static_stage_cannot_be_split_for_independent_validation(self):
        annotations = [
            {"id": "a", "start_s": 1., "end_s": 2., "role": "calibration", "distance_mm": 10., "action": "静止"},
            {"id": "b", "start_s": 3.2, "end_s": 4.2, "role": "validation", "distance_mm": 10.5, "action": "静止"},
            {"id": "c", "start_s": 6., "end_s": 7., "role": "calibration", "distance_mm": 20., "action": "静止"},
            {"id": "d", "start_s": 8.2, "end_s": 9.2, "role": "validation", "distance_mm": 20.5, "action": "静止"},
            {"id": "e", "start_s": 11., "end_s": 12., "role": "calibration", "distance_mm": 30., "action": "静止"}]
        rows = [{"t_s": .025+i*.05, "relative_phase_deg": 50. if i < 100 else (70. if i < 200 else 90.),
                 "relative_amplitude_dB": -.1, "coherent_fraction": .98, "label": "same_hold"} for i in range(260)]
        result = self.report(rows, annotations)
        self.assertEqual(result["status"], "validation_failed")
        self.assertIn("同一连续静止阶段", " ".join(result["errors"]))

    def test_free_historical_generic_labels_need_action_and_motion_evidence(self):
        rows, annotations = make_case()
        for row in rows:
            row["label"] = "historical_generic"
        result = self.report(rows, annotations)
        self.assertEqual(result["status"], "ready", result["errors"])
        self.assertTrue(all(e["basis"] == "operator_annotated_motion" for e in result["validation"]["independence"]))
        for a in annotations:
            a["action"] = ""
        self.assertEqual(self.report(rows, annotations)["status"], "validation_failed")

    def test_temporal_guard_required_even_with_different_stage_labels(self):
        rows, annotations = make_case()
        annotations[3].update(start_s=8.5, end_s=9.5)
        change_window(rows, annotations[3], relative_phase_deg=60., label="new_validation")
        result = self.report(rows, annotations)
        self.assertIn("至少间隔", " ".join(result["errors"]))

    def test_nonmonotonic_and_insignificant_phase_rejected(self):
        rows, annotations = make_case()
        change_window(rows, annotations[1], relative_phase_deg=100.)
        self.assertIn("未严格单调", " ".join(self.report(rows, annotations)["errors"]))
        rows, annotations = make_case()
        for index, a in enumerate(annotations[:3]):
            change_window(rows, a, relative_phase_deg=50.+.1*index)
        self.assertIn("噪声门槛", " ".join(self.report(rows, annotations)["errors"]))

    def test_consistent_repeat_combined_inconsistent_repeat_rejected(self):
        rows, annotations = make_case()
        annotations[5].update(role="calibration", distance_mm=20.)
        change_window(rows, annotations[5], relative_phase_deg=70.)
        result = self.report(rows, annotations)
        self.assertEqual(result["status"], "ready", result["errors"])
        self.assertEqual(len(result["calibration"]["nodes"]), 3)
        self.assertEqual(len(result["calibration"]["nodes"][1]["window_ids"]), 2)
        change_window(rows, annotations[5], relative_phase_deg=73.)
        self.assertIn("迟滞", " ".join(self.report(rows, annotations)["errors"]))

    def test_missing_baseline_coherence_transition_and_nonfinite_values_rejected(self):
        for field, value in (("relative_phase_deg", None), ("relative_amplitude_dB", float("nan")),
                             ("coherent_fraction", .7), ("coherent_fraction", 1.1), ("label", "transition:move")):
            with self.subTest(field=field, value=value):
                rows, annotations = make_case()
                change_window(rows, annotations[0], **{field: value})
                result = self.report(rows, annotations)
                self.assertEqual(result["status"], "invalid")
                self.assertIsNone(result["model"])
                json.dumps(result, allow_nan=False)

    def test_min_duration_sample_count_coverage_and_gaps(self):
        rows, annotations = make_case()
        annotations[0]["end_s"] = 1.4
        self.assertIn("持续时间", " ".join(self.report(rows, annotations)["errors"]))
        rows, annotations = make_case()
        self.assertIn("数量不足", " ".join(self.report(rows, annotations, min_samples=25)["errors"]))
        rows, annotations = make_case()
        trimmed = [r for r in rows if r["t_s"] >= 1.3]
        self.assertIn("覆盖", " ".join(self.report(trimmed, annotations)["errors"]))
        rows, annotations = make_case()
        del rows[30]
        result = self.report(rows, annotations)
        self.assertIn("缺口", " ".join(result["errors"]))

    def test_gap_between_selected_intervals_also_invalidates_phase_reference(self):
        rows, annotations = make_case()
        del rows[50]  # 未落入任何标注区间，仍不能忽略相位链缺口。
        self.assertIn("缺口", " ".join(self.report(rows, annotations)["errors"]))

    def test_unstable_phase_and_amplitude_rejected(self):
        for kind in ("drift", "phase", "amplitude"):
            with self.subTest(kind=kind):
                rows, annotations = make_case()
                for row in rows:
                    if 1 <= row["t_s"] < 2:
                        if kind == "drift":
                            row["relative_phase_deg"] += 5*(row["t_s"]-1)
                        elif kind == "phase":
                            row["relative_phase_deg"] += 10*math.sin(100*row["t_s"])
                        else:
                            row["relative_amplitude_dB"] += 2*math.sin(100*row["t_s"])
                self.assertEqual(self.report(rows, annotations)["status"], "invalid")

    def test_validation_distance_outside_range_and_no_validation_no_activation(self):
        rows, annotations = make_case()
        annotations[3]["distance_mm"] = 5.
        self.assertIn("尺量距离超出", " ".join(self.report(rows, annotations)["errors"]))
        rows, annotations = make_case()
        result = self.report(rows, [a for a in annotations if a["role"] != "validation"])
        self.assertFalse(result["model"]["active"])
        self.assertFalse(estimate(result["model"], 70., run_key="run-a", coherent_fraction=.98, relative_amplitude_dB=-.1)["ok"])

    def test_invalid_observation_keeps_valid_model_and_estimation_gap(self):
        rows, annotations = make_case()
        change_window(rows, annotations[5], coherent_fraction=.4)
        result = self.report(rows, annotations)
        self.assertEqual(result["status"], "ready", result["errors"])
        self.assertFalse(result["observations"][0]["ok"])
        self.assertIsNone(result["results"][320]["distance_mm"])

    def test_invalid_arguments_and_nonincreasing_time(self):
        rows, annotations = make_case()
        for kwargs in ({"tolerance_mm": 0}, {"min_coherent_fraction": float("nan")}, {"min_samples": 2}):
            self.assertEqual(self.report(rows, annotations, **kwargs)["status"], "invalid")
        self.assertEqual(analyze(rows, annotations, 20., run_key="")["status"], "invalid")
        rows[4]["t_s"] = rows[3]["t_s"]
        self.assertIn("严格递增", " ".join(self.report(rows, annotations)["errors"]))


if __name__ == "__main__":
    unittest.main()
