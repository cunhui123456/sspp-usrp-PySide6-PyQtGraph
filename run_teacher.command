#!/bin/zsh
# 双击即启动实际收发；设备默认取 local_device.json。
# 自有 B210 必须通过参数显式传入 --device-config，避免使用老师配置。
# 切换到脚本目录后运行已有 rf-gnuradio 环境；exec 让信号直接传给 Python。
set -e
cd "${0:A:h}"
export MPLCONFIGDIR="$PWD/.cache/matplotlib"
exec /opt/homebrew/Caskroom/miniforge/base/envs/rf-gnuradio/bin/python -u run_teacher.py "$@"
