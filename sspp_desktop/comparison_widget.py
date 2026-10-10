"""只读实验比较窗口；计算、导出由主窗口的有界后台任务执行。"""
from __future__ import annotations

import json
from html import escape
from PySide6 import QtCore, QtWidgets

from .plots import DiagnosticPlot
from .qt_cleanup import close_plot_widgets

HEADERS = {
    "key": "实验 key", "label": "实验名称", "reference_key": "参考实验 key", "frequency_points": "频点数",
    "repeat_count": "重复次数", "repeat_count_min": "最少重复次数", "repeat_count_max": "最多重复次数",
    "max_repeat_sd_dB": "最大重复标准差 (dB)", "common_points_vs_reference": "共同频点数",
    "mean_difference_dB": "平均差值 (dB)", "max_abs_difference_dB": "最大绝对差值 (dB)",
    "requested_carrier_hz": "请求载频 (Hz)", "actual_tx_carrier_hz": "TX 载频回读 (Hz)",
    "actual_rx_carrier_hz": "RX 载频回读 (Hz)", "expected_tone_hz": "预期基带单音 (Hz)",
    "measured_tone_hz": "RX 检测单音 (Hz)", "target_rf_hz": "载波 + 单音 (Hz)",
    "frequency_basis": "频点依据", "count": "样本数", "mean_amplitude_dB": "平均幅度 (dB)",
    "repeat_sd_dB": "重复标准差 (dB)", "max_tone_error_hz": "最大单音偏差 (Hz)",
    "repeat": "重复轮次", "point": "测量点", "amplitude_dB": "数字幅度 (dB)",
    "target_count": "目标重复数", "reference_count": "参考重复数", "target_mean_dB": "目标均值 (dB)",
    "reference_mean_dB": "参考均值 (dB)", "difference_dB": "目标 − 参考 (dB)",
    "combined_repeat_sd_dB": "合成重复标准差 (dB)", "t_s": "原记录时间 (s)",
    "time_from_baseline_s": "参考结束后时间 (s)", "relative_time_s": "参考结束后时间 (s)",
    "relative_amplitude_dB": "相对幅度 (dB)", "relative_phase_deg": "相对相位 (°)",
    "coherent_fraction": "相干比例", "sample_count": "样本数", "duration_s": "时长 (s)",
    "phase_std_deg": "相位标准差 (°)", "amplitude_std_dB": "幅度标准差 (dB)",
    "distance_mm": "距离 (mm)", "displacement_mm": "位移 (mm)", "error": "说明",
    "processed_windows": "全部处理窗口数", "post_baseline_windows": "参考后窗口数",
    "baseline_end_s": "静止参考结束 (s)", "response_end_s": "参考后响应结束 (s)",
    "baseline_amplitude_sd_dB": "静止参考幅度标准差 (dB)",
    "baseline_phase_residual_sd_deg": "静止参考相位残差标准差 (°)",
    "baseline_phase_drift_deg_per_s": "静止参考相位漂移 (°/s)", "min_coherent_fraction": "最小相干比例",
    "low_coherence_windows": "低相干窗口数", "missing_relative_windows": "相对值缺失窗口数",
    "sample_gaps": "样本或时间缺口数", "transition_windows": "过渡窗口数",
    "amplitude_peak_to_peak_dB": "全程幅度峰峰值 (dB)", "phase_peak_to_peak_deg": "全程相位峰峰值 (°)",
    "time_after_baseline_s": "参考结束后时间 (s)", "stage_label": "实验阶段", "start_sample": "起始样本序号",
    "gap_before": "此前存在缺口",
}
TABLE_NAMES = {"sweep_summary": "扫频均值与波动", "sweep_measurements": "逐点测量", "sweep_difference": "相对参考差值",
               "phase_samples": "幅相样点", "phase_summary": "幅相摘要", "phase_stages": "阶段摘要",
               "phase_aligned": "对齐幅相样点", "phase_curves": "参考后幅相窗口", "finger_validation": "手指验证摘要"}

def _text(value):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return {"unknown": "未知", "same": "一致", "different": "不同", "reference": "参考"}.get(str(value), str(value))


