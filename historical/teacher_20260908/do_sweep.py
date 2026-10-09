# -*- coding: utf-8 -*-
# ============================================================================
# 文件：do_sweep.py
# 代码来源：老师提供的 SSPP / B210-B200 扫频实验代码，原始 Git 基线 95e6815。
# 本机维护：2026-10-09 补充中文注释；原有可执行语句、参数和 docstring 保持不变。
#
# 文件职责：老师的重复扫频状态机、FFT 峰值估计和结果导出。
# 核心接口：SweepConfig / SweepRunner / fft_peak()。
# 调用关系：main.py 或 run_local.py 创建 SweepRunner；硬件设置委托 USRPManager，
#           IQ 来自 raw_queue，实时显示委托 OnTimeDraw。
# 默认扫描：2.4–3.0 GHz，含首尾 12 点，步长 600 MHz / 11，重复 3 轮。
# 基带设置：tone = 2 kHz + 点序号×1 kHz；正频率单音位于 TX 载波上方 tone 处。
# 每点状态：切载波/单音 → 等待 0.1 s → 清队列 → VERIFY → MEASURE → 下一点。
# VERIFY：采 20 个单音周期，峰频与目标 tone 的差严格小于 200 Hz 才通过。
# MEASURE：重新采 80 个周期，保存峰值幅度及期望/实测基带频率。
# 窗口长度：cycles × 实际 RX rate / tone，向整数截断；高 tone 的窗口较短。
# FFT 方法：Hann 加窗 → 2048 点 FFT → 窗和归一化 → 20log10 幅度。
#           长输入取尾 2048 点，短输入补零；补零不增加真实频率分辨能力。
# 找峰范围：默认目标 tone ±1000 Hz；没有目标或无候选频格时退回全谱找峰。
# 输入：SDR/Sweep 参数、硬件管理器、IQ 队列、停止事件和可选绘图对象。
# 输出：每轮载波 GHz、幅值 dB、期望/实测 tone Hz 数组，以及 Excel/图/打印汇总。
# Excel 格式：保留老师频率及均值/SD 两位小数；dB 域 SD 使用样本标准差。
# 数据边界：读数是数字幅度估计，没有 RF 功率/S21 标定或材料分类。
# 连续性：原控制器取样时忽略帧起始索引；原队列可能丢帧，不能推定全程连续。
# 超时分工：原 VERIFY 可持续重试；run_local.py 子类补充分点超时和 IQ 留存。
# 汇总图：plot_sweep_summary() 阻塞显示；停止 RF 的顺序由调用入口决定。
# 连续相位：本模块切频，不用作 run_phase.py 的同会话固定载频相位计算。
# ============================================================================
"""扫频逻辑模块（简化版）。"""

import os
import time
from queue import Empty
from dataclasses import dataclass

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

"""
Python 里的装饰器。
装饰器的作用是：在不改变函数或类主体写法的情况下，给它额外增加一种行为
@dataclass 的作用是：让这个类自动变成一个参数容器
如果不用 @dataclass，你可能要这样写：
class SweepConfig:
    def __init__(
        self,
        num_points=12,
        freq_start=2.4e9,
        freq_stop=3.0e9,
        sweep_repeat=3,
        tone_start=2e3,
        tone_step=1e3
    ):
        self.num_points = num_points
        self.freq_start = freq_start
        self.freq_stop = freq_stop
        self.sweep_repeat = sweep_repeat
        self.tone_start = tone_start
        self.tone_step = tone_step
"""
# 集中保存扫描范围、轮数、单音周期窗、FFT 点数和导出位置。
@dataclass 
class SweepConfig:
    """扫频实验参数集中管理。"""
    num_points: int = 12
    freq_start: float = 2.4e9
    freq_stop: float = 3.0e9
    sweep_repeat: int = 3#改
    tone_start: float = 2e3
    tone_step: float = 1e3
    # VERIFY 阶段：采集 20 个 tone 周期，用于判断当前频点是否稳定；
    verify_cycles: int = 20
    # MEASURE 阶段：采集 80 个 tone 周期，用于正式记录幅值
    measure_cycles: int = 80
    # 这是验证容差。比如当前期望 tone 是 13000 Hz，实测 FFT 峰值只要在：
    # 12800 Hz 到 13200 Hz 之间，就认为验证通过
    # 如果采样率是 250 kS/s，nfft=2048 时频率间隔大约是：250000 / 2048 ≈ 122 Hz
    # 设置的验证容差是 100 Hz，比一个 FFT bin 还小。
    # 会导致有时明明主峰已经接近期望 tone，也可能因为落在相邻格点而失败。
    # 上方历史说明中的“100 Hz”是旧文字；当前实际配置为 200 Hz，以字段值为准。
    verify_tolerance_hz: float = 200.0
    nfft: int = 2048
    switch_settle_time: float = 0.1
    save_dir: str = "/home/xinke/SDR/b210_sweep/Data"
    save_filename: str = "Sweep_data.xlsx"

    # @property 的作用是：把一个函数伪装成一个属性来用
    # 正常函数调用应该是：sweep_cfg.freq_step()
    # 但是加了 @property 后，就可以写成：sweep_cfg.freq_step
    # 包含首尾两端点，因此分母是 num_points - 1；单点返回零避免除零。
    @property
    def freq_step(self):
        if self.num_points <= 1:
            return 0.0
        return (self.freq_stop - self.freq_start) / (self.num_points - 1)

    # 以接收采样率检查最高基带单音，防止落到 Nyquist 范围之外。
    def validate(self, sdr_rate):
        max_tone = self.tone_start + (self.num_points - 1) * self.tone_step
        if max_tone >= sdr_rate / 2:
            raise ValueError("最大 tone 不能超过 Nyquist 频率")

