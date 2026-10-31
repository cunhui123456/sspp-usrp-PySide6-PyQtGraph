"""单后台线程拥有串口及文件；Qt 仅读取有界内存快照。"""

from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import csv
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from .protocol import CSV_FIELDS, LineParser, PROTOCOLS
from .serial_io import PosixSerialReader

SCHEMA = "sspp.temperature_pressure.v1"


def utc():
    return datetime.now(timezone.utc).isoformat()


def _save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class CaptureConfig:
    port: str
    protocol: str
    sample_name: str
    channel: str = "A2"
    baudrate: int = 9600
    notes: str = ""
    preparation: dict | None = None

    def validate(self):
        if not isinstance(self.port, str) or not self.port.startswith("/dev/cu.") or "/" in self.port[len("/dev/"):]:
            raise ValueError("请选择 Arduino /dev/cu.* 串口")
        if self.protocol not in PROTOCOLS or self.baudrate != 9600:
            raise ValueError("不支持的串口协议或波特率")
        if not isinstance(self.sample_name, str) or not self.sample_name.strip() or len(self.sample_name) > 80:
            raise ValueError("请填写样品名称（最多 80 字）")
        if not isinstance(self.channel, str) or not self.channel.strip() or len(self.channel) > 80:
            raise ValueError("请填写实际通道说明（最多 80 字）")
        if not isinstance(self.notes, str) or len(self.notes) > 1000:
            raise ValueError("实验说明最多 1000 字")
        if self.preparation is not None:
            if (not isinstance(self.preparation, dict)
                    or self.preparation.get("port") != self.port
                    or self.preparation.get("protocol") != self.protocol
                    or not self.preparation.get("preparation_key")):
                raise ValueError("固件准备记录与当前端口／实验模式不一致，请重新验证")


