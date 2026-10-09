#!/usr/bin/env python3
"""启动仅监听本机的 SSPP 网页服务；不会自动启动射频采集。"""
from sspp_lab.web_server import main
if __name__ == '__main__':
    raise SystemExit(main())