# 输入复数 IQ 和采样率，返回峰频 Hz/幅度 dB；短窗补零不会提高真实频率分辨能力。
def fft_peak(data_seg, rate, nfft=2048, expected_freq=None, search_bw=1000.0):
    """
    返回数据段 FFT 峰值频率(Hz)和幅度(dB)。

    expected_freq:
        如果为 None，则全频谱找最大峰；
        如果给定，例如当前 tone=2000 Hz，则只在 tone 附近找峰。

    search_bw:
        在 expected_freq ± search_bw 范围内搜索峰值。
    """
    if data_seg is None or len(data_seg) < 2 or nfft < 2:
        return 0.0, -300.0

    x = np.asarray(data_seg, dtype=np.complex64)

    if len(x) > nfft:
        x = x[-nfft:]

    # Hann 窗抑制频谱泄漏；用窗和归一化可估计落在 FFT 栅格上的单音幅值。
    window = np.hanning(len(x))
    spec = np.fft.fftshift(np.fft.fft(x * window, nfft))
    freq = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / rate))
    # 幅度用 20 log10，不是功率的 10 log10；频率不落在栅格上时仍有栅栏误差。
    spec_db = 20.0 * np.log10(np.abs(spec) / np.sum(window) + 1e-12)

    # 局部找峰只回答目标单音附近的问题；需要全频谱检查才能排除别处更强峰。
    if expected_freq is not None:
        mask = (
            (freq >= expected_freq - search_bw)
            &
            (freq <= expected_freq + search_bw)
        )

        if np.any(mask):
            local_freq = freq[mask]
            local_spec_db = spec_db[mask]
            idx = np.argmax(local_spec_db)
            return float(local_freq[idx]), float(local_spec_db[idx])

    idx = np.argmax(spec_db)
    return float(freq[idx]), float(spec_db[idx])

# def fft_peak(data_seg, rate, nfft=2048):
#     """
#     返回数据段 FFT 峰值频率(Hz)和幅度(dB)。
#     高 tone 时 VERIFY 数据段较短，实际 FFT 点数被压得很小。
#     例如 13 kHz 时，验证阶段只取 20 个周期，数据点大约几百个。    
#     """
#     if data_seg is None or len(data_seg) < 2 or nfft < 2:
#         return 0.0, -300.0
#     # 把输入数据转成复数数组。USRP 接收的是 IQ 数据，所以数据类型应该是复数。
#     x = np.asarray(data_seg, dtype=np.complex64)

#     if len(x) > nfft:
#         x = x[-nfft:]

#     window = np.hanning(len(x))
#     spec = np.fft.fftshift(np.fft.fft(x * window, nfft))
#     freq = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / rate))
#     spec_db = 20.0 * np.log10(np.abs(spec) / np.sum(window) + 1e-12)
#     idx = np.argmax(spec_db)

#     return float(freq[idx]), float(spec_db[idx])

