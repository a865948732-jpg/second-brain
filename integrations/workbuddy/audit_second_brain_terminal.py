"""Read-only native terminal evidence audit; never changes run or gate state."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
from pathlib import Path


ROOT = Path(os.environ.get("SECOND_BRAIN_WORKBUDDY_HOME", Path(__file__).resolve().parents[2] / ".local" / "workbuddy"))
DAILY_ID = os.environ.get("SECOND_BRAIN_WORKBUDDY_DAILY_ID", "automation-1000000000001")
WEEKLY_ID = os.environ.get("SECOND_BRAIN_WORKBUDDY_WEEKLY_ID", "automation-1000000000002")
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
SESSION_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class TerminalAuditError(RuntimeError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TerminalAuditError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_):
    raise TerminalAuditError("nonfinite_json_value")


def _decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except (json.JSONDecodeError, TypeError, RecursionError) as error:
        raise TerminalAuditError("malformed_json") from error


def _read_row(db_path: Path, thread_id: str):
    path = Path(db_path).resolve()
    try:
        with contextlib.closing(sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, timeout=2
        )) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT thread_id,automation_id,status,result_success,runs_json,"
                "metadata_json,updated_at FROM automation_runs WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise TerminalAuditError("run_not_found")
            if row["automation_id"] not in {DAILY_ID, WEEKLY_ID}:
                raise TerminalAuditError("run_outside_second_brain")
            if not re.fullmatch(re.escape(row["automation_id"]) + r":\d+", thread_id):
                raise TerminalAuditError("invalid_thread_identity")
            runtime = connection.execute(
                "SELECT running FROM automation_runtime_state WHERE automation_id=?",
                (row["automation_id"],),
            ).fetchone()
            if runtime is None:
                raise TerminalAuditError("runtime_state_missing")
            if runtime["running"] != 0 or row["status"] == "IN_PROGRESS":
                raise TerminalAuditError("automation_still_running")
            if row["status"] not in {"PENDING_REVIEW", "ACCEPTED"}:
                raise TerminalAuditError("unknown_run_status")
            return dict(row)
    except (OSError, sqlite3.Error) as error:
        raise TerminalAuditError("database_read_failed") from error


def _read_bytes(path: Path):
    try:
        if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise TerminalAuditError("transcript_too_large")
        with path.open("rb") as stream:
            raw = stream.read(MAX_TRANSCRIPT_BYTES + 1)
        if len(raw) > MAX_TRANSCRIPT_BYTES:
            raise TerminalAuditError("transcript_too_large")
        return raw
    except OSError as error:
        raise TerminalAuditError("transcript_read_failed") from error


def read_terminal(projects_root: Path, *, session: str, request: str):
    """Read native evidence independently of the host's tool-only run envelope."""
    if not isinstance(session, str) or not SESSION_RE.fullmatch(session):
        raise TerminalAuditError("invalid_session_identity")
    if not isinstance(request, str) or not request.strip():
        raise TerminalAuditError("missing_request_identity")
    root = Path(projects_root).resolve()
    matches = list(root.glob(f"*/{session}.jsonl"))
    if len(matches) != 1:
        raise TerminalAuditError("ambiguous_or_missing_transcript")
    path = matches[0].resolve()
    if not path.is_relative_to(root) or path.parent == root:
        raise TerminalAuditError("transcript_outside_projects")
    raw = _read_bytes(path)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise TerminalAuditError("invalid_transcript_utf8") from error
    records = [_decode(line) for line in text.splitlines() if line.strip()]
    if any(not isinstance(record, dict) for record in records):
        raise TerminalAuditError("invalid_transcript_record")
    assistants = [index for index, record in enumerate(records)
                  if record.get("type") == "message" and record.get("role") == "assistant"]
    if not assistants:
        raise TerminalAuditError("missing_assistant_terminal")
    index = assistants[-1]
    terminal = records[index]
    if terminal.get("status") != "completed":
        raise TerminalAuditError("assistant_not_completed")
    if terminal.get("sessionId") != session:
        raise TerminalAuditError("session_identity_mismatch")
    provider = terminal.get("providerData")
    if not isinstance(provider, dict) or provider.get("conversationRequestId") != request:
        raise TerminalAuditError("request_identity_mismatch")
    if any(record.get("type") != "file-history-snapshot" for record in records[index + 1:]):
        raise TerminalAuditError("events_after_terminal")
    content = terminal.get("content")
    if not isinstance(content, list) or not content or any(
        not isinstance(part, dict) or part.get("type") != "output_text"
        or not isinstance(part.get("text"), str) for part in content
    ):
        raise TerminalAuditError("terminal_content_not_text")
    answer = "".join(part["text"] for part in content)
    return path, raw, answer


