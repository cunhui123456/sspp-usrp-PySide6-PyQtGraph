# SSPP / USRP 扫频实验

## 2026-10-09 中文注释补充

本次给根目录 11 个 Python 文件和两个 `.command` 启动器补充中文注释，覆盖参数单位、收发线程、VERIFY/MEASURE 状态、FFT 幅度、连续锁相/解缠、原始数据契约与停止顺序。只增加 `#` 注释，Python AST 与所有非注释可执行 token 均逐文件对比一致；没有修改算法、默认参数、设备配置或实验数据，也没有为注释验收启动硬件。

下文“原始模块保持原样/未修改”描述 2026-09-08 的复现阶段。当前五个教师模块已增加注释，执行逻辑仍与此次修改前一致；原始字节版本可从 Git `95e6815` 追溯。注释也会改变文件哈希，不能要求新的 run manifest 与旧实验源码哈希相同；旧 manifest 与原始数据继续保留历史版本信息。

建议阅读顺序：`uhd_lib.py` → `thread_works.py` → `do_sweep.py` → `main.py` → `run_local.py`；连续幅相部分先看 `phase_dsp.py` 再看 `run_phase.py`；原版交互包装见 `run_teacher.py`，绘图见 `on_time_draw.py`。`test_run_local.py` 与 `test_phase_dsp.py` 是不连接真实 USRP 的离线检查。

老师原始代码基线：Git `95e6815`。`main.py`、`uhd_lib.py`、`thread_works.py`、`do_sweep.py`、`on_time_draw.py` 保持原样。本机入口 `run_local.py` 复用原 `SweepRunner` 的 VERIFY / MEASURE 流程和 `fft_peak`，补充显式设备选择、数据留存、超时与退出处理。

## 已有环境与设备

使用本机已有 Conda **rf-gnuradio**：Python 3.14.6、UHD 4.10.0.0、GNU Radio 3.10.12.0。缺失的 pandas 3.0.5 与 openpyxl 3.1.5 已补入此环境。未新建环境。Python 包版本见 `requirements-local.txt`；UHD 由现有 Conda 环境提供，勿另装同名 pip 包。

设备配置在本机忽略文件 `local_device.json`，包含序列号、通道、FPGA 路径与 SHA-256。移机请从 `local_device.example.json` 建立自己的配置。配置校验不会自动切换到其他设备。

本次接线由用户确认：SSPP 两端接 **TX1 / RX1**，不放测试物体，记录空载基线。实际短测确认本次接线应使用 **channel 0**：2.4 GHz 单音约 −52.5 dBFS，2.45 GHz 单音约 −52.9 dBFS，均正确跟随期望频率。channel 1 的首次扫频接近噪声，36 点中 24 点单音偏差超限。UHD 的 frontend 名称却是 channel 0 = FE-TX2 / FE-RX2、channel 1 = FE-TX1 / FE-RX1，因此不能根据这些内部名称直接推断这台实物的面板编号。此结论限于本次用户确认的接线，不能推广到所有 B210。代码中的 `tx_ant="TX/RX"`、`rx_ant="RX2"` 是所选通道的 UHD 天线选项，不能把逻辑字符串 `RX2` 直接当成用户面板上的编号。

