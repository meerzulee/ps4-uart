# PS4 UART Logger

Capture and monitor UART serial output from a jailbroken PS4 using an Arduino CH340 as a USB-to-serial passthrough.

Tested on firmware **12.02**.

## Requirements

- Jailbreakable PlayStation 4 (any model)
- Arduino with CH340 USB-to-serial chip
- Soldering iron + wires
- macOS with Python 3 and `pyserial` (`pip install pyserial`)

## Setup

### 1. Solder UART on PS4

Follow the soldering guide for your PS4 model:

**[PlayStation 4 UART Pinout Guide](https://repair.wiki/w/Playstation_4_Fault_finding_with_UART#Connect_to_UART)**

### 2. Load the UART Payload

Using [Payload Guest](https://github.com/Al-Azif/ps4-payload-guest),
[PSFree Enhanced](https://github.com/ArabPixel/PSFree-Enhanced), or another
compatible payload sender, load the **permanent UART payload**.

You can get it from:
- This repo: [`ps4-permanent-uart.bin`](ps4-permanent-uart.bin)
- Or from [PSFree-Enhanced payloads](https://github.com/ArabPixel/PSFree-Enhanced/tree/main/includes/payloads)

### 3. Connect Arduino CH340

Wire the Arduino as a USB-to-serial passthrough:

| From | To |
|---|---|
| PS4 TX | Arduino **TX** (pin 1) |
| PS4 GND | Arduino **GND** |
| Arduino **RST** | Arduino **GND** |

> Grounding RST disables the ATmega chip, making the CH340 act as a direct USB-to-serial converter.

Plug the Arduino into your Mac via USB.

### 4. Run the Logger

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt

./uart start    # auto-detect one supported USB UART
./uart status   # must say READY before a hardware action
./uart live     # watch output in real-time
```

The port can be selected explicitly if more than one adapter is connected:

```bash
./uart start --port /dev/cu.wchusbserial140
```

`PS4_UART_PORT=/dev/cu.DEVICE` is also supported. Device suffixes may change
after reconnecting, so the repository does not hard-code a machine-specific
path.

## Bounded debugging sessions

Keep the raw logger running continuously, but mark only the action being
debugged:

```bash
./uart session start "EXP-20260810-001-A1 load Linux payload"

# Perform the action on the PS4.

./uart session stop
```

For a single terminal, the interactive form is even shorter:

```bash
./uart session capture "EXP-20260810-001-A1 load Linux payload"
# Perform the action, then press Enter to finish and extract it.
```

Session labels must start with `EXP-YYYYMMDD-NNN`; use `-A1`, `-A2`, and so on
only for predeclared actions within one experiment. Stopping a session creates
three files under `sessions/`:

- `.raw`: the exact UART bytes captured during the action;
- `.events.jsonl`: logger connection events during the action;
- `.md`: normalized, duplicate-collapsed, size-bounded context intended for an
  LLM or issue report.

The session is valid only if the same serial connection remains open from
start to stop. A disconnect, logger restart, stale heartbeat, replaced log, or
truncated log closes the session as `invalid`: partial evidence is preserved,
but it must not be used as a clean acceptance result.

At stop, the session tool asks the capture process to drain bytes already
waiting in the serial reader, flushes them durably, and receives an offset
fence. Bytes arriving later remain in the continuous stream and cannot leak
into the closed bounded slice. Lifecycle and session locks prevent two starts
or two finalizers from racing. The first fence offsets and artifact hashes are
stored before indexing, so an interrupted finalization resumes against the
same evidence instead of expanding the session window.

The continuous logger is not interrupted. If the context exceeds 2,000 lines,
its middle is omitted while the complete raw slice remains beside it. Change
the context budget with `session stop --max-lines 4000`.

Use `session status` to inspect an active marker or
`session abort --reason "why the operator action never began"` to close an
unused marker. Abort still preserves its bounded raw, event, and Markdown
artifacts. Every completed, invalid, or aborted marker is appended to
`sessions/index.jsonl`; inspect it with `./uart session list`.

### Commands

| Command | Description |
|---|---|
| `./uart start` | Start capturing UART in the background |
| `./uart stop` | Stop the capture |
| `./uart live` | Tail the log in real-time (Ctrl+C to stop watching) |
| `./uart status` | Check if capture is running and show stats |
| `./uart session start LABEL` | Start a bounded action marker |
| `./uart session stop` | Save the action as raw and LLM-ready context |
| `./uart session capture LABEL` | Interactively mark, wait, and extract one action |
| `./uart session status` | Show the active marker and captured size |
| `./uart session list` | List indexed completed, invalid, and aborted sessions |
| `./uart session abort --reason TEXT` | Finalize an unused marker and preserve its evidence |
| `./uart dump` | Print the full current log |
| `./uart list` | List all saved log files |
| `./uart clean` | Remove all continuous raw logs after capture stops |

Exact UART bytes are saved to timestamped `.raw` files under `logs/`. Logger
messages never enter that stream; open/lost/reconnect events go to a sibling
`.events.jsonl`. The logger auto-reconnects, but any bounded session crossing a
reconnect is invalid. By default one raw log is capped at 512 MiB and capture
stops before free space falls below 512 MiB. Override those guards with
`PS4_UART_MAX_LOG_BYTES` and `PS4_UART_MIN_FREE_BYTES` when starting capture.

## Resources

- [PS4 UART Fault Finding Guide](https://repair.wiki/w/Playstation_4_Fault_finding_with_UART#Connect_to_UART)
- [PSFree-Enhanced Payloads](https://github.com/ArabPixel/PSFree-Enhanced/tree/main/includes/payloads)
- [Video Guide](https://youtu.be/5gkQJJbQaeU)