class SerialCaptureSession:
    """start 显式触发；默认构造和 snapshot 不打开硬件。"""

    def __init__(self, root, transport_factory=PosixSerialReader, *, first_sample_timeout_s=10.0,
                 stalled_warning_s=3.0):
        self.root = Path(root).resolve()
        self.transport_factory = transport_factory
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.run_path = None
        self.run_key = None
        self.status = "idle"
        self.preview = deque(maxlen=10000)
        self.events = deque(maxlen=30)
        self.errors = []
        self.rows = 0
        self.raw_bytes = 0
        self.event_counts = Counter()
        self.started_monotonic = None
        self.first_sample_timeout_s = max(.05, float(first_sample_timeout_s))
        self.stalled_warning_s = max(.05, float(stalled_warning_s))
        self.last_sample_monotonic = None

    @property
    def is_running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, config):
        config.validate()
        with self.lock:
            if self.is_running:
                raise RuntimeError("已有温压串口采集正在运行")
            self.stop_event.clear()
            self.preview.clear()
            self.events.clear()
            self.errors = []
            self.rows = self.raw_bytes = 0
            self.event_counts.clear()
            self.status = "initializing"
            ident = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            self.run_path = self.root / "Data" / "temperature_pressure_runs" / ident
            self.run_key = "temperature_pressure_runs/" + ident
            self.started_monotonic = time.monotonic()
            self.last_sample_monotonic = None
            self.thread = threading.Thread(target=self._run, args=(config,), name="temperature-pressure-reader", daemon=True)
            self.thread.start()
        return self.run_key

    def request_stop(self):
        self.stop_event.set()

    def stop(self, timeout_s=2.0):
        self.request_stop()
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0, min(float(timeout_s), 5.0)))
        return not self.is_running

    def snapshot(self):
        with self.lock:
            elapsed = time.monotonic() - self.started_monotonic if self.started_monotonic else 0.0
            sample_age = time.monotonic() - self.last_sample_monotonic if self.last_sample_monotonic else None
            diagnostic = ""
            if self.status == "running":
                if not self.raw_bytes:
                    diagnostic = f"等待首批数据（{elapsed:.1f} 秒）：尚未收到任何字节；请核对 USB 端口与已烧录程序。"
                elif not self.rows:
                    diagnostic = "已收到串口字节，但尚无匹配样本；请核对实验模式与板上固件，查看下方解析日志。"
                elif sample_age is not None and sample_age >= self.stalled_warning_s:
                    diagnostic = f"数据已停更 {sample_age:.1f} 秒；曲线保留最后实收值，请检查供电、连接和固件。"
            return {"status": self.status, "running": self.is_running, "run_key": self.run_key,
                    "rows": self.rows, "raw_bytes": self.raw_bytes, "points": list(self.preview),
                    "events": list(self.events), "errors": list(self.errors), "event_counts": dict(self.event_counts),
                    "elapsed_s": elapsed, "last_sample_age_s": sample_age, "diagnostic": diagnostic}

    def _event(self, event, handle):
        handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        with self.lock:
            self.events.append(event)
            self.event_counts[event["kind"]] += 1

    def _stamp(self):
        return {"host_time_utc": utc(), "host_elapsed_s": time.monotonic() - self.started_monotonic}

    def _run(self, config):
        parser = LineParser(config.protocol, config.channel)
        transport = None
        handles = []
        serial_config = asdict(config)
        preparation = serial_config.pop("preparation")
        manifest = {"acquisition_schema": SCHEMA, "schema_version": 1, "experiment": "temperature_pressure",
                    "status": "initializing", "started_utc": utc(), "label": config.sample_name,
                    "sample_name": config.sample_name, "notes": config.notes, "serial": serial_config,
                    "preparation": preparation,
                    "protocol": config.protocol, "data_file": "samples.csv", "channels": [config.channel] if config.protocol == "pressure_v1" else ["S6", "S7"],
                    "source_hashes": {path.name: _sha(path) for path in Path(__file__).parent.glob("*.py")},
                    "limitations": ["voltage_v uses firmware nominal 5 V reference; not calibrated temperature or pressure",
                                    "host timestamps describe byte receipt; MCU timestamps describe firmware measurements",
                                    "temperature S6/S7 are sequential, not simultaneous; physical channel mapping requires confirmation",
                                    "opening a Nano serial connection may reset MCU; no firmware upload or serial command is issued"],
                    "serial_closed": False, "workers_stopped": False, "data_files_closed": False,
                    "raw_complete_file_closed": False, "errors": [], "files": []}
        failures = []
        events_file = None
        try:
            self.run_path.mkdir(parents=True, exist_ok=False)
            _save(self.run_path / "manifest.json", manifest)
            raw_file = (self.run_path / "raw_serial.bin").open("wb")
            handles.append(raw_file)
            samples_file = (self.run_path / "samples.csv").open("w", newline="", encoding="utf-8")
            handles.append(samples_file)
            events_file = (self.run_path / "serial_events.jsonl").open("w", encoding="utf-8")
            handles.append(events_file)
            writer = csv.DictWriter(samples_file, fieldnames=CSV_FIELDS)
            writer.writeheader()
            transport = self.transport_factory(config.port, config.baudrate)
            with self.lock:
                self.status = "running"
            manifest["status"] = "running"
            manifest["serial_opened"] = True
            _save(self.run_path / "manifest.json", manifest)
            self._event({"kind": "capture_started", "text": "串口接收开始；未发送控制命令", **self._stamp()}, events_file)
            last_flush = time.monotonic()
            opened_monotonic = last_flush
            stop_deadline = None
            while True:
                stopping = self.stop_event.is_set()
                if stopping:
                    if not parser.buffer and not parser.discarding:
                        break
                    if stop_deadline is None:
                        stop_deadline = time.monotonic() + 0.3
                    if time.monotonic() >= stop_deadline:
                        break
                data = transport.read(timeout=0.1)
                if not stopping and not self.rows and time.monotonic() - opened_monotonic >= self.first_sample_timeout_s:
                    if data is None:
                        kind = "no_serial_data" if not self.raw_bytes else "protocol_timeout"
                        message = ("未收到任何串口字节，请核对 USB 端口、供电和已烧录程序"
                                   if not self.raw_bytes else "已收到字节但没有符合所选协议的样本，请重新烧录或验证对应程序")
                        self._event({"kind": kind, "text": message, **self._stamp()}, events_file)
                        raise TimeoutError(f"{self.first_sample_timeout_s:g} 秒内{message}")
                if data is None:
                    continue
                if not isinstance(data, bytes) or not data:
                    raise EOFError("串口断开或返回空数据")
                raw_file.write(data)
                stamp = self._stamp()
                rows, messages = parser.feed(data, **stamp)
                writer.writerows(rows)
                for message in messages:
                    self._event(message, events_file)
                with self.lock:
                    self.raw_bytes += len(data)
                    self.rows += len(rows)
                    self.preview.extend(rows)
                    if rows:
                        self.last_sample_monotonic = time.monotonic()
                if time.monotonic() - last_flush >= 0.25:
                    for handle in handles:
                        handle.flush()
                    last_flush = time.monotonic()
                if not stopping and not self.rows and time.monotonic() - opened_monotonic >= self.first_sample_timeout_s:
                    self._event({"kind": "protocol_timeout", "text": "串口持续有字节但没有符合所选协议的样本", **self._stamp()}, events_file)
                    raise TimeoutError("串口有输出但协议验证超时，请重新烧录或验证对应程序")
                if stopping and b"\n" in data:
                    # 最多等待一个未完成行；同批实收多余字节仍保留于原始文件。
                    break
            manifest["stop_reason"] = "operator_stop"
        except Exception as exc:
            failures.append(type(exc).__name__ + ": " + str(exc))
            manifest["stop_reason"] = "serial_or_storage_error"
            if events_file:
                try:
                    self._event({"kind": "capture_error", "text": failures[-1], **self._stamp()}, events_file)
                except OSError:
                    pass
        finally:
            with self.lock:
                self.status = "saving"
            if events_file:
                try:
                    for message in parser.finish(**self._stamp()):
                        self._event(message, events_file)
                    self._event({"kind": "capture_stopping", "text": "结束串口接收并封存文件", **self._stamp()}, events_file)
                except OSError as exc:
                    failures.append("尾部日志保存失败: " + str(exc))
            if transport:
                try:
                    transport.close()
                    manifest["serial_closed"] = True
                except Exception as exc:
                    failures.append("串口关闭失败: " + str(exc))
            else:
                # 未打开时不存在待关闭的串口；serial_opened 明确另行记录。
                manifest["serial_opened"] = False
                manifest["serial_closed"] = True
            for handle in handles:
                try:
                    handle.flush()
                    os.fsync(handle.fileno())
                except OSError as exc:
                    failures.append("文件刷新失败: " + str(exc))
                finally:
                    try:
                        handle.close()
                    except OSError as exc:
                        failures.append("文件关闭失败: " + str(exc))
            manifest["data_files_closed"] = all(handle.closed for handle in handles) and len(handles) == 3
            manifest["raw_complete_file_closed"] = manifest["data_files_closed"]
            manifest["workers_stopped"] = True  # 此后不再接收或写数据，UI 等线程退出后发布记录。
            bad = ("malformed_line", "invalid_encoding", "line_too_long", "firmware_error")
            if any(self.event_counts[kind] for kind in bad):
                failures.append("存在畸形/截断/固件错误行；请查看 serial_events.jsonl")
            if not self.rows:
                failures.append("未取得符合所选协议的完整数据行")
            warnings = []
            if self.event_counts["truncated_line"]:
                warnings.append("停止时的未完成尾行已从样本表排除，原始字节保留；未补造数据")
            manifest.update(ended_utc=utc(), rows=self.rows, raw_bytes=self.raw_bytes,
                            event_counts=dict(self.event_counts), time_segments=parser.segment + 1,
                            errors=failures, warnings=warnings)
            try:
                manifest["files"] = [{"path": name, "bytes": (self.run_path / name).stat().st_size,
                                      "sha256": _sha(self.run_path / name)}
                                     for name in ("raw_serial.bin", "samples.csv", "serial_events.jsonl")
                                     if (self.run_path / name).is_file()]
                manifest["status"] = "complete" if not failures and manifest["serial_closed"] and manifest["data_files_closed"] else "failed_or_incomplete"
                _save(self.run_path / "manifest.json", manifest)
            except Exception as exc:
                failures.append("封存 manifest 失败: " + str(exc))
                manifest["status"] = "failed_or_incomplete"
            with self.lock:
                self.errors = list(failures)
                self.status = manifest["status"]
