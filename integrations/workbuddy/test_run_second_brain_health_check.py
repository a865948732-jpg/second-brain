import subprocess
import unittest

import run_second_brain_health_check as health


PASSING_REPORT = """---
title: test
---
- 知识卡：188
- 来源覆盖率：100%
- 孤立卡片：0（0%）
- 断链：0
- 超过 45 天未复核：0
- 争议卡片：3
- 问题总数：0
- 健康门策略：second-brain-health-policy-v1
- 健康门策略哈希：aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
- 硬阻断问题：0
- 普通逾期提醒：0
- 关键卡逾期：0
- 本轮依赖逾期：0
- 日期异常：0
- 本轮依赖卡：0
"""


class HealthCheckRunnerTests(unittest.TestCase):
    def test_ordinary_overdue_reminders_do_not_block_or_disappear(self):
        report = PASSING_REPORT.replace("问题总数：0", "问题总数：2").replace(
            "未复核：0", "未复核：2"
        ).replace("普通逾期提醒：0", "普通逾期提醒：2")
        result = health.run_health_check(
            phase="daily", runner=lambda *a, **k: subprocess.CompletedProcess(a[0], 0, report, "")
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["problem_count"], 2)
        self.assertEqual(result["maintenance_status"], "DUE")
        self.assertEqual(result["hard_problem_count"], 0)

    def test_parses_passing_report(self):
        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, PASSING_REPORT, "")

        result = health.run_health_check(phase="daily", runner=runner)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["knowledge_card_count"], 188)
        self.assertEqual(result["source_coverage_percent"], 100)
        self.assertEqual(result["problem_count"], 0)
        self.assertTrue(result["no_write"])

    def test_nonzero_problem_count_holds(self):
        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(
                args[0], 0, PASSING_REPORT.replace("问题总数：0", "问题总数：2"), ""
            )

        result = health.run_health_check(phase="weekly", runner=runner)

        self.assertEqual(result["status"], "HOLD")
        self.assertEqual(result["problem_count"], 2)

    def test_unparseable_output_holds(self):
        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 0, "incomplete", "")

        result = health.run_health_check(phase="daily", runner=runner)

        self.assertEqual(result["status"], "HOLD")
        self.assertFalse(result["parsed"])

    def test_missing_duplicate_or_contradictory_layer_metrics_hold(self):
        for report in (
            PASSING_REPORT.replace("- 硬阻断问题：0\n", ""),
            PASSING_REPORT + "- 硬阻断问题：0\n",
            PASSING_REPORT.replace("问题总数：0", "问题总数：1"),
            PASSING_REPORT.replace("普通逾期提醒：0", "普通逾期提醒：1"),
            PASSING_REPORT.replace("来源覆盖率：100%", "来源覆盖率：101%"),
            PASSING_REPORT.replace("来源覆盖率：100%", "来源覆盖率：90%"),
            PASSING_REPORT.replace("本轮依赖卡：0", "本轮依赖卡：1"),
        ):
            with self.subTest(report=report):
                result = health.run_health_check(phase="daily", runner=lambda *a, **k: subprocess.CompletedProcess(a[0], 0, report, ""))
                self.assertEqual(result["status"], "HOLD")

    def test_critical_dependency_and_date_faults_remain_hard(self):
        for key in ("关键卡逾期", "本轮依赖逾期", "日期异常"):
            with self.subTest(key=key):
                report = PASSING_REPORT.replace(key + "：0", key + "：1").replace(
                    "问题总数：0", "问题总数：1"
                ).replace("硬阻断问题：0", "硬阻断问题：1")
                if key != "日期异常":
                    report = report.replace("未复核：0", "未复核：1")
                result = health.run_health_check(phase="daily", runner=lambda *a, **k: subprocess.CompletedProcess(a[0], 0, report, ""))
                self.assertTrue(result["consistent"])
                self.assertEqual(result["status"], "HOLD")

    def test_shell_characters_are_quoted_and_not_executed(self):
        captured = {}
        def runner(command, **kwargs):
            captured["command"] = command
            return subprocess.CompletedProcess(command, 0, PASSING_REPORT.replace("本轮依赖卡：0", "本轮依赖卡：1"), "")
        health.run_health_check(phase="daily", dependencies=["02_Knowledge/name';$x.md"], runner=runner)
        self.assertIn("'02_Knowledge/name'';$x.md'", captured["command"][-1])
        for path in ("../outside.md", "02_Knowledge/../outside.md", "02_Knowledge/x\n.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                health.run_health_check(phase="daily", dependencies=[path], runner=runner)

    def test_invokes_no_write_script_with_hidden_window(self):
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return subprocess.CompletedProcess(command, 0, PASSING_REPORT, "")

        result = health.run_health_check(phase="weekly", runner=runner)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(captured["command"][0], str(health.POWERSHELL))
        self.assertIn("-NoWrite", captured["command"][-1])
        self.assertEqual(captured["kwargs"]["creationflags"], health._creation_flags())
        self.assertTrue(captured["kwargs"]["capture_output"])

    def test_nonzero_exit_holds_even_when_metrics_parse(self):
        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 7, PASSING_REPORT, "failure")

        result = health.run_health_check(phase="daily", runner=runner)

        self.assertEqual(result["status"], "HOLD")
        self.assertEqual(result["exit_code"], 7)
        self.assertTrue(result["parsed"])

    def test_script_failure_exposes_safe_reason_not_raw_stderr(self):
        diagnostic = '文件在资格检查后发生变化，健康检查停止'
        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args[0], 1, '', diagnostic + '\nsynthetic-secret-text')
        result = health.run_health_check(phase='daily', runner=runner)
        self.assertEqual(result['failure_reason'], 'qualification_hash_changed')
        self.assertNotIn('synthetic-secret-text', str(result))


if __name__ == "__main__":
    unittest.main()