# 处理扫频状态与缓存；硬件设置委托 manager，图形更新委托 drawer。
class SweepRunner:
    """扫频流程控制器（不涉及USRP底层）。"""
    VERIFY, MEASURE = 0, 1
    # 把外部对象保存到类内部
    # 共享 args.tone 供 TX 读取；raw_queue、stop_event 连接接收线程和控制器。
    def __init__(self, args, sweep_cfg, usrp_mgr, raw_queue, stop_event, drawer=None):
        self.args = args              # args 是 SDRConfig，比如采样率、当前 tone 等
        self.cfg = sweep_cfg          # sweep_cfg 是 SweepConfig，也就是扫频参数
        self.usrp_mgr = usrp_mgr      # usrp_mgr 是 USRPManager，负责设置 USRP 的 TX/RX 频率
        self.raw_queue = raw_queue
        self.stop_event = stop_event
        self.drawer = drawer

    # 报告请求扫描参数；实际硬件载波回读由本机入口另行保存。
    def _print_info(self, freq_list):
        num = len(freq_list)
        print(f"重复扫频次数: {self.cfg.sweep_repeat}")
        print(f"扫频范围: {self.cfg.freq_start/1e9:.3f} ~ {self.cfg.freq_stop/1e9:.3f} GHz, 共 {num} 个点")
        print(f"频率步进: {self.cfg.freq_step/1e6:.3f} MHz")
        print(f"基带频率: {self.cfg.tone_start/1e3:.1f} ~ {(self.cfg.tone_start+(num-1)*self.cfg.tone_step)/1e3:.1f} kHz, 步进 {self.cfg.tone_step/1e3:.1f} kHz")
        print(f"FFT 点数: {self.cfg.nfft}")

    # 同时切换基带单音和双端载波；等待期用于减少切频过渡，不保证在途帧全部排空。
    def _set_freq_and_tone(self, freq, tone):
        self.args.tone = tone
        self.usrp_mgr.set_center_freq(freq)
        time.sleep(self.cfg.switch_settle_time)

    # 清除切频前已入队的数据；后续 VERIFY 仍需识别残留旧单音。
    def _clear_queue(self):
        """
        切换频点后，队列里可能还有上一个频点残留的数据。
        为了避免上一个频点的数据参与当前频点的 VERIFY/MEASURE，切频后要清空队列。
        """
        while True:
            try:
                self.raw_queue.get_nowait()
            except Empty:
                break

    # 取出所有当前帧供测量，最后一帧供显示；原版丢弃 start_index 不作连续性判定。
    def _fetch_available_frames(self):
        """
        从 raw_queue 中取出当前已经到达的所有 RX 数据帧。
        返回:
            frames:
                当前队列中所有新数据帧，按接收顺序排列。
                用于加入 rx_buffer，保证扫频测量尽量使用连续数据。
            latest:
                最新一帧数据。
                用于实时绘图。
        raw_queue 中的数据格式可以是：
            1. (start_index, samples)
            2. samples
        如果是第一种格式，只取 samples。

        raw_queue 中可能有多帧 RX 数据
                    ↓
        fetch_available_frames() 全部取出
                    ↓
        所有 frames 加入 rx_buffer 参与 VERIFY / MEASURE
                    ↓
        最后一帧 latest_samples 用于实时绘图
        """
        frames = []
        latest = None

        while True:
            try:
                # 不断从 raw_queue 里取数据，直到队列为空
                # get_nowait() 是非阻塞读取。如果队列有数据，就取出；
                # 如果没有数据，就抛出 Empty，结束while循环
                item = self.raw_queue.get_nowait()
                # 这里兼容两种队列格式
                if isinstance(item, tuple) and len(item) == 2:
                    _, samples = item
                else:
                    samples = item
                # 把数据统一转成 complex64 格式
                samples = np.asarray(samples, dtype=np.complex64)
                # 把当前帧加入 frames。同时把 latest 更新为当前帧。
                # 因为循环会一直取到队列为空，所以最后一次赋值的 latest 就是最新一帧。
                frames.append(samples)
                latest = samples
            except Empty:
                break

        return frames, latest       
    
    # 根据 tone 计算当前阶段需要多少点
    # 周期数 × 采样率 / 单音频率得到阶段长度；tone 越高，采样窗口越短。
    def _calc_samples(self, tone):
        """
        例如采样率为 250000 S/s，tone 为 13000 Hz。
        一个 tone 周期需要：
        250000 / 13000 ≈ 19.23 点
        VERIFY 阶段需要 20 个周期：
        20 × 250000 / 13000 ≈ 384 点
        MEASURE 阶段需要 80 个周期：
        80 × 250000 / 13000 ≈ 1538 点
        """
        rate = getattr(self.args, "rx_rate_actual", self.args.rate)
        verify_samples = int(self.cfg.verify_cycles * rate / tone)
        measure_samples = int(self.cfg.measure_cycles * rate / tone)
        return verify_samples, measure_samples

    # 严格小于容差才通过；默认容差 200 Hz，等于 200 Hz 时不通过。
    def _verify_pass(self, peak_freq, tone):
        # 判断验证是否通过
        return abs(peak_freq - tone) < self.cfg.verify_tolerance_hz

    # 每轮从第一点开始：VERIFY 重试 → MEASURE 记录 → 切到下一点 → 再 VERIFY。
    # 原控制器无每点硬超时；run_local.py 子类额外提供超时和原始帧检查。
    def run(self, enable_plot=True):
        # 先读取实际 RX 采样率，再检查扫频参数是否合法
        rate = getattr(self.args, "rx_rate_actual", self.args.rate)
        self.cfg.validate(rate)
        freq_list = np.linspace(self.cfg.freq_start, self.cfg.freq_stop, self.cfg.num_points)
        self._print_info(freq_list)

        if enable_plot and self.drawer:
            self.drawer.setup_plot()

        # 存储所有轮次结果
        """
        all_freq: 每轮实际完成测量的载波频率，单位 GHz
        all_amp: 每轮测得的幅值，单位 dB
        all_exp_tone: 每轮每个频点期望的 tone, 单位 Hz
        all_meas_tone: 每轮每个频点实测的 tone, 单位 Hz
        """
        all_freq, all_amp, all_exp_tone, all_meas_tone = [], [], [], []
        last_plot_time = 0.0
        latest_samples = None

        for sweep_idx in range(self.cfg.sweep_repeat):
            if self.stop_event.is_set() or (enable_plot and self.drawer and not self.drawer.is_open()):
                break

            print(f"\n{'='*60}\n开始第 {sweep_idx+1} / {self.cfg.sweep_repeat} 次扫频\n{'='*60}")
            
            # 每一轮开始时,初始化当前轮的记录表，并从第 0 个频点开始
            freq_record, amp_record, exp_tone_record, meas_tone_record = [], [], [], []
            idx = 0
            freq = freq_list[idx]

            # 切到当前频点
            tone = self.cfg.tone_start + idx * self.cfg.tone_step
            self._set_freq_and_tone(freq, tone)
            print(f"初始频率：{freq/1e9:.3f} GHz, 基带：{tone/1e3:.1f} kHz")

            # 清空旧数据，创建新的接收缓存，并进入 VERIFY 状态
            self._clear_queue()
            rx_buffer = []
            state = self.VERIFY
            # 计算验证阶段和测量阶段需要的采样点数
            verify_samples, measure_samples = self._calc_samples(tone)
            required = verify_samples

            while not self.stop_event.is_set() and idx < self.cfg.num_points:
                # 如果图窗被关闭，就退出当前扫频
                if enable_plot and self.drawer and not self.drawer.is_open():
                    break

                # 取新数据
                # 这一步会把队列中目前已有的所有新帧全部取出来
                new_frames, new_latest = self._fetch_available_frames()

                if new_frames:
                    # latest_samples 只保存最后一帧，用于实时绘图
                    latest_samples = new_latest
                    # new_frames 中所有帧都加入 rx_buffer，用于 VERIFY/MEASURE
                    for frame in new_frames:
                        rx_buffer.extend(frame)

                # 实时绘图
                if enable_plot and self.drawer and latest_samples is not None:
                    now = time.time()
                    if now - last_plot_time >= self.drawer.plot_interval:
                        self.drawer.update_plots(latest_samples)
                        self.drawer.pause(0.001)
                        last_plot_time = now
                # 判断数据是否足够：
                if len(rx_buffer) < required:
                    time.sleep(0.001)
                    continue
                # 一旦数据够了：从缓存中取出前 required 个点作为当前 VERIFY 
                # 或 MEASURE 的数据。剩余数据保留
                data = np.asarray(rx_buffer[:required], dtype=np.complex64)
                rx_buffer = rx_buffer[required:]
                # peak_freq, peak_amp = fft_peak(data, rate, nfft=self.cfg.nfft)
                peak_freq, peak_amp = fft_peak(
                    data,
                    rate,
                    nfft=self.cfg.nfft,
                    expected_freq=tone,
                    search_bw=1000.0
                )


                if state == self.VERIFY:
                    if self._verify_pass(peak_freq, tone):
                        print(f"[第 {sweep_idx+1} 次] 验证通过: {freq/1e9:.3f} GHz, 期望 {tone:.1f} Hz, 实测 {peak_freq:.1f} Hz")
                        state = self.MEASURE
                        # VERIFY 通过后舍弃缓存余量，正式 MEASURE 必须重新收集窗口。
                        required = measure_samples
                        rx_buffer = []
                    else: # 如果验证失败：
                        print(f"[第 {sweep_idx+1} 次] 验证失败: {freq/1e9:.3f} GHz, 期望 {tone:.1f} Hz, 实测 {peak_freq:.1f} Hz, 重新验证...")
                        rx_buffer = [] # 清空缓存，重新收集数据，再验证一次。
                else:  # MEASURE
                    print(f"[第 {sweep_idx+1} 次] 测量: {freq/1e9:.3f} GHz, 幅值 {peak_amp:.2f} dB, 实测基带 {peak_freq:.1f} Hz")
                    # 记录请求载波的 GHz 值；实际基带峰频另存 meas_tone_record。
                    freq_record.append(freq / 1e9)
                    amp_record.append(peak_amp)
                    exp_tone_record.append(tone)
                    meas_tone_record.append(peak_freq)

                    idx += 1
                    if idx >= self.cfg.num_points:
                        print(f"第 {sweep_idx+1} 次扫频完成。")
                        break
                    
                    # 如果还没扫完，就计算下一个频点：
                    freq = freq_list[idx]
                    tone = self.cfg.tone_start + idx * self.cfg.tone_step
                    self._set_freq_and_tone(freq, tone)
                    print(f"切换到频率 {freq/1e9:.3f} GHz, 基带 {tone/1e3:.1f} kHz")
                    state = self.VERIFY
                    verify_samples, measure_samples = self._calc_samples(tone)
                    required = verify_samples
                    rx_buffer = []
                    self._clear_queue()

                time.sleep(0.001)

            # 保存本轮结果
            all_freq.append(freq_record)
            all_amp.append(amp_record)
            all_exp_tone.append(exp_tone_record)
            all_meas_tone.append(meas_tone_record)

        print("\n所有扫频循环结束。")
        return {
            "freq_list": freq_list,
            "all_freq_records": all_freq,
            "all_amp_records": all_amp,
            "all_expected_tone_records": all_exp_tone,
            "all_measured_tone_records": all_meas_tone,
        }


