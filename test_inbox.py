import os
import io
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from PIL import Image
import inbox
from publisher import add_account, db, save_account_identity
from workbench import app


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, {'OWN_PUBLISHER_HOME': self.folder.name})
        env.start()
        self.addCleanup(env.stop)
        for alias, identity in (('xhs01', '1' * 64), ('xhs02', '2' * 64)):
            add_account(alias, 'xiaohongshu')
            save_account_identity(alias, alias, identity)
        self.item = {'external_id': 'conversation', 'location': 'main', 'display_name': '客户甲',
                     'last_preview': '你好', 'last_time_label': '今天', 'unread_before': 1}
        self.message = {'external_id': 'message', 'store_id': '10', 'direction': 'incoming',
                        'content_type': '1', 'body': '你好，测试内容', 'time_label': '今天'}
        for alias, identity in (('xhs01', '1' * 64), ('xhs02', '2' * 64)):
            inbox._save_conversation(alias, identity, self.item, [self.message],
                                     {'message': 1794640000000}, True, 1)

    def test_deduplication_account_isolation_and_local_state(self):
        original = inbox.list_conversations()['conversations']
        self.assertEqual(len(original), 2)
        one = next(row for row in original if row['alias'] == 'xhs01')
        inbox.update_conversation(one['id'], status='processed', important=True)
        inbox._save_conversation('xhs01', '1' * 64, self.item, [self.message],
                                 {'message': 1794640000000}, True, 1)
        current = inbox.list_conversations(alias='xhs01')['conversations']
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['message_count'], 1)
        self.assertEqual(current[0]['status'], 'processed')
        self.assertTrue(current[0]['important'])
        self.assertEqual(inbox.list_conversations(status='important')['total'], 1)
        self.assertEqual(inbox.list_conversations(query='测试内容')['total'], 2)
        self.assertEqual(inbox.list_conversations(query='%')['total'], 0)
        self.assertEqual(inbox.conversation(one['id'])['messages'][0]['body'], '你好，测试内容')
        self.assertEqual(inbox.conversation(one['id'])['total'], 1)
        self.assertEqual(inbox.conversation(one['id'], limit=1, offset=1)['messages'], [])

    def test_changed_identity_cannot_read_or_write_old_conversations(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        save_account_identity('xhs01', 'new identity', '3' * 64)
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'], 0)
        with self.assertRaises(ValueError):
            inbox.conversation(one['id'])
        with self.assertRaises(inbox.IdentityMismatch):
            inbox._save_conversation('xhs01', '1' * 64, self.item, [self.message], {}, True, 1)
        self.assertEqual(inbox.list_conversations(alias='xhs02')['total'], 1)

    def test_mcp_stable_keys_survive_different_database_row_ids(self):
        import agent_server
        first = inbox.list_conversations(alias='xhs01')['conversations'][0]
        data = agent_server.inbox_messages(conversation_key=first['conversation_key'])
        message = data['messages'][0]
        self.assertTrue(message['message_id'].startswith('xhm1_'))
        self.assertEqual(message['platform_message_id'],'message')
        self.assertTrue(message['id_verified'])
        self.assertIsNone(message['content_complete'])
        self.assertIn('history_complete',data['conversation'])
        self.assertIn('history_pages',data['conversation'])
        self.assertEqual(message['timestamp_ms'],1794640000000)
        with tempfile.TemporaryDirectory() as other, patch.dict(os.environ,{'OWN_PUBLISHER_HOME':other}):
            add_account('xhs01','xiaohongshu')
            save_account_identity('xhs01','xhs01','1'*64)
            unrelated = self.item | {'external_id':'inserted-first'}
            inbox._save_conversation('xhs01','1'*64,unrelated,[self.message],{'message':1794640000000},True,1)
            inbox._save_conversation('xhs01','1'*64,self.item,[self.message],{'message':1794640000000},True,1)
            restored = next(x for x in inbox.list_conversations(alias='xhs01')['conversations']
                            if x['conversation_key']==first['conversation_key'])
            self.assertNotEqual(restored['id'],first['id'])
            again = agent_server.inbox_messages(conversation_key=restored['conversation_key'])
            self.assertEqual(again['messages'][0]['message_id'],message['message_id'])
            with self.assertRaises(ValueError):
                agent_server.inbox_messages(conversation_id=restored['id']+1,conversation_key=restored['conversation_key'])

    def test_stable_message_keys_are_scoped_to_conversation_and_identity(self):
        first = inbox.list_conversations(alias='xhs01')['conversations'][0]
        second = inbox.list_conversations(alias='xhs02')['conversations'][0]
        self.assertNotEqual(first['conversation_key'],second['conversation_key'])
        self.assertNotEqual(inbox.conversation(first['id'])['messages'][0]['message_id'],
                            inbox.conversation(second['id'])['messages'][0]['message_id'])
        save_account_identity('xhs01','new identity','3'*64)
        with self.assertRaises(ValueError):
            inbox.conversation_id_for_key(first['conversation_key'])

    def test_failed_account_does_not_prevent_later_account(self):
        with patch.object(inbox, 'sync_account', side_effect=[ValueError('请先检查账号'),
                              {'alias':'xhs02', 'state':'complete', 'conversations':1}]):
            result = inbox.sync_accounts(['xhs01', 'xhs02'])['accounts']
        self.assertEqual([r['state'] for r in result], ['failed', 'complete'])
        with self.assertRaises(ValueError):
            inbox.sync_accounts(['xhs01', 'xhs01'])

    def test_duplicate_aliases_for_same_platform_identity_are_not_synced_twice(self):
        add_account('xhs03', 'xiaohongshu')
        save_account_identity('xhs03', 'same', '1' * 64)
        with patch.object(inbox, 'sync_account', side_effect=[{'alias':'xhs01','state':'complete'},
                                                              {'alias':'xhs02','state':'complete'}]) as run:
            result = inbox.sync_accounts(['xhs01', 'xhs03', 'xhs02'])['accounts']
        self.assertEqual([r['state'] for r in result], ['complete', 'failed', 'complete'])
        self.assertEqual(run.call_count, 2)

    def test_ten_accounts_continue_after_one_requires_login(self):
        aliases = [f'xhs{number:02}' for number in range(1, 11)]
        for number in range(3, 11):
            add_account(f'xhs{number:02}', 'xiaohongshu')
            save_account_identity(f'xhs{number:02}', '', f'{number:064x}')
        called = []
        def simulate(alias, progress=None, **kwargs):
            called.append(alias)
            return {'alias':alias, 'state':'login_needed' if alias == 'xhs04' else 'complete', 'conversations':0}
        with patch.object(inbox, 'sync_account', side_effect=simulate):
            results = inbox.sync_accounts(aliases)['accounts']
        self.assertEqual(called, aliases)
        self.assertEqual(len(results), 10)
        self.assertEqual(results[3]['state'], 'login_needed')
        self.assertEqual(results[-1]['state'], 'complete')
        with self.assertRaises(ValueError):
            inbox.sync_accounts(aliases + ['xhs11'])

    def test_large_local_inbox_paginates_without_overlap(self):
        for number in range(120):
            item = self.item | {'external_id':f'person-{number}', '_last_timestamp':1794640000000+number}
            inbox._save_conversation('xhs01', '1' * 64, item, [self.message], {}, True, 1)
        first = inbox.list_conversations(alias='xhs01', limit=100)
        last = inbox.list_conversations(alias='xhs01', limit=100, offset=100)
        first_ids = {row['id'] for row in first['conversations']}
        last_ids = {row['id'] for row in last['conversations']}
        self.assertEqual(first['total'], 121)
        self.assertEqual((len(first_ids), len(last_ids)), (100, 21))
        self.assertFalse(first_ids.intersection(last_ids))

    def test_private_api_requires_local_session_and_rejects_foreign_origin(self):
        client = TestClient(app, base_url='http://127.0.0.1:8765')
        self.assertEqual(client.get('/api/inbox/conversations').status_code, 403)
        client.get('/')
        response = client.get('/api/inbox/conversations')
        self.assertEqual(response.status_code, 200)
        result = response.json()['conversations'][0]
        self.assertNotIn('identity_hash', result)
        self.assertNotIn('external_id', result)
        self.assertEqual(client.get('/api/inbox/conversations', headers={'origin':'https://example.com'}).status_code, 403)
        self.assertEqual(client.post('/api/inbox/sync', json={'accounts':['xhs01']}).status_code, 403)
        headers = {'origin':'http://127.0.0.1:8765'}
        self.assertEqual(client.post('/api/inbox/conversations/1/state', headers=headers,
                                     json={'important': True}).status_code, 200)
        self.assertEqual(client.post('/api/inbox/conversations/1/state', headers=headers,
                                     json={'important':'yes'}).status_code, 400)

    def test_history_result_does_not_claim_hidden_rows_complete(self):
        class FakePage:
            def wait_for_timeout(self, _):
                pass
        events = [{'ok': True, 'ids': {'visible', 'hidden'}, 'count': 2, 'limit': 30}]
        row = self.message | {'external_id': 'visible'}
        other = self.message | {'external_id': 'different-conversation'}
        with patch.object(inbox, '_read_messages', return_value=[row, other]):
            messages, complete, pages = inbox._finish_history(FakePage(), events, {}, False, 0, set(), 1)
        self.assertEqual(len(messages), 1)
        self.assertEqual(pages, 1)
        self.assertFalse(complete)

    def test_quick_sync_stops_at_known_history_and_full_sync_reads_older_page(self):
        def run(quick):
            events = [{'ok':True,'ids':{'new','known'},'ordered_ids':['new','known'],
                       'count':2,'limit':2,'invalid_ids':False}]
            rows = [self.message | {'external_id':'known'}, self.message | {'external_id':'new'}]
            older = self.message | {'external_id':'older'}
            class Scroll:
                first = None
                def __init__(self): self.first = self
                def count(self): return 1
                def evaluate(self, _):
                    events.append({'ok':True,'ids':{'older'},'ordered_ids':['older'],
                                   'count':1,'limit':2,'invalid_ids':False})
            class Page:
                def wait_for_timeout(self, _): pass
                def locator(self, _): return Scroll()
            def visible(_): return rows if len(events) == 1 else [older] + rows
            with patch.object(inbox, '_read_messages', side_effect=visible):
                return inbox._finish_history(Page(), events, {}, False, 0, {'known'}, 2, quick=quick)
        quick_rows, quick_complete, quick_pages = run(True)
        full_rows, full_complete, full_pages = run(False)
        self.assertEqual((len(quick_rows),quick_complete,quick_pages), (2,False,1))
        self.assertEqual((len(full_rows),full_complete,full_pages), (3,True,2))

    def test_open_window_can_be_cancelled_and_releases_next_browser_job(self):
        from contextlib import contextmanager
        from playwright.sync_api import TimeoutError as BrowserTimeout
        from browser_queue import QueueCancelled, queue_status, queued_browser
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        opened, cancel_open, closed, next_started = (threading.Event() for _ in range(4))
        outcomes = []
        page = MagicMock()
        page.url = 'https://www.xiaohongshu.com/chat/conversation'
        page.is_closed.return_value = False
        def wait_close(*args, **kwargs):
            opened.set()
            cancel_open.wait(3)
            raise BrowserTimeout('still open')
        page.wait_for_event.side_effect = wait_close
        @contextmanager
        def fake_page(*args, **kwargs):
            try: yield page
            finally: closed.set()
        def open_task():
            try: inbox.open_conversation(one['id'], cancel_requested=cancel_open.is_set)
            except QueueCancelled: outcomes.append('cancelled')
        def next_task():
            with queued_browser('sync'):
                next_started.set()
        with patch.object(inbox, 'creator_page', fake_page), \
             patch.object(inbox, 'observe_xiaohongshu_identity', return_value={}), \
             patch.object(inbox, 'verify_origin'), patch.object(inbox, 'check_challenge'), \
             patch.object(inbox, 'require_xiaohongshu_identity'), patch.object(inbox, '_wait_for_list'), \
             patch.object(inbox, 'verify_web_identity'):
            first = threading.Thread(target=open_task)
            first.start()
            self.assertTrue(opened.wait(5))
            second = threading.Thread(target=next_task)
            second.start()
            for _ in range(50):
                if len(queue_status()['tasks']) == 2: break
                threading.Event().wait(.05)
            self.assertEqual([x['state'] for x in queue_status()['tasks']], ['running','waiting'])
            cancel_open.set()
            first.join(5)
            second.join(5)
        self.assertEqual(outcomes, ['cancelled'])
        self.assertTrue(closed.is_set())
        self.assertTrue(next_started.is_set())
        self.assertEqual(queue_status()['tasks'], [])

    def test_rendered_message_without_id_is_not_claimed_complete(self):
        class FakePage:
            def wait_for_timeout(self, _): pass
        events = [{'ok': True, 'ids': {'visible'}, 'count': 1, 'limit': 30}]
        with patch.object(inbox, '_read_messages', return_value=[self.message | {'external_id':'visible'},
                                                                  self.message | {'external_id':''}]):
            messages, complete, _ = inbox._finish_history(FakePage(), events, {}, False, 0, set(), 1)
        self.assertEqual(len(messages), 1)
        self.assertFalse(complete)

    def test_unmatched_rendered_message_cannot_advance_confirmation_baseline(self):
        class FakePage:
            def wait_for_timeout(self, _): pass
        for alias, identity, unmatched in (('xhs01','1'*64,'ui-only'), ('xhs02','2'*64,'')):
            with self.subTest(alias=alias, unmatched=unmatched):
                one = inbox.list_conversations(alias=alias)['conversations'][0]
                latest = self.message | {'external_id':'latest','store_id':'11'}
                extra = self.message | {'external_id':unmatched}
                events = [{'ok':True, 'ids':{'latest','message'}, 'ordered_ids':['latest','message'],
                           'count':2, 'limit':30, 'invalid_ids':False}]
                timestamps = {'message':1794640000000,'latest':1794640050000}
                item = self.item | {'_last_timestamp':1794640050000, '_max_store_id':11,
                                    '_list_update_time':1794640051000, 'unread_before':0}
                with patch.object(inbox,'_read_messages',return_value=[self.message,latest,extra]):
                    messages,complete,pages = inbox._finish_history(FakePage(),events,timestamps,True,0,{'message'},1)
                self.assertFalse(complete)
                self.assertTrue(events[0]['order_verified'])
                inbox._save_conversation(alias,identity,item,messages,timestamps,complete,pages,observations=events)
                result = inbox.conversation(one['id'])['conversation']
                self.assertEqual(result['new_count'],1)
                with db() as con:
                    baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',
                                           (one['id'],)).fetchone()[0]
                self.assertEqual(baseline,1794640000000)
                self.assertTrue(result['review_needed'])
                self.assertEqual(inbox._existing_history(alias,identity,self.item['external_id'])[2],{'message'})
                self.assertIsNone(inbox._reuse_unchanged_conversation(alias,identity,item))
                before = inbox.inbox_summary()
                inbox._save_conversation(alias,identity,item,messages,timestamps,complete,pages,observations=events)
                self.assertEqual(inbox.inbox_summary(),before)
                inbox.update_conversation(one['id'],status='processed',alerts_version=result['alerts_version'])
                inbox._save_conversation(alias,identity,item,messages,timestamps,complete,pages,observations=events)
                with db() as con:
                    baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',
                                           (one['id'],)).fetchone()[0]
                self.assertEqual(baseline,1794640000000)
                changed = item | {'_list_update_time':1794640052000}
                inbox._save_conversation(alias,identity,changed,messages,timestamps,complete,pages,observations=events)
                self.assertTrue(inbox.conversation(one['id'])['conversation']['review_needed'])

    def test_complete_current_window_advances_even_if_older_history_remains(self):
        class FakePage:
            def wait_for_timeout(self, _): pass
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        latest = self.message | {'external_id':'latest','store_id':'11'}
        events = [{'ok':True,'ids':{'latest','message'},'ordered_ids':['latest','message'],
                   'count':2,'limit':2,'invalid_ids':False}]
        timestamps = {'message':1794640000000,'latest':1794640050000}
        item = self.item | {'_last_timestamp':1794640050000,'_max_store_id':11,
                            '_list_update_time':1794640051000,'unread_before':0}
        with patch.object(inbox,'_read_messages',return_value=[self.message,latest]):
            messages,complete,pages = inbox._finish_history(FakePage(),events,timestamps,False,0,{'message'},1,quick=True)
        self.assertFalse(complete)
        self.assertTrue(events[0]['render_verified'])
        inbox._save_conversation('xhs01','1'*64,item,messages,timestamps,complete,pages,observations=events)
        current = inbox.conversation(one['id'])['conversation']
        self.assertEqual(current['new_count'],1)
        self.assertFalse(current['review_needed'])
        with db() as con:
            baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',
                                   (one['id'],)).fetchone()[0]
        self.assertEqual(baseline,1794640050000)
        self.assertIsNotNone(inbox._reuse_unchanged_conversation('xhs01','1'*64,item))

    def test_missing_render_verification_fails_closed(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        item = self.item | {'_last_timestamp':1794640050000}
        event = {'ok':True,'ids':{'message'},'ordered_ids':['message'],'order_verified':True,
                 'count':1,'limit':30,'invalid_ids':False}
        inbox._save_conversation('xhs01','1'*64,item,[self.message],{'message':1794640000000},
                                 True,1,observations=[event])
        with db() as con:
            baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',
                                   (one['id'],)).fetchone()[0]
        self.assertEqual(baseline,1794640000000)
        self.assertTrue(inbox.conversation(one['id'])['conversation']['review_needed'])

    def test_unstorable_message_id_does_not_claim_complete_history(self):
        valid = self.message | {'external_id':'valid'}
        invalid = self.message | {'external_id':'x' * 257}
        self.assertFalse(inbox._save_conversation('xhs01', '1' * 64, self.item,
                                                   [valid, invalid], {}, True, 1))
        row = inbox.list_conversations(alias='xhs01')['conversations'][0]
        self.assertFalse(row['history_complete'])
        self.assertEqual(inbox.conversation(row['id'])['total'], 2)

    def test_old_unverified_rows_are_preserved_but_not_displayed(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        with db() as con:
            inbox.ensure_schema(con)
            con.execute('''INSERT INTO inbox_messages(conversation_id,external_id,direction,content_type,body)
                           VALUES(?,?,?,?,?)''', (one['id'], 'old-unverified', 'incoming', '1', '隐藏测试'))
        self.assertEqual(inbox.conversation(one['id'])['total'], 1)
        self.assertEqual(inbox.list_conversations(query='隐藏测试')['total'], 0)
        complete = inbox._save_conversation('xhs01', '1' * 64, self.item, [self.message], {}, True, 1)
        self.assertFalse(complete)
        self.assertFalse(inbox.conversation(one['id'])['conversation']['history_complete'])
        with db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM inbox_messages WHERE conversation_id=?', (one['id'],)).fetchone()[0], 2)

    def test_failed_conversation_save_rolls_back_as_a_unit(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        changed = self.item | {'last_preview': 'should roll back'}
        broken = self.message | {'external_id': 'broken', 'body': None}
        with self.assertRaises(sqlite3.IntegrityError):
            inbox._save_conversation('xhs01', '1' * 64, changed, [self.message, broken], {}, True, 1)
        result = inbox.conversation(one['id'])
        self.assertEqual(result['conversation']['last_preview'], '你好')
        self.assertEqual(result['total'], 1)

    def test_new_incoming_reopens_processed_without_repeating_or_alerting_old_history(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        inbox.update_conversation(one['id'], status='processed')
        new = self.message | {'external_id':'new','body':'新的咨询'}
        old = self.message | {'external_id':'older','body':'补录的旧消息'}
        timestamps={'message':1794640000000,'new':1794640010000,'older':1794639990000}
        events=[{'ok':True,'ids':{'new','message','older'},'ordered_ids':['new','message','older'],
                 'order_verified':True,'render_verified':True,'count':3,'limit':30}]
        inbox._save_conversation('xhs01','1'*64,self.item,[old,self.message,new],timestamps,True,1,
                                 observations=events)
        current=inbox.conversation(one['id'])
        self.assertEqual(current['conversation']['status'],'pending')
        self.assertEqual(current['conversation']['new_count'],1)
        self.assertEqual(len(current['unseen_notifications']),1)
        inbox.update_conversation(one['id'],status='processed', alerts_version=current['conversation']['alerts_version'])
        inbox._save_conversation('xhs01','1'*64,self.item,[old,self.message,new],timestamps,True,1,
                                 observations=events)
        again=inbox.conversation(one['id'])
        self.assertEqual(again['conversation']['status'],'processed')
        self.assertEqual(again['conversation']['new_count'],0)

    def test_old_backfill_and_unverified_order_do_not_become_new_alerts(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        inbox.update_conversation(one['id'],status='processed')
        old = self.message | {'external_id':'older'}
        timestamps={'message':1794640000000,'older':1794639900000}
        events=[{'ok':True,'ids':{'message','older'},'ordered_ids':['message','older'],
                 'order_verified':True,'render_verified':True,'count':2,'limit':30}]
        inbox._save_conversation('xhs01','1'*64,self.item,[old,self.message],timestamps,True,1,
                                 observations=events)
        current=inbox.conversation(one['id'])
        self.assertEqual(current['conversation']['status'],'processed')
        self.assertEqual(current['conversation']['new_count'],0)
        ambiguous=[{'ok':True,'ids':{'message','unexpected'},'ordered_ids':['unexpected','message'],
                    'order_verified':False,'count':2,'limit':30}]
        unexpected=self.message | {'external_id':'unexpected'}
        inbox._save_conversation('xhs01','1'*64,self.item,[self.message,unexpected],
                                 {'message':1794640000000,'unexpected':1794640050000},False,1,
                                 observations=ambiguous)
        current=inbox.conversation(one['id'])
        self.assertEqual(current['conversation']['new_count'],0)
        self.assertTrue(current['conversation']['review_needed'])
        self.assertEqual(current['conversation']['status'], 'pending')
        inbox.update_conversation(one['id'], status='processed', alerts_version=current['conversation']['alerts_version'])
        inbox._save_conversation('xhs01','1'*64,self.item,[self.message,unexpected],
                                 {'message':1794640000000,'unexpected':1794640050000},False,1,
                                 observations=ambiguous)
        repeated = inbox.conversation(one['id'])
        self.assertEqual(repeated['conversation']['status'], 'processed')
        self.assertFalse(repeated['conversation']['review_needed'])

    def test_acknowledging_visible_snapshot_preserves_later_alerts_and_other_accounts(self):
        first = inbox.list_conversations(alias='xhs01')['conversations'][0]
        second = inbox.list_conversations(alias='xhs02')['conversations'][0]
        def add(alias, identity, mid, stamp, known):
            message = self.message | {'external_id':mid}
            events = [{'ok':True, 'ids':{mid, known}, 'ordered_ids':[mid, known],
                       'order_verified':True,'render_verified':True, 'count':2, 'limit':30}]
            inbox._save_conversation(alias, identity, self.item, [self.message, message],
                {'message':1794640000000,mid:stamp}, True, 1, observations=events)
        add('xhs01','1'*64,'first-new',1794640010000,'message')
        add('xhs02','2'*64,'other-new',1794640010000,'message')
        snapshot = inbox.conversation(first['id'])['unseen_notifications']
        add('xhs01','1'*64,'second-new',1794640020000,'first-new')
        other_snapshot = inbox.conversation(second['id'])['unseen_notifications']
        inbox.acknowledge_alerts(first['id'], snapshot + other_snapshot)
        self.assertEqual(inbox.conversation(first['id'])['conversation']['new_count'], 1)
        self.assertEqual(inbox.conversation(second['id'])['conversation']['new_count'], 1)
        self.assertEqual(inbox.inbox_summary()['new_count'], 2)

    def test_summary_exposes_new_alert_when_total_stays_the_same(self):
        first = inbox.list_conversations(alias='xhs01')['conversations'][0]
        def incoming(alias, identity, mid):
            message = self.message | {'external_id':mid}
            event = {'ok':True, 'ids':{mid,'message'}, 'ordered_ids':[mid,'message'],
                     'order_verified':True,'render_verified':True, 'count':2, 'limit':30}
            inbox._save_conversation(alias,identity,self.item,[self.message,message],
                {'message':1794640000000,mid:1794640010000},True,1,observations=[event])
        incoming('xhs01','1'*64,'first-new')
        before = inbox.inbox_summary()
        inbox.acknowledge_alerts(first['id'],inbox.conversation(first['id'])['unseen_notifications'])
        incoming('xhs02','2'*64,'second-new')
        after = inbox.inbox_summary()
        self.assertEqual(before['new_count'],1)
        self.assertEqual(after['new_count'],1)
        self.assertGreater(after['latest_new_id'],before['latest_new_id'])
        self.assertEqual(after['latest_review_id'],0)
        incoming('xhs02','2'*64,'second-new')
        self.assertEqual(inbox.inbox_summary(),after)

    def test_stale_mark_processed_does_not_clear_newer_message(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        old_view = inbox.conversation(one['id'])['conversation']['alerts_version']
        new = self.message | {'external_id':'new'}
        event = {'ok':True,'ids':{'new','message'},'ordered_ids':['new','message'],
                 'order_verified':True,'render_verified':True,'count':2,'limit':30}
        inbox._save_conversation('xhs01','1'*64,self.item,[self.message,new],
            {'message':1794640000000,'new':1794640010000},True,1,observations=[event])
        with self.assertRaisesRegex(ValueError, '新'):
            inbox.update_conversation(one['id'],status='processed',alerts_version=old_view)
        current = inbox.conversation(one['id'])['conversation']
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(current['new_count'], 1)

    def test_partial_read_does_not_advance_alert_anchor_past_missing_message(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        inbox.update_conversation(one['id'], status='processed')
        latest = self.message | {'external_id':'latest'}
        missing = self.message | {'external_id':'missing'}
        stamps = {'message':1794640000000, 'latest':1794640030000, 'missing':1794640020000}
        item = self.item | {'_last_timestamp':1794640030000}
        event = {'ok':True, 'ids':{'latest','missing','message'},
                 'ordered_ids':['latest','missing','message'], 'order_verified':True,'render_verified':True,
                 'count':3, 'limit':30, 'invalid_ids':False}
        inbox._save_conversation('xhs01','1'*64,item,[self.message,latest],stamps,False,1,
                                 observations=[event])
        partial = inbox.conversation(one['id'])['conversation']
        self.assertEqual(partial['new_count'], 1)
        self.assertTrue(partial['review_needed'])
        with db() as con:
            cursor = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?', (one['id'],)).fetchone()
        self.assertEqual(cursor['baseline_ms'], 1794640000000)
        self.assertEqual(inbox._existing_history('xhs01','1'*64,self.item['external_id'])[2], {'message'})
        inbox.update_conversation(one['id'],status='processed',alerts_version=partial['alerts_version'])
        inbox._save_conversation('xhs01','1'*64,item,[self.message,missing,latest],stamps,True,1,
                                 observations=[event])
        completed = inbox.conversation(one['id'])['conversation']
        self.assertEqual(completed['status'], 'pending')
        self.assertEqual(completed['new_count'], 1)
        self.assertEqual(inbox._existing_history('xhs01','1'*64,self.item['external_id'])[2],
                         {'message','missing','latest'})

    def test_list_time_ahead_cannot_hide_a_later_incoming_message(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        inbox.update_conversation(one['id'], status='processed')
        stamp = 1794640000000
        item = self.item | {'_last_timestamp': stamp + 50000}
        old = {'ok':True, 'ids':{'message'}, 'ordered_ids':['message'],
               'order_verified':True, 'render_verified':True, 'count':1, 'limit':30}
        inbox._save_conversation('xhs01','1'*64,item,[self.message],{'message':stamp},
                                 True,1,observations=[old])
        with db() as con:
            baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',
                                   (one['id'],)).fetchone()[0]
        self.assertEqual(baseline, stamp)
        self.assertEqual(inbox.conversation(one['id'])['conversation']['last_timestamp_ms'], stamp + 50000)
        incoming = self.message | {'external_id':'incoming'}
        window = old | {'ids':{'incoming','message'}, 'ordered_ids':['incoming','message'], 'count':2}
        for _ in range(2):
            inbox._save_conversation('xhs01','1'*64,item,[self.message,incoming],
                                     {'message':stamp,'incoming':stamp+10000},True,1,observations=[window])
        current = inbox.conversation(one['id'])['conversation']
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(current['new_count'], 1)
        with db() as con:
            baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',
                                   (one['id'],)).fetchone()[0]
        self.assertEqual(baseline, stamp + 10000)

    def test_existing_ahead_cursor_is_bounded_by_confirmed_anchor(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        inbox.update_conversation(one['id'], status='processed')
        stamp = 1794640000000
        with db() as con:
            con.execute('UPDATE inbox_alert_cursors SET baseline_ms=? WHERE conversation_id=?',
                        (stamp+50000,one['id']))
        incoming = self.message | {'external_id':'incoming'}
        window = {'ok':True, 'ids':{'incoming','message'}, 'ordered_ids':['incoming','message'],
                  'order_verified':True, 'render_verified':True, 'count':2, 'limit':30}
        inbox._save_conversation('xhs01','1'*64,self.item,[self.message,incoming],
                                 {'message':stamp,'incoming':stamp+10000},True,1,observations=[window])
        current = inbox.conversation(one['id'])['conversation']
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(current['new_count'], 1)
        with db() as con:
            baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',
                                   (one['id'],)).fetchone()[0]
        self.assertEqual(baseline, stamp+10000)

    def test_missing_cursor_does_not_inherit_unverified_list_time(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        inbox.update_conversation(one['id'], status='processed')
        stamp = 1794640000000
        with db() as con:
            con.execute('DELETE FROM inbox_alert_cursors WHERE conversation_id=?', (one['id'],))
            con.execute('UPDATE inbox_conversations SET last_timestamp_ms=? WHERE id=?', (stamp+50000,one['id']))
        incoming = self.message | {'external_id':'incoming'}
        window = {'ok':True, 'ids':{'incoming','message'}, 'ordered_ids':['incoming','message'],
                  'order_verified':True, 'render_verified':True, 'count':2, 'limit':30}
        inbox._save_conversation('xhs01','1'*64,self.item,[self.message,incoming],
                                 {'message':stamp,'incoming':stamp+10000},True,1,observations=[window])
        self.assertEqual(inbox.conversation(one['id'])['conversation']['new_count'], 1)

    def test_unverified_message_time_cannot_advance_baseline(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        stamp = 1794640000000
        extra = self.message | {'external_id':'extra'}
        window = {'ok':True, 'ids':{'message'}, 'ordered_ids':['message'],
                  'order_verified':True, 'render_verified':True, 'count':1, 'limit':30}
        inbox._save_conversation('xhs01','1'*64,self.item,[self.message,extra],
                                 {'message':stamp,'extra':stamp+50000},True,1,observations=[window])
        with db() as con:
            baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',
                                   (one['id'],)).fetchone()[0]
        self.assertEqual(baseline, stamp)
        self.assertTrue(inbox.conversation(one['id'])['conversation']['review_needed'])
        self.assertNotIn('extra',inbox._existing_history('xhs01','1'*64,self.item['external_id'])[2])

    def test_missing_newest_time_does_not_advance_or_cache(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        stamp = 1794640000000
        newer = self.message | {'external_id':'newer', 'direction':'outgoing', 'store_id':'11'}
        item = self.item | {'_last_timestamp':stamp+50000,'_max_store_id':11,
                            '_list_update_time':stamp+51000,'unread_before':0}
        window = {'ok':True,'ids':{'newer','message'},'ordered_ids':['newer','message'],
                  'order_verified':True,'render_verified':True,'count':2,'limit':30}
        inbox._save_conversation('xhs01','1'*64,item,[self.message,newer],{'message':stamp},
                                 True,1,observations=[window])
        with db() as con:
            baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',
                                   (one['id'],)).fetchone()[0]
        self.assertEqual(baseline, stamp)
        self.assertEqual(inbox._existing_history('xhs01','1'*64,self.item['external_id'])[2], {'message'})
        self.assertIsNone(inbox._reuse_unchanged_conversation('xhs01','1'*64,item))

    def test_new_conversation_cutoff_is_not_stored_ahead_of_verified_message(self):
        stamp = 1794640000000
        item = self.item | {'external_id':'other'}
        window = {'ok':True,'ids':{'message'},'ordered_ids':['message'],
                  'order_verified':True,'render_verified':True,'count':1,'limit':30}
        inbox._save_conversation('xhs01','1'*64,item,[self.message],{'message':stamp},
                                 True,1,observations=[window],account_cutoff_ms=stamp+50000)
        with db() as con:
            cursors = [row[0] for row in con.execute('''SELECT c.baseline_ms FROM inbox_alert_cursors c
                       JOIN inbox_conversations v ON v.id=c.conversation_id
                       WHERE v.alias=? AND v.external_id=?''',('xhs01','other'))]
        self.assertEqual(cursors, [stamp])

    def test_incomplete_older_rows_do_not_advance_timestamp_cursor(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        item = self.item | {'_last_timestamp':1794640050000}
        event = {'ok':True,'ids':{'message','hidden-old'},'ordered_ids':['message','hidden-old'],
                 'order_verified':True,'render_verified':True,'count':2,'limit':30,'invalid_ids':False}
        inbox._save_conversation('xhs01','1'*64,item,[self.message],{'message':1794640000000},False,1,
                                 observations=[event])
        with db() as con:
            baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',(one['id'],)).fetchone()[0]
        self.assertEqual(baseline, 1794640000000)
        first = inbox.conversation(one['id'])['conversation']
        self.assertTrue(first['review_needed'])
        self.assertEqual(inbox.inbox_summary()['review_count'],1)
        inbox.update_conversation(one['id'],status='processed',alerts_version=first['alerts_version'])
        inbox._save_conversation('xhs01','1'*64,item,[self.message],{'message':1794640000000},False,1,
                                 observations=[event])
        again = inbox.conversation(one['id'])['conversation']
        self.assertEqual(again['status'],'processed')
        self.assertFalse(again['review_needed'])

    def test_unresolved_prior_gap_blocks_cursor_even_if_next_page_looks_complete(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        item = self.item | {'_last_timestamp':1794640050000}
        incomplete = {'ok':True,'ids':{'message','hidden'},'ordered_ids':['message','hidden'],
                      'order_verified':True,'render_verified':True,'count':2,'limit':30,'invalid_ids':False}
        inbox._save_conversation('xhs01','1'*64,item,[self.message],{'message':1794640000000},False,1,
                                 observations=[incomplete])
        complete = {'ok':True,'ids':{'message'},'ordered_ids':['message'],
                    'order_verified':True,'render_verified':True,'count':1,'limit':30,'invalid_ids':False}
        inbox._save_conversation('xhs01','1'*64,item,[self.message],{'message':1794640000000},True,1,
                                 observations=[complete])
        with db() as con:
            baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',(one['id'],)).fetchone()[0]
        self.assertEqual(baseline,1794640000000)
        details = inbox.conversation(one['id'])
        inbox.acknowledge_alerts(one['id'],details['review_alerts'])
        inbox._save_conversation('xhs01','1'*64,item,[self.message],{'message':1794640000000},True,1,
                                 observations=[complete])
        with db() as con:
            baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',(one['id'],)).fetchone()[0]
        self.assertEqual(baseline,1794640000000)

    def test_no_anchor_overlap_cannot_advance_until_history_exhausted_and_reviewed(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        new = self.message | {'external_id':'new'}
        item = self.item | {'_last_timestamp':1794640050000}
        event = {'ok':True,'ids':{'new'},'ordered_ids':['new'],'order_verified':True,'render_verified':True,
                 'count':1,'limit':30,'invalid_ids':False}
        for _ in range(2):
            inbox._save_conversation('xhs01','1'*64,item,[new],{'new':1794640050000},False,1,observations=[event])
            current = inbox.conversation(one['id'])
            if current['review_alerts']:
                inbox.acknowledge_alerts(one['id'],current['review_alerts'])
        with db() as con:
            baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',(one['id'],)).fetchone()[0]
        self.assertEqual(baseline,1794640000000)
        self.assertEqual(inbox._existing_history('xhs01','1'*64,self.item['external_id'])[2],{'message'})
        inbox._save_conversation('xhs01','1'*64,item,[new],{'new':1794640050000},True,1,observations=[event])
        with db() as con:
            baseline = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',(one['id'],)).fetchone()[0]
        self.assertEqual(baseline,1794640050000)

    def test_quick_reuse_requires_complete_unchanged_and_recent_platform_metadata(self):
        item = self.item | {'_last_timestamp':1794640000000,'_max_store_id':10,
                            '_list_update_time':1794640001000,'unread_before':0}
        event = {'ok':True,'ids':{'message'},'ordered_ids':['message'],
                 'order_verified':True,'render_verified':True,'count':1,'limit':30,'invalid_ids':False}
        with patch('inbox.time.time',return_value=1794640100):
            inbox._save_conversation('xhs01','1'*64,item,[self.message],{'message':1794640000000},
                                     True,1,observations=[event])
            self.assertIsNotNone(inbox._reuse_unchanged_conversation('xhs01','1'*64,item))
            for changed in (item|{'_max_store_id':11},item|{'_last_timestamp':1794640000001},
                            item|{'_list_update_time':1794640001001},item|{'last_preview':'新内容'},
                            item|{'unread_before':1},item|{'_max_store_id':None}):
                self.assertIsNone(inbox._reuse_unchanged_conversation('xhs01','1'*64,changed))
        with patch('inbox.time.time',return_value=1794640100+inbox.QUICK_RECHECK_SECONDS+1):
            self.assertIsNone(inbox._reuse_unchanged_conversation('xhs01','1'*64,item))
        mismatched = item | {'_max_store_id':11}
        inbox._save_conversation('xhs01','1'*64,mismatched,[self.message],{'message':1794640000000},
                                 True,1,observations=[event])
        self.assertIsNone(inbox._reuse_unchanged_conversation('xhs01','1'*64,mismatched))

    def test_old_quick_cache_is_not_reused_without_render_verification(self):
        item = self.item | {'_last_timestamp':1794640000000,'_max_store_id':10,
                            '_list_update_time':1794640001000,'unread_before':0}
        legacy = [item['_last_timestamp'],str(item['_max_store_id']),str(item['_list_update_time']),item['last_preview']]
        old_signature = hashlib.sha256(json.dumps(legacy,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        with db() as con:
            con.execute('''INSERT INTO inbox_quick_checkpoints(conversation_id,signature,verified_at_ms)
                           VALUES(?,?,?)''',(one['id'],old_signature,1794640100000))
        with patch('inbox.time.time',return_value=1794640100):
            self.assertIsNone(inbox._reuse_unchanged_conversation('xhs01','1'*64,item))

    def test_quick_cache_from_before_cursor_fix_is_rechecked(self):
        item = self.item | {'_last_timestamp':1794640000000,'_max_store_id':10,
                            '_list_update_time':1794640001000,'unread_before':0}
        legacy = ['render-verified-v2',item['_last_timestamp'],str(item['_max_store_id']),
                  str(item['_list_update_time']),item['last_preview']]
        old_signature = hashlib.sha256(json.dumps(legacy,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        with db() as con:
            con.execute('''INSERT INTO inbox_quick_checkpoints(conversation_id,signature,verified_at_ms)
                           VALUES(?,?,?)''',(one['id'],old_signature,1794640100000))
        with patch('inbox.time.time',return_value=1794640100):
            self.assertIsNone(inbox._reuse_unchanged_conversation('xhs01','1'*64,item))

    def test_partial_history_invalidates_quick_reuse_and_reports_account_timing(self):
        item = self.item | {'_last_timestamp':1794640000000,'_max_store_id':10,
                            '_list_update_time':1794640001000,'unread_before':0}
        safe = {'ok':True,'ids':{'message'},'ordered_ids':['message'],
                'order_verified':True,'render_verified':True,'count':1,'limit':30,'invalid_ids':False}
        inbox._save_conversation('xhs01','1'*64,item,[self.message],{'message':1794640000000},True,1,observations=[safe])
        self.assertIsNotNone(inbox._reuse_unchanged_conversation('xhs01','1'*64,item))
        incomplete = safe | {'ids':{'message','hidden'},'ordered_ids':['message','hidden'],'count':2}
        inbox._save_conversation('xhs01','1'*64,item,[self.message],{'message':1794640000000},False,1,observations=[incomplete])
        self.assertIsNone(inbox._reuse_unchanged_conversation('xhs01','1'*64,item))
        inbox._record_account('xhs01','partial','待核对',1,identity='1'*64,mode='quick',
                              elapsed_ms=1250,wait_ms=100,opened=1,reused=0)
        row = next(a for a in inbox.inbox_accounts()['accounts'] if a['alias']=='xhs01')
        self.assertEqual((row['elapsed_ms'],row['wait_ms'],row['opened'],row['reused']),(1250,100,1,0))

    def test_unknown_initial_baseline_recovers_after_quiet_sync(self):
        item = self.item | {'external_id':'new-conversation'}
        first = self.message | {'external_id':'initial'}
        first_event = {'ok':True,'ids':{'initial'},'ordered_ids':['initial'],
                       'order_verified':True,'render_verified':True,'count':1,'limit':30}
        inbox._save_conversation('xhs01','1'*64,item,[first],{'initial':1794640000000},
                                 True,1,observations=[first_event])
        row = inbox.list_conversations(alias='xhs01')['conversations']
        identifier = next(x['id'] for x in row if x['review_needed'])
        current = inbox.conversation(identifier)
        self.assertTrue(current['conversation']['review_needed'])
        inbox.update_conversation(identifier,status='processed',alerts_version=current['conversation']['alerts_version'])
        inbox._save_conversation('xhs01','1'*64,item,[first],{'initial':1794640000000},
                                 True,1,observations=[first_event])
        later = self.message | {'external_id':'later'}
        later_event = {'ok':True,'ids':{'later','initial'},'ordered_ids':['later','initial'],
                       'order_verified':True,'render_verified':True,'count':2,'limit':30}
        inbox._save_conversation('xhs01','1'*64,item,[first,later],
                                 {'initial':1794640000000,'later':1794640010000},True,1,observations=[later_event])
        current = inbox.conversation(identifier)['conversation']
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(current['new_count'], 1)

    def test_missing_order_or_time_boundary_is_marked_for_review(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        candidate = self.message | {'external_id':'new'}
        event = {'ok':True,'ids':{'new','message'},'ordered_ids':['new','message'],
                 'count':2,'limit':30}
        inbox._save_conversation('xhs01','1'*64,self.item,[self.message,candidate],
                                 {'message':1794640000000,'new':1794640001000},False,1,
                                 observations=[event])
        current = inbox.conversation(one['id'])
        self.assertEqual(current['conversation']['new_count'], 0)
        self.assertTrue(current['conversation']['review_needed'])

    def test_unverified_order_warns_again_only_when_observed_ids_change(self):
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        a = self.message | {'external_id':'a'}
        b = self.message | {'external_id':'b'}
        event = {'ok':True,'ids':{'message','a'},'ordered_ids':['a','message'],
                 'order_verified':False,'count':2,'limit':30}
        stamps={'message':1794640000000,'a':1794640010000,'b':1794640020000}
        inbox._save_conversation('xhs01','1'*64,self.item,[self.message,a],stamps,False,1,observations=[event])
        first = inbox.conversation(one['id'])['conversation']
        inbox.update_conversation(one['id'],status='processed',alerts_version=first['alerts_version'])
        reordered = event | {'ordered_ids':['message','a']}
        inbox._save_conversation('xhs01','1'*64,self.item,[self.message,a],stamps,False,1,observations=[reordered])
        self.assertFalse(inbox.conversation(one['id'])['conversation']['review_needed'])
        changed = event | {'ids':{'message','a','b'},'ordered_ids':['b','a','message'],'count':3}
        inbox._save_conversation('xhs01','1'*64,self.item,[self.message,a,b],stamps,False,1,observations=[changed])
        current = inbox.conversation(one['id'])['conversation']
        self.assertTrue(current['review_needed'])
        self.assertEqual(current['status'],'pending')

    def test_new_conversation_still_appears_when_history_cannot_load(self):
        new_item = self.item | {'external_id':'new-customer', 'display_name':'新会话'}
        page = MagicMock()
        with patch.object(inbox, 'browser_lock', return_value=nullcontext()), \
             patch.object(inbox, 'creator_page', return_value=nullcontext(page)), \
             patch.object(inbox, 'observe_xiaohongshu_identity', return_value={}), \
             patch.object(inbox, 'verify_origin'), patch.object(inbox, 'check_challenge'), \
             patch.object(inbox, 'require_xiaohongshu_identity'), patch.object(inbox, '_wait_for_list'), \
             patch.object(inbox, 'verify_web_identity'), patch.object(inbox, '_gather_conversations', return_value=[self.item, new_item]), \
             patch.object(inbox, '_read_history', return_value=([], {}, object())), \
             patch.object(inbox, '_select_conversation', side_effect=ValueError('temporary page failure')):
            result = inbox.sync_account('xhs01')
        self.assertEqual(result['errors'], 2)
        self.assertEqual(inbox.list_conversations(alias='xhs01', query='新会话')['total'], 1)
        row = inbox.list_conversations(alias='xhs01', query='新会话')['conversations'][0]
        self.assertFalse(row['history_complete'])
        self.assertEqual(row['message_count'], 0)
        existing = inbox.list_conversations(alias='xhs01', query='客户甲')['conversations'][0]
        self.assertFalse(existing['history_complete'])
        self.assertEqual(inbox.conversation(existing['id'])['total'], 1)

    def test_conversation_time_uses_current_platform_list_timestamp(self):
        latest = 1794641234000
        item = self.item | {'_last_timestamp': latest}
        inbox._save_conversation('xhs01', '1' * 64, item, [self.message],
                                 {'message':1794640000000}, True, 1)
        row = inbox.list_conversations(alias='xhs01')['conversations'][0]
        self.assertEqual(row['last_timestamp_ms'], latest)

    def test_history_events_must_match_selected_conversation(self):
        class Page:
            def on(self, name, callback): self.callback = callback
        class Response:
            status = 200
            def __init__(self, selected):
                self.url = 'https://edith.xiaohongshu.com/api/im/web/messages/history?limit=30&chat_user_id=' + selected
            def json(self):
                return {'data': {'result': {'success': True}, 'out_message_list': [{'id': 'm', 'created_at': 1794640000000}]}}
        page = Page()
        events, timestamps, callback = inbox._read_history(page, 'expected')
        callback(Response('another'))
        self.assertEqual(events, [])
        callback(Response('expected'))
        self.assertEqual(len(events), 1)
        self.assertEqual(timestamps['m'], 1794640000000)

    def test_platform_rows_without_unique_ids_do_not_claim_complete(self):
        class Page:
            def on(self, name, callback): self.callback = callback
            def wait_for_timeout(self, _): pass
        class Response:
            status = 200
            url = 'https://edith.xiaohongshu.com/api/im/web/messages/history?limit=30&chat_user_id=expected'
            def json(self):
                return {'data': {'result': {'success': True}, 'out_message_list': [{'id': None}]}}
        page = Page()
        events, timestamps, callback = inbox._read_history(page, 'expected')
        callback(Response())
        with patch.object(inbox, '_read_messages', return_value=[]):
            _, complete, _ = inbox._finish_history(page, events, timestamps, False, 0, set(), 1)
        self.assertFalse(complete)

    def test_local_media_is_validated_scoped_and_session_protected(self):
        image = io.BytesIO()
        Image.new('RGB', (2, 3), 'red').save(image, format='PNG')
        data = image.getvalue()
        self.assertEqual(inbox._validate_image(data), 'image/png')
        self.assertIsNone(inbox._validate_image(b'<svg onload=alert(1)>'))
        inbox._save_conversation('xhs01', '1' * 64, self.item, [self.message], {}, True, 1,
                                 {'message': ('image/png', data)})
        one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        media_id = inbox.conversation(one['id'])['messages'][0]['media_id']
        client = TestClient(app, base_url='http://127.0.0.1:8765')
        self.assertEqual(client.get(f'/api/inbox/media/{media_id}').status_code, 403)
        client.get('/')
        response = client.get(f'/api/inbox/media/{media_id}')
        self.assertEqual(response.content, data)
        self.assertEqual(response.headers['content-type'], 'image/png')
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        save_account_identity('xhs01', 'different', '3' * 64)
        self.assertEqual(client.get(f'/api/inbox/media/{media_id}').status_code, 400)

    def test_media_fetch_never_requests_untrusted_hosts(self):
        image = io.BytesIO()
        Image.new('RGB', (2, 3), 'blue').save(image, format='PNG')
        data = image.getvalue()
        class Result:
            ok = True
            headers = {'content-length': str(len(data))}
            def body(self):
                return data
        class Client:
            def __init__(self): self.urls = []
            def get(self, url, **kwargs): self.urls.append(url); return Result()
        class Context:
            request = Client()
        class Page:
            context = Context()
        messages = [self.message | {'external_id':'good', 'image_src':'https://msg-img.xhscdn.com/test'},
                    self.message | {'external_id':'bad', 'image_src':'https://evil.example/test'}]
        files = inbox._fetch_media(Page(), 'xhs01', '1' * 64, self.item, messages)
        self.assertEqual(list(files), ['good'])
        self.assertEqual(Page.context.request.urls, ['https://msg-img.xhscdn.com/test'])

    def test_virtualized_long_conversation_list_can_scan_and_select_middle(self):
        from playwright.sync_api import sync_playwright
        fixture = '''<!doctype html><style>
          .xhs-im-conv-list__scroll {height:120px;overflow:auto;position:relative}
          #spacer {height:4800px;position:relative}
          .xhs-im-conv-item {position:absolute;left:0;right:0;height:40px}
          </style><div class="xhs-im-conv-list__scroll"><div id="spacer"></div></div><script>
          const box=document.querySelector('.xhs-im-conv-list__scroll'), space=document.querySelector('#spacer');
          function render() {
            const start=Math.floor(box.scrollTop/40);
            space.innerHTML='';
            for (let n=start;n<Math.min(120,start+5);n++) {
              const e=document.createElement('div'); e.className='xhs-im-conv-item';
              e.dataset.convId='friend'+n; e.dataset.convKind='c2c'; e.style.top=(n*40)+'px';
              e.innerHTML='<span class="xhs-im-conv-item__name">Test</span>';
              e.addEventListener('click',()=>history.pushState({},'', '/chat/'+e.dataset.convId));
              space.appendChild(e);
            }
          }
          box.addEventListener('scroll',render);render();</script>'''
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            try:
                page = browser.new_page()
                page.route('https://www.xiaohongshu.com/**', lambda route: route.fulfill(status=200, body=fixture, content_type='text/html'))
                page.goto('https://www.xiaohongshu.com/chat')
                rows = inbox._scroll_conversation_list(page)
                self.assertEqual(len(rows), 120)
                inbox._select_conversation(page, {'external_id':'friend37','location':'main'}, 'main')
                self.assertTrue(page.url.endswith('/chat/friend37'))
                inbox._select_conversation(page, rows['friend84'] | {'location':'main'}, 'main')
                self.assertTrue(page.url.endswith('/chat/friend84'))
            finally:
                browser.close()

    def test_long_inbox_keeps_headers_visible_while_messages_scroll(self):
        from playwright.sync_api import sync_playwright
        css = (Path(__file__).parent / 'web' / 'styles.css').read_text(encoding='utf-8')
        items = ''.join('<button class="inbox-item">会话</button>' for _ in range(150))
        messages = ''.join('<div class="inbox-message"><div class="inbox-bubble">消息</div></div>' for _ in range(300))
        html = f'''<!doctype html><div class="main"><div class="work"><section><div class="inbox-layout">
          <div class="inbox-list-panel"><div class="inbox-list-heading">会话</div><div class="inbox-list">{items}</div></div>
          <div class="inbox-thread"><div class="inbox-thread-head"><h2>会话</h2>
            <div class="inbox-thread-actions"><button class="secondary">打开原会话</button></div></div>
            <div class="inbox-messages">{messages}</div><div class="inbox-thread-foot">提示</div></div>
          </div></section></div></div>'''
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            try:
                page = browser.new_page(viewport={'width':1440, 'height':900})
                page.set_content(html)
                page.add_style_tag(content=css)
                page.locator('.inbox-message').last.scroll_into_view_if_needed()
                state = page.evaluate('''() => {
                  const panel=document.querySelector('.inbox-layout'), list=document.querySelector('.inbox-list');
                  const messages=document.querySelector('.inbox-messages'), frame=panel.getBoundingClientRect();
                  const header=document.querySelector('.inbox-thread-head').getBoundingClientRect();
                  return {outerScroll:panel.scrollTop, outerHeight:panel.clientHeight, outerContent:panel.scrollHeight,
                    listScrollable:list.scrollHeight>list.clientHeight, messagesScrollable:messages.scrollHeight>messages.clientHeight,
                    headerVisible:header.top>=frame.top-1 && header.bottom<=frame.bottom+1};
                }''')
                self.assertEqual(state['outerScroll'], 0)
                self.assertLessEqual(state['outerContent'], state['outerHeight'] + 1)
                self.assertTrue(state['listScrollable'])
                self.assertTrue(state['messagesScrollable'])
                self.assertTrue(state['headerVisible'])
                page.set_viewport_size({'width':390,'height':844})
                mobile = page.evaluate('''() => {
                  const list=document.querySelector('.inbox-list-panel').getBoundingClientRect();
                  const thread=document.querySelector('.inbox-thread').getBoundingClientRect();
                  return {noOverlap:thread.top>=list.bottom-1,noHorizontalOverflow:document.documentElement.scrollWidth<=innerWidth};
                }''')
                self.assertTrue(mobile['noOverlap'])
                self.assertTrue(mobile['noHorizontalOverflow'])
            finally:
                browser.close()

    def test_conversation_selector_escapes_ids_and_rejects_duplicates(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            try:
                page = browser.new_page()
                page.route('https://www.xiaohongshu.com/**', lambda route: route.fulfill(
                    status=200, body='<div class="xhs-im-conv-list__scroll"></div>', content_type='text/html'))
                page.goto('https://www.xiaohongshu.com/chat')
                cid = 'person"\\name]'
                page.evaluate('''cid => {
                  const e=document.createElement('button');e.className='xhs-im-conv-item';
                  e.dataset.convKind='c2c';e.dataset.convId=cid;e.textContent='会话';
                  e.onclick=()=>history.pushState({},'', '/chat/'+encodeURIComponent(cid));
                  document.querySelector('.xhs-im-conv-list__scroll').appendChild(e);
                }''', cid)
                self.assertEqual(inbox._select_conversation(page, {'external_id':cid,'location':'main'}, 'main'), 'main')
                page.evaluate('''() => {const e=document.querySelector('.xhs-im-conv-item');e.parentNode.appendChild(e.cloneNode(true));}''')
                with self.assertRaisesRegex(ValueError, '不唯一'):
                    inbox._select_conversation(page, {'external_id':cid,'location':'main'}, 'main')
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
