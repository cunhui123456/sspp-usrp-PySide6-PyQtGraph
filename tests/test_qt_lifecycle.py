"""Qt 原生析构必须发生在进程正常退出前，避免只看到 unittest 的 OK。"""
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class QtLifecycleTests(unittest.TestCase):
    def test_window_close_releases_scenes_and_subprocess_exits_cleanly(self):
        root = Path(__file__).resolve().parents[1]
        program = textwrap.dedent("""
            import sys, time
            from pathlib import Path
            sys.path.insert(0, str(Path.cwd() / 'tests'))
            from test_desktop_ui import FakeService
            from PySide6 import QtCore, QtWidgets
            import pyqtgraph as pg
            from shiboken6 import isValid
            from sspp_desktop.window import MainWindow

            app = QtWidgets.QApplication([])
            def wait_until(predicate):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    app.processEvents()
                    if predicate():
                        return
                    time.sleep(.005)
                raise AssertionError('窗口关闭未完成')

            for _ in range(3):
                service = FakeService()
                window = MainWindow(service)
                window.show()
                wait_until(lambda: not window.runner.pending)
                window.analysis_plot.display({
                    'kind': 'heatmap', 'title': '生命周期', 'xlabel': '时间',
                    'ylabel': '频率', 'x': [0, 1], 'y': [1, 2],
                    'z': [[1, None], [2, 3]], 'colorbar': 'PSD'})
                app.processEvents()
                plots = window.findChildren(pg.PlotWidget)
                assert len(plots) >= 8
                window.close()
                wait_until(lambda: service.closed and not window.isVisible())
                assert not window.runner.pending
                assert all(not isValid(plot) or plot.plotItem is None for plot in plots)
                window.deleteLater()
                QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
                app.processEvents()
                assert not isValid(window)
            print('Qt windows disposed')
        """)
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", MPLBACKEND="Agg")
        result = subprocess.run([sys.executable, "-B", "-X", "faulthandler", "-c", program],
                                cwd=root, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Qt windows disposed", result.stdout)
        self.assertNotIn("shared QObject was deleted", result.stderr)
