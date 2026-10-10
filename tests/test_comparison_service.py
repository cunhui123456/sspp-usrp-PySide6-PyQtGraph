"""比较导出的内容一致性、并发与原文件保护，不使用硬件。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from sspp_desktop.comparison_service import result_category
from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_lab.controller import LabError


class ComparisonServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = DesktopService(Settings(root=self.root))
        self.keys = ['runs/a', 'runs/b']
        sources = []
        for key in self.keys:
            folder = self.root / 'Data' / key
            folder.mkdir(parents=True)
            data = b'frequency,value\n2.4,-50\n'
            (folder / 'measurements.csv').write_bytes(data)
            sources.append({'key': key, 'label': key, 'parameters': {'rate': 250000}, 'files': [
                {'relative_path': 'measurements.csv', 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}]})
        self.base = {'schema_version': 1, 'kind': 'material_sweep', 'reference_key': self.keys[0],
                     'sources': sources, 'warnings': ['仅为数字幅度描述'], 'compatibility': [],
                     'metrics': [{'key': self.keys[0], 'sd': None}, {'key': self.keys[1], 'sd': .2}],
                     'tables': {'points': {'columns': ['frequency', 'amplitude'], 'rows': [{'frequency': 2.4, 'amplitude': -50}]}},
                     'charts': [{'id': 'sweep', 'series': []}]}
        self.builder = patch('sspp_experiments.comparison.build_comparison', side_effect=lambda *a: deepcopy(self.base))
        self.builder.start()
        self.figures = patch('sspp_desktop.comparison_service.subprocess.run', side_effect=self.fake_figures)
        self.figures.start()
        self.target = self.root / 'comparison.zip'

    def fake_figures(self, args, **kwargs):
        directory = Path(args[-1])
        (directory / 'comparison_01_sweep.png').write_bytes(b'png-for-archive-unit-test')
        (directory / 'comparison_report.pdf').write_bytes(b'pdf-for-archive-unit-test')
        return SimpleNamespace(returncode=0, stderr='')

    def tearDown(self):
        self.builder.stop()
        self.figures.stop()
        self.temp.cleanup()

    def test_archive_contains_reference_settings_tables_figures_sources_and_hashes(self):
        report = self.service.compare_runs(self.keys)
        result = self.service.export_comparison(report, str(self.target))
        with zipfile.ZipFile(self.target) as archive:
            index = json.loads(archive.read('bundle_manifest.json'))
            self.assertEqual(index['reference_key'], self.keys[0])
            self.assertEqual(index['source_keys'], self.keys)
            self.assertFalse(index['raw_iq_included'])
            for item in index['files']:
                data = archive.read(item['path'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), item['sha256'])
                self.assertEqual(len(data), item['bytes'])
            self.assertIn('sources/runs/b/measurements.csv', archive.namelist())
            self.assertIn('figures/comparison_report.pdf', archive.namelist())
            self.assertIn('tables/points.csv', archive.namelist())
            self.assertEqual(json.loads(archive.read('comparison.json')), report)
        self.assertEqual(result['sha256'], hashlib.sha256(self.target.read_bytes()).hexdigest())
        self.assertFalse(self.service.exporting)

    def test_reference_or_report_edit_cannot_silently_change_export(self):
        report = self.service.compare_runs(self.keys)
        report['reference_key'] = self.keys[1]
        with self.assertRaisesRegex(LabError, '报告已更改'):
            self.service.export_comparison(report, str(self.target))
        self.assertFalse(self.target.exists())
        self.assertFalse(self.service.exporting)

    def test_changed_source_preserves_existing_destination(self):
        report = self.service.compare_runs(self.keys)
        self.target.write_bytes(b'previous-archive')
        (self.root / 'Data/runs/a/measurements.csv').write_bytes(b'changed')
        with self.assertRaisesRegex(LabError, '比较后已改变'):
            self.service.export_comparison(report, str(self.target))
        self.assertEqual(self.target.read_bytes(), b'previous-archive')

    def test_plot_failure_preserves_existing_destination(self):
        report = self.service.compare_runs(self.keys)
        self.target.write_bytes(b'previous-archive')
        with patch('sspp_desktop.comparison_service.subprocess.run', return_value=SimpleNamespace(returncode=1, stderr='plot failed')):
            with self.assertRaisesRegex(LabError, '图表生成失败'):
                self.service.export_comparison(report, str(self.target))
        self.assertEqual(self.target.read_bytes(), b'previous-archive')
        self.assertFalse(self.service.exporting)
        self.assertEqual(list(self.root.glob('.*.tmp')), [])

    def test_changed_plot_code_requires_fresh_comparison(self):
        code = self.root / 'sspp_desktop/comparison_export.py'
        code.parent.mkdir()
        code.write_text('version_a')
        report = self.service.compare_runs(self.keys)
        code.write_text('version_b')
        self.target.write_bytes(b'previous-archive')
        with self.assertRaisesRegex(LabError, '绘图代码在比较后已改变'):
            self.service.export_comparison(report, str(self.target))
        self.assertEqual(self.target.read_bytes(), b'previous-archive')

    def test_running_or_saving_prevents_compare_and_export(self):
        report = self.service.compare_runs(self.keys)
        for status in ('starting', 'running', 'stopping', 'saving'):
            self.service.job = {'status': status}
            with self.subTest(status=status):
                with self.assertRaises(LabError):
                    self.service.compare_runs(self.keys)
                with self.assertRaises(LabError):
                    self.service.export_comparison(report, str(self.target))
        self.assertFalse(self.service.exporting)

    def test_data_directory_and_source_symlink_are_rejected(self):
        report = self.service.compare_runs(self.keys)
        with self.assertRaisesRegex(LabError, 'Data'):
            self.service.export_comparison(report, str(self.root / 'Data/comparison.zip'))
        source = self.root / 'Data/runs/a/measurements.csv'
        source.unlink()
        source.symlink_to(self.root / 'Data/runs/b/measurements.csv')
        with self.assertRaisesRegex(LabError, '路径已变化'):
            self.service.export_comparison(report, str(self.target))

    def test_history_categories_keep_legacy_phase_distinct(self):
        self.assertEqual(result_category({'mode': 'sweep'}, {}), 'material_sweep')
        self.assertEqual(result_category({'mode': 'teacher'}, {}), 'teacher_sweep')
        self.assertEqual(result_category({'mode': 'phase'}, {}), 'phase_history')
        self.assertEqual(result_category({'mode': 'phase'}, {'experiment': 'finger_displacement'}), 'finger_displacement')


if __name__ == '__main__':
    unittest.main()
