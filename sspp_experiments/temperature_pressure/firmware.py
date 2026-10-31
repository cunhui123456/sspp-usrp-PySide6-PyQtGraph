"""Explicit Nano application upload and protocol verification, outside experiment runs.

Construction never touches hardware. Only start() may open the selected USB port.
A flash write first requires a complete backup and the ATmega328P signature. The
bundled application HEX never extends into the bootloader. Serial verification
without upload proves the wire protocol only, not firmware identity.
"""

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import threading
import time

from .protocol import LineParser, PROTOCOLS
from .serial_io import PosixSerialReader

BOARD_LABEL = "Arduino Nano · ATmega328P · Old Bootloader（57600）"
USB_PREFIXES = ("usbserial", "usbmodem", "wchusbserial", "slab_usbtouart")
APPLICATION_BYTES = 30720
FLASH_BYTES = 32768
SIGNATURE = bytes.fromhex("1e950f")


class PreparationCancelled(RuntimeError):
    pass


def is_usb_serial_port(port):
    """Conservative name candidate, never a claim that the board is identified."""
    return (isinstance(port, str) and port.startswith("/dev/cu.")
            and "/" not in port[len("/dev/"):]
            and Path(port).name[3:].casefold().startswith(USB_PREFIXES))


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _save(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _hex_memory(path):
    """Validate Intel HEX checksums, EOF and application address boundaries."""
    memory, base, ended = {}, 0, False
    for line in Path(path).read_text(encoding="ascii").splitlines():
        if not line:
            continue
        if ended or not line.startswith(":"):
            raise ValueError("固件 HEX 格式或结束标记无效")
        raw = bytes.fromhex(line[1:])
        if len(raw) < 5 or len(raw) != raw[0] + 5 or sum(raw) & 255:
            raise ValueError("固件 HEX 长度或校验和不符")
        size, address, kind = raw[0], int.from_bytes(raw[1:3], "big"), raw[3]
        data = raw[4:-1]
        if kind == 0:
            for index, value in enumerate(data):
                location = base + address + index
                if location >= APPLICATION_BYTES or location in memory:
                    raise ValueError("固件超出 Nano 应用区或有重复地址；拒绝写入 bootloader")
                memory[location] = value
        elif kind == 1 and size == 0:
            ended = True
        elif kind == 4 and size == 2:
            base = int.from_bytes(data, "big") << 16
        elif kind == 2 and size == 2:
            base = int.from_bytes(data, "big") << 4
        else:
            raise ValueError("不支持的固件 HEX 记录")
    if not ended or not memory or 0 not in memory:
        raise ValueError("固件 HEX 缺少有效应用或结束标记")
    return memory


def firmware_bundle(root, protocol):
    if protocol not in PROTOCOLS:
        raise ValueError("请选择已支持的压力或温度实验模式")
    folder = Path(root) / "firmware" / "temperature_pressure"
    metadata = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    entry = metadata["protocols"][protocol]
    for name, hash_name in (("source_file", "source_sha256"), ("hex_file", "hex_sha256")):
        path = folder / entry[name]
        if path.parent != folder or _sha(path) != entry[hash_name]:
            raise ValueError("固件包来源校验失败，请恢复配套固件文件后重试")
    image = folder / entry["hex_file"]
    memory = _hex_memory(image)
    return {**entry, "path": str(image), "application_bytes": len(memory)}


def avrdude_candidates():
    """Inspect existing files only; installation is a separate maintenance action."""
    # Reuse Arduino's own uploader first. A native installed alternative is
    # only used when the bundled executable cannot run on this Mac.
    candidates = []
    packages = Path.home() / "Library" / "Arduino15" / "packages" / "arduino" / "tools" / "avrdude"
    for version in sorted(packages.glob("*"), reverse=True):
        candidates.append((version / "bin" / "avrdude", version / "etc" / "avrdude.conf"))
    candidates.append((Path("/opt/homebrew/bin/avrdude"), Path("/opt/homebrew/etc/avrdude.conf")))
    import sys
    candidates.append((Path(sys.prefix) / "bin" / "avrdude", Path(sys.prefix) / "etc" / "avrdude.conf"))
    path = shutil.which("avrdude")
    if path:
        executable = Path(path).resolve()
        candidates.append((executable, executable.parent.parent / "etc" / "avrdude.conf"))
    for prefix in (Path("/opt/homebrew"), Path("/usr/local")):
        candidates.append((prefix / "bin" / "avrdude", prefix / "etc" / "avrdude.conf"))
    result, seen = [], set()
    for executable, config in candidates:
        pair = (str(executable.resolve()), str(config.resolve()))
        if pair not in seen and executable.is_file() and config.is_file():
            result.append({"executable": str(executable), "config": str(config)})
            seen.add(pair)
    if result:
        return result
    raise RuntimeError("未找到可用的 avrdude 烧录工具及配置；可先选择“验证已有固件”，或安装适合此 Mac 的 AVR 工具后重试。")


def discover_avrdude():
    """First installed candidate; preparation additionally checks executability."""
    return avrdude_candidates()[0]


class PreparationSession:
    def __init__(self, root, *, transport_factory=PosixSerialReader,
                 process_factory=subprocess.Popen, toolchain=None, verify_timeout_s=10.0):
        self.root = Path(root).resolve()
        self.transport_factory = transport_factory
        self.process_factory = process_factory
        self.toolchain = toolchain
        self.verify_timeout_s = max(.1, float(verify_timeout_s))
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.status = "idle"
        self.message = "选择实验模式，再烧录对应固件或验证板上已有固件。"
        self.log = deque(maxlen=160)
        self.result = None
        self.write_active = False
        self.invalidated = False
        self.path = None
        self.key = None
        self._log_handle = None
        self._evidence = {}
        self.log_errors = []

    @property
    def is_running(self):
        return bool(self.thread and self.thread.is_alive())

    def snapshot(self):
        with self.lock:
            return {"status": self.status, "running": self.is_running,
                    "message": self.message, "log": list(self.log),
                    "result": deepcopy(self.result), "write_active": self.write_active,
                    "preparation_key": self.key}

    def matches(self, port, protocol, board="nano_old"):
        with self.lock:
            return bool(not self.is_running and self.status == "ready" and self.result
                        and not self.invalidated and self.result["port"] == port
                        and self.result["protocol"] == protocol and self.result["board"] == board)

    def invalidate(self):
        with self.lock:
            self.invalidated = True
            self.result = None
            if self.is_running:
                self.stop_event.set()
            else:
                self.status = "idle"
                self.message = "设备或实验模式已变化，请重新准备或验证。"

    def start(self, port, protocol, board="nano_old", upload=True):
        if not is_usb_serial_port(port):
            raise ValueError("请选择 Arduino USB 串口（usbserial / usbmodem / wchusbserial / SLAB_USBtoUART）；蓝牙端口不能用于此实验。")
        if protocol not in PROTOCOLS or board != "nano_old":
            raise ValueError("当前支持 Nano ATmega328P Old Bootloader 及两种已验证实验固件。")
        if not isinstance(upload, bool):
            raise ValueError("请明确选择烧录固件或只验证已有固件")
        with self.lock:
            if self.is_running:
                raise RuntimeError("正在准备 Arduino，请等待当前操作结束")
            self.stop_event.clear()
            self.invalidated = False
            self.result = None
            self.log.clear()
            self.log_errors = []
            self.status = "checking"
            self.message = "正在检查固件来源和工具；尚未写入板卡。"
            ident = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            self.key = "device_preparations/" + ident
            self.path = self.root / "Data" / self.key
            self.thread = threading.Thread(target=self._run, args=(port, protocol, board, upload),
                                           name="arduino-preparation", daemon=True)
            self.thread.start()
            return self.key

    def request_stop(self):
        self.stop_event.set()
        with self.lock:
            if self.write_active:
                self.message = "已请求取消；正在完成 Flash 写入及校验，请勿拔线。完成后不进入采集。"
            elif self.is_running:
                self.message = "正在取消准备并关闭串口…"

    def stop(self, timeout_s=2.0):
        self.request_stop()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=max(0, min(float(timeout_s), 5.0)))
        return not self.is_running

    def _check_cancelled(self):
        if self.stop_event.is_set() or self.invalidated:
            raise PreparationCancelled("已取消准备；未开始正式采集。")

    def _log(self, text):
        line = str(text).strip()[:2000]
        if not line:
            return
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        line = f"[{stamp}][Arduino 准备] {line}"
        warning = None
        with self.lock:
            self.log.append(line)
            if self._log_handle:
                try:
                    self._log_handle.write(line + "\n")
                    self._log_handle.flush()
                except Exception as exc:
                    warning = self._disable_failed_log(exc)
        try:
            print(line, flush=True)
            if warning:
                print(warning, flush=True)
        except (OSError, UnicodeError, ValueError):
            pass

    def _disable_failed_log(self, exc):
        """Logging must never interrupt an already-running flash operation."""
        message = "准备日志保存失败：" + type(exc).__name__ + ": " + str(exc)
        self.log_errors.append(message)
        self._evidence["log_errors"] = list(self.log_errors)
        handle, self._log_handle = self._log_handle, None
        if handle:
            try:
                handle.close()
            except Exception:
                pass
        warning = message + "；继续等待当前工具操作结束，不再开启新步骤。"
        self.log.append(warning)
        return warning

    def _check_log_health(self):
        if self.log_errors:
            raise RuntimeError("准备日志未完整保存；当前工具操作已经结束。请检查磁盘空间后重新准备，不能进入采集。")

    def _phase(self, status, message):
        with self.lock:
            self.status, self.message = status, message
        self._log(message)

    @staticmethod
    def _terminate(process):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, AttributeError):
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, AttributeError):
                process.kill()
            process.wait(timeout=2)

    def _run_command(self, command, *, timeout_s=30, cancellable=True):
        """Bounded streaming logs; a new process group allows reliable teardown."""
        self._evidence.setdefault("commands", []).append(list(command))
        self._log("执行 " + Path(command[0]).name + " " + " ".join(command[1:]))
        # If the sink fails before launching, no new device command is allowed.
        self._check_log_health()
        process = None
        tail, pending = deque(maxlen=30), bytearray()
        started = time.monotonic()
        exited = None
        try:
            process = self.process_factory(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, bufsize=0, start_new_session=True)
            while True:
                if cancellable:
                    self._check_cancelled()
                if time.monotonic() - started > timeout_s:
                    raise TimeoutError("烧录工具超时；若已写入，必须重新烧录并验证后才可采集")
                available, _, _ = select.select([process.stdout], [], [], .05)
                if available:
                    chunk = os.read(process.stdout.fileno(), 8192)
                    if chunk:
                        pending.extend(chunk)
                        while b"\n" in pending or len(pending) > 4096:
                            split = pending.find(b"\n")
                            if split < 0 or split > 4096:
                                split = 4096
                            line = bytes(pending[:split]).decode("utf-8", errors="replace").strip()
                            del pending[:split + (1 if pending[split:split + 1] == b"\n" else 0)]
                            if line:
                                tail.append(line)
                                self._log(line)
                    elif process.poll() is not None:
                        break
                if process.poll() is not None:
                    if exited is None:
                        exited = time.monotonic()
                    if time.monotonic() - exited > .5:
                        break
            if pending:
                line = pending.decode("utf-8", errors="replace")
                tail.append(line)
                self._log(line)
            code = process.wait(timeout=1)
            if code != 0:
                raise RuntimeError(f"烧录工具退出码 {code}：" + " | ".join(tail)[-2000:])
            return "\n".join(tail)
        except OSError as exc:
            if getattr(exc, "errno", None) == 86 or "Bad CPU" in str(exc) or "Exec format" in str(exc):
                raise RuntimeError("已安装的 AVR 烧录工具架构不适合此 Mac，无法启动。尚未烧录；请修复本机 avrdude 工具，或验证板上已有固件。") from exc
            raise
        finally:
            if process is not None:
                self._terminate(process)
                if process.stdout:
                    process.stdout.close()

    def _verify_stream(self, port, protocol):
        """Three complete advancing frames; all bytes saved, no writes to serial."""
        parser = LineParser(protocol)
        other = LineParser("temperature_v1" if protocol == "pressure_v1" else "pressure_v1")
        reader, raw_count, matched, previous = None, 0, 0, None
        other_matches = 0
        previous_line = None
        began = time.monotonic()
        frames = []
        try:
            with (self.path / "verification_serial.bin").open("wb") as raw:
                reader = self.transport_factory(port, 9600)
                while time.monotonic() - began < self.verify_timeout_s:
                    self._check_cancelled()
                    data = reader.read(timeout=.1)
                    if data is None:
                        continue
                    if not isinstance(data, bytes) or not data:
                        raise EOFError("Arduino 串口已断开")
                    raw.write(data)
                    raw_count += len(data)
                    if raw_count > 1024 * 1024:
                        raise RuntimeError("串口数据量异常，尚未确认所选 9600 波特协议")
                    stamp = {"host_time_utc": datetime.now(timezone.utc).isoformat(), "host_elapsed_s": time.monotonic() - began}
                    rows, events = parser.feed(data, **stamp)
                    alt_rows, _ = other.feed(data, **stamp)
                    other_matches += len(alt_rows)
                    for event in events:
                        if event["kind"] == "firmware_error":
                            raise RuntimeError("板上固件报告硬件错误：" + event["text"])
                    by_line = {}
                    for row in rows:
                        by_line.setdefault(row["line_number"], []).append(row)
                    for group in by_line.values():
                        expected = 2 if protocol == "temperature_v1" else 1
                        if len(group) != expected:
                            continue
                        clocks = tuple(row["mcu_time_ms"] for row in group)
                        advancing = (previous_line is None or group[0]["line_number"] == previous_line + 1) and (previous is None or all(0 < (new - old) % 2**32 < 2**31 for new, old in zip(clocks, previous)))
                        previous_line = group[0]["line_number"]
                        matched = matched + 1 if advancing else 1
                        previous = clocks
                        frames.append(group)
                        frames = frames[-3:]
                        if matched >= 3:
                            raw.flush()
                            os.fsync(raw.fileno())
                            self._log(f"收到 {matched} 个连续且 MCU 时间递增的完整数据帧，协议验证通过。")
                            return {"verified_frames": matched, "verification_bytes": raw_count,
                                    "verification_protocol": protocol, "preview": [row for frame in frames for row in frame]}
                if raw_count == 0:
                    raise RuntimeError(f"{self.verify_timeout_s:g} 秒内未收到任何串口字节。请检查 USB 端口、数据线、供电及板上程序；仅能打开端口不表示 Arduino 就绪。")
                if other_matches:
                    expected = PROTOCOLS[protocol]
                    raise RuntimeError(f"收到另一种实验协议，当前选择为“{expected}”。请切换实验模式，或烧录对应固件后重新验证。")
                raise RuntimeError("收到串口字节，但没有 3 个连续、时间递增且符合所选协议的完整数据帧。请检查固件、9600 波特率及 TCA9555 接线。")
        finally:
            if reader is not None:
                reader.close()

    def _run(self, port, protocol, board, upload):
        evidence = {"schema": "multisensor.device_preparation.v1", "started_utc": datetime.now(timezone.utc).isoformat(),
                    "port": port, "protocol": protocol, "board": board, "upload": upload,
                    "preparation_key": self.key, "status": "checking", "commands": [],
                    "firmware_identity": "protocol_only", "firmware_sha256": None,
                    "limitations": ["串口验证只证明当前输出协议及时间递增，不是温度/压力标定", "不写 EEPROM、熔丝或 bootloader"]}
        self._evidence = evidence
        final_status, result = "failed", None
        try:
            self.path.mkdir(parents=True, exist_ok=False)
            self._log_handle = (self.path / "preparation.log").open("w", encoding="utf-8")
            _save(self.path / "manifest.json", evidence)
            self._check_cancelled()
            if upload:
                bundle = firmware_bundle(self.root, protocol)
                evidence["requested_firmware"] = {key: value for key, value in bundle.items() if key != "path"}
                candidates = [self.toolchain] if self.toolchain else avrdude_candidates()
                self._phase("checking", "检查配套固件校验和及本机烧录工具。")
                failures = []
                base = None
                for tool in candidates:
                    self._check_cancelled()
                    self._check_log_health()
                    candidate = [tool["executable"], "-C", tool["config"]]
                    try:
                        self._run_command(candidate + ["-h"], timeout_s=10)
                    except PreparationCancelled:
                        raise
                    except (OSError, RuntimeError, TimeoutError) as exc:
                        failures.append(str(exc))
                        self._log("此烧录工具不可用，检查下一个已安装候选：" + str(exc))
                        continue
                    base = candidate
                    evidence["toolchain"] = dict(tool)
                    break
                self._check_log_health()
                if base is None:
                    raise RuntimeError("没有可启动的 AVR 烧录工具：" + "；".join(failures))
                self._check_cancelled()
                # avrdude enforces the selected part without -F. Independently read
                # its signature as well as all flash before allowing any write.
                device = base + ["-p", "m328p", "-c", "arduino", "-P", port, "-b", "57600", "-D", "-A"]
                backup = self.path / "flash_before.bin"
                signature = self.path / "device_signature.bin"
                self._phase("backing_up", "读取 ATmega328P 签名与全部 32768 字节 Flash 备份；尚未写入。")
                self._run_command(device + ["-U", f"signature:r:{signature}:r", "-U", f"flash:r:{backup}:r"], timeout_s=45)
                self._check_log_health()
                if signature.read_bytes() != SIGNATURE:
                    raise RuntimeError("芯片签名不是 ATmega328P（1e950f），拒绝烧录。")
                if backup.stat().st_size != FLASH_BYTES:
                    raise RuntimeError("Flash 备份不足 32768 字节，拒绝烧录。")
                evidence["backup_sha256"] = _sha(backup)
                evidence["signature"] = SIGNATURE.hex()
                for file in (backup, signature):
                    with file.open("rb") as handle:
                        os.fsync(handle.fileno())
                image = self.path / "requested_application.hex"
                shutil.copyfile(bundle["path"], image)
                if _sha(image) != bundle["hex_sha256"]:
                    raise RuntimeError("待烧录固件复制校验失败，拒绝写入。")
                _save(self.path / "manifest.json", evidence)
                self._check_cancelled()
                with self.lock:
                    self.write_active = True
                try:
                    self._phase("uploading", "正在写入所选实验应用并执行校验；请勿拔线。取消请求会等此步骤完成。")
                    evidence["write_started"] = True
                    _save(self.path / "manifest.json", evidence)
                    self._run_command(device + ["-U", f"flash:w:{image}:i", "-U", f"flash:v:{image}:i"],
                                      timeout_s=90, cancellable=False)
                    evidence["firmware_identity"] = "uploaded_and_avrdude_verified"
                    evidence["firmware_sha256"] = bundle["hex_sha256"]
                    evidence["write_verified"] = True
                finally:
                    with self.lock:
                        self.write_active = False
                self._check_log_health()
                self._check_cancelled()
            self._phase("verifying", "验证 9600 波特串口数据：等待至少 3 个完整且时间递增的数据帧。")
            self._check_log_health()
            verification = self._verify_stream(port, protocol)
            self._check_log_health()
            self._check_cancelled()
            evidence.update(verification)
            final_status = "ready"
            self._phase("ready", "实验固件与数据已准备好，可以填写样品信息并开始实时采集。" if upload else "板上现有输出协议已验证，可以开始采集；未鉴定或改写固件。")
            result = {**evidence, "status": final_status}
        except PreparationCancelled as exc:
            final_status = "cancelled"
            self._phase("cancelled", str(exc))
        except Exception as exc:
            self._phase("failed", str(exc))
            evidence["error"] = type(exc).__name__ + ": " + str(exc)
        finally:
            try:
                with self.lock:
                    if self._log_handle:
                        try:
                            self._log_handle.flush()
                            os.fsync(self._log_handle.fileno())
                            self._log_handle.close()
                            self._log_handle = None
                        except Exception as exc:
                            self._disable_failed_log(exc)
                if self.log_errors:
                    final_status, result = "failed", None
                    self._phase("failed", "准备日志未完整保存；工具操作已结束，请检查磁盘空间后重新准备。")
                    evidence["log_errors"] = list(self.log_errors)
                evidence["status"] = final_status
                evidence["ended_utc"] = datetime.now(timezone.utc).isoformat()
                evidence["message"] = self.message
                artifacts = [{"path": str(path.relative_to(self.root / "Data")), "bytes": path.stat().st_size,
                              "sha256": _sha(path)} for path in sorted(self.path.iterdir())
                             if path.is_file() and path.name != "manifest.json" and not path.name.endswith(".tmp")]
                evidence["artifacts"] = artifacts
                _save(self.path / "manifest.json", evidence)
                if result:
                    result = deepcopy(evidence)
            except Exception as exc:
                final_status, result = "failed", None
                self._phase("failed", "准备记录保存失败：" + str(exc))
            with self.lock:
                self.status = final_status
                self.result = result if not self.invalidated else None
                self.write_active = False
