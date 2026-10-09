#!/bin/zsh
set -e
cd "${0:A:h}"
export PYQTGRAPH_QT_LIB=PySide6
exec /opt/homebrew/Caskroom/miniforge/base/envs/rf-gnuradio/bin/python -B -u run_desktop.py "$@"
