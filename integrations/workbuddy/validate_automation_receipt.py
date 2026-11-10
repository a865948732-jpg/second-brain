import argparse
import base64
import json
import re
import sys


COMMON_STEPS = (
    "STEP_AUTHORIZATION",
    "STEP_MCP_GATE",
    "STEP_INPUTS",
    "STEP_CLASSIFICATION",
    "STEP_HEALTH_CHECK",
    "STEP_KNOWLEDGE_COMMIT",
    "STEP_REPORT_COMMIT",
    "STEP_REPORT_VERIFY",
)
WEEKLY_STEPS = (
    "STEP_DATE_GATE",
    "STEP_RECENT_SUNDAY_CHECK",
    "STEP_LEDGER_RECONCILE",
    "STEP_WEEKLY_HEALTH_CHECK",
    "STEP_WEEKLY_REPORT_COMMIT",
    "STEP_WEEKLY_REPORT_VERIFY",
)
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def extract_terminal_receipt(runs: list) -> str:
    if not isinstance(runs, list) or len(runs) != 1 or not isinstance(runs[0], dict):
        raise ValueError('unexpected_run_count')
    run = runs[0]
    result = run.get('runResult')
    if not isinstance(result, dict):
        raise ValueError('missing_structured_content')
    if 'messages' in result:
        messages = result['messages']
        if not isinstance(messages, list) or not messages or not isinstance(messages[-1], dict):
            raise ValueError('missing_final_assistant_message')
        terminal = messages[-1].get('message', messages[-1])
        if not isinstance(terminal, dict) or terminal.get('role') != 'assistant':
            raise ValueError('missing_final_assistant_message')
        content = terminal.get('content')
    else:
        content = result.get('content')
    if not isinstance(content, list) or not content:
        raise ValueError('missing_structured_content')
    final = content[-1]
    if not isinstance(final, dict) or final.get('type') != 'text':
        raise ValueError('final_event_not_text')
    text = final.get('text')
    if not isinstance(text, str) or not text:
        raise ValueError('missing_final_text')
    text = text.replace('\r', '').rstrip('\n')
    starts = [match.start() for match in re.finditer(r'(?m)^STEP_AUTHORIZATION=', text)]
    if not starts:
        raise ValueError('missing_final_receipt')
    receipt = text[starts[-1]:]
    output = run.get('output')
    if not isinstance(output, str) or not output:
        raise ValueError('missing_output')
    if not output.replace('\r', '').rstrip('\n').endswith(receipt):
        raise ValueError('receipt_not_output_suffix')
    return receipt


def fail(errors: list[str]) -> None:
    print(json.dumps({"valid": False, "errors": errors}, ensure_ascii=True))
    raise SystemExit(1)


def parse_receipt(encoded: str) -> str:
    try:
        raw = base64.b64decode(encoded, validate=True)
        text = raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        fail([f"invalid_base64_or_utf8:{type(exc).__name__}"])
    return text


