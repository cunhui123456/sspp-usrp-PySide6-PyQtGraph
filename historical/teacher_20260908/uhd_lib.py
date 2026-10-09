# -*- coding: utf-8 -*-
# ============================================================================
# 文件：uhd_lib.py
# 代码来源：老师提供的 SSPP / B210-B200 扫频实验代码，原始 Git 基线 95e6815。
# 本机维护：2026-10-09 补充中文注释；原有可执行语句、参数和 docstring 保持不变。
#
# 文件职责：老师的 USRP 硬件配置与数据流适配层，供入口和扫频控制器调用。
# 核心对象：
#   SDRConfig：数据类参数容器，保存设备地址、载波、采样率、单音、幅值和增益。
#   USRPManager：封装 MultiUSRP 初始化、配置、streamer 创建和 RX 启停。
# 调用关系：main.py / run_local.py / run_phase.py 使用此模块；
#           do_sweep.SweepRunner 通过 set_center_freq() 切换 TX/RX 载波。
# 输入：SDRConfig；device 可包含显式 serial 与 fpga 选择项。
# 输出：已配置的 USRP、TX/RX streamer，以及写回参数对象的实际采样率。
# 单位：freq/tone 为 Hz，rate 为复采样 S/s，tx_gain/rx_gain 为 dB，amp 为数字幅值。
# 默认参数：freq 2.4 GHz，rate 250 kS/s，tone 2 kHz，amp 0.3，gain 20/30 dB。
# 通道配置：channel 0；TX/RX 与 RX2 是该通道的 UHD 天线选项，不能推定面板编号。
# 数据格式：主机 fc32（复 float32），传输 sc16（复 int16），nsamps 默认 8192。
# 实际采样率：configure() 回读 TX/RX rate；FFT 和绘图以实际 RX rate 为准。
# 启动边界：导入模块或构造 manager 不打开设备；configure() 才初始化硬件，
#           初始化可能加载固件/FPGA；create_streamers() 本身不启动连续收发。
# 启停分工：start_rx_stream()/stop_rx_stream() 发 RX 命令；TX/EOB 由发送线程负责。
# 频率切换：set_center_freq() 顺序修改 TX/RX，不能用于维持跨切频的连续相位参考。
# 参数检查：validate() 检查 Nyquist 和数字幅值；实际射频功率需另行校准。
# 本模块不包含 FFT、扫频状态机、绘图、Excel 或材料分类。
# ============================================================================
"""
USRP/B210 硬件配置模块。

职责：
1. 保存 SDR 硬件参数；
2. 配置 USRP；
3. 创建 TX/RX streamer；
4. 启动/停止 RX 连续接收；
5. 动态设置扫频载波频率。

不要在这里放扫频流程、绘图、Excel、KNN 或信号处理逻辑。
"""

import time
import uhd
from dataclasses import dataclass
from typing import Optional

# 数据类自动生成初始化函数；频率/采样率为 Hz 与 S/s，增益为 dB，amp 为数字幅值。
@dataclass
class SDRConfig:
    # device 是 UHD 地址串；空串自动发现，不等于选择用户自己的设备。
    device: str = ""
    # freq 为射频载波 Hz；rate 为复采样率 S/s；tone 为基带单音 Hz。
    freq: float = 2.4e9
    rate: float = 250e3
    tone: float = 2e3
    amp: float = 0.3

    tx_gain: float = 20.0
    rx_gain: float = 30.0
    # TX/RX、RX2 是所选 channel 的逻辑天线名称，不直接代表面板编号。
    tx_ant: str = "TX/RX"
    rx_ant: str = "RX2"

    channel: int = 0
    # nsamps 是接收缓冲区容量，实际 recv() 返回长度可能更短。
    nsamps: int = 8192

    tx_rate_actual: Optional[float] = None
    rx_rate_actual: Optional[float] = None

    # 检查基带单音的 Nyquist 条件与数字幅值范围；这些条件不等同于完整射频功率校准。
    def validate(self):
        """检查 SDR 基本参数。"""
        if abs(self.tone) >= self.rate / 2:
            raise ValueError("tone 不能超过 Nyquist 频率。")

        if self.amp <= 0 or self.amp > 1.0:
            raise ValueError("amp 应在 0–1 之间。")

        if self.tx_gain > 30:
            print("[WARN] 当前 TX gain 较高，请确认实验连接和射频安全。")