class ComparisonDialog(QtWidgets.QDialog):
    reference_requested = QtCore.Signal(str)
    export_requested = QtCore.Signal()
    closed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("实验结果比较 · 只读分析")
        self.setModal(False)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1120, 850)
        self.setMinimumSize(850, 650)
        self.report = None
        self.is_closed = False
        layout = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        self.reference_label = QtWidgets.QLabel("参考实验")
        top.addWidget(self.reference_label)
        self.reference = QtWidgets.QComboBox()
        self.reference.setMinimumContentsLength(28)
        self.reference.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.reference.currentIndexChanged.connect(self._reference_changed)
        top.addWidget(self.reference, 1)
        self.export_button = QtWidgets.QPushButton("打包导出比较…")
        self.export_button.clicked.connect(self.export_requested)
        top.addWidget(self.export_button)
        layout.addLayout(top)
        self.summary = QtWidgets.QLabel("正在比较所选实验…")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.warning = QtWidgets.QPlainTextEdit()
        self.warning.setReadOnly(True)
        self.warning.setMaximumHeight(110)
        self.warning.hide()
        layout.addWidget(self.warning)
        self.tabs = QtWidgets.QTabWidget()
        plots = QtWidgets.QWidget()
        pl = QtWidgets.QVBoxLayout(plots)
        self.chart_choice = QtWidgets.QComboBox()
        self.chart_choice.currentIndexChanged.connect(self._chart_changed)
        pl.addWidget(self.chart_choice)
        self.legend = QtWidgets.QLabel()
        self.legend.setWordWrap(True)
        self.legend.setTextFormat(QtCore.Qt.TextFormat.RichText)
        pl.addWidget(self.legend)
        self.plot = DiagnosticPlot()
        self.plot.legend.hide()
        self.plot.plot.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Ignored)
        pl.addWidget(self.plot, 1)
        pl.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        plot_scroll = QtWidgets.QScrollArea()
        plot_scroll.setWidgetResizable(True)
        plot_scroll.setWidget(plots)
        self.tabs.addTab(plot_scroll, "比较图")
        self.metrics = self._table()
        self.tabs.addTab(self.metrics, "结果指标")
        self.parameters = self._table()
        self.tabs.addTab(self.parameters, "参数核对")
        self.data_tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.data_tabs, "数据表")
        layout.addWidget(self.tabs, 1)
        self.set_busy(True)

    @staticmethod
    def _table():
        table = QtWidgets.QTableWidget()
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setMaximumSectionSize(420)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    @staticmethod
    def _fill(table, columns, rows):
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels([HEADERS.get(column, column) for column in columns])
        table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                full = _text(value)
                item = QtWidgets.QTableWidgetItem(full)
                item.setToolTip(full)
                table.setItem(r, c, item)

    def apply_report(self, report):
        self.report = report
        sources = report.get("sources", [])
        self.reference.blockSignals(True)
        self.reference.clear()
        for source in sources:
            self.reference.addItem(f"{source.get('label') or source['key']} · {source['key']}", source["key"])
        self.reference.setCurrentIndex(self.reference.findData(report.get("reference_key")))
        self.reference.blockSignals(False)
        allowed = report.get("quantitative_difference_allowed", False)
        kind = report.get("kind")
        note = ("参数与频点条件允许时显示相对参考的幅度差。" if allowed else "此组合仅显示各实验曲线与指标；不生成跨实验定量差值。")
        if kind in ("phase_history", "finger_displacement"):
            note = "时间以各实验静止参考结束为零点；各自相位参考独立，不进行跨实验相位相减或距离模型迁移。"
        phase = kind in ("phase_history", "finger_displacement")
        self.reference_label.setText("报告排序参考" if phase else "差值参考实验")
        self.reference.setToolTip("各自静止参考；无跨实验相减" if phase else "默认最早实验；扫频幅度差 = 目标 − 参考")
        prefix = "各自静止参考；无跨实验相减。" if phase else "默认参考为最早实验，差值方向为目标 − 参考。"
        self.summary.setText(f"共 {len(sources)} 条已保存实验 · {prefix}\n{note}")
        warnings = report.get("warnings", [])
        self.warning.setPlainText("\n".join(str(value) for value in warnings))
        self.warning.setVisible(bool(warnings))
        self.chart_choice.blockSignals(True)
        self.chart_choice.clear()
        for chart in report.get("charts", []):
            self.chart_choice.addItem(chart.get("title", chart.get("id", "比较图")), chart)
        self.chart_choice.blockSignals(False)
        self._chart_changed()
        metrics = report.get("metrics", [])
        columns = list(dict.fromkeys(key for row in metrics for key in row))
        self._fill(self.metrics, columns, [[row.get(key) for key in columns] for row in metrics])
        self._fill(self.parameters, ["参数", "核对结果"] + [source.get("label") or source["key"] for source in sources] + ["说明"],
                   [[entry.get("label", entry.get("parameter", "")),
                     {"same": "一致", "different": "不同", "unknown": "未知"}.get(entry.get("status"), entry.get("status", ""))]
                    + list(entry.get("values", [])) + [entry.get("note", "")]
                    for entry in report.get("compatibility", [])])
        while self.data_tabs.count():
            widget = self.data_tabs.widget(0)
            self.data_tabs.removeTab(0)
            widget.deleteLater()
        for name, dataset in report.get("tables", {}).items():
            page = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(page)
            table = self._table()
            columns = dataset.get("columns", [])
            rows = dataset.get("rows", [])
            count = min(500, len(rows))
            note = QtWidgets.QLabel(f"预览前 {count} / 总 {len(rows)} 行；导出包含完整数据。")
            note.setWordWrap(True)
            layout.addWidget(note)
            self._fill(table, columns, [[row.get(key) for key in columns] if isinstance(row, dict) else row for row in rows[:500]])
            layout.addWidget(table)
            self.data_tabs.addTab(page, TABLE_NAMES.get(name, name))
        self.status.setText("")

    def set_busy(self, busy, message="", blocked=False):
        self.reference.setEnabled(not busy and not blocked and self.report is not None and self.report.get("kind") == "material_sweep")
        self.export_button.setEnabled(not busy and not blocked and self.report is not None)
        if message:
            self.status.setText(message)

    def show_error(self, error):
        self.status.setText(str(error))

    def _reference_changed(self):
        key = self.reference.currentData()
        if key is not None and self.report and key != self.report.get("reference_key"):
            self.reference_requested.emit(key)

    def _chart_changed(self):
        model = self.chart_choice.currentData() or {}
        self.plot.display(model)
        # Keep provenance labels outside the data region; a short experiment
        # must not disappear beneath a legend over its entire visible trace.
        entries = []
        for series in model.get("series", []):
            color = escape(str(series.get("color", "#087f85")), quote=True)
            entries.append(f'<span style="color:{color}">━</span> {escape(str(series.get("label", "")))}')
        self.legend.setText(" &nbsp;&nbsp; ".join(entries))

    def closeEvent(self, event):
        self.is_closed = True
        close_plot_widgets(self)
        self.closed.emit()
        super().closeEvent(event)
