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
  ps4uart.py dump        Print the full current log
  ps4uart.py list        List all saved log files
  ps4uart.py clean       Remove all log files
"""

import serial
import sys
import os
import signal
import time
import subprocess
from datetime import datetime
from pathlib import Path

PORT = os.environ.get("PS4_UART_PORT", "/dev/ttyUSB0")
BAUD = 115200
BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
PID_FILE = BASE_DIR / ".uart_pid"
LOGPATH_FILE = BASE_DIR / ".uart_logfile"
RECONNECT_DELAY = 2
MAX_RECONNECT_ATTEMPTS = 999


def get_log_path():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return LOG_DIR / f"ps4_uart_{ts}.log"


def is_running():
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True, pid
    except (OSError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        return False, None


def current_logfile():
    if LOGPATH_FILE.exists():
        p = Path(LOGPATH_FILE.read_text().strip())
        if p.exists():
            return p
    return None


def open_serial():
    """Open serial port with retries."""
    return serial.Serial(
        port=PORT,
        baudrate=BAUD,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=1,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )


def capture_loop(logfile):
    """Main capture loop with auto-reconnect."""
    running = True

    def shutdown(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    with open(logfile, "a") as log:
        ser = None
        reconnect_count = 0

        while running and reconnect_count < MAX_RECONNECT_ATTEMPTS:
            # Connect / reconnect
            if ser is None:
                try:
                    ser = open_serial()
                    ser.reset_input_buffer()
                    if reconnect_count > 0:
                        msg = f"\n# [*] Reconnected at {datetime.now().isoformat()} (attempt {reconnect_count})\n"
                        log.write(msg)
                        log.flush()
                    reconnect_count = 0
                except serial.SerialException as e:
                    # Distinguish "port missing" (retry-able) from "permission
                    # denied" / "device busy" (won't self-resolve).
                    errno = getattr(e, "errno", None)
                    if errno == 13:
                        kind = "PERMISSION DENIED — add user to the tty group "
                        kind += "(Arch: uucp, Debian/Ubuntu: dialout) and re-login, "
                        kind += "or run via `newgrp <group>`"
                    elif errno == 2:
                        kind = "device not present"
                    elif errno == 16:
                        kind = "device busy — another process holds it"
                    else:
                        kind = f"errno={errno}"
                    reconnect_count += 1
                    if reconnect_count == 1 or reconnect_count % 30 == 0:
                        log.write(
                            f"\n# [!] Cannot open {PORT} at "
                            f"{datetime.now().isoformat()}: {kind} "
                            f"(retry #{reconnect_count})\n"
                        )
                        log.flush()
                    time.sleep(RECONNECT_DELAY)
                    continue
                except Exception as e:
                    log.write(
                        f"\n# [!] Open error at {datetime.now().isoformat()}: "
                        f"{type(e).__name__}: {e}\n"
                    )
                    log.flush()
                    time.sleep(RECONNECT_DELAY)
                    continue

            # Read data
            try:
                data = ser.read(ser.in_waiting or 1)
                if data:
                    text = data.decode("utf-8", errors="replace")
                    log.write(text)
                    log.flush()
            except serial.SerialException:
                log.write(
                    f"\n# [!] Serial error at {datetime.now().isoformat()}, reconnecting...\n"
                )
                log.flush()
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                reconnect_count += 1
                time.sleep(RECONNECT_DELAY)
            except Exception as e:
                log.write(f"\n# [!] Error: {e} at {datetime.now().isoformat()}\n")
                log.flush()
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                reconnect_count += 1
                time.sleep(RECONNECT_DELAY)

        # Cleanup
        log.write(f"\n# {'=' * 56}\n")
        log.write(f"# Capture ended: {datetime.now().isoformat()}\n")
        log.flush()

        if ser:
            try:
                ser.close()
            except Exception:
                pass


def cmd_start():
    running, pid = is_running()
    if running:
        print(f"[!] Already running (PID {pid})")
        print(f"    Log: {current_logfile()}")
        print(f"    Use 'ps4uart.py live' to watch or 'ps4uart.py stop' to restart")
        return

    LOG_DIR.mkdir(exist_ok=True)
    logfile = get_log_path()

    # Write header before forking
    with open(logfile, "w") as f:
        f.write(f"# PS4 UART Log\n")
        f.write(f"# Started: {datetime.now().isoformat()}\n")
        f.write(f"# Port: {PORT} | Baud: {BAUD} 8N1\n")
        f.write(f"# Auto-reconnect: enabled\n")
        f.write(f"# {'=' * 56}\n\n")

    child_pid = os.fork()
    if child_pid > 0:
        # Parent
        PID_FILE.write_text(str(child_pid))
        LOGPATH_FILE.write_text(str(logfile))
        time.sleep(1)
        running, _ = is_running()
        if running:
            print(f"[+] UART capture started")
            print(f"    PID:  {child_pid}")
            print(f"    Port: {PORT} @ {BAUD} 8N1")
            print(f"    Log:  {logfile}")
            print(f"    Auto-reconnect: enabled")
            print()
            print(f"    ps4uart.py live    — watch output in real-time")
            print(f"    ps4uart.py stop    — stop capture")
        else:
            print(f"[!] Failed to start. Check port {PORT}")
            PID_FILE.unlink(missing_ok=True)
        return

    # Child — detach
    os.setsid()
    sys.stdin.close()
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    capture_loop(logfile)
    os._exit(0)


def cmd_stop():
    running, pid = is_running()
    if not running:
        print("[*] Not running")
        return

    os.kill(pid, signal.SIGTERM)
    # Wait for process to exit
    for _ in range(10):
        time.sleep(0.3)
        try:
            os.kill(pid, 0)
        except OSError:
            break

    PID_FILE.unlink(missing_ok=True)
    lf = current_logfile()
    if lf:
        size = lf.stat().st_size
        print(f"[+] Stopped (PID {pid})")
        print(f"    Log saved: {lf} ({size:,} bytes)")
    else:
        print(f"[+] Stopped (PID {pid})")


def cmd_live():
    lf = current_logfile()
    if not lf:
        print("[!] No active log. Run 'ps4uart.py start' first.")
        return

    running, _ = is_running()
    status = "CAPTURING" if running else "STOPPED"
    print(f"[*] Live log ({status}): {lf}")
    print(f"[*] Ctrl+C to stop watching (capture continues in background)\n")

    try:
        subprocess.run(["tail", "-n", "50", "-f", str(lf)])
    except KeyboardInterrupt:
        print(f"\n[*] Stopped watching. Capture still running.")


def cmd_status():
    running, pid = is_running()
    lf = current_logfile()

    port_exists = os.path.exists(PORT)

    if running:
        size = lf.stat().st_size if lf else 0
        lines = 0
        if lf:
            lines = int(subprocess.check_output(["wc", "-l", str(lf)]).split()[0])
        print(f"[+] Status:  RUNNING")
        print(f"    PID:     {pid}")
        print(
            f"    Port:    {PORT} @ {BAUD} {'(connected)' if port_exists else '(DISCONNECTED)'}"
        )
        print(f"    Log:     {lf}")
        print(f"    Size:    {size:,} bytes")
        print(f"    Lines:   {lines:,}")
    else:
        print(f"[-] Status:  NOT RUNNING")
        print(f"    Port:    {PORT} {'(connected)' if port_exists else '(NOT FOUND)'}")
        if lf:
            size = lf.stat().st_size
            print(f"    Last log: {lf} ({size:,} bytes)")
        print(f"\n    Run 'ps4uart.py start' to begin capture")


def cmd_dump():
    lf = current_logfile()
    if not lf:
        logs = sorted(LOG_DIR.glob("ps4_uart_*.log")) if LOG_DIR.exists() else []
        if logs:
            lf = logs[-1]
        else:
            print("[!] No log files found.")
            return
    print(lf.read_text())


def cmd_list():
    if not LOG_DIR.exists():
        print("[*] No logs directory yet.")
        return

    logs = sorted(LOG_DIR.glob("ps4_uart_*.log"))
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


def cmd_clean():
    if not LOG_DIR.exists():
        print("[*] Nothing to clean.")
        return

    running, _ = is_running()
    if running:
        print("[!] Stop capture first: ps4uart.py stop")
        return

    logs = list(LOG_DIR.glob("ps4_uart_*.log"))
    if not logs:
        print("[*] No log files to remove.")
        return

    total = sum(f.stat().st_size for f in logs)
    for f in logs:
        f.unlink()
    LOGPATH_FILE.unlink(missing_ok=True)
    print(f"[+] Removed {len(logs)} file(s), freed {total:,} bytes")


COMMANDS = {
    "start": cmd_start,
    "stop": cmd_stop,
    "live": cmd_live,
    "status": cmd_status,
    "dump": cmd_dump,
    "list": cmd_list,
    "clean": cmd_clean,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        print("Commands:")
        for name in COMMANDS:
            print(f"  ps4uart.py {name}")
        print()
        sys.exit(0)

    COMMANDS[sys.argv[1]]()