# 保留老师的频率两位小数与统计舍入格式；精确频率表由 run_local.py 导出。
def save_sweep_summary_excel(result, save_dir, filename):
    """保存多次扫频数据到 Excel（自动调整列宽）。"""
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, filename)
    freq_ghz = [round(f / 1e9, 2) for f in result["freq_list"]]
    amp_records = result["all_amp_records"]

    data = {"Carrier_Frequency_GHz": freq_ghz}
    for i, rec in enumerate(amp_records):
        col = f"Amp_Run_{i+1}_dB"
        data[col] = rec + [np.nan] * (len(freq_ghz) - len(rec))
    df = pd.DataFrame(data)

    if len(amp_records) > 1:
        amp_cols = [f"Amp_Run_{i+1}_dB" for i in range(len(amp_records))]
        df["Amp_Mean_dB"] = df[amp_cols].mean(axis=1).round(2)
        # Pandas std 默认 ddof=1，为各轮 dB 读数的样本标准差，非独立取放重复性。
        df["Amp_Std_dB"] = df[amp_cols].std(axis=1).round(2)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Sweep_Data")
        ws = writer.sheets["Sweep_Data"]
        for col in ws.columns:
            max_len = max(len(str(cell.value)) for cell in col if cell.value is not None)
            ws.column_dimensions[col[0].column_letter].width = max_len + 2

    print(f"多次扫频数据已保存至：{path}")
    return path


