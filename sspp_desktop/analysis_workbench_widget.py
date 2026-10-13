"""现有实验软件中的离线研究分析工作台，直接引用采集正本目录。"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from .analysis_bridge import AnalysisBridge, analysis_defaults
from .plots import DiagnosticPlot
from .qt_cleanup import close_plot_widgets

TASKS = {"material": ("材料分类", "sweep"), "finger": ("手指距离／位移", "phase"),
         "sensor": ("温压响应／标定", "sensor")}
OPERATIONS = {
    "material": [("响应曲线", "response"), ("训练并独立验证", "material_train"), ("加载模型预测", "material_predict")],
    "finger": [("幅相响应曲线", "response"), ("同次标定与留出验证", "finger_calibrate"), ("加载同次模型验证／估距", "finger_validate")],
    "sensor": [("原始响应曲线", "response"), ("拟合标定并验证", "sensor_calibrate"), ("加载模型独立验证", "sensor_validate"), ("加载模型转换响应", "sensor_predict")],
}


def finite_number(text, label):
    try:
        value = float(str(text).strip())
    except (ValueError, TypeError) as exc:
        raise ValueError(f"请填写 {label}（有限数值）") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label} 必须是有限数值")
    return value


def _read_only(text):
    item = QtWidgets.QTableWidgetItem(str(text or ""))
    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
    return item


def result_preview(value, budget=None, depth=0):
    """Qt 文本视图只放有限预览；完整结果始终在后台保存的 result.json。"""
    budget = [2000] if budget is None else budget
    if budget[0] <= 0 or depth > 10:
        return "…完整内容见结果文件"
    budget[0] -= 1
    if isinstance(value, list):
        preview = [result_preview(item, budget, depth + 1) for item in value[:100]]
        if len(value) > 100:
            preview.append(f"…本列表共 {len(value)} 项，完整内容见结果文件")
        return preview
    if isinstance(value, dict):
        return {key: result_preview(item, budget, depth + 1) for key, item in list(value.items())[:100]}
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "…"
    return value


class AnalysisWorkbenchWidget(QtWidgets.QWidget):
    busy_changed = QtCore.Signal(bool)

    def __init__(self, root: Path, analysis_project=None, python_executable=None, parent=None, bridge=None):
        super().__init__(parent)
        self.root = Path(root).resolve()
        project, python = analysis_defaults(self.root)
        self.bridge = bridge or AnalysisBridge(analysis_project or project, python_executable or python, self)
        self.sources = []
        self.models = []
        self.result = None
        self.selected = set()
        self._labels = {}
        self._roles = {}
        self._annotations = {"finger": {}, "sensor": {}}
        self._settings = {"finger": {}, "sensor": {}}
        self._annotation_sources = {"finger": [], "sensor": []}
        self._rendering = False
        self._action = None
        self._refresh_pending = False
        self._select_pending = None
        self._acquisition_busy = False
        self._closing = False
        self._build_ui()
        self.bridge.succeeded.connect(self._on_result)
        self.bridge.failed.connect(self._on_error)
        self.bridge.busy_changed.connect(self._busy_changed)
        QtCore.QTimer.singleShot(0, self.refresh_sources)

    @property
    def is_running(self):
        return self.bridge.is_running

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        self.intro = QtWidgets.QLabel("采集正本直接分析，无需导入或搬移。标注、结果和模型按版本存入 rf-analysis/analysis_runs；原始数据只读。")
        self.intro.setWordWrap(True)
        layout.addWidget(self.intro)
        environment = QtWidgets.QGroupBox("分析环境（使用已有 rf-analysis Python）")
        environment.setCheckable(True)
        environment.setChecked(False)
        environment_layout = QtWidgets.QGridLayout(environment)
        self.project_path = QtWidgets.QLineEdit(str(self.bridge.project))
        self.python_path = QtWidgets.QLineEdit(str(self.bridge.python_executable))
        project_browse = QtWidgets.QPushButton("选择项目")
        python_browse = QtWidgets.QPushButton("选择 Python")
        project_browse.clicked.connect(self._choose_project)
        python_browse.clicked.connect(self._choose_python)
        environment_layout.addWidget(QtWidgets.QLabel("项目"), 0, 0)
        environment_layout.addWidget(self.project_path, 0, 1)
        environment_layout.addWidget(project_browse, 0, 2)
        environment_layout.addWidget(QtWidgets.QLabel("解释器"), 1, 0)
        environment_layout.addWidget(self.python_path, 1, 1)
        environment_layout.addWidget(python_browse, 1, 2)
        layout.addWidget(environment)
        self.environment_group = environment

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        sources_page = QtWidgets.QWidget()
        sources_layout = QtWidgets.QVBoxLayout(sources_page)
        sources_layout.setContentsMargins(0, 0, 0, 0)
        source_bar = QtWidgets.QHBoxLayout()
        self.task = QtWidgets.QComboBox()
        for key, (label, _) in TASKS.items():
            self.task.addItem(label, key)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("查找名称／实验编号")
        self.refresh_button = QtWidgets.QPushButton("刷新实验")
        self.refresh_button.clicked.connect(self.refresh_sources)
        source_bar.addWidget(self.task)
        source_bar.addWidget(self.search, 1)
        source_bar.addWidget(self.refresh_button)
        sources_layout.addLayout(source_bar)
        self.source_table = QtWidgets.QTableWidget(0, 6)
        self.source_table.setHorizontalHeaderLabels(["选择", "实验／UTC 时间", "状态", "材料标签", "数据角色", "收口检查"])
        self.source_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.source_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.source_table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 45), (2, 75), (3, 110), (4, 112), (5, 110)):
            self.source_table.setColumnWidth(col, width)
        self.source_table.verticalHeader().setDefaultSectionSize(42)
        self.source_table.setMinimumHeight(185)
        self.source_table.itemChanged.connect(self._source_edited)
        sources_layout.addWidget(self.source_table, 1)
        self.source_notice = QtWidgets.QLabel("正在读取本项目 Data 中的实验元数据…")
        self.source_notice.setWordWrap(True)
        sources_layout.addWidget(self.source_notice)
        self.inspect_button = QtWidgets.QPushButton("检查已勾选实验的参数与来源")
        self.inspect_button.clicked.connect(lambda: self.run_action("inspect"))
        sources_layout.addWidget(self.inspect_button)
        splitter.addWidget(sources_page)

        form_page = QtWidgets.QWidget()
        form_layout = QtWidgets.QVBoxLayout(form_page)
        form_layout.setContentsMargins(0, 0, 0, 0)
        self.forms = QtWidgets.QStackedWidget()
        material = QtWidgets.QLabel("为每次独立采集填写材料标签，并指定训练或独立验证。\n至少 2 类；每类至少 1 条训练和 1 条独立验证记录。\n同一 run 的重复扫频不能替代独立采集。\n尚无标签时先看响应曲线，或保存标注草稿。")
        material.setWordWrap(True)
        material.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        self.forms.addWidget(material)
        self.forms.addWidget(self._window_form("finger"))
        self.forms.addWidget(self._window_form("sensor"))
        form_layout.addWidget(self.forms, 1)
        action_bar = QtWidgets.QHBoxLayout()
        self.operation = QtWidgets.QComboBox()
        self.run_button = QtWidgets.QPushButton("执行分析")
        self.run_button.clicked.connect(lambda: self.run_action(self.operation.currentData()))
        self.save_button = QtWidgets.QPushButton("保存标注草稿")
        self.save_button.clicked.connect(lambda: self.run_action("save_annotations"))
        action_bar.addWidget(self.operation, 1)
        action_bar.addWidget(self.run_button)
        action_bar.addWidget(self.save_button)
        form_layout.addLayout(action_bar)
        model_bar = QtWidgets.QHBoxLayout()
        model_bar.addWidget(QtWidgets.QLabel("版本模型"))
        self.model = QtWidgets.QComboBox()
        self.model.setMinimumWidth(130)
        self.model.currentIndexChanged.connect(self._model_changed)
        self.model_browse = QtWidgets.QPushButton("选择模型文件")
        self.model_browse.clicked.connect(self._choose_model)
        model_bar.addWidget(self.model, 1)
        model_bar.addWidget(self.model_browse)
        form_layout.addLayout(model_bar)
        self.advanced = QtWidgets.QPlainTextEdit()
        self.advanced.setPlaceholderText('可选高级参数 JSON，例如 {"quality_policy": {"min_samples": 10}}；标签与参考区间由表格填写。')
        self.advanced.setMaximumHeight(55)
        form_layout.addWidget(self.advanced)
        splitter.addWidget(form_page)
        splitter.setSizes([740, 600])
        layout.addWidget(splitter, 1)

        status_bar = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel("等待选择已完成实验")
        self.status.setWordWrap(True)
        self.cancel_button = QtWidgets.QPushButton("取消分析")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(lambda: self.bridge.cancel())
        self.open_result_button = QtWidgets.QPushButton("打开结果目录")
        self.open_result_button.setEnabled(False)
        self.open_result_button.clicked.connect(self._open_result)
        status_bar.addWidget(self.status, 1)
        status_bar.addWidget(self.cancel_button)
        status_bar.addWidget(self.open_result_button)
        layout.addLayout(status_bar)
        self.result_tabs = QtWidgets.QTabWidget()
        curve_page = QtWidgets.QWidget()
        curve_layout = QtWidgets.QVBoxLayout(curve_page)
        curve_layout.setContentsMargins(0, 0, 0, 0)
        self.chart_choice = QtWidgets.QComboBox()
        self.chart_choice.currentIndexChanged.connect(self._show_chart)
        self.plot = DiagnosticPlot()
        self.plot.plot.setMinimumHeight(180)
        curve_layout.addWidget(self.chart_choice)
        curve_layout.addWidget(self.plot, 1)
        self.result_tabs.addTab(curve_page, "结果曲线")
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.result_tabs.addTab(self.details, "参数／验证／来源")
        self.artifacts = QtWidgets.QListWidget()
        self.artifacts.itemDoubleClicked.connect(self._open_artifact)
        self.result_tabs.addTab(self.artifacts, "结果文件（双击打开）")
        layout.addWidget(self.result_tabs, 1)
        self.task.currentIndexChanged.connect(self._task_changed)
        self.search.textChanged.connect(self._render_sources)
        self._task_changed()

    def _window_form(self, task):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = ("只勾选一次连续幅相采集。填写互不重叠的静止区间 [起点, 终点)，尺量单位 mm；标定与验证分离。\n位移＝距离－初始距离；模型仅绑定同一次采集。" if task == "finger" else
                "填写 ADC／电压与参考仪器读数对应的稳定区间。时间为所选通道的 MCU elapsed 秒；复位后按段分别标注。\n一次拟合需 3 个标定和 2 个独立验证窗口；参考单位明确后才能换算物理量。")
        label = QtWidgets.QLabel(hint)
        label.setWordWrap(True)
        layout.addWidget(label)
        controls = QtWidgets.QHBoxLayout()
        if task == "finger":
            self.initial_distance = QtWidgets.QLineEdit()
            self.initial_distance.setPlaceholderText("实测初始距离")
            self.initial_distance.setMaximumWidth(110)
            self.finger_tolerance = QtWidgets.QDoubleSpinBox()
            self.finger_tolerance.setRange(.001, 1000)
            self.finger_tolerance.setDecimals(3)
            self.finger_tolerance.setValue(2)
            controls.addWidget(QtWidgets.QLabel("初始距离 mm"))
            controls.addWidget(self.initial_distance)
            controls.addWidget(QtWidgets.QLabel("验证门槛 mm"))
            controls.addWidget(self.finger_tolerance)
        else:
            self.sensor_channel = QtWidgets.QComboBox()
            self.sensor_channel.setEditable(True)
            self.sensor_channel.addItems(["S6", "S7", "A2"])
            self.input_field = QtWidgets.QComboBox()
            self.input_field.addItem("ADC 原始计数", "adc")
            self.input_field.addItem("电压 V", "voltage_v")
            self.reference_unit = QtWidgets.QLineEdit()
            self.reference_unit.setPlaceholderText("degC / kPa / N…")
            self.reference_unit.setMaximumWidth(100)
            self.sensor_degree = QtWidgets.QComboBox()
            self.sensor_degree.addItem("一次拟合", 1)
            self.sensor_degree.addItem("二次拟合", 2)
            self.sensor_tolerance = QtWidgets.QDoubleSpinBox()
            self.sensor_tolerance.setRange(.000001, 1e9)
            self.sensor_tolerance.setDecimals(6)
            self.sensor_tolerance.setValue(1)
            controls.addWidget(QtWidgets.QLabel("通道"))
            controls.addWidget(self.sensor_channel)
            controls.addWidget(self.input_field)
            controls.addWidget(self.reference_unit)
            controls.addWidget(self.sensor_degree)
            controls.addWidget(QtWidgets.QLabel("误差门槛"))
            controls.addWidget(self.sensor_tolerance)
        layout.addLayout(controls)
        table = QtWidgets.QTableWidget(0, 6)
        table.setHorizontalHeaderLabels(["来源 run", "起点 s", "终点 s", "角色", "距离 mm" if task == "finger" else "参考值（上方单位）", "动作说明"])
        if task == "sensor":
            table.setColumnCount(7)
            table.setHorizontalHeaderLabels(["来源 run", "MCU 起点 s", "MCU 终点 s", "角色", "参考值（上方单位）", "说明", "复位时间段"])
        table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for col, width in ((1, 76), (2, 76), (3, 100), (4, 100), (5, 90)):
            table.setColumnWidth(col, width)
        if task == "sensor":
            table.setColumnWidth(6, 70)
        table.setMinimumHeight(115)
        setattr(self, f"{task}_annotations", table)
        layout.addWidget(table, 1)
        bar = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("添加参考区间")
        remove = QtWidgets.QPushButton("删除选中区间")
        add.clicked.connect(lambda: self.add_annotation(task))
        remove.clicked.connect(lambda: table.removeRow(table.currentRow()) if table.currentRow() >= 0 else None)
        bar.addWidget(add)
        bar.addWidget(remove)
        bar.addStretch()
        layout.addLayout(bar)
        return page

    def _choose_project(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择 rf-analysis 项目", self.project_path.text())
        if path:
            self.project_path.setText(path)
            self.refresh_sources()

    def _choose_python(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择已有 rf-analysis 环境的 Python", self.python_path.text())
        if path:
            self.python_path.setText(path)
            self.refresh_sources()

    def _choose_model(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "选择版本 model.json", self.project_path.text(), "模型 (model.json *.json)")
        if path:
            path = str(Path(path).resolve())
            index = self.model.findData(path)
            if index < 0:
                self.model.addItem(f"手动选择 · {Path(path).parent.name}", path)
                index = self.model.count() - 1
            self.model.setCurrentIndex(index)

    def _task_changed(self):
        self._stash_annotations()
        task = self.task.currentData()
        self.forms.setCurrentIndex(list(TASKS).index(task))
        self.operation.clear()
        for label, action in OPERATIONS[task]:
            self.operation.addItem(label, action)
        kind = TASKS[task][1]
        valid = {self._capture(row) for row in self.sources if row.get("kind") == kind}
        self.selected &= valid
        self.source_table.setColumnHidden(3, task != "material")
        self.source_table.setColumnHidden(4, task != "material")
        self._render_sources()
        self._render_models()
        self._load_annotation_selection()

    @staticmethod
    def _capture(row):
        return str(row.get("capture") or row.get("run") or "")

    def _render_sources(self):
        self._rendering = True
        task = self.task.currentData()
        text = self.search.text().strip().casefold()
        rows = [row for row in self.sources if row.get("kind") == TASKS[task][1]
                and (not text or text in (self._capture(row) + " " + str(row.get("label", ""))).casefold())]
        self.source_table.setRowCount(len(rows))
        for n, row in enumerate(rows):
            capture = self._capture(row)
            eligible = row.get("eligible") is True
            item = _read_only("")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, capture)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            if not eligible:
                item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEnabled)
                self.selected.discard(capture)
            item.setCheckState(QtCore.Qt.CheckState.Checked if capture in self.selected else QtCore.Qt.CheckState.Unchecked)
            self.source_table.setItem(n, 0, item)
            display = f"{row.get('label') or Path(capture).name}\n{row.get('started_utc', Path(capture).name)}"
            source_item = _read_only(display)
            source_item.setToolTip(capture)
            self.source_table.setItem(n, 1, source_item)
            self.source_table.setItem(n, 2, _read_only(row.get("status")))
            label = QtWidgets.QTableWidgetItem(self._labels.get(capture, row.get("label_override", "")))
            label.setToolTip("人工材料类别；不同 run 可填同一类别，实验名称不自动当作类别。")
            self.source_table.setItem(n, 3, label)
            self.source_table.removeCellWidget(n, 4)
            if task == "material":
                role = QtWidgets.QComboBox()
                role.addItem("训练", "training")
                role.addItem("独立验证", "validation")
                role.addItem("观察／预测", "observation")
                selected_role = self._roles.get(capture, row.get("role_override", "observation"))
                role.setCurrentIndex(max(0, role.findData(selected_role)))
                role.currentIndexChanged.connect(lambda _, p=capture, combo=role: self._roles.__setitem__(p, combo.currentData()))
                self.source_table.setCellWidget(n, 4, role)
            self.source_table.setItem(n, 5, _read_only("可分析" if eligible else row.get("reason", "尚未完整保存或收口")))
        self.source_table.setColumnHidden(3, task != "material")
        self.source_table.setColumnHidden(4, task != "material")
        self._rendering = False
        count = sum(row.get("eligible") is True for row in rows)
        self.source_notice.setText(f"{len(rows)} 条记录，{count} 条可分析；已勾选 {len(self.selected)} 条。" if rows else
                                   "暂无这类采集记录。先在对应实验页采集并完成保存，再刷新；训练／标定需新实验标签和参考值。")
        self._update_controls()

    def _source_edited(self, item):
        if self._rendering:
            return
        capture = self.source_table.item(item.row(), 0).data(QtCore.Qt.ItemDataRole.UserRole)
        if item.column() == 3:
            self._labels[capture] = item.text().strip()
        elif item.column() == 0:
            self._stash_annotations()
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                if self.task.currentData() == "finger" and self.selected and capture not in self.selected:
                    self._rendering = True
                    item.setCheckState(QtCore.Qt.CheckState.Unchecked)
                    self._rendering = False
                    self.status.setText("手指标定仅使用同一次连续采集；请先取消其他 run 的勾选。")
                    return
                self.selected.add(capture)
            else:
                self.selected.discard(capture)
            self._load_annotation_selection()
            self.source_notice.setText(f"已勾选 {len(self.selected)} 条完整收口实验；直接读取原目录。")
            self._update_controls()

    def _render_models(self):
        previous = self.model.currentData()
        self.model.clear()
        self.model.addItem("请选择已生成模型", None)
        task = self.task.currentData()
        for model in self.models:
            if model.get("kind") != task:
                continue
            path = str(model.get("path") or (Path(model["model_dir"]) / "model.json"))
            active = "已激活" if model.get("active") else "未激活／仅拟合"
            self.model.addItem(f"{Path(path).parent.name} · {active}", path)
            self.model.setItemData(self.model.count() - 1, f"{path}\n{model.get('created_utc', '')}", QtCore.Qt.ItemDataRole.ToolTipRole)
        index = self.model.findData(previous)
        if index >= 0:
            self.model.setCurrentIndex(index)

    def _model_changed(self):
        if self.task.currentData() != "sensor":
            return
        info = next((row for row in self.models if str(row.get("path", "")) == self.model.currentData()), None)
        if info is None:
            return
        if info.get("channel"):
            self.sensor_channel.setCurrentText(str(info["channel"]))
        if info.get("reference_unit"):
            self.reference_unit.setText(str(info["reference_unit"]))
        if info.get("input_field"):
            self.input_field.setCurrentIndex(max(0, self.input_field.findData(info["input_field"])))

    def refresh_sources(self):
        if self._closing:
            return
        if self.is_running:
            self._refresh_pending = True
            return
        if self._acquisition_busy:
            self.status.setText("采集／保存正在进行；完成后再刷新分析来源。")
            return
        self._stash_annotations()
        try:
            self.bridge.configure(self.project_path.text(), self.python_path.text())
        except ValueError as exc:
            self._on_error(str(exc))
            return
        self._action = "list"
        self.status.setText("后台读取实验索引与版本模型…")
        self.bridge.start({"action": "list", "captures": [], "parameters": {"desktop_project": str(self.root)}}, timeout_ms=30_000)

    def select_source(self, path_or_key):
        path = Path(path_or_key)
        if not path.is_absolute():
            path = self.root / "Data" / path
        capture = str(path.resolve())
        row = next((row for row in self.sources if self._capture(row) == capture), None)
        if row is None:
            self._select_pending = capture
            self.refresh_sources()
            return
        if not row.get("eligible"):
            self.status.setText(row.get("reason", "该实验尚未完整收口，暂不可分析"))
            return
        self._stash_annotations()
        task = next(key for key, (_, kind) in TASKS.items() if kind == row["kind"])
        self.task.setCurrentIndex(self.task.findData(task))
        self.selected = {capture}
        self.search.clear()
        self._render_sources()
        self._load_annotation_selection()

    def _read_annotations(self, task, strict=False):
        table = getattr(self, f"{task}_annotations")
        rows = []
        for n in range(table.rowCount()):
            capture = table.cellWidget(n, 0).currentData()
            reference_text = table.item(n, 4).text().strip()
            role = table.cellWidget(n, 3).currentData()
            reference = None
            if reference_text:
                try:
                    reference = finite_number(reference_text, f"第 {n + 1} 行参考值")
                except ValueError:
                    if strict:
                        raise
                    reference = reference_text
            elif strict and role != "observation":
                raise ValueError(f"请填写第 {n + 1} 行的实测参考值")
            row = {"id": f"window_{n + 1}", "capture": capture,
                   "start_s": table.cellWidget(n, 1).value(), "end_s": table.cellWidget(n, 2).value(),
                   "role": role, "action": table.item(n, 5).text().strip(),
                   "distance_mm" if task == "finger" else "reference": reference}
            if task == "sensor":
                row["time_segment"] = table.cellWidget(n, 6).value()
            if strict and row["end_s"] <= row["start_s"]:
                raise ValueError(f"第 {n + 1} 行终点必须大于起点")
            rows.append(row)
        return rows

    def _stash_annotations(self):
        if not hasattr(self, "finger_annotations"):
            return
        for task in ("finger", "sensor"):
            grouped = {capture: [] for capture in self._annotation_sources[task]}
            for row in self._read_annotations(task):
                grouped.setdefault(row["capture"], []).append(row)
            self._annotations[task].update(grouped)
            if task == "finger":
                settings = {"initial_distance_mm": self.initial_distance.text().strip(), "tolerance_mm": self.finger_tolerance.value()}
            else:
                settings = {"channel": self.sensor_channel.currentText(), "input_field": self.input_field.currentData(),
                            "reference_unit": self.reference_unit.text().strip(), "degree": self.sensor_degree.currentData(),
                            "tolerance": self.sensor_tolerance.value()}
            for capture in self._annotation_sources[task]:
                self._settings[task][capture] = dict(settings)

    def _load_annotation_selection(self):
        task = self.task.currentData()
        if task == "material":
            return
        captures = sorted(self.selected)
        selected_sources = [row for row in self.sources if self._capture(row) in captures]
        selected_settings = []
        for capture in captures:
            source = next((row for row in selected_sources if self._capture(row) == capture), {})
            saved = {**source.get("annotation_settings", {}), **source.get("annotation_draft", {}).get("parameters", {})}
            selected_settings.append(self._settings[task].get(capture, saved))
        if task == "finger":
            settings = selected_settings[0] if selected_settings else {}
            value = settings.get("initial_distance_mm")
            self.initial_distance.setText("" if value is None else str(value))
            self.finger_tolerance.setValue(float(settings.get("tolerance_mm", 2)))
        elif task == "sensor":
            units = {str(settings["reference_unit"]) for settings in selected_settings if settings.get("reference_unit")}
            self.reference_unit.setText(next(iter(units)) if len(units) == 1 else "")
            if len(units) > 1:
                self.status.setText("所选来源的标注参考单位冲突；请分别标定，或明确填写本次统一参考单位。")
        if task == "sensor":
            channels = sorted({str(channel) for source in selected_sources for channel in source.get("channels", [])})
            if channels:
                previous = self.sensor_channel.currentText()
                self.sensor_channel.clear()
                self.sensor_channel.addItems(channels)
                if previous in channels:
                    self.sensor_channel.setCurrentText(previous)
            settings = selected_settings[0] if selected_settings else {}
            if settings.get("channel"):
                self.sensor_channel.setCurrentText(str(settings["channel"]))
            self.input_field.setCurrentIndex(max(0, self.input_field.findData(settings.get("input_field", "adc"))))
            self.sensor_degree.setCurrentIndex(max(0, self.sensor_degree.findData(settings.get("degree", 1))))
            self.sensor_tolerance.setValue(float(settings.get("tolerance", 1)))
        table = getattr(self, f"{task}_annotations")
        table.setRowCount(0)
        self._annotation_sources[task] = captures
        for capture in captures:
            source = next((row for row in self.sources if self._capture(row) == capture), {})
            draft = source.get("annotation_draft", {}).get("parameters", {})
            draft_annotations = [row for row in draft.get("annotations", [])
                                 if row.get("capture", capture) == capture]
            annotations = self._annotations[task].get(capture, draft_annotations if "annotations" in draft else source.get("annotations", []))
            if isinstance(annotations, list):
                for row in annotations:
                    self.add_annotation(task, {**row, "capture": capture})

    def add_annotation(self, task, row=None):
        captures = sorted(self.selected)
        if not captures:
            self.status.setText("先勾选完整收口的实验，再添加参考区间。")
            return
        row = row or {}
        table = getattr(self, f"{task}_annotations")
        n = table.rowCount()
        table.insertRow(n)
        capture = QtWidgets.QComboBox()
        for path in captures:
            capture.addItem(Path(path).name, path)
        capture.setCurrentIndex(max(0, capture.findData(row.get("capture"))))
        table.setCellWidget(n, 0, capture)
        for col, name in ((1, "start_s"), (2, "end_s")):
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(0, 1e8)
            spin.setDecimals(3)
            spin.setValue(float(row.get(name, 0)))
            table.setCellWidget(n, col, spin)
        role = QtWidgets.QComboBox()
        role.addItem("标定", "calibration")
        role.addItem("独立验证", "validation")
        if task == "finger":
            role.addItem("观察／估距", "observation")
        role.setCurrentIndex(max(0, role.findData(row.get("role", "calibration"))))
        table.setCellWidget(n, 3, role)
        reference = row.get("distance_mm" if task == "finger" else "reference")
        table.setItem(n, 4, QtWidgets.QTableWidgetItem("" if reference is None else str(reference)))
        table.setItem(n, 5, QtWidgets.QTableWidgetItem(str(row.get("action", ""))))
        if task == "sensor":
            segment = QtWidgets.QSpinBox()
            segment.setRange(0, 1_000_000)
            segment.setValue(int(row.get("time_segment", 0)))
            segment.setToolTip("CSV 中的 time_segment；MCU 复位后不能与前一段混合标定。")
            table.setCellWidget(n, 6, segment)

    def build_request(self, action):
        task = self.task.currentData()
        captures = sorted(self.selected)
        if not captures:
            raise ValueError("请先勾选已完成并收口的实验记录")
        kind = TASKS[task][1]
        lookup = {self._capture(row): row for row in self.sources}
        if any(p not in lookup or lookup[p].get("eligible") is not True or lookup[p].get("kind") != kind for p in captures):
            raise ValueError("选择中存在未收口或类型不符的来源，请刷新实验")
        advanced = self.advanced.toPlainText().strip()
        try:
            parameters = json.loads(advanced) if advanced else {}
        except ValueError as exc:
            raise ValueError("高级参数必须是有效 JSON 对象") from exc
        if not isinstance(parameters, dict):
            raise ValueError("高级参数必须是对象")
        protected = {"labels", "validation_runs", "annotations", "desktop_project", "task",
                     "initial_distance_mm", "tolerance_mm", "channel", "input_field", "reference_unit", "degree", "tolerance", "time_field"}
        if parameters.keys() & protected:
            raise ValueError("标签、参考区间、环境与基本参数请在表格／表单中填写")
        parameters["desktop_project"] = str(self.root)
        request = {"action": action, "captures": captures, "parameters": parameters}
        if action in {"material_predict", "finger_validate", "sensor_validate", "sensor_predict"}:
            model = self.model.currentData()
            if not model:
                raise ValueError("请先选择相应的版本模型")
            request["model"] = model
        if action == "inspect" or action == "response":
            if task == "sensor":
                parameters.update(channel=self.sensor_channel.currentText().strip(), input_field=self.input_field.currentData(), time_field="mcu_elapsed_s")
            return request
        if action != "save_annotations" and action not in {op for _, op in OPERATIONS[task]}:
            raise ValueError("当前实验类型不支持此分析操作")
        if task == "material":
            labels = {p: self._labels.get(p, lookup[p].get("label_override", "")).strip() for p in captures}
            roles = {p: self._roles.get(p, lookup[p].get("role_override", "observation")) for p in captures}
            if action == "material_train":
                if any(roles[p] == "observation" for p in captures):
                    raise ValueError("训练数据必须明确设为训练或独立验证；观察／预测记录请取消勾选")
                if any(not labels[p] for p in captures):
                    raise ValueError("请为每条训练／验证记录填写人工材料类别")
                classes = set(labels.values())
                if len(classes) < 2 or any(not all(any(labels[p] == label and roles[p] == role for p in captures)
                                                   for role in ("training", "validation")) for label in classes):
                    raise ValueError("至少 2 个材料类别，且每类需要独立的训练 run 与验证 run")
            parameters.update(labels=labels, validation_runs=[p for p in captures if roles[p] == "validation"])
            if action == "save_annotations":
                parameters["roles"] = roles
        else:
            if task == "finger" and len(captures) != 1:
                raise ValueError("手指模型只允许同一次连续采集")
            strict = action in {"finger_calibrate", "finger_validate", "sensor_calibrate", "sensor_validate"}
            annotations = self._read_annotations(task, strict=strict)
            if action in {"finger_validate", "sensor_validate"}:
                annotations = [row for row in annotations if row["role"] == "validation"]
            if task == "finger":
                annotations = [{k: v for k, v in row.items() if k != "capture"} for row in annotations]
                initial = self.initial_distance.text().strip()
                if action == "finger_calibrate" or initial:
                    parameters["initial_distance_mm"] = finite_number(initial, "初始尺量距离 mm")
                    if parameters["initial_distance_mm"] < 0:
                        raise ValueError("初始尺量距离不能为负数")
                parameters["tolerance_mm"] = self.finger_tolerance.value()
            else:
                parameters.update(channel=self.sensor_channel.currentText().strip(), input_field=self.input_field.currentData(), time_field="mcu_elapsed_s")
                if not parameters["channel"]:
                    raise ValueError("请填写 CSV 中的实际通道名")
                unit = self.reference_unit.text().strip()
                if action in {"sensor_calibrate", "sensor_validate"} and not unit:
                    raise ValueError("请填写参考仪器读数单位，例如 degC、kPa 或 N")
                if unit:
                    parameters["reference_unit"] = unit
                if action == "sensor_validate":
                    info = next((row for row in self.models if str(row.get("path", "")) == request.get("model")), None)
                    if info:
                        aliases = {"°C": "degC", "℃": "degC", "C": "degC"}
                        if aliases.get(unit, unit) != info.get("reference_unit"):
                            raise ValueError(f"参考值单位必须与模型一致：{info.get('reference_unit')}；单位不能自动换算")
                        if info.get("channel") and parameters["channel"] != info["channel"]:
                            raise ValueError(f"所选模型的实际通道是 {info['channel']}，请核对通道")
                        if info.get("input_field") and parameters["input_field"] != info["input_field"]:
                            raise ValueError("输入 ADC／电压字段必须与所选标定模型一致")
                parameters.update(degree=self.sensor_degree.currentData(), tolerance=self.sensor_tolerance.value())
            parameters["annotations"] = annotations
            if action in {"finger_calibrate", "sensor_calibrate"} and not all(any(a["role"] == role for a in annotations) for role in ("calibration", "validation")):
                raise ValueError("请分别填写标定区间和独立验证区间，再执行拟合")
            if action == "sensor_validate" and not any(a["role"] == "validation" for a in annotations):
                raise ValueError("独立验证需要填写新的验证参考区间")
        if action == "save_annotations":
            parameters["task"] = task
        return request

    def run_action(self, action):
        if self.is_running or self._closing or self._acquisition_busy:
            return
        try:
            request = self.build_request(action)
            self.bridge.configure(self.project_path.text(), self.python_path.text())
        except (ValueError, OSError) as exc:
            self._on_error(str(exc))
            return
        self._stash_annotations()
        self._action = action
        self.status.setText("后台分析运行中，可取消；原始数据只读。")
        self.bridge.start(request)

    def _busy_changed(self, busy):
        self._update_controls()
        self.busy_changed.emit(busy)

    def _update_controls(self):
        if not hasattr(self, "run_button"):
            return
        idle = not self.is_running and not self._closing and not self._acquisition_busy
        for widget in (self.environment_group, self.source_table, self.task, self.search, self.forms,
                       self.operation, self.model, self.model_browse, self.advanced):
            widget.setEnabled(idle)
        self.refresh_button.setEnabled(idle)
        for button in (self.run_button, self.save_button, self.inspect_button):
            button.setEnabled(idle and bool(self.selected))
        self.cancel_button.setEnabled(self.is_running)

    def set_acquisition_busy(self, busy):
        self._acquisition_busy = bool(busy)
        self._update_controls()

    def _on_error(self, message):
        if not self._closing:
            self.status.setText(str(message))
            self._update_controls()
        self._after_task()

    def _on_result(self, response):
        if self._closing:
            return
        result = response.get("result") or {}
        if response.get("action", self._action) == "list":
            self.sources = list(result.get("sources", []))
            self.models = list(result.get("models", []))
            for source in self.sources:
                capture = self._capture(source)
                draft = source.get("annotation_draft", {}).get("parameters", {})
                if capture in draft.get("labels", {}) and capture not in self._labels:
                    self._labels[capture] = str(draft["labels"][capture])
                if capture not in self._roles and draft:
                    self._roles[capture] = draft.get("roles", {}).get(capture,
                        "validation" if capture in draft.get("validation_runs", []) else "training")
            valid = {self._capture(row) for row in self.sources if row.get("eligible") is True}
            self.selected &= valid
            self._render_sources()
            self._render_models()
            self._load_annotation_selection()
            self.status.setText("实验索引已刷新。勾选实验、填写标签／参考区间，再选择分析方法。")
            if self._select_pending:
                capture, self._select_pending = self._select_pending, None
                if any(self._capture(row) == capture for row in self.sources):
                    self.select_source(capture)
        else:
            self.result = response
            charts = result.get("charts", [])
            self.chart_choice.clear()
            for chart in charts:
                self.chart_choice.addItem(chart.get("title", chart.get("id", "结果曲线")), chart)
            if not charts:
                self.plot.display({"title": "本次结果无曲线", "note": "请查看参数／验证／来源或结果文件。", "series": []})
            # 曲线数据单独显示；文本仍保留模型状态、误差、输入参数与来源。
            summary = {k: v for k, v in result.items() if k != "charts"}
            self.details.setPlainText("有限预览；完整结果与来源请查看结果文件。\n" +
                                      json.dumps(result_preview(summary), ensure_ascii=False, indent=2, allow_nan=False))
            self.artifacts.clear()
            for artifact in response.get("artifacts", []):
                path = artifact.get("path", "")
                item = QtWidgets.QListWidgetItem(f"{artifact.get('kind', '文件')} · {Path(path).name}")
                item.setData(QtCore.Qt.ItemDataRole.UserRole, path)
                item.setToolTip(path)
                self.artifacts.addItem(item)
            self.open_result_button.setEnabled(bool(response.get("analysis_dir")))
            warnings = result.get("warnings", [])
            message = "分析完成，版本结果已保存。"
            model_active = result.get("active", (result.get("model") or {}).get("active", result.get("status") == "ready"))
            if response.get("model_dir") and not model_active:
                message = "拟合／验证完成，但模型未通过激活门槛；请查看验证原因。"
            if result.get("status") in {"invalid", "validation_failed", "insufficient_data"}:
                message = "分析未通过质量／验证门槛；请查看错误与参考区间。"
            if result.get("hint"):
                message += " " + str(result["hint"])
            if warnings:
                message += " " + "；".join(map(str, warnings[:3]))
            self.status.setText(message)
            if response.get("model_dir"):
                path = str(Path(response["model_dir"]) / "model.json")
                self.models.append({"path": path, "kind": self.task.currentData(), "active": model_active})
                self._render_models()
                self.model.setCurrentIndex(self.model.findData(path))
            if not charts:
                self.result_tabs.setCurrentIndex(1)
        self._update_controls()
        self._after_task()

    def _after_task(self):
        if self._refresh_pending and not self._closing:
            self._refresh_pending = False
            QtCore.QTimer.singleShot(0, self.refresh_sources)

    def _show_chart(self):
        chart = self.chart_choice.currentData()
        if chart:
            self.plot.display(chart)

    def _open_result(self):
        if self.result and self.result.get("analysis_dir"):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self.result["analysis_dir"]))

    def _open_artifact(self, item):
        path = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if path:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def shutdown(self):
        self._closing = True
        return self.bridge.shutdown()

    def closeEvent(self, event):
        if not self.shutdown():
            event.ignore()
            return
        close_plot_widgets(self)
        super().closeEvent(event)
