#!/usr/bin/env python3
# 原版交互包装：通过替换 teacher 模块中的引用复用老师 main()，不改其执行语句。
# 本文件选择显式设备、记录图形/导出证据、添加运行时限，并在汇总图出现前停止 RF。
# 注意本入口直接进入实验流程，没有 plan 子命令；仅阅读/导入不会调用 main()。
# 2026-10-09 教师模块新增注释；原始基线仍可从 Git 95e6815 追溯。
"""Run the teacher's original main.py on this Mac, with explicit device setup.

Original modules are unchanged. A wrapper selects the reviewed device, records
GUI/export evidence, imposes a stream time limit, and stops RF before summary UI.
"""
from __future__ import annotations
import argparse
import builtins
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]

# 将终端输出同时写入本轮日志，以保留原界面反馈和错误证据。
class Tee:
    # 保留原终端和已打开的日志句柄，供 stdout/stderr 临时代理使用。
    def __init__(self, terminal, logfile): self.terminal,self.logfile=terminal,logfile
    # 两个目标均写入并及时刷新日志；返回写入字符数以兼容文件接口。
    def write(self, text):
        self.terminal.write(text);self.logfile.write(text);self.logfile.flush()
        return len(text)
    # 同时刷新两个流，避免关键停止消息滞留在缓冲区。
    def flush(self): self.terminal.flush();self.logfile.flush()
    # 透传终端属性，保持交互程序对 TTY 的判断。
    def isatty(self): return self.terminal.isatty()

