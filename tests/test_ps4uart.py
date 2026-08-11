import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ps4uart


class TextCompactionTests(unittest.TestCase):
    def test_normalizes_uart_control_sequences(self):
        raw = b"first\r\n\x1b[31merror\x1b[0m\rthird\x00\n"
        self.assertEqual(ps4uart.normalize_uart_text(raw), "first\nerror\nthird\n")

    def test_collapses_only_adjacent_duplicates(self):
        lines = ps4uart.compact_repeated_lines("ready\nready\nwork\nready\n")
        self.assertEqual(
            lines,
            ["ready", "[previous line repeated 1 more times]", "work", "ready"],
        )

    def test_context_keeps_head_and_tail(self):
        fitted, omitted = ps4uart.fit_context([str(i) for i in range(20)], 8)
        self.assertEqual(fitted[:2], ["0", "1"])
        self.assertEqual(fitted[-5:], ["15", "16", "17", "18", "19"])
        self.assertEqual(omitted, 13)


class CaptureTests(unittest.TestCase):
    def test_readiness_requires_open_serial_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "capture.raw"
            raw.touch()
            state_file = root / ".uart_state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "pid": 42,
                        "status": "opening",
                        "updated_at": datetime.now().astimezone().isoformat(),
                        "raw_log": str(raw),
                    }
                )
            )
            with (
                patch.object(ps4uart, "CAPTURE_STATE_FILE", state_file),
                patch.object(ps4uart, "is_running", return_value=(True, 42)),
            ):
                ready, reason, _ = ps4uart.capture_ready()
            self.assertFalse(ready)
            self.assertIn("opening", reason)

    def test_capture_loop_preserves_exact_binary_bytes(self):
        payload = b"\x00\xffboot\r\n\x1b[31merror\x00"

        class FakeSerialException(Exception):
            pass

        class FakeSerialModule:
            SerialException = FakeSerialException

        class FakeSerial:
            in_waiting = 1

            def __init__(self):
                self.reads = 0

            def read(self, _size):
                self.reads += 1
                return payload if self.reads == 1 else b"x"

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "capture.raw"
            events = root / "capture.events.jsonl"
            raw.touch()
            events.touch()
            with (
                patch.object(ps4uart, "CAPTURE_STATE_FILE", root / ".state.json"),
                patch.object(ps4uart, "FENCE_REQUEST_FILE", root / ".fence.json"),
                patch.object(ps4uart, "LOG_DIR", root),
                patch.object(ps4uart, "MAX_LOG_BYTES", len(payload)),
                patch.object(ps4uart, "MIN_FREE_BYTES", 0),
                patch.object(ps4uart, "serial_module", return_value=FakeSerialModule),
                patch.object(ps4uart, "open_serial", return_value=FakeSerial()),
            ):
                ps4uart.capture_loop(raw, events, "/dev/fake", "generation")

            self.assertEqual(raw.read_bytes(), payload)
            state = json.loads((root / ".state.json").read_text())
            self.assertEqual(state["status"], "failed")
            self.assertIn("raw log limit reached", state["failure_reason"])

    def test_partial_storage_write_fails_capture_without_reconnect(self):
        payload = b"abcdef"

        class FakeSerialException(Exception):
            pass

        class FakeSerialModule:
            SerialException = FakeSerialException

        class FakeSerial:
            in_waiting = len(payload)

            def read(self, _size):
                return payload

            def close(self):
                pass

        def partial_then_fail(stream, data):
            stream.write(data[:2])
            raise OSError("simulated disk failure")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "capture.raw"
            events = root / "capture.events.jsonl"
            raw.touch()
            events.touch()
            state_path = root / ".state.json"
            with (
                patch.object(ps4uart, "CAPTURE_STATE_FILE", state_path),
                patch.object(ps4uart, "FENCE_REQUEST_FILE", root / ".fence.json"),
                patch.object(ps4uart, "LOG_DIR", root),
                patch.object(ps4uart, "MIN_FREE_BYTES", 0),
                patch.object(ps4uart, "serial_module", return_value=FakeSerialModule),
                patch.object(ps4uart, "open_serial", return_value=FakeSerial()),
                patch.object(ps4uart, "write_all", side_effect=partial_then_fail),
            ):
                ps4uart.capture_loop(raw, events, "/dev/fake", "generation")

            self.assertEqual(raw.read_bytes(), b"ab")
            state = json.loads(state_path.read_text())
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["bytes_written"], 2)
            self.assertIn("storage write failed", state["failure_reason"])
            event_names = [json.loads(line)["event"] for line in events.read_text().splitlines()]
            self.assertIn("capture_storage_failed", event_names)
            self.assertEqual(event_names.count("serial_opened"), 1)

    def test_free_space_floor_stops_even_when_uart_is_quiet(self):
        class FakeSerialException(Exception):
            pass

        class FakeSerialModule:
            SerialException = FakeSerialException

        class QuietSerial:
            in_waiting = 0

            def read(self, _size):
                raise AssertionError("free-space guard must run before serial read")

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "capture.raw"
            events = root / "capture.events.jsonl"
            raw.touch()
            events.touch()
            with (
                patch.object(ps4uart, "CAPTURE_STATE_FILE", root / ".state.json"),
                patch.object(ps4uart, "FENCE_REQUEST_FILE", root / ".fence.json"),
                patch.object(ps4uart, "LOG_DIR", root),
                patch.object(ps4uart, "MIN_FREE_BYTES", 1),
                patch.object(ps4uart, "serial_module", return_value=FakeSerialModule),
                patch.object(ps4uart, "open_serial", return_value=QuietSerial()),
                patch.object(
                    ps4uart.shutil,
                    "disk_usage",
                    return_value=SimpleNamespace(total=10, used=10, free=0),
                ),
            ):
                ps4uart.capture_loop(raw, events, "/dev/fake", "generation")

            self.assertEqual(raw.read_bytes(), b"")
            state = json.loads((root / ".state.json").read_text())
            self.assertEqual(state["status"], "failed")
            self.assertIn("free-space floor reached", state["failure_reason"])

    def test_start_rejects_running_but_not_ready_capture(self):
        with (
            patch.object(ps4uart, "is_running", return_value=(True, 42)),
            patch.object(
                ps4uart,
                "capture_ready",
                return_value=(False, "serial status is reconnecting", {}),
            ),
            patch.object(ps4uart, "current_logfile", return_value=None),
        ):
            self.assertEqual(ps4uart.cmd_start(SimpleNamespace(port=None)), 1)

    def test_stop_retains_state_when_child_survives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_file = root / ".uart_pid"
            pid_file.write_text("42")
            with (
                patch.object(ps4uart, "SESSION_FILE", root / ".session"),
                patch.object(ps4uart, "PID_FILE", pid_file),
                patch.object(ps4uart, "is_running", return_value=(True, 42)),
                patch.object(ps4uart.os, "kill"),
                patch.object(ps4uart.time, "sleep"),
            ):
                self.assertEqual(ps4uart.cmd_stop(SimpleNamespace()), 1)
            self.assertTrue(pid_file.exists())

    def test_write_all_recovers_from_short_writes(self):
        class ShortWriter:
            def __init__(self):
                self.value = bytearray()

            def write(self, data):
                count = min(2, len(data))
                self.value.extend(data[:count])
                return count

        writer = ShortWriter()
        ps4uart.write_all(writer, b"abcdef")
        self.assertEqual(bytes(writer.value), b"abcdef")

    def test_capture_fence_drains_pending_bytes_before_ack(self):
        class PendingSerial:
            def __init__(self, value):
                self.value = bytearray(value)

            @property
            def in_waiting(self):
                return len(self.value)

            def read(self, size):
                result = bytes(self.value[:size])
                del self.value[:size]
                return result

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw_path = root / "capture.raw"
            events_path = root / "capture.events.jsonl"
            fence_path = root / ".fence.json"
            state_path = root / ".state.json"
            fence_path.write_text(
                json.dumps({"id": "fence-id", "generation": "generation"})
            )
            state = {
                "generation": "generation",
                "connection_epoch": 1,
                "event_seq": 2,
                "bytes_written": 0,
                "status": "open",
            }
            with (
                raw_path.open("wb", buffering=0) as raw,
                events_path.open("a") as events,
                patch.object(ps4uart, "FENCE_REQUEST_FILE", fence_path),
                patch.object(ps4uart, "CAPTURE_STATE_FILE", state_path),
                patch.object(ps4uart, "LOG_DIR", root),
                patch.object(ps4uart, "MIN_FREE_BYTES", 0),
            ):
                ps4uart.acknowledge_fence(
                    raw, events, PendingSerial(b"late panic\r\n"), state
                )

            self.assertEqual(raw_path.read_bytes(), b"late panic\r\n")
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved["fence_ack"]["id"], "fence-id")
            self.assertEqual(saved["fence_ack"]["raw_offset"], 12)
            self.assertFalse(fence_path.exists())

    def test_lifecycle_lock_refuses_second_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary) / "lifecycle.lock"
            first = ps4uart.acquire_lock(lock_path, "test lifecycle")
            try:
                with self.assertRaises(RuntimeError):
                    ps4uart.acquire_lock(lock_path, "test lifecycle")
            finally:
                first.close()

    def test_clean_refuses_active_session_after_capture_dies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            logs = root / "logs"
            logs.mkdir()
            raw = logs / "ps4_uart_test.raw"
            raw.write_bytes(b"evidence")
            session = root / ".session.json"
            session.write_text("{}")
            with (
                patch.object(ps4uart, "LOG_DIR", logs),
                patch.object(ps4uart, "SESSION_FILE", session),
                patch.object(ps4uart, "is_running", return_value=(False, None)),
            ):
                self.assertEqual(ps4uart._cmd_clean_unlocked(SimpleNamespace()), 1)
            self.assertEqual(raw.read_bytes(), b"evidence")


