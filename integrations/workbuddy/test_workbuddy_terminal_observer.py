import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import workbuddy_terminal_observer as observer


SESSION = "2918bfc0-918a-4c0f-bf91-6bdb50cabfc6"
REQUEST = "request-fixture-001"
THREAD = "automation-1000000000001:1790868626797"
NOW = 1790868700000


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.projects = self.root / "projects"
        self.project = self.projects / "example-vault"
        self.project.mkdir(parents=True)
        self.transcript = self.project / f"{SESSION}.jsonl"
        self.observations = self.root / "observations"
        self.db = self.root / "workbuddy.db"
        self.vault = self.root / "vault"
        self.vault.mkdir()
        fixture_path = Path(__file__).with_name("test_validate_automation_receipt.py")
        validator_spec = importlib.util.spec_from_file_location(
            "observer_fixture_validator", fixture_path.with_name("validate_automation_receipt.py")
        )
        validator = importlib.util.module_from_spec(validator_spec)
        validator_spec.loader.exec_module(validator)
        spec = importlib.util.spec_from_file_location("observer_fixture", fixture_path)
        fixture = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"validate_automation_receipt": validator}):
            spec.loader.exec_module(fixture)
        self.answer = fixture.build_receipt(business="HOLD", action="HOLD")
        self.terminal = {
            "type": "message", "role": "assistant", "status": "completed",
            "sessionId": SESSION, "providerData": {"conversationRequestId": REQUEST},
            "content": [{"type": "output_text", "text": self.answer}],
        }
        self.records = [self.terminal]
        self.write_transcript()
        self.metadata = {"conversationId": SESSION, "sessionId": SESSION}
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.executescript("""
                CREATE TABLE automation_runs (
                    thread_id TEXT PRIMARY KEY, automation_id TEXT, status TEXT,
                    source_cwd TEXT, created_at INTEGER, result_success INTEGER,
                    runs_json TEXT, metadata_json TEXT, updated_at INTEGER
                );
                CREATE TABLE automation_runtime_state (
                    automation_id TEXT PRIMARY KEY, running INTEGER,
                    running_started_at INTEGER, running_conversation_id TEXT
                );
            """)
            connection.execute(
                "INSERT INTO automation_runs VALUES (?,?,?,?,?,?,?,?,?)",
                (THREAD, observer.audit.DAILY_ID, "IN_PROGRESS", str(self.vault),
                 NOW - 1000, 0, "[]", json.dumps(self.metadata), NOW - 1000),
            )
            connection.execute(
                "INSERT INTO automation_runtime_state VALUES (?,1,?,?)",
                (observer.audit.DAILY_ID, NOW - 1001, SESSION),
            )
        self.payload = {
            "hook_event_name": "FinalStop", "final_stop_reason": "completed",
            "session_id": SESSION, "transcript_path": str(self.transcript),
            "cwd": str(self.vault),
        }

    def write_transcript(self):
        self.transcript.write_text(
            "\n".join(json.dumps(record) for record in self.records) + "\n", encoding="utf-8"
        )

    def capture(self):
        return observer.capture(
            self.payload, request_id=REQUEST, db_path=self.db, projects_root=self.projects,
            observation_root=self.observations, vault=self.vault, now_ms=NOW,
        )

    def finish(self, *, started_at=None):
        started_at = NOW - 1000 if started_at is None else started_at
        run = {"conversationId": SESSION, "output": self.answer,
               "startedAt": started_at,
               "runResult": {"requestId": REQUEST, "messages": [{"role": "tool"}]}}
        with contextlib.closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute(
                "UPDATE automation_runs SET status='PENDING_REVIEW',runs_json=?,created_at=?",
                (json.dumps([run]), started_at),
            )
            connection.execute("UPDATE automation_runtime_state SET running=0")

    def verify(self):
        return observer.verify(
            THREAD, db_path=self.db, projects_root=self.projects,
            observation_root=self.observations,
        )

    def test_final_stop_captures_without_touching_workbuddy_database(self):
        before = self.db.read_bytes()
        result = self.capture()
        self.assertEqual(result["evidence_status"], "CAPTURED")
        self.assertEqual(result["mode"], "OBSERVATION_ONLY")
        self.assertFalse(result["complete_repair_proven"])
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(len(list(self.observations.glob("*.json"))), 1)
        self.assertNotIn(self.answer, next(self.observations.glob("*.json")).read_text())

    def test_capture_and_readback_still_preserve_business_hold(self):
        self.capture()
        self.finish()
        before = self.db.read_bytes()
        result = self.verify()
        self.assertTrue(result["final_stop_observation_verified"])
        self.assertEqual(result["declared_business_status"], "HOLD")
        self.assertFalse(result["complete_repair_proven"])
        self.assertEqual(self.db.read_bytes(), before)

    def test_native_finalization_replaces_aggregate_clock_with_subrun_clock(self):
        captured = self.capture()
        self.finish(started_at=NOW - 963)
        before = self.db.read_bytes()
        result = self.verify()
        self.assertTrue(result["final_stop_observation_verified"])
        self.assertEqual(result["declared_business_status"], "HOLD")
        self.assertEqual(captured["run_identity_started_at"], int(THREAD.rsplit(":", 1)[1]))
        self.assertEqual(self.db.read_bytes(), before)

    def test_final_subrun_clock_must_exactly_match_serialized_run(self):
        self.capture()
        self.finish()
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE automation_runs SET created_at=created_at+37")
        with self.assertRaisesRegex(observer.ObservationError, "observation_run_time_mismatch"):
            self.verify()

    def test_final_clock_before_capture_clock_is_rejected(self):
        self.capture()
        self.finish(started_at=NOW - 1001)
        with self.assertRaisesRegex(observer.ObservationError, "observation_run_time_mismatch"):
            self.verify()

    def test_final_clock_after_observation_is_rejected(self):
        self.capture()
        self.finish(started_at=NOW + 1)
        with self.assertRaisesRegex(observer.ObservationError, "observation_run_time_mismatch"):
            self.verify()

    def test_serialized_start_clock_is_required_for_v2(self):
        self.capture()
        self.finish()
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            run = json.loads(c.execute("SELECT runs_json FROM automation_runs").fetchone()[0])
            del run[0]["startedAt"]
            c.execute("UPDATE automation_runs SET runs_json=?", (json.dumps(run),))
        with self.assertRaisesRegex(observer.ObservationError, "observation_run_time_mismatch"):
            self.verify()

    def test_legacy_observation_is_not_reinterpreted_after_clock_rewrite(self):
        self.capture()
        path = next(self.observations.glob("*.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record["schema"] = "second-brain-final-stop-observation-v1"
        record.pop("run_identity_started_at", None)
        path.write_text(json.dumps(record), encoding="utf-8")
        self.finish(started_at=NOW - 963)
        before = path.read_bytes()
        with self.assertRaisesRegex(observer.ObservationError, "observation_run_time_mismatch"):
            self.verify()
        self.assertEqual(path.read_bytes(), before)

    def test_unknown_observation_schema_is_rejected(self):
        self.capture()
        self.finish()
        path = next(self.observations.glob("*.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record["schema"] = "unknown"
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(observer.ObservationError, "observation_identity_or_evidence_mismatch"):
            self.verify()

    def test_run_identity_clock_cannot_be_borrowed_from_another_run(self):
        self.capture()
        self.finish()
        path = next(self.observations.glob("*.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        record["run_identity_started_at"] = int(THREAD.rsplit(":", 1)[1]) + 1
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(observer.ObservationError):
            self.verify()

    def test_future_run_identity_clock_is_rejected_before_capture(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE automation_runs SET thread_id=?", (observer.audit.DAILY_ID + ":" + str(NOW + 1),))
        with self.assertRaisesRegex(observer.ObservationError, "not_current_active_run"):
            self.capture()
        self.assertFalse(self.observations.exists())

    def test_repeat_is_idempotent_and_never_overwrites_changed_evidence(self):
        self.capture()
        path = next(self.observations.glob("*.json"))
        before = path.read_bytes()
        self.capture()
        self.assertEqual(path.read_bytes(), before)
        self.records.append({"type": "message", "role": "user"})
        self.write_transcript()
        with self.assertRaisesRegex(observer.ObservationError, "observation_replay_conflict"):
            self.capture()
        self.assertEqual(path.read_bytes(), before)

    def test_normal_chat_and_other_events_have_no_observation_side_effect(self):
        for key, value in (("hook_event_name", "Stop"), ("cwd", str(self.root)),
                           ("session_id", "11223344-1122-1122-1122-112233445566")):
            original = self.payload[key]
            self.payload[key] = value
            self.assertEqual(self.capture()["evidence_status"], "SKIPPED")
            self.payload[key] = original
        self.assertFalse(self.observations.exists())

    def test_noncompleted_wrong_request_or_unfinished_message_is_not_verified(self):
        for variant in ("cancelled", "request", "in_progress"):
            with self.subTest(variant=variant):
                self.observations = self.root / variant
                self.payload["final_stop_reason"] = "cancelled" if variant == "cancelled" else "completed"
                self.terminal["status"] = "in_progress" if variant == "in_progress" else "completed"
                self.terminal["providerData"]["conversationRequestId"] = "wrong" if variant == "request" else REQUEST
                self.write_transcript()
                self.assertEqual(self.capture()["evidence_status"], "NOT_VERIFIED")

    def test_stale_run_and_ambiguous_binding_do_not_create_evidence(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE automation_runtime_state SET running_started_at=?", (NOW - 9000,))
        with self.assertRaisesRegex(observer.ObservationError, "not_current_active_run"):
            self.capture()
        self.assertFalse(self.observations.exists())

    def test_two_matching_active_runs_are_rejected(self):
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO automation_runs SELECT ?,automation_id,status,source_cwd,"
                      "created_at,result_success,runs_json,metadata_json,updated_at FROM automation_runs",
                      (observer.audit.DAILY_ID + ":1790868626798",))
        with self.assertRaisesRegex(observer.ObservationError, "ambiguous_active_run"):
            self.capture()

    def test_transcript_path_mismatch_is_not_read_as_evidence(self):
        self.payload["transcript_path"] = str(self.root / "private.jsonl")
        result = self.capture()
        self.assertEqual(result["error"], "transcript_path_mismatch")
        self.assertEqual(result["evidence_status"], "NOT_VERIFIED")

    def test_duplicate_keys_and_later_events_are_not_captured(self):
        self.transcript.write_text('{"type":"message","type":"unknown"}', encoding="utf-8")
        self.assertEqual(self.capture()["error"], "duplicate_json_key")
        self.observations = self.root / "trailing"
        self.records.append({"type": "function_call"})
        self.write_transcript()
        self.assertEqual(self.capture()["error"], "events_after_terminal")

    def test_evidence_change_during_callback_is_recorded_as_failure(self):
        read = observer.audit._read_bytes
        def changing(path):
            result = read(path)
            if path == self.transcript:
                self.transcript.write_bytes(result + b'{}\n')
            return result
        with patch.object(observer.audit, "_read_bytes", side_effect=changing):
            self.assertEqual(self.capture()["error"], "evidence_changed_during_capture")

    def test_missing_observation_never_borrows_historical_terminal(self):
        self.finish()
        with self.assertRaisesRegex(observer.ObservationError, "native_final_stop_observation_missing"):
            self.verify()

    def test_tampered_observation_is_rejected(self):
        self.capture()
        self.finish()
        path = next(self.observations.glob("*.json"))
        record = json.loads(path.read_text(encoding="utf-8"))
        for key, value in (("thread_id", "other"), ("request_id", "other"),
                           ("transcript_sha256", "00"), ("run_created_at", 1),
                           ("transcript_relative_path", "other/path.jsonl")):
            with self.subTest(key=key):
                corrupted = {**record, key: value}
                path.write_text(json.dumps(corrupted), encoding="utf-8")
                with self.assertRaises(observer.ObservationError):
                    self.verify()

    def test_changed_transcript_after_capture_is_rejected(self):
        self.capture()
        self.finish()
        self.records.append({"type": "file-history-snapshot"})
        self.write_transcript()
        with self.assertRaisesRegex(observer.ObservationError, "observation_identity_or_evidence_mismatch"):
            self.verify()

    def test_observation_changes_during_readback_are_rejected(self):
        self.capture()
        self.finish()
        path = next(self.observations.glob("*.json"))
        read = observer.audit._read_bytes
        def changing(candidate):
            result = read(candidate)
            if candidate == path:
                path.write_bytes(result + b' ')
            return result
        with patch.object(observer.audit, "_read_bytes", side_effect=changing):
            with self.assertRaisesRegex(observer.ObservationError, "evidence_changed_during_verification"):
                self.verify()

    def test_changed_workspace_after_capture_is_rejected(self):
        self.capture()
        self.finish()
        with contextlib.closing(sqlite3.connect(self.db)) as c, c:
            c.execute("UPDATE automation_runs SET source_cwd=?", (str(self.root),))
        with self.assertRaisesRegex(observer.ObservationError, "observation_identity_or_evidence_mismatch"):
            self.verify()

    def test_created_time_changes_during_readback_are_rejected(self):
        self.capture()
        self.finish()
        read = observer._read_identity
        calls = 0
        def changing(db, thread):
            nonlocal calls
            result = read(db, thread)
            calls += 1
            if calls == 1:
                with contextlib.closing(sqlite3.connect(self.db)) as c, c:
                    c.execute("UPDATE automation_runs SET created_at=created_at+1")
            return result
        with patch.object(observer, "_read_identity", side_effect=changing):
            with self.assertRaisesRegex(observer.ObservationError, "evidence_changed_during_verification"):
                self.verify()

    def test_database_missing_never_creates_database(self):
        missing = self.root / "missing.db"
        with self.assertRaises(sqlite3.OperationalError):
            observer.capture(self.payload, request_id=REQUEST, db_path=missing,
                             vault=self.vault, now_ms=NOW)
        self.assertFalse(missing.exists())

    def test_running_cli_returns_wait_code_without_business_success(self):
        with patch.object(observer, "verify", side_effect=observer.audit.TerminalAuditError("automation_still_running")):
            with patch("sys.argv", ["observer", "--verify-thread", THREAD]), contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as caught:
                    observer.main()
        self.assertEqual(caught.exception.code, 3)
        self.assertFalse(json.loads(out.getvalue())["final_stop_observation_verified"])

    def test_extended_windows_path_is_same_workspace(self):
        self.payload["cwd"] = "\\\\?\\" + str(self.vault)
        self.payload["transcript_path"] = "\\\\?\\" + str(self.transcript)
        self.assertEqual(self.capture()["evidence_status"], "CAPTURED")

    def test_linked_observation_directory_is_rejected(self):
        import subprocess
        target = self.root / "target"
        target.mkdir()
        if os.name == "nt":
            command = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(self.observations), str(target)],
                                     capture_output=True, creationflags=0x08000000)
            self.assertEqual(command.returncode, 0, command.stderr)
        else:
            self.observations.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(observer.ObservationError, "reparse_path_not_allowed"):
            self.capture()
        self.assertEqual(list(target.iterdir()), [])


class FixtureIsolationTests(unittest.TestCase):
    def test_single_case_runs_without_external_import_path(self):
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-m", "unittest", "discover",
                 "-s", str(Path(__file__).parent), "-p", Path(__file__).name,
                 "-k", "test_final_stop_captures_without_touching_workbuddy_database"],
                cwd=cwd, capture_output=True, timeout=15,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        self.assertIn(b"Ran 1 test", result.stderr)


if __name__ == "__main__":
    unittest.main()
