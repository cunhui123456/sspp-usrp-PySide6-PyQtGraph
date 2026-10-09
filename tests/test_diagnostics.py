"""只读图形契约测试；合成数据只用于数值回归，不作为真实实验验收。"""
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from sspp_lab.diagnostics import build_diagnostics,sampled
from sspp_lab.controller import LabController,LabError

class DiagnosticTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.run=self.root/'Data/phase_runs/example';self.run.mkdir(parents=True)
    def tearDown(self):self.temp.cleanup()
    def raw(self,iq):
        iq.astype(np.complex64).tofile(self.run/'rx_iq.cf32')
        (self.run/'manifest.json').write_text(json.dumps({'status':'complete','actual_sdr':{'freq':2.4e9,'rx_rate_actual':250000.,'tone':10000.,'amp':.3},'telemetry':{}}))
    def test_known_received_tone_and_tx_reference_are_distinct(self):
        n=np.arange(8192);self.raw(.1*np.exp(2j*np.pi*10000*n/250000))
        model=build_diagnostics(self.run,'phase');self.assertAlmostEqual(model['sample']['window_rms_dBFS_est'],-20,places=4)
        self.assertLess(model['sample']['peak_error_hz'],model['sample']['fft_bin_hz'])
        plots={p['id']:p for p in model['groups']['diagnostic']}
        self.assertEqual(plots['tx_reference']['source_type'],'computed_setting')
        self.assertIn('实际TX',plots['tx_reference']['note']);self.assertEqual(len(plots),5)
    def test_nonfinite_iq_not_shown_as_a_good_signal(self):
        iq=np.ones(8192,np.complex64);iq[-1]=np.nan;self.raw(iq)
        model=build_diagnostics(self.run,'phase');self.assertFalse(model['sample']['available']);self.assertEqual(model['groups']['diagnostic'],[])
    def test_empty_record_has_no_fabricated_samples_or_stop_evidence(self):
        model=build_diagnostics(self.run,'phase');self.assertEqual(model['chart_count'],0);self.assertFalse(model['sample']['available'])
        values={p['name']:p['value'] for p in model['facts']};self.assertEqual(values['TX EOB'],'未确认/无记录')
    def test_extrema_decimation_preserves_isolated_gap_spike(self):
        x=np.arange(10000);y=np.zeros(10000);y[1234]=500;y[8765]=-7
        points=sampled(x,y,300);self.assertIn([1234.,500.],points);self.assertIn([8765.,-7.],points)
        self.assertEqual(points[0][0],0);self.assertEqual(points[-1][0],9999)
    def test_missing_repeat_is_not_zero_and_single_repeat_sd_unknown(self):
        run=self.root/'Data/runs/sweep';run.mkdir(parents=True)
        (run/'measurements.csv').write_text('repeat,point,requested_carrier_GHz,peak_amplitude_dBFS_est\n1,1,2.4,-20\n1,2,3,-30\n2,1,2.4,-22\n')
        model=build_diagnostics(run,'sweep');plots={p['id']:p for p in model['groups']['sweep']}
        self.assertIsNone(plots['sweep_heatmap']['z'][1][1]);mean=plots['sweep_mean']['series'][0]
        self.assertAlmostEqual(mean['data'][0][1],-21);self.assertIsNone(mean['errors'][1])
    def test_phase_stages_contact_and_hardware_gap_are_retained(self):
        baseline={'start_s':1.,'end_s':2.,'reference_amplitude':.1,'reference_phase_unwrapped_deg':0.,'amplitude_sd_dB':0.}
        (self.run/'baseline.json').write_text(json.dumps(baseline))
        rows=[]
        for i,t in enumerate(np.arange(0,5,.1)):
            label='initial' if t<2.5 else 'transition_to_near' if t<3 else 'near'
            rows.append({'t_s':float(t),'amplitude_dBFS_est':-20,'phase_unwrapped_deg':2*t,'i':.1,'q':0,'label':label,'coherent_fraction':.99,'phase_step_deg':.2})
        (self.run/'amplitude_phase.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
        (self.run/'web_context.json').write_text(json.dumps({'manual_events':[{'event':'WEB_MARK_REQUEST','label':'near','contact_reported':True}]}))
        (self.run/'manifest.json').write_text(json.dumps({'actual_sdr':{'rx_rate_actual':250000},'telemetry':{}}))
        (self.run/'rx_frames.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in [
            {'start_sample':0,'count':5000,'device_tick_at_rx_rate':10},{'start_sample':5000,'count':5000,'device_tick_at_rx_rate':5011}]))
        model=build_diagnostics(self.run,'phase');self.assertTrue(model['segments'][-1]['contact'])
        plots={p['id']:p for p in model['groups']['quality']};self.assertEqual(plots['hardware_gaps']['series'][0]['data'][0][1],1)
        self.assertIn('舍入',plots['hardware_gaps']['note'])
        stage=next(p for p in model['groups']['phase'] if p['id']=='stage_phase');self.assertNotIn('transition_to_near',stage['xticks'])

    def test_hardware_clock_gaps_override_absolute_sample_rounding(self):
        (self.run/'manifest.json').write_text(json.dumps({'actual_sdr':{'rx_rate_actual':250000},'telemetry':{}}))
        row={'t_s':.01,'amplitude_dBFS_est':-20,'phase_unwrapped_deg':0,'i':.1,'q':0,'label':'initial'}
        (self.run/'amplitude_phase.jsonl').write_text(json.dumps(row)+'\n')
        for offset in (0,128,-128):
            with self.subTest(offset=offset):
                frames=[{'start_sample':0,'count':5000,'device_tick_at_rx_rate':681082,
                         'device_tick_at_master_rate':87178432,'master_clock_rate_hz':32000000,'ticks_per_sample':128},
                        {'start_sample':5000,'count':5000,'device_tick_at_rx_rate':686081,
                         'device_tick_at_master_rate':87818432+offset,'master_clock_rate_hz':32000000,'ticks_per_sample':128}]
                (self.run/'rx_frames.jsonl').write_text(''.join(json.dumps(f)+'\n' for f in frames))
                model=build_diagnostics(self.run,'phase')
                plot=next(p for p in model['groups']['quality'] if p['id']=='hardware_gaps')
                self.assertEqual(plot['series'][0]['data'][0][1],offset/128)
                self.assertIn('主时钟',plot['note'])
    def test_diagnostic_api_does_not_leak_serial_or_follow_escape(self):
        (self.run/'manifest.json').write_text(json.dumps({'device_profile':{'serial':'PRIVATE_SERIAL'}}))
        lab=LabController(self.root);self.assertNotIn('PRIVATE_SERIAL',json.dumps(lab.diagnostic_view('phase_runs/example')))
        with self.assertRaises(LabError):lab.diagnostic_view('phase_runs/../secret')
    def test_export_during_capture_rejected(self):
        class Proc:
            def poll(self):return None
        lab=LabController(self.root);lab.proc=Proc()
        with self.assertRaises(LabError):lab.export_diagnostics('phase_runs/example')

    def test_partial_csv_and_nonfinite_processed_rows_ignored(self):
        run=self.root/'Data/runs/sweep';run.mkdir(parents=True)
        (run/'measurements.csv').write_text('repeat,point,requested_carrier_GHz,peak_amplitude_dBFS_est\n1,1,2.4,-20\n1,2,3,')
        model=build_diagnostics(run,'sweep');self.assertEqual(model['facts'][0]['value'],1)
        (self.run/'amplitude_phase.jsonl').write_text(json.dumps({'t_s':1,'amplitude_dBFS_est':float('nan'),'phase_unwrapped_deg':0,'i':0,'q':0,'label':'bad'})+'\n')
        self.assertEqual(build_diagnostics(self.run,'phase')['groups']['phase'],[])

    def test_export_blocks_concurrent_export_and_rf_start(self):
        lab=LabController(self.root);lab.exporting=True
        with self.assertRaises(LabError):lab.export_diagnostics('phase_runs/example')
        with self.assertRaises(LabError):lab.start({})

if __name__=='__main__':unittest.main()
