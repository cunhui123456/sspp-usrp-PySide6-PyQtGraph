#!/usr/bin/env python3
"""旧老师 GUI 的兼容启动器；新软件使用 run_desktop.py。"""
from pathlib import Path
import runpy
import sys
if __name__ == '__main__':
    folder=Path(__file__).resolve().parent/'historical'/'teacher_20260908'
    sys.path.insert(0,str(folder))
    runpy.run_path(str(folder/'run_teacher.py'),run_name='__main__')
