"""研究流程与打包导出应用层回归：合成数据/伪进程，不启动硬件。"""
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from sspp_desktop.config import Settings
from sspp_desktop.services import DesktopService
from sspp_lab.controller import DEFAULTS, LabError
from test_finger_domain import make_case


class ResearchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = DesktopService(Settings(root=self.root))
        self.run = self.root / 'Data/phase_runs/test'
        self.run.mkdir(parents=True)
        self.rows, self.annotations = make_case()
        (self.run / 'amplitude_phase.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in self.rows))
        self.baseline = {'end_s': .5, 'min_coherent_fraction': .99, 'amplitude_sd_dB': .01,
                         'phase_linear_drift_deg_per_s': .01, 'phase_residual_sd_deg': .01}
        (self.run / 'baseline.json').write_text(json.dumps(self.baseline))
        self.manifest = {'status': 'complete', 'errors': [], 'requested_sdr': {'freq': 2.4e9},
                         'telemetry': {'tx_end_of_burst_sent': True, 'rx_stop_sent': True, 'workers_stopped': True}}
        (self.run / 'manifest.json').write_text(json.dumps(self.manifest))
        self.service.job = {'id': 'job', 'status': 'running', 'config': dict(DEFAULTS, mode='phase'),
                            'output': 'Data/phase_runs/test', 'baseline': self.baseline, 'errors': []}
        self.service.proc = SimpleNamespace(poll=lambda: None, stdin=io.StringIO())

    def tearDown(self):
        self.temp.cleanup()

    def test_window_uses_child_ack_time_not_button_time_and_incomplete_not_kept(self):
        s = self.service
        before = s.finger_begin_window(20, hold_s=2, settle_s=2)
        self.assertIsNone(before['pending']['start_s'])
        marker = before['pending']['label']
        s._handle_event({'event': 'MARK_ACCEPTED', 'label': 'transition_to_'+marker, 't_s': 8.5}, s.job)
        self.assertEqual(s.finger_snapshot()['pending']['start_s'], 10.5)
        s._handle_event({'event': 'TRACE', 'seq': 1, 'points': [{'x': 13, 'phase': 70, 'amplitude': -.1, 'label': marker}]}, s.job)
        snapshot = s.finger_snapshot()
        self.assertIsNone(snapshot['pending'])
        self.assertEqual(snapshot['annotations'][0]['end_s'], 12.5)
        s.finger_begin_window(25)
        s._handle_event({'event': 'RF_STOPPED'}, s.job)
        self.assertIsNone(s.finger_snapshot()['pending'])
        self.assertEqual(len(s.finger_annotations), 1)

    def test_ruler_reference_settings_persist_before_any_analysis(self):
        settings = {"initial_distance_mm": 22., "tolerance_mm": 1., "workflow": "guided"}
        self.service.finger_begin_window(20, research_settings=settings)
        stored = json.loads((self.run / 'finger_annotations.json').read_text())
        self.assertEqual(stored['settings'], settings)
        self.assertEqual(stored['pending_not_used']['research_settings'], settings)
        self.assertEqual(self.service.finger_load('phase_runs/test')['settings'], settings)

    def test_action_is_not_calibration_truth_and_conflicting_actions_are_rejected(self):
        s = self.service
        s.finger_mark_action(10, 'near', '尺子约10mm')
        self.assertEqual(s.finger_annotations, [])
        marker = s.finger_actions[0]['label']
        s._handle_event({'event': 'MARK_ACCEPTED', 'label': marker, 't_s': 12}, s.job)
        self.assertEqual(s.finger_actions[0]['t_s'], 12)
        s.finger_begin_window(10)
        with self.assertRaises(LabError):
            s.finger_mark_action(20)
        s.finger_cancel_window()
        self.assertIsNone(s.finger_pending)

    def test_research_writes_derived_reports_without_modifying_originals(self):
        originals = {p.name: p.read_bytes() for p in self.run.iterdir()}
        report = self.service.finger_analyze(20, annotations=self.annotations)
        self.assertEqual(report['status'], 'ready', report['errors'])
        self.assertNotIn('results', report)  # bounded GUI snapshot, full rows remain on disk
        output = Path(report['export_path'])
        for filename in ('analysis.json', 'annotations.json', 'windows.csv', 'validation.csv', 'estimated_series.csv', 'research_report.png', 'research_report.pdf'):
            self.assertGreater((output / filename).stat().st_size, 10)
        with (output / 'validation.csv').open(encoding='utf-8-sig') as f:
            self.assertEqual(len(list(csv.DictReader(f))), 2)
        with (output / 'estimated_series.csv').open(encoding='utf-8-sig') as f:
            values = list(csv.DictReader(f))
        self.assertEqual(values[0]['distance_mm'], '')  # transition is never estimated
        for name, content in originals.items():
            self.assertEqual((self.run/name).read_bytes(), content)
        loaded = self.service.finger_load('phase_runs/test')
        self.assertEqual(loaded['annotations'], self.annotations)
        self.assertEqual(loaded['analysis']['status'], 'ready')

    def test_history_report_refuses_changed_source_prefix(self):
        self.service.finger_analyze(20, annotations=self.annotations)
        path = self.run / 'amplitude_phase.jsonl'
        changed = path.read_text().replace('0.98', '0.97', 1)
        path.write_text(changed)
        loaded = self.service.finger_load('phase_runs/test')
        self.assertEqual(loaded['analysis']['status'], 'rejected')
        self.assertFalse(loaded['analysis']['model']['active'])

    def test_live_estimate_stale_transition_cross_run_are_refused(self):
        s = self.service
        s.finger_analyze(20, annotations=self.annotations)
        point = {'x': 17, 'phase': 76, 'amplitude': -.1, 'coherent_fraction': .98, 'label': 'hold'}
        s._handle_event({'event': 'TRACE', 'seq': 1, 'points': [point]}, s.job)
        self.assertAlmostEqual(s.finger_snapshot()['estimate']['distance_mm'], 23)
        s.finger_last_trace_wall = time.monotonic() - 3
        self.assertFalse(s.finger_snapshot()['estimate']['ok'])
        point['label'] = 'transition_to_next'
        s._handle_event({'event': 'TRACE', 'seq': 2, 'points': [point]}, s.job)
        self.assertFalse(s.finger_snapshot()['estimate']['ok'])
        s.job['output'] = 'Data/phase_runs/another'
        point['label'] = 'hold'
        s._handle_event({'event': 'TRACE', 'seq': 3, 'points': [point]}, s.job)
        self.assertFalse(s.finger_snapshot()['estimate']['ok'])

    def test_failed_source_cannot_activate_even_if_curve_fits(self):
        self.service.proc = None
        self.service.job['status'] = 'failed'
        self.manifest['status'] = 'failed_or_incomplete'
        (self.run / 'manifest.json').write_text(json.dumps(self.manifest))
        result = self.service.finger_analyze(20, annotations=self.annotations)
        self.assertEqual(result['status'], 'rejected')
        self.assertFalse(result['model']['active'])

    def test_analysis_and_guided_window_are_mutually_exclusive(self):
        s = self.service
        s.finger_analysis_lock.acquire()
        try:
            with self.assertRaises(LabError):
                s.finger_begin_window(20)
            self.assertIsNone(s.finger_pending)
        finally:
            s.finger_analysis_lock.release()
        s.finger_begin_window(20)
        pending_id = s.finger_pending['id']
        with self.assertRaises(LabError):
            s.finger_analyze(20, annotations=self.annotations)
        self.assertFalse(s.finger_analysis_lock.locked())
        self.assertEqual(s.finger_pending['id'], pending_id)
        self.assertEqual(s.finger_annotations, [])


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = DesktopService(Settings(root=self.root))
        self.run = self.root/'Data/runs/run_a'
        self.run.mkdir(parents=True)
        (self.run/'manifest.json').write_text(json.dumps({'status': 'complete', 'sweep': {'num_points': 12}}))
        (self.run/'measurements.csv').write_text('point,value\n1,2\n')
        (self.run/'rx_iq.cf32').write_bytes(b'raw-data')
        (self.run/'iq_point_01.npz').write_bytes(b'raw-point')
        (self.run/'sweep_summary.png').write_bytes(b'image-stub')
        (self.run/'desktop_context.json').write_text(json.dumps({'parameters': {'label': 'water', 'rate': 250000}}))
        self.target = self.root/'experiment.zip'
        self.mock = patch('sspp_desktop.bundle.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='{}', stderr=''))
        self.mock.start()

    def tearDown(self):
        self.mock.stop()
        self.temp.cleanup()

    def test_package_contains_settings_results_figures_and_verified_hashes(self):
        result = self.service.export_bundle('runs/run_a', str(self.target))
        self.assertEqual(result['sha256'], hashlib.sha256(self.target.read_bytes()).hexdigest())
        with zipfile.ZipFile(self.target) as archive:
            names = archive.namelist()
            self.assertIn('parameters.json', names)
            self.assertIn('results/measurements.csv', names)
            self.assertIn('results/sweep_summary.png', names)
            self.assertNotIn('results/rx_iq.cf32', names)
            params = json.loads(archive.read('parameters.json'))
            self.assertEqual(params['sweep_config']['num_points'], 12)
            self.assertEqual(params['desktop_parameters']['label'], 'water')
            manifest = json.loads(archive.read('bundle_manifest.json'))
            for item in manifest['files']:
                self.assertEqual(hashlib.sha256(archive.read(item['path'])).hexdigest(), item['sha256'])
            self.assertEqual(len(manifest['raw_iq_excluded']), 2)
        self.assertFalse(self.service.exporting)

    def test_raw_opt_in_and_research_reports_are_packed(self):
        sub = self.run/'finger_research/v1'
        sub.mkdir(parents=True)
        (sub/'analysis.json').write_text('{"status":"ready"}')
        self.service.export_bundle('runs/run_a', str(self.target), include_raw=True)
        with zipfile.ZipFile(self.target) as archive:
            self.assertIn('results/rx_iq.cf32', archive.namelist())
            self.assertIn('results/iq_point_01.npz', archive.namelist())
            self.assertIn('results/finger_research/v1/analysis.json', archive.namelist())

    def test_generation_failure_preserves_existing_destination_and_unlocks(self):
        self.target.write_bytes(b'previous-package')
        with patch('sspp_desktop.bundle.subprocess.run', return_value=SimpleNamespace(returncode=1, stderr='failed')):
            with self.assertRaises(LabError):
                self.service.export_bundle('runs/run_a', str(self.target))
        self.assertEqual(self.target.read_bytes(), b'previous-package')
        self.assertFalse(self.service.exporting)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_failed_run_without_plottable_data_keeps_original_status_and_warnings(self):
        source = {'status': 'failed_or_incomplete', 'errors': ['RX failed before first frame']}
        (self.run/'manifest.json').write_text(json.dumps(source))
        (self.run/'console.log').write_text('RX failed before first frame\n')
        settings = {'initial_distance_mm': 20., 'tolerance_mm': 2., 'workflow': 'guided'}
        (self.run/'finger_annotations.json').write_text(json.dumps({'settings': settings}))
        with patch('sspp_desktop.bundle.subprocess.run', return_value=SimpleNamespace(returncode=1, stderr='No plottable data')):
            result = self.service.export_bundle('runs/run_a', str(self.target))
        self.assertTrue(any('仅打包实际存在' in item for item in result['warnings']))
        with zipfile.ZipFile(self.target) as archive:
            self.assertEqual(json.loads(archive.read('results/manifest.json')), source)
            self.assertIn('results/console.log', archive.namelist())
            self.assertEqual(json.loads(archive.read('bundle_manifest.json'))['source_status'], 'failed_or_incomplete')
            self.assertIn('No plottable data', archive.read('README.txt').decode('utf-8'))
            self.assertEqual(json.loads(archive.read('parameters.json'))['finger_research_settings'], settings)
        self.assertFalse(self.service.exporting)

    def test_active_busy_invalid_destination_and_symlink_files(self):
        self.service.job = {'status': 'saving'}
        with self.assertRaises(LabError):
            self.service.export_bundle('runs/run_a', str(self.target))
        self.service.job = None
        self.service.exporting = True
        with self.assertRaises(LabError):
            self.service.export_bundle('runs/run_a', str(self.target))
        self.service.exporting = False
        with self.assertRaises(LabError):
            self.service.export_bundle('runs/run_a', str(self.run/'nested.zip'))
        (self.run/'secret.json').symlink_to(self.run/'manifest.json')
        self.service.export_bundle('runs/run_a', str(self.target))
        with zipfile.ZipFile(self.target) as archive:
            self.assertNotIn('results/secret.json', archive.namelist())


if __name__ == '__main__':
    unittest.main()
