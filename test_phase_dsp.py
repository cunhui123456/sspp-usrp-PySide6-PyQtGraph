# 离线幅相测试：已知复数响应、跨 ±180° 解缠、频偏漂移、噪声拒绝和样点连续性。
# TX 测试使用虚拟 send()；导入 UHD 只是创建元数据/时间类型，不运行真实收发。
import threading
import unittest
import numpy as np
from phase_dsp import lockin, RelativeMonitor, baseline_problems, SampleContinuity
from run_phase import transmit


# 这些断言检验软件处理契约，不代表真实传感器已标定位移。
class PhaseTests(unittest.TestCase):
    # 非整数频率与非零起点检验全局相位原点，防止每帧重置参考。
    def test_known_complex_response_with_arbitrary_window_origin(self):
        fs,tone=250000.,10543.21
        expected=.013*np.exp(1j*.73)
        for start,size in [(0,5000),(137,4517),(8123982,4999)]:
            n=np.arange(start,start+size)
            x=expected*np.exp(2j*np.pi*tone*n/fs)
            actual,fraction=lockin(x,start,fs,tone)
            self.assertAlmostEqual(abs(actual),abs(expected),places=10)
            self.assertLess(abs(np.angle(actual/expected)),1e-9)
            self.assertGreater(fraction,.999999)

    # 幅值减半应为 -6.02 dB；170°→190° 应保留 +20° 而不是跳到 -340°。
    def test_amplitude_step_and_phase_wrap_are_retained(self):
        fs,tone=1000.,37.
        monitor=RelativeMonitor(fs,tone)
        for k in range(100):
            n=np.arange(k*100,(k+1)*100)
            amp,angle=(.1,170) if k<75 else (.05,190)
            x=amp*np.exp(1j*(2*np.pi*tone*n/fs+np.deg2rad(angle)))
            row=monitor.process(x,int(n[0]),'water' if k<75 else 'empty')
        self.assertEqual(baseline_problems(monitor.baseline),[])
        self.assertAlmostEqual(row['relative_amplitude_dB'],20*np.log10(.5),places=8)
        self.assertAlmostEqual(row['relative_phase_deg'],20.,places=7)

    # 0.02 Hz 频偏对应 7.2°/s 漂移；应报告并拒绝，而非自动扣掉。
    def test_frequency_offset_is_reported_not_removed(self):
        fs,tone=1000.,37.
        monitor=RelativeMonitor(fs,tone)
        for k in range(90):
            n=np.arange(k*100,(k+1)*100)
            x=.1*np.exp(2j*np.pi*(tone+.02)*n/fs)
            monitor.process(x,int(n[0]),'static')
        self.assertAlmostEqual(monitor.baseline['phase_linear_drift_deg_per_s'],7.2,places=7)
        self.assertTrue(any('drift' in x for x in baseline_problems(monitor.baseline)))
        self.assertFalse(monitor.baseline['drift_correction_applied'])

    # 固定随机种子的宽带噪声应仅有很小目标单音相干分量。
    def test_noise_is_not_accepted_as_a_phase_reference(self):
        rng=np.random.default_rng(318)
        x=rng.normal(size=5000)+1j*rng.normal(size=5000)
        _,fraction=lockin(x,0,250000.,10000.)
        self.assertLess(fraction,.01)

    # 故意跳过一个 tick，必须抛出连续性错误。
    def test_one_sample_loss_rejects_continuity(self):
        c=SampleContinuity()
        c.check(125000,5000);c.check(130000,1234)
        with self.assertRaises(RuntimeError): c.check(131235,5000)

    # 模拟首块部分发送，检查剩余样本补发、定时元数据只出现一次和最终 EOB。
    def test_partial_tx_send_does_not_reset_signal_or_schedule(self):
        from types import SimpleNamespace
        import uhd
        stop=threading.Event();calls=[];values=[]
        # 虚拟 TX 接口，只收集数组和元数据，不接触设备。
        class Stream:
            # 先收 1000 点再收剩余点，刻意覆盖 partial send 分支。
            def send(self,buffer,md,timeout):
                calls.append((md.start_of_burst,md.has_time_spec,md.end_of_burst))
                if md.end_of_burst: return len(buffer)
                count=1000 if not values else len(buffer)
                values.append(buffer[:count].copy())
                if len(values)==2: stop.set()
                return count
        telemetry={'tx_partial_sends':0};errors=[]
        cfg=SimpleNamespace(amp=.3,tone=10000.,tx_rate_actual=250000.)
        transmit(Stream(),cfg,uhd.types.TimeSpec(1.),stop,telemetry,errors)
        np.testing.assert_allclose(np.concatenate(values),.3*np.exp(2j*np.pi*10000*np.arange(10000)/250000),atol=2e-8)
        self.assertEqual(calls,[(True,True,False),(False,False,False),(False,False,True)])
        self.assertEqual(errors,[])
        self.assertTrue(telemetry['tx_end_of_burst_sent'])


if __name__=='__main__': unittest.main()
