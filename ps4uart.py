#!/usr/bin/env python3
"""
PS4 UART Logger
===============
Capture and monitor UART serial output from a jailbroken PS4.
Auto-reconnects on serial errors or disconnects.

Usage:
  ps4uart.py start       Start capturing in the background
  ps4uart.py stop        Stop the capture
  ps4uart.py live        Tail the log in real-time (Ctrl+C to stop watching)
  ps4uart.py status      Check if capture is running and show stats
  ps4uart.py session start LABEL  Mark the beginning of a debugging action
  ps4uart.py session stop         Save only that action as raw + LLM context
  ps4uart.py dump        Print the full current log
  ps4uart.py list        List all saved log files
  ps4uart.py clean       Remove all log files
"""

import argparse
import errno
import fcntl
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

BAUD = 115200
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
SESSION_DIR = BASE_DIR / "sessions"
SESSION_INDEX = SESSION_DIR / "index.jsonl"
PID_FILE = BASE_DIR / ".uart_pid"
LOGPATH_FILE = BASE_DIR / ".uart_logfile"
PORT_FILE = BASE_DIR / ".uart_port"
CAPTURE_STATE_FILE = BASE_DIR / ".uart_state.json"
SESSION_FILE = BASE_DIR / ".uart_session.json"
FENCE_REQUEST_FILE = BASE_DIR / ".uart_fence_request.json"
LIFECYCLE_LOCK_FILE = BASE_DIR / ".uart_lifecycle.lock"
SESSION_LOCK_FILE = BASE_DIR / ".uart_session.lock"
RECONNECT_DELAY = 2
MAX_RECONNECT_ATTEMPTS = 999
STARTUP_TIMEOUT = 8
HEARTBEAT_MAX_AGE = 4
FENCE_TIMEOUT = 4
MAX_LOG_BYTES = int(os.environ.get("PS4_UART_MAX_LOG_BYTES", 512 * 1024 * 1024))
MIN_FREE_BYTES = int(os.environ.get("PS4_UART_MIN_FREE_BYTES", 512 * 1024 * 1024))
PORT_PATTERNS = (
    "/dev/cu.wchusbserial*",
    "/dev/cu.usbserial*",
    "/dev/cu.SLAB_USBtoUART*",
    "/dev/cu.usbmodem*",
)
ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
EXPERIMENT_LABEL = re.compile(r"^EXP-\d{8}-\d{3}(?:-A\d+)?(?:\s+.+)?$")


class CaptureGuardError(RuntimeError):
    pass


class CaptureStorageError(RuntimeError):
    pass


def timestamp_id():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def get_log_paths(generation):
    stem = f"ps4_uart_{timestamp_id()}_{generation[:8]}"
    return LOG_DIR / f"{stem}.raw", LOG_DIR / f"{stem}.events.jsonl"


