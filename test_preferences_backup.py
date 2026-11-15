import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
import app_config
import database_backup
import workbench
from browser_queue import queued_browser
from publisher import add_account, db, save_account_identity


class PreferencesAndBackupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, {'OWN_PUBLISHER_HOME': self.folder.name})
        env.start()
        self.addCleanup(env.stop)
        processes = patch('database_backup._other_project_processes',return_value=[])
        processes.start()
        self.addCleanup(processes.stop)
        add_account('xhs01', 'xiaohongshu')
        save_account_identity('xhs01', 'test', '1' * 64)

    def test_theme_survives_new_client_and_address(self):
        first = TestClient(workbench.app, base_url='http://127.0.0.1:8765')
        first.get('/')
        response = first.post('/api/settings/theme', headers={'origin':'http://127.0.0.1:8765'}, json={'theme':'dark'})
        self.assertEqual(response.status_code, 200)
        second = TestClient(workbench.app, base_url='http://127.0.0.1:8766')
        html = second.get('/').text
        self.assertIn('data-theme="dark" data-theme-source="saved"', html)
        self.assertEqual(second.get('/api/settings').json()['theme'], 'dark')

    def test_backup_restore_preserves_safety_copy_and_disables_schedule(self):
        app_config.set_theme('dark')
        app_config.set_interval(60)
        backup = database_backup.create_backup()
        add_account('xhs02', 'xiaohongshu')
        app_config.set_theme('light')
        result = database_backup.restore_backup(backup['name'])
        self.assertTrue((Path(self.folder.name) / 'backups' / result['safety_backup']).is_file())
        with db() as con:
            self.assertEqual([r['alias'] for r in con.execute('SELECT alias FROM accounts')], ['xhs01'])
        self.assertEqual(app_config.settings()['theme'], 'dark')
        self.assertEqual(app_config.settings()['schedule']['interval_minutes'], 0)
        safety = Path(self.folder.name) / 'backups' / result['safety_backup']
        with closing(sqlite3.connect(safety)) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 2)

    def test_invalid_backup_or_active_browser_never_changes_data(self):
        backup = database_backup.create_backup()
        broken = Path(self.folder.name) / 'backups' / 'backup-20261115-010101-deadbeef.sqlite3'
        broken.write_bytes(b'not a sqlite database')
        with self.assertRaisesRegex(ValueError, '损坏'):
            database_backup.restore_backup(broken.name)
        with self.assertRaises(ValueError):
            database_backup.restore_backup('../publisher.sqlite3')
        with queued_browser('open'):
            with self.assertRaisesRegex(ValueError, '浏览器任务'):
                database_backup.restore_backup(backup['name'])
        with db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM accounts').fetchone()[0], 1)
        self.assertEqual(len(database_backup.list_backups()['backups']), 2)

    def test_restore_refuses_even_a_job_that_has_not_entered_the_queue_yet(self):
        backup = database_backup.create_backup()
        with workbench.jobs_lock:
            workbench.jobs['test-waiting'] = {'state':'waiting'}
        try:
            with self.assertRaisesRegex(ValueError, '工作台任务'):
                workbench._restore_guarded(backup['name'])
        finally:
            with workbench.jobs_lock:
                workbench.jobs.pop('test-waiting', None)

    def test_scheduler_waits_until_due_and_prevents_duplicate_claims(self):
        with patch('app_config.time.time', return_value=1000):
            app_config.set_interval(30)
        self.assertIsNone(app_config.claim_due_sync(now=2799))
        claim = app_config.claim_due_sync(now=2800)
        self.assertEqual(claim['aliases'], ['xhs01'])
        self.assertIsNone(app_config.claim_due_sync(now=5000))
        with patch('app_config.time.time',return_value=5000):
            app_config.finish_sync(claim['token'], 'complete', 'done')
        self.assertIsNone(app_config.claim_due_sync(now=6799))
        self.assertIsNotNone(app_config.claim_due_sync(now=6800))
        with self.assertRaises(ValueError):
            app_config.set_interval(True)

    def test_long_queued_sync_waits_a_full_interval_after_finishing(self):
        with patch('app_config.time.time',return_value=1000):
            app_config.set_interval(30)
        claim = app_config.claim_due_sync(now=2800)
        self.assertIsNone(app_config.claim_due_sync(now=10000))
        with patch('app_config.time.time',return_value=10000):
            app_config.finish_sync(claim['token'], 'attention', 'done')
        self.assertEqual(app_config.settings()['schedule']['next_run'], 11800)
        self.assertIsNone(app_config.claim_due_sync(now=11799))
        self.assertIsNotNone(app_config.claim_due_sync(now=11800))

    def test_finishing_obeys_interval_changes_and_ignores_stale_tokens(self):
        with patch('app_config.time.time',return_value=1000):
            app_config.set_interval(30)
        claim = app_config.claim_due_sync(now=2800)
        with patch('app_config.time.time',return_value=3000):
            app_config.set_interval(60)
            app_config.finish_sync('stale-token', 'complete', 'ignored')
            self.assertEqual(app_config.settings()['schedule']['state'], 'running')
            app_config.finish_sync(claim['token'], 'complete', 'done')
        self.assertEqual(app_config.settings()['schedule']['next_run'], 6600)
        claim = app_config.claim_due_sync(now=6600)
        with patch('app_config.time.time',return_value=6700):
            app_config.set_interval(0)
            app_config.finish_sync(claim['token'], 'cancelled', 'cancelled')
        self.assertEqual(app_config.settings()['schedule']['next_run'], 0)
        self.assertIsNone(app_config.claim_due_sync(now=100000))

    def test_interrupted_old_run_is_not_retried_immediately(self):
        with patch('app_config.time.time',return_value=1000):
            app_config.set_interval(30)
        app_config.claim_due_sync(now=2800)
        with patch('app_config._alive',return_value=False):
            self.assertIsNone(app_config.claim_due_sync(now=10000))
        schedule = app_config.settings()['schedule']
        self.assertEqual(schedule['state'],'interrupted')
        self.assertEqual(schedule['next_run'],11800)
        self.assertIsNone(app_config.claim_due_sync(now=11799))
        self.assertIsNotNone(app_config.claim_due_sync(now=11800))

    def test_orphaned_worker_waits_before_another_attempt(self):
        with patch('app_config.time.time',return_value=1000):
            app_config.set_interval(30)
        app_config.claim_due_sync(now=2800)
        with patch('app_config.time.time',return_value=10000):
            app_config.recover_orphaned_run(set())
        schedule = app_config.settings()['schedule']
        self.assertEqual(schedule['state'],'attention')
        self.assertEqual(schedule['next_run'],11800)
        self.assertIsNone(app_config.claim_due_sync(now=11799))
        self.assertIsNotNone(app_config.claim_due_sync(now=11800))

    def test_scheduler_does_not_silently_skip_accounts_over_limit(self):
        for number in range(2, 12):
            alias = f'xhs{number:02}'
            add_account(alias, 'xiaohongshu')
            save_account_identity(alias, alias, f'{number:064x}')
        with patch('app_config.time.time', return_value=1000):
            app_config.set_interval(30)
        self.assertIsNone(app_config.claim_due_sync(now=2800))
        schedule = app_config.settings()['schedule']
        self.assertEqual(schedule['state'], 'attention')
        self.assertIn('超过 10', schedule['detail'])

    def test_scheduled_sync_reports_partial_results(self):
        finished = threading.Event()
        with patch('app_config.time.time', return_value=1000):
            app_config.set_interval(30)
        original_finish = app_config.finish_sync
        def record(*args):
            original_finish(*args)
            finished.set()
        def fake_sync(aliases, **kwargs):
            kwargs['on_queue']('running', '正在同步')
            return {'accounts':[{'alias':aliases[0], 'state':'partial'}]}
        with patch('app_config.time.time', return_value=2800), patch('inbox.sync_accounts', fake_sync), \
             patch('app_config.finish_sync', side_effect=record):
            workbench._scheduler_tick()
            self.assertTrue(finished.wait(5))
        self.assertEqual(app_config.settings()['schedule']['state'], 'attention')

    def test_manual_and_scheduled_duplicate_sync_share_one_job(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def fake_sync(aliases, **kwargs):
            calls.append(tuple(aliases))
            kwargs['on_queue']('waiting','正在排队')
            entered.set()
            if not release.wait(5): raise TimeoutError('test did not release sync')
            kwargs['on_queue']('running','正在同步')
            return {'accounts':[{'alias':aliases[0],'state':'complete'}]}
        original_finish = app_config.finish_sync
        def record(*args):
            original_finish(*args)
            finished.set()
        with patch('app_config.time.time',return_value=1000): app_config.set_interval(30)
        try:
            with patch('inbox.sync_accounts',fake_sync), patch('app_config.finish_sync',side_effect=record):
                first = workbench.start_sync_job(['xhs01'])
                self.assertTrue(entered.wait(5))
                second = workbench.start_sync_job(['xhs01'])
                self.assertEqual(second,{'job':first['job'],'merged':True})
                with patch('app_config.time.time',return_value=2800): workbench._scheduler_tick()
                self.assertEqual(calls,[('xhs01',)])
                release.set()
                self.assertTrue(finished.wait(5))
        finally:
            release.set()
        self.assertEqual(app_config.settings()['schedule']['state'],'complete')

    def test_requests_during_running_sync_coalesce_into_one_followup(self):
        first_started, second_waiting, release, finished = (threading.Event() for _ in range(4))
        exclusive = threading.Lock()
        calls = []
        def fake_sync(aliases, **kwargs):
            kwargs['on_queue']('waiting','排队')
            if first_started.is_set(): second_waiting.set()
            with exclusive:
                calls.append(tuple(aliases))
                kwargs['on_queue']('running','同步')
                if len(calls)==1:
                    first_started.set()
                    if not release.wait(5): raise TimeoutError('test did not release sync')
                else:
                    finished.set()
                return {'accounts':[{'alias':aliases[0],'state':'complete'}]}
        try:
            with patch('inbox.sync_accounts',fake_sync):
                first = workbench.start_sync_job(['xhs01'])
                self.assertTrue(first_started.wait(5))
                followup = workbench.start_sync_job(['xhs01'])
                self.assertNotEqual(first['job'],followup['job'])
                self.assertTrue(second_waiting.wait(5))
                merged = workbench.start_sync_job(['xhs01'])
                self.assertEqual(merged,{'job':followup['job'],'merged':True})
                release.set()
                self.assertTrue(finished.wait(5))
        finally:
            release.set()
        self.assertEqual(calls,[('xhs01',),('xhs01',)])

    def test_scheduler_recovers_if_completion_state_cannot_be_recorded(self):
        failed = threading.Event()
        def fake_sync(aliases, **kwargs):
            kwargs['on_queue']('running','同步')
            return {'accounts':[{'alias':aliases[0],'state':'complete'}]}
        def fail_finish(*args):
            failed.set()
            raise OSError('临时写入失败')
        with patch('app_config.time.time',return_value=1000): app_config.set_interval(30)
        with patch('inbox.sync_accounts',fake_sync), patch('app_config.finish_sync',side_effect=fail_finish):
            with patch('app_config.time.time',return_value=2800): workbench._scheduler_tick()
            self.assertTrue(failed.wait(5))
        deadline = time.monotonic()+5
        while workbench._scheduler_tokens and time.monotonic()<deadline:
            threading.Event().wait(.02)
        with patch('app_config.time.time',return_value=2801): workbench._scheduler_tick()
        status = app_config.settings()['schedule']
        self.assertEqual(status['state'],'attention')
        self.assertIn('未能记录结果',status['detail'])


if __name__ == '__main__':
    unittest.main()
