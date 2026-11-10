import contextlib
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_workbuddy_terminal_observer as fixtures
import workbuddy_terminal_ingress as ingress

NOW, REQUEST, THREAD = fixtures.NOW, fixtures.REQUEST, fixtures.THREAD


class IngressTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ObserverTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.settings = self.fixture.root / 'settings.json'
        self.settings.write_text('{"hooks": {}}', encoding='utf-8')
        with contextlib.closing(sqlite3.connect(self.fixture.db)) as c, c:
            ingress.create_tables(c)
            ingress.configure(c, self.settings, not_before_ms=int(THREAD.rsplit(':', 1)[1]))

    def publish(self):
        f = self.fixture
        return ingress.publish(f.payload, request_id=REQUEST, db_path=f.db,
                               projects_root=f.projects, observation_root=f.observations,
                               vault=f.vault, settings_path=self.settings, now_ms=NOW)

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.fixture.db)) as c:
            return c.execute('SELECT record_json FROM second_brain_terminal_ingress').fetchall()

    def test_native_answer_is_preserved_without_rewriting_host_run(self):
        with contextlib.closing(sqlite3.connect(self.fixture.db)) as c:
            before = c.execute('SELECT * FROM automation_runs').fetchall()
        self.assertEqual(self.publish()['evidence_status'], 'PUBLISHED')
        stored = json.loads(self.rows()[0][0])
        self.assertEqual(stored['assistant_text'], self.fixture.answer)
        with contextlib.closing(sqlite3.connect(self.fixture.db)) as c:
            self.assertEqual(c.execute('SELECT * FROM automation_runs').fetchall(), before)
        self.fixture.finish()
        result = ingress.verify(THREAD, db_path=self.fixture.db,
                                projects_root=self.fixture.projects,
                                observation_root=self.fixture.observations,
                                settings_path=self.settings)
        self.assertTrue(result['native_ingress_verified'])
        self.assertEqual(result['declared_business_status'], 'HOLD')
        self.assertFalse(result['complete_repair_proven'])

    def test_completed_history_cannot_be_published(self):
        self.fixture.capture()
        self.fixture.finish()
        self.assertEqual(self.publish()['evidence_status'], 'SKIPPED')
        self.assertEqual(self.rows(), [])

    def test_cutoff_prevents_publishing_an_existing_run(self):
        with contextlib.closing(sqlite3.connect(self.fixture.db)) as c, c:
            c.execute('UPDATE second_brain_terminal_ingress_config SET not_before_ms=?', (NOW,))
        with self.assertRaisesRegex(ingress.IngressError, 'run_predates_ingress'):
            self.publish()
        self.assertEqual(self.rows(), [])

    def test_identical_callback_is_idempotent_and_conflicting_one_rejected(self):
        self.publish()
        self.publish()
        self.assertEqual(len(self.rows()), 1)
        self.fixture.answer += '\n'
        self.fixture.terminal['content'][0]['text'] = self.fixture.answer
        self.fixture.write_transcript()
        with self.assertRaises(Exception):
            self.publish()
        self.assertEqual(len(self.rows()), 1)

    def test_settings_or_implementation_drift_blocks_publication(self):
        self.settings.write_text('{"hooks":{"FinalStop":[]}}', encoding='utf-8')
        with self.assertRaisesRegex(ingress.IngressError, 'ingress_configuration_drift'):
            self.publish()
        self.assertEqual(self.rows(), [])

    def test_unrelated_settings_change_does_not_block_publication(self):
        self.settings.write_text(
            '{"hooks":{},"officeFileAssociationsRepairMarker":"v9:win32:5.8.2"}',
            encoding='utf-8')
        self.assertEqual(self.publish()['evidence_status'], 'PUBLISHED')

    def test_invalid_hook_configuration_is_rejected(self):
        self.settings.write_text('{"changed":true}', encoding='utf-8')
        with self.assertRaisesRegex(ingress.IngressError, 'invalid_hook_configuration'):
            self.publish()
        self.assertEqual(self.rows(), [])

    def test_terminal_or_binding_change_after_capture_blocks_publication(self):
        original = ingress.observer.capture
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.fixture.records.append({'type': 'message', 'role': 'tool'})
            self.fixture.write_transcript()
            return result
        with patch.object(ingress.observer, 'capture', side_effect=changed):
            with self.assertRaises(Exception):
                self.publish()
        self.assertEqual(self.rows(), [])

    def test_sensitive_receipt_is_not_published(self):
        self.fixture.answer = self.fixture.answer.replace('STEP_AUTHORIZATION_EVIDENCE=',
            'STEP_AUTHORIZATION_EVIDENCE=access_token=synthetic-sentinel;')
        self.fixture.terminal['content'][0]['text'] = self.fixture.answer
        self.fixture.write_transcript()
        with self.assertRaisesRegex(ingress.IngressError, 'sensitive_receipt_blocked'):
            self.publish()
        self.assertEqual(self.rows(), [])

    def test_disabled_ingress_rejects_without_touching_history(self):
        with contextlib.closing(sqlite3.connect(self.fixture.db)) as c, c:
            c.execute('UPDATE second_brain_terminal_ingress_config SET enabled=0')
        with self.assertRaisesRegex(ingress.IngressError, 'ingress_disabled'):
            self.publish()
        self.assertEqual(self.rows(), [])

    def test_stored_records_cannot_be_overwritten_or_deleted(self):
        self.publish()
        with contextlib.closing(sqlite3.connect(self.fixture.db)) as c:
            for sql in ('UPDATE second_brain_terminal_ingress SET record_json="{}"',
                        'DELETE FROM second_brain_terminal_ingress',
                        'INSERT OR REPLACE INTO second_brain_terminal_ingress SELECT * FROM second_brain_terminal_ingress'):
                with self.assertRaises(sqlite3.IntegrityError):
                    c.execute(sql)
                c.rollback()

    def test_hook_process_skips_other_events_and_never_echoes_sensitive_input(self):
        import subprocess
        import sys
        for payload in (b'{"hook_event_name":"Stop"}', b'invalid-SENSITIVE-SENTINEL'):
            run = subprocess.run([sys.executable, '-I', '-B', ingress.__file__],
                                 input=payload, capture_output=True, timeout=10,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(run.returncode, 0)
            self.assertEqual(run.stdout.strip(), b'{}')
            self.assertNotIn(b'SENSITIVE-SENTINEL', run.stderr)

    def test_changed_transcript_is_rejected_on_readback(self):
        self.publish()
        self.fixture.finish()
        self.fixture.records.append({'type': 'message', 'role': 'tool'})
        self.fixture.write_transcript()
        with self.assertRaises(Exception):
            ingress.verify(THREAD, db_path=self.fixture.db,
                           projects_root=self.fixture.projects,
                           observation_root=self.fixture.observations,
                           settings_path=self.settings)


if __name__ == '__main__':
    unittest.main()
