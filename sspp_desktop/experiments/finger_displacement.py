"""同次手指实验的人工定位、稳定窗、独立验证和毫米估计工作区。

所有设备与文件操作通过信号交给 MainWindow 的有界后台任务。这里不打开硬件。
"""
from __future__ import annotations

import json
import math
import uuid

import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets
from ..qt_cleanup import close_plot_widgets

ROLES = {"calibration": "标定", "validation": "独立验证", "observation": "观察", "action": "动作"}
ACTIONS = [("保持", "hold"), ("靠近", "near"), ("远离", "far")]


def text_label(text, role=None):
    widget = QtWidgets.QLabel(text)
    widget.setWordWrap(True)
    if role:
        widget.setObjectName(role)
    return widget


def number(value, minimum=0, maximum=1000, suffix=" mm"):
    widget = QtWidgets.QDoubleSpinBox()
    widget.setDecimals(2)
    widget.setRange(minimum, maximum)
    widget.setValue(value)
    widget.setSuffix(suffix)
    widget.setKeyboardTracking(False)
    return widget


class FingerDisplacementWidget(QtWidgets.QWidget):
    begin_requested = QtCore.Signal(dict)
    cancel_requested = QtCore.Signal()
    action_requested = QtCore.Signal(dict)
    analyze_requested = QtCore.Signal(dict)
    current_requested = QtCore.Signal()
    export_requested = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source_key = None
        self.run_key = None
        self.records = []
        self._server_annotations = None
        self._dirty = False
        self._capture_ready = False
        self._active = False
        self._pending = None
        self._submitted = False
        self._analysis_busy = False
        self._plan = []
        self._step = 0
        self._awaited_id = None
        self._export_path = None
        self._duration = 180.0
        self.analysis = None
        self._build()
        self._availability()

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        source_row = QtWidgets.QHBoxLayout()
        self.source_label = text_label("当前手指实验 · 尚未开始采集", "viewBanner")
        source_row.addWidget(self.source_label, 1)
        self.current_button = QtWidgets.QPushButton("使用当前实验")
        self.current_button.clicked.connect(self.current_requested)
        source_row.addWidget(self.current_button)
        layout.addLayout(source_row)
        layout.addWidget(text_label(
            "尺量距离是标注真值。引导与自由记录使用同一连续幅相采集；每次移动都由你完成并确认。"
            "标定与独立验证通过后，才显示本次会话有效范围内的毫米估计。", "muted"))
        self.estimate_label = text_label("距离 — mm    相对初始位移 — mm", "status")
        self.estimate_reason = text_label("尚未建立并验证本次模型。", "muted")
        layout.addWidget(self.estimate_label)
        layout.addWidget(self.estimate_reason)
        self.pages = QtWidgets.QTabWidget()
        layout.addWidget(self.pages, 1)
        self.pages.addTab(self._build_capture(), "① 现场采点")
        self.pages.addTab(self._build_intervals(), "② 区间与标注")
        self.pages.addTab(self._build_report(), "③ 标定与验证报告")

    def _build_settings(self):
        # 设置保持为一个组件：独立使用时嵌在采点页，主窗口可取出放入左侧。
        self.settings_widget = QtWidgets.QWidget()
        self.settings_widget.setObjectName("fingerSettings")
        self.settings_widget.setMinimumWidth(0)
        self.settings_widget.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                                           QtWidgets.QSizePolicy.Policy.Maximum)
        settings_layout = QtWidgets.QVBoxLayout(self.settings_widget)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.setSpacing(10)
        settings = QtWidgets.QGroupBox("尺量参考与窗口")
        form = QtWidgets.QFormLayout(settings)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self.initial_distance = number(10)
        self.tolerance = number(2, .01, 100)
        self.settle = number(2, 0, 30, " s")
        self.hold = number(2, .5, 30, " s")
        form.addRow("初始尺量距离", self.initial_distance)
        form.addRow("验证允许误差", self.tolerance)
        form.addRow("移动后等待", self.settle)
        form.addRow("稳定采样时长", self.hold)
        form.addRow(text_label("位移 = 估计距离 − 初始尺量距离；正值表示远离参考表面。", "muted"))
        settings_layout.addWidget(settings)
        method_group = QtWidgets.QGroupBox("操作方式")
        method_layout = QtWidgets.QVBoxLayout(method_group)
        self.method = QtWidgets.QComboBox()
        self.method.addItem("引导采点 · 按计划逐个确认", "guided")
        self.method.addItem("自由记录 · 自选位置与动作", "free")
        self.method.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.method.setMinimumContentsLength(12)
        self.method.currentIndexChanged.connect(self._method_changed)
        method_layout.addWidget(self.method)
        settings_layout.addWidget(method_group)
        self.guided_settings = QtWidgets.QGroupBox("引导距离计划")
        gf = QtWidgets.QFormLayout(self.guided_settings)
        gf.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        gf.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self.calibration_plan = QtWidgets.QLineEdit("10, 20, 30")
        self.validation_plan = QtWidgets.QLineEdit("15, 25")
        self.calibration_plan.setMinimumWidth(0)
        self.validation_plan.setMinimumWidth(0)
        gf.addRow("标定距离 mm", self.calibration_plan)
        gf.addRow("独立验证距离 mm", self.validation_plan)
        gf.addRow(text_label("距离用逗号分隔。示例不表示已知有效范围；标定与验证使用不同稳定区间。", "muted"))
        settings_layout.addWidget(self.guided_settings)
        self._settings_taken = False
        return self.settings_widget

    def take_settings_widget(self):
        """取出可放在约 340px 左栏的参数组件；重复调用返回同一组件。

        未调用时设置保留在现场采点页。调用方应将返回值加入自己的布局并
        显示；字段及信号仍由本工作区持有，采点/取消/状态留在主工作区。
        """
        if not self._settings_taken:
            self._capture_layout.removeWidget(self.settings_widget)
            self.settings_widget.setParent(None)
            self._settings_taken = True
            self.settings_location_label.show()
        return self.settings_widget

    def _build_capture(self):
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        self._capture_layout = outer
        outer.addWidget(self._build_settings())
        self.settings_location_label = text_label(
            "在左侧设置尺量参考、窗口和距离计划；定位完成后在这里确认采点。", "muted")
        self.settings_location_label.hide()
        outer.addWidget(self.settings_location_label)
        self.guided = QtWidgets.QGroupBox("按计划采点 · 不控制机械移动")
        gf = QtWidgets.QVBoxLayout(self.guided)
        self.plan_button = QtWidgets.QPushButton("采用计划 / 从第一个位置开始")
        self.plan_button.clicked.connect(self.use_plan)
        gf.addWidget(self.plan_button)
        self.step_label = text_label("先采用计划，再手动定位。", "viewBanner")
        gf.addWidget(self.step_label)
        outer.addWidget(self.guided)
        self.free = QtWidgets.QGroupBox("自由采点")
        ff = QtWidgets.QFormLayout(self.free)
        self.distance = number(10)
        self.role = QtWidgets.QComboBox()
        self.role.addItem("标定窗口", "calibration")
        self.role.addItem("独立验证窗口", "validation")
        self.role.addItem("观察窗口（不参与拟合）", "observation")
        self.action = QtWidgets.QComboBox()
        for title, key in ACTIONS:
            self.action.addItem(title, key)
        self.action_note = QtWidgets.QLineEdit()
        self.action_note.setMaxLength(80)
        self.action_note.setPlaceholderText("可选操作说明")
        ff.addRow("当前尺量距离", self.distance)
        ff.addRow("窗口用途", self.role)
        ff.addRow("动作", self.action)
        ff.addRow("说明", self.action_note)
        self.action_button = QtWidgets.QPushButton("记录此刻动作与尺量距离")
        self.action_button.clicked.connect(self._record_action)
        ff.addRow(self.action_button)
        ff.addRow(text_label("动作标记不自动进入模型。需要稳定样本时，定位完成后再采集窗口。", "muted"))
        self.action_log = QtWidgets.QPlainTextEdit()
        self.action_log.setReadOnly(True)
        self.action_log.setMaximumHeight(85)
        self.action_log.setPlaceholderText("已记录动作及采集进程确认时刻")
        ff.addRow(self.action_log)
        outer.addWidget(self.free)
        self.free.hide()
        controls = QtWidgets.QHBoxLayout()
        self.capture_button = QtWidgets.QPushButton("已用尺定位，开始稳定窗口")
        self.capture_button.setObjectName("primary")
        self.capture_button.clicked.connect(self.request_capture)
        self.cancel_button = QtWidgets.QPushButton("取消当前窗口")
        self.cancel_button.clicked.connect(self.cancel_requested)
        controls.addWidget(self.capture_button, 1)
        controls.addWidget(self.cancel_button)
        outer.addLayout(controls)
        self.progress = text_label("先在左侧开始手指实验，静止参考通过后可采点。", "notice")
        outer.addWidget(self.progress)
        outer.addStretch()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _build_intervals(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        layout.addWidget(text_label("已保存会话可拖动阴影选区，或输入起止秒数；为每个区间填写尺量距离和用途。原始 IQ 不改写。", "muted"))
        self.interval_plot = pg.PlotWidget(title="本次相对相位与区间选择")
        self.interval_plot.setLabel("bottom", "实验时间", units="s")
        self.interval_plot.setLabel("left", "相对相位", units="°")
        self.interval_plot.setMinimumHeight(210)
        self.interval_plot.showGrid(x=True, y=True, alpha=.15)
        self.interval_line = self.interval_plot.plot(pen=pg.mkPen("#087f85", width=1.5), connect="finite")
        self.region = pg.LinearRegionItem((8, 10), brush=pg.mkBrush(8, 127, 133, 35))
        self.interval_plot.addItem(self.region)
        self.region.sigRegionChangeFinished.connect(self._region_changed)
        layout.addWidget(self.interval_plot, 2)
        self.editor = QtWidgets.QWidget()
        editor_layout = QtWidgets.QVBoxLayout(self.editor)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        self.interval_start = number(8, 0, 3600, " s")
        self.interval_end = number(10, 0, 3600, " s")
        self.interval_distance = number(10)
        self.interval_role = QtWidgets.QComboBox()
        self.interval_role.addItem("标定", "calibration")
        self.interval_role.addItem("独立验证", "validation")
        self.interval_role.addItem("观察", "observation")
        self.interval_action = QtWidgets.QComboBox()
        for title, key in ACTIONS:
            self.interval_action.addItem(title, key)
        for caption, widget in [("起", self.interval_start), ("止", self.interval_end), ("尺量", self.interval_distance)]:
            row.addWidget(text_label(caption))
            row.addWidget(widget)
        row.addWidget(self.interval_role)
        row.addWidget(self.interval_action)
        editor_layout.addLayout(row)
        self.interval_start.valueChanged.connect(self._editor_region)
        self.interval_end.valueChanged.connect(self._editor_region)
        actions = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton("添加区间")
        self.replace_button = QtWidgets.QPushButton("替换选中区间")
        self.remove_button = QtWidgets.QPushButton("删除选中标注")
        self.add_button.clicked.connect(lambda: self.add_interval(False))
        self.replace_button.clicked.connect(lambda: self.add_interval(True))
        self.remove_button.clicked.connect(self.remove_interval)
        for widget in (self.add_button, self.replace_button, self.remove_button):
            actions.addWidget(widget)
        actions.addStretch()
        editor_layout.addLayout(actions)
        layout.addWidget(self.editor)
        self.table = QtWidgets.QTableWidget(0, 6)
        self.table.setMinimumHeight(150)
        self.table.setHorizontalHeaderLabels(["用途", "尺量 mm", "起点 s", "终点 s", "动作", "状态"])
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selected_interval)
        layout.addWidget(self.table, 2)
        self.interval_note = text_label("尚无稳定区间。现场完成窗口后会自动加入；停止后也可从保存曲线补充标注。", "muted")
        layout.addWidget(self.interval_note)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def _build_report(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        layout.addWidget(text_label("仅用标定区间拟合，再用独立验证区间检验。失败或超出有效范围时保留原因，不显示虚构毫米数。", "muted"))
        row = QtWidgets.QHBoxLayout()
        self.analyze_button = QtWidgets.QPushButton("分析标定与独立验证 / 保存结果")
        self.analyze_button.setObjectName("primary")
        self.analyze_button.clicked.connect(self.request_analysis)
        self.export_button = QtWidgets.QPushButton("打开本次分析文件")
        self.export_button.clicked.connect(lambda: self.export_requested.emit(str(self._export_path)) if self._export_path else None)
        row.addWidget(self.analyze_button, 1)
        row.addWidget(self.export_button)
        layout.addLayout(row)
        self.report_summary = text_label("尚未分析；至少需要多个标定位置与独立验证窗口。", "viewBanner")
        layout.addWidget(self.report_summary)
        charts = QtWidgets.QHBoxLayout()
        self.calibration_plot = pg.PlotWidget(title="标定响应 · 尺量距离与相对相位")
        self.calibration_plot.setLabel("bottom", "尺量距离（mm）")
        self.calibration_plot.setLabel("left", "相对相位（°）")
        self.validation_plot = pg.PlotWidget(title="独立验证 · 实测与估计距离")
        self.validation_plot.setLabel("bottom", "尺量距离（mm）")
        self.validation_plot.setLabel("left", "估计距离（mm）")
        self.calibration_line = self.calibration_plot.plot(pen="#087f85", symbol="o", symbolBrush="#087f85")
        self.validation_line = self.validation_plot.plot(pen=None, symbol="o", symbolBrush="#c17b26", symbolSize=8)
        self.identity_line = self.validation_plot.plot(pen=pg.mkPen("#a5b6c0", style=QtCore.Qt.PenStyle.DashLine))
        for chart in (self.calibration_plot, self.validation_plot):
            chart.setMinimumHeight(220)
            chart.showGrid(x=True, y=True, alpha=.15)
            charts.addWidget(chart)
        layout.addLayout(charts, 1)
        self.report_text = QtWidgets.QPlainTextEdit()
        self.report_text.setReadOnly(True)
        self.report_text.setMinimumHeight(150)
        self.report_text.setPlaceholderText("标定范围、拟合与验证误差、质量拒绝原因将在此显示。")
        layout.addWidget(self.report_text, 1)
        self.report_path = text_label("分析结果作为派生文件保存，不覆盖原始实验。", "muted")
        self.report_path.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.report_path)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    @staticmethod
    def parse_distances(text):
        values = [float(value.strip()) for value in text.replace("，", ",").split(",") if value.strip()]
        if not values or any(not math.isfinite(value) or not 0 <= value <= 1000 for value in values):
            raise ValueError("距离列表应为 0–1000 mm 的有限数值，用逗号分隔。")
        return values

    def use_plan(self):
        try:
            calibration = self.parse_distances(self.calibration_plan.text())
            validation = self.parse_distances(self.validation_plan.text())
            if len(set(calibration)) < 3 or len(set(validation)) < 2:
                raise ValueError("请计划至少 3 个不同标定位置和 2 个不同验证位置。")
            if min(validation) < min(calibration) or max(validation) > max(calibration):
                raise ValueError("验证位置应位于标定距离范围内；范围外不外推毫米估计。")
            self._plan = [("calibration", d) for d in calibration] + [("validation", d) for d in validation]
            self._step = 0
            self._awaited_id = None
            self._show_step()
        except ValueError as exc:
            self.progress.setText(str(exc))
        self._availability()

    def _show_step(self):
        if not self._plan:
            self.step_label.setText("先采用计划，再手动定位。")
        elif self._step >= len(self._plan):
            self.step_label.setText("计划已完成。请在报告页运行标定与独立验证；后续仍可自由采点。")
        else:
            role, distance = self._plan[self._step]
            self.step_label.setText(f"第 {self._step+1}/{len(self._plan)} 步 · {ROLES[role]} · 请手动定位到 {distance:g} mm，再确认采集。")

    def _method_changed(self):
        guided = self.method.currentData() == "guided"
        self.guided.setVisible(guided)
        self.guided_settings.setVisible(guided)
        self.free.setVisible(not guided)
        self._availability()

    def request_capture(self):
        if not self.capture_button.isEnabled():
            return
        if self.method.currentData() == "guided":
            role, distance = self._plan[self._step]
            action = "hold"
        else:
            role, distance, action = self.role.currentData(), self.distance.value(), self.action.currentData()
        self.set_request_busy(True)
        self.begin_requested.emit(dict(distance_mm=distance, role=role, action=action,
                                       settle_s=self.settle.value(), hold_s=self.hold.value(), research_settings=self.research_settings()))

    def research_settings(self):
        return {"initial_distance_mm": self.initial_distance.value(), "tolerance_mm": self.tolerance.value(),
                "workflow": self.method.currentData()}

    def _record_action(self):
        if self.action_button.isEnabled():
            self.set_request_busy(True)
            self.action_requested.emit(dict(distance_mm=self.distance.value(), action=self.action.currentData(),
                                            label=self.action_note.text().strip(), contact=False,
                                            settle_s=self.settle.value(), research_settings=self.research_settings()))

    def set_request_busy(self, busy):
        self._submitted = busy
        self._availability()

    def capture_result(self, snapshot):
        self._submitted = False
        self._awaited_id = (snapshot.get("pending") or {}).get("id") if self.method.currentData() == "guided" else None
        self.apply_snapshot(snapshot)

    def show_error(self, error):
        self._submitted = self._analysis_busy = False
        self.progress.setText(str(error))
        self.report_summary.setText(str(error))
        self._availability()

    def set_acquisition(self, active, ready):
        self._active, self._capture_ready = bool(active), bool(ready)
        self._availability()

    def use_current(self):
        self.source_key = None
        self.run_key = None
        self._server_annotations = None
        self._dirty = False
        self._submitted = self._analysis_busy = False
        self.records = []
        self.analysis = None
        self._export_path = None
        self.calibration_line.setData([], [])
        self.validation_line.setData([], [])
        self.identity_line.setData([], [])
        self.interval_line.setData([], [])
        self.estimate_label.setText("距离 — mm    相对初始位移 — mm")
        self.estimate_reason.setText("当前实验尚未建立并验证模型。")
        self._duration = 180.0
        self.region.setBounds((0, self._duration))
        for widget in (self.interval_start, self.interval_end):
            widget.setMaximum(self._duration)
        self._render_table()
        self.report_text.clear()
        self.report_summary.setText("当前实验 · 尚未分析。")
        self.source_label.setText("当前手指实验 · 等待状态更新")
        self._availability()

    def load_history(self, payload, curve=None):
        settings = payload.get("settings") or {}
        if settings.get("initial_distance_mm") is not None:
            self.initial_distance.setValue(settings["initial_distance_mm"])
        if settings.get("tolerance_mm") is not None:
            self.tolerance.setValue(settings["tolerance_mm"])
        self.source_key = payload["run_key"]
        self.run_key = self.source_key
        self.records = [dict(row) for row in payload.get("annotations", [])]
        self._dirty = False
        self._submitted = self._analysis_busy = False
        self._pending = None
        self._duration = float(payload.get("duration_s") or 180)
        for widget in (self.interval_start, self.interval_end):
            widget.setMaximum(self._duration)
        self.region.setBounds((0, self._duration))
        self.source_label.setText(f"历史手指实验 · {self.source_key} · 标注与分析，不启动硬件")
        self.estimate_label.setText("历史记录 · 距离 — mm    相对初始位移 — mm")
        self.estimate_reason.setText("历史分析不会用于当前采集的实时毫米估计。")
        self._render_table()
        self._render_actions(payload.get("actions", []))
        self.update_curve(curve or {})
        self.apply_analysis(payload.get("analysis") or {})
        self.pages.setCurrentIndex(1)
        self._availability()

    def apply_snapshot(self, snapshot):
        if self.source_key is not None:
            return
        incoming_key = snapshot.get("run_key")
        if incoming_key and not incoming_key.startswith("phase_runs/"):
            incoming_key = None
        if incoming_key != self.run_key:
            self.run_key = incoming_key
            self.records = []
            self._dirty = False
            self._server_annotations = None
            self._step = 0
            self._awaited_id = None
            self.analysis = None
            self._export_path = None
            self._analysis_busy = False
            self.calibration_line.setData([], [])
            self.validation_line.setData([], [])
            self.identity_line.setData([], [])
            self.report_text.clear()
            self.report_summary.setText("新会话需重新标定与独立验证。")
            self._show_step()
        self.source_label.setText(f"当前手指实验 · {incoming_key or '尚未开始'}")
        incoming = snapshot.get("annotations") or []
        self._render_actions(snapshot.get("actions", []))
        signature = json.dumps(incoming, sort_keys=True, ensure_ascii=False)
        if signature != self._server_annotations:
            if not self._dirty:
                self.records = [dict(row) for row in incoming]
            self._server_annotations = signature
            self._render_table()
        self._pending = snapshot.get("pending")
        if self._awaited_id and not self._pending:
            if any(row.get("id") == self._awaited_id and row.get("state", "complete") == "complete" for row in incoming):
                self._step += 1
                self._show_step()
            self._awaited_id = None
        if self._pending:
            pending = self._pending
            state = {"awaiting_ack": "等待采集进程确认", "settling": "移动后等待", "capturing": "采集稳定窗口"}.get(pending.get("state"), pending.get("state", "处理中"))
            self.progress.setText(f"{state} · {ROLES.get(pending.get('role'), '')} {pending.get('distance_mm', '—')} mm · 请保持不动。")
        elif snapshot.get("error"):
            self.progress.setText(str(snapshot["error"]))
        elif self._capture_ready:
            self.progress.setText("静止参考已通过。手动定位并确认后才会开始窗口，软件不移动手指。")
        else:
            self.progress.setText("采点需当前 phase 实验正在运行且静止参考通过。已保存数据可在区间页标注。")
        estimate = snapshot.get("estimate") or {}
        if estimate.get("ok", estimate.get("valid", False)) and all(isinstance(estimate.get(k), (int, float)) and math.isfinite(estimate[k]) for k in ("distance_mm", "displacement_mm")):
            self.estimate_label.setText(f"估计距离 {estimate['distance_mm']:.2f} mm    相对初始位移 {estimate['displacement_mm']:+.2f} mm")
            initial = (snapshot.get("analysis") or {}).get("initial_distance_mm", "—")
            self.estimate_reason.setText(f"同次已验证模型 · 模型初始距离 {initial} mm · 数据时刻 {estimate.get('t_s', snapshot.get('t_s', '—'))} s")
        else:
            self.estimate_label.setText("距离 — mm    相对初始位移 — mm")
            self.estimate_reason.setText(str(estimate.get("error") or estimate.get("reason") or "尚未建立并验证本次模型。"))
        if snapshot.get("analysis") and snapshot["analysis"] != self.analysis:
            self.apply_analysis(snapshot["analysis"])
        elif not snapshot.get("analysis") and self.analysis:
            self.apply_analysis({})
        self._availability()

    def update_curve(self, live):
        if live.get("mode") != "phase":
            self.interval_line.setData([], [])
            return
        rows = live.get("points") or []
        self.interval_line.setData([r.get("x", float("nan")) for r in rows],
                                   [float("nan") if r.get("phase") is None else r["phase"] for r in rows], connect="finite")
        if rows and isinstance(rows[-1].get("x"), (float, int)):
            self._duration = max(self._duration, rows[-1]["x"])

    def _render_actions(self, actions):
        lines = [f"{row.get('t_s') if row.get('t_s') is not None else '等待确认'} s · {row.get('action', '')} · {row.get('distance_mm', '—')} mm · {row.get('note', '')}" for row in actions[-20:]]
        text = "\n".join(lines)
        if self.action_log.toPlainText() != text:
            self.action_log.setPlainText(text)

    def _region_changed(self):
        start, end = self.region.getRegion()
        for widget, value in ((self.interval_start, start), (self.interval_end, end)):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)

    def _editor_region(self):
        self.region.setRegion((self.interval_start.value(), self.interval_end.value()))

    def add_interval(self, replace=False):
        start, end = self.interval_start.value(), self.interval_end.value()
        if start >= end:
            self.interval_note.setText("区间终点必须晚于起点。")
            return
        if not self.editor.isEnabled():
            return
        row = dict(id=str(uuid.uuid4()), start_s=start, end_s=end, distance_mm=self.interval_distance.value(),
                   role=self.interval_role.currentData(), action=self.interval_action.currentData(), state="complete", source="manual_interval")
        selected = self.table.currentRow()
        if replace:
            if not 0 <= selected < len(self.records):
                self.interval_note.setText("请先选择要替换的区间。")
                return
            self.records[selected] = row
        else:
            self.records.append(row)
        self._dirty = True
        self.report_summary.setText("标注已修改；请重新分析，旧报告不代表当前标注。")
        self._render_table()
        self.interval_note.setText("标注已修改，尚未保存；在报告页分析后与派生结果一起保存。")
        self._availability()

    def remove_interval(self):
        selected = self.table.currentRow()
        if self.editor.isEnabled() and 0 <= selected < len(self.records):
            self.records.pop(selected)
            self._dirty = True
            self.report_summary.setText("标注已修改；请重新分析。")
            self._render_table()
            self.interval_note.setText("标注已修改，尚未保存；原始记录不变。")
            self._availability()

    def _selected_interval(self):
        selected = self.table.currentRow()
        if not 0 <= selected < len(self.records):
            return
        row = self.records[selected]
        for widget, field in ((self.interval_start, "start_s"), (self.interval_end, "end_s"), (self.interval_distance, "distance_mm")):
            if isinstance(row.get(field), (int, float)):
                widget.setValue(row[field])
        for widget, field in ((self.interval_role, "role"), (self.interval_action, "action")):
            index = widget.findData(row.get(field))
            if index >= 0:
                widget.setCurrentIndex(index)

    def _render_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.records))
        for index, row in enumerate(self.records):
            values = [ROLES.get(row.get("role"), row.get("role", "")), row.get("distance_mm", "—"), row.get("start_s", row.get("t_s", "—")),
                      row.get("end_s", "—"), dict((key, title) for title, key in ACTIONS).get(row.get("action"), row.get("action", "")),
                      "完成" if row.get("state", "complete") == "complete" else row.get("state", "")]
            for column, value in enumerate(values):
                self.table.setItem(index, column, QtWidgets.QTableWidgetItem(f"{value:.3f}" if isinstance(value, float) else str(value)))
        self.table.blockSignals(False)

    def request_analysis(self):
        if not self.analyze_button.isEnabled():
            return
        self._analysis_busy = True
        self.report_summary.setText("正在后台提取稳定窗口、拟合并检验独立验证数据…")
        self._availability()
        self.analyze_requested.emit(dict(initial_distance_mm=self.initial_distance.value(), tolerance_mm=self.tolerance.value(),
                                         key=self.source_key, annotations=[dict(row) for row in self.records]))

    def apply_analysis(self, analysis):
        self._analysis_busy = False
        self.analysis = analysis or None
        self._export_path = (analysis or {}).get("export_path")
        if not analysis:
            self.report_summary.setText("尚未分析；标定与验证使用独立区间。")
            self.report_text.clear()
            self.calibration_line.setData([], [])
            self.validation_line.setData([], [])
            self.identity_line.setData([], [])
            self.report_path.setText("分析结果作为派生文件保存，不覆盖原始实验。")
            self._availability()
            return
        model = analysis.get("model") or {}
        active = bool(model.get("active"))
        validation = analysis.get("validation") or {}
        calibration = analysis.get("calibration") or {}
        nodes = calibration.get("nodes") or []
        self.calibration_line.setData([r["distance_mm"] for r in nodes], [r["phase_deg"] for r in nodes])
        valid_rows = [row for row in validation.get("windows", []) if isinstance(row.get("estimated_distance_mm"), (int, float))]
        self.validation_line.setData([r["distance_mm"] for r in valid_rows], [r["estimated_distance_mm"] for r in valid_rows])
        bounds = calibration.get("distance_range_mm")
        self.identity_line.setData(bounds or [], bounds or [])
        detail = f"范围 {bounds} mm · 最大验证误差 {validation.get('max_error_mm', '—')} mm · RMSE {validation.get('rmse_mm', '—')} mm"
        if not active:
            detail = "；".join(str(error) for error in analysis.get("errors", [])) or detail
        self.report_summary.setText(("验证通过 · 仅限本次会话\n" if active else "模型未启用\n") + detail)
        report = analysis.get("report") or {key: analysis.get(key) for key in ("status", "errors", "warnings", "calibration", "validation", "model", "windows") if key in analysis}
        self.report_text.setPlainText(report if isinstance(report, str) else json.dumps(report, ensure_ascii=False, indent=2))
        self.report_path.setText(f"派生结果：{self._export_path}" if self._export_path else "尚无导出路径。")
        if isinstance(analysis.get("initial_distance_mm"), (int, float)):
            self.initial_distance.setValue(analysis["initial_distance_mm"])
        if isinstance(analysis.get("tolerance_mm"), (int, float)):
            self.tolerance.setValue(analysis["tolerance_mm"])
        self._dirty = False
        self._availability()

    def _availability(self):
        current = self.source_key is None
        ready = current and self._capture_ready and not self._pending and not self._submitted and not self._analysis_busy
        guided_ready = self.method.currentData() != "guided" or bool(self._plan) and self._step < len(self._plan)
        self.capture_button.setEnabled(ready and guided_ready)
        self.action_button.setEnabled(ready)
        self.cancel_button.setEnabled(current and bool(self._pending) and not self._submitted)
        self.method.setEnabled(not self._pending and not self._submitted)
        self.plan_button.setEnabled(not self._pending and not self._submitted)
        self.editor.setEnabled(bool(self.run_key) and not (current and self._active) and not self._analysis_busy)
        self.region.setMovable(self.editor.isEnabled())
        self.analyze_button.setEnabled(bool(self.run_key) and bool(self.records) and not self._pending and not self._analysis_busy)
        self.export_button.setEnabled(bool(self._export_path))
        self.initial_distance.setEnabled(not self._analysis_busy)
        self.tolerance.setEnabled(not self._analysis_busy)

    def closeEvent(self, event):
        close_plot_widgets(self)
        super().closeEvent(event)
