# -*- coding: utf-8 -*-
# ============================================================================
# 文件：main.py
# 代码来源：老师提供的 SSPP / B210-B200 扫频实验代码，原始 Git 基线 95e6815。
# 本机维护：2026-10-09 补充中文注释；原有可执行语句、参数和 docstring 保持不变。
#
# 文件职责：老师的 SSPP 扫频交互主入口，组织整次实验。
# 调用关系：
#   main → uhd_lib.SDRConfig / USRPManager：创建参数、配置设备和数据流。
#   main → thread_works.rx_worker / tx_worker：启动接收与发射线程。
#   main → do_sweep.SweepRunner：执行 VERIFY → MEASURE → 下一个频点。
#   main → on_time_draw.OnTimeDraw：显示最新接收帧的时域、频谱和 RMS。
#   main → do_sweep 导出函数：保存 Excel、打印汇总并显示多轮曲线。
# 运行流程：输入条件名称 → 配置 USRP → RX 首帧 → TX → 3 轮扫频 → 导出 → 停止。
# 输入：终端材料/条件标签、SDRConfig 默认参数、来自 RX 的复数 IQ。
# 输出：Data/train/<标签>/<主机本地时间>.xlsx、实时图、汇总图和终端读数。
# 主要默认参数：channel 0，250 kS/s，数字幅值 0.3，TX/RX gain 20/30 dB。
# 扫描参数：2.4–3.0 GHz、12 点×3 轮，基带单音随频点从 2 kHz 增至 13 kHz。
# 线程协作：长度为 1 的 raw_queue 保存最新帧，stop_event 传递退出请求。
# 数据含义：记录的是数字 FFT 幅度；材料名称只是操作者标签，KNN 入口尚未实现。
# 停止顺序：原 main 在阻塞汇总图返回后执行 finally；图打开时原流程尚未收尾。
# 本机入口：run_teacher.py 复用此交互流程并在汇总图前停止 RF；
#           run_local.py 用于有时限、可留存分点原始 IQ 的正式扫频。
# 设备选择：直接运行本文件使用空 UHD 地址；包装入口会注入显式设备配置。
# 本文件不执行连续相位或毫米位移估计；相关新增代码为 run_phase.py / phase_dsp.py。
# ============================================================================
"""
B210/B200 扫频主程序。
"""
import signal
import threading
import time
from queue import Queue
from datetime import datetime#改


from uhd_lib import SDRConfig, USRPManager
from thread_works import (
    tx_worker,
    rx_worker,
    wait_first_rx_data
)
from on_time_draw import OnTimeDraw
from do_sweep import (
    SweepConfig,
    SweepRunner,
    save_sweep_summary_excel,
    plot_sweep_summary,
    print_sweep_summary
)

RAW_QUEUE_SIZE = 1
# RX 线程到扫频处理之间的原始 IQ 队列长度。
# 扫频和实时绘图只需要最新数据，所以这里设为 1。

# 组织一次原版交互扫频；共享 stop_event 用于 Ctrl+C 和线程协同退出。
def main():
    stop_event = threading.Event()
    signal.signal(signal.SIGINT, lambda s, f: stop_event.set())
    
    material_name = input("请输入当前测量的物质名称：").strip()#改

    if not material_name:
        raise ValueError("物质名称不能为空。")

    # 避免名称中出现路径字符
    invalid_chars = '<>:"/\\|?*'
    material_name = "".join(
        "_" if char in invalid_chars else char
        for char in material_name
    )

    # 老师导出文件名采用主机本地时间；受限入口的 run 目录另用 UTC。
    session_name = datetime.now().strftime("%Y%m%d_%H%M%S")#改

    # 1. 参数与对象初始化
    # ========================================================
    # 无参数 SDRConfig 使用设备空地址；包装入口会注入经确认的显式设备配置。
    args = SDRConfig()
    args.validate()
    usrp_mgr = USRPManager(args)
    usrp_mgr.configure()
    tx_streamer, rx_streamer = usrp_mgr.create_streamers()

    # 2. 创建线程
    # ========================================================
    raw_queue = Queue(maxsize=RAW_QUEUE_SIZE)
    rx_thread = threading.Thread(
        target=rx_worker,
        args=(rx_streamer, args, stop_event, raw_queue),
        daemon=True
    )

    tx_thread = threading.Thread(
        target=tx_worker,
        args=(tx_streamer, args, stop_event),
        daemon=True
    )

    # 3. 启动 RX/TX
    # ========================================================
    # 先确认 RX 数据通路，再启动 TX；收到第一帧只证明 RX 有数据，不证明 DUT 传输。
    usrp_mgr.start_rx_stream()
    rx_thread.start()
    print("Waiting for first RX data...")
    wait_first_rx_data(raw_queue)
    tx_thread.start()
    time.sleep(0.3)

    print("========== Running ==========")

    drawer = OnTimeDraw(args)
    sweep_result = None
    # sweep_cfg = SweepConfig()
    
    sweep_cfg = SweepConfig(#改
    sweep_repeat=3,
    save_dir=f"./Data/train/{material_name}",
    save_filename=f"{session_name}.xlsx"
    )#改

    try:
        # 4. 执行扫频
        # ====================================================
        sweep_runner = SweepRunner(
            args=args,
            sweep_cfg=sweep_cfg,
            usrp_mgr=usrp_mgr,
            raw_queue=raw_queue,
            stop_event=stop_event,
            drawer=drawer
        )

        sweep_result = sweep_runner.run(enable_plot=True)

        # 5. 保存、绘图、汇总
        # ====================================================
        if sweep_result is not None and sweep_result["all_amp_records"]:
            print_sweep_summary(sweep_result)

            save_sweep_summary_excel(
                sweep_result,
                sweep_cfg.save_dir,
                sweep_cfg.save_filename
            )

            # 此原调用会阻塞至汇总图关闭；本文件 finally 在它返回后才执行。
            plot_sweep_summary(sweep_result)
        else:
            print("未记录到数据。")

        # ====================================================
        # 6. KNN 分类入口预留
        # ====================================================
        # 后续可以新增：
        #     from b210_sweep.knn_classifier import classify_sweep_result
        #
        # 然后调用：
        #     classify_result = classify_sweep_result(sweep_result)
        #     print(classify_result)

    except KeyboardInterrupt:
        pass

    finally:
        print("")
        print("正在停止...")
        stop_event.set()
        usrp_mgr.stop_rx_stream()
        # join 有超时；原入口未逐项验收线程确已退出，包装入口会补充记录。
        tx_thread.join(timeout=2.0)
        rx_thread.join(timeout=2.0)
        drawer.close()
        print("停止。")


if __name__ == "__main__":
    main()