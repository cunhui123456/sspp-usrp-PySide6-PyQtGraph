# SSPP 本机实验软件

在这台 Mac 的浏览器中操作真实 B210，完成扫频、连续幅相记录和手动条件标记。服务仅监听 `127.0.0.1`；启动网页不会打开设备或开始收发，没有模拟实验模式。

## 启动与使用

双击 `run_web.command`，或在已有 `rf-gnuradio` 环境运行：

```sh
conda activate rf-gnuradio
cd /Users/cunhui/Git-Projects/Academic_Research/sspp-usrp
python -B run_web.py
```

打开 <http://127.0.0.1:8768>。`--port 8769` 可更换端口。

1. 选择设备配置、扫频或连续幅相，填写初始条件、实验标签和传感器名称。
2. 启动前调整参数；运行和保存期间参数锁定。“检查参数”只校验配置及 FPGA 哈希，不初始化 USRP。
3. 确认现场接线、固定线缆和初始状态后勾选确认框，点击“开始实验”。
4. 扫频逐点绘制各轮幅度；连续幅相先等待约 2 秒，再建立 5 秒静止基线。基线通过后可手动标记靠近、远离、材料状态或自定义阶段。
5. 过渡等待默认 3 秒。接触传感器或线缆的情况单独勾选记录；阶段按钮仅标记操作指令，实际距离未测量。
6. 点击“停止收发并保存”或等待时限结束，确认收发停止，再从“实验记录”查看曲线和下载文件。
7. 在“实验图像与诊断”切换接收诊断、扫频分析、幅相分析或采集质量。每张图可放大、指针读数、保存当前视图；停止采集后可生成全部PNG和多页PDF图册。历史实验有完整原始数据时同样支持。

完整扫频提供12张分析图，带有效参考及硬件帧记录的连续幅相提供24张。包括RX时域、频谱、IQ复平面、抽样时频图、TX数学参考、扫频重复性、幅相响应/分布/波动、阶段和硬件连续性。详见 [图形说明与判读](docs/visualizations.md)。TX参考是数字设定计算，不是对实际TX输出的测量。

关闭网页不停止实验，重开页面可恢复当前状态；关闭服务终端会请求停止。只有 manifest 同时确认 TX EOB、RX stop 和线程退出，网页才显示收发停止已确认。外部命令行/历史老师 GUI 不应与网页采集同时运行。

## 默认参数和设备

扫频：2.4–3.0 GHz，12 点×3 轮，基带 2–13 kHz，250 kS/s，数字幅值 0.3，TX/RX gain 20/30 dB，单点 8 秒、总时限 90 秒。连续幅相：2.4 GHz、10 kHz、相同采样率/幅值/增益，默认 120 秒，允许 10–180 秒。

可调整范围：载频 70 MHz–6 GHz（网页输入 0.07–6 GHz）、采样率 200 kS/s–2 MS/s、数字幅值 0.001–0.3、TX gain 0–30 dB、RX gain 0–76 dB。服务端同时检查 Nyquist、整数数量和时限。默认扫频仍为2.4–3.0 GHz、默认连续载频仍为2.4 GHz；当前真实验收覆盖该默认配置。70 MHz–6 GHz已通过参数边界与无收发plan检查，未进行全频段真实扫频。

网页只读取设备配置，不执行 UHD 发现或 probe：自有机使用 `Data/device_profiles/own_device_20261008.json`，老师机使用 `local_device.json`。设备序列号不显示在网页，原配置/FPGA 不改写。用户确认的 TX1/RX1 实测为 channel 0；UHD 的逻辑天线名不直接代表面板编号。

## 项目结构

