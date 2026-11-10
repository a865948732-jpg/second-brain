import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import resolve_second_brain_run_context as resolver


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
AUTOMATION_ID = "automation-test"


def epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RunContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "workbuddy.db"
        connection = sqlite3.connect(self.db)
        connection.executescript(
            """
            CREATE TABLE automations (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                rrule TEXT NOT NULL,
                deleted_at INTEGER
            );
            CREATE TABLE automation_runtime_state (
                automation_id TEXT PRIMARY KEY,
                running INTEGER NOT NULL,
                running_started_at INTEGER,
                running_conversation_id TEXT
            );
            CREATE TABLE automation_runs (
                thread_id TEXT PRIMARY KEY,
                automation_id TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata_json TEXT,
                created_at INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO automations(
                id,status,schedule_type,rrule,deleted_at
            ) VALUES(?,?,?,?,NULL)
            """,
            (
                AUTOMATION_ID,
                "ACTIVE",
                "recurring",
                "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA;BYHOUR=23;BYMINUTE=30",
            ),
        )
        connection.execute(
            """
            INSERT INTO automation_runtime_state(
                automation_id,running,running_started_at,running_conversation_id
            ) VALUES(?,0,NULL,NULL)
            """,
            (AUTOMATION_ID,),
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def add_run(
        self,
        *,
        created_at: int,
        run_kind: str = "scheduled",
        missed_scheduled_at: int | str | None = None,
        conversation_id: str = "conversation-1",
        status: str = "ACCEPTED",
        raw_metadata: str | None = None,
        thread_suffix: str = "1",
    ) -> str:
        thread_id = f"{AUTOMATION_ID}:{thread_suffix}"
        metadata = raw_metadata
        if metadata is None:
            metadata = json.dumps(
                {
                    "runKind": run_kind,
                    "missedScheduledAt": missed_scheduled_at,
                    "conversationId": conversation_id,
                }
            )
        connection = sqlite3.connect(self.db)
        connection.execute(
            """
            INSERT INTO automation_runs(
                thread_id,automation_id,status,metadata_json,created_at
            ) VALUES(?,?,?,?,?)
            """,
            (thread_id, AUTOMATION_ID, status, metadata, created_at),
        )
        connection.commit()
        connection.close()
        return thread_id

    def test_scheduled_run_uses_rrule_occurrence_in_shanghai(self):
        created = epoch_ms(datetime(2026, 9, 2, 23, 30, tzinfo=SHANGHAI))
        thread_id = self.add_run(created_at=created)

        result = resolver.resolve_run_context(
            self.db,
            automation_id=AUTOMATION_ID,
            now_ms=created + 60_000,
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["run_kind"], "scheduled")
        self.assertEqual(result["target_date"], "2026-09-02")
        self.assertEqual(result["occurrence_ms"], created)
        self.assertEqual(result["thread_id"], thread_id)
        self.assertEqual(result["context_source"], "rrule_occurrence")

    def test_delayed_scheduled_run_after_midnight_uses_rrule_occurrence(self):
        occurrence = epoch_ms(datetime(2026, 9, 2, 23, 30, tzinfo=SHANGHAI))
        created = epoch_ms(datetime(2026, 9, 3, 0, 5, tzinfo=SHANGHAI))
        self.add_run(created_at=created)

        result = resolver.resolve_run_context(
            self.db,
            automation_id=AUTOMATION_ID,
            now_ms=created + 60_000,
        )

        self.assertEqual(result["run_kind"], "scheduled")
        self.assertEqual(result["target_date"], "2026-09-02")
        self.assertEqual(result["occurrence_ms"], occurrence)
        self.assertEqual(result["context_source"], "rrule_occurrence")

    def test_scheduled_run_with_malformed_rrule_fails_closed(self):
        created = epoch_ms(datetime(2026, 9, 2, 23, 30, tzinfo=SHANGHAI))
        self.add_run(created_at=created)
        connection = sqlite3.connect(self.db)
        connection.execute(
            "UPDATE automations SET rrule='FREQ=WEEKLY;BYHOUR=23' WHERE id=?",
            (AUTOMATION_ID,),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(resolver.RunContextError, "rrule"):
            resolver.resolve_run_context(
                self.db,
                automation_id=AUTOMATION_ID,
                now_ms=created + 60_000,
            )

    def test_scheduled_run_beyond_dispatch_window_fails_closed(self):
        created = epoch_ms(datetime(2026, 9, 3, 0, 31, tzinfo=SHANGHAI))
        self.add_run(created_at=created)

        with self.assertRaisesRegex(resolver.RunContextError, "dispatch delay"):
            resolver.resolve_run_context(
                self.db,
                automation_id=AUTOMATION_ID,
                now_ms=created + 60_000,
            )

    def test_missed_run_uses_missed_scheduled_at_not_dispatch_date(self):
        occurrence = epoch_ms(datetime(2026, 9, 2, 23, 30, tzinfo=SHANGHAI))
        created = epoch_ms(datetime(2026, 9, 3, 5, 12, tzinfo=SHANGHAI))
        self.add_run(
            created_at=created,
            run_kind="missed",
            missed_scheduled_at=occurrence,
        )

        result = resolver.resolve_run_context(
            self.db,
            automation_id=AUTOMATION_ID,
            now_ms=created + 60_000,
        )

        self.assertEqual(result["run_kind"], "missed")
        self.assertEqual(result["target_date"], "2026-09-02")
        self.assertEqual(result["occurrence_ms"], occurrence)
        self.assertEqual(result["context_source"], "missedScheduledAt")

    def test_current_native_window_accepts_fifteen_hour_replay(self):
        occurrence = epoch_ms(datetime(2026, 10, 6, 23, 30, tzinfo=SHANGHAI))
        created = epoch_ms(datetime(2026, 10, 7, 14, 54, tzinfo=SHANGHAI))
        self.add_run(created_at=created, run_kind="missed", missed_scheduled_at=occurrence)
        before = digest(self.db)
        result = resolver.resolve_run_context(
            self.db, automation_id=AUTOMATION_ID, now_ms=created + 60_000
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["target_date"], "2026-10-06")
        self.assertEqual(digest(self.db), before)

    def test_current_native_window_accepts_exact_twenty_four_hour_boundary(self):
        occurrence = epoch_ms(datetime(2026, 10, 6, 23, 30, tzinfo=SHANGHAI))
        created = occurrence + 24 * 60 * 60 * 1000
        self.add_run(created_at=created, run_kind="missed", missed_scheduled_at=occurrence)
        result = resolver.resolve_run_context(
            self.db, automation_id=AUTOMATION_ID, now_ms=created + 60_000
        )
        self.assertEqual(result["status"], "PASS")

    def test_current_native_window_rejects_one_millisecond_beyond_boundary(self):
        occurrence = epoch_ms(datetime(2026, 10, 6, 23, 30, tzinfo=SHANGHAI))
        created = occurrence + 24 * 60 * 60 * 1000 + 1
        self.add_run(created_at=created, run_kind="missed", missed_scheduled_at=occurrence)
        with self.assertRaisesRegex(resolver.RunContextError, "native replay window"):
            resolver.resolve_run_context(
                self.db, automation_id=AUTOMATION_ID, now_ms=created + 60_000
            )

    def test_running_conversation_selects_matching_current_run(self):
        current = epoch_ms(datetime(2026, 9, 2, 23, 30, tzinfo=SHANGHAI))
        self.add_run(
            created_at=current,
            conversation_id="current-conversation",
            status="IN_PROGRESS",
            thread_suffix="current",
        )
        self.add_run(
            created_at=current + 1_000,
            conversation_id="different-conversation",
            thread_suffix="newer-but-not-current",
        )
        connection = sqlite3.connect(self.db)
        connection.execute(
            """
            UPDATE automation_runtime_state
            SET running=1,running_started_at=?,running_conversation_id=?
            WHERE automation_id=?
            """,
            (current, "current-conversation", AUTOMATION_ID),
        )
        connection.commit()
        connection.close()

        result = resolver.resolve_run_context(
            self.db,
            automation_id=AUTOMATION_ID,
            now_ms=current + 60_000,
        )

        self.assertEqual(result["thread_id"], f"{AUTOMATION_ID}:current")
        self.assertEqual(result["selection"], "running_conversation")

    def test_running_stale_conversation_id_uses_current_in_progress_start(self):
        current = epoch_ms(datetime(2026, 9, 3, 23, 30, tzinfo=SHANGHAI))
        self.add_run(
            created_at=current - 60_000,
            conversation_id="stale-conversation",
            thread_suffix="prior",
        )
        self.add_run(
            created_at=current + 1,
            conversation_id="actual-current-conversation",
            status="IN_PROGRESS",
            thread_suffix="current",
        )
        connection = sqlite3.connect(self.db)
        connection.execute(
            """
            UPDATE automation_runtime_state
            SET running=1,running_started_at=?,running_conversation_id=?
            WHERE automation_id=?
            """,
            (current, "stale-conversation", AUTOMATION_ID),
        )
        connection.commit()
        connection.close()

        result = resolver.resolve_run_context(
            self.db,
            automation_id=AUTOMATION_ID,
            now_ms=current + 60_000,
        )

        self.assertEqual(result["thread_id"], f"{AUTOMATION_ID}:current")
        self.assertEqual(result["selection"], "running_started_at")
        self.assertEqual(result["run_start_delta_ms"], 1)

    def test_running_without_started_at_fails_closed(self):
        current = epoch_ms(datetime(2026, 9, 3, 23, 30, tzinfo=SHANGHAI))
        self.add_run(
            created_at=current,
            status="IN_PROGRESS",
            thread_suffix="current",
        )
        connection = sqlite3.connect(self.db)
        connection.execute(
            """
            UPDATE automation_runtime_state
            SET running=1,running_started_at=NULL,running_conversation_id=NULL
            WHERE automation_id=?
            """,
            (AUTOMATION_ID,),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(resolver.RunContextError, "start time"):
            resolver.resolve_run_context(
                self.db,
                automation_id=AUTOMATION_ID,
                now_ms=current + 60_000,
            )

    def test_running_ambiguous_start_records_fail_closed(self):
        current = epoch_ms(datetime(2026, 9, 3, 23, 30, tzinfo=SHANGHAI))
        self.add_run(
            created_at=current,
            conversation_id="current-a",
            status="IN_PROGRESS",
            thread_suffix="current-a",
        )
        self.add_run(
            created_at=current + 1,
            conversation_id="current-b",
            status="IN_PROGRESS",
            thread_suffix="current-b",
        )
        connection = sqlite3.connect(self.db)
        connection.execute(
            """
            UPDATE automation_runtime_state
            SET running=1,running_started_at=?,running_conversation_id=?
            WHERE automation_id=?
            """,
            (current, "stale-conversation", AUTOMATION_ID),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(resolver.RunContextError, "ambiguous"):
            resolver.resolve_run_context(
                self.db,
                automation_id=AUTOMATION_ID,
                now_ms=current + 60_000,
            )

    def test_running_duplicate_start_records_fail_even_when_conversation_matches(self):
        current = epoch_ms(datetime(2026, 9, 3, 23, 30, tzinfo=SHANGHAI))
        self.add_run(
            created_at=current,
            conversation_id="reported-conversation",
            status="IN_PROGRESS",
            thread_suffix="current-a",
        )
        self.add_run(
            created_at=current + 1,
            conversation_id="other-conversation",
            status="IN_PROGRESS",
            thread_suffix="current-b",
        )
        connection = sqlite3.connect(self.db)
        connection.execute(
            """
            UPDATE automation_runtime_state
            SET running=1,running_started_at=?,running_conversation_id=?
            WHERE automation_id=?
            """,
            (current, "reported-conversation", AUTOMATION_ID),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(resolver.RunContextError, "ambiguous"):
            resolver.resolve_run_context(
                self.db,
                automation_id=AUTOMATION_ID,
                now_ms=current + 60_000,
            )

    def test_stale_latest_run_fails_closed_at_age_boundary(self):
        created = epoch_ms(datetime(2026, 9, 2, 23, 30, tzinfo=SHANGHAI))
        self.add_run(created_at=created)

        with self.assertRaisesRegex(resolver.RunContextError, "stale"):
            resolver.resolve_run_context(
                self.db,
                automation_id=AUTOMATION_ID,
                now_ms=created + resolver.DEFAULT_MAX_RUN_AGE_MS + 1,
            )

    def test_malformed_metadata_fails_closed(self):
        created = epoch_ms(datetime(2026, 9, 2, 23, 30, tzinfo=SHANGHAI))
        self.add_run(created_at=created, raw_metadata="{broken")

        with self.assertRaisesRegex(resolver.RunContextError, "metadata"):
            resolver.resolve_run_context(
                self.db,
                automation_id=AUTOMATION_ID,
                now_ms=created + 60_000,
            )

    def test_missed_run_rejects_non_integer_occurrence(self):
        created = epoch_ms(datetime(2026, 9, 3, 5, 12, tzinfo=SHANGHAI))
        self.add_run(
            created_at=created,
            run_kind="missed",
            missed_scheduled_at="2026-09-02",
        )

        with self.assertRaisesRegex(resolver.RunContextError, "missedScheduledAt"):
            resolver.resolve_run_context(
                self.db,
                automation_id=AUTOMATION_ID,
                now_ms=created + 60_000,
            )

    def test_inactive_automation_fails_closed(self):
        created = epoch_ms(datetime(2026, 9, 2, 23, 30, tzinfo=SHANGHAI))
        self.add_run(created_at=created)
        connection = sqlite3.connect(self.db)
        connection.execute(
            "UPDATE automations SET status='PAUSED' WHERE id=?",
            (AUTOMATION_ID,),
        )
        connection.commit()
        connection.close()

        with self.assertRaisesRegex(resolver.RunContextError, "ACTIVE"):
            resolver.resolve_run_context(
                self.db,
                automation_id=AUTOMATION_ID,
                now_ms=created + 60_000,
            )

    def test_resolution_is_read_only(self):
        created = epoch_ms(datetime(2026, 9, 2, 23, 30, tzinfo=SHANGHAI))
        self.add_run(created_at=created)
        before = digest(self.db)

        resolver.resolve_run_context(
            self.db,
            automation_id=AUTOMATION_ID,
            now_ms=created + 60_000,
        )

        self.assertEqual(digest(self.db), before)


if __name__ == "__main__":
    unittest.main()
