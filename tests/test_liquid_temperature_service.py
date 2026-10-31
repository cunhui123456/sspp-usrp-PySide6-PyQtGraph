"""液体温度服务：合成记录、来源不变、温度定量门槛和版本化导出。"""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_experiments.liquid_temperature.metadata import validate_metadata
from sspp_lab.controller import LabError, validate_request
import test_comparison_domain as comparison_fixtures
import test_liquid_temperature_domain as phase_fixtures


def metadata(temperature=20., **changes):
    return {"liquid": "water", "sample_id": "sample_1", "container": "glass_1",
            "geometry": "fixed_jig", "thermometer": "reference_1", "repeat_id": "cycle_1",
            "target_temperature_c": temperature, "temperature_c": temperature,
            "volume_ml": 20., "distance_mm": 10., **changes}


class LiquidTemperatureServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = DesktopService(Settings(root=self.root))
        self.fixture = comparison_fixtures.ComparisonTests()
        self.fixture.root = self.root
        self.keys = ["runs/cool", "runs/warm"]
        self.fixture.sweep(self.keys[0])
        self.fixture.sweep(self.keys[1], ((-37., -25.), (-39., -27.)),
                           started="2026-10-27T02:00:00Z")
        for key, temp in zip(self.keys, (20., 30.)):
            self.context(key, metadata(temp))
        self.figures = patch("sspp_desktop.liquid_temperature_service.subprocess.run",
                             side_effect=self.fake_figures)
        self.figures.start()
        self.addCleanup(self.figures.stop)

    def context(self, key, info):
        mode = "sweep" if key.startswith("runs/") else "phase"
        self.fixture.save(key, "desktop_context.json", {
            "experiment": "liquid_temperature_" + mode, "desktop_status": "complete",
            "sensor": "same_sensor", "parameters": {"liquid_temperature": info}})

    @staticmethod
    def fake_figures(args, **kwargs):
        folder = Path(args[-1])
        (folder / "comparison_01.png").write_bytes(b"synthetic-test-image")
        (folder / "comparison_report.pdf").write_bytes(b"synthetic-test-report")
        return SimpleNamespace(returncode=0, stderr="")

    def finish(self, temperatures=(20., 30.)):
        for key, value in zip(self.keys, temperatures):
            self.service.liquid_save_annotations(key, [], value)

    def sources(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for key in self.keys for p in (self.root / "Data" / key).iterdir() if p.is_file()}

    def phase_record(self, key, slope=2., offset=0., temperature=20., **conditions):
        rows, annotations, baseline = phase_fixtures.make_case()
        annotations = [a for a in annotations if a["block"] == "cool"]
        for row in rows:
            row["relative_phase_deg"] = offset + row["relative_phase_deg"]*slope/2.
        for annotation in annotations:
            annotation.update(temperature_c=temperature, temperature_end_c=temperature)
        self.fixture.save(key, "manifest.json", self.fixture.manifest(key))
        self.fixture.save(key, "baseline.json", baseline)
        self.fixture.write_rows(key, rows)
        self.context(key, metadata(temperature, **conditions))
        self.service.liquid_save_annotations(key, annotations, temperature)
        return annotations

    def test_metadata_requires_reference_temperature_and_finite_physical_values(self):
        original = metadata()
        normalized = validate_metadata(original)
        self.assertEqual(original, metadata())
        self.assertEqual(normalized["temperature_source"], "operator_reference_thermometer")
        self.assertEqual(validate_request({"liquid_temperature": original}, False)["liquid_temperature"], normalized)
        for field, invalid in (("temperature_c", None), ("temperature_c", float("nan")),
                               ("temperature_c", True), ("temperature_c", -273.15),
                               ("target_temperature_c", float("inf")), ("volume_ml", 0),
                               ("distance_mm", -1), ("liquid", " "), ("thermometer", "")):
            with self.subTest(field=field, invalid=invalid):
                with self.assertRaises((ValueError, LabError)):
                    validate_request({"liquid_temperature": metadata(**{field: invalid})}, False)
        self.assertIsNone(validate_request({}, False)["liquid_temperature"])

    def test_sweep_temperature_coefficients_use_actual_change_not_target(self):
        self.context(self.keys[1], metadata(30., target_temperature_c=40.))
        before = self.sources()
        self.finish()
        report = self.service.liquid_compare_sweeps(self.keys)
        self.assertTrue(report["temperature_quantitative_allowed"])
        self.assertEqual(report["status"], "ready")
        rows = report["tables"]["temperature_coefficients"]
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["delta_temperature_c"] for r in rows], [10., 10.])
        self.assertEqual([r["amplitude_change_dB_per_c"] for r in rows], [.3, .5])
        for name, content in before.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        self.assertNotIn("PRIVATE-IDENTITY", json.dumps(report))

    def test_missing_end_temperature_restricts_to_overlay(self):
        self.service.liquid_save_annotations(self.keys[0], [], 20.)
        report = self.service.liquid_compare_sweeps(self.keys)
        self.assertFalse(report["temperature_quantitative_allowed"])
        self.assertFalse(report["quantitative_difference_allowed"])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["tables"]["temperature_coefficients"], [])
        self.assertNotIn("sweep_difference", report["tables"])
        self.assertFalse(any(c["id"] == "sweep_difference" for c in report["charts"]))
        self.assertTrue(all(m["mean_difference_dB"] is None for m in report["metrics"]))

    def test_unstable_temperature_and_geometry_mismatch_disable_differences(self):
        for changes, ends in (({}, (20., 31.)), ({"geometry": "moved_jig"}, (20., 30.)),
                              ({"volume_ml": 21.}, (20., 30.)), ({"distance_mm": 11.}, (20., 30.))):
            with self.subTest(changes=changes, ends=ends):
                self.context(self.keys[1], metadata(30., **changes))
                self.finish(ends)
                report = self.service.liquid_compare_sweeps(self.keys)
                self.assertFalse(report["temperature_quantitative_allowed"])
                self.assertEqual(report["tables"]["temperature_coefficients"], [])
                self.assertNotIn("sweep_difference", report["tables"])

    def test_temperature_coefficients_reverse_with_explicit_reference(self):
        self.finish()
        report = self.service.liquid_compare_sweeps(self.keys[::-1], self.keys[1])
        rows = report["tables"]["temperature_coefficients"]
        self.assertEqual([r["delta_temperature_c"] for r in rows], [-10., -10.])
        self.assertEqual([r["difference_dB"] for r in rows], [-3., -5.])
        self.assertEqual([r["amplitude_change_dB_per_c"] for r in rows], [.3, .5])

    def test_identical_temperatures_do_not_divide_by_zero(self):
        self.context(self.keys[1], metadata(20.))
        self.finish((20., 20.))
        report = self.service.liquid_compare_sweeps(self.keys)
        self.assertEqual(report["tables"]["temperature_coefficients"], [])
        json.dumps(report, allow_nan=False)

    def test_annotation_and_report_versions_preserve_prior_content(self):
        first = self.service.liquid_save_annotations(self.keys[0], [], 20.)
        folder = self.root / "Data" / self.keys[0] / "liquid_temperature_annotations"
        original = {p.name: p.read_bytes() for p in folder.iterdir()}
        self.service.liquid_save_annotations(self.keys[0], [], 20.2)
        self.assertEqual(len(list(folder.glob("*.json"))), 2)
        for name, content in original.items():
            self.assertEqual((folder / name).read_bytes(), content)
        self.assertEqual(self.service.liquid_load(self.keys[0])["temperature_end_c"], 20.2)
        self.assertEqual(first["temperature_end_c"], 20.)
        self.service.liquid_save_annotations(self.keys[1], [], 30.)
        first_report = self.service.liquid_compare_sweeps(self.keys)
        saved = (Path(first_report["export_path"]) / "analysis.json").read_bytes()
        second_report = self.service.liquid_compare_sweeps(self.keys)
        self.assertNotEqual(first_report["export_path"], second_report["export_path"])
        self.assertEqual((Path(first_report["export_path"]) / "analysis.json").read_bytes(), saved)

    def test_export_contains_verified_report_files_without_raw_iq(self):
        self.finish()
        report = self.service.liquid_compare_sweeps(self.keys)
        target = self.root / "liquid_report.zip"
        result = self.service.liquid_export(report["export_path"], str(target))
        self.assertFalse(result["raw_iq_included"])
        with zipfile.ZipFile(target) as archive:
            manifest = json.loads(archive.read("report_manifest.json"))
            self.assertFalse(manifest["raw_iq_included"])
            self.assertIn("temperature_coefficients.csv", archive.namelist())
            self.assertIn("comparison_report.pdf", archive.namelist())
            for item in manifest["files"]:
                content = archive.read(item["path"])
                self.assertEqual(len(content), item["bytes"])
                self.assertEqual(hashlib.sha256(content).hexdigest(), item["sha256"])
            self.assertFalse(any(n.endswith((".cf32", ".npz")) for n in archive.namelist()))

    def test_changed_source_rejects_export_and_preserves_destination(self):
        self.finish()
        report = self.service.liquid_compare_sweeps(self.keys)
        target = self.root / "liquid_report.zip"
        target.write_bytes(b"previous-export")
        path = self.root / "Data" / self.keys[0] / "measurements.csv"
        path.write_bytes(path.read_bytes().replace(b"-40.0", b"-41.0"))
        with self.assertRaisesRegex(LabError, "来源.*改变"):
            self.service.liquid_export(report["export_path"], str(target))
        self.assertEqual(target.read_bytes(), b"previous-export")
        self.assertFalse(self.service.exporting)

    def test_changed_report_rejects_export(self):
        self.finish()
        report = self.service.liquid_compare_sweeps(self.keys)
        (Path(report["export_path"]) / "temperature_coefficients.csv").write_text("changed")
        with self.assertRaisesRegex(LabError, "报告文件已改变"):
            self.service.liquid_export(report["export_path"], str(self.root / "report.zip"))

    def test_live_run_rejects_analysis_annotations_and_export(self):
        self.finish()
        report = self.service.liquid_compare_sweeps(self.keys)
        for status in ("starting", "running", "stopping", "saving"):
            self.service.job = {"status": status}
            with self.subTest(status=status):
                with self.assertRaises(LabError):
                    self.service.liquid_compare_sweeps(self.keys)
                with self.assertRaises(LabError):
                    self.service.liquid_analyze_phase("phase_runs/none", [])
                with self.assertRaises(LabError):
                    self.service.liquid_save_annotations(self.keys[0], [], 20.)
                with self.assertRaises(LabError):
                    self.service.liquid_export(report["export_path"], str(self.root / "report.zip"))
        self.assertFalse(self.service.exporting)

    def test_legacy_record_cannot_be_assigned_guessed_temperature(self):
        self.fixture.save(self.keys[0], "desktop_context.json", {"experiment": "material_sweep"})
        with self.assertRaisesRegex(LabError, "旧实验不能补猜"):
            self.service.liquid_save_annotations(self.keys[0], [], 20.)

    def test_failed_capture_cannot_save_temperature_annotations(self):
        path = self.root / "Data" / self.keys[0] / "manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["telemetry"]["workers_stopped"] = False
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(LabError, "停止证据"):
            self.service.liquid_save_annotations(self.keys[0], [], 20.)

    def test_saved_annotations_bind_measurements_and_reject_changed_source(self):
        saved = self.service.liquid_save_annotations(self.keys[0], [], 20.)
        self.assertIn("measurements.csv", [entry["relative_path"] for entry in saved["source_files"]])
        path = self.root / "Data" / self.keys[0] / "measurements.csv"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(LabError, "来源已改变"):
            self.service.liquid_load(self.keys[0])
        with self.assertRaisesRegex(LabError, "来源已改变"):
            self.service.liquid_compare_sweeps(self.keys)

    def test_phase_annotations_bind_series_and_baseline(self):
        key = "phase_runs/reference"
        self.phase_record(key)
        folder = self.root / "Data" / key / "liquid_temperature_annotations"
        saved = json.loads(next(folder.glob("*.json")).read_bytes())
        names = {entry["relative_path"] for entry in saved["source_files"]}
        self.assertTrue({"amplitude_phase.jsonl", "baseline.json"}.issubset(names))
        path = self.root / "Data" / key / "baseline.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(LabError, "来源已改变"):
            self.service.liquid_load(key)

    def test_source_changes_between_comparison_and_temperature_read_are_rejected(self):
        from sspp_experiments.comparison import build_comparison

        def changed_after_compute(*args, **kwargs):
            result = build_comparison(*args, **kwargs)
            path = self.root / "Data" / self.keys[0] / "manifest.json"
            manifest = json.loads(path.read_bytes())
            manifest["started_utc"] = "2026-10-27T15:00:00Z"
            path.write_text(json.dumps(manifest))
            return result

        with patch("sspp_desktop.liquid_temperature_service.build_comparison", side_effect=changed_after_compute):
            with self.assertRaisesRegex(LabError, "读取过程中来源已改变"):
                self.service.liquid_compare_sweeps(self.keys)

    def test_cross_run_phase_offsets_cancel_and_temperature_slopes_remain_distinct(self):
        keys = ["phase_runs/cool", "phase_runs/warm", "phase_runs/hot"]
        for key, slope, offset, temperature in zip(keys, (2., 2., 3.), (0., 271., -159.), (20., 30., 40.)):
            self.phase_record(key, slope, offset, temperature)
        before = {p: p.read_bytes() for key in keys for p in (self.root / "Data" / key).rglob("*") if p.is_file()}
        report = self.service.liquid_compare_phase_runs(keys)
        self.assertTrue(report["temperature_quantitative_allowed"], report["errors"])
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["thermal_drift"], [])
        self.assertEqual(report["scope"], "compare_within_run_relative_responses_only")
        slopes = [block["sensitivity_deg_per_mm"] for block in report["blocks"]]
        for actual, expected in zip(slopes, (2., 2., 3.)):
            self.assertAlmostEqual(actual, expected)
        curves = report["charts"][0]["series"]
        for (x1, y1), (x2, y2) in zip(curves[0]["data"], curves[1]["data"]):
            self.assertEqual(x1, x2)
            self.assertAlmostEqual(y1, y2)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_cross_run_phase_config_geometry_and_position_mismatch_block_quantitative_comparison(self):
        keys = ["phase_runs/cool", "phase_runs/warm"]
        for mutation in ("gain", "geometry", "positions"):
            with self.subTest(mutation=mutation):
                self.phase_record(keys[0], temperature=20.)
                annotations = self.phase_record(keys[1], temperature=30.)
                if mutation == "gain":
                    path = self.root / "Data" / keys[1] / "manifest.json"
                    manifest = json.loads(path.read_bytes())
                    manifest["actual_hardware"]["rx_gain"] += 1
                    path.write_text(json.dumps(manifest))
                elif mutation == "geometry":
                    self.context(keys[1], metadata(30., geometry="different_fixture"))
                else:
                    for annotation in annotations:
                        if annotation["distance_mm"] == 30.:
                            annotation["distance_mm"] = 35.
                # Each edit is intentionally followed by a new, correctly source-bound version.
                self.service.liquid_save_annotations(keys[1], annotations, 30.)
                report = self.service.liquid_compare_phase_runs(keys)
                self.assertFalse(report["temperature_quantitative_allowed"])
                self.assertEqual(report["status"], "partial")
                self.assertEqual(report["thermal_drift"], [])
                self.assertTrue(report["charts"])


if __name__ == "__main__":
    unittest.main()
