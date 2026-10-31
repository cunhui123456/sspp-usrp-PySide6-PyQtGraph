"""全局双设备初始化确认；弹窗只列端口名称，不访问设备。"""
from PySide6 import QtCore, QtWidgets
from sspp_experiments.temperature_pressure import PROTOCOLS, list_serial_ports
from .serial_ports import arduino_serial_ports


class EnvironmentDialog(QtWidgets.QDialog):
    def __init__(self, profiles, selected_profile, *, serial_target=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("初始化多传感器实验台")
        self.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        self.setMinimumWidth(460)
        self.resize(520, 570)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        def note(text, target=layout):
            widget = QtWidgets.QLabel(text)
            widget.setWordWrap(True)
            target.addWidget(widget)
            return widget

        note("选择本次要检查的设备。两类设备分别记录状态；只连接一方也可进行对应实验，切换实验不会清除已就绪状态。")
        rf = QtWidgets.QGroupBox("B210 · 材料、手指和液体实验共用")
        rf_layout = QtWidgets.QVBoxLayout(rf)
        self.usrp_check = QtWidgets.QCheckBox("本次检查 B210")
        rf_layout.addWidget(self.usrp_check)
        self.profile_combo = QtWidgets.QComboBox()
        self.profile_combo.setObjectName("environmentProfileChoice")
        for profile in profiles:
            self.profile_combo.addItem(profile["name"], profile["id"])
        index = self.profile_combo.findData(selected_profile)
        if index >= 0:
            self.profile_combo.setCurrentIndex(index)
        rf_layout.addWidget(self.profile_combo)
        note("冷启动可能加载固件、重新枚举 USB 或加载 FPGA，最长等待 120 秒。检查后释放设备，不启动射频采集。", rf_layout)
        self.usrp_check.setEnabled(bool(profiles))
        self.usrp_check.setChecked(bool(profiles))
        if not profiles:
            note("未找到 B210 配置，本次可跳过。", rf_layout)
        layout.addWidget(rf)

        serial = QtWidgets.QGroupBox("Arduino · 温压串口")
        serial_layout = QtWidgets.QVBoxLayout(serial)
        self.serial_check = QtWidgets.QCheckBox("本次检查 Arduino 串口")
        serial_layout.addWidget(self.serial_check)
        form = QtWidgets.QFormLayout()
        port_row = QtWidgets.QHBoxLayout()
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.port_combo.setMinimumContentsLength(24)
        port_row.addWidget(self.port_combo, 1)
        self.refresh_button = QtWidgets.QPushButton("刷新端口")
        self.refresh_button.clicked.connect(self.refresh_ports)
        port_row.addWidget(self.refresh_button)
        form.addRow("串口", port_row)
        # Retain the current experiment protocol for the compatibility request
        # format; firmware mode is chosen only on the experiment page.
        self.protocol_combo = QtWidgets.QComboBox(self)
        self.protocol_combo.hide()
        for key, text in PROTOCOLS.items():
            self.protocol_combo.addItem(text, key)
        serial_target = serial_target or {}
        index = self.protocol_combo.findData(serial_target.get("protocol"))
        if index >= 0:
            self.protocol_combo.setCurrentIndex(index)
        serial_layout.addLayout(form)
        self.port_note = note("", serial_layout)
        self.explanation = note("本次只检查 USB 串口能否打开并关闭，不识别固件、不烧录。完成后进入温压实验，烧录并验证所选程序，或验证板上已有程序；收到匹配数据后才能开始记录。连接可能使板卡复位，最长等待 30 秒。", serial_layout)
        layout.addWidget(serial)
        note("点击后在后台依次检查 Arduino 和 B210，可在主窗口取消。过程显示在启动器终端；温压实验还需完成固件与数据验证。")
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QtWidgets.QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.apply_button = QtWidgets.QPushButton("应用并初始化")
        self.apply_button.setObjectName("primary")
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self.accept)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)
        self.usrp_check.toggled.connect(self._availability)
        self.serial_check.toggled.connect(self._availability)
        self.refresh_ports(selected=serial_target.get("port"))
        self._availability()

    def refresh_ports(self, checked=False, *, selected=None):
        previous = selected if selected is not None else self.port_combo.currentData()
        was_available = self.port_combo.count() > 0
        self.port_combo.clear()
        for port in arduino_serial_ports(list_serial_ports()):
            self.port_combo.addItem(port, port)
        index = self.port_combo.findData(previous)
        if index >= 0:
            self.port_combo.setCurrentIndex(index)
        available = bool(self.port_combo.count())
        self.serial_check.setEnabled(available)
        if not available:
            self.serial_check.setChecked(False)
        elif not was_available:
            self.serial_check.setChecked(True)
        self.port_note.setText("仅列 USB 串口候选，不代表已确认板卡身份；刷新不打开设备。" if available else "未发现支持的 USB 串口；蓝牙与系统虚拟端口不用于 Nano。连接 Arduino 后刷新，或跳过温压设备。")
        self._availability()

    def _availability(self):
        self.profile_combo.setEnabled(self.usrp_check.isChecked())
        self.port_combo.setEnabled(self.serial_check.isChecked())
        self.protocol_combo.setEnabled(self.serial_check.isChecked())
        self.apply_button.setEnabled(self.usrp_check.isChecked() or self.serial_check.isChecked())

    def selected_profile(self):
        return self.profile_combo.currentData()

    def selected_serial(self):
        return {"kind": "serial", "port": self.port_combo.currentData() or "", "protocol": self.protocol_combo.currentData()}

    def selected_targets(self):
        return {"usrp": {"kind": "usrp", "profile": self.selected_profile()} if self.usrp_check.isChecked() else None,
                "serial": self.selected_serial() if self.serial_check.isChecked() else None}
