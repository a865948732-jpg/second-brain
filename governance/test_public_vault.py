"""Exercise the published initializer and governance entry points together."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicVaultTests(unittest.TestCase):
    def test_fresh_template_passes_health_and_preview_without_writes(self):
        with tempfile.TemporaryDirectory(prefix="second-brain-public-") as temporary:
            vault = Path(temporary) / "示例 Vault"
            initialized = subprocess.run(
                ["node", str(ROOT / "scripts/init-vault.mjs"), "--path", str(vault)],
                capture_output=True, encoding="utf-8", timeout=10,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)

            def snapshot():
                return {path.relative_to(vault).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in vault.rglob("*") if path.is_file()}

            before = snapshot()
            preview = subprocess.run(
                [sys.executable, "-B", str(ROOT / "governance/self_growing_distillation.py"),
                 "--vault", str(vault), "--dry-run"],
                capture_output=True, encoding="utf-8", timeout=30,
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertTrue(json.loads(preview.stdout.strip().splitlines()[-1])["dry_run"])
            health = subprocess.run(
                [sys.executable, "-B", str(ROOT / "integrations/workbuddy/run_second_brain_health_check.py"),
                 "--phase", "daily"],
                env={**os.environ, "SECOND_BRAIN_VAULT": str(vault)},
                capture_output=True, encoding="utf-8", timeout=60,
            )
            self.assertEqual(health.returncode, 0, health.stderr)
            result = json.loads(health.stdout)
            self.assertEqual(result["status"], "PASS", result)
            self.assertTrue(result["parsed"] and result["consistent"] and result["no_write"])
            self.assertEqual(result["hard_problem_count"], 0)
            self.assertEqual(snapshot(), before)
