"""Prospective native FinalStop storage; never changes host results or grants PASS."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


observer = load('ingress_observer', Path(__file__).with_name('workbuddy_terminal_observer.py'))
security = load('ingress_sensitive_scan', Path(__file__).resolve().parents[2] / 'governance/self_growing_distillation.py')
ROOT = observer.ROOT
SCHEMA = 'second-brain-native-terminal-ingress-v1'
MODE = 'STAGED_NATIVE_INGRESS'
TABLE = 'second_brain_terminal_ingress'
CONFIG = 'second_brain_terminal_ingress_config'


class IngressError(RuntimeError):
    pass


def fingerprint(path):
    return hashlib.sha256(observer.plain_path(path).read_bytes()).hexdigest()


def settings_fingerprint(path):
    """Fingerprint only the host hook configuration protected by this ingress."""
    try:
        settings = json.loads(observer.plain_path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise IngressError('invalid_hook_configuration') from error
    hooks = settings.get('hooks') if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        raise IngressError('invalid_hook_configuration')
    canonical = json.dumps(hooks, ensure_ascii=True, sort_keys=True,
                           separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()


def implementation_hashes(settings_path):
    hashes = {name: fingerprint(path) for name, path in {
        'publisher_sha256': Path(__file__),
        'observer_sha256': Path(observer.__file__),
        'audit_sha256': Path(observer.audit.__file__),
        'validator_sha256': Path(__file__).with_name('validate_automation_receipt.py'),
        'safety_sha256': Path(security.__file__),
    }.items()}
    hashes['settings_sha256'] = settings_fingerprint(settings_path)
    return hashes


def create_tables(c):
    # This namespace is additive. Existing host runs, audits and receipt-gate objects are untouched.
    statements = [
        f'''CREATE TABLE {CONFIG} (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), not_before_ms INTEGER NOT NULL,
            hashes_json TEXT NOT NULL CHECK(json_valid(hashes_json)))''',
        f'''CREATE TABLE {TABLE} (
            thread_id TEXT PRIMARY KEY, record_json TEXT NOT NULL CHECK(json_valid(record_json)),
            CHECK(json_extract(record_json,'$.thread_id')=thread_id))''',
        f'''CREATE TRIGGER {TABLE}_immutable_insert BEFORE INSERT ON {TABLE}
            WHEN EXISTS(SELECT 1 FROM {TABLE} WHERE thread_id=NEW.thread_id)
            BEGIN SELECT RAISE(ABORT,'immutable_native_ingress'); END''',
        f'''CREATE TRIGGER {TABLE}_immutable_update BEFORE UPDATE ON {TABLE}
            BEGIN SELECT RAISE(ABORT,'immutable_native_ingress'); END''',
        f'''CREATE TRIGGER {TABLE}_immutable_delete BEFORE DELETE ON {TABLE}
            BEGIN SELECT RAISE(ABORT,'immutable_native_ingress'); END''',
    ]
    for statement in statements:
        c.execute(statement)


def configure(c, settings_path, *, not_before_ms):
    c.execute(f'INSERT INTO {CONFIG} VALUES (1,?,1,?,?)',
              (SCHEMA, not_before_ms, json.dumps(implementation_hashes(settings_path), sort_keys=True)))


def checked_config(c, settings_path):
    row = c.execute(f'SELECT schema,enabled,not_before_ms,hashes_json FROM {CONFIG} WHERE singleton=1').fetchone()
    if row is None or row[0] != SCHEMA or row[1] != 1 or type(row[2]) is not int or row[2] <= 0:
        raise IngressError('ingress_disabled_or_invalid')
    if observer.audit._decode(row[3]) != implementation_hashes(settings_path):
        raise IngressError('ingress_configuration_drift')
    return tuple(row)


def connect(db_path, *, write=False):
    c = sqlite3.connect(observer.plain_path(db_path).as_uri() + ('?mode=rw' if write else '?mode=ro'),
                        uri=True, timeout=2)
    if not write:
        c.execute('PRAGMA query_only=ON')
    return c


def safe_error(error):
    result = {'error_type': type(error).__name__}
    if isinstance(error, (IngressError, observer.ObservationError, observer.audit.TerminalAuditError)):
        code = str(error)
        if observer.re.fullmatch(r'[a-z0-9_]+', code):
            result['error'] = code
    return result


def publish(payload, *, request_id, db_path=ROOT / 'workbuddy.db', projects_root=ROOT / 'projects',
            observation_root=observer.DEFAULT_OBSERVATIONS, vault=observer.DEFAULT_VAULT,
            settings_path=ROOT / 'hooks.json', now_ms=None):
    captured = observer.capture(payload, request_id=request_id, db_path=db_path,
                                projects_root=projects_root, observation_root=observation_root,
                                vault=vault, now_ms=now_ms)
    if captured.get('evidence_status') != 'CAPTURED':
        return {'evidence_status': captured.get('evidence_status'), 'complete_repair_proven': False}
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    thread = captured['thread_id']
    source, raw, answer = observer.audit.read_terminal(observer.plain_path(projects_root),
        session=captured['session_id'], request=captured['request_id'])
    if (fingerprint(source) != captured['transcript_sha256']
            or hashlib.sha256(answer.encode('utf-8')).hexdigest() != captured['terminal_sha256']):
        raise IngressError('native_evidence_changed')
    if security.sensitive_types_for(answer):
        raise IngressError('sensitive_receipt_blocked')
    kind = 'weekly' if captured['automation_id'] == observer.audit.WEEKLY_ID else 'daily'
    observer.audit.validate_answer(answer, kind)
    observation = observer.observation_path(observation_root, thread, request_id)
    observation_raw = observer.audit._read_bytes(observation)
    record = {**captured, 'schema': SCHEMA, 'observation_schema': captured['schema'],
              'mode': MODE, 'assistant_text': answer,
              'observation_sha256': hashlib.sha256(observation_raw).hexdigest()}
    record.pop('evidence_status')
    encoded = json.dumps(record, sort_keys=True, ensure_ascii=True)
    with contextlib.closing(connect(db_path, write=True)) as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            config = checked_config(c, settings_path)
            if captured['run_identity_started_at'] < config[2]:
                raise IngressError('run_predates_ingress')
            binding = observer._active_binding(db_path, captured['session_id'],
                                               observer.plain_path(vault), now_ms)
            if binding is None or any(binding[k] != captured[v] for k, v in {
                'thread_id': 'thread_id', 'automation_id': 'automation_id',
                'created_at': 'run_created_at',
            }.items()):
                raise IngressError('active_binding_changed')
            prior = c.execute(f'SELECT record_json FROM {TABLE} WHERE thread_id=?', (thread,)).fetchone()
            if prior:
                if observer.audit._decode(prior[0]) != record:
                    raise IngressError('ingress_replay_conflict')
            else:
                c.execute(f'INSERT INTO {TABLE} VALUES (?,?)', (thread, encoded))
            if (checked_config(c, settings_path) != config or observer.audit._read_bytes(source) != raw
                    or observer.audit._read_bytes(observation) != observation_raw):
                raise IngressError('evidence_changed_during_publication')
            c.commit()
        except BaseException:
            c.rollback()
            raise
    return {'evidence_status': 'PUBLISHED', 'thread_id': thread, 'mode': MODE,
            'declared_business_status': captured['declared_business_status'],
            'complete_repair_proven': False}


def verify(thread_id, *, db_path=ROOT / 'workbuddy.db', projects_root=ROOT / 'projects',
           observation_root=observer.DEFAULT_OBSERVATIONS, settings_path=ROOT / 'hooks.json'):
    with contextlib.closing(connect(db_path)) as c:
        config = checked_config(c, settings_path)
        row = c.execute(f'SELECT record_json FROM {TABLE} WHERE thread_id=?', (thread_id,)).fetchone()
    if row is None:
        raise IngressError('native_ingress_missing')
    stored = observer.audit._decode(row[0])
    if (stored.get('schema') != SCHEMA or stored.get('mode') != MODE
            or stored.get('observation_schema') != observer.SCHEMA
            or stored.get('run_identity_started_at', 0) < config[2]):
        raise IngressError('native_ingress_identity_mismatch')
    audited = observer.verify(thread_id, db_path=db_path, projects_root=projects_root,
                              observation_root=observation_root)
    obs_path = observer.observation_path(observation_root, thread_id, stored.get('request_id'))
    obs_raw = observer.audit._read_bytes(obs_path)
    observation = observer.audit._decode(obs_raw.decode('utf-8', errors='strict'))
    source, raw, answer = observer.audit.read_terminal(observer.plain_path(projects_root),
        session=stored.get('session_id'), request=stored.get('request_id'))
    expected = {**observation, 'schema': SCHEMA, 'observation_schema': observation['schema'],
                'mode': MODE, 'assistant_text': answer,
                'observation_sha256': hashlib.sha256(obs_raw).hexdigest()}
    expected.pop('evidence_status', None)
    if stored != expected or hashlib.sha256(raw).hexdigest() != audited['transcript_sha256']:
        raise IngressError('native_ingress_evidence_mismatch')
    with contextlib.closing(connect(db_path)) as c:
        if (checked_config(c, settings_path) != config
                or c.execute(f'SELECT record_json FROM {TABLE} WHERE thread_id=?', (thread_id,)).fetchone() != row
                or observer.audit._read_bytes(source) != raw
                or observer.audit._read_bytes(obs_path) != obs_raw):
            raise IngressError('evidence_changed_during_ingress_readback')
    return {**audited, 'mode': MODE, 'native_ingress_verified': True,
            'official_gate_upgraded': False, 'complete_repair_proven': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-thread')
    args = parser.parse_args()
    if args.verify_thread:
        try:
            result = verify(args.verify_thread)
            code = 0 if result['declared_business_status'] == 'PASS' else 2
        except Exception as error:
            result = {'native_ingress_verified': False, **safe_error(error)}
            code = 1
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        raise SystemExit(code)
    try:
        raw = sys.stdin.buffer.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise IngressError('hook_input_too_large')
        publish(observer.audit._decode(raw.decode('utf-8', errors='strict')),
                request_id=os.environ.get('CODEBUDDY_CONVERSATION_REQUEST_ID'))
    except Exception as error:
        print('SECOND_BRAIN_NATIVE_INGRESS_ERROR:' + json.dumps(safe_error(error)), file=sys.stderr)
    print('{}')


if __name__ == '__main__':
    main()
