"""Validate the explicit critical-card registry and run-local dependencies."""

import argparse
import hashlib
import json
import re
import stat
from pathlib import Path


SCHEMA = "second-brain-health-policy-v1"


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_policy_key")
        result[key] = value
    return result


def _safe_path(vault, value):
    if not isinstance(value, str) or not re.fullmatch(
        r"02_Knowledge/(?!.*(?:\.\.|[\\\x00-\x1f]))[^<>:*?\"|]+\.md", value
    ):
        raise ValueError("invalid_card_path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or part != part.strip() or part.endswith(".") for part in parts):
        raise ValueError("noncanonical_card_path")
    if parts[-1].lower() in {"readme.md", "_知识索引.md"}:
        raise ValueError("not_a_substantive_card")
    path = vault
    for part in parts:
        path /= part
        if path.exists() or path.is_symlink():
            attrs = path.lstat()
            if path.is_symlink() or getattr(attrs, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("reparse_card_path")
    if not path.resolve().is_relative_to(vault):
        raise ValueError("card_outside_vault")
    return value


def load_context(vault, *, phase, dependencies=(), stale_days=45):
    if phase not in {"daily", "weekly"}:
        raise ValueError("invalid_phase")
    vault = Path(vault).resolve(strict=True)
    raw = (vault / "06_Skills" / "KnowledgeIteration" / "health_gate_policy.json").read_bytes()
    policy = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object)
    if not isinstance(policy, dict) or set(policy) != {"schema", "max_review_age_days", "critical_cards"}:
        raise ValueError("invalid_policy_fields")
    if policy["schema"] != SCHEMA or type(policy["max_review_age_days"]) is not int or (
        policy["max_review_age_days"] != 45 or stale_days != 45
    ):
        raise ValueError("invalid_policy_version_or_age")
    cards = policy["critical_cards"]
    if not isinstance(cards, list) or not cards:
        raise ValueError("critical_registry_empty")
    critical = []
    seen = set()
    for card in cards:
        if not isinstance(card, dict) or set(card) != {"path", "scope"} or card["scope"] not in {"shared", "daily", "weekly"}:
            raise ValueError("invalid_critical_card")
        path = _safe_path(vault, card["path"])
        if path.casefold() in seen:
            raise ValueError("duplicate_critical_card")
        seen.add(path.casefold())
        if card["scope"] in {"shared", phase}:
            critical.append(path)
    if not critical:
        raise ValueError("critical_scope_empty")
    dependencies = sorted(set(_safe_path(vault, value) for value in dependencies))
    if len({path.casefold() for path in dependencies}) != len(dependencies):
        raise ValueError("ambiguous_dependency_case")
    return {
        "schema": SCHEMA, "sha256": hashlib.sha256(raw).hexdigest(),
        "vault": str(vault), "phase": phase,
        "critical_paths": critical, "dependency_paths": dependencies,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True)
    parser.add_argument("--phase", choices=("daily", "weekly"), required=True)
    parser.add_argument("--stale-days", type=int, default=45)
    parser.add_argument("--dependency", action="append", default=[])
    args = parser.parse_args()
    try:
        context = load_context(args.vault, phase=args.phase, dependencies=args.dependency, stale_days=args.stale_days)
    except Exception as error:
        print(json.dumps({"status": "HOLD", "error_type": type(error).__name__}))
        raise SystemExit(1)
    print(json.dumps(context, ensure_ascii=True))


if __name__ == "__main__":
    main()
