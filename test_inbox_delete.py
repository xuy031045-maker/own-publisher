import os
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import agent_server
import inbox
from publisher import add_account, db, save_account_identity
from workbench import app


class InboxDeleteTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, {'OWN_PUBLISHER_HOME': self.folder.name})
        env.start()
        self.addCleanup(env.stop)
        for alias, nickname, identity in (('xhs01', '真实昵称甲', '1' * 64),
                                          ('xhs02', '真实昵称乙', '2' * 64)):
            add_account(alias, 'xiaohongshu', '自定义名称')
            save_account_identity(alias, nickname, identity)
        self.stamp = 1794640000000
        self.cutoff = self.stamp + 20000
        self.item = {'external_id': 'conversation', 'location': 'main', 'display_name': '客户甲',
                     'last_preview': '第二条旧消息', 'last_time_label': '今天', 'unread_before': 0}
        self.old1 = self.message('old1', '第一条旧消息')
        self.old2 = self.message('old2', '第二条旧消息')
        self.times = {'old1': self.stamp-10000, 'old2': self.stamp}
        for alias, identity in (('xhs01', '1' * 64), ('xhs02', '2' * 64)):
            inbox._save_conversation(alias, identity, self.item, [self.old1,self.old2],
                                     self.times, True, 1)
        self.one = inbox.list_conversations(alias='xhs01')['conversations'][0]
        self.two = inbox.list_conversations(alias='xhs02')['conversations'][0]

    @staticmethod
    def message(mid, body, direction='incoming'):
        return {'external_id':mid, 'store_id':mid, 'direction':direction, 'content_type':'1',
                'body':body, 'time_label':'今天'}

    def sync(self, messages, timestamps, *, alias='xhs01', complete=True, item=None, mode=None):
        ids = [m['external_id'] for m in reversed(messages)]
        event = {'ok':True, 'ids':set(ids), 'ordered_ids':ids, 'order_verified':True,
                 'render_verified':True, 'count':len(ids), 'limit':30, 'invalid_ids':False}
        identity = '2' * 64 if alias == 'xhs02' else '1' * 64
        return inbox._save_conversation(alias,identity,item or self.item,messages,timestamps,
                                        complete,1,observations=[event],sync_mode=mode)

    def delete_thread(self):
        snapshot = inbox.conversation(self.one['id'])['conversation']
        with patch('inbox.time.time',return_value=self.cutoff / 1000):
            return inbox.delete_conversation(self.one['id'], messages_version=snapshot['messages_version'],
                                             alerts_version=snapshot['alerts_version'])

    def test_single_message_deletion_survives_sync_search_media_and_mcp(self):
        data = inbox.conversation(self.one['id'])
        message = next(m for m in data['messages'] if m['platform_message_id']=='old2')
        with db() as con:
            con.execute('''INSERT INTO inbox_media(conversation_id,message_external_id,mime_type,data)
                           VALUES(?,?,?,?)''',(self.one['id'],'old2','image/png',b'test'))
            media_id = con.execute('SELECT id FROM inbox_media WHERE conversation_id=? AND message_external_id=?',
                                   (self.one['id'],'old2')).fetchone()[0]
        inbox.delete_message(self.one['id'], message['message_id'], message['platform_message_id'])
        with db() as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM inbox_message_deletions WHERE conversation_id=?',
                                         (self.one['id'],)).fetchone()[0],1)
            saved = con.execute('SELECT body FROM inbox_messages WHERE conversation_id=? AND external_id=?',
                                (self.one['id'],'old2')).fetchone()[0]
        changed = self.old2 | {'body':'不能重新写入'}
        self.sync([self.old1,changed],self.times)
        current = inbox.conversation(self.one['id'])
        self.assertEqual([m['platform_message_id'] for m in current['messages']], ['old1'])
        self.assertNotIn('第二条旧消息',inbox.list_conversations(alias='xhs01')['conversations'][0]['last_preview'])
        self.assertEqual(inbox.list_conversations(alias='xhs01',query='第二条旧消息')['total'],0)
        with db() as con:
            self.assertEqual(con.execute('SELECT body FROM inbox_messages WHERE conversation_id=? AND external_id=?',
                                         (self.one['id'],'old2')).fetchone()[0],saved)
        with self.assertRaises(ValueError): inbox.media_file(media_id)
        mcp = agent_server.inbox_messages(conversation_key=self.one['conversation_key'])
        self.assertEqual([m['platform_message_id'] for m in mcp['messages']],['old1'])
        self.assertEqual(inbox.conversation(self.two['id'])['total'],2)

    def test_deletion_follows_the_same_platform_identity_across_aliases(self):
        add_account('xhs03','xiaohongshu','备用别名')
        save_account_identity('xhs03','真实昵称甲','1'*64)
        self.sync([self.old1,self.old2],self.times,alias='xhs03',mode='full')
        third = inbox.list_conversations(alias='xhs03')['conversations'][0]
        message = next(m for m in inbox.conversation(self.one['id'])['messages']
                       if m['platform_message_id']=='old2')
        inbox.delete_message(self.one['id'],message['message_id'],message['platform_message_id'])
        for mode in ('quick','full'):
            self.sync([self.old1,self.old2 | {'body':'不应重新写入'}],self.times,alias='xhs03',mode=mode)
            self.assertEqual([m['platform_message_id'] for m in inbox.conversation(third['id'])['messages']],['old1'])
        with db() as con:
            self.assertEqual(con.execute('SELECT body FROM inbox_messages WHERE conversation_id=? AND external_id=?',
                                         (third['id'],'old2')).fetchone()[0],'第二条旧消息')
        self.assertEqual(inbox.list_conversations(alias='xhs03',query='第二条旧消息')['total'],0)
        self.assertEqual(inbox.conversation(self.two['id'])['total'],2)
        add_account('xhs04','xiaohongshu','新别名')
        save_account_identity('xhs04','真实昵称甲','1'*64)
        self.sync([self.old1,self.old2],self.times,alias='xhs04',mode='full')
        fourth = inbox.list_conversations(alias='xhs04')['conversations'][0]
        self.assertEqual([m['platform_message_id'] for m in inbox.conversation(fourth['id'])['messages']],['old1'])
        with db() as con:
            self.assertIsNone(con.execute('SELECT 1 FROM inbox_messages WHERE conversation_id=? AND external_id=?',
                                          (fourth['id'],'old2')).fetchone())

    def test_conversation_deletion_also_applies_to_future_aliases(self):
        add_account('xhs03','xiaohongshu','备用别名')
        save_account_identity('xhs03','真实昵称甲','1'*64)
        self.sync([self.old1,self.old2],self.times,alias='xhs03',mode='full')
        self.delete_thread()
        self.assertEqual(inbox.list_conversations(alias='xhs03')['total'],0)
        add_account('xhs04','xiaohongshu','新别名')
        save_account_identity('xhs04','真实昵称甲','1'*64)
        for alias in ('xhs03','xhs04'):
            self.sync([self.old1,self.old2],self.times,alias=alias,mode='full')
            self.assertEqual(inbox.list_conversations(alias=alias)['total'],0)
        new=self.message('after-delete','删除后的新消息')
        stamps=self.times | {'after-delete':self.cutoff+10000}
        for alias in ('xhs03','xhs04'):
            self.sync([self.old1,self.old2,new],stamps,alias=alias,mode='quick')
            row=inbox.list_conversations(alias=alias)['conversations'][0]
            self.assertEqual([m['platform_message_id'] for m in inbox.conversation(row['id'])['messages']],['after-delete'])
        self.assertEqual(inbox.conversation(self.two['id'])['total'],2)

    def test_whole_deletion_rejects_unseen_messages_under_another_alias(self):
        add_account('xhs03','xiaohongshu','备用别名')
        save_account_identity('xhs03','真实昵称甲','1'*64)
        third=self.message('other-new','另一别名刚收到的新消息')
        self.sync([self.old1,self.old2,third],self.times | {'other-new':self.stamp+10000},alias='xhs03')
        with self.assertRaisesRegex(ValueError,'别名|同步'):
            self.delete_thread()
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],1)
        self.assertEqual(inbox.list_conversations(alias='xhs03')['total'],1)

    def test_duplicate_aliases_keep_mcp_lookup_explicit_when_stable_key_is_ambiguous(self):
        add_account('xhs03','xiaohongshu','备用别名')
        save_account_identity('xhs03','真实昵称甲','1'*64)
        self.sync([self.old1,self.old2],self.times,alias='xhs03',mode='full')
        with self.assertRaises(ValueError):
            agent_server.inbox_messages(conversation_key=self.one['conversation_key'])
        self.assertEqual(agent_server.inbox_messages(conversation_id=self.one['id'])['total'],2)

    def test_new_mcp_process_filters_deleted_messages(self):
        import asyncio
        import sys
        from pathlib import Path
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        data=inbox.conversation(self.one['id'])
        message=next(m for m in data['messages'] if m['platform_message_id']=='old2')
        inbox.delete_message(self.one['id'],message['message_id'],message['platform_message_id'])

        async def exercise():
            params=StdioServerParameters(command=sys.executable,args=['-X','utf8','agent_server.py'],
                                         env=os.environ.copy(),cwd=str(Path(__file__).resolve().parent))
            async with stdio_client(params) as (reader,writer):
                async with ClientSession(reader,writer) as session:
                    await session.initialize()
                    result=await asyncio.wait_for(session.call_tool('inbox_messages',
                        {'conversation_key':self.one['conversation_key']}),timeout=10)
                    self.assertFalse(result.isError)
                    self.assertEqual([m['platform_message_id'] for m in result.structuredContent['messages']],['old1'])
        asyncio.run(exercise())

    def test_deleted_conversation_remains_hidden_and_old_unknown_history_is_not_written(self):
        self.delete_thread()
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],0)
        self.assertEqual(inbox.list_conversations(alias='xhs02')['total'],1)
        accounts = {a['alias']:a for a in inbox.inbox_accounts()['accounts']}
        self.assertEqual((accounts['xhs01']['conversations'],accounts['xhs02']['conversations']),(0,1))
        with self.assertRaises(ValueError): inbox.conversation(self.one['id'])
        with self.assertRaises(ValueError): agent_server.inbox_messages(conversation_key=self.one['conversation_key'])
        self.sync([self.old1,self.old2],self.times)
        unseen_old = self.message('unseen-old','此前未加载的旧消息')
        times = self.times | {'unseen-old':self.stamp-20000}
        self.sync([unseen_old,self.old1,self.old2],times,complete=True)
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],0)
        self.assertEqual(agent_server.inbox_conversations(alias='xhs01')['total'],0)
        with db() as con:
            self.assertIsNone(con.execute('SELECT 1 FROM inbox_messages WHERE conversation_id=? AND external_id=?',
                                          (self.one['id'],'unseen-old')).fetchone())

    def test_new_message_reopens_deleted_conversation_without_restoring_history(self):
        self.delete_thread()
        new = self.message('new','删除后的新消息')
        times = self.times | {'new':self.cutoff+10000}
        item = self.item | {'last_preview':'删除后的新消息','_last_timestamp':times['new']}
        for _ in range(2):
            self.sync([self.old1,self.old2,new],times,item=item)
        rows = inbox.list_conversations(alias='xhs01')['conversations']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['status'],'pending')
        self.assertEqual(rows[0]['new_count'],1)
        data = inbox.conversation(self.one['id'])
        self.assertEqual([m['platform_message_id'] for m in data['messages']],['new'])
        self.assertEqual(data['conversation']['conversation_key'],self.one['conversation_key'])
        self.assertEqual(inbox.list_conversations(alias='xhs01',query='第一条旧消息')['total'],0)

    def test_new_message_without_trustworthy_time_cannot_restore_old_history(self):
        self.delete_thread()
        unknown = self.message('unknown','时间未核实')
        self.assertFalse(self.sync([self.old1,self.old2,unknown],self.times))
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],0)
        with db() as con:
            self.assertIsNone(con.execute('SELECT 1 FROM inbox_messages WHERE conversation_id=? AND external_id=?',
                                          (self.one['id'],'unknown')).fetchone())
        self.sync([self.old1,self.old2,unknown],self.times | {'unknown':self.cutoff+10000})
        self.assertEqual([m['platform_message_id'] for m in inbox.conversation(self.one['id'])['messages']],['unknown'])

    def test_ambiguous_delete_time_is_not_silently_treated_as_complete(self):
        self.delete_thread()
        uncertain=self.message('same-second','时间粒度不够')
        result=self.sync([self.old1,self.old2,uncertain],
                         self.times | {'same-second':self.cutoff-500},mode='quick')
        self.assertFalse(result)
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],0)

    def test_quick_sync_skips_older_backfill_but_full_sync_can_read_it(self):
        older = self.message('older','更早的历史')
        new = self.message('new','最新消息')
        times = self.times | {'older':self.stamp-20000,'new':self.stamp+10000}
        self.sync([older,self.old1,self.old2,new],times,mode='quick')
        data = inbox.conversation(self.one['id'])
        self.assertEqual([m['platform_message_id'] for m in data['messages']],['old1','old2','new'])
        self.assertFalse(data['conversation']['history_complete'])
        self.sync([older,self.old1,self.old2,new],times,mode='full')
        self.assertEqual([m['platform_message_id'] for m in inbox.conversation(self.one['id'])['messages']],
                         ['older','old1','old2','new'])

    def test_deleted_conversation_does_not_reopen_on_older_row_with_future_timestamp(self):
        self.delete_thread()
        older = self.message('older-clock-skew','旧消息但时间异常')
        self.sync([older,self.old1,self.old2],self.times | {'older-clock-skew':self.cutoff+10000},mode='full')
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],0)
        with db() as con:
            self.assertIsNone(con.execute('SELECT 1 FROM inbox_messages WHERE conversation_id=? AND external_id=?',
                                          (self.one['id'],'older-clock-skew')).fetchone())

    def test_post_delete_new_messages_survive_a_missing_old_anchor(self):
        self.delete_thread()
        first = self.message('new-gap-1','删除后第一条')
        second = self.message('new-gap-2','删除后第二条')
        first_time = self.cutoff+10000
        self.sync([first],{'new-gap-1':first_time},complete=False,mode='quick')
        data = inbox.conversation(self.one['id'])
        self.assertEqual([m['platform_message_id'] for m in data['messages']], ['new-gap-1'])
        self.assertEqual(data['conversation']['new_count'],1)
        self.assertTrue(data['conversation']['review_needed'])
        stamps={'new-gap-1':first_time,'new-gap-2':first_time+10000}
        for _ in range(2):
            self.sync([first,second],stamps,complete=False,mode='quick')
        data = inbox.conversation(self.one['id'])
        self.assertEqual([m['platform_message_id'] for m in data['messages']],['new-gap-1','new-gap-2'])
        self.assertEqual(data['conversation']['new_count'],2)

    def test_repeated_deletion_keeps_both_generations_hidden(self):
        self.delete_thread()
        first = self.message('new1','第一次删除之后')
        first_time = self.cutoff+10000
        self.sync([self.old1,self.old2,first],self.times | {'new1':first_time},mode='quick')
        snapshot = inbox.conversation(self.one['id'])['conversation']
        with patch('inbox.time.time',return_value=(first_time+10000)/1000):
            inbox.delete_conversation(self.one['id'],messages_version=snapshot['messages_version'],
                                      alerts_version=snapshot['alerts_version'])
        second = self.message('new2','第二次删除之后')
        stamps = self.times | {'new1':first_time,'new2':first_time+20000}
        self.sync([self.old1,self.old2,first,second],stamps,mode='full')
        self.assertEqual([m['platform_message_id'] for m in inbox.conversation(self.one['id'])['messages']],['new2'])
        self.assertEqual(inbox.conversation(self.one['id'])['conversation']['new_count'],1)

    def test_delete_removes_only_the_target_alerts_from_summary(self):
        new = self.message('new','一条新提醒')
        self.sync([self.old1,self.old2,new],self.times | {'new':self.stamp+10000})
        self.assertEqual(inbox.inbox_summary()['new_count'],1)
        data=inbox.conversation(self.one['id'])
        message=next(m for m in data['messages'] if m['platform_message_id']=='new')
        inbox.delete_message(self.one['id'],message['message_id'],message['platform_message_id'])
        self.assertEqual(inbox.inbox_summary()['new_count'],0)
        self.sync([self.old1,self.old2,new],self.times | {'new':self.stamp+10000},mode='full')
        self.assertEqual(inbox.inbox_summary()['new_count'],0)

    def test_quick_cache_does_not_restore_deleted_preview(self):
        latest=self.old2 | {'store_id':'2'}
        item=self.item | {'_last_timestamp':self.stamp,'_max_store_id':2,
                          '_list_update_time':self.stamp+1000}
        message=next(m for m in inbox.conversation(self.one['id'])['messages'] if m['platform_message_id']=='old2')
        inbox.delete_message(self.one['id'],message['message_id'],message['platform_message_id'])
        self.sync([self.old1,latest],self.times,item=item,mode='quick')
        self.assertIsNotNone(inbox._reuse_unchanged_conversation('xhs01','1'*64,item))
        row=inbox.list_conversations(alias='xhs01')['conversations'][0]
        self.assertNotIn('第二条旧消息',row['last_preview'])
        self.assertEqual(row['message_count'],1)

    def test_both_sync_modes_respect_deleted_conversation(self):
        self.delete_thread()
        page=MagicMock()
        holder={'messages':[self.old1,self.old2],'times':self.times}
        def read_history(*_):
            order=[m['external_id'] for m in reversed(holder['messages'])]
            events=[{'ok':True,'ids':set(order),'ordered_ids':order,'order_verified':True,
                     'render_verified':True,'count':len(order),'limit':30,'invalid_ids':False}]
            return events,holder['times'],object()
        with patch.object(inbox,'queued_browser',return_value=nullcontext()), \
             patch.object(inbox,'creator_page',return_value=nullcontext(page)), \
             patch.object(inbox,'observe_xiaohongshu_identity',return_value={}), \
             patch.object(inbox,'verify_origin'), patch.object(inbox,'check_challenge'), \
             patch.object(inbox,'require_xiaohongshu_identity'), patch.object(inbox,'_wait_for_list'), \
             patch.object(inbox,'verify_web_identity'), patch.object(inbox,'_gather_conversations',return_value=[self.item]), \
             patch.object(inbox,'_read_history',side_effect=read_history), \
             patch.object(inbox,'_select_conversation',return_value='main'), \
             patch.object(inbox,'_finish_history',side_effect=lambda *a,**k:(holder['messages'],True,1)), \
             patch.object(inbox,'_fetch_media',return_value={}):
            for mode in ('quick','full'):
                result=inbox.sync_account('xhs01',mode=mode)
                self.assertEqual(result['errors'],0)
                self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],0)
            new=self.message('new-mode','两种同步都保留的新消息')
            holder['messages']=[self.old1,self.old2,new]
            holder['times']=self.times | {'new-mode':self.cutoff+10000}
            for mode in ('quick','full'):
                result=inbox.sync_account('xhs01',mode=mode)
                self.assertEqual(result['errors'],0)
                self.assertEqual([m['platform_message_id'] for m in inbox.conversation(self.one['id'])['messages']],
                                 ['new-mode'])
            self.assertEqual(inbox.conversation(self.one['id'])['conversation']['new_count'],1)

    def test_account_nickname_is_in_conversation_and_cannot_cross_identity(self):
        row = inbox.conversation(self.one['id'])['conversation']
        self.assertEqual(row['account_nickname'],'真实昵称甲')
        self.assertTrue(row['identity_verified'])
        save_account_identity('xhs01','', '3'*64)
        client = TestClient(app, base_url='http://127.0.0.1:8765')
        client.get('/')
        account = next(a for a in client.get('/api/accounts').json()['accounts'] if a['alias']=='xhs01')
        self.assertFalse(account['nickname'])
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],0)

    def test_stale_conversation_delete_wont_remove_newer_message(self):
        snapshot = inbox.conversation(self.one['id'])['conversation']
        new = self.message('new','刚到的消息')
        self.sync([self.old1,self.old2,new],self.times | {'new':self.stamp+10000})
        with self.assertRaisesRegex(ValueError,'新|刷新'):
            inbox.delete_conversation(self.one['id'],messages_version=snapshot['messages_version'],
                                      alerts_version=snapshot['alerts_version'])
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],1)
        self.assertEqual(inbox.conversation(self.one['id'])['total'],3)

    def test_http_deletion_requires_confirmation_and_local_session(self):
        client = TestClient(app, base_url='http://127.0.0.1:8765')
        data = inbox.conversation(self.one['id'])
        message = data['messages'][0]
        body = {'message_id':message['message_id'],'platform_message_id':message['platform_message_id'],
                'confirm':'delete'}
        url = f"/api/inbox/conversations/{self.one['id']}/delete-message"
        self.assertEqual(client.post(url,json=body).status_code,403)
        client.get('/')
        self.assertEqual(client.post(url,json=body|{'confirm':'no'},
                                     headers={'origin':'http://127.0.0.1:8765'}).status_code,400)
        self.assertEqual(client.post(url,json=body,headers={'origin':'https://evil.example'}).status_code,403)
        self.assertEqual(client.post(url,json=body,headers={'origin':'http://127.0.0.1:8765'}).status_code,200)
        self.assertEqual(inbox.conversation(self.one['id'])['total'],1)

    def test_http_conversation_delete_requires_explicit_confirmation_and_current_snapshot(self):
        client = TestClient(app,base_url='http://127.0.0.1:8765')
        client.get('/')
        headers={'origin':'http://127.0.0.1:8765'}
        snapshot=inbox.conversation(self.one['id'])['conversation']
        body={'confirm':'delete','messages_version':snapshot['messages_version'],'alerts_version':snapshot['alerts_version']}
        url=f"/api/inbox/conversations/{self.one['id']}/delete"
        self.assertEqual(client.post(url,json=body|{'confirm':'no'},headers=headers).status_code,400)
        self.assertEqual(client.post(url,json=body|{'messages_version':-1},headers=headers).status_code,400)
        self.assertEqual(client.post(url,json=body,headers=headers).status_code,200)
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],0)


if __name__ == '__main__':
    unittest.main()
