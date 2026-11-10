import contextlib
import io
import unittest

import validate_automation_receipt as validator


COMMON_STEPS = list(validator.COMMON_STEPS)
WEEKLY_STEPS = list(validator.WEEKLY_STEPS)


def build_receipt(kind="daily", *, business="PASS", action="APPLY", overrides=None):
    values = {
        "AUTOMATION_BUSINESS_STATUS": business,
        "ACTION": action,
        "UNATTENDED_AUTHORIZATION": "PASS",
        "RUN_KIND": "scheduled",
        "NO_INTERACTIVE_ASK": "TRUE",
        "CURRENT_LOCAL_DATE": "2026-08-30",
        "CURRENT_ISO_WEEKDAY": "7",
        "DATE_GATE_EVIDENCE": "clock=2026-08-30;weekday=7",
        "WEEKLY_STATUS": "PASS",
    }
    values.update(overrides or {})
    steps = COMMON_STEPS + (WEEKLY_STEPS if kind == "weekly" else [])
    lines = []
    for step in steps:
        lines.append(f"{step}={values.get(step, 'PASS')}")
        lines.append(f"{step}_EVIDENCE={step.lower()}_verified")
    if kind == "weekly":
        lines.extend(
            [
                f"CURRENT_LOCAL_DATE={values['CURRENT_LOCAL_DATE']}",
                f"CURRENT_ISO_WEEKDAY={values['CURRENT_ISO_WEEKDAY']}",
                f"DATE_GATE_EVIDENCE={values['DATE_GATE_EVIDENCE']}",
                f"WEEKLY_STATUS={values['WEEKLY_STATUS']}",
            ]
        )
    lines.extend(
        [
            f"ACTION={values['ACTION']}",
            f"AUTOMATION_BUSINESS_STATUS={values['AUTOMATION_BUSINESS_STATUS']}",
            f"UNATTENDED_AUTHORIZATION={values['UNATTENDED_AUTHORIZATION']}",
            f"RUN_KIND={values['RUN_KIND']}",
            f"NO_INTERACTIVE_ASK={values['NO_INTERACTIVE_ASK']}",
        ]
    )
    return "\n".join(lines)


class ReceiptValidatorTests(unittest.TestCase):
    def assert_invalid(self, text, kind="daily"):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                validator.validate(text, kind)
        self.assertEqual(caught.exception.code, 1)

    def test_valid_daily_passes(self):
        values = validator.validate(build_receipt(), "daily")
        self.assertEqual(values["AUTOMATION_BUSINESS_STATUS"], "PASS")

    def test_valid_weekly_passes(self):
        self.assertIn("STEP_LEDGER_RECONCILE", validator.WEEKLY_STEPS)
        values = validator.validate(build_receipt("weekly"), "weekly")
        self.assertEqual(values["CURRENT_ISO_WEEKDAY"], "7")

    def test_weekly_missing_ledger_reconciliation_is_rejected(self):
        text = build_receipt("weekly").replace(
            "STEP_LEDGER_RECONCILE=PASS\n"
            "STEP_LEDGER_RECONCILE_EVIDENCE=step_ledger_reconcile_verified\n",
            "",
        )
        self.assert_invalid(text, "weekly")

    def test_business_pass_rejects_not_run_step(self):
        text = build_receipt(overrides={"STEP_CLASSIFICATION": "NOT_RUN"})
        self.assert_invalid(text)

    def test_prelude_is_rejected(self):
        self.assert_invalid("progress narration\n" + build_receipt())

    def test_missing_adjacent_evidence_is_rejected(self):
        text = build_receipt().replace(
            "STEP_INPUTS_EVIDENCE=step_inputs_verified\n", ""
        )
        self.assert_invalid(text)

    def test_weekly_noop_hold_is_structurally_valid(self):
        overrides = {step: "NOT_RUN" for step in COMMON_STEPS[1:] + WEEKLY_STEPS[2:]}
        overrides.update(
            {
                "AUTOMATION_BUSINESS_STATUS": "HOLD",
                "ACTION": "NOOP",
                "WEEKLY_STATUS": "HOLD",
                "RUN_KIND": "manual",
            }
        )
        values = validator.validate(
            build_receipt("weekly", business="HOLD", action="NOOP", overrides=overrides),
            "weekly",
        )
        self.assertEqual(values["ACTION"], "NOOP")


if __name__ == "__main__":
    unittest.main()
