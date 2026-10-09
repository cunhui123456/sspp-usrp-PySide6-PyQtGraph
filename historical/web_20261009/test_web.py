"""网页控制契约回归；不初始化 B210，不将测试替身当作实验结果。"""
import io
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request,build_opener,ProxyHandler
from unittest.mock import patch
from sspp_lab.controller import LabController,LabError,DEFAULTS,validate_request
from sspp_lab.web_server import create_server
urlopen=build_opener(ProxyHandler({})).open

class Contracts(unittest.TestCase):
    def test_setup_range_and_nonfinite_rejected(self):
        with self.assertRaises(LabError):validate_request(DEFAULTS)
        for update in [{'amplitude':1},{'points':2.5},{'rate':float('nan')},{'unknown':True},
                       {'points':100,'tone_step':20000},{'freq_stop_ghz':2.4}]:
            with self.subTest(update=update),self.assertRaises(LabError):validate_request({**DEFAULTS,'setup_confirmed':True,**update})
    def test_carrier_range_boundaries_for_sweep_and_phase(self):
        # 接受完整跨度和两端点；任何越界在打开设备之前拒绝。
        cfg={**DEFAULTS,'setup_confirmed':True,'freq_start_ghz':.07,'freq_stop_ghz':6.}
        self.assertEqual(validate_request(cfg)['freq_stop_ghz'],6.)
        for carrier in (.07,6.):
            self.assertEqual(validate_request({**cfg,'mode':'phase','carrier_ghz':carrier})['carrier_ghz'],carrier)
        for update in ({'freq_start_ghz':.069999},{'freq_stop_ghz':6.000001},
                       {'mode':'phase','carrier_ghz':.069999},{'mode':'phase','carrier_ghz':6.000001}):
            with self.subTest(update=update),self.assertRaises(LabError):validate_request({**cfg,**update})
    def test_parameters_go_to_both_real_commands(self):
        lab=LabController('/tmp/example')
        cfg={**DEFAULTS,'setup_confirmed':True,'rate':500000,'amplitude':.2,'tx_gain':15,'rx_gain':25}
        for mode in ('phase','sweep'):
            cfg['mode']=mode
            cmd=lab.command(validate_request(cfg),Path('/tmp/device.json'))
            for flag,val in [('--rate','500000'),('--amplitude','0.2'),('--tx-gain','15'),('--rx-gain','25')]:self.assertEqual(cmd[cmd.index(flag)+1],val)
            self.assertIn('acquire',cmd)
    def test_busy_rejects_second_start_before_preflight(self):
        lab=LabController();lab.job={'status':'saving'}
        with patch.object(lab,'preflight') as prepare:
            with self.assertRaises(LabError):lab.start({**DEFAULTS,'setup_confirmed':True})
            prepare.assert_not_called()
    def test_mark_requires_baseline_and_running_state(self):
        class Process:
            stdin=io.StringIO()
            def poll(self):return None
        lab=LabController();lab.proc=Process()
        lab.job={'status':'running','config':{'mode':'phase'},'output':None}
        with self.assertRaises(LabError):lab.mark('near')
        lab.job['baseline']={'end_s':7};lab.mark('near',3,False)
        self.assertEqual(json.loads(lab.proc.stdin.getvalue()),{'mark':'near','settle':3})
        self.assertFalse(lab.events[0]['contact_reported'])
        lab.job['status']='stopping'
        with self.assertRaises(LabError):lab.mark('far')
    def test_stop_requests_control_without_claiming_stopped(self):
        class Process:
            stdin=io.StringIO()
            def poll(self):return None
            def wait(self,timeout):return 0
        lab=LabController();lab.proc=Process();lab.job={'status':'running','config':{'mode':'phase'},'rf_stopped':False}
        result=lab.stop()
        self.assertEqual(json.loads(lab.proc.stdin.getvalue()),{'stop':True})
        self.assertEqual(result['job']['status'],'stopping');self.assertFalse(result['job']['rf_stopped'])
    def test_history_redacts_profiles_and_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            lab=LabController(folder);out=Path(folder)/'Data/runs/example';out.mkdir(parents=True)
            (out/'manifest.json').write_text(json.dumps({'status':'complete','device_profile':{'serial':'PRIVATE'},'requested_sdr':{'device':'PRIVATE'}}))
            (out/'measurements.csv').write_text('repeat,point,requested_carrier_GHz,peak_amplitude_dBFS_est\n1,1,2.4,-52\n')
            (out/'leaked.csv').symlink_to(out/'manifest.json')
            detail=lab.details('runs/example')
            self.assertNotIn('PRIVATE',json.dumps(detail));self.assertNotIn('manifest.json',detail['files']);self.assertNotIn('leaked.csv',detail['files'])
            self.assertEqual(detail['curve']['points'][0]['carrier_hz'],2400000000)
            for key in ('runs/../example','/etc/passwd','phase_runs/no_such_run'):
                with self.assertRaises(LabError):lab.run_path(key)
    def test_missing_stop_evidence_never_becomes_complete(self):
        class Process:
            stdout=io.StringIO('')
            def wait(self):return 0
            def poll(self):return 0
        lab=LabController();job={'config':DEFAULTS,'output':None,'errors':[],'rf_stopped':False}
        lab._collect(Process(),job);self.assertEqual(job['status'],'stop_unconfirmed')
    def test_watchdog_bounds_cpp_initialization(self):
        class Process:
            def poll(self):return None
        lab=LabController();job={'config':DEFAULTS,'status':'starting','errors':[]}
        with patch('sspp_lab.controller.time.monotonic',side_effect=[0,31]),patch.object(lab,'_end_process') as end:
            lab._watchdog(Process(),job)
            end.assert_called_once()
        self.assertEqual(job['status'],'stopping');self.assertTrue(job['timeout_requested'])
    def test_initialization_exit_not_reported_as_real_capture(self):
        class Process:
            stdout=io.StringIO('')
            def wait(self):return -9
            def poll(self):return -9
        with tempfile.TemporaryDirectory() as folder:
            lab=LabController(folder);out=Path(folder)/'Data/runs/example';out.mkdir(parents=True)
            (out/'manifest.json').write_text('{"status":"initializing"}')
            job={'id':'test','config':DEFAULTS,'status':'stopping','output':'Data/runs/example','errors':[],'rf_stopped':False}
            lab._collect(Process(),job)
            self.assertEqual(job['status'],'initialization_failed');self.assertFalse(job['acquisition_started'])

