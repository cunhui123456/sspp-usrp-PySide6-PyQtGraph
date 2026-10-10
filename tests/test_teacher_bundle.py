"""老师原 Excel 位于 Data/train；迁移后仍需安全映射并校验原哈希。"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_lab.controller import LabError


class TeacherBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = DesktopService(Settings(root=self.root))
        self.run = self.root / "Data/teacher_runs/history"
        self.run.mkdir(parents=True)
        self.excel = self.root / "Data/train/material/old.xlsx"
        self.excel.parent.mkdir(parents=True)
        self.excel.write_bytes(b"historical-xlsx-fixture")
        self.manifest = {"status": "complete", "sdr": {"rate": 250000}, "exports": [{
            "file": "/old/project/sspp-usrp/Data/train/material/old.xlsx",
            "sha256": hashlib.sha256(self.excel.read_bytes()).hexdigest()}]}
        self.target = self.root / "teacher.zip"
        self.save_manifest()

    def tearDown(self):
        self.temp.cleanup()

    def save_manifest(self):
        (self.run / "feature_report.json").write_text(json.dumps(self.manifest))

    def test_old_absolute_path_maps_current_copy_and_preserves_excel_hash(self):
        with patch("sspp_desktop.bundle.subprocess.run") as launch:
            self.service.export_bundle("teacher_runs/history", str(self.target))
            launch.assert_not_called()
        with zipfile.ZipFile(self.target) as archive:
            name = "results/exports/material/old.xlsx"
            self.assertEqual(archive.read(name), self.excel.read_bytes())
            parameters = json.loads(archive.read("parameters.json"))
            self.assertEqual(parameters["requested_sdr"]["rate"], 250000)
            index = json.loads(archive.read("bundle_manifest.json"))
            record = next(f for f in index["files"] if f["path"] == name)
            self.assertEqual(record["sha256"], self.manifest["exports"][0]["sha256"])
        self.assertFalse(self.service.exporting)

    def test_changed_or_missing_excel_preserves_existing_destination(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.target.write_bytes(b"keep-previous")
                if missing:
                    self.excel.unlink()
                else:
                    self.excel.write_bytes(b"changed")
                with self.assertRaises(LabError):
                    self.service.export_bundle("teacher_runs/history", str(self.target))
                self.assertEqual(self.target.read_bytes(), b"keep-previous")
                self.assertFalse(self.service.exporting)
                self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_traversal_wrong_root_missing_hash_and_symlink_rejected(self):
        for file in ("/old/Data/train/../outside.xlsx", "/old/private/data.xlsx", "/old/Data/train/Data/train/old.xlsx"):
            with self.subTest(file=file):
                self.manifest["exports"][0]["file"] = file
                self.save_manifest()
                with self.assertRaises(LabError):
                    self.service.export_bundle("teacher_runs/history", str(self.target))
        self.manifest["exports"][0]["file"] = "/old/Data/train/material/old.xlsx"
        self.manifest["exports"][0]["sha256"] = "not-a-hash"
        self.save_manifest()
        with self.assertRaises(LabError):
            self.service.export_bundle("teacher_runs/history", str(self.target))
        content = self.excel.read_bytes()
        external = self.root / "outside.xlsx"
        external.write_bytes(content)
        self.excel.unlink()
        self.excel.symlink_to(external)
        self.manifest["exports"][0]["sha256"] = hashlib.sha256(content).hexdigest()
        self.save_manifest()
        with self.assertRaises(LabError):
            self.service.export_bundle("teacher_runs/history", str(self.target))

    def test_complete_missing_exports_fails_but_incomplete_has_explicit_warning(self):
        self.manifest["exports"] = []
        self.save_manifest()
        with self.assertRaises(LabError):
            self.service.export_bundle("teacher_runs/history", str(self.target))
        self.manifest["status"] = "failed_or_incomplete"
        self.save_manifest()
        result = self.service.export_bundle("teacher_runs/history", str(self.target))
        self.assertTrue(any("没有记录 Excel" in w for w in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
