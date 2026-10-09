"""有界后台任务：IO、分析和设备控制都不占用 Qt 主线程。"""
from __future__ import annotations

from collections.abc import Callable
from PySide6 import QtCore


class _Signals(QtCore.QObject):
    finished = QtCore.Signal(str, object, object)


class _Task(QtCore.QRunnable):
    def __init__(self, key: str, operation: Callable, signals: _Signals):
        super().__init__()
        self.key, self.operation, self.signals = key, operation, signals

    def run(self):
        try:
            value, error = self.operation(), None
        except Exception as exc:
            value, error = None, exc
        self.signals.finished.emit(self.key, value, error)


class TaskRunner(QtCore.QObject):
    """相同 key 不堆积；分析串行，停止操作有独立线程可立即排入。"""
    idle = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.control = QtCore.QThreadPool(self)
        self.control.setMaxThreadCount(2)
        self.urgent = QtCore.QThreadPool(self)
        self.urgent.setMaxThreadCount(1)
        self.analysis = QtCore.QThreadPool(self)
        self.analysis.setMaxThreadCount(1)
        self.polling = QtCore.QThreadPool(self)
        self.polling.setMaxThreadCount(1)
        self.signals = _Signals(self)
        self.signals.finished.connect(self._finished)
        self.pending = {}
        self.closing = False

    def submit(self, key, operation, success=None, failure=None, lane="analysis"):
        if key in self.pending or (self.closing and key != "shutdown"):
            return False
        # At most four analysis requests, two control requests and one poll.
        limit = {"analysis": 4, "control": 2, "polling": 1, "urgent": 1}[lane]
        if sum(item[2] == lane for item in self.pending.values()) >= limit:
            return False
        self.pending[key] = (success, failure, lane)
        getattr(self, lane).start(_Task(key, operation, self.signals))
        return True

    @QtCore.Slot(str, object, object)
    def _finished(self, key, value, error):
        success, failure, _ = self.pending.pop(key, (None, None, None))
        try:
            if error is not None:
                if failure:
                    failure(error)
            elif success:
                success(value)
        finally:
            if not self.pending:
                self.idle.emit()