class HTTPContracts(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.lab=LabController(self.temp.name);self.server=create_server(0,self.lab)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.token=re.search(r'name="sspp-token" content="([^"]+)"',urlopen(self.url).read().decode()).group(1)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def post(self,path,body,token=None,origin=None):
        headers={'Content-Type':'application/json'}
        if token:headers['X-SSPP-Token']=token
        if origin:headers['Origin']=origin
        return urlopen(Request(self.url+path,data=json.dumps(body).encode(),headers=headers),timeout=3)
    def test_local_only_and_token_protected(self):
        self.assertTrue(json.load(urlopen(self.url+'/api/bootstrap'))['real_hardware_only'])
        for token,origin in [(None,None),(self.token,'http://external.example')]:
            with self.assertRaises(HTTPError) as error:self.post('/api/start',{},token,origin)
            self.assertEqual(error.exception.code,403);error.exception.close()
        self.assertIsNone(self.lab.proc)
    def test_invalid_requests_do_not_launch_hardware(self):
        with self.assertRaises(HTTPError) as error:self.post('/api/start',DEFAULTS,self.token)
        self.assertEqual(error.exception.code,400);error.exception.close();self.assertIsNone(self.lab.proc)
        with self.assertRaises(HTTPError) as error:urlopen(self.url+'/api/run?key=runs/../secret')
        self.assertEqual(error.exception.code,404);error.exception.close()
    def test_rejects_host_rebinding(self):
        with self.assertRaises(HTTPError) as error:urlopen(Request(self.url+'/api/status',headers={'Host':'external.example'}))
        self.assertEqual(error.exception.code,403);error.exception.close()

if __name__=='__main__':unittest.main()
