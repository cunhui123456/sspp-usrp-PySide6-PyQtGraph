#!/usr/bin/env python3
"""启动多传感器实验台；默认空闲，不自动打开硬件。"""
import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查环境与历史索引，不打开窗口/设备")
    args = parser.parse_args()
    os.environ["PYQTGRAPH_QT_LIB"] = "PySide6"
    os.environ.setdefault("MPLBACKEND", "Agg")
    os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).parent / ".cache" / "matplotlib"))
    from sspp_desktop.config import Settings
    from sspp_desktop.services import DesktopService
    settings = Settings.from_env()
    if Path(sys.prefix).name != settings.conda_env:
        parser.error("请先 conda activate rf-gnuradio，或使用 run_desktop.command")
    service = DesktopService(settings)
    if args.check:
        import json
        from PySide6 import QtCore
        import pyqtgraph
        import uhd  # 仅导入驱动；不创建 MultiUSRP，也不 probe。
        from gnuradio import gr
        print(json.dumps({
            "environment": settings.conda_env, "python": sys.executable,
            "qt": QtCore.qVersion(), "pyqtgraph": pyqtgraph.__version__,
            "gnuradio": gr.version(), "uhd_import": True,
            "profiles": service.refresh_profiles(), "history_count": len(service.history()),
            "hardware_opened": False,
        }, ensure_ascii=False, indent=2))
        return 0
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtCore import QLockFile
    from sspp_desktop.window import MainWindow
    app = QApplication(sys.argv[:1])
    app.setApplicationName("多传感器实验台")
    app.setOrganizationName("Sensor Research")
    cache = settings.root / ".cache"
    cache.mkdir(exist_ok=True)
    instance_lock = QLockFile(str(cache / "desktop.lock"))
    instance_lock.setStaleLockTime(0)
    if not instance_lock.tryLock(0):
        QMessageBox.information(None, "多传感器实验台", "此项目的桌面软件已经运行，请使用现有窗口。")
        return 1
    window = MainWindow(service)
    window.show()
    try:
        return app.exec()
    finally:
        service.shutdown()
        instance_lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
