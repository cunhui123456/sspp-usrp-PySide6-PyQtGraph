"""温压串口页面。run_completed 在后台线程退出、文件封存后发相对 run key。"""

from pathlib import Path

from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

from sspp_experiments.temperature_pressure import CaptureConfig, PROTOCOLS, SerialCaptureSession, list_serial_ports
from .plots import COLORS, make_plot
from .qt_cleanup import close_plot_widgets


class TemperaturePressureWidget(QtWidgets.QWidget):
    run_completed = QtCore.Signal(str)
    error = QtCore.Signal(str)
    capture_state_changed = QtCore.Signal(bool)
    busy_changed = QtCore.Signal(bool)

    def __init__(self, root: Path, parent=None, *, session=None):
        super().__init__(parent)
        self.root = Path(root)
        self.session = session or SerialCaptureSession(self.root)
        self._published_key = None
        self._was_running = False
        self._last_rows = -1
        self._external_busy = False
        self.lines = {}
        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel("温压传感器 · Arduino 串口采集")
        title.setStyleSheet("font-size: 18px; font-weight: 600; color: #087f85")
        layout.addWidget(title)
        note = QtWidgets.QLabel("选择板上实际运行的协议和接线，再开始接收。保存原始串口字节、ADC／名义电压与两套时间；温度与压力物理量标定在分析工作台完成。串口连接可能使 Nano 复位。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.config_group = QtWidgets.QGroupBox("本次采集")
        form = QtWidgets.QFormLayout(self.config_group)
        self.port = QtWidgets.QComboBox()
        self.refresh_button = QtWidgets.QPushButton("刷新端口")
        self.refresh_button.clicked.connect(self.refresh_ports)
        port_row = QtWidgets.QHBoxLayout()
        port_row.addWidget(self.port, 1)
        port_row.addWidget(self.refresh_button)
        form.addRow("串口（仅列名称）", port_row)
        self.protocol = QtWidgets.QComboBox()
        for key, label in PROTOCOLS.items():
            self.protocol.addItem(label, key)
        self.protocol.currentIndexChanged.connect(self._protocol_changed)
        form.addRow("板上输出协议 · 9600 波特", self.protocol)
        self.sample_name = QtWidgets.QLineEdit()
        self.sample_name.setPlaceholderText("样品名称／编号；同一样品的标定与验证保持一致")
        self.sample_name.setMaxLength(80)
        form.addRow("样品名称", self.sample_name)
        self.channel = QtWidgets.QLineEdit("A2")
        self.channel.setMaxLength(80)
        form.addRow("实际通道／接线说明", self.channel)
        self.notes = QtWidgets.QLineEdit()
        self.notes.setMaxLength(1000)
        self.notes.setPlaceholderText("记录本次条件、选通位置、基线与参考仪器")
        form.addRow("实验说明", self.notes)
        self.confirm = QtWidgets.QCheckBox("已确认本次板上协议、传感器接线和样品；其他串口工具已关闭")
        form.addRow(self.confirm)
        layout.addWidget(self.config_group)
        buttons = QtWidgets.QHBoxLayout()
        self.start_button = QtWidgets.QPushButton("开始串口采集")
        self.start_button.clicked.connect(self.start_capture)
        self.stop_button = QtWidgets.QPushButton("停止并保存")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_capture)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.status = QtWidgets.QLabel("未开始；刷新端口不会打开设备。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.voltage_plot = make_plot("串口名义电压", "主机接收经过时间（s）", "电压（V）")
        self.voltage_plot.addLegend(offset=(12, 12))
        self.adc_plot = make_plot("固件平均 ADC", "主机接收经过时间（s）", "ADC 码值")
        self.adc_plot.setXLink(self.voltage_plot)
        self.adc_plot.addLegend(offset=(12, 12))
        layout.addWidget(self.voltage_plot, 1)
        layout.addWidget(self.adc_plot, 1)
        self.event_text = QtWidgets.QPlainTextEdit()
        self.event_text.setReadOnly(True)
        self.event_text.setMaximumHeight(110)
        self.event_text.setPlaceholderText("状态、复位、回绕、错误和断连记录")
        layout.addWidget(self.event_text)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        self._protocol_changed()
        self.refresh_ports()

    @property
    def is_running(self):
        return self.session.is_running

    def refresh_ports(self):
        if self.is_running:
            return
        selected = self.port.currentData()
        self.port.clear()
        for name in list_serial_ports():
            self.port.addItem(name, name)
        index = self.port.findData(selected)
        if index >= 0:
            self.port.setCurrentIndex(index)
        if not self.port.count():
            self.status.setText("未发现 /dev/cu.* 端口。连接 Arduino 后刷新；当前没有打开设备。")

    def set_external_busy(self, busy):
        """RF 采集时阻止新开串口；已有串口采集仍可停止。"""
        self._external_busy = bool(busy)
        self.start_button.setEnabled(not self._external_busy and not self.is_running)

    def _protocol_changed(self):
        temperature = self.protocol.currentData() == "temperature_v1"
        if self.channel.text() in ("A2", "A1 / S6,S7"):
            self.channel.setText("A1 / S6,S7" if temperature else "A2")
        self.adc_plot.setVisible(temperature)

    def _fail(self, message):
        self.status.setText(message)
        self.error.emit(message)

    def start_capture(self):
        if self.is_running:
            return
        if self._external_busy:
            self._fail("请先结束当前 RF 采集并完成保存，再开始温压串口采集。")
            return
        if not self.confirm.isChecked():
            self._fail("请先确认本次板上协议、传感器接线、样品及串口工具已关闭。")
            return
        try:
            config = CaptureConfig(port=self.port.currentData() or "", protocol=self.protocol.currentData(),
                                   sample_name=self.sample_name.text().strip(), channel=self.channel.text().strip(),
                                   notes=self.notes.text().strip())
            key = self.session.start(config)
        except (ValueError, RuntimeError, OSError) as exc:
            self._fail(str(exc))
            return
        self._published_key = None
        self._last_rows = -1
        for pair in self.lines.values():
            for line in pair:
                line.setData([], [])
        self.config_group.setEnabled(False)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.status.setText("正在打开所选串口并建立记录：" + key)
        self._was_running = True
        self.capture_state_changed.emit(True)
        self.busy_changed.emit(True)

    def stop_capture(self):
        self.session.request_stop()
        self.stop_button.setEnabled(False)
        self.status.setText("正在停止串口接收、关闭文件并保存来源哈希…")

    def shutdown(self, timeout_s=2.0):
        """只收尾后台会话，可由父窗口关闭任务调用；不从后台操作 Qt。"""
        return self.session.stop(timeout_s)

    def _plot_points(self, points):
        channels = sorted({row["channel"] for row in points})
        for i, channel in enumerate(channels):
            if channel not in self.lines:
                pen = pg.mkPen(COLORS[i % len(COLORS)], width=1.5)
                self.lines[channel] = (self.voltage_plot.plot(pen=pen, name=channel, connect="finite"),
                                       self.adc_plot.plot(pen=pen, name=channel, connect="finite"))
            selected = [row for row in points if row["channel"] == channel]
            times, voltage, adc = [], [], []
            previous = None
            for row in selected:
                if previous and (row["time_segment"] != previous["time_segment"] or row["line_number"] > previous["line_number"] + 1):
                    times.append(row["host_elapsed_s"])
                    voltage.append(float("nan"))
                    adc.append(float("nan"))
                times.append(row["host_elapsed_s"])
                voltage.append(row["voltage_v"])
                adc.append(float("nan") if row["adc"] == "" else row["adc"])
                previous = row
            self.lines[channel][0].setData(times, voltage)
            self.lines[channel][1].setData(times, adc)

    def _poll(self):
        snapshot = self.session.snapshot()
        if snapshot["rows"] != self._last_rows:
            self._last_rows = snapshot["rows"]
            self._plot_points(snapshot["points"])
        self.event_text.setPlainText("\n".join(f"{row.get('host_elapsed_s', 0):.3f}s · {row['kind']} · {row['text']}" for row in snapshot["events"]))
        running = snapshot["running"]
        labels = {"idle": "未开始", "initializing": "正在打开串口", "running": "正在接收", "saving": "正在封存",
                  "complete": "已停止并保存", "failed_or_incomplete": "采集失败或不完整，证据已保留"}
        if snapshot["run_key"]:
            text = f"{labels.get(snapshot['status'], snapshot['status'])} · {snapshot['rows']} 行样本 · {snapshot['raw_bytes']} 原始字节\n{snapshot['run_key']}"
            if snapshot["errors"]:
                text += "\n" + "；".join(snapshot["errors"])
            self.status.setText(text)
        if self._was_running and not running:
            self._was_running = False
            self.config_group.setEnabled(True)
            self.start_button.setEnabled(not self._external_busy)
            self.stop_button.setEnabled(False)
            self.capture_state_changed.emit(False)
            self.busy_changed.emit(False)
        if snapshot["run_key"] and not running and snapshot["status"] in ("complete", "failed_or_incomplete") and self._published_key != snapshot["run_key"]:
            self._published_key = snapshot["run_key"]
            self.run_completed.emit(snapshot["run_key"])
            if snapshot["errors"]:
                self.error.emit("；".join(snapshot["errors"]))

    def closeEvent(self, event):
        if not self.shutdown():
            self._fail("串口后台尚未结束；请等待封存后关闭。")
            event.ignore()
            return
        self.timer.stop()
        close_plot_widgets(self)
        event.accept()