```text
sspp_lab/
  hardware.py          当前硬件适配层
  sweep_logic.py       VERIFY/MEASURE 与 FFT 估计
  dsp.py               锁相、解缠、同会话基线
  acquire_sweep.py     扫频、分点 IQ 留存和超时
  acquire_phase.py     连续 IQ、时间戳与幅相
  display.py           命令行 GUI 兼容显示
  controller.py        子进程、单实验锁、手动标记、历史记录
  diagnostics.py       真实IQ/CSV/JSONL的只读图形计算
  export_figures.py    后台PNG与PDF图册导出
  web_server.py        本机 HTTP API
  web/                 浏览器界面，无额外构建依赖
historical/teacher_20260908/
  main.py uhd_lib.py thread_works.py do_sweep.py on_time_draw.py
  run_teacher.py       历史 GUI 包装，调整引用路径和参数兼容
  source_hashes.json   五个老师模块的归档哈希
Data/                  全部历史和新增实验数据，原位置保留
tests/                 网页控制与访问边界回归
```

五个老师模块以 2026-10-09 中文注释版原字节搬入历史目录；最初版本由 Git `95e6815` 追溯，新软件不修改历史模块。原始 README 亦在历史目录保留。旧 manifest 的源码哈希继续代表旧实验，新增模块另存当前来源哈希。

兼容入口：`run_local.py` → `sspp_lab.acquire_sweep`，`run_phase.py` → `sspp_lab.acquire_phase`，`phase_dsp.py` 兼容旧 DSP 导入，`run_teacher.py` 启动历史老师 GUI。CLI 的默认 `local_device.json` 仍是老师配置；自有机必须显式指定 `--device-config`。

## 数据与测量边界

扫频在 `Data/runs/<UTC>/` 保存 manifest、CSV、Excel、曲线 PNG/PDF 和分点 NPZ。分点 IQ 包含控制器取出的 VERIFY/MEASURE 帧，不是全会话硬件时间戳连续记录。FFT 加窗幅度是 dBFS 估计，不是 S21 或 dBm。

连续幅相在 `Data/phase_runs/<UTC>/` 保存单通道 complex64 `rx_iq.cf32`、逐帧硬件时间戳、逐窗幅相、阶段事件、基线与曲线。时间戳间断、队列溢出、异常 TX 事件或不合格参考会停止并记为未完成。原始与处理样本数分别记录，不假设队列尾部均已解调。

网页附加 `web_context.json` 保存传感器名称、初始条件确认、参数、手动标记、接触报告和停止确认。现有扫频/幅相实验仍可查看。原采集 manifest 含本机设备标识，不作为网页下载文件；每运行另存已脱敏的 `web_console.log`。

网页显示最多约 2500 个点；完整数据继续保存在文件中。相位仅相对同次初始参考，未自动扣除仪器漂移；不跨会话比较绝对相位。首版不调用 `rf-analysis`，不标定毫米位移，不执行材料分类、温压同步或位移平台控制。

新增诊断图每3秒从已落盘数据刷新。连续IQ预览使用最新完整8192样点；扫频使用最新完整NPZ，运行时可能滞后一频点。时频图抽取最多64个真实短窗；不将间隔补成连续测量。缺失测量及单轮不可估计SD保持空缺，异常/未完成记录保留原状态。图册新增 `diagnostic_*.png`、`diagnostic_report.pdf`、`diagnostic_figures.json`，不改写原始IQ、CSV或历史manifest；来源哈希和原manifest哈希保存在图册索引中。导出期间暂缓开始新采集。

## 验收

```sh
MPLBACKEND=Agg python -B -m unittest -v test_run_local test_phase_dsp
python -B -m unittest discover -s tests -v
```

离线回归只验证软件契约。最终必须从网页完成真实 12 点×3 轮扫频和带手动标记的连续幅相采集，核查实时曲线、原始 IQ、阶段/参数记录、TX EOB、RX stop 和线程退出；不以模拟或历史数据代替新实验验收。

设备初始化超过30秒会请求中止；停止请求后先等待正常退出，再有界终止进程。若缺少TX EOB、RX stop或线程退出证据，界面不会声称停止已确认；重新连接设备后再开始。

2026-10-09 自有 B210 真实网页扫频、30秒幅相及手动停止验收已通过，22项离线回归通过。详见 [验收记录](docs/acceptance_20261009.md)。此次新软件实际验收为空载，手指/材料新动作未执行。
