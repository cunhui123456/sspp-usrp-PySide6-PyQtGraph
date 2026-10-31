"""显式设备初始化的短命子进程；不创建数据目录或任何 RF 数据流。"""

import argparse
from contextlib import contextmanager, redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import re
import sys

EVENT = "DEVICE_ENVIRONMENT_RESULT"
PROTOCOL_VERSION = 1


def progress(message):
    print(f"[设备初始化] {message}", file=sys.stderr, flush=True)


@contextmanager
def stage(name):
    progress(name)
    try:
        yield
    except Exception as exc:
        progress(f"{name}失败：{type(exc).__name__}: {exc}")
        raise
    else:
        progress(f"{name}完成")


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_usrp(profile_path, expected_sha256, usrp_factory=None):
    """先验证文件，再仅打开明确指定的 B210 并读取身份与通道数量。"""
    with stage("校验设备 profile 与配置哈希"):
        payload = Path(profile_path).read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("设备配置已改变，请重新初始化实验环境")
        profile = json.loads(payload)
        serial = profile.get("serial")
        if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", serial):
            raise ValueError("设备配置中的序列号无效")
        channel = profile.get("channel")
        if type(channel) is not int or channel not in (0, 1):
            raise ValueError("B210 通道必须为整数 0 或 1")
        expected_image = profile.get("fpga_sha256")
        if not isinstance(expected_image, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_image):
            raise ValueError("FPGA SHA-256 配置无效")
        if not isinstance(profile.get("fpga"), str) or not profile["fpga"]:
            raise ValueError("缺少 FPGA 镜像路径")
        image = Path(profile["fpga"]).expanduser().resolve()
        if any(character in str(image) for character in (",", "\n", "\r", "\0")):
            raise ValueError("FPGA 镜像路径无效")
    with stage("校验 FPGA 镜像与 SHA-256"):
        if file_sha256(image) != expected_image.lower():
            raise ValueError("FPGA SHA-256 与已审核设备配置不一致")
    with stage("加载 UHD 驱动接口"):
        if usrp_factory is None:
            import uhd
            usrp_factory = uhd.usrp.MultiUSRP
    with stage("打开指定 USRP（可能加载固件 / FPGA，请等待；不启动采样）"):
        usrp = usrp_factory(f"type=b200,serial={serial},fpga={image}")
    try:
        with stage("核对收发通道与设备身份"):
            if channel >= usrp.get_tx_num_channels() or channel >= usrp.get_rx_num_channels():
                raise ValueError("所选设备不提供配置中的收发通道")
            # 当前 UHD Python binding 的身份接口接受通道索引，主板序列号字段为 mboard_serial。
            rx_info = usrp.get_usrp_rx_info(channel)
            tx_info = usrp.get_usrp_tx_info(channel)
            if (str(rx_info.get("mboard_serial", "")) != serial
                    or str(tx_info.get("mboard_serial", "")) != serial):
                raise ValueError("已打开设备的收发主板序列号与所选配置不一致")
            # 不设置频率、增益或天线；不创建 streamer，不发送/接收任何采样。
    finally:
        with stage("释放 USRP 设备句柄"):
            del usrp


def check_serial(port, protocol, reader_factory=None):
    with stage("校验串口与所选协议参数"):
        from sspp_experiments.temperature_pressure.acquisition import CaptureConfig
        CaptureConfig(port, protocol, "初始化检查").validate()
    with stage("加载串口驱动接口"):
        if reader_factory is None:
            from sspp_experiments.temperature_pressure.serial_io import PosixSerialReader
            reader_factory = PosixSerialReader
    with stage("打开所选串口（可能触发板卡复位；不读数据、不发命令）"):
        reader = reader_factory(port, 9600)
    try:
        pass  # 仅确认可独占打开；不读数据、不发命令，也不推定固件协议已验证。
    finally:
        with stage("关闭并释放串口"):
            reader.close()


def main(argv=None, *, result_fd=None):
    """可直接测试的入口；CLI 另在文件描述符层隔离原生驱动日志。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("usrp", "serial"), required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--profile-path")
    parser.add_argument("--profile-sha256")
    parser.add_argument("--port")
    parser.add_argument("--protocol")
    args = parser.parse_args(argv)
    status, message = "ready", "初始化检查成功，设备句柄已释放；采集时将重新打开设备"
    result_stream = sys.stdout
    with redirect_stdout(sys.stderr):
        try:
            if args.kind == "usrp":
                check_usrp(args.profile_path, args.profile_sha256)
            else:
                check_serial(args.port, args.protocol)
                message = "串口打开检查成功，句柄已释放；尚未验证固件数据协议"
        except Exception as exc:
            status, message = "failed", str(exc)[:2000] or "设备初始化检查失败"
            progress(f"初始化检查失败：{type(exc).__name__}: {message}")
    payload = json.dumps({"event": EVENT, "version": PROTOCOL_VERSION,
                          "request_id": args.request_id, "kind": args.kind,
                          "status": status, "message": message, "released": True},
                         ensure_ascii=False, allow_nan=False) + "\n"
    if result_fd is None:
        print(payload, end="", file=result_stream, flush=True)
    else:
        remaining = memoryview(payload.encode("utf-8"))
        while remaining:
            remaining = remaining[os.write(result_fd, remaining):]
    return 0 if status == "ready" else 1


def cli(argv=None):
    """原生 stdout 持续指向 stderr，包含驱动在解释器退出时的日志。"""
    sys.stdout.flush()
    result_fd = os.dup(1)
    try:
        os.dup2(2, 1)
        return main(argv, result_fd=result_fd)
    finally:
        sys.stdout.flush()
        os.close(result_fd)
        # 不恢复 FD 1；驱动后台线程与退出清理仍可能向 stdout 写日志。


if __name__ == "__main__":
    raise SystemExit(cli())
