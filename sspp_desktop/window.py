"""本机 SSPP 实验工作台：设备参数、实时观察、历史回放与离线诊断。"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import time
from PySide6 import QtCore, QtGui, QtWidgets

from sspp_lab.controller import DEFAULTS
from .plots import DiagnosticPlot, LivePlots, PreviewPlots
from .workers import TaskRunner

BUSY = {"starting", "running", "stopping", "saving"}
STATUS = {"starting": "正在初始化", "running": "正在采集", "stopping": "正在停止", "saving": "正在保存",
          "complete": "已完成", "stopped": "已停止并保存", "failed": "实验失败", "initialization_failed": "初始化失败",
          "stop_unconfirmed": "停止状态待确认", "unknown": "未知"}
GROUPS = {"diagnostic": "接收诊断", "sweep": "扫频分析", "phase": "幅相分析", "quality": "采集质量"}


def label(text, role=None):
    result = QtWidgets.QLabel(text)
    result.setWordWrap(True)
    if role:
        result.setObjectName(role)
    return result


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.runner = TaskRunner(self)
        self.fields = {}
        self.snapshot = {}
        self.viewing_key = None
        self.history_rows = []
        self.selected_details = None
        self.diagnostic_model = {}
        self.replay = None
        self.replay_running = False
        self.replay_elapsed = 0.0
        self._replay_tick = time.monotonic()
        self._history_generation = 0
        self._live_revision = None
        self._last_preview = 0.0
        self._close_requested = False
        self._shutdown_done = False
        self._start_pending = False
        self._shutdown_submitted = False
        self.setWindowTitle("SSPP 实验台 · PySide6 + PyQtGraph")
        self.resize(1480, 1000)
        self.setMinimumSize(1080, 740)
        self._build_ui()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(service.settings.ui_interval_ms)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.runner.idle.connect(self._finish_close)
        self._load_profiles()
        self.refresh_history()
        self._tick()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(20, 16, 20, 12)
        heading = QtWidgets.QHBoxLayout()
        title = QtWidgets.QVBoxLayout()
        title.addWidget(label("SSPP 实验台", "title"))
        subtitle = label("本机 · B210 · 实时幅相与射频传感实验", "muted")
        subtitle.setMinimumWidth(450)
        title.addWidget(subtitle)
        heading.addLayout(title)
        heading.addStretch()
        self.status_label = label("待开始", "status")
        self.status_detail = label("启动软件不会自动收发。", "muted")
        state = QtWidgets.QVBoxLayout()
        state.addWidget(self.status_label)
        state.addWidget(self.status_detail)
        heading.addLayout(state)
        layout.addLayout(heading)
        self.notice = label("", "notice")
        self.notice.hide()
        layout.addWidget(self.notice)
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_sidebar())
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_live(), "实时实验")
        self.tabs.addTab(self._build_history(), "历史记录与回放")
        self.tabs.addTab(self._build_diagnostics(), "离线分析")
        self.logs = QtWidgets.QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(250)
        self.tabs.addTab(self.logs, "运行日志")
        splitter.addWidget(self.tabs)
        splitter.setSizes([340, 1100])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.statusBar().showMessage(f"数据保存在 {Path(self.service.settings.root) / 'Data'}   ·   rf-gnuradio")
        self.setStyleSheet("""
            QMainWindow {background: #eef3f5;} QWidget {font-size: 13px; color: #243e50;}
            QLabel#title {font-size: 25px; font-weight: 700; color: #113644;}
            QLabel#muted {color: #657e8b;} QLabel#status {font-size: 20px; font-weight: 650; color: #087f85;}
            QLabel#notice {background: #fff5d9; color: #654a18; border: 1px solid #ead19c; padding: 9px; border-radius: 5px;}
            QLabel#viewBanner {background: #e4f1f0; color: #075e66; padding: 8px; border-radius: 5px; font-weight: 600;}
            QGroupBox {background: #ffffff; border: 1px solid #d7e1e6; border-radius: 6px; margin-top: 12px; padding: 13px 8px 8px;}
            QGroupBox::title {subcontrol-origin: margin; left: 10px; padding: 0 4px; font-weight: 600;}
            QPushButton {border: 1px solid #bdd0d9; border-radius: 5px; background: #ffffff; padding: 7px 10px;}
            QPushButton:hover {background: #e5f1f2;} QPushButton:disabled {color: #8c9ca5; background: #eff2f3; border-color: #dce2e5;}
            QPushButton#primary {color: white; background: #087f85; border-color: #087f85; font-weight: 600;}
            QPushButton#stop {color: #a03e42; border-color: #d7a6a7; background: #fff3f2; font-weight: 600;}
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {background: #ffffff; border: 1px solid #bdcdd5; border-radius: 4px; padding: 5px;}
            QTabWidget::pane {border: 1px solid #d3e0e5; background: #ffffff;} QTabBar::tab {padding: 10px 18px;}
            QTabBar::tab:selected {background: #ffffff; color: #087f85;} QTableWidget {border: 1px solid #dbe5e9; gridline-color: #eef2f4;}
            QHeaderView::section {background: #edf3f5; border: none; padding: 8px; font-weight: 600;}
            QCheckBox {spacing: 7px;} QScrollArea {border: none; background: transparent;}
        """)

    def _build_sidebar(self):
        side = QtWidgets.QWidget()
        side.setMinimumWidth(300)
        side.setMaximumWidth(425)
        layout = QtWidgets.QVBoxLayout(side)
        layout.setContentsMargins(0, 0, 8, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QtWidgets.QWidget()
        config_layout = QtWidgets.QVBoxLayout(holder)
        config_layout.setContentsMargins(0, 0, 0, 0)
        self.config_container = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(self.config_container)
        cl.setContentsMargins(0, 0, 0, 0)
        group = QtWidgets.QGroupBox("实验设置")
        form = QtWidgets.QFormLayout(group)
        self.profile = self._combo("profile", [])
        form.addRow("设备配置", self.profile)
        self.mode = self._combo("mode", [("连续幅相", "phase"), ("逐点扫频", "sweep")])
        form.addRow("记录方式", self.mode)
        form.addRow("实验条件", self._combo("condition", [("空载基线", "empty"), ("手指靠近／远离", "finger"), ("材料加载", "material")]))
        self.fields["label"] = QtWidgets.QLineEdit(DEFAULTS["label"])
        self.fields["label"].setMaxLength(80)
        self.fields["label"].setToolTip("文字、数字、下划线和连字符")
        form.addRow("实验标签", self.fields["label"])
        self.fields["sensor"] = QtWidgets.QLineEdit()
        self.fields["sensor"].setMaxLength(80)
        self.fields["sensor"].setPlaceholderText("传感器名称／版本")
        form.addRow("传感器", self.fields["sensor"])
        cl.addWidget(group)
        self.phase_group = QtWidgets.QGroupBox("连续幅相参数")
        pf = QtWidgets.QFormLayout(self.phase_group)
        self._number(pf, "carrier_ghz", "载频 GHz", .07, 6, 3, .001)
        self._number(pf, "duration", "记录时长 s", 10, 180, 1, 10)
        self._number(pf, "tone", "基带单音 Hz", 1, 50000, 0, 100)
        pf.addRow(label("先保持初始状态静止约 7 秒，参考通过后再标记阶段。", "muted"))
        cl.addWidget(self.phase_group)
        self.sweep_group = QtWidgets.QGroupBox("扫频参数")
        sf = QtWidgets.QFormLayout(self.sweep_group)
        for key, name, lo, hi, decimals, step in [
            ("freq_start_ghz", "起点 GHz", .07, 6, 3, .001), ("freq_stop_ghz", "终点 GHz", .07, 6, 3, .001),
            ("points", "频点数", 2, 100, 0, 1), ("repeat", "重复轮数", 1, 20, 0, 1),
            ("max_seconds", "总时限 s", 10, 3600, 0, 10), ("point_timeout", "单点时限 s", 1, 30, 1, .5),
            ("tone_start", "首个单音 Hz", 1, 50000, 0, 100), ("tone_step", "单音步进 Hz", 0, 20000, 0, 100)]:
            self._number(sf, key, name, lo, hi, decimals, step)
        cl.addWidget(self.sweep_group)
        advanced = QtWidgets.QGroupBox("采样与发射参数")
        af = QtWidgets.QFormLayout(advanced)
        for key, name, lo, hi, decimals, step in [
            ("rate", "采样率 S/s", 200000, 2000000, 0, 1000), ("amplitude", "数字幅值", .001, .3, 3, .001),
            ("tx_gain", "TX gain dB", 0, 30, 1, .5), ("rx_gain", "RX gain dB", 0, 76, 1, .5)]:
            self._number(af, key, name, lo, hi, decimals, step)
        af.addRow(label("可调载频 70 MHz–6 GHz；数字幅值不是实测射频功率。", "muted"))
        cl.addWidget(advanced)
        self.confirm = QtWidgets.QCheckBox("已确认设备接线、线缆固定及初始状态")
        self.confirm.setToolTip("所选设备 TX1/RX1 接在传感器两端；确认本次允许频段、功率与负载。")
        self.fields["setup_confirmed"] = self.confirm
        cl.addWidget(self.confirm)
        config_layout.addWidget(self.config_container)
        config_layout.addStretch()
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        buttons = QtWidgets.QHBoxLayout()
        self.check_button = QtWidgets.QPushButton("检查参数")
        self.check_button.clicked.connect(self.preflight)
        self.start_button = QtWidgets.QPushButton("开始实验")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_experiment)
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)
        self.stop_button = QtWidgets.QPushButton("停止收发并保存")
        self.stop_button.setObjectName("stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_experiment)
        layout.addWidget(self.stop_button)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self._mode_changed()
        return side

    def _combo(self, key, items):
        combo = QtWidgets.QComboBox()
        for title, value in items:
            combo.addItem(title, value)
        self.fields[key] = combo
        return combo

    def _number(self, form, key, title, lo, hi, decimals, step):
        widget = QtWidgets.QDoubleSpinBox() if decimals else QtWidgets.QSpinBox()
        if decimals:
            widget.setDecimals(decimals)
        widget.setRange(lo, hi)
        widget.setSingleStep(step)
        widget.setValue(DEFAULTS[key])
        widget.setKeyboardTracking(False)
        form.addRow(title, widget)
        self.fields[key] = widget

    def _mode_changed(self):
        phase = self.mode.currentData() == "phase"
        self.phase_group.setVisible(phase)
        self.sweep_group.setVisible(not phase)

    def _build_live(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        top = QtWidgets.QHBoxLayout()
        self.view_banner = label("当前实验 · 等待真实采集", "viewBanner")
        top.addWidget(self.view_banner, 1)
        self.return_live_button = QtWidgets.QPushButton("返回当前实验")
        self.return_live_button.clicked.connect(self.return_live)
        top.addWidget(self.return_live_button)
        layout.addLayout(top)
        self.metrics = label("幅度 —     相位 —     阶段 —", "muted")
        layout.addWidget(self.metrics)
        self.live_plots = LivePlots()
        layout.addWidget(self.live_plots, 3)
        self.diagnostic_toggle = QtWidgets.QCheckBox("展开实时 RX 诊断（时域 / 频谱 / IQ）")
        self.diagnostic_toggle.setChecked(True)
        self.preview_plots = PreviewPlots()
        self.diagnostic_toggle.toggled.connect(self._toggle_preview)
        layout.addWidget(self.diagnostic_toggle)
        layout.addWidget(self.preview_plots, 2)
        self.replay_note = label("", "muted")
        self.replay_note.hide()
        layout.addWidget(self.replay_note)
        self.live_layout = layout
        self.preview_summary = label("等待真实 RX 样本；显示抽取窗口，完整 IQ 独立保存。", "muted")
        layout.addWidget(self.preview_summary)
        self.mark_group = QtWidgets.QGroupBox("手动阶段标记 · 静止参考通过后可用")
        ml = QtWidgets.QVBoxLayout(self.mark_group)
        shortcuts = QtWidgets.QHBoxLayout()
        for title, value in [("靠近", "finger_near"), ("远离", "finger_far"), ("材料已加载", "material_loaded"), ("材料已移除", "material_removed")]:
            button = QtWidgets.QPushButton(title)
            button.clicked.connect(lambda checked=False, stage=value: self.mark(stage))
            shortcuts.addWidget(button)
        ml.addLayout(shortcuts)
        row = QtWidgets.QHBoxLayout()
        self.mark_label = QtWidgets.QLineEdit()
        self.mark_label.setPlaceholderText("自定义阶段，例如 water_2of3")
        self.mark_label.setMaxLength(80)
        row.addWidget(self.mark_label, 1)
        row.addWidget(label("过渡 s"))
        self.settle = QtWidgets.QDoubleSpinBox()
        self.settle.setRange(0, 30)
        self.settle.setSingleStep(.5)
        self.settle.setValue(3)
        row.addWidget(self.settle)
        self.contact = QtWidgets.QCheckBox("接触传感器或线缆")
        row.addWidget(self.contact)
        mark_button = QtWidgets.QPushButton("记录阶段")
        mark_button.clicked.connect(lambda: self.mark(self.mark_label.text()))
        row.addWidget(mark_button)
        ml.addLayout(row)
        self.mark_state = label("按钮记录操作时刻；实际距离需另行测量。", "muted")
        ml.addWidget(self.mark_state)
        self.mark_group.setEnabled(False)
        layout.addWidget(self.mark_group)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _build_history(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(label("已保存实验 · 选择记录查看曲线、回放或生成图册", "viewBanner"), 1)
        refresh = QtWidgets.QPushButton("刷新记录")
        refresh.clicked.connect(self.refresh_history)
        top.addWidget(refresh)
        layout.addLayout(top)
        self.history_table = QtWidgets.QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels(["开始时间（UTC）", "实验标签", "类型", "状态", "传感器"])
        self.history_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.history_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.history_table.itemSelectionChanged.connect(self._select_history)
        layout.addWidget(self.history_table, 2)
        row = QtWidgets.QHBoxLayout()
        self.view_history_button = QtWidgets.QPushButton("查看已保存曲线")
        self.view_history_button.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
        row.addWidget(self.view_history_button)
        self.analyze_button = QtWidgets.QPushButton("加载完整诊断")
        self.analyze_button.clicked.connect(self.load_diagnostics)
        row.addWidget(self.analyze_button)
        self.export_button = QtWidgets.QPushButton("生成 PNG / PDF 图册")
        self.export_button.clicked.connect(self.export_diagnostics)
        row.addWidget(self.export_button)
        self.open_run_button = QtWidgets.QPushButton("打开实验目录")
        self.open_run_button.clicked.connect(self.open_run)
        row.addWidget(self.open_run_button)
        layout.addLayout(row)
        playback = QtWidgets.QGroupBox("历史回放 · 只读数据，不启动硬件")
        pl = QtWidgets.QHBoxLayout(playback)
        self.replay_button = QtWidgets.QPushButton("载入并播放")
        self.replay_button.clicked.connect(self.play_replay)
        row.addWidget(self.replay_button)
        self.pause_button = QtWidgets.QPushButton("暂停")
        self.pause_button.clicked.connect(self.pause_replay)
        pl.addWidget(self.pause_button)
        self.replay_speed = QtWidgets.QComboBox()
        self.replay_speed.addItem("1×", 1)
        self.replay_speed.addItem("4×", 4)
        pl.addWidget(self.replay_speed)
        self.replay_slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.replay_slider.setRange(0, 1000)
        self.replay_slider.sliderMoved.connect(self.seek_replay)
        pl.addWidget(self.replay_slider, 1)
        self.replay_position = label("尚未载入")
        pl.addWidget(self.replay_position)
        self.replay_controls = playback
        self.live_layout.insertWidget(2, playback)
        self.live_layout.insertWidget(3, self.replay_note)
        playback.hide()
        bottom = QtWidgets.QSplitter()
        self.metadata = QtWidgets.QPlainTextEdit()
        self.metadata.setReadOnly(True)
        self.metadata.setPlaceholderText("选择实验查看状态、停止证据与采集质量。")
        bottom.addWidget(self.metadata)
        files_widget = QtWidgets.QWidget()
        fl = QtWidgets.QVBoxLayout(files_widget)
        fl.setContentsMargins(8, 0, 0, 0)
        fl.addWidget(label("实验文件 · 双击打开", "muted"))
        self.file_list = QtWidgets.QListWidget()
        self.file_list.itemDoubleClicked.connect(lambda item: self.open_file(item.text()))
        fl.addWidget(self.file_list, 1)
        save = QtWidgets.QPushButton("将所选文件另存为…")
        save.clicked.connect(self.save_file)
        fl.addWidget(save)
        bottom.addWidget(files_widget)
        layout.addWidget(bottom, 2)
        self._history_buttons(False)
        return page

    def _build_diagnostics(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        self.analysis_title = label("选择历史记录后加载完整诊断", "viewBanner")
        layout.addWidget(self.analysis_title)
        selectors = QtWidgets.QHBoxLayout()
        self.plot_group = QtWidgets.QComboBox()
        for key, name in GROUPS.items():
            self.plot_group.addItem(name, key)
        self.plot_group.currentIndexChanged.connect(self._select_plot_group)
        self.plot_choice = QtWidgets.QComboBox()
        self.plot_choice.currentIndexChanged.connect(self._select_plot)
        selectors.addWidget(self.plot_group)
        selectors.addWidget(self.plot_choice, 1)
        refresh = QtWidgets.QPushButton("重新加载")
        refresh.clicked.connect(self.load_diagnostics)
        selectors.addWidget(refresh)
        layout.addLayout(selectors)
        self.analysis_plot = DiagnosticPlot()
        layout.addWidget(self.analysis_plot, 1)
        self.quality_facts = label("", "muted")
        self.quality_facts.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.quality_facts)
        return page

    def config(self):
        result = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QtWidgets.QComboBox):
                result[key] = widget.currentData()
            elif isinstance(widget, QtWidgets.QCheckBox):
                result[key] = widget.isChecked()
            elif isinstance(widget, QtWidgets.QLineEdit):
                result[key] = widget.text().strip()
            else:
                result[key] = widget.value()
        return result

    def _message(self, text):
        self.notice.setText(str(text))
        self.notice.setVisible(bool(text))

    def _error(self, error):
        self._message(str(error))

    def _submit(self, key, operation, success=None, lane="analysis", failure=None):
        accepted = self.runner.submit(key, operation, success, failure or self._error, lane)
        if not accepted:
            self._message("此任务正在处理，或后台队列已满，请稍候。")
        return accepted

    def _load_profiles(self):
        def loaded(profiles):
            selected = self.profile.currentData() or getattr(self.service.settings, "default_profile", "own")
            self.profile.clear()
            for profile in profiles:
                self.profile.addItem(profile["name"] + (f" · CH {profile['channel']}" if "channel" in profile else ""), profile["id"])
            found = self.profile.findData(selected)
            if found >= 0:
                self.profile.setCurrentIndex(found)
            if not profiles:
                self._message("未找到有效设备配置。可查看历史数据；请先补齐本地设备 profile。")
        self._submit("profiles", self.service.refresh_profiles, loaded)

    def preflight(self):
        config = self.config()
        self.check_button.setEnabled(False)
        def done(result):
            self.check_button.setEnabled(True)
            self._message("参数检查通过，未打开硬件。\n" + result.get("summary", ""))
        def failed(error):
            self.check_button.setEnabled(True)
            self._error(error)
        if not self._submit("preflight", lambda: self.service.preflight(config), done, "control", failed):
            self.check_button.setEnabled(True)

    def start_experiment(self):
        if not self.confirm.isChecked():
            self._message("请先确认所选设备接线、线缆固定和初始状态。")
            return
        config = self.config()
        self._start_pending = True
        self.return_live()
        self._set_active(True)
        self.status_label.setText("检查参数并准备启动")
        self._message("")
        def done(snapshot):
            self._start_pending = False
            self._apply_status(snapshot)
        def failed(error):
            self._start_pending = False
            self._set_active(False)
            self._error(error)
        if not self._submit("start", lambda: self.service.start(config), done, "control", failed):
            self._start_pending = False
            self._set_active(False)

    def stop_experiment(self):
        self.status_detail.setText("已请求停止，等待收发退出与数据保存。")
        self._submit("stop", self.service.stop, self._apply_status, "urgent")

    def mark(self, stage):
        settle, contact = self.settle.value(), self.contact.isChecked()
        self._submit("mark", lambda: self.service.mark(stage, settle, contact),
                     lambda value: self._message(f"已提交阶段：{stage}；过渡 {settle:g} 秒；接触报告：{'是' if contact else '否'}。"), "control")

    def _set_active(self, active):
        self.config_container.setEnabled(not active and not self._close_requested)
        self.start_button.setEnabled(not active and not self._close_requested)
        self.check_button.setEnabled(not active and "preflight" not in self.runner.pending and not self._close_requested)
        self.stop_button.setEnabled(active and not self._close_requested)
        self.export_button.setEnabled(not active and self.selected_details is not None and "export" not in self.runner.pending)

    def _tick(self):
        if self._close_requested:
            self._finish_close()
            return
        if self.replay is not None:
            now = time.monotonic()
            if self.replay_running:
                self.replay_elapsed = min(self.replay.duration_s, self.replay_elapsed + (now-self._replay_tick)*self.replay_speed.currentData())
                if self.replay_elapsed >= self.replay.duration_s:
                    self.replay_running = False
                    self.pause_button.setText("继续")
                self._render_replay()
            self._replay_tick = now
        def poll():
            return self.service.snapshot(), self.service.live_snapshot()
        self.runner.submit("poll", poll, self._poll_done, self._error, "polling")

    def _poll_done(self, result):
        snapshot, live = result
        self._apply_status(snapshot)
        if self.viewing_key is None and self.replay is None:
            if live.get("revision") != self._live_revision or live.get("revision") is None:
                self._render_live(live)
                self._live_revision = live.get("revision")
        text = "\n".join(snapshot.get("logs") or [])
        if self.logs.toPlainText() != text:
            self.logs.setPlainText(text)
            self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())

    def _apply_status(self, snapshot):
        previous = (self.snapshot.get("job") or {}).get("status")
        self.snapshot = snapshot or {}
        job = self.snapshot.get("job") or {}
        status = job.get("status")
        active = status in BUSY or self._start_pending or bool(self.snapshot.get("preparing"))
        self._set_active(active)
        self.status_label.setText("正在准备" if self.snapshot.get("preparing") or self._start_pending else STATUS.get(status, "待开始"))
        if job.get("errors"):
            self.status_detail.setText("；".join(job["errors"][-2:]))
        elif job:
            self.status_detail.setText(("收发停止已确认 · " if job.get("rf_stopped") else "") + (job.get("output") or "等待设备准备"))
        else:
            self.status_detail.setText("启动软件不会自动收发。")
        can_mark = active and status == "running" and job.get("config", {}).get("mode") == "phase" and bool(job.get("baseline"))
        self.mark_group.setVisible(self.viewing_key is None and self.replay is None)
        self.mark_group.setEnabled(can_mark and self.viewing_key is None and self.replay is None)
        events = self.snapshot.get("events") or []
        if events:
            last = events[-1]
            self.mark_state.setText(f"最近标记：{last.get('label', '')} · {last.get('event', '')}    操作时刻不等于实测位置。")
        else:
            self.mark_state.setText("静止参考已通过，可以标记阶段。" if can_mark else "等待静止参考通过；标记仅适用于连续幅相。")
        if previous in BUSY and status not in BUSY:
            self.refresh_history()

    def _toggle_preview(self, shown):
        self.preview_plots.setVisible(shown)
        self._last_preview = 0
        self._live_revision = None
        if shown and self.replay is not None:
            self._render_replay()

    def _render_live(self, live, force_preview=False):
        self.live_plots.update_snapshot(live)
        points = live.get("points") or []
        if live.get("mode") == "phase" and points:
            last = points[-1]
            def formatted(value):
                return f"{value:.3f}" if isinstance(value, (float, int)) else "—"
            self.metrics.setText(f"相对幅度 {formatted(last.get('amplitude'))} dB     相对相位 {formatted(last.get('phase'))}°     阶段 {last.get('label', '—')}     时间 {formatted(last.get('x'))} s")
        elif points:
            self.metrics.setText(f"已测 {len(points)} 点     当前第 {points[-1].get('repeat', '—')} 轮     频点 {points[-1].get('point', '—')}")
        else:
            self.metrics.setText("等待真实数据；连续幅相需先建立静止参考。")
        now = time.monotonic()
        if self.diagnostic_toggle.isChecked() and (force_preview or now-self._last_preview >= self.service.settings.preview_interval_ms/1000):
            preview = live.get("preview")
            self.preview_plots.update_preview(preview)
            if preview:
                self.preview_summary.setText(f"RX 抽取窗口 · 载频 {float(preview.get('carrier_hz') or 0)/1e9:g} GHz · 采样率 {float(preview.get('rate') or 0):g} S/s · 单音 {float(preview.get('tone_hz') or 0):g} Hz · 记录时刻 {float(preview.get('t_s') or 0):.3f} s · 跳过预览 {preview.get('dropped_total', 0)} 帧")
            else:
                self.preview_summary.setText("尚无可用 RX 预览；完整原始 IQ 独立保存。")
            self._last_preview = now

    def return_live(self):
        self.replay_running = False
        self.replay = None
        self.replay_elapsed = 0
        self.replay_controls.hide()
        self.replay_note.hide()
        self.viewing_key = None
        self._history_generation += 1
        self._live_revision = None
        self.live_plots.clear()
        self.preview_plots.update_preview(None)
        self.view_banner.setText("当前实验 · 实时数据")
        self.tabs.setCurrentIndex(0)

    def refresh_history(self):
        def loaded(rows):
            selected = self.selected_details.get("key") if self.selected_details else None
            self.history_rows = rows
            self.history_table.blockSignals(True)
            self.history_table.setRowCount(len(rows))
            selected_row = None
            for index, row in enumerate(rows):
                values = [row.get("started_utc", ""), row.get("label", ""), {"phase": "连续幅相", "sweep": "扫频", "teacher": "老师 GUI"}.get(row.get("mode"), "未知"), STATUS.get(row.get("status"), row.get("status", "")), row.get("sensor", "")]
                for column, value in enumerate(values):
                    item = QtWidgets.QTableWidgetItem(str(value))
                    item.setToolTip(row["key"])
                    self.history_table.setItem(index, column, item)
                if row["key"] == selected:
                    selected_row = index
            if selected_row is not None:
                self.history_table.selectRow(selected_row)
            self.history_table.blockSignals(False)
        self._submit("history", self.service.history, loaded)

    def _history_buttons(self, enabled):
        for widget in (self.view_history_button, self.analyze_button, self.export_button, self.open_run_button, self.replay_button):
            widget.setEnabled(enabled)
        self.pause_button.setEnabled(False)
        self.replay_slider.setEnabled(False)

    def _select_history(self):
        row = self.history_table.currentRow()
        if not 0 <= row < len(self.history_rows):
            return
        key = self.history_rows[row]["key"]
        self._history_generation += 1
        generation = self._history_generation
        self.replay_running = False
        self.replay = None
        self.replay_controls.hide()
        self.replay_note.hide()
        self.viewing_key = key
        self.selected_details = None
        self._history_buttons(False)
        self.metadata.setPlainText("正在读取记录…")
        self.file_list.clear()
        self.view_banner.setText(f"历史静态记录 · {key} · 非实时采集")
        self.live_plots.clear()
        self.preview_plots.update_preview(None)
        def loaded(details):
            if generation != self._history_generation:
                return
            self.selected_details = details
            self.metadata.setPlainText(json.dumps(details.get("metadata", {}), ensure_ascii=False, indent=2))
            self.file_list.addItems(details.get("files", []))
            self.live_plots.update_snapshot(details.get("curve") or {})
            self.metrics.setText(f"历史已保存曲线 · {key}")
            self.preview_summary.setText("历史静态记录；点击回放可按时间重现 RX 预览。")
            self._history_buttons(True)
            self.replay_button.setEnabled(not key.startswith("teacher_runs/"))
            self._apply_status(self.snapshot)
        self._submit(f"details:{generation}", lambda: self.service.details(key), loaded)

    def load_diagnostics(self):
        key = self.selected_details.get("key") if self.selected_details else None
        if not key:
            self._message("请先在历史记录中选择实验。")
            return
        self.analysis_title.setText(f"正在读取 {key} 的真实记录…")
        self.tabs.setCurrentIndex(2)
        def loaded(model):
            self.diagnostic_model = model
            self.analysis_title.setText(f"{key} · {model.get('chart_count', 0)} 张诊断图")
            self.quality_facts.setText("    |    ".join(f"{fact['name']}：{fact['value']}" for fact in model.get("facts", [])))
            self._select_plot_group()
            if not model.get("chart_count"):
                self.analysis_plot.note.setText(model.get("sample", {}).get("reason", "无可用图形"))
        self._submit("diagnostics", lambda: self.service.diagnostic_view(key), loaded)

    def _select_plot_group(self):
        models = self.diagnostic_model.get("groups", {}).get(self.plot_group.currentData(), [])
        self.plot_choice.blockSignals(True)
        self.plot_choice.clear()
        for model in models:
            self.plot_choice.addItem(model["title"], model)
        self.plot_choice.blockSignals(False)
        self._select_plot()

    def _select_plot(self):
        self.analysis_plot.display(self.plot_choice.currentData() or {})

    def export_diagnostics(self):
        key = self.selected_details.get("key") if self.selected_details else None
        if not key:
            return
        self.export_button.setEnabled(False)
        self._message("正在后台生成 PNG 与 PDF 图册…")
        def done(result):
            self._message(f"图册已生成：{result.get('count', 0)} 张 PNG。可在实验文件中打开或另存。")
            self._select_history()
        def failed(error):
            self._error(error)
            self._apply_status(self.snapshot)
        self._submit("export", lambda: self.service.export_diagnostics(key), done, failure=failed)

    def open_run(self):
        if self.selected_details:
            key = self.selected_details["key"]
            self._submit("open_run", lambda: self.service.run_path(key), lambda path: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path))))

    def open_file(self, name):
        if self.selected_details:
            key = self.selected_details["key"]
            self._submit("open_file", lambda: self.service.download(key, name), lambda path: QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path))))

    def save_file(self):
        item = self.file_list.currentItem()
        if not item or not self.selected_details:
            self._message("请先选择要另存的文件。")
            return
        key, name = self.selected_details["key"], item.text()
        destination, _ = QtWidgets.QFileDialog.getSaveFileName(self, "另存实验文件", str(Path.home() / "Downloads" / name))
        if not destination:
            return
        def copy():
            source = Path(self.service.download(key, name))
            if source.resolve() != Path(destination).resolve():
                shutil.copy2(source, destination)
            return destination
        self._submit("save_file", copy, lambda path: self._message(f"文件已保存：{path}"))

    def play_replay(self):
        if not self.selected_details:
            return
        key = self.selected_details["key"]
        mode = self.selected_details.get("curve", {}).get("mode")
        if mode not in ("phase", "sweep"):
            self._message("此记录没有可回放的曲线数据。")
            return
        self._message("正在只读载入历史回放…")
        generation = self._history_generation
        def load():
            from .replay import ReplaySession
            return ReplaySession(self.service.run_path(key), mode)
        def ready(replay):
            if generation != self._history_generation:
                return
            self.replay = replay
            self.replay_note.setText(replay.timing_note)
            self.replay_note.show()
            self.replay_controls.show()
            self.viewing_key = key
            self.replay_elapsed = 0
            self.replay_running = True
            self._replay_tick = time.monotonic()
            self.view_banner.setText(f"历史回放 · {key} · 不启动硬件")
            self.pause_button.setText("暂停")
            self.pause_button.setEnabled(True)
            self.replay_slider.setEnabled(True)
            self.live_plots.clear()
            self.tabs.setCurrentIndex(0)
            self._message("")
            self._render_replay()
        self._submit("replay_load", load, ready)

    def _render_replay(self):
        if self.replay is None:
            return
        self._render_live(self.replay.snapshot(self.replay_elapsed), force_preview=not self.replay_running)
        self.replay_slider.blockSignals(True)
        self.replay_slider.setValue(round(1000*self.replay_elapsed/max(self.replay.duration_s, .001)))
        self.replay_slider.blockSignals(False)
        self.replay_position.setText(f"{self.replay_elapsed:.1f} / {self.replay.duration_s:.1f} s")
        self.view_banner.setText(f"历史回放 · {'播放中' if self.replay_running else '已暂停/结束'} · {self.replay_elapsed:.1f} / {self.replay.duration_s:.1f} s · {self.replay_speed.currentText()} · 不启动硬件")

    def pause_replay(self):
        if self.replay is None:
            return
        self.replay_running = not self.replay_running
        self._replay_tick = time.monotonic()
        self.pause_button.setText("暂停" if self.replay_running else "继续")
        self._render_replay()

    def seek_replay(self, position):
        if self.replay is not None:
            self.replay_elapsed = self.replay.duration_s*position/1000
            self._replay_tick = time.monotonic()
            self._render_replay()

    def closeEvent(self, event):
        if self._shutdown_done and not self.runner.pending:
            self.timer.stop()
            event.accept()
            return
        event.ignore()
        if self._close_requested:
            return
        job = self.snapshot.get("job") or {}
        active = job.get("status") in BUSY or self._start_pending or self.snapshot.get("preparing")
        if active:
            answer = QtWidgets.QMessageBox.question(self, "停止实验并退出", "实验仍在采集、初始化或保存。停止收发并等待保存完成后退出？",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self._close_requested = True
        self.replay_running = False
        self.runner.closing = True
        self._set_active(False)
        self._message("正在停止后台工作并保存，完成后自动关闭。")
        self._finish_close()

    def _finish_close(self):
        if not self._close_requested:
            return
        if not self._shutdown_submitted:
            def done(value):
                self._shutdown_done = True
                QtCore.QTimer.singleShot(0, self._finish_close)
            def failed(error):
                self._close_requested = False
                self._shutdown_submitted = False
                self.runner.closing = False
                self._error(f"关闭时发生错误，请检查设备状态：{error}")
            self._shutdown_submitted = self.runner.submit("shutdown", self.service.shutdown, done, failed, "urgent")
        if self._shutdown_done and not self.runner.pending:
            self.close()