# 在 GUI 中阻塞展示已记录曲线；外部包装负责保证查看图时 RF 已停止。
def plot_sweep_summary(result):
    """绘制多次扫频曲线。"""
    freq_list, amp_records = result["freq_list"], result["all_amp_records"]
    if not amp_records:
        print("未记录到数据。")
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    for i, rec in enumerate(amp_records):
        if rec:
            ax.plot(freq_list[:len(rec)] / 1e9, rec, marker="o", label=f"Run {i+1}")
    ax.set(xlabel="Carrier Frequency (GHz)", ylabel="Magnitude (dB)",
           title="Frequency Response: Multi-run Sweep")
    ax.grid(True)
    ax.legend()
    plt.show(block=True)


# 逐载波打印各轮读数；不足一轮时显示 NaN，不伪造缺失测量。
def print_sweep_summary(result):
    """终端打印多次扫频幅值汇总。"""
    freq_ghz = result["freq_list"] / 1e9
    amp_records = result["all_amp_records"]
    header = "载波(GHz)" + "".join(f"    Run{i+1}_Amp(dB)" for i in range(len(amp_records)))
    print("\n多次扫频幅值汇总:")
    print(header)
    for i, f in enumerate(freq_ghz):
        row = f"{f:8.3f}"
        for rec in amp_records:
            row += f"        {rec[i]:8.2f}" if i < len(rec) else "        NaN"
        print(row)