def validate_answer(answer: str, kind: str):
    spec = importlib.util.spec_from_file_location(
        "terminal_receipt_validator", Path(__file__).with_name("validate_automation_receipt.py")
    )
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            values = validator.validate(answer, kind)
        except SystemExit as error:
            raise TerminalAuditError("invalid_machine_receipt") from error
    return validator, values


def assess_run(db_path: Path, projects_root: Path, *, thread_id: str) -> dict:
    row = _read_row(db_path, thread_id)
    runs = _decode(row["runs_json"])
    metadata = _decode(row["metadata_json"] or "{}")
    if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict):
        raise TerminalAuditError("unexpected_run_count")
    run = runs[0]
    session = run.get("conversationId")
    result = run.get("runResult")
    request = result.get("requestId") if isinstance(result, dict) else None
    if not isinstance(metadata, dict) or metadata.get("conversationId") != session:
        raise TerminalAuditError("database_session_mismatch")
    if "sessionId" in metadata and metadata["sessionId"] != session:
        raise TerminalAuditError("database_session_mismatch")
    output = run.get("output")
    if not isinstance(output, str) or not output:
        raise TerminalAuditError("missing_output")
    path, raw, answer = read_terminal(projects_root, session=session, request=request)
    if answer.rstrip("\r\n") != output.rstrip("\r\n"):
        raise TerminalAuditError("terminal_output_mismatch")

    kind = "weekly" if row["automation_id"] == WEEKLY_ID else "daily"
    # Validate native terminal text, never the unbound outer output or tool results.
    validator, values = validate_answer(answer, kind)
    try:
        validator.extract_terminal_receipt(runs)
        envelope = "available_not_installed_by_audit"
    except ValueError as error:
        envelope = str(error)

    if _read_row(db_path, thread_id) != row or _read_bytes(path) != raw:
        raise TerminalAuditError("evidence_changed_during_audit")
    return {
        "scope": "read-only-native-terminal-and-format",
        "thread_id": thread_id,
        "kind": kind,
        "terminal_verified": True,
        "receipt_valid": True,
        "declared_business_status": values["AUTOMATION_BUSINESS_STATUS"],
        "action": values["ACTION"],
        "steps": {key: values[key] for key in validator.COMMON_STEPS
                  + (validator.WEEKLY_STEPS if kind == "weekly" else ())},
        "stored_envelope_status": envelope,
        "outer_result_success": row["result_success"],
        "transcript_relative_path": path.relative_to(Path(projects_root).resolve()).as_posix(),
        "transcript_sha256": hashlib.sha256(raw).hexdigest(),
        "terminal_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
        "complete_repair_proven": False,
        "not_verified": ["artifact_readback", "commit_boundaries", "installed_host_gate",
                         "natural_schedule_acceptance"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "workbuddy.db")
    parser.add_argument("--projects", type=Path, default=ROOT / "projects")
    parser.add_argument("--thread-id", required=True)
    args = parser.parse_args()
    try:
        result = assess_run(args.db, args.projects, thread_id=args.thread_id)
        code = 0 if result["declared_business_status"] == "PASS" else 2
    except TerminalAuditError as error:
        result = {"terminal_verified": False, "error": str(error), "complete_repair_proven": False}
        code = 3 if str(error) == "automation_still_running" else 1
    except Exception as error:
        result = {"terminal_verified": False, "error_type": type(error).__name__,
                  "complete_repair_proven": False}
        code = 1
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