def atomic_write_json(path, value):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def exclusive_write_json(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def atomic_write_bytes(path, data):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        write_all(stream, data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def atomic_write_text(path, value):
    atomic_write_bytes(path, value.encode("utf-8"))


def write_all(stream, data):
    remaining = memoryview(data)
    while remaining:
        written = stream.write(remaining)
        if written is None or written <= 0:
            raise OSError("short write while preserving UART bytes")
        remaining = remaining[written:]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def acquire_lock(path, purpose):
    stream = path.open("a+")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        stream.close()
        raise RuntimeError(f"another {purpose} operation is already running") from exc
    return stream


def read_capture_state():
    if not CAPTURE_STATE_FILE.exists():
        return None
    try:
        return json.loads(CAPTURE_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def heartbeat_age(state):
    try:
        updated = datetime.fromisoformat(state["updated_at"])
        return (datetime.now().astimezone() - updated).total_seconds()
    except (KeyError, TypeError, ValueError):
        return float("inf")


def capture_ready():
    running, pid = is_running()
    state = read_capture_state()
    if not running or not state:
        return False, "capture process or state is missing", state
    if state.get("pid") != pid:
        return False, "capture PID does not match state", state
    if state.get("status") != "open":
        return False, f"serial status is {state.get('status', 'unknown')}", state
    age = heartbeat_age(state)
    if age > HEARTBEAT_MAX_AGE:
        return False, f"capture heartbeat is stale ({age:.1f}s)", state
    source = Path(state.get("raw_log", ""))
    if not source.exists():
        return False, "raw capture file is missing", state
    logfile = current_logfile()
    if not logfile or logfile.resolve() != source.resolve():
        return False, "active-log pointer does not match capture state", state
    return True, "ready", state


def detect_port(explicit=None):
    """Resolve one UART port without silently choosing between adapters."""
    requested = explicit or os.environ.get("PS4_UART_PORT")
    if requested:
        path = Path(requested).expanduser()
        if not path.exists():
            raise RuntimeError(f"UART port does not exist: {path}")
        return str(path)

    candidates = sorted(
        {candidate for pattern in PORT_PATTERNS for candidate in glob.glob(pattern)}
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RuntimeError(
            "No supported USB UART found. Connect it or pass --port /dev/cu.DEVICE."
        )
    rendered = "\n  ".join(candidates)
    raise RuntimeError(
        f"Multiple USB UART devices found; select one with --port:\n  {rendered}"
    )


def active_port():
    if PORT_FILE.exists():
        value = PORT_FILE.read_text().strip()
        if value:
            return value
    try:
        return detect_port()
    except RuntimeError:
        return None


def serial_module():
    try:
        import serial
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "pyserial is required for capture. Run: python3 -m pip install -r requirements.txt"
        ) from exc
    return serial


def safe_label(value):
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:64] or "session"


def normalize_uart_text(raw):
    """Make UART bytes readable while preserving line content."""
    text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return ANSI_ESCAPE.sub("", text)


def compact_repeated_lines(text):
    """Collapse adjacent duplicate lines without hiding unique diagnostics."""
    lines = text.splitlines()
    if not lines:
        return []

    compacted = []
    previous = lines[0]
    repeats = 1
    for line in lines[1:]:
        if line == previous:
            repeats += 1
            continue
        compacted.append(previous)
        if repeats > 1:
            compacted.append(f"[previous line repeated {repeats - 1} more times]")
        previous = line
        repeats = 1
    compacted.append(previous)
    if repeats > 1:
        compacted.append(f"[previous line repeated {repeats - 1} more times]")
    return compacted


def fit_context(lines, max_lines):
    """Bound LLM context while retaining both trigger and final outcome."""
    if max_lines <= 0 or len(lines) <= max_lines:
        return lines, 0
    head_count = max(1, max_lines // 4)
    tail_count = max_lines - head_count - 1
    omitted = len(lines) - head_count - tail_count
    marker = f"[... {omitted} lines omitted; see the sibling raw .raw file ...]"
    return lines[:head_count] + [marker] + lines[-tail_count:], omitted


def is_running():
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        command = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="], text=True
        ).strip()
        if Path(__file__).name not in command:
            PID_FILE.unlink(missing_ok=True)
            return False, None
        return True, pid
    except (OSError, ValueError, subprocess.SubprocessError):
        PID_FILE.unlink(missing_ok=True)
        return False, None


def current_logfile():
    if LOGPATH_FILE.exists():
        p = Path(LOGPATH_FILE.read_text().strip())
        if p.exists():
            return p
    return None


def open_serial(port):
    """Open serial port with retries."""
    serial = serial_module()
    return serial.Serial(
        port=port,
        baudrate=BAUD,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=1,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )


def write_capture_state(state, **updates):
    state.update(updates)
    state["updated_at"] = datetime.now().astimezone().isoformat()
    atomic_write_json(CAPTURE_STATE_FILE, state)


def write_capture_event(stream, state, event, detail=None):
    state["event_seq"] = int(state.get("event_seq", 0)) + 1
    record = {
        "seq": state["event_seq"],
        "at": datetime.now().astimezone().isoformat(),
        "event": event,
        "generation": state["generation"],
        "connection_epoch": state["connection_epoch"],
    }
    if detail:
        record["detail"] = detail
    stream.write(json.dumps(record, sort_keys=True) + "\n")
    stream.flush()
    write_capture_state(state)


def preserve_uart_bytes(raw, state, data):
    if not data:
        return
    projected = int(state["bytes_written"]) + len(data)
    if projected > MAX_LOG_BYTES:
        raise CaptureGuardError(f"raw log limit reached ({MAX_LOG_BYTES} bytes)")
    free = shutil.disk_usage(LOG_DIR).free
    if free < MIN_FREE_BYTES:
        raise CaptureGuardError(
            f"free-space floor reached ({free} < {MIN_FREE_BYTES})"
        )
    try:
        write_all(raw, data)
    except OSError as exc:
        try:
            state["bytes_written"] = raw.tell()
        except (OSError, AttributeError):
            pass
        raise CaptureStorageError(f"raw UART storage write failed: {exc}") from exc
    state["bytes_written"] = projected


def read_fence_request():
    if not FENCE_REQUEST_FILE.exists():
        return None
    try:
        return json.loads(FENCE_REQUEST_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def acknowledge_fence(raw, events, ser, state):
    request = read_fence_request()
    if not request:
        return
    if request.get("generation") != state["generation"]:
        FENCE_REQUEST_FILE.unlink(missing_ok=True)
        return

    while running_bytes := int(ser.in_waiting or 0):
        preserve_uart_bytes(raw, state, ser.read(running_bytes))

    try:
        raw.flush()
        os.fsync(raw.fileno())
        events.flush()
        os.fsync(events.fileno())
    except OSError as exc:
        try:
            state["bytes_written"] = raw.tell()
        except (OSError, AttributeError):
            pass
        raise CaptureStorageError(f"capture fence flush failed: {exc}") from exc
    write_capture_state(
        state,
        fence_ack={
            "id": request["id"],
            "at": datetime.now().astimezone().isoformat(),
            "generation": state["generation"],
            "connection_epoch": state["connection_epoch"],
            "event_seq": state["event_seq"],
            "raw_offset": int(state["bytes_written"]),
            "events_offset": events.tell(),
        },
    )
    FENCE_REQUEST_FILE.unlink(missing_ok=True)


def request_capture_fence(capture, request_id=None):
    request = {
        "version": 1,
        "id": request_id or uuid.uuid4().hex,
        "generation": capture["generation"],
        "requested_at": datetime.now().astimezone().isoformat(),
    }
    current_state = read_capture_state() or {}
    existing_ack = current_state.get("fence_ack") or {}
    if (
        existing_ack.get("id") == request["id"]
        and existing_ack.get("generation") == request["generation"]
    ):
        return existing_ack, None
    atomic_write_json(FENCE_REQUEST_FILE, request)

    def clear_request():
        current = read_fence_request()
        if current and current.get("id") == request["id"]:
            FENCE_REQUEST_FILE.unlink(missing_ok=True)

    deadline = time.monotonic() + FENCE_TIMEOUT
    while time.monotonic() < deadline:
        ready, reason, state = capture_ready()
        if not ready:
            clear_request()
            return None, f"capture lost readiness before fence: {reason}"
        if state.get("generation") != request["generation"]:
            clear_request()
            return None, "capture generation changed before fence"
        ack = state.get("fence_ack") or {}
        if ack.get("id") == request["id"]:
            return ack, None
        time.sleep(0.05)

    clear_request()
    return None, f"capture fence timed out after {FENCE_TIMEOUT} seconds"


def serial_failure_detail(exc, port):
    hints = {
        errno.EACCES: (
            "permission denied; add your user to the serial device's group "
            "(Arch: uucp, Debian/Ubuntu: dialout) and log in again"
        ),
        errno.ENOENT: "device not present",
        errno.EBUSY: "device busy; another process may hold it",
    }
    code = getattr(exc, "errno", None)
    hint = hints.get(code, f"errno={code}" if code is not None else "serial error")
    return f"{port}: {hint}; {type(exc).__name__}: {exc}"


def capture_loop(raw_path, events_path, port, generation):
    """Capture exact UART bytes and keep logger events in a sidecar."""
    running = True

    def shutdown(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    state = {
        "version": 2,
        "pid": os.getpid(),
        "generation": generation,
        "port": port,
        "baud": BAUD,
        "status": "opening",
        "started_at": datetime.now().astimezone().isoformat(),
        "updated_at": datetime.now().astimezone().isoformat(),
        "raw_log": str(raw_path.resolve()),
        "events_log": str(events_path.resolve()),
        "connection_epoch": 0,
        "event_seq": 0,
        "bytes_written": raw_path.stat().st_size if raw_path.exists() else 0,
        "failure_reason": None,
    }

    with raw_path.open("ab", buffering=0) as raw, events_path.open("a") as events:
        ser = None
        reconnect_count = 0
        ever_opened = False
        write_capture_event(events, state, "capture_started")

        while running and reconnect_count < MAX_RECONNECT_ATTEMPTS:
            if ser is None:
                write_capture_state(
                    state,
                    status="reconnecting" if ever_opened else "opening",
                )
                try:
                    ser = open_serial(port)
                    ever_opened = True
                    state["connection_epoch"] += 1
                    reconnect_count = 0
                    write_capture_state(state, status="open", failure_reason=None)
                    write_capture_event(events, state, "serial_opened")
                except serial_module().SerialException as exc:
                    reconnect_count += 1
                    detail = serial_failure_detail(exc, port)
                    write_capture_state(state, failure_reason=detail)
                    write_capture_event(events, state, "serial_open_failed", detail)
                    time.sleep(RECONNECT_DELAY)
                    continue
                except Exception as exc:
                    reconnect_count += 1
                    write_capture_state(state, failure_reason=f"{type(exc).__name__}: {exc}")
                    write_capture_event(
                        events,
                        state,
                        "serial_open_failed",
                        f"{type(exc).__name__}: {exc}",
                    )
                    time.sleep(RECONNECT_DELAY)
                    continue

            try:
                free = shutil.disk_usage(LOG_DIR).free
                if free < MIN_FREE_BYTES:
                    reason = f"free-space floor reached ({free} < {MIN_FREE_BYTES})"
                    write_capture_state(state, status="failed", failure_reason=reason)
                    write_capture_event(events, state, "capture_failed", reason)
                    running = False
                    continue
                data = ser.read(ser.in_waiting or 1)
                if data:
                    preserve_uart_bytes(raw, state, data)
                write_capture_state(state, status="open")
                acknowledge_fence(raw, events, ser, state)
            except CaptureGuardError as exc:
                reason = str(exc)
                write_capture_state(state, status="failed", failure_reason=reason)
                write_capture_event(events, state, "capture_failed", reason)
                running = False
            except CaptureStorageError as exc:
                reason = str(exc)
                try:
                    write_capture_state(
                        state, status="failed", failure_reason=reason
                    )
                    write_capture_event(
                        events, state, "capture_storage_failed", reason
                    )
                except OSError:
                    pass
                running = False
            except serial_module().SerialException as exc:
                detail = serial_failure_detail(exc, port)
                write_capture_state(
                    state,
                    status="reconnecting",
                    failure_reason=detail,
                )
                write_capture_event(events, state, "serial_lost", detail)
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                reconnect_count += 1
                time.sleep(RECONNECT_DELAY)
            except Exception as e:
                detail = f"{type(e).__name__}: {e}"
                write_capture_state(
                    state,
                    status="reconnecting",
                    failure_reason=detail,
                )
                write_capture_event(events, state, "serial_error", detail)
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                reconnect_count += 1
                time.sleep(RECONNECT_DELAY)

        if ser:
            try:
                ser.close()
            except Exception:
                pass
        final_status = "failed" if state.get("status") == "failed" else "stopped"
        write_capture_state(state, status=final_status)
        write_capture_event(events, state, "capture_stopped")


def _cmd_start_unlocked(args, lifecycle_lock):
    running, pid = is_running()
    if running:
        ready, reason, _ = capture_ready()
        marker = "+" if ready else "!"
        print(f"[{marker}] Already running (PID {pid}; {'READY' if ready else 'NOT READY'})")
        print(f"    Log: {current_logfile()}")
        if not ready:
            print(f"    Reason: {reason}", file=sys.stderr)
        print("    Use './uart live' to watch or './uart stop' to restart")
        return 0 if ready else 1

    try:
        port = detect_port(args.port)
        serial_module()
    except RuntimeError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1

    LOG_DIR.mkdir(exist_ok=True)
    generation = uuid.uuid4().hex
    raw_path, events_path = get_log_paths(generation)
    raw_path.touch(exist_ok=False)
    events_path.touch(exist_ok=False)
    CAPTURE_STATE_FILE.unlink(missing_ok=True)
    FENCE_REQUEST_FILE.unlink(missing_ok=True)

    child_pid = os.fork()
    if child_pid > 0:
        PID_FILE.write_text(str(child_pid))
        LOGPATH_FILE.write_text(str(raw_path))
        PORT_FILE.write_text(port)
        deadline = time.monotonic() + STARTUP_TIMEOUT
        state = None
        while time.monotonic() < deadline:
            running, _ = is_running()
            state = read_capture_state()
            if not running:
                break
            if state and state.get("generation") == generation:
                if state.get("status") == "open":
                    break
                if state.get("status") == "failed":
                    break
            time.sleep(0.1)

        ready, reason, state = capture_ready()
        if ready and state.get("generation") == generation:
            print("[+] UART capture started and serial port is open")
            print(f"    PID:  {child_pid}")
            print(f"    Port: {port} @ {BAUD} 8N1")
            print(f"    Raw:  {raw_path}")
            print(f"    Events: {events_path}")
            print(f"    Generation: {generation}")
            print(f"    Auto-reconnect: enabled")
            print(f"    Raw-log limit: {MAX_LOG_BYTES:,} bytes")
            print(f"    Free-space floor: {MIN_FREE_BYTES:,} bytes")
            print()
            print("    ./uart live    — watch output in real-time")
            print("    ./uart stop    — stop capture")
            return 0

        print(f"[!] UART capture did not become ready: {reason}", file=sys.stderr)
        if state and state.get("failure_reason"):
            print(f"    {state['failure_reason']}", file=sys.stderr)
        try:
            os.kill(child_pid, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):
            child_running, _ = is_running()
            if not child_running:
                break
            time.sleep(0.1)
        child_running, _ = is_running()
        if child_running:
            print(
                f"[!] Capture PID {child_pid} survived startup cleanup; "
                "PID state was retained.",
                file=sys.stderr,
            )
        else:
            PID_FILE.unlink(missing_ok=True)
        return 1

    lifecycle_lock.close()
    os.setsid()
    sys.stdin.close()
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    capture_loop(raw_path, events_path, port, generation)
    os._exit(0)


def cmd_start(args):
    lifecycle_lock = acquire_lock(LIFECYCLE_LOCK_FILE, "UART lifecycle")
    try:
        return _cmd_start_unlocked(args, lifecycle_lock)
    finally:
        if not lifecycle_lock.closed:
            lifecycle_lock.close()


def _cmd_stop_unlocked(args):
    if SESSION_FILE.exists():
        print(
            "[!] A bounded session is active. Stop or abort that session before "
            "stopping continuous capture.",
            file=sys.stderr,
        )
        return 1
    running, pid = is_running()
    if not running:
        print("[*] Not running")
        return

    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.1)
        still_running, _ = is_running()
        if not still_running:
            break

    still_running, _ = is_running()
    if still_running:
        print(
            f"[!] Capture PID {pid} did not stop; state and PID file were retained.",
            file=sys.stderr,
        )
        return 1

    PID_FILE.unlink(missing_ok=True)
    lf = current_logfile()
    if lf:
        size = lf.stat().st_size
        print(f"[+] Stopped (PID {pid})")
        print(f"    Log saved: {lf} ({size:,} bytes)")
    else:
        print(f"[+] Stopped (PID {pid})")


def cmd_stop(args):
    lifecycle_lock = acquire_lock(LIFECYCLE_LOCK_FILE, "UART lifecycle")
    try:
        return _cmd_stop_unlocked(args)
    finally:
        lifecycle_lock.close()


def cmd_live(args):
    lf = current_logfile()
    if not lf:
        print("[!] No active log. Run './uart start' first.")
        return

    running, _ = is_running()
    status = "CAPTURING" if running else "STOPPED"
    print(f"[*] Live log ({status}): {lf}")
    print(f"[*] Ctrl+C to stop watching (capture continues in background)\n")

    try:
        subprocess.run(["tail", "-n", "50", "-f", str(lf)])
    except KeyboardInterrupt:
        print(f"\n[*] Stopped watching. Capture still running.")


def cmd_status(args):
    running, pid = is_running()
    lf = current_logfile()
    port = active_port()
    ready, reason, state = capture_ready()

    if running:
        size = lf.stat().st_size if lf else 0
        lines = 0
        if lf:
            lines = int(subprocess.check_output(["wc", "-l", str(lf)]).split()[0])
        print(f"[{'+' if ready else '!'}] Status:  {'READY' if ready else 'NOT READY'}")
        print(f"    PID:     {pid}")
        print(f"    Serial:  {state.get('status', 'unknown') if state else 'unknown'} ({reason})")
        print(f"    Port:    {port or 'unknown'} @ {BAUD}")
        print(f"    Generation: {state.get('generation', 'unknown') if state else 'unknown'}")
        print(f"    Raw:     {lf}")
        if state:
            print(f"    Events:  {state.get('events_log', 'unknown')}")
        print(f"    Size:    {size:,} bytes")
        print(f"    Lines:   {lines:,}")
        if state and state.get("failure_reason"):
            print(f"    Last error: {state['failure_reason']}")
        return 0 if ready else 1
    else:
        print(f"[-] Status:  NOT RUNNING")
        print(f"    Port:    {port or 'not detected'}")
        if lf:
            size = lf.stat().st_size
            print(f"    Last log: {lf} ({size:,} bytes)")
        else:
            print("    Last log: none")
        print("\n    Run './uart start' to begin capture")
        return 1


def cmd_dump(args):
    lf = current_logfile()
    if not lf:
        logs = []
        if LOG_DIR.exists():
            logs = sorted(
                [*LOG_DIR.glob("ps4_uart_*.raw"), *LOG_DIR.glob("ps4_uart_*.log")]
            )
        if logs:
            lf = logs[-1]
        else:
            print("[!] No log files found.")
            return
    sys.stdout.write(normalize_uart_text(lf.read_bytes()))


def cmd_list(args):
    if not LOG_DIR.exists():
        print("[*] No logs directory yet.")
        return

    logs = sorted(
        [*LOG_DIR.glob("ps4_uart_*.raw"), *LOG_DIR.glob("ps4_uart_*.log")]
    )
    if not logs:
        print("[*] No log files.")
        return

    lf_active = (
        Path(LOGPATH_FILE.read_text().strip()) if LOGPATH_FILE.exists() else None
    )

    print(f"[*] {len(logs)} log file(s) in {LOG_DIR}/\n")
    total_size = 0
    for lf in logs:
        size = lf.stat().st_size
        total_size += size
        mtime = datetime.fromtimestamp(lf.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        lines = int(subprocess.check_output(["wc", "-l", str(lf)]).split()[0])
        active = " <-- active" if lf_active and lf == lf_active else ""
        print(f"  {lf.name}  {size:>10,} B  {lines:>6,} lines  {mtime}{active}")
    print(f"\n  Total: {total_size:,} bytes")


def _cmd_clean_unlocked(args):
    if not LOG_DIR.exists():
        print("[*] Nothing to clean.")
        return

    running, _ = is_running()
    if running:
        print("[!] Stop capture first: ./uart stop")
        return 1
    if SESSION_FILE.exists():
        print(
            "[!] A bounded session is active. Stop it to preserve partial "
            "evidence before cleaning.",
            file=sys.stderr,
        )
        return 1

    logs = [
        *LOG_DIR.glob("ps4_uart_*.raw"),
        *LOG_DIR.glob("ps4_uart_*.events.jsonl"),
        *LOG_DIR.glob("ps4_uart_*.log"),
    ]
    if not logs:
        print("[*] No log files to remove.")
        return

    total = sum(f.stat().st_size for f in logs)
    for f in logs:
        f.unlink()
    LOGPATH_FILE.unlink(missing_ok=True)
    CAPTURE_STATE_FILE.unlink(missing_ok=True)
    FENCE_REQUEST_FILE.unlink(missing_ok=True)
    print(f"[+] Removed {len(logs)} file(s), freed {total:,} bytes")


def cmd_clean(args):
    lifecycle_lock = acquire_lock(LIFECYCLE_LOCK_FILE, "UART lifecycle")
    try:
        return _cmd_clean_unlocked(args)
    finally:
        lifecycle_lock.close()


def load_session():
    if not SESSION_FILE.exists():
        return None
    try:
        return json.loads(SESSION_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Invalid session state in {SESSION_FILE}: {exc}") from exc


def append_session_index(record):
    SESSION_DIR.mkdir(exist_ok=True)
    with SESSION_INDEX.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.seek(0)
        for line in stream:
            try:
                existing = json.loads(line)
            except json.JSONDecodeError:
                continue
            if existing.get("session_id") == record.get("session_id"):
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                return False
        stream.seek(0, os.SEEK_END)
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return True


def source_window(path, expected_inode, start_offset, end_offset=None):
    """Return a stable byte window or a reason that it cannot be trusted."""
    if not path.exists():
        return None, 0, f"source disappeared: {path}"
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        if stat.st_ino != int(expected_inode):
            return None, stat.st_size, "source file was replaced"
        requested_end = stat.st_size if end_offset is None else int(end_offset)
        if stat.st_size < int(start_offset):
            return None, stat.st_size, "source file was truncated"
        if stat.st_size < requested_end:
            return None, stat.st_size, "source is shorter than capture fence"
        if requested_end < int(start_offset):
            return None, stat.st_size, "capture fence moved before session start"
        stream.seek(int(start_offset))
        return stream.read(requested_end - int(start_offset)), requested_end, None


def _cmd_session_start_unlocked(args):
    if SESSION_FILE.exists():
        state = load_session()
        print(
            f"[!] Session already active: {state.get('label', 'unknown')}\n"
            "    Stop or abort it before starting another.",
            file=sys.stderr,
        )
        return 1

    if not EXPERIMENT_LABEL.fullmatch(args.label.strip()):
        print(
            "[!] Session label must begin with EXP-YYYYMMDD-NNN "
            "(optional -A1 action suffix and description).",
            file=sys.stderr,
        )
        return 1

    ready, reason, capture = capture_ready()
    if not ready:
        print(f"[!] UART capture is not READY: {reason}", file=sys.stderr)
        return 1

    logfile = Path(capture["raw_log"])
    events = Path(capture["events_log"])
    if not events.exists():
        print("[!] Capture event sidecar is missing.", file=sys.stderr)
        return 1

    stat = logfile.stat()
    events_stat = events.stat()
    now = datetime.now().astimezone()
    state = {
        "version": 2,
        "session_id": uuid.uuid4().hex,
        "label": args.label,
        "slug": safe_label(args.label),
        "started_at": now.isoformat(),
        "stamp": now.strftime("%Y%m%d_%H%M%S_%f"),
        "capture_generation": capture["generation"],
        "connection_epoch": int(capture["connection_epoch"]),
        "start_event_seq": int(capture["event_seq"]),
        "source": str(logfile.resolve()),
        "source_inode": stat.st_ino,
        "start_offset": stat.st_size,
        "events_source": str(events.resolve()),
        "events_inode": events_stat.st_ino,
        "events_start_offset": events_stat.st_size,
    }
    try:
        exclusive_write_json(SESSION_FILE, state)
    except FileExistsError:
        print("[!] Another process started a session first.", file=sys.stderr)
        return 1
    print(f"[+] Session started: {args.label}")
    print(f"    Source: {logfile}")
    print(f"    Capture generation: {capture['generation']}")
    print(f"    Connection epoch: {capture['connection_epoch']}")
    print(f"    Offset: {stat.st_size:,} bytes")
    print("    Perform the action, then run: ./uart session stop")
    return 0


def cmd_session_start(args):
    lifecycle_lock = acquire_lock(LIFECYCLE_LOCK_FILE, "UART lifecycle")
    try:
        session_lock = acquire_lock(SESSION_LOCK_FILE, "bounded-session")
        try:
            return _cmd_session_start_unlocked(args)
        finally:
            session_lock.close()
    finally:
        lifecycle_lock.close()


def _finalize_session_unlocked(args, requested_state=None, reason=None):
    state = load_session()
    if not state:
        print("[!] No active debugging session.", file=sys.stderr)
        return 1
    if 0 < args.max_lines < 3:
        print("[!] --max-lines must be 0 (unlimited) or at least 3.", file=sys.stderr)
        return 1

    finalization = state.get("finalization")
    if finalization:
        saved_state = finalization.get("requested_state")
        requested_state = None if saved_state == "completed" else saved_state
        reason = finalization.get("reason")
        args.max_lines = int(finalization["max_lines"])
        stopped_at = finalization["stopped_at"]
    else:
        stopped_at = datetime.now().astimezone().isoformat()
        state["finalization"] = {
            "requested_state": requested_state or "completed",
            "reason": reason,
            "max_lines": args.max_lines,
            "stopped_at": stopped_at,
        }
        finalization = state["finalization"]
        atomic_write_json(SESSION_FILE, state)

    source = Path(state["source"])
    start_offset = int(state["start_offset"])
    events_source = Path(state["events_source"])
    SESSION_DIR.mkdir(exist_ok=True)
    stem = f"{state['stamp']}-{state['slug']}-{state['session_id'][:8]}"
    raw_path = SESSION_DIR / f"{stem}.raw"
    events_path = SESSION_DIR / f"{stem}.events.jsonl"
    context_path = SESSION_DIR / f"{stem}.md"

    manifest = finalization.get("artifact_manifest")
    indexed_record = finalization.get("index_record")
    if manifest and indexed_record:
        artifacts = {"raw": raw_path, "events": events_path, "context": context_path}
        valid = all(
            path.exists() and sha256_file(path) == manifest[name]["sha256"]
            for name, path in artifacts.items()
        )
        if not valid:
            raise RuntimeError(
                "finalized session artifacts failed hash verification; marker retained"
            )
        append_session_index(indexed_record)
        SESSION_FILE.unlink()
        evidence_state = indexed_record["state"]
        marker = "+" if evidence_state == "completed" else "!"
        print(f"[{marker}] Session recovered: {state['label']} ({evidence_state})")
        print(f"    Raw slice:   {raw_path} ({indexed_record['bytes']:,} bytes)")
        print(
            f"    Events:      {events_path} "
            f"({manifest['events']['bytes']:,} bytes)"
        )
        print(
            f"    LLM context: {context_path} "
            f"({indexed_record['context_lines']:,} lines)"
        )
        return 0 if evidence_state in ("completed", "aborted") else 2

    snapshot = finalization.get("snapshot")
    snapshot_created = False
    raw_seed = None
    events_seed = None
    if snapshot:
        continuity_errors = list(snapshot["continuity_errors"])
        raw_end_fence = int(snapshot["raw_end_offset"])
        events_end_fence = int(snapshot["events_end_offset"])
        evidence_state = snapshot["evidence_state"]
    else:
        persisted_fence = finalization.get("fence")
        if persisted_fence:
            continuity_errors = list(persisted_fence["continuity_errors"])
            raw_end_fence = int(persisted_fence["raw_end_offset"])
            events_end_fence = int(persisted_fence["events_end_offset"])
        else:
            continuity_errors = []
            raw_end_fence = None
            events_end_fence = None
            ready, ready_reason, capture = capture_ready()
            if not ready:
                continuity_errors.append(f"capture not READY at stop: {ready_reason}")
            if not capture:
                continuity_errors.append("capture state missing at stop")
            else:
                if capture.get("generation") != state.get("capture_generation"):
                    continuity_errors.append("capture generation changed")
                if int(capture.get("connection_epoch", -1)) != int(
                    state["connection_epoch"]
                ):
                    continuity_errors.append("serial connection epoch changed")
                if Path(capture.get("raw_log", "")).resolve() != source.resolve():
                    continuity_errors.append("active raw log changed")
                if int(capture.get("event_seq", -1)) < int(
                    state["start_event_seq"]
                ):
                    continuity_errors.append("capture event sequence moved backwards")

            fence_id = finalization.get("fence_request_id")
            if not continuity_errors:
                if not fence_id:
                    fence_id = uuid.uuid4().hex
                    finalization["fence_request_id"] = fence_id
                    atomic_write_json(SESSION_FILE, state)
                fence, fence_error = request_capture_fence(capture, fence_id)
                if fence_error:
                    continuity_errors.append(fence_error)
                else:
                    raw_end_fence = int(fence["raw_offset"])
                    events_end_fence = int(fence["events_offset"])
                    if int(fence["connection_epoch"]) != int(
                        state["connection_epoch"]
                    ):
                        continuity_errors.append(
                            "serial connection changed at capture fence"
                        )

            if raw_end_fence is None:
                raw_end_fence = (
                    source.stat().st_size if source.exists() else start_offset
                )
            if events_end_fence is None:
                events_end_fence = (
                    events_source.stat().st_size
                    if events_source.exists()
                    else int(state["events_start_offset"])
                )
            finalization["fence"] = {
                "id": fence_id,
                "raw_end_offset": raw_end_fence,
                "events_end_offset": events_end_fence,
                "continuity_errors": continuity_errors,
            }
            atomic_write_json(SESSION_FILE, state)

        raw_seed, _, source_error = source_window(
            source, state["source_inode"], start_offset, raw_end_fence
        )
        events_seed, _, events_error = source_window(
            events_source,
            state["events_inode"],
            state["events_start_offset"],
            events_end_fence,
        )
        if source_error:
            continuity_errors.append(source_error)
        if events_error:
            continuity_errors.append(f"event sidecar {events_error}")
        evidence_state = requested_state or (
            "invalid" if continuity_errors else "completed"
        )
        finalization["snapshot"] = {
            "raw_end_offset": raw_end_fence,
            "events_end_offset": events_end_fence,
            "continuity_errors": continuity_errors,
            "evidence_state": evidence_state,
        }
        atomic_write_json(SESSION_FILE, state)
        snapshot_created = True

    if raw_path.exists():
        raw = raw_path.read_bytes()
    elif snapshot_created:
        raw = raw_seed or b""
        atomic_write_bytes(raw_path, raw)
    else:
        raw, _, source_error = source_window(
            source, state["source_inode"], start_offset, raw_end_fence
        )
        if source_error:
            raise RuntimeError(
                f"cannot recover snapshotted raw evidence: {source_error}"
            )
        raw = raw or b""
        atomic_write_bytes(raw_path, raw)

    if events_path.exists():
        events_raw = events_path.read_bytes()
    elif snapshot_created:
        events_raw = events_seed or b""
        atomic_write_bytes(events_path, events_raw)
    else:
        events_raw, _, events_error = source_window(
            events_source,
            state["events_inode"],
            state["events_start_offset"],
            events_end_fence,
        )
        if events_error:
            raise RuntimeError(
                f"cannot recover snapshotted logger events: {events_error}"
            )
        events_raw = events_raw or b""
        atomic_write_bytes(events_path, events_raw)

    end_offset = raw_end_fence
    events_end_offset = events_end_fence

    normalized = normalize_uart_text(raw)
    compacted = compact_repeated_lines(normalized)
    context_lines, omitted = fit_context(compacted, args.max_lines)
    context = [
        f"# PS4 UART session: {state['label']}",
        "",
        f"- started: `{state['started_at']}`",
        f"- stopped: `{stopped_at}`",
        f"- evidence state: `{evidence_state}`",
        f"- capture generation: `{state['capture_generation']}`",
        f"- connection epoch: `{state['connection_epoch']}`",
        f"- source: `{source.name}`",
        f"- byte range: `{start_offset}..{end_offset}` ({len(raw):,} bytes)",
        f"- capture events: `{events_path.name}` "
        f"({state['events_start_offset']}..{events_end_offset}; "
        f"{len(events_raw):,} bytes)",
        f"- normalized lines: `{len(compacted):,}`",
        f"- omitted from this context: `{omitted:,}` lines",
        f"- exact raw slice: `{raw_path.name}`",
        "",
    ]
    if reason:
        context.extend([f"- finalization reason: {reason}", ""])
    if continuity_errors:
        context.extend(
            [
                "## Continuity failure",
                "",
                *[f"- {reason}" for reason in continuity_errors],
                "",
                "This partial evidence must not be treated as a valid bounded session.",
                "",
            ]
        )
    context.extend(
        [
            "## UART output",
            "",
            "````text",
            *context_lines,
            "````",
            "",
        ]
    )
    atomic_write_text(context_path, "\n".join(context))
    record = {
            "version": 2,
            "session_id": state["session_id"],
            "state": evidence_state,
            "label": state["label"],
            "slug": state["slug"],
            "started_at": state["started_at"],
            "stopped_at": stopped_at,
            "capture_generation": state["capture_generation"],
            "connection_epoch": state["connection_epoch"],
            "source": str(source),
            "start_offset": start_offset,
            "end_offset": end_offset,
            "bytes": len(raw),
            "normalized_lines": len(compacted),
            "context_lines": len(context_lines),
            "omitted_lines": omitted,
            "raw": str(raw_path.resolve()),
            "events": str(events_path.resolve()),
            "events_start_offset": int(state["events_start_offset"]),
            "events_end_offset": events_end_offset,
            "context": str(context_path.resolve()),
            "continuity_errors": continuity_errors,
            "reason": reason,
        }
    finalization["index_record"] = record
    finalization["artifact_manifest"] = {
        "raw": {"sha256": sha256_file(raw_path), "bytes": raw_path.stat().st_size},
        "events": {
            "sha256": sha256_file(events_path),
            "bytes": events_path.stat().st_size,
        },
        "context": {
            "sha256": sha256_file(context_path),
            "bytes": context_path.stat().st_size,
        },
    }
    atomic_write_json(SESSION_FILE, state)
    append_session_index(record)
    SESSION_FILE.unlink()

    marker = "+" if evidence_state == "completed" else "!"
    print(f"[{marker}] Session finalized: {state['label']} ({evidence_state})")
    print(f"    Raw slice:   {raw_path} ({len(raw):,} bytes)")
    print(f"    Events:      {events_path} ({len(events_raw):,} bytes)")
    print(f"    LLM context: {context_path} ({len(context_lines):,} lines)")
    if omitted:
        print(f"    Context omitted {omitted:,} middle lines; raw slice is complete.")
    if continuity_errors:
        for reason in continuity_errors:
            print(f"    Continuity failure: {reason}", file=sys.stderr)
        return 0 if requested_state == "aborted" else 2
    return 0


def cmd_session_stop(args):
    session_lock = acquire_lock(SESSION_LOCK_FILE, "bounded-session")
    try:
        return _finalize_session_unlocked(args)
    finally:
        session_lock.close()


def cmd_session_capture(args):
    result = cmd_session_start(args)
    if result:
        return result
    print("\n[REC] Perform the PS4 action now.")
    try:
        input("[REC] Press Enter when the action is complete: ")
    except (EOFError, KeyboardInterrupt):
        print("\n[!] Input ended; the session marker remains active.", file=sys.stderr)
        print("    Finish with: ./uart session stop", file=sys.stderr)
        return 130
    return cmd_session_stop(args)


def cmd_session_status(args):
    state = load_session()
    if not state:
        print("[-] No active debugging session.")
        return
    source = Path(state["source"])
    current_size = source.stat().st_size if source.exists() else int(state["start_offset"])
    captured = max(0, current_size - int(state["start_offset"]))
    print(f"[+] Session active: {state['label']}")
    print(f"    Started: {state['started_at']}")
    print(f"    Source:  {source}")
    print(f"    Bytes:   {captured:,}")
    ready, reason, capture = capture_ready()
    continuity = (
        ready
        and capture.get("generation") == state.get("capture_generation")
        and int(capture.get("connection_epoch", -1)) == int(state["connection_epoch"])
    )
    print(f"    Continuity: {'valid so far' if continuity else 'INVALID'} ({reason})")
    return 0 if continuity else 1


def cmd_session_list(args):
    if args.limit <= 0:
        print("[!] --limit must be greater than zero.", file=sys.stderr)
        return 1
    if not SESSION_INDEX.exists():
        print("[-] No completed or aborted sessions indexed yet.")
        return
    records = []
    for line_number, line in enumerate(SESSION_INDEX.read_text().splitlines(), 1):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"[!] Ignoring invalid index line {line_number}.", file=sys.stderr)
    records = records[-args.limit :]
    print(f"[*] Showing {len(records)} most recent session(s):")
    for record in records:
        print(
            f"    {record.get('started_at', '?')}  "
            f"{record.get('state', '?'):9}  {record.get('label', '?')}"
        )
        if record.get("context"):
            print(f"      context: {record['context']}")
        if record.get("reason"):
            print(f"      reason:  {record['reason']}")


def _cmd_session_abort_unlocked(args):
    state = load_session()
    if not state:
        print("[-] No active debugging session.")
        return
    result = _finalize_session_unlocked(
        args,
        requested_state="aborted",
        reason=args.reason,
    )
    print("    Abort preserved the bounded raw, events, and context artifacts.")
    return result


def cmd_session_abort(args):
    session_lock = acquire_lock(SESSION_LOCK_FILE, "bounded-session")
    try:
        return _cmd_session_abort_unlocked(args)
    finally:
        session_lock.close()


def build_parser():
    parser = argparse.ArgumentParser(
        description="Continuously capture PS4 UART and extract bounded debug sessions."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="start continuous background capture")
    start.add_argument(
        "--port",
        help="serial device; auto-detected when exactly one supported adapter exists",
    )
    start.set_defaults(func=cmd_start)

    for name, help_text, handler in (
        ("stop", "stop continuous capture", cmd_stop),
        ("live", "follow the continuous log", cmd_live),
        ("status", "show capture and port status", cmd_status),
        ("dump", "print the current or latest continuous log", cmd_dump),
        ("list", "list continuous raw logs", cmd_list),
        ("clean", "remove continuous raw logs after capture stops", cmd_clean),
    ):
        command = commands.add_parser(name, help=help_text)
        command.set_defaults(func=handler)

    session = commands.add_parser(
        "session", help="mark and extract one bounded debugging action"
    )
    session_commands = session.add_subparsers(dest="session_command", required=True)

    session_start = session_commands.add_parser(
        "start", help="record the current raw-log offset"
    )
    session_start.add_argument("label", help="short description of the action")
    session_start.set_defaults(func=cmd_session_start)

    session_stop = session_commands.add_parser(
        "stop", help="extract raw and LLM-ready files for the marked action"
    )
    session_stop.add_argument(
        "--max-lines",
        type=int,
        default=2000,
        help="maximum lines in Markdown context; raw slice is never truncated (default: 2000)",
    )
    session_stop.set_defaults(func=cmd_session_stop)

    session_capture = session_commands.add_parser(
        "capture", help="start, wait for Enter, then extract one action"
    )
    session_capture.add_argument("label", help="short description of the action")
    session_capture.add_argument(
        "--max-lines",
        type=int,
        default=2000,
        help="maximum lines in Markdown context; raw slice is never truncated (default: 2000)",
    )
    session_capture.set_defaults(func=cmd_session_capture)

    session_status = session_commands.add_parser(
        "status", help="show the active marker and bytes captured"
    )
    session_status.set_defaults(func=cmd_session_status)

    session_list = session_commands.add_parser(
        "list", help="list completed and aborted bounded sessions"
    )
    session_list.add_argument(
        "--limit", type=int, default=20, help="number of recent sessions to show"
    )
    session_list.set_defaults(func=cmd_session_list)

    session_abort = session_commands.add_parser(
        "abort", help="discard the marker without stopping continuous capture"
    )
    session_abort.add_argument(
        "--reason", required=True, help="why this marker cannot produce valid evidence"
    )
    session_abort.add_argument(
        "--max-lines",
        type=int,
        default=2000,
        help="maximum lines in preserved Markdown context (default: 2000)",
    )
    session_abort.set_defaults(func=cmd_session_abort)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except RuntimeError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
