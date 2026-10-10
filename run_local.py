#!/usr/bin/env python3
"""兼容命令行入口；导入不会启动硬件。"""
from sspp_experiments.material_sweep.acquisition import *
if __name__ == "__main__":
    raise SystemExit(main())
