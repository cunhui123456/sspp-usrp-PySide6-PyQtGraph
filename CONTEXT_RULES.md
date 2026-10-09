# SSPP 桌面项目规则

- 用户于 2026-10-09 确认采用纯桌面 PySide6 + PyQtGraph，不采用 FastAPI，不增加网页后端。
- **继续使用既有 Conda `rf-gnuradio` 环境**；不要自动创建另一个环境，不把该名称误解为 Python 库或 `rf-analysis` 仓库。
- 项目根 `.env` 作为本机配置，提交 `.env.example`，不要提交真实 `.env`、设备身份或实验原始数据。
- 原项目 `/Users/cunhui/Git-Projects/Academic_Research/sspp-usrp` 不修改；本项目的历史 Data 副本与老师冻结代码均保留来源和哈希。
- 主线是本机个人实验：扫频、连续相对幅相、人工阶段标记、历史、诊断、导出；AI 只作为后续扩展方向。
- 采集子进程不依赖 Qt。GUI 使用主线程，耗时预检、历史读取、分析和导出在后台执行。
- 实时显示不能阻塞原始 IQ 保存和停止流程。预览丢包必须可识别，不插值伪造实验数据，不将回放标为实时采集。
- 不修改历史 manifest、原始记录或冻结教师文件。派生图册可单独生成并记录算法来源。
- 不自动探测/初始化/发射 USRP。真实实验必须有用户对本次设备与接线、参数、现场条件的明确确认。
- 参数与设备配置沿用现有校验。进程退出不等于已确认 RF 停止；必须保留 EOB、RX stop 和 workers_stopped 证据。
- 中文界面与说明，Python snake_case，模块功能明确；不要无关重写原采集/DSP算法。
- 本次约定先做离线回归和真实历史回放，硬件新验收另行确认。事实、目标和未验证能力须分开记录。
- 项目事实笔记：Note-Public 的 `1_Projects/Academic_Research/RF_Vital_Signs_Research/02_Engineering/Decision_SSPP桌面实时实验软件重构_2026-10-09.md`。
