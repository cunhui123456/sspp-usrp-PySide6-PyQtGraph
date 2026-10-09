# -*- coding: utf-8 -*-
# ============================================================================
# 文件：on_time_draw.py
# 代码来源：老师提供的 SSPP / B210-B200 扫频实验代码，原始 Git 基线 95e6815。
# 本机维护：2026-10-09 补充中文注释；原有可执行语句、参数和 docstring 保持不变。
#
# 文件职责：老师的实时时域、频域和 RMS 显示模块，仅负责可视化。
# 核心对象：OnTimeDraw，持有 Matplotlib 图窗、两条曲线和刷新计时。
# 调用关系：main.py 创建对象，SweepRunner 定期将最新 RX 帧交给 update_plots()；
#           run_teacher.py 继承对象以记录图形/频谱证据。
# 输入：SDRConfig 中的实际 RX rate，以及最新一帧 complex64 IQ。
# 输出：时域 I 分量图、复数 IQ 频谱图、每 2 s 的 RX RMS 数字强度打印。
# 默认节奏：绘图间隔 0.2 s，坐标轴每 5 帧更新，最多显示 2000 个时域点。
# 时域公式：t_ms = n / fs_rx × 1000，y = real(IQ)；图上只显示 I，不显示 Q。
# 频域公式：尾部最多 2048 点 → Hann 窗 → FFT/fftshift → 20log10 窗和归一化幅值。
# 频率含义：横轴为相对载波的基带频率偏移 kHz；显示范围默认 ±fs_rx/8。
# RMS 公式：sqrt(mean(|IQ|²))，再取 20log10；包含整帧所有频率成分。
# 读数区别：RMS 不等于 do_sweep.py 的目标单音局部 FFT 峰值，两者不能混作同一量。
# 图形更新：setup_plot() 非阻塞显示，pause()/flush_events() 处理 GUI 操作，
#           is_open() 让控制器判断用户是否关闭窗口。
# 停止分工：close() 只关闭图窗，不直接发 RX stop 或 TX EOB。
# 测量边界：绘图窗口不是正式 VERIFY/MEASURE 窗口，不在此保存实验原始数据。
# 本模块不计算连续相位、毫米位移、S21、材料类别或温度/压力。
# ============================================================================
"""
实时绘图模块。
用于 SDR 收发验证和扫频运行过程中的图形显示。

职责：
1. 显示最新一帧 IQ 的时域波形；
2. 显示最新一帧 IQ 的频谱；
3. 打印 RX RMS power。

不在这里放扫频状态机、Excel 保存或 KNN 分类。
"""
import time
import numpy as np
import matplotlib.pyplot as plt

