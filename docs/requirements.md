# SSPP 桌面软件需求确认

确认日期：2026-10-09（Asia/Shanghai）。状态：需求已确认，实施与验证按实际记录更新。

## 用户与目标

本人在连接 USRP 的本机进行科研实验，希望连续观察手指／材料变化时的幅度和相位响应，并查看、分析和导出历史实验数据。实时展示是本次核心改善目标。

## 已确认决策

- 使用 **PySide6 + PyQtGraph 纯桌面架构**。不引入 FastAPI，不另建网页前端。
- 保留原项目 `/Users/cunhui/Git-Projects/Academic_Research/sspp-usrp`。
- 新项目放置于 `/Users/cunhui/Git-Projects/Academic_Research/sspp-usrp-PySide6-PyQtGraph`。
- **继续使用既有 Conda `rf-gnuradio` 环境**，不新建环境，不改用 `rf-analysis` 工程。Python 入口是 `/opt/homebrew/Caskroom/miniforge/base/envs/rf-gnuradio/bin/python`。
- 根目录建立 `.env`，提供配置示例和中文运行说明。依赖与配置遵循 Miniforge/Conda 管理习惯。
- 原项目约 1 GB 历史实验数据完整复制至新项目，原数据保留不动；最终大小与一致性以复制核验为准。新实验输出保存在新项目。
- 桌面布局可以重新组织，保留设备配置、扫频、连续幅相、阶段标记、历史记录、分析和导出能力。
- 实时幅相是主视图，时域、频谱和 IQ 为实时诊断视图。未指定精确帧率或延迟 SLA。
- 采集、应用服务、分析和界面模块化，便于未来新增分析与 AI。首版不实现尚未定义的 AI 功能。

## 验收与证据边界

第一阶段只开展离线测试、桌面软件检查和历史数据回放，核验功能、原数据保护与已有结果兼容性。测试通过范围必须与实际证据一致，不把离线回放写成硬件实时采集通过。

**重构后的真实 USRP 采集尚未新验收，须另行确认具体实验后进行。**旧项目已有真实实验记录继续有效，但不自动证明新桌面版已完成设备初始化、收发、扫频、停止或长时稳定性验收。

本次不新增材料分类、毫米位移标定、校准 S21、跨会话绝对相位或生命体征检测结论。TX 数学参考与实测 RX 必须清楚区分；原始 IQ 和 manifest 保持可追溯。

## 笔记正本

Note-Public 中的 `1_Projects/Academic_Research/RF_Vital_Signs_Research/02_Engineering/Decision_SSPP桌面实时实验软件重构_2026-10-09.md` 保存项目决策，并由 RF Project MOC、Engineering MOC 与 `VAULT_MEMORY.md` 导航。源码、配置、测试和数据以本项目文件为准。