class SessionIntegrationTests(unittest.TestCase):
    def session_environment(self, root):
        source = root / "continuous.raw"
        events = root / "continuous.events.jsonl"
        source.write_bytes(b"old river\n")
        events.write_text('{"seq": 1, "event": "serial_opened"}\n')
        state_file = root / ".uart_state.json"
        state_file.write_text(
            json.dumps(
                {
                    "pid": 42,
                    "status": "open",
                    "updated_at": datetime.now().astimezone().isoformat(),
                    "generation": "capture-generation",
                    "connection_epoch": 1,
                    "event_seq": 1,
                    "raw_log": str(source),
                    "events_log": str(events),
                }
            )
        )
        return source, events, state_file

    def patches(self, root, source, state_file):
        session_dir = root / "sessions"
        events = root / "continuous.events.jsonl"

        def fence(_capture, _request_id=None):
            return (
                {
                    "raw_offset": source.stat().st_size,
                    "events_offset": events.stat().st_size,
                    "connection_epoch": 1,
                },
                None,
            )

        return (
            patch.object(ps4uart, "SESSION_FILE", root / ".uart_session.json"),
            patch.object(ps4uart, "SESSION_DIR", session_dir),
            patch.object(ps4uart, "SESSION_INDEX", session_dir / "index.jsonl"),
            patch.object(ps4uart, "CAPTURE_STATE_FILE", state_file),
            patch.object(ps4uart, "is_running", return_value=(True, 42)),
            patch.object(ps4uart, "current_logfile", return_value=source),
            patch.object(ps4uart, "request_capture_fence", side_effect=fence),
        )

    def test_extracts_only_exact_bytes_after_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, state_file = self.session_environment(root)
            patches = self.patches(root, source, state_file)
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                start = SimpleNamespace(label="EXP-20260810-001-A1 load Linux payload")
                self.assertEqual(ps4uart.cmd_session_start(start), 0)
                captured = b"init\r\ninit\r\n\xffdone\x00\r\n"
                with source.open("ab") as stream:
                    stream.write(captured)

                self.assertEqual(
                    ps4uart.cmd_session_stop(SimpleNamespace(max_lines=2000)), 0
                )

            session_dir = root / "sessions"
            raw_files = list(session_dir.glob("*.raw"))
            context_files = list(session_dir.glob("*.md"))
            event_files = list(session_dir.glob("*.events.jsonl"))
            self.assertEqual(len(raw_files), 1)
            self.assertEqual(len(event_files), 1)
            self.assertEqual(raw_files[0].read_bytes(), captured)
            context = context_files[0].read_text()
            self.assertNotIn("old river", context)
            self.assertIn("previous line repeated 1 more times", context)
            records = [
                json.loads(line)
                for line in (session_dir / "index.jsonl").read_text().splitlines()
            ]
            self.assertEqual(records[0]["state"], "completed")
            self.assertEqual(records[0]["bytes"], len(captured))

    def test_disconnect_marks_partial_evidence_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, state_file = self.session_environment(root)
            patches = self.patches(root, source, state_file)
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                self.assertEqual(
                    ps4uart.cmd_session_start(
                        SimpleNamespace(label="EXP-20260810-001-A1 payload")
                    ),
                    0,
                )
                with source.open("ab") as stream:
                    stream.write(b"partial\n")
                capture = json.loads(state_file.read_text())
                capture["connection_epoch"] = 2
                capture["event_seq"] = 3
                state_file.write_text(json.dumps(capture))

                result = ps4uart.cmd_session_stop(SimpleNamespace(max_lines=2000))

            self.assertEqual(result, 2)
            session_dir = root / "sessions"
            record = json.loads((session_dir / "index.jsonl").read_text().strip())
            self.assertEqual(record["state"], "invalid")
            self.assertIn("serial connection epoch changed", record["continuity_errors"])
            self.assertEqual(list(session_dir.glob("*.raw"))[0].read_bytes(), b"partial\n")
            self.assertFalse((root / ".uart_session.json").exists())

    def test_abort_is_indexed_with_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, state_file = self.session_environment(root)
            patches = self.patches(root, source, state_file)
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                ps4uart.cmd_session_start(
                    SimpleNamespace(label="EXP-20260810-001-A1 bad capture")
                )
                with source.open("ab") as stream:
                    stream.write(b"partial\n")
                ps4uart.cmd_session_abort(
                    SimpleNamespace(reason="operator action never began", max_lines=2000)
                )

            record = json.loads(
                (root / "sessions" / "index.jsonl").read_text().strip()
            )
            self.assertEqual(record["state"], "aborted")
            self.assertEqual(record["reason"], "operator action never began")
            self.assertEqual(record["bytes"], 8)
            self.assertEqual(
                list((root / "sessions").glob("*.raw"))[0].read_bytes(), b"partial\n"
            )

    def test_rejects_untracked_session_label(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_file = Path(temporary) / ".session.json"
            with patch.object(ps4uart, "SESSION_FILE", state_file):
                result = ps4uart.cmd_session_start(SimpleNamespace(label="try payload"))
            self.assertEqual(result, 1)
            self.assertFalse(state_file.exists())

    def test_exclusive_state_write_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_file = Path(temporary) / "state.json"
            ps4uart.exclusive_write_json(state_file, {"owner": "first"})
            with self.assertRaises(FileExistsError):
                ps4uart.exclusive_write_json(state_file, {"owner": "second"})
            self.assertEqual(json.loads(state_file.read_text()), {"owner": "first"})

    def test_session_list_rejects_nonpositive_limit(self):
        self.assertEqual(ps4uart.cmd_session_list(SimpleNamespace(limit=0)), 1)

    def test_session_index_is_idempotent_by_session_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session_dir = root / "sessions"
            with (
                patch.object(ps4uart, "SESSION_DIR", session_dir),
                patch.object(ps4uart, "SESSION_INDEX", session_dir / "index.jsonl"),
            ):
                record = {"session_id": "same", "state": "completed"}
                self.assertTrue(ps4uart.append_session_index(record))
                self.assertFalse(ps4uart.append_session_index(record))
            self.assertEqual(
                len((session_dir / "index.jsonl").read_text().splitlines()), 1
            )

    def test_retry_after_index_crash_reuses_original_evidence_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, state_file = self.session_environment(root)
            session_state = root / ".uart_session.json"
            patches = self.patches(root, source, state_file)
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                ps4uart.cmd_session_start(
                    SimpleNamespace(label="EXP-20260810-001-A1 retry fence")
                )
                with source.open("ab") as stream:
                    stream.write(b"bounded\n")

                original_unlink = Path.unlink

                def fail_marker_unlink(path, *args, **kwargs):
                    if path == session_state:
                        raise OSError("simulated crash after index")
                    return original_unlink(path, *args, **kwargs)

                with patch.object(Path, "unlink", new=fail_marker_unlink):
                    with self.assertRaises(OSError):
                        ps4uart.cmd_session_stop(SimpleNamespace(max_lines=2000))

                session_dir = root / "sessions"
                raw_path = list(session_dir.glob("*.raw"))[0]
                original_hash = ps4uart.sha256_file(raw_path)
                original_record = json.loads(
                    (session_dir / "index.jsonl").read_text().strip()
                )

                with source.open("ab") as stream:
                    stream.write(b"later unrelated UART\n")
                self.assertEqual(
                    ps4uart.cmd_session_stop(SimpleNamespace(max_lines=2000)), 0
                )

            self.assertEqual(ps4uart.sha256_file(raw_path), original_hash)
            self.assertEqual(raw_path.read_bytes(), b"bounded\n")
            records = (session_dir / "index.jsonl").read_text().splitlines()
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0]), original_record)
            self.assertFalse(session_state.exists())

    def test_retry_after_post_fence_crash_reuses_first_fence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, state_file = self.session_environment(root)
            patches = self.patches(root, source, state_file)
            with ExitStack() as stack:
                entered = [stack.enter_context(item) for item in patches]
                fence_mock = entered[-1]
                ps4uart.cmd_session_start(
                    SimpleNamespace(label="EXP-20260810-001-A1 post-fence crash")
                )
                with source.open("ab") as stream:
                    stream.write(b"bounded\n")

                with patch.object(
                    ps4uart,
                    "source_window",
                    side_effect=RuntimeError("simulated crash after fence"),
                ):
                    with self.assertRaises(RuntimeError):
                        ps4uart.cmd_session_stop(SimpleNamespace(max_lines=2000))

                marker = json.loads((root / ".uart_session.json").read_text())
                first_end = marker["finalization"]["fence"]["raw_end_offset"]
                self.assertEqual(first_end, len(b"old river\nbounded\n"))
                self.assertEqual(fence_mock.call_count, 1)

                with source.open("ab") as stream:
                    stream.write(b"later unrelated UART\n")
                self.assertEqual(
                    ps4uart.cmd_session_stop(SimpleNamespace(max_lines=2000)), 0
                )
                self.assertEqual(fence_mock.call_count, 1)

            raw_path = list((root / "sessions").glob("*.raw"))[0]
            self.assertEqual(raw_path.read_bytes(), b"bounded\n")


if __name__ == "__main__":
    unittest.main()
