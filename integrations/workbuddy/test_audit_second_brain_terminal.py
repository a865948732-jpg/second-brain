import contextlib
import copy
import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import audit_second_brain_terminal as auditor


spec = importlib.util.spec_from_file_location(
    "receipt_fixture", Path(__file__).with_name("test_validate_automation_receipt.py")
)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
SESSION = "2918bfc0-918a-4c0f-bf91-6bdb50cabfc6"
REQUEST = "request-fixture-001"
THREAD = "automation-1000000000001:1790868626797"


class TerminalAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.projects = self.root / "projects"
        self.project = self.projects / "example-vault"
        self.project.mkdir(parents=True)
        self.path = self.project / f"{SESSION}.jsonl"
        self.db = self.root / "workbuddy.db"
        self.output = fixture.build_receipt(
            business="HOLD", action="HOLD", overrides={"STEP_INPUTS": "FAIL"}
        )
        self.run = {
            "conversationId": SESSION,
            "output": self.output,
            "runResult": {
                "requestId": REQUEST,
                "messages": [{"role": "tool", "content": [{"type": "tool-result"}]}],
            },
        }
        self.terminal = {
            "type": "message", "role": "assistant", "status": "completed",
            "sessionId": SESSION, "providerData": {"conversationRequestId": REQUEST},
            "content": [{"type": "output_text", "text": self.output}],
        }
        self.records = [
            {"type": "message", "role": "user", "sessionId": SESSION},
            self.terminal,
        ]
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.executescript("""
                CREATE TABLE automation_runs (
                    thread_id TEXT PRIMARY KEY, automation_id TEXT, status TEXT,
                    result_success INTEGER, runs_json TEXT, metadata_json TEXT,
                    updated_at INTEGER
                );
                CREATE TABLE automation_runtime_state (
                    automation_id TEXT PRIMARY KEY, running INTEGER
                );
            """)
            connection.execute(
                "INSERT INTO automation_runtime_state VALUES (?,0)", (auditor.DAILY_ID,)
            )
        self.write_run()
        self.write_transcript()

    def write_run(self, *, status="PENDING_REVIEW", metadata=None):
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO automation_runs VALUES (?,?,?,?,?,?,?)",
                (THREAD, auditor.DAILY_ID, status, 0, json.dumps([self.run]),
                 json.dumps(metadata or {"conversationId": SESSION, "sessionId": SESSION}), 1),
            )

    def write_transcript(self):
        self.path.write_text(
            "\n".join(json.dumps(record) for record in self.records) + "\n", encoding="utf-8"
        )

    def assess(self):
        return auditor.assess_run(self.db, self.projects, thread_id=THREAD)

    def reject(self, code):
        with self.assertRaises(auditor.TerminalAuditError) as caught:
            self.assess()
        self.assertEqual(str(caught.exception), code)

    def test_real_terminal_hold_is_not_business_success(self):
        before = self.db.read_bytes()
        result = self.assess()
        self.assertTrue(result["terminal_verified"])
        self.assertTrue(result["receipt_valid"])
        self.assertEqual(result["declared_business_status"], "HOLD")
        self.assertFalse(result["complete_repair_proven"])
        self.assertEqual(result["stored_envelope_status"], "missing_final_assistant_message")
        self.assertEqual(self.db.read_bytes(), before)
        self.assertNotIn(self.output, json.dumps(result))

    def test_pass_receipt_does_not_prove_artifacts_or_host_gate(self):
        text = fixture.build_receipt()
        self.run["output"] = text
        self.terminal["content"][0]["text"] = text
        self.write_run()
        self.write_transcript()
        result = self.assess()
        self.assertEqual(result["declared_business_status"], "PASS")
        self.assertFalse(result["complete_repair_proven"])

    def test_running_is_not_replayed(self):
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE automation_runtime_state SET running=1")
        self.reject("automation_still_running")

    def test_request_identity_must_match(self):
        self.terminal["providerData"]["conversationRequestId"] = "wrong-request"
        self.write_transcript()
        self.reject("request_identity_mismatch")

    def test_session_identity_must_match(self):
        self.terminal["sessionId"] = "different-session"
        self.write_transcript()
        self.reject("session_identity_mismatch")

    def test_output_is_not_accepted_without_matching_terminal(self):
        self.terminal["content"][0]["text"] = "not the stored output"
        self.write_transcript()
        self.reject("terminal_output_mismatch")

    def test_intermediate_assistant_is_rejected(self):
        self.terminal["status"] = "in_progress"
        self.write_transcript()
        self.reject("assistant_not_completed")

    def test_later_business_event_invalidates_old_final(self):
        for event in ("message", "reasoning", "function_call", "function_call_result", "unknown"):
            with self.subTest(event=event):
                self.records = [copy.deepcopy(self.terminal), {"type": event}]
                self.write_transcript()
                self.reject("events_after_terminal")

    def test_metadata_only_snapshot_after_terminal_is_allowed(self):
        self.records.append({"type": "file-history-snapshot"})
        self.write_transcript()
        self.assertTrue(self.assess()["terminal_verified"])

    def test_tool_only_transcript_is_rejected(self):
        self.records = [{"type": "function_call_result", "content": self.output}]
        self.write_transcript()
        self.reject("missing_assistant_terminal")

    def test_nontext_terminal_part_is_rejected(self):
        self.terminal["content"].append({"type": "tool-result"})
        self.write_transcript()
        self.reject("terminal_content_not_text")

    def test_duplicate_json_keys_fail_closed(self):
        self.path.write_text('{"type":"message","type":"message"}\n', encoding="utf-8")
        self.reject("duplicate_json_key")

    def test_malformed_utf8_fail_closed(self):
        self.path.write_bytes(b"\xff")
        self.reject("invalid_transcript_utf8")

    def test_ambiguous_transcript_fail_closed(self):
        other = self.projects / "other"
        other.mkdir()
        (other / self.path.name).write_bytes(self.path.read_bytes())
        self.reject("ambiguous_or_missing_transcript")

    def test_mismatched_metadata_fail_closed(self):
        self.write_run(metadata={"conversationId": "wrong-session"})
        self.reject("database_session_mismatch")

    def test_invalid_receipt_is_not_echoed(self):
        text = "untrusted sensitive payload"
        self.run["output"] = text
        self.terminal["content"][0]["text"] = text
        self.write_run()
        self.write_transcript()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            self.reject("invalid_machine_receipt")
        self.assertEqual(stream.getvalue(), "")

    def test_empty_db_is_not_created(self):
        absent = self.root / "absent.db"
        with self.assertRaises(auditor.TerminalAuditError):
            auditor.assess_run(absent, self.projects, thread_id=THREAD)
        self.assertFalse(absent.exists())

    def test_duplicate_database_json_keys_fail_closed(self):
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE automation_runs SET runs_json=?",
                ('[{"conversationId":"first","conversationId":"second"}]',),
            )
        self.reject("duplicate_json_key")

    def test_transcript_cannot_resolve_outside_project_root(self):
        original_resolve = Path.resolve
        external = self.root / "external.jsonl"
        external.write_bytes(self.path.read_bytes())

        def resolve(path, *args, **kwargs):
            if path == self.path:
                return external
            return original_resolve(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            self.reject("transcript_outside_projects")

    def test_transcript_change_during_read_fails_closed(self):
        original_open = Path.open
        changed = False

        @contextlib.contextmanager
        def open_file(path, mode="r", *args, **kwargs):
            nonlocal changed
            with original_open(path, mode, *args, **kwargs) as stream:
                yield stream
            if path == self.path and mode == "rb" and not changed:
                changed = True
                with original_open(path, "ab") as writer:
                    writer.write(b'{"type":"file-history-snapshot"}\n')

        with patch.object(Path, "open", open_file):
            self.reject("evidence_changed_during_audit")

    def test_database_change_during_read_fails_closed(self):
        original_open = Path.open
        changed = False

        @contextlib.contextmanager
        def open_file(path, mode="r", *args, **kwargs):
            nonlocal changed
            with original_open(path, mode, *args, **kwargs) as stream:
                yield stream
            if path == self.path and mode == "rb" and not changed:
                changed = True
                with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
                    connection.execute("UPDATE automation_runs SET updated_at=2")

        with patch.object(Path, "open", open_file):
            self.reject("evidence_changed_during_audit")

    def test_cli_hold_uses_nonzero_exit_without_body_echo(self):
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(Path(auditor.__file__)),
             "--db", str(self.db), "--projects", str(self.projects), "--thread-id", THREAD],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["declared_business_status"], "HOLD")
        self.assertEqual(result.stderr, "")
        self.assertNotIn(self.output, result.stdout)


if __name__ == "__main__":
    unittest.main()
