# 当前 SSPP 实验软件模块；源自已验证实现，老师原件保留在 historical/teacher_20260908/。
"""Fixed-frequency, uninterrupted amplitude/phase acquisition.

No phase comparisons across retunes/restarts. Initial static condition is the
within-session reference, not calibrated absolute S21. stdin accepts JSON lines:
{"mark":"condition_name","settle":3} or {"stop":true}.
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from queue import Queue, Empty, Full
import select
import signal
import sys
import threading
import time
import traceback
import numpy as np
from sspp_lab.dsp import (RelativeMonitor, HardwareTimeContinuity, TimestampDiscontinuity,
                          baseline_problems, summarize_labels)
from sspp_lab.realtime import RealtimePublisher

ROOT = Path(__file__).resolve().parent.parent
ORIGINALS = ['historical/teacher_20260908/'+n for n in ['main.py', 'uhd_lib.py', 'thread_works.py', 'do_sweep.py', 'on_time_draw.py']]


# 计算原始字节哈希，用于设备镜像和实验源码版本记录。
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
# 所有实验元数据采用带 UTC 时区的时间，便于跨主机/时区复核。
def utc(): return datetime.now(timezone.utc).isoformat()
# 写入标准 JSON；禁止 NaN，避免无效计算静默进入实验记录。
def dump(path, value): Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)+'\n')
# 输出单行 JSON 事件并立即刷新，供外部语音控制器读取。
def emit(kind, **values): print(json.dumps({'event': kind, **values}, ensure_ascii=False, allow_nan=False), flush=True)


def request_stop(stop, telemetry, reason):
    """Latch the first actual stop cause; shutdown failures cannot rewrite it."""
    telemetry.setdefault('stop_reason', reason)
    stop.set()


def record_failure(stop, telemetry, errors, reason, message):
    errors.append(message)
    request_stop(stop, telemetry, reason)


# 限制采集时长10–180 s、可调载频70 MHz–6 GHz；解析本身不访问设备。
def options():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['plan', 'acquire'])
    p.add_argument('--device-config', type=Path, default=ROOT/'local_device.json')
    p.add_argument('--duration', type=float, default=20)
    p.add_argument('--initial-label', default='water_2of3_static')
    p.add_argument('--gui', action='store_true')
    p.add_argument('--carrier', type=float, default=2.4e9)
    p.add_argument('--output-root', type=Path, default=ROOT/'Data'/'phase_runs')
    p.add_argument('--rate', type=float, default=250000.)
    p.add_argument('--tone', type=float, default=10000.)
    p.add_argument('--amplitude', type=float, default=.3)
    p.add_argument('--tx-gain', type=float, default=20.)
    p.add_argument('--rx-gain', type=float, default=30.)
    args = p.parse_args()
    if not (10 <= args.duration <= 180): p.error('Duration must be 10–180 seconds')
    if not (70e6 <= args.carrier <= 6e9): p.error('Carrier must be within 70 MHz–6 GHz')
    if not (200000 <= args.rate <= 2000000 and 0 < args.amplitude <= .3): p.error('Rate/amplitude outside current experiment bounds')
    if not (0 < args.tone < args.rate/2 and 0 <= args.tx_gain <= 30 and 0 <= args.rx_gain <= 76): p.error('Invalid tone/gain')
    if not args.initial_label.replace('_','').replace('-','').isalnum(): p.error('Use a plain condition label')
    return args


# 固定 tone，以实际 TX rate 和连续样点编号合成信号，首块定时启动。
def transmit(stream, sdr, start_time, stop, telemetry, errors):
    import uhd
    md = uhd.types.TXMetadata()
    md.start_of_burst, md.end_of_burst, md.has_time_spec = True, False, True
    # 仅首个 TX 包携带未来设备时间；后续包连续发送，避免每块重复定时。
    md.time_spec = start_time
    index = 0
    try:
        while not stop.is_set():
            n = np.arange(10000, dtype=np.float64)+index
            block = (sdr.amp*np.exp(2j*np.pi*sdr.tone*n/sdr.tx_rate_actual)).astype(np.complex64)
            sent = 0
            while sent < len(block) and not stop.is_set():
                count = stream.send(block[sent:], md, timeout=1.)
                if count <= 0: raise RuntimeError('TX send returned no samples')
                # 部分发送时补发剩余切片；成功发送后立即清除首包与定时标记。
                telemetry['tx_partial_sends'] += int(count < len(block)-sent)
                sent += count
                md.start_of_burst = md.has_time_spec = False
            index += sent
            telemetry['tx_samples'] = index
    except Exception as exc:
        record_failure(stop, telemetry, errors, 'tx_error', 'TX: '+str(exc))
    finally:
        md.start_of_burst = md.has_time_spec = False
        md.end_of_burst = True
        try:
            stream.send(np.zeros(1, np.complex64), md, timeout=.5)
            telemetry['tx_end_of_burst_sent'] = True
        except Exception as exc:
            record_failure(stop, telemetry, errors, 'tx_shutdown_error', 'TX shutdown: '+str(exc))


# 监听 TX 异步元数据，burst_ack 之外的事件均记录并请求停止。
def async_monitor(stream, done, stop, events, errors, telemetry):
    import uhd
    md = uhd.types.TXAsyncMetadata()
    try:
        while not done.is_set():
            if stream.recv_async_msg(md, .05):
                name = md.event_code.name
                events.append({'code': name, 'device_time_s': md.time_spec.get_real_secs() if md.has_time_spec else None})
                if name != 'burst_ack':
                    record_failure(stop, telemetry, errors, 'tx_async_error', 'TX async event: '+name)
    except Exception as exc:
        record_failure(stop, telemetry, errors, 'tx_async_error', 'TX async monitor: '+str(exc))


# 逐帧验收硬件时间戳和有限值，原始 IQ/帧信息落盘后送交主线程解调。
def receive(stream, rate, output, queue, stop, telemetry, errors, master_clock_rate_hz):
    import uhd
    md = uhd.types.RXMetadata()
    # 5000 点 / 250 kS/s ≈ 20 ms；recv 也可能返回更短帧，处理按实际长度执行。
    buffer = np.empty((1,5000), np.complex64)
    timeline = HardwareTimeContinuity(master_clock_rate_hz, rate)
    start_wall = time.monotonic()
    try:
        with (output/'rx_iq.cf32').open('wb') as raw, (output/'rx_frames.jsonl').open('w') as frames:
            while not stop.is_set():
                count = stream.recv(buffer, md, timeout=.25)
                if stop.is_set(): break
                if md.error_code == uhd.types.RXMetadataErrorCode.timeout:
                    telemetry['rx_timeouts'] += 1
                    # 定时开始前允许少量等待超时；首帧后再超时即拒绝连续相位采集。
                    if timeline.samples or time.monotonic()-start_wall > 3:
                        raise RuntimeError('RX timeout during phase capture')
                    continue
                if md.error_code != uhd.types.RXMetadataErrorCode.none or md.out_of_sequence:
                    raise RuntimeError(md.strerror())
                if not count: continue
                if not md.has_time_spec: raise RuntimeError('Missing RX hardware timestamp')
                # Native clock ticks retain the sub-sample origin and avoid
                # false +/-1 sample jumps from independent absolute rounding.
                first = timeline.samples
                frame = timeline.check(md.time_spec, count)
                values = buffer[0,:count].copy()
                if not np.isfinite(values).all(): raise RuntimeError('Nonfinite IQ')
                peak = float(max(np.max(np.abs(values.real)),np.max(np.abs(values.imag))))
                telemetry['peak_component'] = max(peak,telemetry['peak_component'])
                if peak >= .98: raise RuntimeError('Near-fullscale IQ')
                raw.write(values.tobytes())
                frames.write(json.dumps(frame, allow_nan=False)+'\n')
                # 队列满会作为异常停止；原始文件可能比已处理数据长，manifest 分别记录。
                queue.put_nowait((first, values))
                telemetry['rx_samples'] = timeline.samples
                telemetry['rx_frames'] += 1
    except Exception as exc:
        cause = 'rx_timestamp_discontinuity' if isinstance(exc, TimestampDiscontinuity) else 'rx_error'
        record_failure(stop, telemetry, errors, cause, 'RX: '+str(exc))
        if isinstance(exc, TimestampDiscontinuity):
            evidence = {**exc.evidence, 'reason': cause, 'accepted_into_iq': False}
            telemetry['timestamp_failure'] = evidence
            try:
                dump(output/'rx_timestamp_failure.json', evidence)
            except OSError as save_error:
                errors.append('Timestamp evidence saving: '+str(save_error))


# 以同一静止参考绘制全时序相对幅相，不扣除漂移，不跨会话拼接。
def update_figure(fig, axes, lines, monitor, events, live=True):
    if not monitor.rows or monitor.baseline is None: return
    b = monitor.baseline
    t = np.array([r['t_s'] for r in monitor.rows])
    a = np.array([r['amplitude_dBFS_est'] for r in monitor.rows])-20*np.log10(b['reference_amplitude'])
    ph = np.array([r['phase_unwrapped_deg'] for r in monitor.rows])-b['reference_phase_unwrapped_deg']
    for ax, line, y in zip(axes, lines, [a,ph]):
        line.set_data(t,y); ax.relim(); ax.autoscale_view()
    axes[0].set_title('Within-session reference: '+events[0]['label']+' | '+monitor.rows[-1]['label'])
    fig.canvas.draw_idle()


# 创建本轮目录和 manifest，执行定时收发/标记/解调，然后停止并导出。
def main():
    opts = options()
    publisher = RealtimePublisher()
    device = json.loads(opts.device_config.read_text())
    if sha(device['fpga']) != device['fpga_sha256']: raise ValueError('FPGA hash mismatch')
    if device['channel'] != 0: raise ValueError('This physical setup was reviewed for channel 0')
    from sspp_lab.hardware import SDRConfig, USRPManager
    sdr = SDRConfig(device=f'type=b200,serial={device["serial"]},fpga={device["fpga"]}',
                    freq=opts.carrier, tone=opts.tone, channel=0, rate=opts.rate, amp=opts.amplitude,
                    tx_gain=opts.tx_gain, rx_gain=opts.rx_gain)
    sdr.validate()
    emit('PLAN', carrier_Hz=sdr.freq, tone_Hz=sdr.tone, rate=sdr.rate, duration_s=opts.duration,
         tx_gain=sdr.tx_gain, rx_gain=sdr.rx_gain, method='continuous complex lock-in; no retune; relative to initial condition')
    # plan 在此返回，后面的 MultiUSRP、streamer 与线程不会创建。
    if opts.command == 'plan': return 0
    import uhd
    os.environ.setdefault('MPLCONFIGDIR',str(ROOT/'.cache'/'matplotlib'))
    import matplotlib
    matplotlib.use('QtAgg' if opts.gui else 'Agg')
    import matplotlib.pyplot as plt
    output = opts.output_root/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output.mkdir(parents=True, exist_ok=False)
    print('OUTPUT_DIR='+str(output), flush=True)
    manifest = {'status':'initializing','started_utc':utc(),'initial_label':opts.initial_label,
                'device_profile':device,'requested_sdr':asdict(sdr),'duration_limit_s':opts.duration,
                'source_hashes':{n:sha(ROOT/n) for n in ORIGINALS},
                'phase_source_hashes':{n:sha(ROOT/n) for n in ['sspp_lab/acquire_phase.py','sspp_lab/dsp.py','sspp_lab/hardware.py','sspp_lab/realtime.py']},
                'python':sys.executable,'method':'single-channel fixed-carrier complex lock-in; baseline-relative amplitude and phase',
                'limitations':['No simultaneous RF reference channel; instrument drift not fully removed.',
                               'Not calibrated S21 or absolute phase; no cross-run phase comparison.',
                               'Unwrapping assumes inter-window phase motion under 180 degrees.',
                               'No material classifier; mechanical loading and geometry require controls.']}
    dump(output/'manifest.json',manifest)
    stop, async_done = threading.Event(), threading.Event()
    errors, async_events, threads = [], [], []
    telemetry={'tx_samples':0,'rx_samples':0,'rx_frames':0,'tx_partial_sends':0,'rx_timeouts':0,
               'peak_component':0.,'tx_end_of_burst_sent':False,'rx_stop_sent':False}
    manager, monitor, fig = None, None, None
    events=[{'t_s':0.,'label':opts.initial_label,'source':'user-confirmed initial condition','host_utc':utc()}]
    current_label=opts.initial_label; pending=None
    reason=None; baseline_announced=False; last_print=-1.; last_gui=0.; last_data_wall=time.monotonic()
    # Ctrl+C/SIGTERM 只设置停止原因与共享事件，由 finally 统一释放资源。
    def interrupted(signum, frame):
        request_stop(stop, telemetry, 'signal_'+str(signum))
    signal.signal(signal.SIGINT,interrupted);signal.signal(signal.SIGTERM,interrupted)
    # 独立墙钟 watchdog 比 RX 样点时长稍长，防止无数据时采集无法到达时限。
    def hard_stop():
        record_failure(stop, telemetry, errors, 'wall_clock_watchdog', 'Independent wall-clock watchdog expired')
    timer=threading.Timer(opts.duration+5,hard_stop);timer.daemon=True
    raw_series=None; rx_started=False
    publisher.start()
    try:
        manager=USRPManager(sdr);manager.configure()
        emit('HARDWARE_READY')
        if abs(sdr.rx_rate_actual-sdr.tx_rate_actual)>1e-6: raise RuntimeError('TX/RX rates differ')
        manifest['actual_sdr']=asdict(sdr)
        manifest['actual_hardware']={'tx_gain':manager.usrp.get_tx_gain(0),'rx_gain':manager.usrp.get_rx_gain(0),
                                     'master_clock_rate':manager.usrp.get_master_clock_rate(),
                                     'tx_antenna':manager.usrp.get_tx_antenna(0),'rx_antenna':manager.usrp.get_rx_antenna(0)}
        master_clock_rate_hz = manifest['actual_hardware']['master_clock_rate']
        # Reject unsupported fractional clock/sample ratios before any stream
        # creation, timed RX command, or TX worker can begin.
        clock_check = HardwareTimeContinuity(master_clock_rate_hz, sdr.rx_rate_actual)
        manifest['timestamp_continuity'] = clock_check.policy()
        tx,rx=manager.create_streamers()
        monitor=RelativeMonitor(sdr.rx_rate_actual,sdr.tone)
        if opts.gui:
            plt.ion()
        fig,axes=plt.subplots(2,1,figsize=(11,7),sharex=True,layout='constrained')
        lines=[axes[0].plot([],[],color='#007b83')[0],axes[1].plot([],[],color='#bc5d13')[0]]
        axes[0].set_ylabel('Amplitude change (dB)');axes[1].set_ylabel('Relative phase (degrees)')
        axes[1].set_xlabel('Time since first RX sample (s)')
        for ax in axes: ax.grid(alpha=.2)
        if opts.gui: fig.canvas.manager.set_window_title('SSPP live amplitude and phase');plt.show(block=False)
        # 有界处理队列最多 128 帧；不为追上实时显示而丢弃 IQ。
        q=Queue(maxsize=128)
        raw_series=(output/'amplitude_phase.jsonl').open('w')
        # RX/TX 共用未来 0.7 s 的设备时刻；同设备定时不等于存在第二路 RF 相位参考。
        start_time=manager.usrp.get_time_now()+uhd.types.TimeSpec(.7)
        cmd=uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
        cmd.stream_now=False;cmd.time_spec=start_time
        rx.issue_stream_cmd(cmd);rx_started=True
        workers=[threading.Thread(target=receive,args=(rx,sdr.rx_rate_actual,output,q,stop,telemetry,errors,master_clock_rate_hz),daemon=True),
                 threading.Thread(target=async_monitor,args=(tx,async_done,stop,async_events,errors,telemetry),daemon=True),
                 threading.Thread(target=transmit,args=(tx,sdr,start_time,stop,telemetry,errors),daemon=True)]
        timer.start()
        for worker in workers: worker.start();threads.append(worker)
        last_data_wall=time.monotonic()
        manifest['status']='running';manifest['scheduled_start_device_s']=start_time.get_real_secs();dump(output/'manifest.json',manifest)
        emit('ACQUIRING', instruction='Keep initial sample still for baseline; do not retune or restart.')
        while not stop.is_set():
            if sys.stdin in select.select([sys.stdin],[],[],0)[0]:
                line=sys.stdin.readline()
                if line.strip():
                    control=json.loads(line)
                    t=monitor.rows[-1]['t_s'] if monitor.rows else 0.
                    if control.get('stop'):
                        request_stop(stop, telemetry, 'operator_stop');break
                    if 'mark' in control:
                        label=str(control['mark'])
                        if not label.replace('_','').replace('-','').isalnum(): raise ValueError('Invalid condition label')
                        if monitor.baseline is None: raise ValueError('Do not change conditions during baseline')
                        settle=float(control.get('settle',3.))
                        if not (0<=settle<=30): raise ValueError('Invalid settle time')
                        # 收到标记后先置 transition，再按 RX 窗口时间等待 settle；不是实际动作传感器。
                        current_label='transition_to_'+label if settle else label
                        pending=(t+settle,label) if settle else None
                        events.append({'t_s':t,'label':current_label,'source':'operator command','host_utc':utc()})
                        emit('MARK_ACCEPTED',label=current_label,t_s=t,settle_s=settle)
            try: start,iq=q.get(timeout=.05)
            except Empty:
                if time.monotonic()-last_data_wall>4: raise RuntimeError('No RX data for four seconds')
                continue
            last_data_wall=time.monotonic()
            t=(start+(len(iq)-1)/2)/sdr.rx_rate_actual
            if pending and t>=pending[0]:
                current_label=pending[1];pending=None
                events.append({'t_s':t,'label':current_label,'source':'settle period elapsed','host_utc':utc()})
                emit('STABLE_LABEL',label=current_label,t_s=t)
            row=monitor.process(iq,start,current_label)
            raw_series.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')
            # Display delivery never waits for stdout or changes the recorded series.
            publisher.trace(row)
            publisher.preview(iq, rate=sdr.rx_rate_actual, carrier_hz=sdr.freq,
                              tone_hz=sdr.tone, t_s=row['t_s'], sample_start=start, mode='phase')
            if monitor.baseline is not None and not baseline_announced:
                baseline_announced=True
                issues=baseline_problems(monitor.baseline)
                emit('BASELINE_READY' if not issues else 'BASELINE_REJECTED',baseline=monitor.baseline,issues=issues)
                dump(output/'baseline.json',monitor.baseline)
                if issues: raise RuntimeError('; '.join(issues))
            if monitor.baseline and row['coherent_fraction']<.5:
                raise RuntimeError('Signal coherence below 0.5; phase no longer accepted')
            # 120°/窗是保守工程停止门槛，为 180° 解缠极限留余量。
            if monitor.baseline and abs(row['phase_step_deg'])>120:
                raise RuntimeError('Phase step exceeds 120 degrees/window; unwrap ambiguous')
            if t-last_print>=2:
                last_print=t
                emit('LIVE',t_s=round(t,2),label=current_label,
                     amplitude_dB=row['relative_amplitude_dB'],phase_deg=row['relative_phase_deg'],
                     coherent_fraction=round(row['coherent_fraction'],4),remaining_s=round(max(0,opts.duration-t),1))
            if opts.gui and time.monotonic()-last_gui>.25:
                last_gui=time.monotonic()
                if not plt.fignum_exists(fig.number): request_stop(stop, telemetry, 'window_closed');break
                update_figure(fig,axes,lines,monitor,events)
                plt.pause(.001)
            if t>=opts.duration: request_stop(stop, telemetry, 'duration_limit')
    except Exception as exc:
        record_failure(stop, telemetry, errors, 'acquisition_error' if rx_started else 'initialization_error',
                       type(exc).__name__+': '+str(exc))
        traceback.print_exc(file=sys.stdout)
    finally:
        stop.set();timer.cancel()
        if manager and rx_started:
            try: manager.stop_rx_stream();telemetry['rx_stop_sent']=True
            except Exception as exc:
                record_failure(stop, telemetry, errors, 'rx_shutdown_error', 'RX shutdown: '+str(exc))
        # 先等待 RX/TX；索引 1 的异步监听继续读取结束事件，再设置 async_done 收尾。
        for idx,th in enumerate(threads):
            if idx != 1: th.join(timeout=3)
        time.sleep(.15)
        async_done.set()
        for th in threads: th.join(timeout=1)
        telemetry['workers_stopped']=all(not th.is_alive() for th in threads)
        if not telemetry['workers_stopped']:
            record_failure(stop, telemetry, errors, 'worker_shutdown_error', 'Worker failed to stop')
        if raw_series: raw_series.close()
        publisher.flush_trace()
        telemetry['realtime_display']=publisher.stats()
        reason=telemetry.get('stop_reason', 'unknown_stop')
        emit('RF_STOPPED',reason=reason,telemetry=telemetry)
    # 此处 RF 已请求停止且已等待线程，之后才生成图/汇总，避免导出期间继续发射。
    summary={}
    try:
        if monitor and monitor.baseline:
            summary=summarize_labels(monitor.rows,monitor.baseline)
            update_figure(fig,axes,lines,monitor,events,live=False)
            for event in events[1:]:
                for ax in axes: ax.axvline(event['t_s'],color='#9ca3af',ls=':',alpha=.6)
            fig.savefig(output/'amplitude_phase.png',dpi=180)
            fig.savefig(output/'amplitude_phase.pdf')
        if fig: plt.close(fig)
    except Exception as exc: errors.append('Plot export: '+str(exc))
    raw=output/'rx_iq.cf32'
    manifest.update({'ended_utc':utc(),'stop_reason':reason,'telemetry':telemetry,
                     'timestamp_failure_file':'rx_timestamp_failure.json' if telemetry.get('timestamp_failure') else None,
                     'tx_async_events':async_events,'errors':errors,'events':events,
                     'baseline':monitor.baseline if monitor else None,'condition_summary':summary,
                     'processed_windows':len(monitor.rows) if monitor else 0,
                     'processed_samples':sum(r['count'] for r in monitor.rows) if monitor else 0,
                     'raw_dtype':'complex64 native little-endian, one RX channel',
                     'raw_bytes':raw.stat().st_size if raw.exists() else 0,
                     'raw_sha256':sha(raw) if raw.exists() else None,
                     'raw_complete_file_closed':telemetry.get('workers_stopped',False)})
    # complete 表示工程数据契约通过，不表示位移已标定或材料已分类。
    valid=not errors and monitor and monitor.baseline and telemetry['tx_end_of_burst_sent'] and telemetry['rx_stop_sent']
    manifest['status']='complete' if valid else 'failed_or_incomplete'
    manifest['experiment_scope']='static_phase_stability_only' if len(summary)<=1 else 'operator_labeled_within_session_condition_comparison'
    dump(output/'manifest.json',manifest);dump(output/'events.json',events)
    dump(output/'condition_summary.json',summary)
    emit('COMPLETE',status=manifest['status'],output=str(output),errors=errors,baseline=manifest['baseline'],summary=summary)
    publisher.close()
    telemetry['realtime_display']=publisher.stats()
    dump(output/'manifest.json',manifest)
    return 0 if valid else 2


if __name__=='__main__': raise SystemExit(main())
