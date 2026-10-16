# PS4 UART contributor instructions

## Scope

- This repository owns continuous UART logging and bounded session extraction.
  Read `README.md` before changing capture or evidence handling.
- Get it from `https://github.com/meerzulee/ps4-uart.git`. In the full workspace,
  use the revision pinned by `omarchy-ps4/manifests/development-sources.json`
  and run that repository's setup helper. Python dependencies come from
  `requirements.txt`; raw logs and runtime state are not Git dependencies.
- Source inspection, unit tests and documentation do not require console
  access. They do not authorize sending a payload or operating the console.
- Preserve exact raw bytes, separate logger events and durable offset fences.
  Never turn a reconnect, stale heartbeat or logger restart into valid
  acceptance evidence. Only `completed` continuity supports acceptance.

## macOS ARM64 and Linux x86-64

- Use native Python 3 with a repository-local virtual environment and the
  committed `requirements.txt`. Recreate `.venv` after moving computers.
- Use `.venv/bin/python -m unittest discover -s tests` for software checks;
  mock or temporary-file tests must not connect to real serial devices.
- Auto-discovery currently matches macOS `/dev/cu.*` devices. On Linux pass
  the actual `/dev/serial/by-id/...` path with `--port` or `PS4_UART_PORT`.
  Do not hard-code an adapter suffix or silently choose among multiple ports.
- Keep native Linux operation separate from container infrastructure. If
  containers or Linux VMs are needed, use only OrbStack and first verify it
  is running with Docker context `orbstack`; do not introduce another runtime.
- A passing host test is not evidence that a physical Linux adapter works.

## Evidence and hardware experiments

- Keep continuous capture running through a coherent hardware experiment.
  Confirm `./uart status` reports readiness before consequential actions.
- Use one bounded session for one objective, with a stable
  `EXP-YYYYMMDD-NNN` identifier, expected evidence, timeout and safe rollback.
  Declare the short operator sequence and stop conditions before starting.
- Keep one experiment active. Close and conclude it before a different
  diagnostic branch; do not create a session for each ordinary UI click.
- Stop on a terminal fault or safety boundary. Do not retry a failed action
  without changed evidence or a diagnostic input; three repeats without new
  evidence end the path.
- In the full workspace, use
  `../omarchy-ps4/docs/DEVELOPMENT-MODE.md` and record curated conclusions in
  `../omarchy-ps4/experiments/SESSIONS.md`. Obtain that workflow before console
  work from a standalone clone.
- Do not stop, clean, alter or move an active capture merely to tidy files.
  Raw logs and per-session evidence remain local unless deliberately reviewed
  and redacted for publication.

## Git and device moves

- Inspect both local history and live remote refs before integrating work.
  Preserve divergent commits and existing uncommitted files; do not reset,
  rebase, merge or push without task authority.
- Copy completed evidence privately when needed. Do not copy PID files,
  locks, active markers, log pointers or port state to another host. Start a
  new connection and a new bounded session there.
- Derive paths from this checkout, not an absolute Mac home directory. Keep
  full raw URLs and absolute artifact paths in operational handoffs; provide
  a macOS `open` command when useful.
