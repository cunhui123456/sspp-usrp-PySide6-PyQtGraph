"""串口选择界面的名称过滤；不探测端口，不推断板卡或协议。"""
from pathlib import PurePath


def user_serial_ports(ports):
    """排除已知系统虚拟端口，保留 USB 和其他用户串口。"""
    system_names = {"bluetooth-incoming-port", "debug-console", "wlan-debug"}
    result = []
    for port in ports:
        name = PurePath(port).name.casefold()
        name = name.removeprefix("cu.").removeprefix("tty.")
        if name not in system_names:
            result.append(port)
    return result


def arduino_serial_ports(ports):
    """仅列当前 Nano 工作流支持的 USB 串口候选，不认定板型或固件。"""
    prefixes = ("usbserial", "usbmodem", "wchusbserial", "slab_usbtouart")
    return [port for port in user_serial_ports(ports)
            if PurePath(port).name.casefold().removeprefix("cu.").startswith(prefixes)]