# 将设备操作与扫频算法分开；实例创建时尚未打开硬件。
class USRPManager:
    """封装 USRP 的配置、流创建与启停操作。"""

    # 保存参数对象；configure() 回读实际采样率后会更新同一个对象。
    def __init__(self, args: SDRConfig):
        self.args = args
        self.usrp = None
        self.tx_streamer = None
        self.rx_streamer = None

    # 打开明确指定的设备并配置双端；TX/RX 回读值可能与请求值不同。
    def configure(self):
        """配置 USRP 设备并读取实际参数。"""
        usrp = uhd.usrp.MultiUSRP(self.args.device)

        usrp.set_tx_rate(self.args.rate, self.args.channel)
        usrp.set_rx_rate(self.args.rate, self.args.channel)

        usrp.set_tx_freq(
            uhd.types.TuneRequest(self.args.freq),
            self.args.channel
        )
        usrp.set_rx_freq(
            uhd.types.TuneRequest(self.args.freq),
            self.args.channel
        )

        usrp.set_tx_gain(self.args.tx_gain, self.args.channel)
        usrp.set_rx_gain(self.args.rx_gain, self.args.channel)

        usrp.set_tx_antenna(self.args.tx_ant, self.args.channel)
        usrp.set_rx_antenna(self.args.rx_ant, self.args.channel)

        time.sleep(0.5)

        self.args.tx_rate_actual = usrp.get_tx_rate(self.args.channel)
        self.args.rx_rate_actual = usrp.get_rx_rate(self.args.channel)

        # 后续 FFT、绘图、扫频测量均以实际 RX 采样率为准。
        # 将通用 rate 更新为实际 RX rate，TX 合成仍单独用 tx_rate_actual。
        self.args.rate = self.args.rx_rate_actual

        self.usrp = usrp

        self._print_config()

        return usrp

    # 主机采用 complex float32（fc32），线缆传输采用复数 int16（sc16）。
    def create_streamers(self):
        """创建 TX/RX 流对象。"""
        if self.usrp is None:
            raise RuntimeError("请先调用 configure() 配置 USRP。")

        tx_args = uhd.usrp.StreamArgs("fc32", "sc16")
        rx_args = uhd.usrp.StreamArgs("fc32", "sc16")

        tx_args.channels = [self.args.channel]
        rx_args.channels = [self.args.channel]

        self.tx_streamer = self.usrp.get_tx_stream(tx_args)
        self.rx_streamer = self.usrp.get_rx_stream(rx_args)

        return self.tx_streamer, self.rx_streamer

    # 立即启动连续 RX；这与 run_phase.py 的未来时刻定时启动不同。
    def start_rx_stream(self):
        """启动 RX 连续接收流。"""
        if self.rx_streamer is None:
            raise RuntimeError("请先调用 create_streamers() 创建流。")

        cmd = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
        cmd.stream_now = True
        self.rx_streamer.issue_stream_cmd(cmd)

    # 向 RX 发送停止命令；TX 的 EOB 由发送线程负责。
    def stop_rx_stream(self):
        """停止 RX 连续接收流。"""
        if self.rx_streamer is not None:
            cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
            self.rx_streamer.issue_stream_cmd(cmd)

    # 同次修改 TX 与 RX 载波；扫频可以使用，连续相位过程中不能这样切频。
    def set_center_freq(self, freq):
        """
        同步设置 TX/RX 载波频率。

        扫频流程中只通过这个函数切换频点。
        """
        if self.usrp is None:
            raise RuntimeError("请先调用 configure() 配置 USRP。")

        self.args.freq = freq

        self.usrp.set_tx_freq(
            uhd.types.TuneRequest(freq),
            self.args.channel
        )
        self.usrp.set_rx_freq(
            uhd.types.TuneRequest(freq),
            self.args.channel
        )

    # 打印硬件回读参数，便于核查实际设置；天线名是 UHD 逻辑选项。
    def _print_config(self):
        """打印当前 USRP 配置信息。"""
        usrp = self.usrp
        args = self.args

        print("========== USRP Config ==========")
        print("TX rate: %.3f S/s" % args.tx_rate_actual)
        print("RX rate: %.3f S/s" % args.rx_rate_actual)
        print("TX freq: %.9f GHz" % (usrp.get_tx_freq(args.channel) / 1e9))
        print("RX freq: %.9f GHz" % (usrp.get_rx_freq(args.channel) / 1e9))
        print("TX gain: %.2f dB" % usrp.get_tx_gain(args.channel))
        print("RX gain: %.2f dB" % usrp.get_rx_gain(args.channel))
        print("TX antenna: %s" % args.tx_ant)
        print("RX antenna: %s" % args.rx_ant)
        print("=================================")