老师设备显式加载已核验的官方 UHD 4.10 FPGA：SHA-256 `148a5a08ebe2a22147e0e7ec1d78f5453bba83468163978abb171ea78d84a777`。本机默认无后缀镜像是另一台设备使用的定制文件，保持原状。UHD 支持通过设备参数 `fpga=` 单独指定镜像：[B2x0 官方说明](https://files.ettus.com/manual/page_usrp_b200.html)。

## 运行

```sh
conda activate rf-gnuradio
cd /Users/cunhui/Git-Projects/Academic_Research/sspp-usrp
python run_local.py plan --headless
python run_local.py acquire --headless --label empty_baseline
```

`plan` 只校验配置和镜像哈希，不打开硬件。`probe` 初始化设备而不启动数据流。`acquire` 实际发射、接收和扫频。去掉 `--headless` 可显示原程序实时图；也可双击 `run_baseline.command` 启动。图窗关闭或终端 Ctrl+C 结束，退出后再保存结果图，不会为了等待查看汇总图而持续发射。

只启动一个采集进程。默认每点超时 8 秒、全程流传输上限 90 秒；故障或信号验证持续失败时退出并在 manifest 留下失败原因。重新实验会创建新目录。

## 原程序参数和读数含义

| 参数 | 默认值 |
| --- | --- |
| 载波范围 | 2.4–3.0 GHz |
| 点数／轮数 | 12 点 × 3 轮 |
| 载波步进 | 600 MHz / 11 ≈ 54.5454545 MHz |
| 复采样率 | 请求 250 kS/s，记录 UHD 返回的实际值 |
| 基带正频率单音 | 2–13 kHz，每点增加 1 kHz |
| 发射数字幅值 | 0.3 |
| TX／RX 增益 | 20 / 30 dB |
| 切频等待 | 0.1 秒 |
| 验证／测量长度 | 20 / 80 个单音周期 |
| FFT／验收容差 | 2048 / 200 Hz |

实际目标 RF 单音位于实际 TX 载波加基带单音处。CSV 同时保存请求载波、期望和实测基带频率；原始分点 NPZ 内保存 UHD 回读的 TX/RX 载波。没有把载波预先四舍五入到两位小数。

幅度是 Hann 窗归一化 FFT 的局部峰值，标为 `peak_amplitude_dBFS_est`。长数据截取最后 2048 点，短数据补零；不同频点的实际测量窗长度及栅栏误差仍沿用老师算法。这个量适合冻结参数后的相对比较，**不是标定后的 S21、dBm、压力或温度**。三个重复的均值与标准差在 dB 值上计算。

这份代码目前没有 KNN 训练、预测、跨样本验证实现；原 `main.py` 的 KNN 说明是占位。这里也不是论文约 5 GHz 实验参数的自动复现。

## 输出与验收

每次输出在 `Data/runs/<UTC时间>/`，不纳入 Git：

- `manifest.json`：设备本地配置、实际参数、代码哈希、状态、错误、流传输统计与原始文件哈希。含设备标识，仅本机留存，分享前脱敏。
- `sweep_result.json`：老师扫频流程返回的完整数组。
- `measurements.csv`、`sweep_summary.xlsx`：逐点三轮读数和 dB 均值、样本标准差。
- `sweep_summary.png/.pdf`：停止收发后的三轮曲线。
- `iq_point_001.npz` 等：各点被扫频流程取出的复数 IQ、帧起始索引、帧长度、采样率和载波／单音信息，覆盖验证和测量过程，可能含已取出但未参与估计的尾部数据。这不是全会话连续硬件时间戳记录。

`status=complete` 要求完整 3×12 记录（或显式指定的轮数×点数）、测量基带误差小于 200 Hz，且无记录的线程／队列／近满幅错误。它证明本次程序采集完成；空载信号路径和重复性还需看实际频谱与曲线，不能只凭局部峰值通过就推断有用信号，更不能视为材料识别准确率。

相比原入口，本机入口处理 TX 部分发送、RX 元数据错误和队列溢出，保存连续帧索引用于核查，在 finally 中发送 RX stop 与 TX EOB。TX `send()` 超时可能仅发送部分数据，处理依据：[UHD TX streamer API](https://files.ettus.com/manual/classuhd_1_1tx__streamer.html)。现有记录不包含独立射频功率计校准和 TX 异步事件的完整日志。

## 离线检查

```sh
MPLBACKEND=Agg MPLCONFIGDIR=.cache/matplotlib python -m unittest -v test_run_local
```

使用合成 IQ 和虚拟 streamer 检查 12 个单音的两阶段 FFT、部分发送与 EOB、队列间断与超时、36 行结果精度和文件导出；不会打开 USRP。真实硬件验收以本次 run 数据与研究笔记为准。

## 本机首次基线结果（2026-09-08）

有效 run：`Data/runs/20260908T070716_445680Z/`，状态 `complete`，12 点 × 3 轮，实测基带最大偏差 60.546875 Hz；幅度 −65.094 至 −52.536 dBFS 估计值，各频点三轮样本标准差最大 0.3491 dB。36 份原始 IQ 的哈希与帧索引连续性核对通过；每份最后 8192 点的全频谱最大峰均位于目标单音 ±200 Hz 内。核验明细见 `baseline_verification.json`。

RX 无超时、元数据错误、队列溢出或近满幅事件；TX 部分发送计数为 0，EOB 和 RX stop 均已发出，两个工作线程退出。未收集完整 TX 异步事件日志，不能据此宣称所有类型的硬件事件均为零。停止收发后完成导出，进程退出码 0。

切频后，日志里可能短暂出现前一个单音的验证失败；本次原有 0.1 秒等待不足以排空全部在途样本，VERIFY 重试后进入新单音。此次保留原算法，不将这些过渡帧当测量点，全部正式测量单音再经过独立误差核对。后续若修改切频或数据流缓冲策略，应重新采基线。

保留 `20260908T070457_529906Z` 的 channel 1 失败扫频及 `20260908T070614_640439Z` 的 channel 0 两点诊断，前者不能用作有效基线。当前连接已收到稳定扫频单音，但尚无断开 DUT 的串扰对照、绝对功率／S21 标定、换样本重复或材料分类。

## 老师原版交互与图形功能

`run_teacher.py` 调用原 `main.main()`：材料名称输入、老师原 TX/RX 线程、实时 I 分量波形／频谱、RMS 输出、原 Excel 保存函数与原汇总图均执行。五个老师模块仍未修改。本机包装只注入已确认设备配置、记录图形／导出证据、限制最长收发时间，并在显示汇总图之前停止收发。

```sh
conda activate rf-gnuradio
cd /Users/cunhui/Git-Projects/Academic_Research/sspp-usrp
python run_teacher.py
```

或双击 `run_teacher.command`。看到 `请输入当前测量的物质名称：` 时输入当前条件，例如 `empty_baseline`。程序自动扫描 3 轮并显示波形／频谱；终端打印 RMS 和逐点幅度。完成后显示三轮汇总图，**此时已停止收发**；图窗默认 45 秒后自动关闭，或手动关闭。可用 `--summary-seconds 120` 延长查看时间；这不延长射频发射。`--material empty_baseline` 可用于跳过手动输入的自动化运行。收发上限默认 90 秒，可 Ctrl+C 请求停止；不要与其他 USRP 采集进程同时启动。

老师原格式 Excel 仍位于 `Data/train/<材料标签>/<本机时间>.xlsx`，有 12 行、6 列：载波频率、三轮幅度、幅度均值和标准差。原导出器会将 GHz 频率、均值、标准差四舍五入到两位小数，这一格式为复现目的保留。需要高精度逐点频率或完整分点原始 IQ 时，使用 `run_local.py`；原版交互模式只额外保存最后一帧显示用 IQ，并不保存完整原始数据流。

包装证据在 `Data/teacher_runs/<UTC时间>/`：`feature_report.json`、`console.log`、未舍入的 `sweep_result.json`、实时与汇总图 PNG、`last_displayed_iq.npz`。报告含本机设备标识，分享前脱敏。GUI 图显示的是接收基带实部与频谱；RMS 是该帧的总体数字强度，区别于 Excel 里局部 FFT 峰值幅度。

2026-09-08 原版功能验收 run：`20260908T084254_474905Z`，材料标签 `empty_baseline_gui`。QtAgg 实际运行 54 次实时图刷新、6 条 RMS 日志，12 点×3 轮全部通过，最大单音偏差 61.5234 Hz；三轮最大样本标准差 0.2492 dB。Excel 每轮幅度、均值与标准差均与返回数组核对一致。实际导出：`Data/train/empty_baseline_gui/20260908_164318.xlsx`。停止顺序已核查：TX/RX 线程退出 → 汇总图 → GUI 自动关闭 → 进程退出 0。

Qt 图窗运行和导出图内容已验证；工具栏逐项交互、异常断线与 Ctrl+C 中断并未在本轮分别注入测试。原线程仍采用最新一帧队列和原错误处理，正式需要可追溯 IQ 的实验继续推荐 `run_local.py`。KNN 分类、相位／距离计算、温压同步与丝杠控制在老师代码中没有实现，本入口不扩展这些功能。


## 连续相对幅度与相位实验

新增 `run_phase.py` 和 `phase_dsp.py`，保留原教师源码。使用已有 rf-gnuradio 和 `local_device.json` 的显式设备／FPGA；固定载频2.4 GHz、基带10 kHz、250 kS/s、TX/RX增益20/30 dB。

```sh
conda activate rf-gnuradio
python run_phase.py plan --duration 20 --initial-label water_2of3_static
python run_phase.py acquire --duration 20 --initial-label water_2of3_static
python run_phase.py acquire --duration 180 --initial-label water_2of3_first --gui
```

`plan` 不打开设备。`acquire` 会实际收发：前2秒等待、随后5秒静止校准；全程不调频、不重启，每20 ms左右对一块IQ作复数锁相解调。显示初始状态归一化幅度dB及连续展开相位degree。初始常相位被参考消除，仪器缓慢漂移未自动扣除；无第二路RF参考，不是标定S21或跨运行绝对相位。

同一会话改变样品前，在终端输入 `{"mark":"transition_move","settle":0}`；确认稳定放好后输入 `{"mark":"empty_return","settle":3}`，留出3秒过渡后开始该标签。输入 `{"stop":true}`、Ctrl+C、关闭实时窗口或到达时限会停止收发。默认20秒，最大180秒，另有独立墙钟超时保护；不要把时间上限当作后台持续监测授权。

保存 `Data/phase_runs/<UTC>/` 下连续 `rx_iq.cf32`、逐帧硬件时间戳、`amplitude_phase.jsonl`、事件标签、基线指标、相对幅相图和manifest。原始IQ是单通道complex64；处理结束时可能留有已经记录但尚未解调的队列尾部，manifest同时记录原始和处理样本数，不把二者假定相等。接收时间戳不连续、TX欠载／时序错误、信号相干分量过低或相位步进过大均停止并标记失败，不能拼接成连续相位。

初始参考验收门槛：同次基线幅度SD≤0.5 dB、相位线性漂移≤1 degree/s、去线性趋势后的相位SD≤3 degree、相干分量≥0.5。这是预先设定的工程停止门槛，不是材料可分性阈值，也不代表允许忽略慢漂移。相位原值保留，不自动去趋势。标签来自操作者，代码没有分类器。

离线核验：`python -B -m unittest -v test_phase_dsp.py`，覆盖已知复数幅相、跨±180°相位变化、漂移暴露、噪声拒绝、单样本缺失与部分TX发送。首次实际静止参考及后续物理条件结果以对应manifest和笔记为准。


### 2026-09-08 连续幅相实验验收

静止20秒 `Data/phase_runs/20260908T115810_362027Z` 通过。随后 `20260908T120220_240708Z` 在同一180秒连续收发中，完成水约三分之二 → 倒空有水滴 → 重新装水约三分之二的操作标记与Qt实时幅相显示。原始45,005,000个复数样本、9,001个解调窗口离线重算一致，硬件时间戳连续；两个RX timeout都发生在定时开始之前，采集中无超时、无TX异步错误，收发和线程已停止。

倒空相对初始水瓶均值变化+3.6781 dB、+45.0996°；重新装水相对初始仍偏离+1.4384 dB、+10.0795°，为部分恢复。末段确认放稳后仅5.74秒。标准差描述连续20 ms技术窗口，不代表独立取放重复性；一个周期不能分离材料、负载/形变、摆放和仪器漂移，也没有训练材料分类器。具体阶段、原始数据核验、图像和分析代码在该运行目录的 `cycle_analysis.json`、`analyze_cycle.py` 和 `sspp_water_drain_refill_phase_20260908T120220Z.png`。既有扫频数据不能跨重启拼接相位。
