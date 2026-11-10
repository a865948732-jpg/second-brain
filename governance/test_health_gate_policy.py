import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("health_gate_policy", ROOT / "health_gate_policy.py")
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class LayeredHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)
        self.skill = self.vault / "06_Skills" / "KnowledgeIteration"
        self.skill.mkdir(parents=True)
        (self.vault / "02_Knowledge").mkdir()
        self.manifest = self.skill / "health_gate_policy.json"
        self.manifest.write_text(json.dumps({
            "schema": "second-brain-health-policy-v1", "max_review_age_days": 45,
            "critical_cards": [{"path": "02_Knowledge/critical.md", "scope": "shared"}],
        }), encoding="utf-8")
        self.write_card("critical", date.today().isoformat())
        self.write_card("ordinary", (date.today() - timedelta(days=46)).isoformat())

    def write_card(self, name, reviewed, *, status="active", body=""):
        text = (f"---\ntitle: {name}\ntype: knowledge\nstatus: {status}\n"
                f"confidence: 高\nupdated: {date.today().isoformat()}\n"
                f"last_reviewed: {reviewed}\nsource: fixture\n---\n{body}\n")
        (self.vault / "02_Knowledge" / f"{name}.md").write_text(text, encoding="utf-8")

    def run_check(self, dependencies=(), *, vault_path=None):
        for name in ("知识健康检查.ps1", "health_gate_policy.py"):
            shutil.copy2(ROOT / name, self.skill / name)
        (self.vault / "index.md").write_text("[[critical]] [[ordinary]]\n", encoding="utf-8")
        (self.skill / "self_growing_distillation.py").write_text(
            "import hashlib,json,pathlib\n"
            "root=pathlib.Path(__file__).resolve().parents[2]\n"
            "files=[]\n"
            "for p in root.rglob('*.md'):\n"
            "    rel=p.relative_to(root).as_posix()\n"
            "    files.append({'path_key':hashlib.sha256(rel.encode()).hexdigest(),"
            "'eligible':True,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'sensitive_types':[]})\n"
            "print(json.dumps({'mode':'gate-check-all','files':files,'quarantined_hash_count':0}))\n",
            encoding="utf-8",
        )
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        command = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); & " + quote(
            self.skill / "知识健康检查.ps1"
        ) + " -Vault " + quote(self.vault if vault_path is None else vault_path) + " -NoWrite"
        if dependencies:
            command += " -DependencyPaths @(" + ",".join(map(quote, dependencies)) + ")"
        return subprocess.run(
            [os.environ.get("SECOND_BRAIN_POWERSHELL", "pwsh"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, encoding="utf-8", timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_ordinary_overdue_is_reminder_but_still_in_total(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("硬阻断问题：0", result.stdout)
        self.assertIn("普通逾期提醒：1", result.stdout)
        self.assertIn("问题总数：1", result.stdout)

    def test_vault_aliases_resolve_before_path_fingerprints(self):
        for alias in (self.vault / "02_Knowledge" / "..", str(self.vault) + os.sep):
            with self.subTest(vault_path=str(alias)):
                result = self.run_check(vault_path=alias)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("知识卡：2", result.stdout)
                self.assertIn("硬阻断问题：0", result.stdout)

    def test_critical_overdue_is_hard(self):
        self.write_card("critical", "2020-01-01")
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("硬阻断问题：1", result.stdout)
        self.assertIn("关键卡逾期：1", result.stdout)
        self.assertIn("问题总数：2", result.stdout)

    def test_used_ordinary_card_is_promoted_to_hard_dependency(self):
        result = self.run_check(["02_Knowledge/ordinary.md"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("普通逾期提醒：0", result.stdout)
        self.assertIn("本轮依赖逾期：1", result.stdout)
        self.assertIn("硬阻断问题：1", result.stdout)

    def test_critical_dependency_is_not_double_counted(self):
        self.write_card("critical", "2020-01-01")
        result = self.run_check(["02_Knowledge/critical.md"])
        self.assertIn("关键卡逾期：1", result.stdout)
        self.assertIn("本轮依赖逾期：0", result.stdout)
        self.assertIn("问题总数：2", result.stdout)

    def test_quoted_date_is_valid(self):
        self.write_card("ordinary", '"' + date.today().isoformat() + '"')
        result = self.run_check()
        self.assertIn("问题总数：0", result.stdout)

    def test_invalid_or_future_dates_are_hard(self):
        for value in ("not-a-date", "2026-02-30", "2099-01-01", "2026-01-01garbage", ""):
            with self.subTest(value=value):
                self.write_card("ordinary", value)
                result = self.run_check()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("日期异常：1", result.stdout)
                self.assertIn("硬阻断问题：1", result.stdout)

    def test_critical_draft_disputed_and_retired_hold(self):
        for status in ("draft", "disputed", "retired"):
            with self.subTest(status=status):
                self.write_card("critical", date.today().isoformat(), status=status)
                result = self.run_check()
                self.assertIn("硬阻断问题：1", result.stdout)

    def test_broken_link_is_not_downgraded(self):
        self.write_card("ordinary", "2020-01-01", body="[[missing]]")
        result = self.run_check()
        self.assertIn("断链：1", result.stdout)
        self.assertIn("硬阻断问题：1", result.stdout)
        self.assertIn("普通逾期提醒：1", result.stdout)

    def test_missing_required_card_fails_closed(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["critical_cards"][0]["path"] = "02_Knowledge/absent.md"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("关键或依赖卡不可用", result.stderr)

    def test_unknown_dependency_fails_closed(self):
        result = self.run_check(["02_Knowledge/unknown.md"])
        self.assertNotEqual(result.returncode, 0)

    def test_registry_or_dependency_cannot_escape_vault(self):
        for path in ("../outside.md", "02_Knowledge/../index.md", "C:/outside.md", "02_Knowledge//ordinary.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                POLICY.load_context(self.vault, phase="daily", dependencies=[path])

    def test_duplicate_and_unknown_policy_fields_rejected(self):
        for raw in ('{"schema":"x","schema":"x"}', '{"schema":"unknown"}'):
            with self.subTest(raw=raw):
                self.manifest.write_text(raw, encoding="utf-8")
                with self.assertRaises(ValueError):
                    POLICY.load_context(self.vault, phase="daily")

    def test_duplicate_review_date_is_hard(self):
        card = self.vault / "02_Knowledge/ordinary.md"
        card.write_text(card.read_text(encoding="utf-8").replace(
            "source: fixture", "last_reviewed: 2020-01-01\nsource: fixture"
        ), encoding="utf-8")
        result = self.run_check()
        self.assertIn("日期异常：1", result.stdout)
        self.assertIn("硬阻断问题：1", result.stdout)

    def test_missing_policy_and_unknown_scope_rejected(self):
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["critical_cards"][0]["scope"] = "unknown"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            POLICY.load_context(self.vault, phase="daily")
        with self.assertRaises(ValueError):
            POLICY.load_context(self.vault, phase="daily", stale_days=99999)

    def test_empty_critical_manifest_is_not_a_bypass(self):
        manifest = json.loads(self.manifest.read_text())
        manifest["critical_cards"] = []
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            POLICY.load_context(self.vault, phase="daily")

    def test_dependency_reparse_directory_rejected(self):
        link = self.vault / "02_Knowledge" / "linked"
        target = self.vault / "target"
        target.mkdir()
        if os.name == "nt":
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0)
        else:
            link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            POLICY.load_context(self.vault, phase="daily", dependencies=["02_Knowledge/linked/card.md"])


if __name__ == "__main__":
    unittest.main()
