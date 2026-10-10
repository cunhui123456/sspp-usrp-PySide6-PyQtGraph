"""确定性销毁离线测试窗口，避免把 Qt 所有权清理留到 Python atexit。"""
from PySide6 import QtCore, QtWidgets


def dispose_closed_windows():
    roots = {"MainWindow", "FingerDisplacementWidget", "LivePlots", "PreviewPlots", "DiagnosticPlot"}
    app = QtWidgets.QApplication.instance()
    for widget in list(app.topLevelWidgets()):
        if type(widget).__name__ not in roots:
            continue
        if widget.isVisible():
            raise AssertionError(f"测试仍留有打开窗口：{type(widget).__name__}")
        if hasattr(widget, "runner") and widget.runner.pending:
            raise AssertionError("测试仍留有未完成后台任务")
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
