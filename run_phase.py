#!/usr/bin/env python3
"""兼容命令行入口；当前实现见 sspp_lab.acquire_phase，导入不会启动硬件。"""
from sspp_lab.acquire_phase import *
if __name__ == '__main__':
    raise SystemExit(main())
