"""Qt 与独立 rf-analysis Python 的有界 JSON 子进程接口。

不把分析环境的 NumPy/模型库导入采集进程，也不启动服务器或接触硬件。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6 import QtCore

DEFAULT_ANALYSIS_PYTHON = Path("/opt/homebrew/Caskroom/miniforge/base/envs/rf-analysis/bin/python")
ACTIONS = frozenset({"list", "inspect", "response", "save_annotations", "material_train",
                     "material_predict", "finger_calibrate", "finger_validate",
                     "sensor_calibrate", "sensor_validate", "sensor_predict"})


def analysis_defaults(root: Path) -> tuple[Path, Path]:
    project = os.environ.get("SSPP_ANALYSIS_PROJECT") or str(Path(root).parent / "rf-analysis")
    python = os.environ.get("SSPP_ANALYSIS_PYTHON") or str(DEFAULT_ANALYSIS_PYTHON)
    return Path(project).expanduser().resolve(), Path(python).expanduser().absolute()


def validate_payload(request: dict) -> bytes:
    """拒绝模糊相对来源和非 JSON 数值；所有路径由明确的项目选择产生。"""
    if not isinstance(request, dict) or request.get("action") not in ACTIONS:
        raise ValueError("分析操作无效")
    captures = request.get("captures", [])
    if not isinstance(captures, list) or any(not isinstance(p, str) or not Path(p).is_absolute() for p in captures):
        raise ValueError("分析来源必须是绝对实验目录")
    if request["action"] != "list" and not captures:
        raise ValueError("请先勾选已完成的实验记录")
    if not isinstance(request.get("parameters", {}), dict):
        raise ValueError("分析参数必须是对象")
    for name in ("model", "analysis_root"):
        if request.get(name) is not None and (not isinstance(request[name], str) or not Path(request[name]).is_absolute()):
            raise ValueError(f"{name} 必须是绝对路径")
    try:
        payload = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise ValueError("分析参数含无效值（例如 NaN），请修正") from exc
    if len(payload) > 2 * 1024 * 1024:
        raise ValueError("标注内容超过 2 MiB；请拆分实验")
    return payload


class AnalysisBridge(QtCore.QObject):
    """一个任务一个进程；取消/超时杀死进程，关闭最多等待 1.2 秒。"""
    succeeded = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    busy_changed = QtCore.Signal(bool)
    max_output_bytes = 16 * 1024 * 1024

    def __init__(self, project: Path, python_executable: Path, parent=None):
        super().__init__(parent)
        self.project = Path(project)
        self.python_executable = Path(python_executable)
        self.process = None
        self._stdout = bytearray()
        self._stderr = bytearray()
        self._payload = b""
        self._abort_reason = None
        self.closing = False
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.cancel("分析超时，进程已停止；请检查数据量或参数"))

    @property
    def is_running(self):
        return self.process is not None

    def configure(self, project, python_executable):
        if self.is_running:
            raise ValueError("请等待或取消当前任务，再修改分析环境")
        self.project = Path(project).expanduser().resolve()
        self.python_executable = Path(python_executable).expanduser().absolute()

    def start(self, request, timeout_ms=600_000):
        if self.closing or self.is_running:
            return False
        try:
            payload = validate_payload(request)
            if not self.project.is_dir() or not (self.project / "rf_analysis").is_dir():
                raise ValueError("分析项目不存在或缺少 rf_analysis；请设置正确的 rf-analysis 项目目录")
            if not self.python_executable.is_file() or not os.access(self.python_executable, os.X_OK):
                raise ValueError("分析 Python 不可执行；请选择已有 rf-analysis 环境的 bin/python")
        except (OSError, ValueError) as exc:
            self.failed.emit(str(exc))
            return False
        self._payload, self._abort_reason = payload, None
        self._stdout.clear()
        self._stderr.clear()
        process = QtCore.QProcess(self)
        self.process = process
        process.setWorkingDirectory(str(self.project))
        env = QtCore.QProcessEnvironment.systemEnvironment()
        # 源码正本优先于 editable 安装；不复用采集 Python 的模块搜索路径。
        env.insert("PYTHONPATH", str(self.project))
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONDONTWRITEBYTECODE", "1")
        env.remove("PYTHONHOME")
        process.setProcessEnvironment(env)
        process.started.connect(self._write_request)
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._process_error)
        self.busy_changed.emit(True)
        self.timer.start(max(1, int(timeout_ms)))
        process.start(str(self.python_executable), ["-B", "-m", "rf_analysis.workbench.worker"])
        return True

    def _write_request(self):
        if self.process is not None:
            self.process.write(self._payload)
            self.process.closeWriteChannel()

    def _read_stdout(self):
        if self.process is None:
            return
        chunk = bytes(self.process.readAllStandardOutput())
        if len(self._stdout) + len(chunk) > self.max_output_bytes:
            self.cancel("分析输出超过上限；请减少预览数据或拆分实验")
            return
        self._stdout.extend(chunk)

    def _read_stderr(self):
        if self.process is not None:
            self._stderr.extend(bytes(self.process.readAllStandardError()))
            self._stderr = self._stderr[-16_384:]

    def _release(self):
        self.timer.stop()
        process, self.process = self.process, None
        if process is not None:
            process.deleteLater()
        self.busy_changed.emit(False)

    def _process_error(self, error):
        if self.process is not None and error == QtCore.QProcess.ProcessError.FailedToStart:
            message = "分析进程启动失败：" + self.process.errorString()
            self._release()
            if not self.closing:
                self.failed.emit(message)

    def _finished(self, exit_code, exit_status):
        if self.process is None:
            return
        self._read_stdout()
        self._read_stderr()
        reason = self._abort_reason
        raw = bytes(self._stdout)
        self._release()
        if self.closing:
            return
        if reason:
            self.failed.emit(reason)
            return
        try:
            response = json.loads(raw)
            if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
                raise ValueError("缺少 ok 状态")
        except (ValueError, UnicodeError) as exc:
            self.failed.emit(f"分析进程未返回有效 JSON（退出码 {exit_code}）：{exc}")
            return
        if not response["ok"]:
            error = response.get("error") or {}
            message = error.get("message", "分析失败") if isinstance(error, dict) else str(error)
            hint = error.get("hint") if isinstance(error, dict) else None
            self.failed.emit(message + (f"\n{hint}" if hint else ""))
        elif exit_code or exit_status != QtCore.QProcess.ExitStatus.NormalExit:
            self.failed.emit(f"分析进程异常退出（退出码 {exit_code}），结果未接受")
        else:
            self.succeeded.emit(response)

    def cancel(self, reason="已取消分析；采集源文件保持完整"):
        if self.process is None:
            return
        self._abort_reason = reason
        self.timer.stop()
        self.process.kill()

    def shutdown(self):
        self.closing = True
        self.timer.stop()
        process = self.process
        if process is not None:
            self.cancel("关闭分析工作台")
            stopped = process.waitForFinished(1200)
            if self.process is not None and stopped:
                self._release()
            return stopped
        return True
