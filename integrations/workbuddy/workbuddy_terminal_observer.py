"""Observe native FinalStop evidence; never authorize distillation or edit WorkBuddy DB."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import stat
import sys
import time
from pathlib import Path


ROOT = Path(os.environ.get("SECOND_BRAIN_WORKBUDDY_HOME", Path(__file__).resolve().parents[2] / ".local" / "workbuddy"))
DEFAULT_VAULT = Path(os.environ.get("SECOND_BRAIN_VAULT", Path(__file__).resolve().parents[2] / ".local" / "vault"))
DEFAULT_OBSERVATIONS = ROOT / "automations/terminal-observations"
SCHEMA = "second-brain-final-stop-observation-v2"
LEGACY_SCHEMA = "second-brain-final-stop-observation-v1"
spec = importlib.util.spec_from_file_location(
    "workbuddy_native_terminal_audit", Path(__file__).with_name("audit_second_brain_terminal.py")
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
MAX_AGE_MS = 30 * 60 * 1000


class ObservationError(RuntimeError):
    pass


def plain_path(path: Path) -> Path:
    raw = os.fspath(path)
    if raw.startswith("\\\\?\\UNC\\"):
        raw = "\\\\" + raw[8:]
    elif raw.startswith("\\\\?\\"):
        raw = raw[4:]
    path = Path(raw)
    path = Path(os.path.abspath(path))
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ObservationError("reparse_path_not_allowed")
    return path.resolve()


def _active_binding(db_path, session, vault, now_ms):
    path = plain_path(db_path)
    with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        rows = list(c.execute(
            "SELECT r.thread_id,r.automation_id,r.source_cwd,r.created_at,r.metadata_json,"
            "s.running,s.running_started_at FROM automation_runs r "
            "JOIN automation_runtime_state s ON s.automation_id=r.automation_id "
            "WHERE r.status='IN_PROGRESS' AND r.automation_id IN (?,?)",
            (audit.DAILY_ID, audit.WEEKLY_ID),
        ))
    matches = []
    for row in rows:
        metadata = audit._decode(row["metadata_json"] or "{}")
        if not isinstance(metadata, dict) or metadata.get("conversationId") != session:
            continue
        if "sessionId" in metadata and metadata["sessionId"] != session:
            raise ObservationError("database_session_mismatch")
        created, started = row["created_at"], row["running_started_at"]
        if (row["running"] != 1 or not isinstance(created, int) or not isinstance(started, int)
                or abs(created - started) > 5000 or not 0 <= now_ms - created <= MAX_AGE_MS):
            raise ObservationError("not_current_active_run")
        if not re.fullmatch(re.escape(row["automation_id"]) + r":\d+", row["thread_id"]):
            raise ObservationError("invalid_thread_identity")
        anchor = int(row["thread_id"].rsplit(":", 1)[1])
        if not 0 < anchor <= min(created, started) <= now_ms or now_ms - anchor > MAX_AGE_MS:
            raise ObservationError("not_current_active_run")
        if not row["source_cwd"] or plain_path(Path(row["source_cwd"])) != vault:
            raise ObservationError("run_workspace_mismatch")
        matches.append(dict(row))
    if len(matches) > 1:
        raise ObservationError("ambiguous_active_run")
    return matches[0] if matches else None


def observation_path(root, thread, request):
    if not re.fullmatch(r"automation-\d+:\d+", thread):
        raise ObservationError("invalid_thread_identity")
    if not isinstance(request, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", request):
        raise ObservationError("invalid_request_identity")
    name = thread.replace(":", "_") + "-" + hashlib.sha256(request.encode()).hexdigest() + ".json"
    return plain_path(root) / name


def _store(root, record):
    path = observation_path(root, record["thread_id"], record["request_id"])
    plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n").encode()
    try:
        # Exclusive creation keeps earlier captures immutable, including incomplete crash records.
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        existing = audit._decode(audit._read_bytes(path).decode("utf-8", errors="strict"))
        comparable = {key: value for key, value in record.items() if key != "observed_at_ms"}
        prior = {key: value for key, value in existing.items() if key != "observed_at_ms"}
        if prior != comparable:
            raise ObservationError("observation_replay_conflict")
        return existing
    return record


def capture(payload, *, request_id, db_path=ROOT / "workbuddy.db",
            projects_root=ROOT / "projects", observation_root=DEFAULT_OBSERVATIONS,
            vault=DEFAULT_VAULT, now_ms=None):
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "FinalStop":
        return {"evidence_status": "SKIPPED", "complete_repair_proven": False}
    vault = plain_path(vault)
    if not isinstance(payload.get("cwd"), str) or plain_path(Path(payload["cwd"])) != vault:
        return {"evidence_status": "SKIPPED", "complete_repair_proven": False}
    session = payload.get("session_id")
    if not isinstance(session, str) or not audit.SESSION_RE.fullmatch(session):
        raise ObservationError("invalid_session_identity")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    binding = _active_binding(db_path, session, vault, now_ms)
    if binding is None:
        return {"evidence_status": "SKIPPED", "complete_repair_proven": False}
    observation_path(observation_root, binding["thread_id"], request_id)
    record = {
        "schema": SCHEMA, "mode": "OBSERVATION_ONLY", "event": "FinalStop",
        "thread_id": binding["thread_id"], "automation_id": binding["automation_id"],
        "session_id": session, "request_id": request_id, "run_created_at": binding["created_at"],
        "run_identity_started_at": int(binding["thread_id"].rsplit(":", 1)[1]),
        "workspace": vault.as_posix(),
        "observed_at_ms": now_ms, "evidence_status": "NOT_VERIFIED",
        "complete_repair_proven": False,
    }
    path = raw = None
    try:
        if payload.get("final_stop_reason") != "completed":
            raise ObservationError("final_stop_not_completed")
        root = plain_path(projects_root)
        claimed = plain_path(Path(payload.get("transcript_path", "")))
        if not claimed.is_relative_to(root) or claimed.name != f"{session}.jsonl":
            raise ObservationError("transcript_path_mismatch")
        path, raw, answer = audit.read_terminal(root, session=session, request=request_id)
        plain_path(path)
        if claimed != path:
            raise ObservationError("transcript_path_mismatch")
        kind = "weekly" if binding["automation_id"] == audit.WEEKLY_ID else "daily"
        _, values = audit.validate_answer(answer, kind)
        if _active_binding(db_path, session, vault, now_ms) != binding or audit._read_bytes(path) != raw:
            raise ObservationError("evidence_changed_during_capture")
        record.update({
            "evidence_status": "CAPTURED", "declared_business_status": values["AUTOMATION_BUSINESS_STATUS"],
            "transcript_relative_path": path.relative_to(root).as_posix(),
            "transcript_sha256": hashlib.sha256(raw).hexdigest(),
            "terminal_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        })
    except (ObservationError, audit.TerminalAuditError) as error:
        record["error"] = str(error)
    return _store(observation_root, record)


def _read_identity(db_path, thread_id):
    with contextlib.closing(sqlite3.connect(plain_path(db_path).as_uri() + "?mode=ro", uri=True)) as c:
        c.execute("PRAGMA query_only=ON")
        row = c.execute("SELECT source_cwd,created_at FROM automation_runs WHERE thread_id=?",
                        (thread_id,)).fetchone()
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise ObservationError("run_workspace_missing")
    return plain_path(Path(row[0])).as_posix(), row[1]


def verify(thread_id, *, db_path=ROOT / "workbuddy.db", projects_root=ROOT / "projects",
           observation_root=DEFAULT_OBSERVATIONS):
    row = audit._read_row(db_path, thread_id)
    identity = _read_identity(db_path, thread_id)
    runs = audit._decode(row["runs_json"])
    if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict):
        raise ObservationError("unexpected_run_count")
    run = runs[0]
    result = run.get("runResult")
    request = result.get("requestId") if isinstance(result, dict) else None
    path = observation_path(observation_root, thread_id, request)
    plain_path(path)
    if not path.is_file():
        raise ObservationError("native_final_stop_observation_missing")
    raw = audit._read_bytes(path)
    record = audit._decode(raw.decode("utf-8", errors="strict"))
    if not isinstance(record, dict) or record.get("schema") not in {SCHEMA, LEGACY_SCHEMA}:
        raise ObservationError("observation_identity_or_evidence_mismatch")
    audited = audit.assess_run(db_path, projects_root, thread_id=thread_id)
    expected = {
        "schema": record["schema"], "mode": "OBSERVATION_ONLY", "event": "FinalStop",
        "thread_id": thread_id, "automation_id": row["automation_id"],
        "session_id": run.get("conversationId"), "request_id": request,
        "workspace": identity[0],
        "evidence_status": "CAPTURED", "complete_repair_proven": False,
        "declared_business_status": audited["declared_business_status"],
        "transcript_relative_path": audited["transcript_relative_path"],
        "transcript_sha256": audited["transcript_sha256"], "terminal_sha256": audited["terminal_sha256"],
    }
    if not isinstance(record, dict) or any(record.get(k) != v for k, v in expected.items()):
        raise ObservationError("observation_identity_or_evidence_mismatch")
    created = identity[1]
    observed = record.get("observed_at_ms")
    captured = record.get("run_created_at")
    clock_valid = (type(created) is int and type(observed) is int
                   and type(captured) is int and 0 <= observed - created <= MAX_AGE_MS)
    if record["schema"] == LEGACY_SCHEMA:
        clock_valid = clock_valid and captured == created
    else:
        # The native writer replaces the aggregate clock with the subrun clock at finish.
        anchor = int(thread_id.rsplit(":", 1)[1])
        clock_valid = (clock_valid and type(record.get("run_identity_started_at")) is int
                       and record["run_identity_started_at"] == anchor
                       and type(run.get("startedAt")) is int and run["startedAt"] == created
                       and 0 < anchor <= captured <= created <= observed
                       and observed - anchor <= MAX_AGE_MS)
    if not clock_valid:
        raise ObservationError("observation_run_time_mismatch")
    if (audit._read_row(db_path, thread_id) != row or _read_identity(db_path, thread_id) != identity
            or audit._read_bytes(path) != raw):
        raise ObservationError("evidence_changed_during_verification")
    return {**audited, "final_stop_observation_verified": True, "mode": "OBSERVATION_ONLY"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-thread")
    args = parser.parse_args()
    if args.verify_thread:
        try:
            result = verify(args.verify_thread)
            code = 0 if result["declared_business_status"] == "PASS" else 2
        except (ObservationError, audit.TerminalAuditError) as error:
            result = {"final_stop_observation_verified": False, "error": str(error)}
            code = 3 if str(error) == "automation_still_running" else 1
        except Exception as error:
            result, code = {"final_stop_observation_verified": False, "error_type": type(error).__name__}, 1
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        raise SystemExit(code)
    try:
        raw = sys.stdin.buffer.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ObservationError("hook_input_too_large")
        capture(audit._decode(raw.decode("utf-8", errors="strict")),
                request_id=os.environ.get("CODEBUDDY_CONVERSATION_REQUEST_ID"))
    except Exception as error:
        # Report only safe error types; never ask the model to continue or print input evidence.
        print("SECOND_BRAIN_FINAL_STOP_OBSERVER_ERROR:" + type(error).__name__, file=sys.stderr)
    print("{}")


if __name__ == "__main__":
    main()
