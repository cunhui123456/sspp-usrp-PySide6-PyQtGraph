#!/usr/bin/env python3
"""兼容命令行入口；当前实现见 sspp_lab.acquire_sweep，导入不会启动硬件。"""
from sspp_lab.acquire_sweep import *
if __name__ == '__main__':
    raise SystemExit(main())
