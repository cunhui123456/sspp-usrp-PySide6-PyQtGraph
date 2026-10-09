# SSPP 实验室 · PySide6 / PyQtGraph

本机科研桌面软件：USRP 扫频、连续相对幅相、人工阶段标记、实时 RX 诊断、历史回放和图册导出。
从 `sspp-usrp` 派生，原项目保留；新项目不运行 HTTP 服务，也不需要 FastAPI。

## 启动

继续使用现有 **Conda `rf-gnuradio`**，不建立第二个环境。该环境负责 Python、GNU Radio、UHD 和科学计算依赖。

```sh
conda activate rf-gnuradio
cd /Users/cunhui/Git-Projects/Academic_Research/sspp-usrp-PySide6-PyQtGraph
python -B run_desktop.py --check
python -B run_desktop.py
```

也可在 Finder 双击 `run_desktop.command`，它使用本机既有环境的绝对路径。
启动窗口、查看历史、回放或“检查参数”均不打开 USRP；只有明确点击开始采集后才进入真实实验。

桌面增量依赖登记在 `requirements-desktop.txt`。仅环境缺少这些包时，在 `rf-gnuradio` 中运行：

```sh
python -m pip install -r requirements-desktop.txt
```

不要用 pip 重装另一套 UHD，不升级/替换既有 FPGA 镜像，不修改全局 `.zshrc`。
本机验证环境：Python 3.14.6、PySide6/Qt 6.12.0、PyQtGraph 0.14.0、GNU Radio 3.10.12.0。

## 使用顺序

1. 选择自有或老师 B210 配置、扫频或连续幅相模式，填写实验标签和传感器说明。
2. 调整采集参数，检查参数，并按实际情况确认设备接线、线缆固定与初始状态。
3. 开始采集，观察实时曲线和 RX 时域、频谱、IQ 图；运行中参数固定。
4. 连续幅相先保持初始状态建立参考，再添加阶段标记、过渡等待和接触情况。
5. 点击停止后等待保存和停止证据。`stop_unconfirmed` 不能当作停止已确认。
6. 选择历史记录查看分析、回放、打开或另存文件；停止采集后生成 PNG/PDF 图册。

连续幅相前约 2 秒等待、5 秒建立参考。参考未建立时相对幅相保持未知，RX 原始 IQ 仍可预览。
扫频是逐点测量：完成一个频点就增加结果点；当前频点的 RX 预览来自采集帧，不等待 NPZ 保存。

连续幅相按设备实际主时钟的整数计数检查 RX 连续性，避免绝对时间换算成采样点时的舍入误报；真实丢样或重复仍会停止。
发生时间戳错误时，历史详情显示原因，`rx_timestamp_failure.json` 保留被拒绝帧的原始时间与偏差；原始 IQ 只包含已接受帧。
旧记录的采样率取整时间戳仍保留，但单独的 ±1 点差值不能证明真实丢样。

## 实时显示与原始数据

- 完整 IQ、逐窗幅相和采集质量证据按原采集协议保存。
- `TRACE` 默认每 100ms 批量发布处理点，界面默认每 100ms 更新。
- `PREVIEW` 默认每 250ms 发布最新真实 RX 窗口，FFT 在独立输出线程计算。
- 显示通道有界；慢消费者只能丢显示批次，不丢原始记录。序号缺口作为断线呈现并计数，不补造数据。
- 控制、采集、绘图、重分析各有执行边界。显示与落盘解耦，不再通过反复读取增长文件刷新主曲线。
- 刷新参数是配置目标，不代表已测得的真实硬件端到端延迟；详见 `docs/acceptance_desktop.md`。

历史连续幅相回放按保存的样点时间播放，预览来自只读原始 IQ；不会连接硬件。
旧扫频 CSV 没有逐点采集时间，回放按每点 1 秒演示，界面明确说明此时间不是原实验速度。

## 配置和数据

项目根 `.env` 已建立，模板为 `.env.example`。进程环境变量优先于文件值；修改后重启应用。
`.env`、设备身份配置和 `Data/` 均被 Git 忽略。

| 配置 | 默认值 | 用途 |
|---|---|---|
| `SSPP_CONDA_ENV` | `rf-gnuradio` | 已确认的运行环境 |
| `SSPP_DEFAULT_PROFILE` | `own` | 初始设备选项，不自动开始实验 |
| `SSPP_OWN_PROFILE` | `Data/device_profiles/own_device_20261008.json` | 自有机配置 |
| `SSPP_TEACHER_PROFILE` | `local_device.json` | 老师机配置 |
| `SSPP_UI_INTERVAL_MS` | `100` | 图表更新间隔 |
| `SSPP_TRACE_INTERVAL_MS` | `100` | 幅相批次发布间隔 |
| `SSPP_PREVIEW_INTERVAL_MS` | `250` | RX 诊断预览发布间隔 |
| `SSPP_MAX_DISPLAY_POINTS` | `20000` | 主曲线内存点数上限；完整数据仍保存 |

历史 `Data/` 已完整复制，新采集也写本项目的 `Data/`。设备配置继续引用原有 FPGA 资产，镜像不另行复制或替换。
743 个历史数据及老师代码文件共 1,049,012,908 字节完成逐文件 SHA-256 核验，老师归档权限保持；证据见 `docs/migration_integrity.json`。
旧 manifest、原始数据和旧 `web_context.json` 均保留；新桌面实验另存 `desktop_context.json`。

## 结构和后续扩展

```text
sspp_desktop/        配置、桌面服务、PySide6窗口、PyQtGraph图表、历史回放
sspp_lab/            采集、DSP、硬件适配、实时发布、诊断、离线图册
Data/                历史副本和新的真实实验记录（Git忽略）
tests/               不连接设备的控制、实时通道和桌面回归
historical/
  teacher_20260908/  老师代码冻结副本
  web_20261009/      原网页源文件与说明，仅供追溯
docs/                需求、设计、迁移核验与桌面验收记录
run_desktop.py       桌面入口
run_desktop.command  macOS启动器
.env                本机配置（Git忽略）
```

为保留既有采集脚本与 source hash 相对路径，本次继续使用根级 Python 包，不强行移动到 `src/`。
新增分析算法放在独立分析模块，由服务层调用，耗时任务交给后台工作线程/进程；Qt 控件只负责交互和显示。
后续 AI 可通过服务层消费只读实验数据或分析结果，不在接收线程内执行推理。本次没有伪造 AI 按钮或空接口。

CLI `run_local.py`、`run_phase.py`、`phase_dsp.py` 保留。历史 `run_teacher.py` 仅供原老师 GUI 追溯与兼容。
原网页快照已移入 `historical/web_20261009/`，不是本项目支持的启动入口。

## 验证与科研边界

```sh
conda activate rf-gnuradio
QT_QPA_PLATFORM=offscreen PYQTGRAPH_QT_LIB=PySide6 MPLBACKEND=Agg python -B -m pytest -q
python -B run_desktop.py --check
```

离线测试/历史回放证明软件行为与已保存数据一致，不代替新的真实 RF 采集验收。
数字幅值和频谱为 dBFS 估计，不是标定 dBm/S21；相位相对同次初始参考，不跨会话比较绝对相位，不自动得到毫米位移或材料分类。
实际采集仍使用明确的设备配置、原有参数边界和 RF 停止证据，不因桌面重构放宽限制。
