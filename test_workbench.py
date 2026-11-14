import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from publisher import add_account, batch_status, browser_lock, db, list_accounts, run_batch, save_account_identity, save_account_nickname, set_state
from workbench import app, start_job


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, {'OWN_PUBLISHER_HOME': self.folder.name})
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app, base_url='http://127.0.0.1:8765')
        self.client.get('/')
        self.headers = {'origin': 'http://127.0.0.1:8765'}
        add_account('xhs01', 'xiaohongshu')
        save_account_identity('xhs01', '', '1' * 64)
        add_account('dy01', 'douyin')

    def test_upload_preview_and_manual_reconciliation(self):
        content = io.BytesIO()
        Image.new('RGB', (3, 4), 'red').save(content, format='PNG')
        response = self.client.post('/api/images', headers=self.headers,
                                    files={'files': ('image.png', content.getvalue(), 'image/png')})
        self.assertEqual(response.status_code, 200, response.text)
        identifier = response.json()['images'][0]['id']
        draft = {'post': {'title': '测试', 'body': '正文', 'images': [identifier]}, 'targets': ['xhs01']}
        response = self.client.post('/api/preview', headers=self.headers, json=draft)
        self.assertEqual(response.status_code, 200, response.text)
        batch = response.json()['batch']
        self.assertEqual(response.json()['tasks'][0]['post']['body'], '正文')
        self.assertEqual(len(self.client.get('/api/batches').json()['batches']), 1)
        set_state(batch, 'xhs01', 'uncertain', '等待核对')
        response = self.client.post(f'/api/batches/{batch}/reconcile', headers=self.headers,
                                    json={'alias': 'xhs01', 'outcome': 'submitted'})
        self.assertEqual(response.json()['tasks'][0]['state'], 'submitted')
        self.assertEqual(self.client.post(f'/api/batches/{batch}/reconcile', headers=self.headers,
                         json={'alias': 'xhs01', 'outcome': 'failed'}).status_code, 400)

    def test_blocks_cross_origin_bad_images_and_non_xhs(self):
        self.assertEqual(self.client.post('/api/accounts', json={'alias': 'z'}).status_code, 403)
        self.assertEqual(self.client.post('/api/images', headers=self.headers,
                         files={'files': ('fake.png', b'not an image', 'image/png')}).status_code, 400)
        self.assertEqual(self.client.post('/api/preview', headers=self.headers,
                         json={'post': {'images': []}, 'targets': ['dy01']}).status_code, 400)
        self.assertEqual(len(self.client.get('/api/accounts').json()['accounts']), 1)

    def test_last_verified_nickname_preserves_account_label(self):
        before = list_accounts()
        save_account_nickname('xhs01', '平台昵称')
        accounts = self.client.get('/api/accounts').json()['accounts']
        self.assertEqual(accounts[0]['nickname'], '平台昵称')
        self.assertEqual(accounts[0]['label'], 'xhs01')
        self.assertEqual(list_accounts(), before)
        save_account_nickname('xhs01', '更新后的昵称')
        self.assertEqual(self.client.get('/api/accounts').json()['accounts'][0]['nickname'], '更新后的昵称')
        content = io.BytesIO()
        Image.new('RGB', (3, 4), 'red').save(content, format='PNG')
        image = self.client.post('/api/images', headers=self.headers,
                                 files={'files': ('image.png', content.getvalue(), 'image/png')}).json()['images'][0]['id']
        draft = {'post': {'title': '账号核对', 'images': [image]}, 'targets': ['xhs01']}
        preview = self.client.post('/api/preview', headers=self.headers, json=draft).json()
        self.assertEqual(preview['tasks'][0]['nickname'], '更新后的昵称')
        self.assertEqual(self.client.get('/api/batches/' + preview['batch']).json()['tasks'][0]['nickname'], '更新后的昵称')

    def test_preview_keeps_verified_identity_snapshot(self):
        save_account_identity('xhs01', '原账号', '1' * 64)
        content = io.BytesIO()
        Image.new('RGB', (3, 4), 'red').save(content, format='PNG')
        image = self.client.post('/api/images', headers=self.headers,
                                 files={'files': ('image.png', content.getvalue(), 'image/png')}).json()['images'][0]['id']
        draft = {'post': {'title': '身份快照', 'images': [image]}, 'targets': ['xhs01']}
        first = self.client.post('/api/preview', headers=self.headers, json=draft).json()
        self.assertTrue(first['tasks'][0]['identity_verified'])
        self.assertEqual(first['tasks'][0]['nickname'], '原账号')
        save_account_identity('xhs01', '另一个账号', '2' * 64)
        old = self.client.get('/api/batches/' + first['batch']).json()
        self.assertEqual(old['tasks'][0]['nickname'], '原账号')
        second = self.client.post('/api/preview', headers=self.headers, json=draft).json()
        self.assertNotEqual(first['batch'], second['batch'])
        self.assertEqual(second['tasks'][0]['nickname'], '另一个账号')
        self.assertNotIn('identity_hash', second['tasks'][0])

    def test_account_notes_and_separate_content_and_timing(self):
        add_account('xhs02', 'xiaohongshu', '第二个账号')
        save_account_identity('xhs02', '第二个昵称', '2' * 64)
        note = self.client.post('/api/accounts/xhs01/note', headers=self.headers, json={'note': '  生活号  '})
        self.assertEqual(note.json()['note'], '生活号')
        accounts = self.client.get('/api/accounts').json()['accounts']
        self.assertEqual(accounts[0]['note'], '生活号')
        self.assertIsNotNone(accounts[0]['checked_at'])
        self.assertEqual(self.client.post('/api/accounts/xhs01/note', headers=self.headers,
                         json={'note': 'x' * 121}).status_code, 400)
        self.assertEqual(self.client.post('/api/accounts/missing/note', headers=self.headers,
                         json={'note': 'hello'}).status_code, 400)

        content = io.BytesIO()
        Image.new('RGB', (3, 4), 'red').save(content, format='PNG')
        image = self.client.post('/api/images', headers=self.headers,
                                 files={'files': ('image.png', content.getvalue(), 'image/png')}).json()['images'][0]['id']
        schedule = (datetime.now() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        draft = {'post': {'title': '共用标题', 'body': '共用正文', 'images': [image], 'schedule': schedule},
                 'targets': ['xhs01', {'account': 'xhs02', 'post': {
                     'title': '第二个标题', 'body': '第二段正文', 'tags': ['话题'], 'schedule': None}}]}
        response = self.client.post('/api/preview', headers=self.headers, json=draft)
        self.assertEqual(response.status_code, 200, response.text)
        first, second = response.json()['tasks']
        self.assertEqual(first['post']['schedule'], schedule)
        self.assertEqual(second['post']['title'], '第二个标题')
        self.assertEqual(second['post']['body'], '第二段正文')
        self.assertEqual(second['post']['tags'], ['话题'])
        self.assertNotIn('schedule', second['post'])
        self.assertEqual(first['post']['images'], second['post']['images'])
        draft['targets'][1]['post']['images'] = [image]
        self.assertEqual(self.client.post('/api/preview', headers=self.headers, json=draft).status_code, 400)

    def test_interrupted_tasks_require_manual_reconciliation(self):
        content = io.BytesIO()
        Image.new('RGB', (3, 4), 'red').save(content, format='PNG')
        image = self.client.post('/api/images', headers=self.headers,
                                 files={'files': ('image.png', content.getvalue(), 'image/png')}).json()['images'][0]['id']
        draft = {'post': {'title': '恢复检查', 'images': [image]}, 'targets': ['xhs01']}
        batch = self.client.post('/api/preview', headers=self.headers, json=draft).json()['batch']
        set_state(batch, 'xhs01', 'running')
        with browser_lock():
            self.assertEqual(self.client.get(f'/api/batches/{batch}').json()['tasks'][0]['state'], 'running')
        self.assertEqual(self.client.get(f'/api/batches/{batch}').json()['tasks'][0]['state'], 'uncertain')
        set_state(batch, 'xhs01', 'verifying', '等待验证')
        self.assertEqual(self.client.get(f'/api/batches/{batch}').json()['tasks'][0]['state'], 'uncertain')
        with db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM verification_signals WHERE batch=?', (batch,)).fetchone()[0], 0)
        self.assertEqual(self.client.post(f'/api/batches/{batch}/continue', headers=self.headers, json={}).status_code, 400)

    def test_platform_match_cannot_be_reconciled_to_failed_or_republished(self):
        content = io.BytesIO()
        Image.new('RGB', (3, 4), 'red').save(content, format='PNG')
        image = self.client.post('/api/images', headers=self.headers,
                                 files={'files': ('image.png', content.getvalue(), 'image/png')}).json()['images'][0]['id']
        draft = {'post': {'title': '平台已找到', 'images': [image]}, 'targets': ['xhs01']}
        batch = self.client.post('/api/preview', headers=self.headers, json=draft).json()['batch']
        set_state(batch, 'xhs01', 'uncertain')
        with db() as con:
            con.execute('INSERT INTO platform_observations(batch,alias,note_id,status) VALUES (?,?,?,?)',
                        (batch, 'xhs01', 'existing-note', 'published'))
        response = self.client.post(f'/api/batches/{batch}/reconcile', headers=self.headers,
                                    json={'alias': 'xhs01', 'outcome': 'failed'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(batch_status(batch)['tasks'][0]['state'], 'uncertain')
        self.assertEqual(batch_status(batch)['tasks'][0]['platform_result']['note_id'], 'existing-note')

        # Even a stale failed state created elsewhere cannot bypass the shared engine guard.
        set_state(batch, 'xhs01', 'failed', '旧入口留下的状态')
        calls = []
        result = run_batch(batch, batch, retry=True, adapter=lambda *args: calls.append(args))
        self.assertEqual(calls, [])
        self.assertEqual(result['tasks'][0]['state'], 'uncertain')
        self.assertEqual(result['tasks'][0]['platform_result']['note_id'], 'existing-note')
        response = self.client.post(f'/api/batches/{batch}/reconcile', headers=self.headers,
                                    json={'alias': 'xhs01', 'outcome': 'submitted'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['tasks'][0]['state'], 'submitted')

    def test_user_can_resume_active_verification(self):
        waiting = threading.Event()
        finished = threading.Event()

        def worker(update, acknowledge):
            update('需要完成平台验证')
            waiting.set()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if acknowledge():
                        update(None)
                        return {'resumed': True}
                    time.sleep(0.01)
                raise TimeoutError('verification not acknowledged')
            finally:
                finished.set()

        started = start_job(worker, batch='example')
        self.assertTrue(waiting.wait(2))
        status = self.client.get('/api/jobs/' + started['job']).json()
        self.assertTrue(status['verification'])
        self.assertNotIn('_acknowledged', status)
        response = self.client.post('/api/batches/example/continue', headers=self.headers, json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(finished.wait(3))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = self.client.get('/api/jobs/' + started['job']).json()
            if status['state'] != 'running':
                break
            time.sleep(0.01)
        self.assertEqual(status['state'], 'done')
        self.assertTrue(status['result']['resumed'])
        self.assertEqual(self.client.post('/api/batches/example/continue', headers=self.headers, json={}).status_code, 400)

    def test_user_can_cancel_an_account_login_job(self):
        waiting = threading.Event()

        def fake_account_session(alias, login=False, cancel_requested=None, on_queue=None):
            if on_queue:
                on_queue('running', '正在使用浏览器。')
            waiting.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if cancel_requested():
                    return {'account': alias, 'state': 'cancelled', 'detail': '已取消'}
                time.sleep(0.01)
            raise TimeoutError('login was not cancelled')

        with patch('workbench.account_session', fake_account_session):
            started = self.client.post('/api/accounts/xhs01/login', headers=self.headers, json={}).json()
            self.assertTrue(waiting.wait(2))
            self.assertEqual(self.client.post('/api/jobs/' + started['job'] + '/cancel', headers=self.headers).status_code, 200)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                job = self.client.get('/api/jobs/' + started['job']).json()
                if job['state'] != 'running':
                    break
                time.sleep(0.02)
        self.assertEqual(job['result']['state'], 'cancelled')

    def test_batch_holds_task_and_resumes_after_acknowledgement(self):
        content = io.BytesIO()
        Image.new('RGB', (3, 4), 'red').save(content, format='PNG')
        image = self.client.post('/api/images', headers=self.headers,
                                 files={'files': ('image.png', content.getvalue(), 'image/png')}).json()['images'][0]['id']
        draft = {'post': {'title': '续接检查', 'images': [image]}, 'targets': ['xhs01']}
        batch = self.client.post('/api/preview', headers=self.headers, json=draft).json()['batch']
        waiting = threading.Event()
        calls = []

        def fake_publish(account, post, on_verification=None, verification_acknowledged=None, verification_cancelled=None):
            calls.append(account['alias'])
            on_verification('请处理验证')
            waiting.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if verification_acknowledged():
                    on_verification(None)
                    return
                time.sleep(0.01)
            raise TimeoutError('verification not acknowledged')

        with patch('browser.publish', fake_publish):
            response = self.client.post(f'/api/batches/{batch}/publish', headers=self.headers,
                                        json={'confirm': batch})
            self.assertEqual(response.status_code, 200)
            identifier = response.json()['job']
            self.assertTrue(waiting.wait(2))
            current = self.client.get(f'/api/batches/{batch}').json()['tasks'][0]
            self.assertEqual(current['state'], 'verifying')
            self.assertEqual(self.client.post(f'/api/batches/{batch}/continue', headers=self.headers).status_code, 200)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = self.client.get('/api/jobs/' + identifier).json()
                if status['state'] != 'running':
                    break
                time.sleep(0.02)
            self.assertEqual(status['state'], 'done')
        self.assertEqual(calls, ['xhs01'])
        self.assertEqual(self.client.get(f'/api/batches/{batch}').json()['tasks'][0]['state'], 'submitted')

    def test_user_can_cancel_a_waiting_batch(self):
        from browser import VerificationStopped

        content = io.BytesIO()
        Image.new('RGB', (3, 4), 'red').save(content, format='PNG')
        image = self.client.post('/api/images', headers=self.headers,
                                 files={'files': ('image.png', content.getvalue(), 'image/png')}).json()['images'][0]['id']
        draft = {'post': {'title': '取消验证', 'images': [image]}, 'targets': ['xhs01']}
        batch = self.client.post('/api/preview', headers=self.headers, json=draft).json()['batch']
        waiting = threading.Event()

        def fake_publish(account, post, on_verification=None, verification_acknowledged=None, verification_cancelled=None):
            on_verification('等待验证')
            waiting.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if verification_cancelled():
                    raise VerificationStopped('已取消等待验证。')
                time.sleep(0.01)
            raise TimeoutError('verification was not cancelled')

        with patch('browser.publish', fake_publish):
            job_id = self.client.post(f'/api/batches/{batch}/publish', headers=self.headers,
                                      json={'confirm': batch}).json()['job']
            self.assertTrue(waiting.wait(2))
            response = self.client.post(f'/api/batches/{batch}/cancel-verification', headers=self.headers, json={})
            self.assertEqual(response.status_code, 200, response.text)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = self.client.get('/api/jobs/' + job_id).json()
                if job['state'] != 'running':
                    break
                time.sleep(0.02)
        self.assertEqual(job['state'], 'done')
        self.assertEqual(self.client.get(f'/api/batches/{batch}').json()['tasks'][0]['state'], 'cancelled')

    def test_workbench_can_signal_a_task_started_elsewhere(self):
        content = io.BytesIO()
        Image.new('RGB', (3, 4), 'red').save(content, format='PNG')
        image = self.client.post('/api/images', headers=self.headers,
                                 files={'files': ('image.png', content.getvalue(), 'image/png')}).json()['images'][0]['id']
        draft = {'post': {'title': '跨入口续接', 'images': [image]}, 'targets': ['xhs01']}
        batch = self.client.post('/api/preview', headers=self.headers, json=draft).json()['batch']
        script = r'''
import sys, time
from publisher import browser_lock, consume_verification_ack, set_state
batch = sys.argv[1]
with browser_lock():
    set_state(batch, "xhs01", "verifying", "等待验证")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if consume_verification_ack(batch, "xhs01"):
            set_state(batch, "xhs01", "uncertain", "本地测试结束")
            print("received")
            break
        time.sleep(0.05)
    else:
        raise TimeoutError("verification was not acknowledged")
'''
        worker = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script, batch],
                                  cwd=Path(__file__).parent, env=os.environ.copy(),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        self.addCleanup(lambda: worker.kill() if worker.poll() is None else None)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and worker.poll() is None:
            if batch_status(batch)['tasks'][0]['state'] == 'verifying':
                break
            time.sleep(0.05)
        self.assertEqual(batch_status(batch)['tasks'][0]['state'], 'verifying')
        response = self.client.post(f'/api/batches/{batch}/continue', headers=self.headers, json={})
        self.assertEqual(response.status_code, 200, response.text)
        out, err = worker.communicate(timeout=10)
        self.assertEqual(worker.returncode, 0, err)
        self.assertEqual(out.strip(), 'received')
        self.assertEqual(batch_status(batch)['tasks'][0]['state'], 'uncertain')


if __name__ == '__main__':
    unittest.main()
