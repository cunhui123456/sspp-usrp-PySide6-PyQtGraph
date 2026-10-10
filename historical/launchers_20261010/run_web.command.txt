#!/bin/zsh
set -e
cd "${0:A:h}"
exec /opt/homebrew/Caskroom/miniforge/base/envs/rf-gnuradio/bin/python -B -u run_web.py "$@"
