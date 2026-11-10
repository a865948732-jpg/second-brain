import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


POWERSHELL = os.environ.get("SECOND_BRAIN_POWERSHELL", "pwsh")
HEALTH_SCRIPT = Path(__file__).resolve().parents[2] / "governance/知识健康检查.ps1"
DEFAULT_VAULT = Path(os.environ.get("SECOND_BRAIN_VAULT", Path(__file__).resolve().parents[2] / ".local" / "vault"))
FIELD_PATTERNS = {
    "knowledge_card_count": r"(?m)^- 知识卡：\s*(\d+)\s*$",
    "source_coverage_percent": r"(?m)^- 来源覆盖率：\s*(\d+)%\s*$",
    "orphan_count": r"(?m)^- 孤立卡片：\s*(\d+)",
    "broken_link_count": r"(?m)^- 断链：\s*(\d+)\s*$",
    "overdue_count": r"(?m)^- 超过 45 天未复核：\s*(\d+)\s*$",
    "disputed_count": r"(?m)^- 争议卡片：\s*(\d+)\s*$",
    "problem_count": r"(?m)^- 问题总数：\s*(\d+)\s*$",
    "hard_problem_count": r"(?m)^- 硬阻断问题：\s*(\d+)\s*$",
    "maintenance_reminder_count": r"(?m)^- 普通逾期提醒：\s*(\d+)\s*$",
    "critical_overdue_count": r"(?m)^- 关键卡逾期：\s*(\d+)\s*$",
    "dependency_overdue_count": r"(?m)^- 本轮依赖逾期：\s*(\d+)\s*$",
    "invalid_date_count": r"(?m)^- 日期异常：\s*(\d+)\s*$",
    "dependency_count": r"(?m)^- 本轮依赖卡：\s*(\d+)\s*$",
}
SAFE_FAILURES = {
    '文件在资格检查后发生变化': 'qualification_hash_changed',
    '文件无法按 UTF-8 安全解码': 'utf8_decode_failed',
    '敏感源资格门禁脚本不存在': 'qualification_gate_missing',
    '敏感源资格门禁执行失败': 'qualification_gate_failed',
    '敏感源资格门禁返回无效 JSON': 'qualification_gate_invalid_json',
    '敏感源资格门禁结果不完整': 'qualification_gate_incomplete',
    '敏感源资格门禁缺少': 'qualification_gate_missing_field',
    '健康分层策略校验失败': 'health_policy_invalid',
    '关键或依赖卡不可用': 'critical_card_unavailable',
}


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run_health_check(*, phase: str, dependencies=(), runner=subprocess.run) -> dict:
    if phase not in {"daily", "weekly"}:
        raise ValueError("invalid_phase")
    dependencies = tuple(dependencies)
    for dependency in dependencies:
        if not isinstance(dependency, str) or not re.fullmatch(
            r"02_Knowledge/(?!.*(?:\.\.|[\\\x00-\x1f]))[^<>:*?\"|]+\.md", dependency
        ):
            raise ValueError("invalid_dependency_path")
    dependency_args = ""
    if dependencies:
        dependency_args = " -DependencyPaths @(" + ",".join(
            "'" + value.replace("'", "''") + "'" for value in dependencies
        ) + ")"
    command = (
        "[Console]::OutputEncoding = "
        "[System.Text.UTF8Encoding]::new($false); "
        f"& '{str(HEALTH_SCRIPT).replace(chr(39), chr(39) * 2)}' -Vault '{str(DEFAULT_VAULT).replace(chr(39), chr(39) * 2)}' -NoWrite -Phase {phase}{dependency_args}"
    )
    completed = runner(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=str(HEALTH_SCRIPT.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        creationflags=_creation_flags(),
    )
    metrics = {}
    for field, pattern in FIELD_PATTERNS.items():
        matches = re.findall(pattern, completed.stdout)
        metrics[field] = int(matches[0]) if len(matches) == 1 else None
    policy_versions = re.findall(r"(?m)^- 健康门策略：\s*(\S+)\s*$", completed.stdout)
    policy_hashes = re.findall(r"(?m)^- 健康门策略哈希：\s*([0-9a-f]{64})\s*$", completed.stdout)
    parsed = all(value is not None for value in metrics.values()) and (
        policy_versions == ["second-brain-health-policy-v1"] and len(policy_hashes) == 1
        and "\ufffd" not in completed.stdout
    )
    consistent = parsed and (
        metrics["problem_count"] == metrics["hard_problem_count"] + metrics["maintenance_reminder_count"]
        and metrics["overdue_count"] == metrics["maintenance_reminder_count"]
            + metrics["critical_overdue_count"] + metrics["dependency_overdue_count"]
        and metrics["hard_problem_count"] >= metrics["critical_overdue_count"]
            + metrics["dependency_overdue_count"] + metrics["invalid_date_count"]
        and metrics["dependency_count"] == len(set(dependencies))
        and 0 <= metrics["source_coverage_percent"] <= 100
        and metrics["overdue_count"] <= metrics["knowledge_card_count"]
        and metrics["orphan_count"] <= metrics["knowledge_card_count"]
        and metrics["disputed_count"] <= metrics["knowledge_card_count"]
        and (metrics["hard_problem_count"] > 0 or (
            metrics["source_coverage_percent"] == 100
            and metrics["orphan_count"] == 0 and metrics["broken_link_count"] == 0
        ))
    )
    passed = completed.returncode == 0 and not completed.stderr and consistent and metrics["hard_problem_count"] == 0
    reason = None
    if not passed:
        reason = ('script_nonzero_exit' if completed.returncode else
                  'metrics_unparseable' if not parsed else
                  'metrics_inconsistent' if not consistent else
                  'script_stderr_nonempty' if completed.stderr else 'hard_health_problem_count_nonzero')
        for marker, code in SAFE_FAILURES.items():
            if marker in completed.stderr:
                reason = code
                break
    return {
        "schema": "second-brain-health-check-v2",
        "phase": phase,
        "status": "PASS" if passed else "HOLD",
        "exit_code": completed.returncode,
        "script": str(HEALTH_SCRIPT),
        "no_write": True,
        "parsed": parsed,
        "consistent": bool(consistent),
        "policy_schema": policy_versions[0] if len(policy_versions) == 1 else None,
        "policy_sha256": policy_hashes[0] if len(policy_hashes) == 1 else None,
        "maintenance_status": "DUE" if parsed and metrics["maintenance_reminder_count"] else "NONE" if parsed else "UNKNOWN",
        "failure_reason": reason,
        "stderr_sha256": hashlib.sha256(completed.stderr.encode('utf-8')).hexdigest(),
        **metrics,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("daily", "weekly"), required=True)
    parser.add_argument("--dependency", action="append", default=[])
    args = parser.parse_args()
    try:
        result = run_health_check(phase=args.phase, dependencies=args.dependency)
    except Exception as error:
        result = {
            "schema": "second-brain-health-check-v2",
            "phase": args.phase,
            "status": "HOLD",
            "error_type": type(error).__name__,
        }
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