# 维护图窗和刷新节奏；显示窗口不是正式测量窗口。
class OnTimeDraw:
    # 最多显示 2000 个时域点与 2048 点 FFT；降低坐标轴重算频率以减轻 GUI 开销。
    def __init__(self, args):
        self.args = args

        self.time_points = 2000
        self.nfft = 2048
        self.plot_interval = 0.2

        self._last_power_print = 0.0
        self.power_print_interval = 2.0

        self._frame_count = 0
        self.axis_update_interval = 5

        self.fig = None
        self.ax_time = None
        self.ax_freq = None
        self.line_time = None
        self.line_freq = None

    # 启用非阻塞交互窗口，主循环必须持续处理 GUI 事件。
    def setup_plot(self, figsize=(9, 6)):
        """初始化实时绘图窗口。"""
        plt.ion()
        self.fig, (self.ax_time, self.ax_freq) = plt.subplots(
            2,
            1,
            figsize=figsize
        )

        self.line_time, = self.ax_time.plot([], [], lw=1.2)
        self.ax_time.set_title("Time domain")
        self.ax_time.set_xlabel("Time (ms)")
        self.ax_time.set_ylabel("Real part")
        self.ax_time.grid(True)

        self.line_freq, = self.ax_freq.plot([], [], lw=1.2)
        self.ax_freq.set_title("Spectrum")
        self.ax_freq.set_xlabel("Freq offset (kHz)")
        self.ax_freq.set_ylabel("Magnitude (dB)")
        self.ax_freq.grid(True)

        fs_rx = getattr(self.args, "rx_rate_actual", self.args.rate)

        t_max = self.time_points / fs_rx * 1e3
        self.ax_time.set_xlim(0, t_max)

        # 显示横轴仅裁到 ±fs/8，FFT 本身仍计算全采样带宽。
        half_bw = fs_rx / 8.0 / 1e3
        self.ax_freq.set_xlim(-half_bw, half_bw)

        self.ax_time.set_ylim(-0.01, 0.01)
        self.ax_freq.set_ylim(-130, -40)

        self.fig.tight_layout()
        plt.show(block=False)
        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

    # 取帧尾 IQ、乘 Hann 窗，再将频谱零频移到中央；输出相对基带频率。
    def calc_spectrum(self, samples, rate, nfft=None):
        """计算频谱，返回 freq_hz 和 spec_db。"""
        if samples is None or len(samples) < 2:
            return None, None

        if nfft is None:
            nfft = min(self.nfft, len(samples))
        else:
            nfft = min(nfft, len(samples))

        if nfft < 2:
            return None, None

        x_fft = samples[-nfft:]
        window = np.hanning(nfft)

        spec = np.fft.fftshift(np.fft.fft(x_fft * window, nfft))
        freq_hz = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / rate))
        # 数字幅度谱未进行仪器功率校准；不能把纵轴直接解读成 dBm。
        spec_db = 20.0 * np.log10(
            np.abs(spec) / np.sum(window) + 1e-12
        )

        return freq_hz, spec_db

    # 按整帧计算 RMS、按尾部窗口显示 I 分量与 FFT；轴范围仅每五帧更新。
    def update_plots(self, samples):
        """更新时域图和频域图。"""
        if samples is None or samples.size == 0:
            return

        if self.fig is None:
            self.setup_plot()

        fs_rx = getattr(self.args, "rx_rate_actual", self.args.rate)
        now = time.time()

        self._frame_count += 1
        update_axis = (
            self._frame_count == 1
            or self._frame_count % self.axis_update_interval == 0
        )

        if now - self._last_power_print >= self.power_print_interval:
            # RMS 汇总整帧所有频率能量，可能含噪声/干扰，不能代替目标单音幅度。
            rms = np.sqrt(np.mean(np.abs(samples) ** 2))
            print("RX RMS power: %.1f dBFS" % (
                20.0 * np.log10(rms + 1e-12)
            ))
            self._last_power_print = now

        # ---------- 时域 ----------
        n_time = min(self.time_points, samples.size)
        x_time = samples[-n_time:]

        t_ms = np.arange(n_time) / fs_rx * 1e3
        # 只显示 I 分量；Q 分量仍参与频谱与 RMS 运算。
        y_time = np.real(x_time)

        self.line_time.set_data(t_ms, y_time)

        if update_axis:
            y_max = np.max(np.abs(y_time))
            y_lim = max(0.001, 1.5 * y_max)
            self.ax_time.set_ylim(-y_lim, y_lim)

        # ---------- 频域 ----------
        freq_hz, spec_db = self.calc_spectrum(
            samples,
            fs_rx,
            nfft=self.nfft
        )

        if freq_hz is None:
            return

        self.line_freq.set_data(freq_hz / 1e3, spec_db)

        if update_axis:
            spec_max = np.max(spec_db)
            self.ax_freq.set_ylim(spec_max - 80, spec_max + 5)

        self.fig.canvas.draw_idle()
        self.fig.canvas.flush_events()

    # 让 GUI 事件循环处理绘图、关闭和工具栏操作。
    def pause(self, seconds=0.001):
        """处理 GUI 事件，避免图窗卡死。"""
        plt.pause(seconds)

    # 图尚未创建时允许流程继续；创建后检查用户是否关闭。
    def is_open(self):
        """判断图窗是否仍然存在。"""
        if self.fig is None:
            return True

        return plt.fignum_exists(self.fig.number)

    # 关闭 Matplotlib 图窗；这一步本身不停止 UHD 收发。
    def close(self):
        """关闭图像窗口。"""
        plt.ioff()
        plt.close("all")