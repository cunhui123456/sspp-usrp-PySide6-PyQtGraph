# 老师 SSPP 历史版本

保存老师的五个模块及 2026-10-09 已补充的中文注释。五个文件按搬移前的原始字节保留，哈希见 source_hashes.json；最初无注释补充版本由 Git 95e6815 追溯。本目录独立于当前网页采集实现，后续不在这里修改算法。

main.py、uhd_lib.py、thread_works.py、do_sweep.py、on_time_draw.py 保留原相互导入关系。运行 root/run_teacher.py 可继续通过旧包装复现老师交互流程；它会实际收发，不用于启动新网页软件。

当前运行软件和新增采集代码位于 sspp_lab/。历史数据保留在项目 Data/ 原位置，旧 manifest 中的源码哈希继续对应旧实验。
