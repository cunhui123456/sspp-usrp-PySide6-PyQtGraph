# -*- coding: utf-8 -*-
# ============================================================================
# 文件：thread_works.py
# 代码来源：老师提供的 SSPP / B210-B200 扫频实验代码，原始 Git 基线 95e6815。
# 本机维护：2026-10-09 补充中文注释；原有可执行语句、参数和 docstring 保持不变。
#
# 文件职责：老师原版 TX/RX 工作线程，以及队列清理/首帧等待工具。
# 调用关系：main.py 创建线程调用本模块；run_teacher.py 包装原线程记录证据。
# TX 数据流：共享 args.tone → 连续样点编号 → amp × exp(j2πtone·n/fs_tx)
#            → complex64 数组 → tx_streamer.send() → USRP 发射。
# RX 数据流：rx_streamer.recv() → 复制有效 IQ 区间 → (start_index, samples)
#            → raw_queue → SweepRunner / 实时显示。
# 输入：UHD streamer、共享 SDRConfig、stop_event；RX 还接收 raw_queue。
# 输出：发射单音、接收帧队列、终端启动/错误/停止日志；本模块不保存文件。
# 队列格式：start_index 是成功接收样本的累计软件编号，samples 是一帧 complex64 IQ。
# 编号边界：start_index 不是硬件时间戳；设备丢样不能只靠累计软件编号识别。
# 基带切换：SweepRunner 修改 args.tone，TX 下一块读取新值；不作跨频点相位比较。
# 缓冲长度：TX 每块 10000 点；RX 容量由 args.nsamps 决定，以 recv 返回长度为准。
# 队列策略：safe_put_queue() 在满队列时丢弃旧帧，优先最新帧，适合原版实时显示。
# 首帧等待：wait_first_rx_data() 消耗首帧；多次等待仍无数据时警告后继续。
# 停止策略：stop_event 结束循环；TX 收尾发送 end_of_burst，RX stop 由主入口负责。
# 原版限制：send() 部分发送未补发，RX 错误打印后继续；不保证全会话 IQ 无间断。
# 连续采集：run_local.py 使用自己的受限线程；run_phase.py 另有硬件时间戳、
#           部分发送补发和 TX 异步事件检查，不使用这里的丢旧帧策略。
# 本模块不执行 FFT、幅相归一化、位移换算、Excel 或分类。
# ============================================================================
"""
扫频实验线程模块。

职责：
1. TX 线程：连续发送当前 args.tone 对应的基带 tone；
2. RX 线程：连续接收 IQ 数据；
3. 队列辅助函数：清空队列、等待第一帧数据、安全放入队列。

队列格式：
    raw_queue 中的数据统一为：
        (start_index, samples)
其中：
    start_index 是当前 samples 在连续 RX 流中的起始样点编号；
    samples 是一帧 complex64 IQ 数据。
"""

import time
from queue import Empty, Full

import numpy as np
import uhd

# 非阻塞排空当前队列；不能清除 USB/设备内部尚在途的样本。
def clear_queue(data_queue):
    """清空队列，避免旧数据影响新频点测量。"""
    while True:
        try:
            data_queue.get_nowait()
        except Empty:
            break

# 队列满时丢旧帧保最新帧；start_index 可暴露间断，但原 SweepRunner 不验证它。
def safe_put_queue(data_queue, item):
    """
    向队列放入数据。

    如果队列已满，先丢掉旧数据，再放入新数据。
    对实时扫频来说，宁可丢旧帧，也不要积压旧数据。
    """
    try:
        data_queue.put_nowait(item)
        return
    except Full:
        pass

    try:
        data_queue.get_nowait()
    except Empty:
        pass

    try:
        data_queue.put_nowait(item)
    except Full:
        pass

# 取出并消耗第一帧，确认 RX 有数据；失败只警告，保持教师原逻辑。
def wait_first_rx_data(data_queue, timeout=0.5, max_tries=30):
    """
    等待第一帧 RX 数据。

    如果超时没有收到数据，打印警告但继续运行。
    """
    first_item = None

    for _ in range(max_tries):
        try:
            first_item = data_queue.get(timeout=timeout)
            break
        except Empty:
            pass

    if first_item is None:
        print("WARNING: No RX data, but continuing...")
    else:
        print("First RX data received.")

    return first_item

# 按当前共享 tone 生成复指数单音；本原版线程不补发 send() 返回的短块。
def tx_worker(tx_streamer, args, stop_event):
    """
    TX 发射线程。

    持续读取 args.tone，并生成对应基带 tone。
    SweepRunner 切换频点时会同步修改 args.tone，
    因此 TX 线程下一轮 buffer 会自动使用新的基带频率。
    """
    buff_len = 10000
    # 编号跨发送块累积，固定 tone 时避免块边界重置相位；切 tone 后不保证跨频点相位可比。
    sample_index = 0

    tx_md = uhd.types.TXMetadata()
    tx_md.start_of_burst = True
    tx_md.end_of_burst = False
    tx_md.has_time_spec = False

    print("[TX] 发射线程启动。")

    while not stop_event.is_set():
        fs_tx = getattr(args, "tx_rate_actual", args.rate)
        tone = args.tone

        n = np.arange(
            sample_index,
            sample_index + buff_len,
            dtype=np.float64
        )
        sample_index += buff_len

        buff = (
            args.amp *
            np.exp(1j * 2.0 * np.pi * tone * n / fs_tx)
        ).astype(np.complex64)

        try:
            # 原版未检查返回样本数；run_local/run_phase 的线程会补发短块。
            tx_streamer.send(buff, tx_md)
        except Exception as e:
            print("[TX] send exception: %s" % str(e))
            time.sleep(0.1)
            continue

        tx_md.start_of_burst = False

    tx_md.start_of_burst = False
    tx_md.end_of_burst = True

    try:
        tx_streamer.send(np.zeros(1, dtype=np.complex64), tx_md)
    except Exception as e:
        print("[TX] stop send exception: %s" % str(e))

    print("[TX] 发射线程停止。")

# 仅复制 recv() 返回的有效区间；原缓冲区下次接收会覆盖，因此必须 copy()。
def rx_worker(rx_streamer, args, stop_event, raw_queue):
    """
    RX 接收线程。

    不断接收 IQ 数据，并把最新一帧放入 raw_queue。
    """
    rx_md = uhd.types.RXMetadata()
    rx_buff = np.zeros((1, args.nsamps), dtype=np.complex64)

    # 这是成功接收样本的累计索引，不是硬件时间戳；设备丢样不能仅靠它识别。
    start_index = 0

    print("[RX] 接收线程启动。")

    while not stop_event.is_set():
        try:
            num_rx = rx_streamer.recv(rx_buff, rx_md, timeout=0.5)
        except Exception as e:
            print("[RX] recv exception: %s" % str(e))
            time.sleep(0.1)
            continue

        if rx_md.error_code != uhd.types.RXMetadataErrorCode.none:
            print("[RX] metadata error: %s" % rx_md.strerror())
            continue

        if num_rx <= 0:
            continue

        samples = rx_buff[0, :num_rx].copy()
        item = (start_index, samples)
        start_index += num_rx

        safe_put_queue(raw_queue, item)

    print("[RX] 接收线程停止。")