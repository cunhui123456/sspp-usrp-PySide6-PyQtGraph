"""在 Qt 事件循环仍有效时释放 PyQtGraph 的图形场景。

父 QWidget.close() 只隐藏窗口，不会执行子 PlotWidget.close()。
PyQtGraph 的 close 链会先解除坐标轴、ViewBox 与控件的所有权，再清空 scene；
跳过这一步会把相互引用的图形对象留到 PySide 的解释器退出析构阶段。
"""
import pyqtgraph as pg


def close_plot_widgets(owner):
    """只对尚未关闭的子图执行清理；关闭后延迟释放 Qt 图形视图。"""
    for plot in owner.findChildren(pg.PlotWidget):
        if plot.plotItem is not None:
            plot.close()
            plot.deleteLater()
