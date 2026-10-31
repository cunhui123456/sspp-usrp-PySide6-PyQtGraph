"""Arduino 温压电信号采集；独立显式固件准备，不换算物理温压。"""

from .acquisition import SerialCaptureSession, CaptureConfig
from .protocol import LineParser, PROTOCOLS
from .serial_io import list_serial_ports

__all__ = ["SerialCaptureSession", "CaptureConfig", "LineParser", "PROTOCOLS", "list_serial_ports"]
