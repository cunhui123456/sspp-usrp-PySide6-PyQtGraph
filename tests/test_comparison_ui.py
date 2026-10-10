"""离线核查比较勾选、筛选、异步来源绑定、导出选择及 Qt 清理。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import copy
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid
from sspp_desktop.window import MainWindow
from test_desktop_ui import FakeService
from qt_test_cleanup import dispose_closed_windows


def comparison_report(keys, reference=None, kind="material_sweep"):
    return {"keys": keys, "reference_key": reference or keys[-1], "kind": kind,
            "sources": [{"key": key, "label": key.split("/")[-1], "parameters": {}} for key in keys],
            "quantitative_difference_allowed": kind == "material_sweep",
            "charts": [{"id": "sweep_mean", "kind": "line", "title": "平均幅度", "xlabel": "GHz", "ylabel": "dB",
                        "series": [{"label": key, "data": [[2.4, -40], [2.5, -38]]} for key in keys]}],
            "metrics": [{"key": key, "repeat_count": 2} for key in keys],
            "compatibility": [{"parameter": "rate", "label": "采样率", "values": [250000] * len(keys), "status": "same", "note": ""}],
            "warnings": ["离线测试来源"], "tables": {"均值": {"columns": ["key", "mean"], "rows": [{"key": key, "mean": -39} for key in keys]}}}


class ComparisonService(FakeService):
    def __init__(self):
        super().__init__()
        self.rows = [{"key": f"local_runs/material_{n}", "label": f"样品 {n}", "mode": "sweep", "category": "material_sweep",
                      "started_utc": f"2026-10-10T00:00:{n:02d}+00:00", "status": "complete", "sensor": "SSPP A"} for n in range(9)]
        self.rows += [{"key": f"phase_runs/{kind}", "label": kind, "mode": "phase", "category": kind,
                       "started_utc": "2026-10-09T00:00:00Z", "status": "stopped", "sensor": "SSPP B"}
                      for kind in ("finger_displacement", "phase_history")]
        self.rows += [{"key": "teacher_runs/one", "label": "老师记录", "mode": "teacher", "category": "teacher_sweep", "status": "failed", "sensor": ""}]
        self.comparisons = []
        self.exports = []
        self.compare_release = threading.Event()
        self.compare_release.set()
        self.details_release = threading.Event()
        self.details_release.set()
        self.fail_reference = False
        self.export_release = threading.Event()
        self.export_release.set()

    def history(self):
        return copy.deepcopy(self.rows)

    def details(self, key):
        self.details_release.wait(2)
        return {"key": key, "metadata": {"key": key}, "curve": {"mode": "sweep", "points": []}, "files": []}

    def compare_runs(self, keys, reference_key=None):
        self.comparisons.append((list(keys), reference_key, threading.get_ident()))
        self.compare_release.wait(3)
        if self.fail_reference and reference_key:
            raise ValueError("测试参考重算失败")
        kind = next(row["category"] for row in self.rows if row["key"] == keys[0])
        return comparison_report(list(keys), reference_key, kind)

    def export_comparison(self, report, destination):
        self.exports.append((report, destination, threading.get_ident()))
        self.export_release.wait(3)
        return {"path": destination, "count": 5}


class ComparisonUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.service = ComparisonService()
        self.window = MainWindow(self.service)
        self.window.show()
        self.wait(lambda: bool(self.window.history_rows) and not self.window.runner.pending)

    def tearDown(self):
        self.service.compare_release.set()
        self.service.details_release.set()
        self.service.export_release.set()
        self.service.stop()
        self.window._apply_status(self.service.snapshot())
        self.wait(lambda: not self.window.runner.pending)
        self.window.close()
        self.wait(lambda: not self.window.isVisible())
        dispose_closed_windows()

    def wait(self, predicate, timeout=4):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail("Qt condition timed out")

    def item(self, key):
        return next(self.window.history_table.item(n, 0) for n in range(self.window.history_table.rowCount())
                    if self.window.history_table.item(n, 0).data(QtCore.Qt.ItemDataRole.UserRole) == key)

    def check(self, key, checked=True):
        self.item(key).setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)

    def prepare_comparison(self):
        keys = [row["key"] for row in self.service.rows[:2]]
        for key in keys:
            self.check(key)
        self.window.compare_selected()
        self.wait(lambda: self.window.comparison_dialog is not None and not self.window.runner.pending)
        return keys, self.window.comparison_dialog

    def test_filters_clear_hidden_checks_and_preserve_only_visible_keys(self):
        first, second = [row["key"] for row in self.service.rows[:2]]
        self.check(first)
        self.check(second)
        self.window.history_search.setText("样品 1")
        self.assertEqual([row["key"] for row in self.window.history_rows], [second])
        self.assertEqual(self.window.comparison_keys, {second})
        self.assertIn("已清除 1 条", self.window.notice.text())
        self.window.history_search.clear()
        self.window.history_type.setCurrentIndex(self.window.history_type.findData("finger_displacement"))
        self.assertEqual(len(self.window.history_rows), 1)
        self.assertFalse(self.window.comparison_keys)
        self.window.history_status.setCurrentIndex(self.window.history_status.findData("complete"))
        self.assertFalse(self.window.history_rows)
        self.window.history_type.setCurrentIndex(0)
        self.assertEqual(len(self.window.history_rows), 9)
        self.window.history_search.setText("sspp a")
        self.assertEqual(len(self.window.history_rows), 9)
        self.window.history_search.setText(first)
        self.assertEqual(self.window.history_rows[0]["key"], first)
        self.assertIn("原始 UTC", self.window.history_table.item(0, 1).toolTip())

    def test_refresh_uses_keys_for_checks_and_highlight_after_reorder(self):
        key = self.service.rows[2]["key"]
        self.check(key)
        self.window.history_table.selectRow(self.item(key).row())
        self.wait(lambda: self.window.selected_details is not None)
        self.service.rows.reverse()
        self.window.refresh_history()
        self.wait(lambda: "history" not in self.window.runner.pending)
        self.assertEqual(self.window.comparison_keys, {key})
        self.assertEqual(self.item(key).checkState(), QtCore.Qt.CheckState.Checked)
        self.assertEqual(self.window.history_table.currentRow(), self.item(key).row())
        self.assertEqual(self.window.selected_details["key"], key)

    def test_same_category_max_eight_and_teacher_rejection(self):
        for row in self.service.rows[:9]:
            self.check(row["key"])
        self.assertEqual(len(self.window.comparison_keys), 8)
        self.assertIn("最多比较 8", self.window.notice.text())
        self.check(self.service.rows[0]["key"], False)
        self.check("phase_runs/finger_displacement")
        self.assertNotIn("phase_runs/finger_displacement", self.window.comparison_keys)
        self.assertIn("同一种实验", self.window.notice.text())
        self.window.clear_comparison_selection()
        self.check("teacher_runs/one")
        self.assertFalse(self.window.comparison_keys)
        self.assertIn("暂不支持", self.window.notice.text())

    def test_late_details_and_comparison_do_not_override_changed_filter(self):
        key = self.service.rows[0]["key"]
        self.service.details_release.clear()
        self.window.history_table.selectRow(0)
        self.window.history_search.setText("finger_displacement")
        self.service.details_release.set()
        self.wait(lambda: not self.window.runner.pending)
        self.assertIsNone(self.window.selected_details)
        self.assertIsNone(self.window.viewing_key)
        self.window.history_search.clear()
        self.check(key)
        self.check(self.service.rows[1]["key"])
        self.service.compare_release.clear()
        self.window.compare_selected()
        self.wait(lambda: bool(self.service.comparisons))
        self.window.history_search.setText("finger_displacement")
        self.service.compare_release.set()
        self.wait(lambda: not self.window.runner.pending)
        self.assertIsNone(self.window.comparison_dialog)
        self.assertFalse(self.window._comparison_busy)

    def test_reference_recompute_and_export_cancel_and_destination(self):
        keys, dialog = self.prepare_comparison()
        self.assertEqual(dialog.reference.currentData(), keys[-1])
        self.assertTrue(dialog.export_button.isEnabled())
        self.service.compare_release.clear()
        dialog.reference.setCurrentIndex(dialog.reference.findData(keys[0]))
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertFalse(dialog.reference.isEnabled())
        self.service.compare_release.set()
        self.wait(lambda: not self.window.runner.pending)
        self.assertEqual(dialog.report["reference_key"], keys[0])
        with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=("", "")):
            self.window._export_comparison(dialog)
        self.assertFalse(self.service.exports)
        with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=("/private/tmp/comparison_ui_result", "")):
            self.window._export_comparison(dialog)
        self.wait(lambda: not self.window.runner.pending)
        self.assertEqual(self.service.exports[0][1], "/private/tmp/comparison_ui_result.zip")
        self.assertIs(self.service.exports[0][0], dialog.report)
        self.assertNotEqual(self.service.exports[0][2], threading.get_ident())
        self.assertTrue(all(thread != threading.get_ident() for _, _, thread in self.service.comparisons))

    def test_failed_reference_restores_last_report_and_closed_dialog_is_not_reopened(self):
        keys, dialog = self.prepare_comparison()
        self.service.fail_reference = True
        dialog.reference.setCurrentIndex(dialog.reference.findData(keys[0]))
        self.wait(lambda: not self.window.runner.pending)
        self.assertEqual(dialog.reference.currentData(), dialog.report["reference_key"])
        self.assertIn("失败", dialog.status.text())
        self.service.fail_reference = False
        self.service.compare_release.clear()
        dialog.reference.setCurrentIndex(dialog.reference.findData(keys[0]))
        dialog.close()
        self.service.compare_release.set()
        self.wait(lambda: not self.window.runner.pending)
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.assertIsNone(self.window.comparison_dialog)
        self.assertFalse(isValid(dialog))

    def test_active_acquisition_and_export_block_new_compare(self):
        keys, dialog = self.prepare_comparison()
        self.service.job = {"status": "running", "config": {"mode": "phase"}}
        self.window._apply_status(self.service.snapshot())
        self.assertFalse(self.window.compare_button.isEnabled())
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertFalse(dialog.reference.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())
        old = set(self.window.comparison_keys)
        self.check(keys[0], False)
        self.assertEqual(self.window.comparison_keys, old)
        self.window.compare_selected()
        self.assertEqual(len(self.service.comparisons), 1)

    def test_export_blocks_reference_and_selection_until_background_finishes(self):
        keys, dialog = self.prepare_comparison()
        self.service.export_release.clear()
        with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=("/private/tmp/comparison_ui_result.zip", "")):
            self.window._export_comparison(dialog)
        self.wait(lambda: bool(self.service.exports))
        self.assertFalse(dialog.export_button.isEnabled())
        self.assertFalse(dialog.reference.isEnabled())
        self.assertFalse(self.window.compare_button.isEnabled())
        self.assertFalse(self.window.start_button.isEnabled())
        self.check(keys[0], False)
        self.assertEqual(self.window.comparison_keys, set(keys))
        self.service.export_release.set()
        self.wait(lambda: not self.window.runner.pending)
        self.assertTrue(dialog.reference.isEnabled())
        self.assertTrue(self.window.compare_button.isEnabled())
        self.assertTrue(self.window.start_button.isEnabled())

    def test_phase_report_disables_cross_run_reference_switch(self):
        from sspp_desktop.comparison_widget import ComparisonDialog
        dialog = ComparisonDialog(self.window)
        dialog.apply_report(comparison_report(["phase_runs/a", "phase_runs/b"], kind="phase_history"))
        dialog.set_busy(False)
        dialog.show()
        self.assertFalse(dialog.reference.isEnabled())
        self.assertTrue(dialog.export_button.isEnabled())
        self.assertIn("无跨实验相减", dialog.summary.text())
        dialog.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(dialog))

    def test_large_table_preview_is_bounded_without_mutating_export_report(self):
        from sspp_desktop.comparison_widget import ComparisonDialog
        report = comparison_report(["phase_runs/a", "phase_runs/b"], kind="phase_history")
        report["tables"] = {"phase_samples": {"columns": ["key", "relative_phase_deg"],
                            "rows": [{"key": "phase_runs/a", "relative_phase_deg": n / 10} for n in range(5000)]}}
        original = copy.deepcopy(report)
        dialog = ComparisonDialog(self.window)
        dialog.apply_report(report)
        page = dialog.data_tabs.widget(0)
        self.assertEqual(page.findChild(QtWidgets.QTableWidget).rowCount(), 500)
        self.assertIn("500 / 总 5000", page.findChild(QtWidgets.QLabel).text())
        self.assertIn("导出包含完整数据", page.findChild(QtWidgets.QLabel).text())
        self.assertEqual(dialog.data_tabs.tabText(0), "幅相样点")
        self.assertEqual(page.findChild(QtWidgets.QTableWidget).horizontalHeaderItem(1).text(), "相对相位 (°)")
        self.assertIs(dialog.report, report)
        self.assertEqual(report, original)
        dialog.close()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
