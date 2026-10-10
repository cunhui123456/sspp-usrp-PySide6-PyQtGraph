"""Offline comparison contracts: no UHD, devices, Qt or source mutations."""
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from sspp_experiments.comparison import build_comparison, history_category
from sspp_experiments.finger_displacement.comparison import _plot_points


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def save(self, key, name, value):
        path = self.root / "Data" / key / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def manifest(self, key, started="2026-10-10T01:00:00Z"):
        return {"status": "complete", "errors": [], "label": key.rsplit("/", 1)[-1], "started_utc": started,
                "telemetry": {"tx_end_of_burst_sent": True, "rx_stop_command_sent": True, "workers_stopped": True},
                "actual_sdr": {"rx_rate_actual": 250000., "tx_rate_actual": 250000., "channel": 0, "freq": 2.4e9, "tone": 10000.},
                "actual_hardware": {"tx_gain": 20., "rx_gain": 30., "tx_antenna": "TX/RX", "rx_antenna": "RX2", "master_clock_rate": 32e6},
                "requested_sdr": {"amp": .3}, "device_profile": {"serial": "PRIVATE-IDENTITY", "fpga_sha256": "PRIVATE-FPGA"},
                "current_source_hashes": {"logic.py": "algo-hash"}, "phase_source_hashes": {"sspp_lab/dsp.py": "dsp-hash"}}

    def sweep(self, key, values=((-40., -30.), (-42., -32.)), frequencies=(2.4e9, 2.5e9), tones=(2000., 3000.), started="2026-10-10T01:00:00Z"):
        manifest = self.manifest(key, started)
        manifest.update(sweep={"nfft": 2048, "verify_cycles": 20, "measure_cycles": 80, "verify_tolerance_hz": 200.,
                               "switch_settle_time": .1, "sweep_repeat": len(values), "num_points": len(frequencies)},
                        quality={"complete": True, "measurement_tone_failures": 0, "counts": [len(frequencies)]*len(values)}, raw_files=[])
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["repeat", "point", "requested_carrier_GHz", "expected_tone_Hz", "measured_tone_Hz", "peak_amplitude_dBFS_est"])
        for rep, amplitudes in enumerate(values, 1):
            for point, (frequency, tone, amplitude) in enumerate(zip(frequencies, tones, amplitudes), 1):
                writer.writerow([rep, point, frequency/1e9, tone, tone, amplitude])
                manifest["raw_files"].append({"point_number": (rep-1)*len(frequencies)+point,
                    "requested_carrier_hz": frequency, "tx_actual_carrier_hz": frequency,
                    "rx_actual_carrier_hz": frequency, "tone_hz": tone})
        self.save(key, "manifest.json", manifest)
        (self.root / "Data" / key / "measurements.csv").write_text(stream.getvalue())
        return manifest

    def phase(self, key, baseline_end=2., offset=0., count=30, finger=False):
        manifest = self.manifest(key)
        self.save(key, "manifest.json", manifest)
        if finger:
            self.save(key, "desktop_context.json", {"experiment": "finger_displacement"})
        self.save(key, "baseline.json", {"start_s": baseline_end-1, "end_s": baseline_end,
            "reference_amplitude": .01, "reference_phase_unwrapped_deg": 100.,
            "min_coherent_fraction": .95, "amplitude_sd_dB": .03,
            "phase_linear_drift_deg_per_s": .1, "phase_residual_sd_deg": .2})
        rows = [{"t_s": baseline_end+.1*i, "start_sample": i*25000, "count": 25000,
                 "relative_amplitude_dB": None if i == 0 else offset+i*.1,
                 "relative_phase_deg": None if i == 0 else offset+i,
                 "coherent_fraction": .95, "label": "transition_to_sample" if i == 2 else "sample"} for i in range(count)]
        self.write_rows(key, rows)
        return rows

    def write_rows(self, key, rows):
        (self.root / "Data" / key / "amplitude_phase.jsonl").write_text("\n".join(json.dumps(r) for r in rows)+"\n")

    def pair(self):
        self.sweep("runs/a")
        self.sweep("runs/b", ((-37., -25.), (-39., -27.)), started="2026-10-10T02:00:00Z")
        return ["runs/a", "runs/b"]

    def test_means_sample_sd_reference_sign_and_privacy(self):
        keys = self.pair()
        report = build_comparison(self.root, keys[::-1])
        self.assertEqual(report["reference_key"], keys[0])
        self.assertTrue(report["quantitative_difference_allowed"])
        rows = report["tables"]["sweep_difference"]["rows"]
        self.assertEqual([r["difference_dB"] for r in rows], [3., 5.])
        self.assertAlmostEqual(rows[0]["combined_repeat_sd_dB"], 2.)
        self.assertEqual(report["tables"]["sweep_summary"]["rows"][0]["mean_amplitude_dB"], -38.)
        self.assertAlmostEqual(report["tables"]["sweep_summary"]["rows"][0]["repeat_sd_dB"], 2**.5)
        self.assertNotIn("PRIVATE-", json.dumps(report))
        self.assertTrue(any("机械位置" in w for w in report["warnings"]))

    def test_explicit_reference_and_single_repeat(self):
        self.sweep("runs/a", ((-40., -30.),))
        self.sweep("runs/b", ((-37., -25.),))
        report = build_comparison(self.root, ["runs/a", "runs/b"], "runs/b")
        self.assertEqual([r["difference_dB"] for r in report["tables"]["sweep_difference"]["rows"]], [-3., -5.])
        self.assertTrue(all(r["repeat_sd_dB"] is None for r in report["tables"]["sweep_summary"]["rows"]))
        self.assertTrue(all(r["combined_repeat_sd_dB"] is None for r in report["tables"]["sweep_difference"]["rows"]))

    def test_parameters_mismatch_and_unknown_allow_only_overlay(self):
        keys = self.pair()
        path = self.root / "Data/runs/b/manifest.json"
        original = json.loads(path.read_text())
        for change in ("gain", "identity", "actual"):
            with self.subTest(change=change):
                m = json.loads(json.dumps(original))
                if change == "gain":
                    m["actual_hardware"]["rx_gain"] += 1
                elif change == "identity":
                    m["device_profile"].pop("serial")
                else:
                    m["raw_files"][0].pop("rx_actual_carrier_hz")
                self.save("runs/b", "manifest.json", m)
                report = build_comparison(self.root, keys)
                self.assertFalse(report["quantitative_difference_allowed"])
                self.assertEqual(len(report["charts"]), 2)
                self.assertEqual(report["tables"]["sweep_difference"]["rows"], [])

    def test_partial_grid_intersection_uses_actual_carrier_and_tone(self):
        self.sweep("runs/a")
        self.sweep("runs/b", frequencies=(2.5e9, 2.6e9), tones=(3000., 4000.))
        report = build_comparison(self.root, ["runs/a", "runs/b"])
        self.assertEqual(len(report["tables"]["sweep_difference"]["rows"]), 1)
        self.assertEqual(report["tables"]["sweep_difference"]["rows"][0]["actual_tx_carrier_hz"], 2.5e9)
        self.assertEqual(len(report["tables"]["sweep_summary"]["rows"]), 4)

    def test_equal_total_rf_does_not_match_different_tones(self):
        self.sweep("runs/a", ((-40.,),), frequencies=(2.4e9,), tones=(2000.,))
        self.sweep("runs/b", ((-40.,),), frequencies=(2.4e9-1000,), tones=(3000.,))
        report = build_comparison(self.root, ["runs/a", "runs/b"])
        self.assertFalse(report["quantitative_difference_allowed"])
        self.assertEqual(report["tables"]["sweep_difference"]["rows"], [])

    def test_sources_hash_exact_bytes(self):
        report = build_comparison(self.root, self.pair())
        for source in report["sources"]:
            for entry in source["files"]:
                content = (self.root / "Data" / source["key"] / entry["relative_path"]).read_bytes()
                self.assertEqual(entry["bytes"], len(content))
                self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())

    def test_selection_limits_duplicates_and_invalid_reference(self):
        for keys, reference in (([], None), (["runs/a"], None), (["runs/a"]*2, None),
                                ([f"runs/{i}" for i in range(9)], None), (["runs/a", "runs/b"], "runs/c"),
                                (["runs/a", "../b"], None)):
            with self.subTest(keys=keys, reference=reference), self.assertRaises(ValueError):
                build_comparison(self.root, keys, reference)

    def test_stop_errors_completion_gates(self):
        keys = self.pair()
        original = json.loads((self.root / "Data/runs/b/manifest.json").read_text())
        for status, errors, stop in (("failed", [], True), ("complete", ["overflow"], True), ("complete", [], False)):
            with self.subTest(status=status, errors=errors, stop=stop), self.assertRaises(ValueError):
                m = json.loads(json.dumps(original))
                m.update(status=status, errors=errors)
                m["telemetry"]["workers_stopped"] = stop
                self.save("runs/b", "manifest.json", m)
                build_comparison(self.root, keys)

    def test_invalid_sweep_rows_rejected(self):
        keys = self.pair()
        path = self.root / "Data/runs/b/measurements.csv"
        original = path.read_text()
        variants = [original+original.splitlines()[1]+"\n", "\n".join(original.splitlines()[:-1])+"\n",
                    original.replace("-37.0", "nan"), original.replace("2000.0,2000.0", "2000.0,2500.0", 1)]
        for changed in variants:
            with self.subTest(changed=changed[-70:]), self.assertRaises(ValueError):
                path.write_text(changed)
                build_comparison(self.root, keys)

    def test_actual_point_metadata_mismatch_rejected(self):
        keys = self.pair()
        m = json.loads((self.root / "Data/runs/b/manifest.json").read_text())
        m["raw_files"][0]["tone_hz"] = 9999
        self.save("runs/b", "manifest.json", m)
        with self.assertRaises(ValueError):
            build_comparison(self.root, keys)

    def test_symlink_source_rejected(self):
        keys = self.pair()
        path = self.root / "Data/runs/b/measurements.csv"
        content = path.read_bytes()
        path.unlink()
        outside = self.root / "foreign.csv"
        outside.write_bytes(content)
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            build_comparison(self.root, keys)

    def test_teacher_rejected_and_categories(self):
        self.save("teacher_runs/a", "feature_report.json", {"status": "complete"})
        with self.assertRaisesRegex(ValueError, "老师历史"):
            build_comparison(self.root, ["teacher_runs/a", "teacher_runs/b"])
        self.assertEqual(history_category({"key": "runs/a"}), "material_sweep")
        self.assertEqual(history_category({"key": "phase_runs/a"}), "phase_history")
        self.assertEqual(history_category({"key": "phase_runs/a"}, {"experiment": "finger_displacement"}), "finger_displacement")

    def test_phase_alignment_does_not_change_amplitude_or_phase(self):
        self.phase("phase_runs/a", baseline_end=2.)
        self.phase("phase_runs/b", baseline_end=7., offset=100.)
        report = build_comparison(self.root, ["phase_runs/a", "phase_runs/b"])
        self.assertEqual(report["kind"], "phase_history")
        self.assertFalse(report["quantitative_difference_allowed"])
        plot = report["charts"][1]
        self.assertAlmostEqual(plot["series"][0]["data"][0][0], .1)
        self.assertAlmostEqual(plot["series"][1]["data"][0][0], .1)
        self.assertEqual(plot["series"][0]["data"][0][1], 1.)
        self.assertEqual(plot["series"][1]["data"][0][1], 101.)
        self.assertEqual(report["metrics"][0]["transition_windows"], 1)
        self.assertEqual(len(report["tables"]["phase_curves"]["rows"]), 58)
        self.assertNotIn("estimated_distance_mm", json.dumps(report))

    def test_mixed_kinds_rejected(self):
        self.phase("phase_runs/a")
        self.phase("phase_runs/b", finger=True)
        with self.assertRaisesRegex(ValueError, "同一种"):
            build_comparison(self.root, ["phase_runs/a", "phase_runs/b"])
        self.sweep("runs/a")
        with self.assertRaisesRegex(ValueError, "同一种"):
            build_comparison(self.root, ["phase_runs/a", "runs/a"])

    def test_phase_gaps_missing_and_low_coherence_preserved(self):
        rows = self.phase("phase_runs/a")
        self.phase("phase_runs/b")
        rows[5]["relative_phase_deg"] = None
        rows[8]["coherent_fraction"] = .1
        del rows[12]
        self.write_rows("phase_runs/a", rows)
        report = build_comparison(self.root, ["phase_runs/a", "phase_runs/b"])
        self.assertEqual(report["metrics"][0]["sample_gaps"], 1)
        self.assertEqual(report["metrics"][0]["missing_relative_windows"], 1)
        self.assertEqual(report["metrics"][0]["low_coherence_windows"], 1)
        self.assertEqual(sum(y is None for _, y in report["charts"][1]["series"][0]["data"]), 2)

    def test_phase_invalid_baseline_and_corrupt_rows_rejected(self):
        self.phase("phase_runs/a")
        self.phase("phase_runs/b")
        baseline_path = self.root / "Data/phase_runs/a/baseline.json"
        baseline = json.loads(baseline_path.read_text())
        for field, value in (("amplitude_sd_dB", .6), ("min_coherent_fraction", .4), ("reference_amplitude", 0)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.save("phase_runs/a", "baseline.json", {**baseline, field: value})
                build_comparison(self.root, ["phase_runs/a", "phase_runs/b"])
        self.save("phase_runs/a", "baseline.json", baseline)
        path = self.root / "Data/phase_runs/a/amplitude_phase.jsonl"
        original = path.read_text()
        for changed in (original+"{broken}", original+original.splitlines()[-1]+"\n", original.replace('"relative_phase_deg": 1.0', '"relative_phase_deg": NaN', 1)):
            with self.subTest(changed=changed[-50:]), self.assertRaises(ValueError):
                path.write_text(changed)
                build_comparison(self.root, ["phase_runs/a", "phase_runs/b"])

    def test_bounded_display_keeps_spike_and_gap(self):
        data = [[float(i), 9999. if i == 10000 else (None if 13000 <= i < 14000 else .1)] for i in range(50000)]
        sampled = _plot_points(data)
        self.assertLessEqual(len(sampled), 2500)
        self.assertIn([10000., 9999.], sampled)
        self.assertTrue(any(y is None for _, y in sampled))
        for left, right in zip(sampled, sampled[1:]):
            if left[1] is not None and right[1] is not None:
                self.assertFalse(left[0] < 13000 and right[0] >= 14000)


if __name__ == "__main__":
    unittest.main()