def validate(text: str, kind: str) -> dict[str, str]:
    errors: list[str] = []
    if text.startswith("\ufeff"):
        errors.append("bom_not_allowed")
    if len(text) > 3000:
        errors.append(f"receipt_too_long:{len(text)}")
    if "```" in text:
        errors.append("markdown_fence_not_allowed")

    normalized = text.rstrip("\r\n")
    lines = normalized.splitlines()
    if not lines or not lines[0].startswith("STEP_AUTHORIZATION="):
        errors.append("first_line_must_be_STEP_AUTHORIZATION")

    values: dict[str, str] = {}
    ordered_keys: list[str] = []
    for index, line in enumerate(lines, start=1):
        if not line or "=" not in line:
            errors.append(f"non_machine_line:{index}")
            continue
        key, value = line.split("=", 1)
        if not KEY_RE.fullmatch(key):
            errors.append(f"invalid_key:{index}:{key}")
            continue
        if key in values:
            errors.append(f"duplicate_key:{key}")
        values[key] = value
        ordered_keys.append(key)
        if key.endswith("_EVIDENCE") and (not value or len(line) > 180):
            errors.append(f"invalid_evidence:{key}")

    required_steps = list(COMMON_STEPS)
    if kind == "weekly":
        required_steps.extend(WEEKLY_STEPS)
    for step in required_steps:
        evidence = f"{step}_EVIDENCE"
        if step not in values:
            errors.append(f"missing:{step}")
            continue
        if values[step] not in {"PASS", "FAIL", "NOT_RUN"}:
            errors.append(f"invalid_step_status:{step}")
        position = ordered_keys.index(step)
        if position + 1 >= len(ordered_keys) or ordered_keys[position + 1] != evidence:
            errors.append(f"evidence_not_adjacent:{step}")

    required_fields = {
        "ACTION",
        "AUTOMATION_BUSINESS_STATUS",
        "UNATTENDED_AUTHORIZATION",
        "RUN_KIND",
        "NO_INTERACTIVE_ASK",
    }
    if kind == "weekly":
        required_fields.update(
            {
                "CURRENT_LOCAL_DATE",
                "CURRENT_ISO_WEEKDAY",
                "DATE_GATE_EVIDENCE",
                "WEEKLY_STATUS",
            }
        )
    for field in required_fields:
        if field not in values:
            errors.append(f"missing:{field}")

    if values.get("STEP_AUTHORIZATION") not in {"PASS", "FAIL"}:
        errors.append("invalid_STEP_AUTHORIZATION")
    if values.get("ACTION") not in {"APPLY", "HOLD", "NOOP"}:
        errors.append("invalid_ACTION")
    if values.get("AUTOMATION_BUSINESS_STATUS") not in {"PASS", "HOLD", "FAIL"}:
        errors.append("invalid_AUTOMATION_BUSINESS_STATUS")
    if values.get("UNATTENDED_AUTHORIZATION") not in {"PASS", "FAIL"}:
        errors.append("invalid_UNATTENDED_AUTHORIZATION")
    if values.get("NO_INTERACTIVE_ASK") != "TRUE":
        errors.append("invalid_NO_INTERACTIVE_ASK")
    if not values.get("RUN_KIND"):
        errors.append("missing_run_kind")

    business_status = values.get("AUTOMATION_BUSINESS_STATUS")
    action = values.get("ACTION")
    if business_status == "PASS":
        if values.get("UNATTENDED_AUTHORIZATION") != "PASS":
            errors.append("business_PASS_requires_authorization_PASS")
        if action != "APPLY":
            errors.append("business_PASS_requires_ACTION_APPLY")
        for step in required_steps:
            if values.get(step) != "PASS":
                errors.append(f"business_PASS_requires_step_PASS:{step}")
        if kind == "weekly" and values.get("WEEKLY_STATUS") != "PASS":
            errors.append("business_PASS_requires_WEEKLY_STATUS_PASS")
    elif business_status in {"HOLD", "FAIL"} and action == "APPLY":
        errors.append("non_PASS_business_forbids_ACTION_APPLY")

    if kind == "weekly":
        if not DATE_RE.fullmatch(values.get("CURRENT_LOCAL_DATE", "")):
            errors.append("invalid_CURRENT_LOCAL_DATE")
        if values.get("CURRENT_ISO_WEEKDAY") not in set("1234567"):
            errors.append("invalid_CURRENT_ISO_WEEKDAY")
        if not values.get("DATE_GATE_EVIDENCE"):
            errors.append("empty_DATE_GATE_EVIDENCE")

    if values.get("ACTION") == "NOOP":
        if values.get("AUTOMATION_BUSINESS_STATUS") != "HOLD":
            errors.append("NOOP_requires_business_HOLD")
        if kind == "weekly" and values.get("WEEKLY_STATUS") != "HOLD":
            errors.append("weekly_NOOP_requires_WEEKLY_STATUS_HOLD")

    if errors:
        fail(errors)
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("daily", "weekly"), required=True)
    parser.add_argument("--text-b64", required=True)
    args = parser.parse_args()
    receipt = parse_receipt(args.text_b64)
    values = validate(receipt, args.kind)
    print(
        json.dumps(
            {
                "valid": True,
                "kind": args.kind,
                "action": values["ACTION"],
                "length": len(receipt),
                "line_count": len(receipt.rstrip("\r\n").splitlines()),
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