# 建立包装对象、暂时替换 teacher 引用、调用原 main()，最后恢复引用与工作目录。
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--material',help='Omit to use the original interactive input prompt')
    p.add_argument('--max-seconds',type=float,default=90)
    p.add_argument('--summary-seconds',type=float,default=45,help='Auto-close summary window after RF is stopped')
    p.add_argument('--device-config',type=Path,default=ROOT/'local_device.json')
    opt=p.parse_args()
    if not 5<=opt.max_seconds<=300: p.error('max-seconds must be 5..300')
    if not 1<=opt.summary_seconds<=300: p.error('summary-seconds must be 1..300')
    os.environ.setdefault('MPLCONFIGDIR',str(ROOT/'.cache'/'matplotlib'))
    os.environ.setdefault('QT_API','pyqt5')
    import matplotlib
    matplotlib.use('QtAgg')
    import matplotlib.pyplot as plt
    import numpy as np
    import main as teacher
    import run_local as local
    opts=SimpleNamespace(device_config=opt.device_config,repeat=3,points=12,max_seconds=opt.max_seconds,
        point_timeout=5,tx_gain=20,rx_gain=30,freq_start=2.4e9,freq_stop=3.0e9,
        rate=250000.,amplitude=.3,tone_start=2000.,tone_step=1000.)
    device,config,sweep=local.make_config(opts)
    output=ROOT/'Data'/'teacher_runs'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output.mkdir(parents=True,exist_ok=False)
    report={'status':'initializing','started_utc':datetime.now(timezone.utc).isoformat(),
        'source_hashes':{name:local.sha(ROOT/name) for name in local.ORIGINALS},
        'wrapper_sha256':local.sha(__file__),'device_profile':device,'sdr':asdict(config),
        'entrypoint':'original main.main()','backend':matplotlib.get_backend(),
        'gui_updates':0,'plot_observations':[],'exports':[],'errors':[]}
    refs={'manager':None,'runner':None,'drawer':None,'result':None,'workers':[],'timer':None}
    # 持续保存 feature_report，异常时也保留当前运行证据。
    def write_report(): local.dump(output/'feature_report.json',report)
    # 统一请求停止、发送 RX stop、等待线程退出；避免等待自己的线程造成死锁。
    def stop_rf():
        runner=refs['runner']
        if runner is not None: runner.stop_event.set()
        if refs.get('stop') is not None: refs['stop'].set()
        manager=refs['manager']
        if manager is not None: manager.stop_rx_stream()
        for t in list(refs['workers']):
            if t is not threading.current_thread(): t.join(timeout=3)
        if refs['timer']: refs['timer'].cancel()
        report['workers_stopped']=all(not t.is_alive() for t in refs['workers'])
        report['rf_stop_requested']=runner is not None
    # 保存原引用以便 finally 恢复；替换只作用于本进程，不改磁盘算法。
    original={n:getattr(teacher,n) for n in ('SDRConfig','USRPManager','SweepRunner','OnTimeDraw',
        'tx_worker','rx_worker','save_sweep_summary_excel','plot_sweep_summary')}
    # 继承老师硬件类，记录实际参数和 RX stop 的调用证据。
    class Manager(original['USRPManager']):
        # 仍由父类打开配置设备；包装只额外记录回读参数。
        def configure(self):
            refs['manager']=self
            value=super().configure()
            report['actual_sdr']=asdict(self.args)
            return value
        # 执行父类停止命令后登记，不能仅凭布尔记录判断所有硬件状态。
        def stop_rx_stream(self):
            super().stop_rx_stream()
            if self.rx_streamer is not None: report['rx_stop_command_sent']=True
    # 执行原扫频控制器并保存未舍入的返回数组。
    class Runner(original['SweepRunner']):
        # 登记当前 runner 和扫频配置，供停止函数与报告复用。
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);refs['runner']=self
            report['sweep']=asdict(self.cfg)
        # 保留父类算法，返回结果前将未舍入数组写入本轮目录。
        def run(self,*args,**kwargs):
            result=super().run(*args,**kwargs)
            refs['result']=result
            local.dump(output/'sweep_result.json',result)
            return result
    # 保留老师绘图行为，附加图窗、RMS 和频谱证据记录。
    class Drawer(original['OnTimeDraw']):
        # 登记实时绘图对象，供汇总阶段保存并关闭。
        def __init__(self,*args,**kwargs): super().__init__(*args,**kwargs);refs['drawer']=self
        # 调用原窗口初始化，再补充标题和已创建标记。
        def setup_plot(self,*args,**kwargs):
            super().setup_plot(*args,**kwargs)
            self.fig.canvas.manager.set_window_title('SSPP - Teacher live time domain and spectrum')
            report['gui_window_created']=True
        # 记录实际显示帧；只有单音匹配时保留最后一帧 IQ，不记录整个数据流。
        def update_plots(self,samples):
            super().update_plots(samples)
            report['gui_updates']+=1
            f,spec=self.calc_spectrum(samples,self.args.rx_rate_actual)
            peak=float(f[np.argmax(spec)])
            rms=float(20*np.log10(np.sqrt(np.mean(np.abs(samples)**2))+1e-12))
            report['plot_observations'].append({'utc':datetime.now(timezone.utc).isoformat(),
                'carrier_hz':self.args.freq,'expected_tone_hz':self.args.tone,
                'spectrum_peak_hz':peak,'rms_dBFS':rms})
            if abs(peak-self.args.tone)<200:
                if 'live_first_image' not in report:
                    self.fig.savefig(output/'live_first.png',dpi=150)
                    report['live_first_image']='live_first.png'
                # Last displayed stable frame, saved after RF has stopped.
                refs['last_iq']=samples.copy()
                refs['last_iq_config']={'carrier_hz':self.args.freq,'tone_hz':self.args.tone,'rate':self.args.rx_rate_actual}
            write_report()
        # 关闭前保存最后显示状态，方便事后核查图形。
        def close(self):
            if self.fig is not None and self.is_open():
                self.fig.savefig(output/'live_last.png',dpi=150)
                report['live_last_image']='live_last.png'
            super().close()
    # 代理真实 streamer，记录原线程的部分发送与结束 EOB；不自动补发短块。
    class TxObserver:
        # 保存真实 streamer；后续 send() 透传给它。
        def __init__(self,stream):self.stream=stream
        # 原线程忽略部分发送返回值，包装将其记为错误，不隐藏该限制。
        def send(self,values,md,*args,**kwargs):
            n=self.stream.send(values,md,*args,**kwargs)
            if md.end_of_burst:report['tx_end_of_burst_sent']=True
            elif n<len(values):report['errors'].append('Original TX worker returned a partial send')
            return n
    # 包装原 TX/RX 线程，记录线程引用；RX 启动时建立最长运行计时器。
    def worker(kind,stream,*args):
        refs['workers'].append(threading.current_thread())
        # Both original worker argument lists put stop_event at args[1].
        stop=args[1]
        refs['stop']=stop
        if kind=='rx_worker':
            # 计时到期记错并请求共享 stop_event 停止。
            def timeout():
                report['errors'].append('Stream time limit reached');stop.set()
            timer=threading.Timer(opt.max_seconds,timeout);timer.daemon=True
            refs['timer']=timer;timer.start()
        try: original[kind](TxObserver(stream) if kind=='tx_worker' else stream,*args)
        except Exception as exc: report['errors'].append(f'{kind}: {exc}');stop.set()
    # 原格式导出前拒绝覆盖同名文件，再记录文件哈希。
    def save_excel(result,save_dir,filename):
        target=Path(save_dir)/filename
        if target.exists():raise FileExistsError(target)
        original['save_sweep_summary_excel'](result,save_dir,filename)
        report['exports'].append({'file':str(target.resolve()),'sha256':local.sha(target)})
    # 先停止 RF，再调用原汇总图；图窗查看时限与发射时限分开。
    def summary_plot(result):
        stop_rf()
        report['rf_stopped_before_summary']=report.get('workers_stopped',False)
        print('RF_STOPPED_BEFORE_SUMMARY',flush=True)
        drawer=refs['drawer']
        if drawer and drawer.fig is not None:
            drawer.fig.savefig(output/'live_last.png',dpi=150)
            report['live_last_image']='live_last.png'
            plt.close(drawer.fig)
        if 'last_iq' in refs:
            np.savez_compressed(output/'last_displayed_iq.npz',iq=refs['last_iq'],
                configuration=json.dumps(refs['last_iq_config']))
        saved_show=plt.show
        # 临时包装 plt.show 保存图形，并用 GUI 计时器自动关闭。
        def show_summary(*args,**kwargs):
            fig=plt.gcf();fig.canvas.manager.set_window_title('SSPP - Teacher sweep summary (RF stopped)')
            fig.savefig(output/'teacher_summary.png',dpi=150)
            report['summary_image']='teacher_summary.png';report['summary_window_created']=True
            write_report()
            timer=fig.canvas.new_timer(interval=int(opt.summary_seconds*1000))
            timer.single_shot=True;timer.add_callback(lambda:plt.close(fig));timer.start()
            print(f'SUMMARY_WINDOW_OPEN_FOR_{opt.summary_seconds:g}_SECONDS',flush=True)
            try:return saved_show(*args,**kwargs)
            finally:timer.stop()
        plt.show=show_summary
        try:return original['plot_sweep_summary'](result)
        finally:plt.show=saved_show
    # 保留原输入提示，或使用 --material 的显式标签；标签不表示分类器识别结果。
    def read_material(prompt):
        if opt.material is None:value=builtins.input(prompt)
        else:value=opt.material;print(prompt+value)
        report['material_input']=value
        return value
    # 把原 main() 的默认参数构造替换为已核验配置，其余交互流程继续由 teacher 执行。
    teacher.SDRConfig=lambda:config
    teacher.USRPManager=Manager;teacher.SweepRunner=Runner;teacher.OnTimeDraw=Drawer
    teacher.tx_worker=lambda stream,*args:worker('tx_worker',stream,*args)
    teacher.rx_worker=lambda stream,*args:worker('rx_worker',stream,*args)
    teacher.save_sweep_summary_excel=save_excel;teacher.plot_sweep_summary=summary_plot
    teacher.input=read_material
    prev_cwd=Path.cwd();stdout,stderr=sys.stdout,sys.stderr
    with (output/'console.log').open('w') as log:
        sys.stdout=Tee(stdout,log);sys.stderr=Tee(stderr,log)
        try:
            os.chdir(ROOT)
            print('TEACHER_RUN_DIR='+str(output),flush=True);write_report()
            teacher.main()
        except Exception as exc:
            report['errors'].append(f'{type(exc).__name__}: {exc}');traceback.print_exc()
        finally:
            try:stop_rf()
            except Exception as exc:report['errors'].append(f'Shutdown: {exc}')
            plt.close('all')
            result=refs['result']
            report['counts']=[] if result is None else [len(x) for x in result['all_amp_records']]
            if result:
                differences=[abs(m-e) for mm,ee in zip(result['all_measured_tone_records'],result['all_expected_tone_records']) for m,e in zip(mm,ee)]
                report['max_tone_error_hz']=max(differences) if differences else None
                if not differences or max(differences)>=sweep.verify_tolerance_hz:report['errors'].append('Measured tone outside tolerance')
            # 完成状态同时要求 36 点、GUI 刷新、导出、汇总前停止和无记录错误。
            complete=(report['counts']==[12,12,12] and report.get('gui_updates',0)>0
                and bool(report['exports']) and report.get('rf_stopped_before_summary')
                and report.get('workers_stopped') and not report['errors'])
            report['status']='complete' if complete else 'failed_or_incomplete'
            report['ended_utc']=datetime.now(timezone.utc).isoformat();write_report()
            print(json.dumps({k:report.get(k) for k in ['status','counts','gui_updates','max_tone_error_hz','workers_stopped','errors']},ensure_ascii=False),flush=True)
            os.chdir(prev_cwd)
            for name,value in original.items():setattr(teacher,name,value)
            del teacher.input
            sys.stdout,sys.stderr=stdout,stderr
    return 0 if report['status']=='complete' else 2

if __name__=='__main__':raise SystemExit(main())
