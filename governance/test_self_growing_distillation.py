from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("self_growing_distillation.py")
SPEC = importlib.util.spec_from_file_location("self_growing_distillation", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SelfGrowingDistillationTests(unittest.TestCase):
    def copy_health_support(self, skill_dir: Path) -> None:
        shutil.copy2(MODULE_PATH.with_name("health_gate_policy.py"), skill_dir / "health_gate_policy.py")
        (skill_dir / "health_gate_policy.json").write_text(json.dumps({
            "schema": "second-brain-health-policy-v1", "max_review_age_days": 45,
            "critical_cards": [{"path": "02_Knowledge/已有判断.md", "scope": "shared"}],
        }), encoding="utf-8")

    def make_vault(self, root: Path) -> Path:
        (root / ".obsidian").mkdir()
        (root / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
        for name in MODULE.MANAGED_ROOTS:
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / "02_Knowledge" / "已有判断.md").write_text(
            "---\ntitle: 已有判断\ntype: knowledge\nstatus: active\n---\n\n模型评估必须包含失败样例。\n",
            encoding="utf-8",
        )
        return root

    def sync_registry(self, vault: Path, now: str = "2099-01-08T12:00:00+08:00") -> dict[str, object]:
        return MODULE.run(
            MODULE.parse_args(
                ["--vault", str(vault), "--now", now, "--sync-review-registry"]
            )
        )

    def set_review_decision(self, vault: Path, digest: str, decision: str) -> None:
        registry_path = vault / MODULE.REVIEW_REGISTRY_RELATIVE
        entries = MODULE.load_review_registry(registry_path)
        entry = dict(entries[digest])
        entry["decision"] = decision
        if decision == "allowed":
            entry.update(
                {
                    "reviewer": "fixture-reviewer",
                    "reviewed_at": "2099-01-08T12:05:00+08:00",
                    "reason": "已确认只是测试夹具，不含有效凭据",
                    "allow_model_context": True,
                    "confirmation": MODULE.ALLOW_CONFIRMATION,
                }
            )
        elif decision in {"blocked", "revoked"}:
            entry.update(
                {
                    "reviewer": "fixture-reviewer",
                    "reviewed_at": "2099-01-08T12:05:00+08:00",
                    "reason": "人工决定保持隔离",
                    "allow_model_context": False,
                    "confirmation": "",
                }
            )
        else:
            entry.update({"allow_model_context": False, "confirmation": ""})
        entries[digest] = entry
        registry_path.write_text(
            MODULE.render_review_registry(entries, "2099-01-08T12:05:00+08:00"),
            encoding="utf-8",
            newline="\n",
        )

    def write_episode_ledger(self, vault: Path, records: list[dict[str, object]]) -> Path:
        def canonical(value: object) -> str:
            if isinstance(value, list):
                return "[" + ",".join(canonical(item) for item in value) + "]"
            if isinstance(value, dict):
                return "{" + ",".join(
                    json.dumps(key, ensure_ascii=False) + ":" + canonical(value[key])
                    for key in sorted(value)
                ) + "}"
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

        previous_hash = "GENESIS"
        prepared = []
        for sequence, source in enumerate(records, 1):
            record = dict(source)
            record["sequence"] = sequence
            record["previous_hash"] = previous_hash
            record.pop("record_hash", None)
            record_hash = hashlib.sha256(canonical(record).encode("utf-8")).hexdigest()
            record["record_hash"] = record_hash
            prepared.append(record)
            previous_hash = record_hash
        ledger_path = vault / MODULE.EPISODE_LEDGER_RELATIVE
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in prepared) + "\n",
            encoding="utf-8",
        )
        return ledger_path

    def test_first_scan_writes_report_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            (vault / "01_Sources" / "新资料.md").write_text(
                "---\ntitle: 新资料\nsource: local\n---\n\n模型评估新增了失败样例与成本边界。\n",
                encoding="utf-8",
            )
            args = MODULE.parse_args(
                ["--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--lookback-days", "30000"]
            )
            result = MODULE.run(args)
            self.assertGreaterEqual(result["changed_count"], 1)
            self.assertTrue(Path(result["report"]).is_file())
            state = json.loads((vault / MODULE.STATE_RELATIVE).read_text(encoding="utf-8"))
            self.assertIn("01_Sources/新资料.md", state["files"])

    def test_second_scan_only_recalls_content_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            first = MODULE.parse_args(
                ["--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--bootstrap"]
            )
            MODULE.run(first)
            (vault / "00_Inbox" / "增量.md").write_text("# 增量\n\n新的可验证主张。\n", encoding="utf-8")
            second = MODULE.parse_args(["--vault", str(vault), "--now", "2099-01-09T12:00:00+08:00"])
            result = MODULE.run(second)
            self.assertEqual(result["changed_count"], 1)
            report = Path(result["report"]).read_text(encoding="utf-8")
            self.assertIn("00_Inbox/增量.md", report)

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            args = MODULE.parse_args(["--vault", str(vault), "--dry-run", "--lookback-days", "30000"])
            with redirect_stdout(io.StringIO()):
                result = MODULE.run(args)
            self.assertTrue(result["dry_run"])
            self.assertFalse((vault / MODULE.STATE_RELATIVE).exists())

    def test_metadata_only_change_is_not_ranked_but_body_change_is(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source = vault / "01_Sources" / "metadata-only.md"
            relative = "01_Sources/metadata-only.md"
            initial = (
                "---\n"
                "title: Metadata example\n"
                "type: source\n"
                "status: active\n"
                "---\n\n"
                "A reusable evidence claim.\n"
            )
            source.write_text(initial, encoding="utf-8")
            MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--bootstrap"]
                )
            )
            state_path = vault / MODULE.STATE_RELATIVE
            state_before = state_path.read_bytes()

            metadata_only = initial.replace(
                "status: active\n", "status: active\nlast_reviewed: 2099-01-09\n"
            )
            source.write_text(metadata_only, encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                preview = MODULE.run(
                    MODULE.parse_args(
                        [
                            "--vault",
                            str(vault),
                            "--now",
                            "2099-01-09T12:00:00+08:00",
                            "--dry-run",
                        ]
                    )
                )

            self.assertEqual(preview["changed_count"], 1)
            self.assertEqual(preview["candidate_count"], 0)
            self.assertEqual(state_path.read_bytes(), state_before)
            baseline_state = json.loads(state_before.decode("utf-8"))
            baseline_candidate_value = baseline_state["candidate_files"][relative]

            applied = MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-09T12:00:00+08:00"]
                )
            )
            self.assertEqual(applied["changed_count"], 1)
            self.assertEqual(applied["candidate_count"], 0)
            applied_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(applied_state["candidate_files"][relative], baseline_candidate_value)
            self.assertEqual(
                applied_state["files"][relative], MODULE.sha256_bytes(source.read_bytes())
            )

            unchanged = MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-10T12:00:00+08:00"]
                )
            )
            self.assertEqual(unchanged["changed_count"], 0)
            self.assertEqual(unchanged["candidate_count"], 0)

            source.write_text(
                metadata_only.replace(
                    "A reusable evidence claim.", "A revised reusable evidence claim."
                ),
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                body_preview = MODULE.run(
                    MODULE.parse_args(
                        [
                            "--vault",
                            str(vault),
                            "--now",
                            "2099-01-11T12:00:00+08:00",
                            "--dry-run",
                        ]
                    )
                )
            self.assertEqual(body_preview["changed_count"], 1)
            self.assertEqual(body_preview["candidate_count"], 1)

    def test_legacy_state_conservatively_recalls_then_gains_body_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source = vault / "01_Sources" / "legacy-state.md"
            relative = "01_Sources/legacy-state.md"
            initial = "---\ntitle: Legacy state\nstatus: active\n---\n\nInitial claim.\n"
            source.write_text(initial, encoding="utf-8")
            MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--bootstrap"]
                )
            )

            state_path = vault / MODULE.STATE_RELATIVE
            legacy_state = json.loads(state_path.read_text(encoding="utf-8"))
            legacy_state.pop("candidate_files", None)
            state_path.write_text(
                json.dumps(legacy_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            source.write_text(initial.replace("Initial claim.", "Revised claim."), encoding="utf-8")

            migrated = MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-09T12:00:00+08:00"]
                )
            )
            self.assertEqual(migrated["changed_count"], 1)
            self.assertEqual(migrated["candidate_count"], 1)
            migrated_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn(relative, migrated_state["candidate_files"])

            source.write_text(
                source.read_text(encoding="utf-8").replace(
                    "status: active\n", "status: active\nlast_reviewed: 2099-01-10\n"
                ),
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                metadata_preview = MODULE.run(
                    MODULE.parse_args(
                        [
                            "--vault",
                            str(vault),
                            "--now",
                            "2099-01-10T12:00:00+08:00",
                            "--dry-run",
                        ]
                    )
                )
            self.assertEqual(metadata_preview["changed_count"], 1)
            self.assertEqual(metadata_preview["candidate_count"], 0)

    def test_crlf_frontmatter_does_not_leak_delimiter(self) -> None:
        frontmatter, body = MODULE.parse_frontmatter("---\r\ntitle: 测试\r\n---\r\n正文\r\n")
        self.assertEqual(frontmatter["title"], "测试")
        self.assertEqual(body, "正文\n")

    def test_explicit_wikilink_wins_over_body_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            linked = vault / "02_Knowledge" / "身份连续性.md"
            linked.write_text("---\ntitle: 身份连续性\n---\n\n主体身份。\n", encoding="utf-8")
            source = vault / "04_Outputs" / "产品决策.md"
            source.write_text(
                "---\ntitle: 长期伴侣产品\n---\n\n模型评估失败样例。关联 [[身份连续性]]。\n",
                encoding="utf-8",
            )
            notes = [MODULE.read_note(path, vault) for path in MODULE.iter_markdown(vault)]
            source_note = next(note for note in notes if note.relative == "04_Outputs/产品决策.md")
            related = MODULE.related_knowledge(source_note, MODULE.knowledge_notes(notes))
            self.assertEqual([item[0].title for item in related], ["身份连续性"])

    def test_feedback_changes_origin_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            (report_dir / "反馈.md").write_text(
                """<!-- distill-candidate
source_path: 01_Sources/接受.md
decision: accepted
decision_reason: 有新增量
-->
<!-- distill-candidate
source_path: 00_Inbox/拒绝.md
decision: rejected
decision_reason: 无来源
-->
""",
                encoding="utf-8",
            )
            decisions, signal = MODULE.parse_feedback(report_dir)
            self.assertEqual(decisions["01_Sources/接受.md"], "accepted")
            self.assertEqual(signal["01_Sources"], 2)
            self.assertEqual(signal["00_Inbox"], -2)

    def test_sensitive_feedback_report_cannot_change_ranking_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report_dir = Path(temporary)
            (report_dir / "隔离反馈.md").write_text(
                """<!-- distill-candidate
source_path: 01_Sources/不应接受.md
decision: accepted
decision_reason: fixture
-->
token=fixture-feedback-value
""",
                encoding="utf-8",
            )

            decisions, signal = MODULE.parse_feedback(report_dir)

            self.assertEqual(decisions, {})
            self.assertEqual(signal, {})

    def test_sensitive_source_is_quarantined_before_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--bootstrap"]
                )
            )
            source = vault / "01_Sources" / "含临时授权参数.md"
            source.write_text(
                "# 来源\n\n可公开的前文。\n"
                "下载地址：https://example.invalid/file?authcode=fixture-value\n"
                "Authorization: Bearer fixture-token\n"
                "可公开的后文。\n",
                encoding="utf-8",
            )
            note = MODULE.read_note(source, vault)
            self.assertEqual(note.sensitive_types, ("authcode", "authorization"))
            self.assertEqual(note.body, "")
            self.assertEqual(note.tokens, frozenset())
            self.assertEqual(MODULE.safe_excerpt(note), "（来源已由敏感资格门禁隔离，未提取正文。）")

            state_before = (vault / MODULE.STATE_RELATIVE).read_bytes()
            checked = MODULE.run(
                MODULE.parse_args(["--vault", str(vault), "--check-path", "01_Sources/含临时授权参数.md"])
            )
            self.assertEqual(checked["quarantined_count"], 1)
            self.assertFalse(checked["files"][0]["eligible"])
            self.assertNotIn("fixture-value", json.dumps(checked, ensure_ascii=False))
            self.assertEqual((vault / MODULE.STATE_RELATIVE).read_bytes(), state_before)

            safely_read = MODULE.run(
                MODULE.parse_args(["--vault", str(vault), "--read-path", "01_Sources/含临时授权参数.md"])
            )
            self.assertFalse(safely_read["files"][0]["eligible"])
            self.assertNotIn("content", safely_read["files"][0])
            self.assertNotIn("fixture-value", json.dumps(safely_read, ensure_ascii=False))

            result = MODULE.run(
                MODULE.parse_args(["--vault", str(vault), "--now", "2099-01-09T12:00:00+08:00"])
            )
            self.assertEqual(result["changed_count"], 1)
            self.assertEqual(result["quarantined_file_count"], 1)
            self.assertEqual(result["quarantined_hash_count"], 1)
            self.assertEqual(result["candidate_count"], 0)
            report = Path(result["report"]).read_text(encoding="utf-8")
            self.assertIn("`authcode`", report)
            self.assertIn("`authorization`", report)
            self.assertNotIn("fixture-value", report)
            self.assertNotIn("fixture-token", report)
            self.assertNotIn("可公开的前文", report)

    def test_sensitive_pattern_types_are_explicit(self) -> None:
        cases = {
            "binary-content": "clean-prefix\x00clean-suffix",
            "api-key": '"api_key": "fixture-api-value"',
            "client-secret": '"client_secret": "fixture-client-value"',
            "access-token": '"access_token": "fixture-access-value"',
            "refresh-token": '"refresh_token": "fixture-refresh-value"',
            "token": '"token": "fixture-token-value"',
            "authcode": '"authcode": "fixture-auth-value"',
            "authorization": '"Authorization": "Bearer fixture-bearer-value"',
            "password": '"password": "fixture-password-value"',
            "cookie": '"cookie" = "fixture-cookie-value"',
            "session": '"session_id": "fixture-session-value"',
            "private-key": "-----BEGIN RSA PRIVATE KEY-----",
        }
        for expected, line in cases.items():
            with self.subTest(expected=expected):
                self.assertIn(expected, MODULE.sensitive_types_for(line))

    def test_invalid_utf8_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source = vault / "01_Sources" / "编码异常.md"
            source.write_bytes(b"\xff\xfe\x00\x80")
            note = MODULE.read_note(source, vault)
            self.assertEqual(note.sensitive_types, ("decode-error",))
            self.assertEqual(note.body, "")
            self.assertEqual(note.tokens, frozenset())

    def test_safe_read_returns_clean_content_from_one_byte_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source = vault / "05_Review" / "Weekly" / "干净复盘.md"
            source.parent.mkdir(parents=True)
            clean_text = "# 干净复盘\n\n只包含可公开内容。\n"
            source.write_bytes(clean_text.encode("utf-8"))
            original_read_bytes = Path.read_bytes
            reads = 0

            def counted_read_bytes(path: Path) -> bytes:
                nonlocal reads
                if path == source:
                    reads += 1
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", counted_read_bytes):
                result = MODULE.run(
                    MODULE.parse_args(["--vault", str(vault), "--read-path", "05_Review/Weekly/干净复盘.md"])
                )

            self.assertEqual(reads, 1)
            self.assertEqual(result["eligible_count"], 1)
            self.assertEqual(result["files"][0]["content"], clean_text)
            self.assertFalse((vault / MODULE.STATE_RELATIVE).exists())

    def test_check_all_covers_review_tree_without_returning_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            weekly = vault / "05_Review" / "Weekly"
            weekly.mkdir(parents=True)
            (weekly / "干净.md").write_text("# 干净\n", encoding="utf-8")
            (weekly / "隔离.md").write_text('"token": "fixture-check-all-value"\n', encoding="utf-8")
            custom_import = vault / "自定义导入"
            custom_import.mkdir()
            (custom_import / "编码异常.md").write_bytes(b"\xff\xfe\x00\x80")

            result = MODULE.run(MODULE.parse_args(["--vault", str(vault), "--check-all"]))

            self.assertEqual(result["mode"], "gate-check-all")
            self.assertEqual(result["quarantined_count"], 2)
            self.assertEqual(result["quarantined_hash_count"], 2)
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("content", serialized)
            self.assertNotIn("fixture-check-all-value", serialized)

    def test_bootstrap_refuses_new_sensitive_source_without_writing_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            (vault / "01_Sources" / "隔离.md").write_text("authcode=fixture-bootstrap-value\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "拒绝 bootstrap"):
                MODULE.run(
                    MODULE.parse_args(
                        [
                            "--vault",
                            str(vault),
                            "--now",
                            "2099-01-08T12:00:00+08:00",
                            "--lookback-days",
                            "30000",
                            "--bootstrap",
                        ]
                    )
                )

            self.assertFalse((vault / MODULE.STATE_RELATIVE).exists())

    def test_bootstrap_refuses_untracked_sensitive_source_outside_lookback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--bootstrap"]
                )
            )
            state_before = (vault / MODULE.STATE_RELATIVE).read_bytes()
            source = vault / "01_Sources" / "旧时间敏感来源.md"
            source.write_text("authcode=fixture-old-value\n", encoding="utf-8")
            old_timestamp = 946684800  # 2000-01-01 UTC, valid on Windows and outside the 2099 lookback.
            os.utime(source, (old_timestamp, old_timestamp))

            with self.assertRaisesRegex(RuntimeError, "拒绝 bootstrap"):
                MODULE.run(
                    MODULE.parse_args(
                        ["--vault", str(vault), "--now", "2099-01-09T12:00:00+08:00", "--bootstrap"]
                    )
                )

            self.assertEqual((vault / MODULE.STATE_RELATIVE).read_bytes(), state_before)

    def test_health_check_does_not_emit_quarantined_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            skill_dir = vault / "06_Skills" / "KnowledgeIteration"
            skill_dir.mkdir(parents=True)
            shutil.copy2(MODULE_PATH, skill_dir / MODULE_PATH.name)
            health_script = MODULE_PATH.with_name("知识健康检查.ps1")
            shutil.copy2(health_script, skill_dir / health_script.name)
            self.copy_health_support(skill_dir)
            (vault / "01_Sources" / "隔离健康来源.md").write_text(
                "[[fixture-leak-target]]\ntoken=fixture-health-value\n",
                encoding="utf-8",
            )
            self.sync_registry(vault)

            result = subprocess.run(
                [
                    os.environ.get("SECOND_BRAIN_POWERSHELL", "pwsh"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(skill_dir / health_script.name),
                    "-Vault",
                    str(vault),
                    "-NoWrite",
                ],
                capture_output=True,
                check=False,
            )

            combined = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, combined.decode(errors="replace"))
            self.assertNotIn(b"fixture-leak-target", combined)
            self.assertNotIn(b"fixture-health-value", combined)

    def test_health_check_ignores_aliases_from_quarantined_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            skill_dir = vault / "06_Skills" / "KnowledgeIteration"
            skill_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(MODULE_PATH, skill_dir / MODULE_PATH.name)
            health_script = MODULE_PATH.with_name("知识健康检查.ps1")
            shutil.copy2(health_script, skill_dir / health_script.name)
            self.copy_health_support(skill_dir)
            (vault / "01_Sources" / "isolated.md").write_text(
                "---\naliases: [isolated-alias]\n---\ntoken=fixture-isolated-value\n",
                encoding="utf-8",
            )
            (vault / "01_Sources" / "clean.md").write_text(
                "---\naliases: [clean-alias]\n---\nSafe source.\n",
                encoding="utf-8",
            )
            (vault / "02_Knowledge" / "reference.md").write_text(
                "---\ntitle: reference\ntype: knowledge\nstatus: active\n"
                "confidence: 高\nupdated: 2099-01-01\nlast_reviewed: 2099-01-01\n"
                "source: fixture\n---\n[[isolated-alias]] [[clean-alias]]\n",
                encoding="utf-8",
            )
            script_path = str(skill_dir / health_script.name).replace("'", "''")
            vault_path = str(vault).replace("'", "''")
            result = subprocess.run(
                [os.environ.get("SECOND_BRAIN_POWERSHELL", "pwsh"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                 "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                 f"& '{script_path}' -Vault '{vault_path}' -NoWrite"],
                capture_output=True, encoding="utf-8", timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("断链 isolated-alias", result.stdout)
            self.assertNotIn("断链 clean-alias", result.stdout)
            self.assertNotIn("fixture-isolated-value", result.stdout + result.stderr)

    def test_health_check_fails_closed_on_changed_or_undecodable_bytes(self) -> None:
        for mode in ("changed", "decode"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                vault = self.make_vault(Path(temporary))
                skill_dir = vault / "06_Skills" / "KnowledgeIteration"
                skill_dir.mkdir(parents=True, exist_ok=True)
                health_script = MODULE_PATH.with_name("知识健康检查.ps1")
                shutil.copy2(health_script, skill_dir / health_script.name)
                self.copy_health_support(skill_dir)
                if mode == "decode":
                    (vault / "01_Sources" / "bad.md").write_bytes(b"\xff\xfe\x80")
                gate_fixture = (
                    "import hashlib,json,pathlib\n"
                    "root=pathlib.Path(__file__).resolve().parents[2]\n"
                    "files=[]\n"
                    "for p in root.rglob('*.md'):\n"
                    "    rel=p.relative_to(root).as_posix()\n"
                    "    if rel=='AGENTS.md': continue\n"
                    "    digest=hashlib.sha256(p.read_bytes()).hexdigest()\n"
                    + ("    digest='0'*64\n" if mode == "changed" else "")
                    + "    files.append({'path_key':hashlib.sha256(rel.encode()).hexdigest(),"
                    "'eligible':True,'sha256':digest,'sensitive_types':[]})\n"
                    "print(json.dumps({'mode':'gate-check-all','files':files,'quarantined_hash_count':0}))\n"
                )
                (skill_dir / MODULE_PATH.name).write_text(gate_fixture, encoding="utf-8")
                script_path = str(skill_dir / health_script.name).replace("'", "''")
                vault_path = str(vault).replace("'", "''")
                result = subprocess.run(
                    [os.environ.get("SECOND_BRAIN_POWERSHELL", "pwsh"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                     "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                     f"& '{script_path}' -Vault '{vault_path}' -NoWrite"],
                    capture_output=True, encoding="utf-8", timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("# rules", result.stdout + result.stderr)
                self.assertIn("发生变化" if mode == "changed" else "UTF-8", result.stderr)

    def test_health_check_reports_advisory_structure_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            skill_dir = vault / "06_Skills" / "KnowledgeIteration"
            skill_dir.mkdir(parents=True)
            shutil.copy2(MODULE_PATH, skill_dir / MODULE_PATH.name)
            health_script = MODULE_PATH.with_name("知识健康检查.ps1")
            shutil.copy2(health_script, skill_dir / health_script.name)
            self.copy_health_support(skill_dir)

            card = (
                "---\n"
                "title: {title}\n"
                "type: knowledge\n"
                "status: active\n"
                "confidence: 高\n"
                "updated: 2099-01-08\n"
                "last_reviewed: 2099-01-08\n"
                "source: fixture\n"
                "---\n\n"
                "# {title}\n\n"
                "## 当前认知\n\n测试认知。\n\n"
                "## 证据时间线\n\n- **2099-01-08**：测试证据。\n"
            )
            (vault / "02_Knowledge" / "知识甲.md").write_text(
                card.format(title="知识甲"), encoding="utf-8"
            )
            (vault / "02_Knowledge" / "知识乙.md").write_text(
                card.format(title="知识乙"), encoding="utf-8"
            )
            (vault / "04_Outputs" / "输出.md").write_text(
                "# 输出\n\n[[知识甲]] 与 [[知识乙]]。\n", encoding="utf-8"
            )
            self.sync_registry(vault)

            script_path = str(skill_dir / health_script.name).replace("'", "''")
            vault_path = str(vault).replace("'", "''")
            command = (
                "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
                f"& '{script_path}' -Vault '{vault_path}' -NoWrite"
            )
            result = subprocess.run(
                [os.environ.get("SECOND_BRAIN_POWERSHELL", "pwsh"), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True,
                check=False,
            )

            combined = result.stdout + result.stderr
            decoded = combined.decode("utf-8", errors="replace")
            self.assertEqual(result.returncode, 0, decoded)
            self.assertIn("缺少实质语义入链：1 / 3（33.3%）", decoded)
            self.assertIn("连接至少两张知识卡的输出：1 / 1（100.0%）", decoded)
            self.assertIn("缺少“当前认知”段落：1", decoded)
            self.assertIn("缺少“证据时间线”段落：1", decoded)
            self.assertNotIn("\x00", decoded)

    def test_health_check_accepts_existing_non_markdown_vault_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            skill_dir = vault / "06_Skills" / "KnowledgeIteration"
            skill_dir.mkdir(parents=True)
            shutil.copy2(MODULE_PATH, skill_dir / MODULE_PATH.name)
            health_script = MODULE_PATH.with_name("知识健康检查.ps1")
            shutil.copy2(health_script, skill_dir / health_script.name)
            self.copy_health_support(skill_dir)
            receipt = vault / "04_Outputs" / "health-fixture" / "receipt.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text("{}\n", encoding="utf-8")
            (vault / "02_Knowledge" / "JSON回执链接.md").write_text(
                "---\n"
                "title: JSON回执链接\n"
                "type: knowledge\n"
                "status: active\n"
                "confidence: 高\n"
                "updated: 2099-01-08\n"
                "last_reviewed: 2099-01-08\n"
                "source: fixture\n"
                "---\n\n"
                "[[04_Outputs/health-fixture/receipt.json]]\n",
                encoding="utf-8",
            )
            self.sync_registry(vault)

            result = subprocess.run(
                [
                    os.environ.get("SECOND_BRAIN_POWERSHELL", "pwsh"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(skill_dir / health_script.name),
                    "-Vault",
                    str(vault),
                    "-NoWrite",
                ],
                capture_output=True,
                check=False,
            )

            combined = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, combined.decode(errors="replace"))
            self.assertNotIn(b"04_Outputs/health-fixture/receipt.json", combined)

    def test_health_check_rejects_non_markdown_target_outside_vault(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault_root = root / "vault"
            vault_root.mkdir()
            vault = self.make_vault(vault_root)
            skill_dir = vault / "06_Skills" / "KnowledgeIteration"
            skill_dir.mkdir(parents=True)
            shutil.copy2(MODULE_PATH, skill_dir / MODULE_PATH.name)
            health_script = MODULE_PATH.with_name("知识健康检查.ps1")
            shutil.copy2(health_script, skill_dir / health_script.name)
            self.copy_health_support(skill_dir)
            (root / "outside.json").write_text("{}\n", encoding="utf-8")
            (vault / "02_Knowledge" / "越界回执链接.md").write_text(
                "---\n"
                "title: 越界回执链接\n"
                "type: knowledge\n"
                "status: active\n"
                "confidence: 高\n"
                "updated: 2099-01-08\n"
                "last_reviewed: 2099-01-08\n"
                "source: fixture\n"
                "---\n\n"
                "[[../outside.json]]\n",
                encoding="utf-8",
            )
            self.sync_registry(vault)

            result = subprocess.run(
                [
                    os.environ.get("SECOND_BRAIN_POWERSHELL", "pwsh"),
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(skill_dir / health_script.name),
                    "-Vault",
                    str(vault),
                    "-NoWrite",
                ],
                capture_output=True,
                check=False,
            )

            combined = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, combined.decode(errors="replace"))
            self.assertIn(b"../outside.json", combined)

    def test_check_path_rejects_unmanaged_path_without_echoing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault_root = root / "vault"
            vault_root.mkdir()
            vault = self.make_vault(vault_root)
            unmanaged = root / "authcode=fixture-path-value.md"
            unmanaged.write_text("not managed\n", encoding="utf-8")
            with self.assertRaises(RuntimeError) as raised:
                MODULE.run(MODULE.parse_args(["--vault", str(vault), "--check-path", str(unmanaged)]))
            self.assertNotIn("fixture-path-value", str(raised.exception))

    def test_duplicate_sensitive_content_has_one_quarantine_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--bootstrap"]
                )
            )
            duplicate = "# 临时链接\n\nauthcode=fixture-duplicate-value\n"
            (vault / "01_Sources" / "敏感来源甲.md").write_text(duplicate, encoding="utf-8")
            (vault / "01_Sources" / "敏感来源乙.md").write_text(duplicate, encoding="utf-8")

            result = MODULE.run(
                MODULE.parse_args(["--vault", str(vault), "--now", "2099-01-09T12:00:00+08:00"])
            )

            self.assertEqual(result["changed_count"], 2)
            self.assertEqual(result["quarantined_file_count"], 2)
            self.assertEqual(result["quarantined_hash_count"], 1)
            self.assertEqual(result["candidate_count"], 0)
            report = Path(result["report"]).read_text(encoding="utf-8")
            self.assertEqual(report.count("内容 SHA-256"), 1)
            self.assertIn("01_Sources/敏感来源甲.md", report)
            self.assertIn("01_Sources/敏感来源乙.md", report)
            self.assertNotIn("fixture-duplicate-value", report)

    def test_sensitive_filename_is_redacted_from_state_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source = vault / "01_Sources" / "authcode=fixture-path-value.md"
            source.write_text("token=fixture-body-value\n", encoding="utf-8")

            result = MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--lookback-days", "30000"]
                )
            )

            state_text = (vault / MODULE.STATE_RELATIVE).read_text(encoding="utf-8")
            report = Path(result["report"]).read_text(encoding="utf-8")
            self.assertNotIn("fixture-path-value", state_text)
            self.assertNotIn("fixture-path-value", report)
            self.assertNotIn("fixture-body-value", report)
            self.assertIn("[redacted-path:", state_text)
            self.assertIn("[redacted-path:", report)

    def test_review_registry_sync_creates_pending_without_source_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source = vault / "01_Sources" / "疑似授权资料.md"
            source_text = "# 测试来源\n\ntoken=fixture-registry-value\n"
            source.write_bytes(source_text.encode("utf-8"))
            digest = MODULE.sha256_bytes(source_text.encode("utf-8"))

            result = self.sync_registry(vault)

            registry_path = vault / MODULE.REVIEW_REGISTRY_RELATIVE
            registry_text = registry_path.read_text(encoding="utf-8")
            entries = MODULE.load_review_registry(registry_path)
            self.assertEqual(result["mode"], "review-registry-sync")
            self.assertEqual(result["entry_count"], 1)
            self.assertEqual(result["pending_count"], 1)
            self.assertIsNone(result["state"])
            self.assertFalse((vault / MODULE.STATE_RELATIVE).exists())
            self.assertEqual(entries[digest]["decision"], "pending")
            self.assertFalse(entries[digest]["allow_model_context"])
            self.assertIn(digest, registry_text)
            self.assertNotIn(source.name, registry_text)
            self.assertNotIn("fixture-registry-value", registry_text)

            checked = MODULE.run(
                MODULE.parse_args(["--vault", str(vault), "--check-path", str(source)])
            )
            self.assertFalse(checked["files"][0]["eligible"])
            self.assertFalse(checked["files"][0]["allowlisted"])

    def test_only_complete_allowed_record_releases_same_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source = vault / "01_Sources" / "测试凭据示例.md"
            source_text = "# 测试来源\n\ntoken=fixture-allowed-value\n"
            source.write_bytes(source_text.encode("utf-8"))
            digest = MODULE.sha256_bytes(source_text.encode("utf-8"))
            self.sync_registry(vault)
            self.set_review_decision(vault, digest, "allowed")

            result = MODULE.run(
                MODULE.parse_args(["--vault", str(vault), "--read-path", str(source)])
            )

            item = result["files"][0]
            self.assertTrue(item["eligible"])
            self.assertTrue(item["allowlisted"])
            self.assertEqual(item["content"], source_text)
            self.assertEqual(item["sensitive_types"], ["token"])
            self.assertFalse((vault / MODULE.STATE_RELATIVE).exists())
            registry_text = (vault / MODULE.REVIEW_REGISTRY_RELATIVE).read_text(encoding="utf-8")
            self.assertNotIn("fixture-allowed-value", registry_text)

    def test_allow_and_revoke_decisions_trigger_incremental_recall(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source = vault / "01_Sources" / "资格变化来源.md"
            source_text = "# 资格变化\n\ntoken=fixture-qualification-value\n"
            source.write_bytes(source_text.encode("utf-8"))
            digest = MODULE.sha256_bytes(source_text.encode("utf-8"))
            self.sync_registry(vault)

            pending_scan = MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-08T13:00:00+08:00", "--lookback-days", "30000"]
                )
            )
            self.assertEqual(pending_scan["quarantined_file_count"], 1)
            pending_state = json.loads((vault / MODULE.STATE_RELATIVE).read_text(encoding="utf-8"))
            self.assertEqual(pending_state["files"]["01_Sources/资格变化来源.md"], digest)

            self.set_review_decision(vault, digest, "allowed")
            allowed_scan = MODULE.run(
                MODULE.parse_args(["--vault", str(vault), "--now", "2099-01-09T13:00:00+08:00"])
            )
            self.assertEqual(allowed_scan["changed_count"], 1)
            self.assertEqual(allowed_scan["allowlisted_file_count"], 1)
            self.assertEqual(allowed_scan["quarantined_file_count"], 0)
            self.assertEqual(allowed_scan["candidate_count"], 1)
            allowed_state = json.loads((vault / MODULE.STATE_RELATIVE).read_text(encoding="utf-8"))
            self.assertEqual(allowed_state["files"]["01_Sources/资格变化来源.md"], f"{digest}:allowed")
            self.assertNotIn(
                "fixture-qualification-value",
                Path(allowed_scan["report"]).read_text(encoding="utf-8"),
            )

            self.set_review_decision(vault, digest, "revoked")
            revoked_scan = MODULE.run(
                MODULE.parse_args(["--vault", str(vault), "--now", "2099-01-10T13:00:00+08:00"])
            )
            self.assertEqual(revoked_scan["changed_count"], 1)
            self.assertEqual(revoked_scan["allowlisted_file_count"], 0)
            self.assertEqual(revoked_scan["quarantined_file_count"], 1)
            self.assertEqual(revoked_scan["candidate_count"], 0)

    def test_pending_blocked_and_revoked_never_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source = vault / "01_Sources" / "保持隔离.md"
            source_text = "token=fixture-denied-value\n"
            source.write_bytes(source_text.encode("utf-8"))
            digest = MODULE.sha256_bytes(source_text.encode("utf-8"))
            self.sync_registry(vault)

            for decision in ("pending", "blocked", "revoked"):
                with self.subTest(decision=decision):
                    self.set_review_decision(vault, digest, decision)
                    result = MODULE.run(
                        MODULE.parse_args(["--vault", str(vault), "--read-path", str(source)])
                    )
                    self.assertFalse(result["files"][0]["eligible"])
                    self.assertFalse(result["files"][0]["allowlisted"])
                    self.assertNotIn("content", result["files"][0])

    def test_incomplete_allowed_and_conflicting_record_stop_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source = vault / "01_Sources" / "不完整复核.md"
            source_text = "token=fixture-incomplete-value\n"
            source.write_bytes(source_text.encode("utf-8"))
            digest = MODULE.sha256_bytes(source_text.encode("utf-8"))
            self.sync_registry(vault)
            self.set_review_decision(vault, digest, "allowed")
            registry_path = vault / MODULE.REVIEW_REGISTRY_RELATIVE
            valid_text = registry_path.read_text(encoding="utf-8")

            incomplete = valid_text.replace('reviewer = "fixture-reviewer"', 'reviewer = ""', 1)
            registry_path.write_text(incomplete, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "放行依据不完整"):
                MODULE.run(MODULE.parse_args(["--vault", str(vault), "--check-path", str(source)]))

            conflicting = valid_text.replace(
                f'sha256 = "{digest}"\ndecision = "allowed"',
                f'sha256 = "{digest}"\ndecision = "blocked"',
                1,
            )
            registry_path.write_text(conflicting, encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "权限冲突"):
                MODULE.run(MODULE.parse_args(["--vault", str(vault), "--check-path", str(source)]))

    def test_content_hash_change_invalidates_and_sync_preserves_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source = vault / "01_Sources" / "会变化的来源.md"
            old_text = "token=fixture-old-reviewed-value\n"
            source.write_bytes(old_text.encode("utf-8"))
            old_digest = MODULE.sha256_bytes(old_text.encode("utf-8"))
            self.sync_registry(vault)
            self.set_review_decision(vault, old_digest, "allowed")

            new_text = "token=fixture-new-unreviewed-value\n"
            source.write_bytes(new_text.encode("utf-8"))
            new_digest = MODULE.sha256_bytes(new_text.encode("utf-8"))
            before_sync = MODULE.run(
                MODULE.parse_args(["--vault", str(vault), "--read-path", str(source)])
            )
            self.assertFalse(before_sync["files"][0]["eligible"])
            self.assertNotIn("content", before_sync["files"][0])

            self.sync_registry(vault, "2099-01-09T12:00:00+08:00")
            registry_path = vault / MODULE.REVIEW_REGISTRY_RELATIVE
            entries = MODULE.load_review_registry(registry_path)
            self.assertEqual(entries[old_digest]["decision"], "allowed")
            self.assertFalse(entries[old_digest]["present"])
            self.assertEqual(entries[old_digest]["file_count"], 0)
            self.assertEqual(entries[new_digest]["decision"], "pending")
            self.assertTrue(entries[new_digest]["present"])
            registry_text = registry_path.read_text(encoding="utf-8")
            self.assertNotIn("fixture-old-reviewed-value", registry_text)
            self.assertNotIn("fixture-new-unreviewed-value", registry_text)

    def test_registry_rejects_invalid_duplicate_and_malformed_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source_text = "token=fixture-registry-validation-value\n"
            (vault / "01_Sources" / "注册表校验.md").write_bytes(source_text.encode("utf-8"))
            self.sync_registry(vault)
            registry_path = vault / MODULE.REVIEW_REGISTRY_RELATIVE
            registry_text = registry_path.read_text(encoding="utf-8")
            digest = MODULE.sha256_bytes(source_text.encode("utf-8"))
            block = MODULE.REVIEW_BLOCK.search(registry_text)
            self.assertIsNotNone(block)

            invalid_sha = registry_text.replace(f'sha256 = "{digest}"', 'sha256 = "abc"', 1)
            with self.assertRaisesRegex(RuntimeError, "SHA-256 无效"):
                MODULE.parse_review_registry_text(invalid_sha)

            duplicate = registry_text.rstrip() + "\n\n" + block.group(0) + "\n"
            with self.assertRaisesRegex(RuntimeError, "SHA-256 重复"):
                MODULE.parse_review_registry_text(duplicate)

            malformed = registry_text.replace('decision = "pending"', "decision = [", 1)
            with self.assertRaisesRegex(RuntimeError, "格式损坏"):
                MODULE.parse_review_registry_text(malformed)

            conflicting_summary = registry_text.replace("pending_count: 1", "pending_count: 0", 1)
            with self.assertRaisesRegex(RuntimeError, "摘要与记录冲突"):
                MODULE.parse_review_registry_text(conflicting_summary)

    def test_binary_and_decode_error_records_cannot_be_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            binary = vault / "01_Sources" / "二进制内容.md"
            decode_error = vault / "01_Sources" / "解码失败.md"
            binary.write_bytes(b"prefix\x00suffix")
            decode_error.write_bytes(b"\xff\xfe\x80")
            self.sync_registry(vault)
            entries = MODULE.load_review_registry(vault / MODULE.REVIEW_REGISTRY_RELATIVE)

            for source in (binary, decode_error):
                digest = MODULE.sha256_bytes(source.read_bytes())
                pending_text = MODULE.render_review_registry(
                    {digest: entries[digest]}, "2099-01-08T12:05:00+08:00"
                )
                invalid_allowed = (
                    pending_text.replace('decision = "pending"', 'decision = "allowed"', 1)
                    .replace('reviewer = ""', 'reviewer = "fixture-reviewer"', 1)
                    .replace('reviewed_at = ""', 'reviewed_at = "2099-01-08T12:05:00+08:00"', 1)
                    .replace('reason = ""', 'reason = "fixture-reviewed"', 1)
                    .replace("allow_model_context = false", "allow_model_context = true", 1)
                    .replace('confirmation = ""', f'confirmation = "{MODULE.ALLOW_CONFIRMATION}"', 1)
                )
                (vault / MODULE.REVIEW_REGISTRY_RELATIVE).write_text(invalid_allowed, encoding="utf-8")
                with self.subTest(source=source.name):
                    with self.assertRaisesRegex(RuntimeError, "不可放行类型"):
                        MODULE.run(
                            MODULE.parse_args(["--vault", str(vault), "--check-path", str(source)])
                        )

    def test_sensitive_registry_stops_before_source_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            source = vault / "01_Sources" / "注册表先行校验.md"
            source.write_text("token=fixture-source-value\n", encoding="utf-8")
            self.sync_registry(vault)
            registry_path = vault / MODULE.REVIEW_REGISTRY_RELATIVE
            registry_path.write_text(
                registry_path.read_text(encoding="utf-8") + "\ntoken=fixture-registry-value\n",
                encoding="utf-8",
            )
            original_read_bytes = Path.read_bytes
            source_reads = 0

            def counted_read_bytes(path: Path) -> bytes:
                nonlocal source_reads
                if path == source:
                    source_reads += 1
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", counted_read_bytes):
                with self.assertRaisesRegex(RuntimeError, "命中敏感模式"):
                    MODULE.run(MODULE.parse_args(["--vault", str(vault), "--check-all"]))
            self.assertEqual(source_reads, 0)

    def test_duplicate_content_produces_one_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--bootstrap"]
                )
            )
            duplicate = "---\ntitle: 同一来源\nsource: local\n---\n\n同一份可复核证据。\n"
            (vault / "01_Sources" / "来源甲.md").write_text(duplicate, encoding="utf-8")
            (vault / "01_Sources" / "来源乙.md").write_text(duplicate, encoding="utf-8")

            result = MODULE.run(
                MODULE.parse_args(["--vault", str(vault), "--now", "2099-01-09T12:00:00+08:00"])
            )

            self.assertEqual(result["changed_count"], 2)
            self.assertEqual(result["candidate_count"], 1)
            report = Path(result["report"]).read_text(encoding="utf-8")
            self.assertEqual(report.count("source_hash:"), 1)

    def test_episode_ledger_generates_only_review_gated_lesson_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            records = [
                {
                    "schema": "second-brain-episode-v1",
                    "event_type": "task_finished",
                    "task_id": "episode-pending",
                    "recorded_at": "2099-01-08T02:00:00Z",
                    "payload": {
                        "outcome": "completed",
                        "verification": {
                            "status": "passed", "regressionPassed": True,
                            "receipt": {"artifactHash": "a" * 64, "runId": "run-a", "verifier": "pytest-a"},
                        },
                        "reflection": "first verified result",
                        "lesson_candidate": {
                            "errorSignature": "lesson-a",
                            "lesson": "keep the first lesson pending",
                        },
                        "source": "codex",
                    },
                },
                {
                    "schema": "second-brain-episode-v1",
                    "event_type": "task_finished",
                    "task_id": "episode-ready",
                    "recorded_at": "2099-01-08T03:00:00Z",
                    "payload": {
                        "outcome": "completed",
                        "verification": {
                            "status": "passed", "regressionPassed": True,
                            "receipt": {"artifactHash": "b" * 64, "runId": "run-b", "verifier": "pytest-b"},
                        },
                        "reflection": "second independent result",
                        "lesson_candidate": {
                            "errorSignature": "lesson-a",
                            "lesson": "keep the first lesson pending",
                        },
                        "source": "claude",
                    },
                },
            ]
            self.write_episode_ledger(vault, records)

            result = MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--lookback-days", "0"]
                )
            )

            self.assertEqual(result["episode_lesson_candidate_count"], 1)
            report = Path(result["report"]).read_text(encoding="utf-8")
            self.assertIn("经历账本经验候选", report)
            self.assertIn("lesson-a", report)
            self.assertIn("eligible_for_review", report)
            self.assertIn("不同验证回执数：2", report)
            self.assertIn("pending-review", report)
            self.assertNotIn("02_Knowledge", str(result["report"]))

    def test_unverified_episode_lesson_never_becomes_a_distillation_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            record = {
                "schema": "second-brain-episode-v1",
                "event_type": "task_finished",
                "task_id": "episode-unverified",
                "recorded_at": "2099-01-08T02:00:00Z",
                "payload": {
                    "outcome": "stopped",
                    "verification": {"status": "unverified", "regressionPassed": "not_run"},
                    "reflection": "hook only",
                    "lesson_candidate": {
                        "errorSignature": "lesson-unverified",
                        "lesson": "do not promote this",
                    },
                    "source": "codex-hook",
                },
            }
            self.write_episode_ledger(vault, [record])

            result = MODULE.run(
                MODULE.parse_args(
                    ["--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--lookback-days", "0"]
                )
            )

            self.assertEqual(result["episode_lesson_candidate_count"], 0)
            if result["report"]:
                report = Path(result["report"]).read_text(encoding="utf-8")
                self.assertNotIn("lesson-unverified", report)

    def test_source_labels_without_verification_receipts_do_not_promote_lessons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            records = []
            for task_id, source in (("labels-a", "codex"), ("labels-b", "claude")):
                records.append({
                    "schema": "second-brain-episode-v1",
                    "event_type": "task_finished",
                    "task_id": task_id,
                    "recorded_at": "2099-01-08T02:00:00Z",
                    "payload": {
                        "outcome": "completed",
                        "verification": {"status": "passed", "regressionPassed": True},
                        "reflection": "labels are not proof",
                        "lesson_candidate": {"errorSignature": "labels-only", "lesson": "require receipts"},
                        "source": source,
                    },
                })
            self.write_episode_ledger(vault, records)
            result = MODULE.run(MODULE.parse_args([
                "--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--lookback-days", "0",
            ]))
            self.assertEqual(result["episode_lesson_candidate_count"], 0)

    def test_python_verifies_node_ledger_with_fractional_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            runtime_path = MODULE_PATH.parents[1] / "packages" / "runtime" / "runtime.mjs"
            script = """
import { recordEvent } from %s;
await recordEvent({
  taskId: "cross-language-number",
  eventType: "metric_sample",
  event: {
    ratio: 0.1,
    small: 0.00001,
    negative: -12.375,
    exponentialInteger: 1e21,
    unsafeInteger: 9007199254740992,
  },
  vaultRoot: process.argv[1],
});
""" % json.dumps(runtime_path.resolve().as_uri())
            completed = subprocess.run(
                ["node", "--input-type=module", "-e", script, str(vault)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            records = MODULE.load_episode_records(vault)
            self.assertEqual(records[0]["payload"]["ratio"], 0.1)
            self.assertEqual(records[0]["payload"]["small"], 0.00001)
            self.assertEqual(records[0]["payload"]["exponentialInteger"], 1e21)
            self.assertEqual(records[0]["payload"]["unsafeInteger"], 9007199254740992)

    def test_tampered_episode_ledger_stops_before_distillation_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            vault = self.make_vault(Path(temporary))
            ledger_path = self.write_episode_ledger(
                vault,
                [
                    {
                        "schema": "second-brain-episode-v1",
                        "event_type": "task_finished",
                        "task_id": "tampered-episode",
                        "recorded_at": "2099-01-08T02:00:00Z",
                        "payload": {
                            "outcome": "completed",
                            "verification": {"status": "passed", "regressionPassed": True},
                            "reflection": "original",
                            "lesson_candidate": {
                                "errorSignature": "tampered-lesson",
                                "lesson": "must not compile",
                            },
                            "source": "codex",
                        },
                    }
                ],
            )
            text = ledger_path.read_text(encoding="utf-8").replace("original", "tampered")
            ledger_path.write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "经历账本.*(哈希|链)"):
                MODULE.run(
                    MODULE.parse_args(
                        ["--vault", str(vault), "--now", "2099-01-08T12:00:00+08:00", "--lookback-days", "0"]
                    )
                )
            self.assertFalse((vault / MODULE.EPISODE_DISTILL_STATE_RELATIVE).exists())


if __name__ == "__main__":
    unittest.main()
