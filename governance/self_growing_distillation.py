#!/usr/bin/env python3
"""Build an incremental, human-gated distillation queue for an Obsidian vault."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


MANAGED_ROOTS = (
    "00_Inbox",
    "01_Sources",
    "02_Knowledge",
    "03_Topics",
    "04_Outputs",
    "05_Review/Daily",
)
EXCLUDED_PARTS = {".obsidian", ".tools", ".claude", ".claudian", ".git"}
STATE_RELATIVE = Path("05_Review") / "自生长蒸馏" / "state.json"
REPORT_DIR_RELATIVE = Path("05_Review") / "自生长蒸馏"
# The runtime episode ledger is deliberately kept outside the legacy file-state
# schema. Only verified, regression-backed lesson candidates cross this boundary.
EPISODE_LEDGER_RELATIVE = Path("05_Review") / "SecondBrainRuntime" / "episode-ledger.jsonl"
EPISODE_DISTILL_STATE_RELATIVE = REPORT_DIR_RELATIVE / "episode-distill-state.json"
MAX_SAFE_INTEGER = (2**53) - 1
REVIEW_REGISTRY_RELATIVE = REPORT_DIR_RELATIVE / "敏感源人工复核与放行清单.md"
CONTROL_FILES = {"AGENTS.md", REVIEW_REGISTRY_RELATIVE.as_posix()}
REVIEW_REGISTRY_MARKER = "<!-- sensitive-review-registry:v1 -->"
REVIEW_BLOCK_OPEN = "```sensitive-review"
REVIEW_BLOCK = re.compile(r"(?ms)^```sensitive-review[ \t]*\n(.*?)\n```[ \t]*$")
REVIEW_HEADING = re.compile(r"(?m)^## SHA-256 `([0-9a-f]{64})`[ \t]*$")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
REVIEW_DECISIONS = {"pending", "allowed", "blocked", "revoked"}
ALLOW_CONFIRMATION = "confirmed-no-live-credential"
NON_ALLOWLISTABLE_TYPES = {"binary-content", "decode-error"}
REVIEW_FIELDS = {
    "sha256",
    "decision",
    "reviewer",
    "reviewed_at",
    "reason",
    "allow_model_context",
    "confirmation",
    "sensitive_types",
    "path_keys",
    "first_seen",
    "last_seen",
    "present",
    "file_count",
}
SENSITIVE_PATTERNS = (
    ("binary-content", re.compile("\x00")),
    ("api-key", re.compile(r"(?i)\bapi[_ -]?key\b[\"']?\s*[:=]")),
    (
        "client-secret",
        re.compile(r"(?i)\b(?:client[_ -]?secret|secret[_ -]?key)\b[\"']?\s*[:=]"),
    ),
    ("access-token", re.compile(r"(?i)\baccess[_ -]?token\b[\"']?\s*[:=]")),
    ("refresh-token", re.compile(r"(?i)\brefresh[_ -]?token\b[\"']?\s*[:=]")),
    ("token", re.compile(r"(?i)(?<![a-z0-9_-])token\b[\"']?\s*[:=]")),
    ("authcode", re.compile(r"(?i)\bauthcode\b[\"']?\s*[:=]")),
    (
        "authorization",
        re.compile(r"(?i)\bauthorization\b[\"']?\s*[:=]|\bbearer\s+[a-z0-9._~+/=-]{8,}"),
    ),
    (
        "password",
        re.compile(r"(?i)\b(?:password|passwd)\b[\"']?\s*[:=]|密码[\"']?\s*[:=]"),
    ),
    ("cookie", re.compile(r"(?i)\bcookie\b[\"']?\s*[:=]")),
    ("session", re.compile(r"(?i)\bsession(?:[_ -]?id)?\b[\"']?\s*[:=]")),
    (
        "private-key",
        re.compile(
            r"(?i)\bprivate[_ -]?key\b[\"']?\s*[:=]|-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"
        ),
    ),
)
SENSITIVE_TYPE_NAMES = frozenset(name for name, _ in SENSITIVE_PATTERNS) | {"decode-error"}
ASCII_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9_.+-]{1,}")
CHINESE_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
FEEDBACK_BLOCK = re.compile(r"<!-- distill-candidate\s*(.*?)-->", re.S)


@dataclass(frozen=True)
class Note:
    path: Path
    relative: str
    digest: str
    modified: datetime
    frontmatter: dict[str, object]
    body: str
    tokens: frozenset[str]
    sensitive_types: tuple[str, ...]
    allowlisted: bool = False

    @property
    def quarantined(self) -> bool:
        return bool(self.sensitive_types) and not self.allowlisted

    @property
    def origin(self) -> str:
        return self.relative.split("/", 1)[0]

    @property
    def title(self) -> str:
        value = self.frontmatter.get("title")
        return str(value).strip('"\' ') if value else self.path.stem


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本机 Obsidian 增量知识蒸馏候选队列")
    parser.add_argument("--vault", type=Path, default=Path(os.environ.get("SECOND_BRAIN_VAULT", Path(__file__).resolve().parents[1] / ".local" / "vault")))
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--max-candidates", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="打印预览，不写报告或状态")
    parser.add_argument("--bootstrap", action="store_true", help="仅建立当前基线，不生成候选")
    parser.add_argument(
        "--check-path",
        action="append",
        type=Path,
        default=[],
        help="只检查指定 Vault 内 Markdown 的敏感资格；可重复，不读取状态或写文件",
    )
    parser.add_argument(
        "--read-path",
        action="append",
        type=Path,
        default=[],
        help="扫描并读取指定 Markdown；仅在同一批字节通过门禁时返回正文",
    )
    parser.add_argument(
        "--check-all",
        action="store_true",
        help="检查所有数据面 Markdown；供本地健康检查消费，不写文件",
    )
    parser.add_argument(
        "--sync-review-registry",
        action="store_true",
        help="同步按内容 SHA-256 记录的人工复核清单；不修改蒸馏状态",
    )
    parser.add_argument("--now", help="测试或复现用 ISO 时间；默认本机当前时间")
    return parser.parse_args(argv)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sensitive_types_for(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in SENSITIVE_PATTERNS if pattern.search(text))


def has_sensitive_line(text: str) -> bool:
    return bool(sensitive_types_for(text))


def valid_iso_datetime(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def validate_review_entry(entry: dict[str, object], index: int) -> dict[str, object]:
    if set(entry) != REVIEW_FIELDS:
        raise RuntimeError(f"人工复核清单第 {index} 项字段不完整或包含未知字段，门禁已停止")

    digest = entry["sha256"]
    decision = entry["decision"]
    string_fields = ("sha256", "decision", "reviewer", "reviewed_at", "reason", "confirmation")
    if any(not isinstance(entry[field], str) for field in string_fields):
        raise RuntimeError(f"人工复核清单第 {index} 项字段类型错误，门禁已停止")
    if not SHA256_PATTERN.fullmatch(str(digest)):
        raise RuntimeError(f"人工复核清单第 {index} 项 SHA-256 无效，门禁已停止")
    if decision not in REVIEW_DECISIONS:
        raise RuntimeError(f"人工复核清单第 {index} 项 decision 无效，门禁已停止")
    if not isinstance(entry["allow_model_context"], bool) or not isinstance(entry["present"], bool):
        raise RuntimeError(f"人工复核清单第 {index} 项布尔字段无效，门禁已停止")
    if not isinstance(entry["file_count"], int) or isinstance(entry["file_count"], bool):
        raise RuntimeError(f"人工复核清单第 {index} 项 file_count 无效，门禁已停止")
    if entry["file_count"] < 0:
        raise RuntimeError(f"人工复核清单第 {index} 项 file_count 无效，门禁已停止")

    sensitive_types = entry["sensitive_types"]
    path_keys = entry["path_keys"]
    if (
        not isinstance(sensitive_types, list)
        or not sensitive_types
        or any(not isinstance(item, str) or not item for item in sensitive_types)
        or any(item not in SENSITIVE_TYPE_NAMES for item in sensitive_types)
        or sensitive_types != sorted(set(sensitive_types))
    ):
        raise RuntimeError(f"人工复核清单第 {index} 项 sensitive_types 无效，门禁已停止")
    if (
        not isinstance(path_keys, list)
        or any(not isinstance(item, str) or not SHA256_PATTERN.fullmatch(item) for item in path_keys)
        or path_keys != sorted(set(path_keys))
    ):
        raise RuntimeError(f"人工复核清单第 {index} 项路径指纹无效，门禁已停止")
    if entry["present"] and (entry["file_count"] < 1 or len(path_keys) != entry["file_count"]):
        raise RuntimeError(f"人工复核清单第 {index} 项在库状态冲突，门禁已停止")
    if not entry["present"] and entry["file_count"] != 0:
        raise RuntimeError(f"人工复核清单第 {index} 项在库状态冲突，门禁已停止")

    for field in ("first_seen", "last_seen"):
        value = entry[field]
        if not isinstance(value, str) or not valid_iso_datetime(value):
            raise RuntimeError(f"人工复核清单第 {index} 项 {field} 无效，门禁已停止")
    reviewed_at = str(entry["reviewed_at"])
    if reviewed_at and not valid_iso_datetime(reviewed_at):
        raise RuntimeError(f"人工复核清单第 {index} 项 reviewed_at 无效，门禁已停止")

    if decision == "allowed":
        required_strings = ("reviewer", "reviewed_at", "reason")
        if any(not str(entry[field]).strip() for field in required_strings):
            raise RuntimeError(f"人工复核清单第 {index} 项放行依据不完整，门禁已停止")
        if entry["allow_model_context"] is not True or entry["confirmation"] != ALLOW_CONFIRMATION:
            raise RuntimeError(f"人工复核清单第 {index} 项放行确认不完整，门禁已停止")
        if NON_ALLOWLISTABLE_TYPES.intersection(sensitive_types):
            raise RuntimeError(f"人工复核清单第 {index} 项属于不可放行类型，门禁已停止")
    else:
        if entry["allow_model_context"] is not False or entry["confirmation"]:
            raise RuntimeError(f"人工复核清单第 {index} 项决定与模型上下文权限冲突，门禁已停止")
        if decision in {"blocked", "revoked"}:
            required_strings = ("reviewer", "reviewed_at", "reason")
            if any(not str(entry[field]).strip() for field in required_strings):
                raise RuntimeError(f"人工复核清单第 {index} 项处置依据不完整，门禁已停止")

    return entry


def parse_review_registry_text(text: str) -> dict[str, dict[str, object]]:
    if sensitive_types_for(text):
        raise RuntimeError("人工复核清单命中敏感模式，门禁已停止")
    if text.count(REVIEW_REGISTRY_MARKER) != 1:
        raise RuntimeError("人工复核清单缺少唯一版本标记，门禁已停止")
    blocks = REVIEW_BLOCK.findall(text.replace("\r\n", "\n"))
    if len(blocks) != text.count(REVIEW_BLOCK_OPEN):
        raise RuntimeError("人工复核清单结构损坏，门禁已停止")

    entries: dict[str, dict[str, object]] = {}
    for index, block in enumerate(blocks, 1):
        try:
            raw_entry = tomllib.loads(block)
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeError(f"人工复核清单第 {index} 项格式损坏，门禁已停止") from exc
        entry = validate_review_entry(raw_entry, index)
        digest = str(entry["sha256"])
        if digest in entries:
            raise RuntimeError(f"人工复核清单第 {index} 项 SHA-256 重复，门禁已停止")
        entries[digest] = entry

    headings = REVIEW_HEADING.findall(text)
    if len(headings) != text.count("## SHA-256 `") or headings != list(entries):
        raise RuntimeError("人工复核清单标题与记录冲突，门禁已停止")

    frontmatter, _ = parse_frontmatter(text)
    expected_summary = {
        "title": "敏感源人工复核与放行清单",
        "type": "sensitive-source-review-registry",
        "status": "active",
        "schema_version": "1",
        "entry_count": str(len(entries)),
        "present_hash_count": str(sum(bool(entry["present"]) for entry in entries.values())),
        "pending_count": str(sum(entry["decision"] == "pending" for entry in entries.values())),
        "allowed_count": str(sum(entry["decision"] == "allowed" for entry in entries.values())),
        "blocked_count": str(sum(entry["decision"] == "blocked" for entry in entries.values())),
        "revoked_count": str(sum(entry["decision"] == "revoked" for entry in entries.values())),
    }
    if any(str(frontmatter.get(field, "")) != value for field, value in expected_summary.items()):
        raise RuntimeError("人工复核清单摘要与记录冲突，门禁已停止")
    updated = frontmatter.get("updated")
    if not isinstance(updated, str) or not valid_iso_datetime(updated):
        raise RuntimeError("人工复核清单更新时间无效，门禁已停止")
    return entries


def load_review_registry(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError("人工复核清单不可按 UTF-8 安全读取，门禁已停止") from exc
    return parse_review_registry_text(text)


def allowed_hashes_for(entries: dict[str, dict[str, object]]) -> frozenset[str]:
    return frozenset(
        digest
        for digest, entry in entries.items()
        if entry["decision"] == "allowed"
        and bool(entry["allow_model_context"])
        and entry["confirmation"] == ALLOW_CONFIRMATION
        and bool(str(entry["reviewer"]).strip())
        and bool(str(entry["reviewed_at"]).strip())
        and bool(str(entry["reason"]).strip())
    )


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    normalized = text.replace("\r\n", "\n")
    match = FRONTMATTER.match(normalized)
    if not match:
        return {}, normalized

    data: dict[str, object] = {}
    current_list: str | None = None
    for raw_line in match.group(1).splitlines():
        list_match = re.match(r"^\s+-\s+(.*?)\s*$", raw_line)
        if list_match and current_list:
            values = data.setdefault(current_list, [])
            if isinstance(values, list):
                values.append(list_match.group(1).strip('"\''))
            continue
        field_match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", raw_line)
        if not field_match:
            current_list = None
            continue
        key, value = field_match.groups()
        if not value:
            data[key] = []
            current_list = key
        elif value.startswith("[") and value.endswith("]"):
            data[key] = [part.strip(' "\'') for part in value[1:-1].split(",") if part.strip()]
            current_list = None
        else:
            data[key] = value.strip('"\'')
            current_list = None
    return data, normalized[match.end() :]


def tokens_for(text: str) -> frozenset[str]:
    lowered = text.lower()
    tokens = {word.group(0) for word in ASCII_WORD.finditer(lowered)}
    for run in CHINESE_RUN.findall(lowered):
        tokens.add(run)
        tokens.update(run[index : index + 2] for index in range(len(run) - 1))
    stop = {"一个", "以及", "进行", "可以", "需要", "当前", "知识", "来源", "内容", "文件"}
    return frozenset(token for token in tokens if token not in stop)


def inspect_note(
    path: Path,
    vault: Path,
    allowed_hashes: frozenset[str] = frozenset(),
) -> tuple[Note, str | None]:
    raw = path.read_bytes()
    relative = path.relative_to(vault).as_posix()
    modified = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    digest = sha256_bytes(raw)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return (
            Note(
                path=path,
                relative=relative,
                digest=digest,
                modified=modified,
                frontmatter={},
                body="",
                tokens=frozenset(),
                sensitive_types=("decode-error",),
            ),
            None,
        )

    sensitive_types = sensitive_types_for(text)
    allowlisted = bool(sensitive_types) and digest in allowed_hashes and not (
        NON_ALLOWLISTABLE_TYPES.intersection(sensitive_types)
    )
    if sensitive_types and not allowlisted:
        return (
            Note(
                path=path,
                relative=relative,
                digest=digest,
                modified=modified,
                frontmatter={},
                body="",
                tokens=frozenset(),
                sensitive_types=sensitive_types,
            ),
            None,
        )

    frontmatter, body = parse_frontmatter(text)
    title = str(frontmatter.get("title") or path.stem)
    tags = frontmatter.get("tags", [])
    tag_text = " ".join(tags) if isinstance(tags, list) else str(tags)
    return (
        Note(
            path=path,
            relative=relative,
            digest=digest,
            modified=modified,
            frontmatter=frontmatter,
            body=body,
            tokens=tokens_for(f"{title} {tag_text} {body[:4000]}"),
            sensitive_types=sensitive_types,
            allowlisted=allowlisted,
        ),
        text,
    )


def read_note(path: Path, vault: Path, allowed_hashes: frozenset[str] = frozenset()) -> Note:
    return inspect_note(path, vault, allowed_hashes)[0]


def iter_markdown(vault: Path) -> Iterable[Path]:
    for root_name in MANAGED_ROOTS:
        root = vault / Path(root_name)
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            if not any(part in EXCLUDED_PARTS for part in path.parts):
                yield path


def iter_gate_markdown(vault: Path) -> Iterable[Path]:
    for path in vault.rglob("*.md"):
        relative = path.relative_to(vault).as_posix()
        if relative in CONTROL_FILES:
            continue
        if not any(part in EXCLUDED_PARTS for part in path.parts):
            yield path


def is_gate_markdown(path: Path, vault: Path) -> bool:
    if path.suffix.lower() != ".md" or any(part in EXCLUDED_PARTS for part in path.parts):
        return False
    if not path.is_relative_to(vault):
        return False
    return path.relative_to(vault).as_posix() not in CONTROL_FILES


def path_key_for(relative: str) -> str:
    return sha256_bytes(relative.encode("utf-8"))


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_string_array(values: Iterable[str]) -> str:
    return "[" + ", ".join(toml_string(value) for value in values) + "]"


def new_review_entry(
    digest: str,
    sensitive_types: Iterable[str],
    path_keys: Iterable[str],
    seen_at: str,
) -> dict[str, object]:
    keys = sorted(set(path_keys))
    return {
        "sha256": digest,
        "decision": "pending",
        "reviewer": "",
        "reviewed_at": "",
        "reason": "",
        "allow_model_context": False,
        "confirmation": "",
        "sensitive_types": sorted(set(sensitive_types)),
        "path_keys": keys,
        "first_seen": seen_at,
        "last_seen": seen_at,
        "present": True,
        "file_count": len(keys),
    }


def render_review_registry(entries: dict[str, dict[str, object]], updated_at: str) -> str:
    present_count = sum(bool(entry["present"]) for entry in entries.values())
    pending_count = sum(entry["decision"] == "pending" for entry in entries.values())
    allowed_count = sum(entry["decision"] == "allowed" for entry in entries.values())
    blocked_count = sum(entry["decision"] == "blocked" for entry in entries.values())
    revoked_count = sum(entry["decision"] == "revoked" for entry in entries.values())
    lines = [
        "---",
        "title: 敏感源人工复核与放行清单",
        "type: sensitive-source-review-registry",
        "status: active",
        "schema_version: 1",
        f"updated: {updated_at[:10]}",
        f"entry_count: {len(entries)}",
        f"present_hash_count: {present_count}",
        f"pending_count: {pending_count}",
        f"allowed_count: {allowed_count}",
        f"blocked_count: {blocked_count}",
        f"revoked_count: {revoked_count}",
        "tags:",
        "  - 自生长蒸馏",
        "  - 敏感源复核",
        "  - 安全门禁",
        "---",
        "",
        "# 敏感源人工复核与放行清单",
        "",
        "> 本清单只记录内容 SHA-256、命中类型和路径指纹，不保存正文、命中行或命中值。默认全部隔离。",
        "",
        "只有同时填写 `decision = \"allowed\"`、复核人、合法复核时间、理由、`allow_model_context = true`，",
        f"并填写固定确认语 `{ALLOW_CONFIRMATION}`，对应内容哈希才会被放行。内容变化会生成新哈希并自动失效。",
        "`binary-content` 与 `decode-error` 不可放行。修改机器字段、重复哈希或破坏结构会使门禁停止。",
        "",
        REVIEW_REGISTRY_MARKER,
        "",
    ]
    field_order = (
        "sha256",
        "decision",
        "reviewer",
        "reviewed_at",
        "reason",
        "allow_model_context",
        "confirmation",
        "sensitive_types",
        "path_keys",
        "first_seen",
        "last_seen",
        "present",
        "file_count",
    )
    for digest in sorted(entries):
        entry = validate_review_entry(entries[digest], len(lines))
        lines.extend([f"## SHA-256 `{digest}`", "", REVIEW_BLOCK_OPEN])
        for field in field_order:
            value = entry[field]
            if isinstance(value, str):
                rendered = toml_string(value)
            elif isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, int):
                rendered = str(value)
            elif isinstance(value, list):
                rendered = toml_string_array(value)
            else:
                raise RuntimeError("人工复核清单包含不可序列化字段，未执行写入")
            lines.append(f"{field} = {rendered}")
        lines.extend(["```", ""])
    return "\n".join(lines).rstrip() + "\n"


def safe_report_path(note: Note) -> str:
    if sensitive_types_for(note.relative):
        return f"[redacted-path:{path_key_for(note.relative)[:12]}]"
    return note.relative


def state_key(note: Note) -> str:
    return safe_report_path(note)


def state_value(note: Note) -> str:
    return f"{note.digest}:allowed" if note.allowlisted else note.digest


def candidate_state_value(note: Note) -> str:
    body_digest = sha256_bytes(note.body.encode("utf-8"))
    return f"{body_digest}:allowed" if note.allowlisted else body_digest


def group_quarantined(
    notes: Iterable[Note],
) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    grouped: dict[str, dict[str, set[str]]] = {}
    for note in notes:
        if not note.quarantined:
            continue
        item = grouped.setdefault(note.digest, {"types": set(), "paths": set()})
        item["types"].update(note.sensitive_types)
        item["paths"].add(safe_report_path(note))
    return sorted(
        (
            digest,
            tuple(sorted(item["types"])),
            tuple(sorted(item["paths"])),
        )
        for digest, item in grouped.items()
    )


def sync_review_registry(
    vault: Path,
    registry_path: Path,
    existing: dict[str, dict[str, object]],
    now: datetime,
) -> dict[str, object]:
    notes = [read_note(path, vault) for path in iter_gate_markdown(vault)]
    grouped: dict[str, dict[str, set[str]]] = {}
    for note in notes:
        if not note.sensitive_types:
            continue
        group = grouped.setdefault(note.digest, {"types": set(), "path_keys": set()})
        group["types"].update(note.sensitive_types)
        group["path_keys"].add(path_key_for(note.relative))

    seen_at = now.isoformat(timespec="seconds")
    updated = {digest: dict(entry) for digest, entry in existing.items()}
    for entry in updated.values():
        entry["present"] = False
        entry["file_count"] = 0
    for digest, group in grouped.items():
        path_keys = sorted(group["path_keys"])
        if digest not in updated:
            updated[digest] = new_review_entry(digest, group["types"], path_keys, seen_at)
            continue
        entry = updated[digest]
        entry["sensitive_types"] = sorted(group["types"])
        entry["path_keys"] = path_keys
        entry["last_seen"] = seen_at
        entry["present"] = True
        entry["file_count"] = len(path_keys)

    rendered = render_review_registry(updated, seen_at)
    reparsed = parse_review_registry_text(rendered)
    if reparsed != updated:
        raise RuntimeError("人工复核清单序列化复核失败，未执行写入")
    atomic_text_write(registry_path, rendered)
    verified = load_review_registry(registry_path)
    if verified != updated:
        raise RuntimeError("人工复核清单写入后复核失败")
    return {
        "vault": str(vault),
        "mode": "review-registry-sync",
        "registry": str(registry_path),
        "entry_count": len(updated),
        "present_hash_count": len(grouped),
        "pending_count": sum(entry["decision"] == "pending" for entry in updated.values()),
        "allowed_count": sum(entry["decision"] == "allowed" for entry in updated.values()),
        "blocked_count": sum(entry["decision"] == "blocked" for entry in updated.values()),
        "revoked_count": sum(entry["decision"] == "revoked" for entry in updated.values()),
        "sensitive_file_count": sum(bool(note.sensitive_types) for note in notes),
        "state": None,
    }


def check_paths(
    vault: Path,
    requested_paths: list[Path],
    allowed_hashes: frozenset[str],
    mode: str = "gate-check",
) -> dict[str, object]:
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for requested in requested_paths:
        path = (requested if requested.is_absolute() else vault / requested).resolve()
        if not path.is_file() or not is_gate_markdown(path, vault):
            raise RuntimeError("检查路径不是受管 Markdown，未读取；请求已拒绝")
        note = read_note(path, vault, allowed_hashes)
        if note.relative in seen:
            continue
        seen.add(note.relative)
        results.append(
            {
                "path": safe_report_path(note),
                "path_key": path_key_for(note.relative),
                "sha256": note.digest,
                "eligible": not note.quarantined,
                "allowlisted": note.allowlisted,
                "sensitive_types": list(note.sensitive_types),
            }
        )
    return {
        "vault": str(vault),
        "mode": mode,
        "checked_count": len(results),
        "eligible_count": sum(bool(item["eligible"]) for item in results),
        "quarantined_count": sum(not bool(item["eligible"]) for item in results),
        "quarantined_hash_count": len(
            {str(item["sha256"]) for item in results if not bool(item["eligible"])}
        ),
        "files": results,
        "state": None,
    }


def safe_read_paths(
    vault: Path,
    requested_paths: list[Path],
    allowed_hashes: frozenset[str],
) -> dict[str, object]:
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for requested in requested_paths:
        path = (requested if requested.is_absolute() else vault / requested).resolve()
        if not path.is_file() or not is_gate_markdown(path, vault):
            raise RuntimeError("读取路径不是受管 Markdown，未读取；请求已拒绝")
        note, clean_text = inspect_note(path, vault, allowed_hashes)
        if note.relative in seen:
            continue
        seen.add(note.relative)
        item: dict[str, object] = {
            "path": safe_report_path(note),
            "path_key": path_key_for(note.relative),
            "sha256": note.digest,
            "eligible": not note.quarantined,
            "allowlisted": note.allowlisted,
            "sensitive_types": list(note.sensitive_types),
        }
        if clean_text is not None:
            item["content"] = clean_text
        results.append(item)
    return {
        "vault": str(vault),
        "mode": "gate-read",
        "checked_count": len(results),
        "eligible_count": sum(bool(item["eligible"]) for item in results),
        "quarantined_count": sum(not bool(item["eligible"]) for item in results),
        "quarantined_hash_count": len(
            {str(item["sha256"]) for item in results if not bool(item["eligible"])}
        ),
        "files": results,
        "state": None,
    }


def load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"schema_version": 1, "files": {}, "candidate_files": {}, "runs": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"状态文件不可读，未执行写入：{path}: {exc}") from exc
    if (
        state.get("schema_version") != 1
        or not isinstance(state.get("files"), dict)
        or ("candidate_files" in state and not isinstance(state["candidate_files"], dict))
    ):
        raise RuntimeError(f"不支持的状态文件格式，未执行写入：{path}")
    return state


def episode_canonical(value: object) -> str:
    if isinstance(value, list):
        return "[" + ",".join(episode_canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False, separators=(",", ":")) + ":" + episode_canonical(value[key])
            for key in sorted(value)
        ) + "}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            number = float(value)
        except OverflowError:
            return "null"
        if not math.isfinite(number):
            return "null"
        if number == 0:
            return "0"
        if number.is_integer() and abs(number) <= MAX_SAFE_INTEGER:
            return str(int(number))
        mantissa, exponent = format(number, ".17e").split("e")
        mantissa = mantissa.rstrip("0").rstrip(".")
        return f"{mantissa}e{int(exponent)}"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def verify_episode_ledger(records: list[dict[str, object]]) -> None:
    previous_hash = "GENESIS"
    for index, record in enumerate(records, 1):
        if record.get("sequence") != index or record.get("previous_hash") != previous_hash:
            raise RuntimeError(f"经历账本链无效：sequence {index}")
        actual_hash = record.get("record_hash")
        if not isinstance(actual_hash, str):
            raise RuntimeError(f"经历账本哈希缺失：sequence {index}")
        without_hash = dict(record)
        without_hash.pop("record_hash", None)
        expected_hash = hashlib.sha256(episode_canonical(without_hash).encode("utf-8")).hexdigest()
        if expected_hash != actual_hash:
            raise RuntimeError(f"经历账本哈希无效：sequence {index}")
        previous_hash = actual_hash


def load_episode_records(vault: Path) -> list[dict[str, object]]:
    ledger = vault / EPISODE_LEDGER_RELATIVE
    if not ledger.exists():
        return []
    try:
        records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"经历账本不可读，停止蒸馏：{ledger}") from exc
    if any(not isinstance(record, dict) for record in records):
        raise RuntimeError("经历账本记录结构无效，停止蒸馏")
    verify_episode_ledger(records)
    return records


def episode_lesson_candidates(vault: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
    records = load_episode_records(vault)
    grouped: dict[str, dict[str, object]] = {}
    for record in records:
        if record.get("event_type") != "task_finished":
            continue
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        candidate = payload.get("lesson_candidate")
        verification = payload.get("verification") or {}
        if not isinstance(candidate, dict) or not candidate.get("errorSignature"):
            continue
        if not isinstance(verification, dict) or verification.get("status") != "passed":
            continue
        signature = str(candidate["errorSignature"])
        group = grouped.setdefault(signature, {
            "sources": set(), "verified_sources": set(), "receipt_keys": set(),
            "verifiers": set(), "lessons": set(), "records": [],
        })
        source = str(payload.get("source") or "unknown")
        group["sources"].add(source)
        receipt = verification.get("receipt") or {}
        artifact_hash = str(receipt.get("artifactHash") or "").lower() if isinstance(receipt, dict) else ""
        run_id = str(receipt.get("runId") or "").strip() if isinstance(receipt, dict) else ""
        verifier = str(receipt.get("verifier") or "").strip() if isinstance(receipt, dict) else ""
        receipt_valid = bool(SHA256_PATTERN.fullmatch(artifact_hash) and run_id and verifier)
        if verification.get("regressionPassed") is True and receipt_valid:
            group["verified_sources"].add(source)
            group["receipt_keys"].add((artifact_hash, run_id))
            group["verifiers"].add(verifier)
        lesson = str(candidate.get("lesson") or "").strip()
        if lesson:
            group["lessons"].add(lesson)
        group["records"].append(record)
    candidates: list[dict[str, object]] = []
    for signature, group in grouped.items():
        verified_sources = sorted(group["verified_sources"])
        if len(verified_sources) < 2 or len(group["receipt_keys"]) < 2 or len(group["verifiers"]) < 2:
            continue
        candidates.append({
            "error_signature": signature,
            "lesson": sorted(group["lessons"])[0] if group["lessons"] else "",
            "independent_sources": len(group["sources"]),
            "verified_independent_sources": len(verified_sources),
            "distinct_verification_receipts": len(group["receipt_keys"]),
            "distinct_verifiers": len(group["verifiers"]),
            "status": "eligible_for_review",
            "evidence_sequences": [record.get("sequence") for record in group["records"]],
        })
    candidates.sort(key=lambda item: item["error_signature"])
    return candidates, {
        "ledger_record_count": len(records),
        "ledger_head": records[-1].get("record_hash") if records else "GENESIS",
        "candidate_count": len(candidates),
    }


def episode_candidate_receipt(candidate: dict[str, object]) -> str:
    return hashlib.sha256(episode_canonical(candidate).encode("utf-8")).hexdigest()


def load_episode_distill_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"schema_version": 1, "candidate_receipts": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"经历候选状态不可读，停止蒸馏：{path}") from exc
    if state.get("schema_version") != 1 or not isinstance(state.get("candidate_receipts"), list):
        raise RuntimeError("经历候选状态结构无效，停止蒸馏")
    return state


def parse_feedback(report_dir: Path) -> tuple[dict[str, str], Counter[str]]:
    decisions: dict[str, str] = {}
    origin_signal: Counter[str] = Counter()
    if not report_dir.exists():
        return decisions, origin_signal
    for path in report_dir.glob("*.md"):
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        if sensitive_types_for(text):
            continue
        for block in FEEDBACK_BLOCK.findall(text):
            fields = {}
            for line in block.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key.strip()] = value.strip()
            source_path = fields.get("source_path", "")
            decision = fields.get("decision", "pending").lower()
            if source_path and decision in {"accepted", "rejected", "deferred", "pending"}:
                decisions[source_path] = decision
                origin = source_path.split("/", 1)[0]
                if decision == "accepted":
                    origin_signal[origin] += 2
                elif decision == "rejected":
                    origin_signal[origin] -= 2
                elif decision == "deferred":
                    origin_signal[origin] -= 1
    return decisions, origin_signal


def knowledge_notes(notes: Iterable[Note]) -> list[Note]:
    return [
        note
        for note in notes
        if note.relative.startswith("02_Knowledge/")
        and note.path.name not in {"README.md", "_知识索引.md"}
        and not note.quarantined
    ]


def is_candidate_input(note: Note) -> bool:
    if note.quarantined or note.path.name in {"README.md", "_知识索引.md"}:
        return False
    note_type = str(note.frontmatter.get("type", "")).lower()
    return note_type not in {
        "compilation-candidates",
        "distillation-candidates",
        "knowledge-health",
    }


def related_knowledge(note: Note, knowledge: list[Note], limit: int = 3) -> list[tuple[Note, int]]:
    explicit_targets = {
        target.strip().replace("\\", "/").removesuffix(".md").lower()
        for target in WIKILINK.findall(note.body)
    }
    explicit: list[tuple[Note, int]] = []
    for card in knowledge:
        if card.relative == note.relative:
            continue
        card_relative = card.relative.removesuffix(".md").lower()
        card_name = card.path.stem.lower()
        if card_relative in explicit_targets or card_name in explicit_targets:
            explicit.append((card, 100))
    if explicit:
        explicit.sort(key=lambda item: item[0].relative)
        return explicit[:limit]

    note_tags = note.frontmatter.get("tags", [])
    note_tag_text = " ".join(note_tags) if isinstance(note_tags, list) else str(note_tags)
    note_routing = tokens_for(f"{note.title} {note_tag_text}")
    ranked: list[tuple[Note, int]] = []
    for card in knowledge:
        if card.relative == note.relative:
            continue
        card_tags = card.frontmatter.get("tags", [])
        card_tag_text = " ".join(card_tags) if isinstance(card_tags, list) else str(card_tags)
        card_routing = tokens_for(f"{card.title} {card_tag_text}")
        overlap_tokens = note_routing & card_routing
        strong_overlap = any(len(token) >= 3 for token in overlap_tokens)
        if len(overlap_tokens) >= 2 or strong_overlap:
            ranked.append((card, len(overlap_tokens)))
    ranked.sort(key=lambda item: (-item[1], item[0].relative))
    return ranked[:limit]


def suggested_action(note: Note, related: list[tuple[Note, int]]) -> str:
    if note.relative.startswith("02_Knowledge/"):
        return "review-update"
    if note.relative.startswith("03_Topics/"):
        return "topic"
    if note.relative.startswith("04_Outputs/"):
        return "output-backflow"
    if note.relative.startswith("00_Inbox/"):
        return "source-or-ignore"
    return "update" if related else "knowledge-or-source"


def score_note(note: Note, now: datetime, related_count: int, origin_signal: Counter[str]) -> int:
    base = {
        "00_Inbox": 55,
        "01_Sources": 65,
        "02_Knowledge": 70,
        "03_Topics": 50,
        "04_Outputs": 60,
        "05_Review": 35,
    }.get(note.origin, 30)
    score = base + min(related_count * 5, 15) + max(-10, min(10, origin_signal[note.origin]))
    status = str(note.frontmatter.get("status", "")).lower()
    if status in {"draft", "inbox", "pending-review"}:
        score -= 5
    if note.frontmatter.get("source") or note.frontmatter.get("sources"):
        score += 5
    age_days = max(0, (now - note.modified).days)
    score -= min(age_days, 14)
    return max(0, min(100, score))


def wiki_link(relative: str, label: str | None = None) -> str:
    target = relative[:-3] if relative.lower().endswith(".md") else relative
    return f"[[{target}|{label}]]" if label else f"[[{target}]]"


def safe_note_title(note: Note) -> str:
    if sensitive_types_for(note.title):
        return f"[redacted-title:{path_key_for(note.relative)[:12]}]"
    return note.title


def report_link(note: Note) -> str:
    if sensitive_types_for(note.relative):
        return f"`{safe_report_path(note)}`"
    return wiki_link(note.relative, safe_note_title(note))


def safe_excerpt(note: Note, limit: int = 420) -> str:
    if note.quarantined:
        return "（来源已由敏感资格门禁隔离，未提取正文。）"
    kept: list[str] = []
    for line in note.body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--") or has_sensitive_line(stripped):
            continue
        stripped = re.sub(r"\s+", " ", stripped)
        kept.append(stripped)
        if sum(len(item) for item in kept) >= limit:
            break
    excerpt = " ".join(kept)[:limit].strip()
    return excerpt or "（未提取到可展示正文；请人工打开来源核对。）"


def build_report(
    now: datetime,
    changed: list[Note],
    candidates: list[tuple[Note, int, list[tuple[Note, int]]]],
    decisions: dict[str, str],
    state_exists: bool,
    lookback_days: int,
    episode_candidates: list[dict[str, object]] | None = None,
    episode_summary: dict[str, object] | None = None,
) -> str:
    eligible_changed = [note for note in changed if not note.quarantined]
    allowlisted_changed = [note for note in changed if note.allowlisted]
    quarantined_notes = [note for note in changed if note.quarantined]
    quarantined_groups = group_quarantined(quarantined_notes)
    accepted = sum(value == "accepted" for value in decisions.values())
    rejected = sum(value == "rejected" for value in decisions.values())
    deferred = sum(value == "deferred" for value in decisions.values())
    pending = sum(value == "pending" for value in decisions.values())
    mode = "增量" if state_exists else f"首次近 {lookback_days} 日回看"
    lines = [
        "---",
        f'title: "{now:%Y-%m-%d} 自生长知识蒸馏候选"',
        "type: distillation-candidates",
        "status: pending-review",
        f"created: {now:%Y-%m-%d}",
        f"changed_count: {len(changed)}",
        f"eligible_changed_count: {len(eligible_changed)}",
        f"allowlisted_changed_count: {len(allowlisted_changed)}",
        f"quarantined_file_count: {len(quarantined_notes)}",
        f"quarantined_hash_count: {len(quarantined_groups)}",
        f"candidate_count: {len(candidates)}",
        "tags:",
        "  - 自生长蒸馏",
        "  - 知识迭代",
        "---",
        "",
        f"# {now:%Y-%m-%d} 自生长知识蒸馏候选",
        "",
        "> 本报告由本机增量扫描生成，只负责召回、关联和排序，不把摘录自动升级为知识。请核对原文后填写人工决定。",
        "",
        "## 运行摘要",
        "",
        f"- 扫描模式：{mode}",
        f"- 内容变化：{len(changed)} 个 Markdown 文件",
        f"- 允许后续处理：{len(eligible_changed)} 个文件",
        f"- 其中人工按哈希放行：{len(allowlisted_changed)} 个文件",
        f"- 敏感隔离：{len(quarantined_notes)} 个文件 / {len(quarantined_groups)} 个内容哈希",
        f"- 进入人审：{len(candidates)} 个候选",
        f"- 历史反馈：accepted {accepted} / rejected {rejected} / deferred {deferred} / pending {pending}",
        "- 写入边界：未修改 `00_Inbox`、`01_Sources`、`02_Knowledge`、`03_Topics` 或 `04_Outputs`。",
        "",
        "## 知识编译候选",
        "",
    ]
    if not candidates:
        lines.extend(["本轮无新增候选。", ""])
    for index, (note, score, related) in enumerate(candidates, 1):
        related_links = "、".join(report_link(card) for card, _ in related) or "未命中，需人工检索"
        action = suggested_action(note, related)
        lines.extend(
            [
                f"### 候选 {index}：{safe_note_title(note)}",
                "",
                "<!-- distill-candidate",
                f"source_path: {safe_report_path(note)}",
                f"source_hash: {note.digest}",
                f"origin: {note.origin}",
                "decision: pending",
                "decision_reason:",
                "-->",
                f"- 来源：{report_link(note)}",
                f"- 召回分：{score}/100（只用于排序，不代表可信度）",
                f"- 建议动作：`{action}`",
                f"- 已有关联：{related_links}",
                f"- 原文线索：{safe_excerpt(note)}",
                "- 待蒸馏主张：请用一句可判断、可反驳的话填写；不得直接复制摘录。",
                "- 新增量：请说明它支持、修正、反驳或限制了哪条当前认知。",
                "- 证据等级：`待核对`",
                "- 反证与边界：`待核对`",
                "- 人工决定：`pending`（同时修改上方隐藏块中的 `decision`）",
                "- 决定备注：",
                "",
            ]
        )
    lines.extend(["## 隔离来源", ""])
    if not quarantined_groups:
        lines.extend(["本轮没有敏感隔离来源。", ""])
    else:
        lines.extend(
            [
                "> 下列文件在 frontmatter 解析、检索词生成、关联和排序前已被隔离。这里只记录脱敏路径、内容哈希和命中类型，不记录正文、命中行或值。",
                "",
            ]
        )
        for index, (digest, sensitive_types, paths) in enumerate(quarantined_groups, 1):
            lines.extend(
                [
                    f"### 隔离 {index}",
                    "",
                    f"- 路径：{'、'.join(f'`{path}`' for path in paths)}",
                    f"- 内容 SHA-256：`{digest}`",
                    f"- 命中类型：{', '.join(f'`{item}`' for item in sensitive_types)}",
                    "- 处置：`quarantined`；未经人工按哈希复核，不得进入蒸馏。",
                    "",
                ]
            )
    lines.extend(["## 全部可处理变化", ""])
    if eligible_changed:
        lines.extend(f"- {report_link(note)}" for note in eligible_changed)
    else:
        lines.append("- 无。")
    lines.extend(
        [
            "",
            "## 审批规则",
            "",
            "- `accepted`：允许后续 Agent 按原文与现有卡执行编译；本脚本本身不会晋升。",
            "- `rejected`：不进入知识层，并填写拒绝原因；下轮同来源层级会降低排序权重。",
            "- `deferred`：写明缺失证据或等待条件；下轮轻度降权但保留召回可能。",
            "- `pending`：保持未决，不修改正式知识。",
            "",
        ]
    )
    episode_candidates = episode_candidates or []
    episode_summary = episode_summary or {"ledger_record_count": 0, "candidate_count": 0}
    lines.extend(
        [
            "## 经历账本经验候选",
            "",
            f"- 账本记录数：{episode_summary.get('ledger_record_count', 0)}",
            f"- 已满足双独立来源且显式回归通过的候选：{episode_summary.get('candidate_count', 0)}",
            "- 门控：仅列为 `eligible_for_review`，不会自动写入 `02_Knowledge`；仍需人审、来源核对和定向回归。",
        ]
    )
    if not episode_candidates:
        lines.append("- 本轮没有达到升级门槛的经历经验候选。")
    else:
        for candidate in episode_candidates:
            lines.extend(
                [
                    f"### {candidate['error_signature']}",
                    "",
                    f"- lesson：{candidate['lesson']}",
                    f"- 独立来源数：{candidate['independent_sources']}",
                    f"- 显式回归通过来源数：{candidate['verified_independent_sources']}",
                    f"- 不同验证回执数：{candidate['distinct_verification_receipts']}",
                    f"- 不同验证器数：{candidate['distinct_verifiers']}",
                    f"- 状态：`{candidate['status']}`",
                    f"- 证据序号：{', '.join(str(item) for item in candidate['evidence_sequences'])}",
                    "",
                ]
            )
    return "\n".join(lines)


def unique_report_path(report_dir: Path, now: datetime) -> Path:
    base = report_dir / f"{now:%Y-%m-%d}-自生长知识蒸馏候选.md"
    if not base.exists():
        return base
    return report_dir / f"{now:%Y-%m-%d-%H%M%S}-自生长知识蒸馏候选.md"


def atomic_json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def atomic_text_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def run(args: argparse.Namespace) -> dict[str, object]:
    vault = args.vault.resolve()
    if not (vault / "AGENTS.md").is_file() or not (vault / ".obsidian").is_dir():
        raise RuntimeError(f"不是预期的 Obsidian Vault：{vault}")
    if args.lookback_days < 0 or not 1 <= args.max_candidates <= 20:
        raise RuntimeError("lookback-days 必须 >= 0，max-candidates 必须在 1..20")
    now = datetime.fromisoformat(args.now).astimezone() if args.now else datetime.now().astimezone()
    registry_path = vault / REVIEW_REGISTRY_RELATIVE
    review_entries = load_review_registry(registry_path)
    allowed_hashes = allowed_hashes_for(review_entries)
    gate_modes = sum(
        (bool(args.check_path), bool(args.read_path), bool(args.check_all), bool(args.sync_review_registry))
    )
    if gate_modes > 1:
        raise RuntimeError("资格检查、读取、全库检查和复核清单同步只能选择一种")
    if gate_modes:
        if args.dry_run or args.bootstrap:
            raise RuntimeError("资格门禁模式不能与 --dry-run 或 --bootstrap 同时使用")
    if args.sync_review_registry:
        return sync_review_registry(vault, registry_path, review_entries, now)
    if args.check_path:
        return check_paths(vault, args.check_path, allowed_hashes)
    if args.read_path:
        return safe_read_paths(vault, args.read_path, allowed_hashes)
    if args.check_all:
        return check_paths(
            vault,
            list(iter_gate_markdown(vault)),
            allowed_hashes,
            mode="gate-check-all",
        )

    state_path = vault / STATE_RELATIVE
    report_dir = vault / REPORT_DIR_RELATIVE
    episode_state_path = vault / EPISODE_DISTILL_STATE_RELATIVE
    all_episode_candidates, episode_summary = episode_lesson_candidates(vault)
    episode_state = load_episode_distill_state(episode_state_path)
    previous_episode_receipts = set(str(item) for item in episode_state["candidate_receipts"])
    new_episode_candidates = [
        candidate
        for candidate in all_episode_candidates
        if episode_candidate_receipt(candidate) not in previous_episode_receipts
        and not sensitive_types_for(json.dumps(candidate, ensure_ascii=False))
    ]
    episode_summary = {
        **episode_summary,
        "candidate_count": len(new_episode_candidates),
        "total_eligible_candidate_count": len(all_episode_candidates),
    }
    state_exists = state_path.exists()
    state = load_state(state_path)
    previous_files = state.get("files", {})
    previous_candidate_files = state.get("candidate_files")
    notes = [read_note(path, vault, allowed_hashes) for path in iter_markdown(vault)]
    notes.sort(key=lambda note: note.relative)
    current_files = {state_key(note): state_value(note) for note in notes}
    current_candidate_files = {state_key(note): candidate_state_value(note) for note in notes}

    cutoff = now - timedelta(days=args.lookback_days)
    changed = [
        note
        for note in notes
        if (state_exists and previous_files.get(state_key(note)) != state_value(note))
        or (not state_exists and note.modified >= cutoff)
    ]
    quarantined_notes = [note for note in changed if note.quarantined]
    allowlisted_notes = [note for note in changed if note.allowlisted]
    quarantined_groups = group_quarantined(quarantined_notes)
    untracked_quarantined = [
        note
        for note in notes
        if note.quarantined
        and (not state_exists or previous_files.get(state_key(note)) != state_value(note))
    ]
    if args.bootstrap and untracked_quarantined:
        raise RuntimeError(
            "检测到新增或变化的敏感隔离来源，已拒绝 bootstrap；请先运行普通扫描生成隔离报告"
        )
    decisions, origin_signal = parse_feedback(report_dir)
    knowledge = knowledge_notes(notes)
    # Legacy state has no body baseline, so recall once rather than risk missing a body edit.
    candidate_changed = [
        note
        for note in changed
        if not isinstance(previous_candidate_files, dict)
        or previous_candidate_files.get(state_key(note)) != candidate_state_value(note)
    ]
    ranked = []
    for note in candidate_changed:
        if not is_candidate_input(note):
            continue
        related = related_knowledge(note, knowledge)
        score = score_note(note, now, len(related), origin_signal)
        ranked.append((note, score, related))
    ranked.sort(key=lambda item: (-item[1], -item[0].modified.timestamp(), item[0].relative))
    unique_ranked = []
    seen_digests: set[str] = set()
    for item in ranked:
        digest = item[0].digest
        if digest in seen_digests:
            continue
        seen_digests.add(digest)
        unique_ranked.append(item)
    candidates = unique_ranked[: args.max_candidates]

    report_path: Path | None = None
    report = build_report(
        now,
        changed,
        candidates,
        decisions,
        state_exists,
        args.lookback_days,
        new_episode_candidates,
        episode_summary,
    )
    if args.dry_run:
        print(report)
    elif not args.bootstrap and (changed or new_episode_candidates):
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = unique_report_path(report_dir, now)
        report_path.write_text(report, encoding="utf-8", newline="\n")

    new_state = {
        "schema_version": 1,
        "vault": str(vault),
        "updated_at": now.isoformat(timespec="seconds"),
        "files": current_files,
        "candidate_files": current_candidate_files,
        "feedback": dict(Counter(decisions.values())),
        "runs": (state.get("runs", []) + [
            {
                "at": now.isoformat(timespec="seconds"),
                "mode": "bootstrap" if args.bootstrap else "scan",
                "changed_count": len(changed),
                "quarantined_file_count": len(quarantined_notes),
                "quarantined_hash_count": len(quarantined_groups),
                "allowlisted_file_count": len(allowlisted_notes),
                "candidate_count": len(candidates),
                "report": report_path.relative_to(vault).as_posix() if report_path else None,
            }
        ])[-50:],
    }
    if not args.dry_run:
        atomic_json_write(state_path, new_state)
        atomic_json_write(
            episode_state_path,
            {
                "schema_version": 1,
                "updated_at": now.isoformat(timespec="seconds"),
                "ledger_head": episode_summary["ledger_head"],
                "candidate_receipts": [
                    episode_candidate_receipt(candidate) for candidate in all_episode_candidates
                ],
            },
        )

    return {
        "vault": str(vault),
        "changed_count": len(changed),
        "quarantined_file_count": len(quarantined_notes),
        "quarantined_hash_count": len(quarantined_groups),
        "allowlisted_file_count": len(allowlisted_notes),
        "candidate_count": len(candidates),
        "episode_lesson_candidate_count": len(new_episode_candidates),
        "report": str(report_path) if report_path else None,
        "state": None if args.dry_run else str(state_path),
        "dry_run": args.dry_run,
        "bootstrap": args.bootstrap,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        result = run(parse_args(argv or sys.argv[1:]))
    except Exception as exc:  # Keep the CLI failure explicit for scheduled runs.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
