# 当前 SSPP 实验软件模块；源自已验证实现，老师原件保留在 historical/teacher_20260908/。
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