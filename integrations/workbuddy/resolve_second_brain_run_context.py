#!/usr/bin/env python3
"""Resolve an immutable WorkBuddy automation run kind and target date."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_DB = Path(os.environ.get("SECOND_BRAIN_WORKBUDDY_HOME", Path(__file__).resolve().parents[2] / ".local" / "workbuddy")) / "workbuddy.db"
DEFAULT_MAX_RUN_AGE_MS = 30 * 60 * 1000
MAX_FUTURE_SKEW_MS = 5 * 60 * 1000
# This adapter explicitly bounds missed-run and scheduled-run delays.
MAX_MISSED_DELAY_MS = 24 * 60 * 60 * 1000
MAX_SCHEDULED_DELAY_MS = 60 * 60 * 1000
MAX_RUNNING_RECORD_SKEW_MS = 5 * 1000
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
WEEKDAYS = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}


class RunContextError(RuntimeError):
    """The database does not prove a safe current automation context."""


def _decode_metadata(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as error:
        raise RunContextError("run metadata is malformed") from error
    if not isinstance(value, dict):
        raise RunContextError("run metadata is not an object")
    return value


def _shanghai_date(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, SHANGHAI).date().isoformat()


def _parse_weekly_rrule(raw: str | None) -> tuple[set[int], int, int]:
    if not isinstance(raw, str) or not raw.strip():
        raise RunContextError("automation rrule is missing")
    fields: dict[str, str] = {}
    for part in raw.split(";"):
        key, separator, value = part.partition("=")
        key = key.strip().upper()
        value = value.strip().upper()
        if not separator or not key or not value or key in fields:
            raise RunContextError("automation rrule is malformed")
        fields[key] = value
    if fields.get("FREQ") != "WEEKLY" or fields.get("INTERVAL", "1") != "1":
        raise RunContextError("automation rrule must be a weekly interval-1 rule")
    try:
        day_tokens = fields["BYDAY"].split(",")
        weekdays = {WEEKDAYS[token] for token in day_tokens}
        hour = int(fields["BYHOUR"])
        minute = int(fields["BYMINUTE"])
    except (KeyError, TypeError, ValueError) as error:
        raise RunContextError("automation rrule is malformed") from error
    if (
        not day_tokens
        or any(token not in WEEKDAYS for token in day_tokens)
        or len(weekdays) != len(day_tokens)
        or not 0 <= hour <= 23
        or not 0 <= minute <= 59
        or "," in fields["BYHOUR"]
        or "," in fields["BYMINUTE"]
    ):
        raise RunContextError("automation rrule is malformed")
    return weekdays, hour, minute


def _scheduled_occurrence_ms(
    created_at: int,
    *,
    schedule_type: str | None,
    rrule: str | None,
) -> int:
    if schedule_type != "recurring":
        raise RunContextError("scheduled run requires a recurring automation")
    weekdays, hour, minute = _parse_weekly_rrule(rrule)
    created_local = datetime.fromtimestamp(created_at / 1000, SHANGHAI)
    for days_back in range(8):
        candidate_date = created_local.date() - timedelta(days=days_back)
        if candidate_date.weekday() not in weekdays:
            continue
        candidate = datetime(
            candidate_date.year,
            candidate_date.month,
            candidate_date.day,
            hour,
            minute,
            tzinfo=SHANGHAI,
        )
        occurrence_ms = int(candidate.timestamp() * 1000)
        dispatch_delay_ms = created_at - occurrence_ms
        if dispatch_delay_ms < -MAX_FUTURE_SKEW_MS:
            continue
        if dispatch_delay_ms > MAX_SCHEDULED_DELAY_MS:
            raise RunContextError("scheduled run exceeds the dispatch delay window")
        return occurrence_ms
    raise RunContextError("scheduled run does not match the automation rrule")


def _select_run(
    rows: list[sqlite3.Row],
    runtime: sqlite3.Row,
    automation_id: str,
) -> tuple:
    if not rows:
        raise RunContextError("automation has no run record")

    if runtime["running"] == 1:
        started_at = runtime["running_started_at"]
        if (
            isinstance(started_at, bool)
            or not isinstance(started_at, int)
            or started_at <= 0
        ):
            raise RunContextError("running automation has no valid start time")

        candidates = []
        for row in rows:
            created_at = row["created_at"]
            thread_id = row["thread_id"]
            if row["status"] != "IN_PROGRESS":
                continue
            if (
                not isinstance(thread_id, str)
                or not thread_id.startswith(f"{automation_id}:")
            ):
                continue
            if isinstance(created_at, bool) or not isinstance(created_at, int):
                continue
            if abs(created_at - started_at) > MAX_RUNNING_RECORD_SKEW_MS:
                continue
            candidates.append((row, _decode_metadata(row["metadata_json"])))

        if not candidates:
            raise RunContextError("current running start has no matching run record")

        if len(candidates) > 1:
            raise RunContextError("current running start has ambiguous run records")

        row, metadata = candidates[0]
        conversation_id = runtime["running_conversation_id"]
        if (
            isinstance(conversation_id, str)
            and conversation_id
            and metadata.get("conversationId") == conversation_id
        ):
            return row, metadata, "running_conversation"
        return row, metadata, "running_started_at"

    if runtime["running"] != 0:
        raise RunContextError("automation runtime running flag is invalid")
    row = rows[0]
    if row["status"] == "IN_PROGRESS":
        raise RunContextError("runtime is idle but latest run is still in progress")
    return row, _decode_metadata(row["metadata_json"]), "latest_fresh_run"


def resolve_run_context(
    db_path: Path,
    *,
    automation_id: str,
    now_ms: int | None = None,
    max_run_age_ms: int = DEFAULT_MAX_RUN_AGE_MS,
) -> dict:
    if not isinstance(automation_id, str) or not automation_id.strip():
        raise RunContextError("automation id is required")
    if max_run_age_ms <= 0:
        raise RunContextError("max run age must be positive")

    path = Path(db_path).resolve()
    if not path.is_file():
        raise RunContextError("WorkBuddy database is missing")
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms

    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
            timeout=2,
        )
        connection.row_factory = sqlite3.Row
        automation = connection.execute(
            """
            SELECT status,deleted_at,schedule_type,rrule
            FROM automations WHERE id=?
            """,
            (automation_id,),
        ).fetchone()
        runtime = connection.execute(
            """
            SELECT running,running_started_at,running_conversation_id
            FROM automation_runtime_state WHERE automation_id=?
            """,
            (automation_id,),
        ).fetchone()
        rows = list(
            connection.execute(
                """
                SELECT thread_id,status,metadata_json,created_at
                FROM automation_runs
                WHERE automation_id=?
                ORDER BY created_at DESC
                LIMIT 50
                """,
                (automation_id,),
            )
        )
    except (OSError, sqlite3.Error) as error:
        raise RunContextError("WorkBuddy database query failed") from error
    finally:
        if "connection" in locals():
            connection.close()

    if automation is None:
        raise RunContextError("automation is missing")
    if automation["deleted_at"] is not None or automation["status"] != "ACTIVE":
        raise RunContextError("automation must be ACTIVE")
    if runtime is None:
        raise RunContextError("automation runtime state is missing")

    row, metadata, selection = _select_run(rows, runtime, automation_id)
    created_at = row["created_at"]
    if isinstance(created_at, bool) or not isinstance(created_at, int):
        raise RunContextError("run created_at is invalid")
    age_ms = current_ms - created_at
    if age_ms > max_run_age_ms:
        raise RunContextError("latest automation run is stale")
    if age_ms < -MAX_FUTURE_SKEW_MS:
        raise RunContextError("latest automation run is in the future")

    run_kind = metadata.get("runKind")
    if run_kind == "missed":
        occurrence_ms = metadata.get("missedScheduledAt")
        if isinstance(occurrence_ms, bool) or not isinstance(occurrence_ms, int):
            raise RunContextError("missedScheduledAt must be an integer epoch")
        missed_delay_ms = created_at - occurrence_ms
        if missed_delay_ms < -MAX_FUTURE_SKEW_MS:
            raise RunContextError("missedScheduledAt is after the replay run")
        if missed_delay_ms > MAX_MISSED_DELAY_MS:
            raise RunContextError("missedScheduledAt exceeds the native replay window")
        context_source = "missedScheduledAt"
    elif run_kind == "scheduled":
        occurrence_ms = _scheduled_occurrence_ms(
            created_at,
            schedule_type=automation["schedule_type"],
            rrule=automation["rrule"],
        )
        context_source = "rrule_occurrence"
    else:
        raise RunContextError("runKind must be scheduled or missed")

    thread_id = row["thread_id"]
    if not isinstance(thread_id, str) or not thread_id.startswith(
        f"{automation_id}:"
    ):
        raise RunContextError("run thread id does not match automation")

    result = {
        "schema": "second-brain-run-context-v1",
        "status": "PASS",
        "automation_id": automation_id,
        "thread_id": thread_id,
        "selection": selection,
        "run_kind": run_kind,
        "target_date": _shanghai_date(occurrence_ms),
        "occurrence_ms": occurrence_ms,
        "created_at_ms": created_at,
        "age_ms": age_ms,
        "context_source": context_source,
        "read_only": True,
    }
    if runtime["running"] == 1:
        result["running_started_at_ms"] = runtime["running_started_at"]
        result["run_start_delta_ms"] = created_at - runtime["running_started_at"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--automation-id", required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--max-run-age-seconds",
        type=int,
        default=DEFAULT_MAX_RUN_AGE_MS // 1000,
    )
    args = parser.parse_args()
    try:
        result = resolve_run_context(
            args.db,
            automation_id=args.automation_id,
            max_run_age_ms=args.max_run_age_seconds * 1000,
        )
    except RunContextError as error:
        result = {
            "schema": "second-brain-run-context-v1",
            "status": "HOLD",
            "automation_id": args.automation_id,
            "error": str(error),
            "read_only": True,
        }
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
