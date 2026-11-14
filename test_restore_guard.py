import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image
from fastapi.testclient import TestClient

import database_backup
import restore_guard
import platform_sync
from workbench import app
from publisher import add_account, batch_status, prepare_batch_data, run_batch, save_account_identity


class RestoreGuardTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, {'OWN_PUBLISHER_HOME': self.folder.name})
        env.start()
        self.addCleanup(env.stop)
        processes = patch('database_backup._other_project_processes',return_value=[])
        processes.start()
        self.addCleanup(processes.stop)
        add_account('xhs01','xiaohongshu')
        save_account_identity('xhs01','demo','1'*64)
        self.picture = Path(self.folder.name)/'image.png'
        Image.new('RGB',(4,4),'red').save(self.picture)

    def batch(self, title='原内容', **options):
        post = {'title':title,'body':'正文','tags':[],'images':[str(self.picture)]} | options
        return prepare_batch_data({'post':post,'targets':['xhs01']}, Path(self.folder.name))['batch']

    def review(self, generation, notes=None):
        restore_guard.record_audit(generation,'xhs01','1'*64,notes or [])
        restore_guard.confirm_review(generation)

    def test_restoring_old_backup_keeps_same_batch_and_changed_options_blocked(self):
        batch = self.batch()
        old = database_backup.create_backup()['name']
        sent = Mock()
        run_batch(batch,batch,adapter=sent)
        self.assertEqual(sent.call_count,1)
        result = database_backup.restore_backup(old)
        self.assertEqual(batch_status(batch)['tasks'][0]['state'],'queued')
        with self.assertRaisesRegex(ValueError,'核对平台'):
            run_batch(batch,batch,adapter=Mock())
        with self.assertRaisesRegex(ValueError,'所有账号'):
            restore_guard.confirm_review(result['generation'])
        self.review(result['generation'])
        duplicate = Mock()
        self.assertEqual(run_batch(batch,batch,adapter=duplicate)['tasks'][0]['state'],'duplicate')
        duplicate.assert_not_called()
        changed = self.batch(declaration='ai')
        self.assertEqual(run_batch(changed,changed,allow_duplicate_accounts=['xhs01'],adapter=duplicate)['tasks'][0]['state'],'duplicate')
        duplicate.assert_not_called()

    def test_platform_title_collision_requires_separate_explicit_confirmation(self):
        old = database_backup.create_backup()['name']
        result = database_backup.restore_backup(old)
        self.review(result['generation'], [{'id':'note1','display_title':'平台已有标题','tab_status':1}])
        batch = self.batch('平台已有标题')
        called = Mock()
        self.assertEqual(run_batch(batch,batch,adapter=called)['tasks'][0]['state'],'duplicate')
        called.assert_not_called()
        self.assertEqual(run_batch(batch,batch,retry=True,allow_duplicate_accounts=['xhs01'],adapter=called)['tasks'][0]['state'],'submitted')
        self.assertEqual(called.call_count,1)

    def test_reaudit_retains_previously_seen_platform_titles(self):
        backup = database_backup.create_backup()['name']
        result = database_backup.restore_backup(backup)
        self.review(result['generation'],[{'id':'note1','display_title':'原平台标题','tab_status':1}])
        restore_guard.record_audit(result['generation'],'xhs01','1'*64,
                                   [{'id':'note1','display_title':'修改后标题','tab_status':1}])
        old = self.batch('原平台标题')
        self.assertEqual(run_batch(old,old,adapter=Mock())['tasks'][0]['state'],'duplicate')

    def test_retained_record_cannot_be_overridden_when_also_in_restored_database(self):
        original = self.batch()
        run_batch(original,original,adapter=Mock())
        backup = database_backup.create_backup()['name']
        restored = database_backup.restore_backup(backup)
        self.review(restored['generation'])
        changed = self.batch(declaration='ai')
        called = Mock()
        result = run_batch(changed,changed,allow_duplicate_accounts=['xhs01'],adapter=called)
        self.assertEqual(result['tasks'][0]['state'],'duplicate')
        called.assert_not_called()

    def test_repeated_restores_keep_the_original_submission_guard(self):
        batch = self.batch()
        backup = database_backup.create_backup()['name']
        run_batch(batch,batch,adapter=Mock())
        first = database_backup.restore_backup(backup)
        self.review(first['generation'])
        second = database_backup.restore_backup(backup)
        self.review(second['generation'])
        called = Mock()
        self.assertEqual(run_batch(batch,batch,adapter=called)['tasks'][0]['state'],'duplicate')
        called.assert_not_called()

    def test_restore_failure_rolls_back_database_but_keeps_review_gate(self):
        batch = self.batch()
        backup = database_backup.create_backup()['name']
        run_batch(batch,batch,adapter=Mock())
        real_validate = database_backup._validate
        calls = []
        def fail_after_copy(path):
            calls.append(path)
            if len(calls)==3: raise ValueError('模拟恢复后校验失败')
            return real_validate(path)
        with patch.object(database_backup,'_validate',side_effect=fail_after_copy):
            with self.assertRaisesRegex(ValueError,'已回退'):
                database_backup.restore_backup(backup)
        self.assertEqual(batch_status(batch)['tasks'][0]['state'],'submitted')
        self.assertTrue(restore_guard.status()['pending'])
        with self.assertRaisesRegex(ValueError,'核对平台'):
            run_batch(batch,batch,adapter=Mock())

    def test_new_identity_after_restore_must_be_audited_before_publishing(self):
        old = database_backup.create_backup()['name']
        result = database_backup.restore_backup(old)
        self.review(result['generation'])
        save_account_identity('xhs01','different','2'*64)
        batch = self.batch('新身份测试')
        with self.assertRaisesRegex(ValueError,'尚未核对'):
            run_batch(batch,batch,adapter=Mock())
        restore_guard.record_audit(result['generation'],'xhs01','2'*64,[])
        self.assertEqual(run_batch(batch,batch,adapter=Mock())['tasks'][0]['state'],'submitted')

    def test_missing_guard_file_fails_closed_after_restore(self):
        old = database_backup.create_backup()['name']
        result = database_backup.restore_backup(old)
        self.review(result['generation'])
        sidecar = Path(self.folder.name)/'restore-guard.sqlite3'
        saved = sidecar.with_suffix('.saved')
        sidecar.rename(saved)
        with self.assertRaisesRegex(ValueError,'不一致'):
            restore_guard.require_review_complete()
        with self.assertRaisesRegex(ValueError,'防重复记录缺失'):
            database_backup.restore_backup(old)

    def test_review_api_requires_local_session_and_complete_account_audits(self):
        backup = database_backup.create_backup()['name']
        generation = database_backup.restore_backup(backup)['generation']
        client = TestClient(app,base_url='http://127.0.0.1:8765')
        self.assertEqual(client.get('/api/restore-guard').status_code,403)
        client.get('/')
        headers={'origin':'http://127.0.0.1:8765'}
        self.assertTrue(client.get('/api/restore-guard').json()['pending'])
        self.assertEqual(client.post('/api/restore-guard/confirm',headers=headers,
                          json={'generation':generation,'confirm':'reviewed'}).status_code,400)
        restore_guard.record_audit(generation,'xhs01','1'*64,[])
        self.assertEqual(client.post('/api/restore-guard/confirm',headers=headers,
                          json={'generation':generation,'confirm':'reviewed'}).status_code,200)
        self.assertFalse(client.get('/api/restore-guard').json()['pending'])

    def test_incomplete_platform_audit_never_unlocks_restore(self):
        backup = database_backup.create_backup()['name']
        generation = database_backup.restore_backup(backup)['generation']
        page = Mock()
        with patch.object(platform_sync,'creator_page',return_value=nullcontext(page)), \
             patch.object(platform_sync,'observe_xiaohongshu_identity',return_value={}), \
             patch.object(platform_sync,'observe_creator_notes',return_value=[]), \
             patch.object(platform_sync,'verify_origin'), patch.object(platform_sync,'check_challenge'), \
             patch.object(platform_sync,'require_xiaohongshu_identity'), \
             patch.object(platform_sync,'collect_creator_notes',side_effect=ValueError('列表未读完')):
            with self.assertRaisesRegex(ValueError,'未读完'):
                platform_sync.audit_restored_account('xhs01',generation)
        self.assertFalse(restore_guard.status()['accounts'][0]['audited'])
        with self.assertRaisesRegex(ValueError,'所有账号'):
            restore_guard.confirm_review(generation)

    def test_restore_refuses_when_another_publisher_process_is_running(self):
        backup = database_backup.create_backup()['name']
        with patch('database_backup._other_project_processes',return_value=[12345]):
            with self.assertRaisesRegex(ValueError,'Codex'):
                database_backup.restore_backup(backup)
        self.assertFalse(restore_guard.status()['pending'])

    def test_process_filter_excludes_self_and_fails_closed_on_unknown_command(self):
        root = str(Path(database_backup.__file__).resolve().parent)
        rows = [{'ProcessId':1,'CommandLine':root+' agent_server.py'},
                {'ProcessId':2,'CommandLine':root+' uvicorn workbench:app'},
                {'ProcessId':3,'CommandLine':root+' agent_server.py'},
                {'ProcessId':4,'CommandLine':'C:\\other\\unrelated.py'}]
        self.assertEqual(database_backup._project_processes(rows,1,2),[3])
        with self.assertRaisesRegex(ValueError,'无法检查'):
            database_backup._project_processes([{'ProcessId':9,'CommandLine':None}],1,2)


if __name__ == '__main__':
    unittest.main()
