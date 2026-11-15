"""Bulk deletion uses disposable accounts and never contacts the platform."""
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import inbox
from publisher import add_account, browser_lock, db, save_account_identity
from workbench import app


class InboxBulkTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env=patch.dict(os.environ,{'OWN_PUBLISHER_HOME':self.folder.name})
        env.start()
        self.addCleanup(env.stop)
        for alias,identity in [('xhs01','1'*64),('xhs02','2'*64)]:
            add_account(alias,'xiaohongshu')
            save_account_identity(alias,alias,identity)
        self.stamp=1794640000000
        self.messages=[self.message('m1'),self.message('m2'),self.message('m3')]
        self.item={'external_id':'customer-a','location':'main','display_name':'匹配客户',
                   'last_preview':'m3','last_time_label':'今天','unread_before':0}
        self.times={m['external_id']:self.stamp+i*1000 for i,m in enumerate(self.messages)}
        self.save(self.messages)
        inbox._save_conversation('xhs02','2'*64,self.item,self.messages,self.times,True,1)
        self.one=inbox.list_conversations(alias='xhs01')['conversations'][0]

    @staticmethod
    def message(mid):
        return {'external_id':mid,'store_id':mid,'direction':'incoming','content_type':'1',
                'body':mid,'time_label':'今天'}

    def save(self,messages,times=None,mode=None):
        ids=[m['external_id'] for m in reversed(messages)]
        event={'ok':True,'ids':set(ids),'ordered_ids':ids,'order_verified':True,
               'render_verified':True,'count':len(ids),'limit':30,'invalid_ids':False}
        return inbox._save_conversation('xhs01','1'*64,self.item,messages,times or self.times,
                                        True,1,observations=[event],sync_mode=mode)

    def test_batch_messages_respect_selection_and_never_backfill(self):
        selected=inbox.message_selection(self.one['id'])['messages']
        first=[m for m in selected if m['platform_message_id'] in ('m1','m3')]
        self.assertEqual(inbox.delete_messages(self.one['id'],first)['deleted'],2)
        for mode in ('quick','full'):
            self.save(self.messages,mode=mode)
            self.assertEqual([m['platform_message_id'] for m in inbox.conversation(self.one['id'])['messages']],['m2'])
        self.assertEqual(inbox.list_conversations(alias='xhs02')['conversations'][0]['message_count'],3)

    def test_batch_messages_are_atomic_for_stale_or_forged_selection(self):
        selected=inbox.message_selection(self.one['id'])['messages']
        invalid=[selected[0],selected[1] | {'message_id':'xhm1_'+'f'*64}]
        with self.assertRaises(ValueError): inbox.delete_messages(self.one['id'],invalid)
        self.assertEqual(inbox.conversation(self.one['id'])['total'],3)
        with self.assertRaises(ValueError): inbox.delete_messages(self.one['id'],[selected[0],selected[0]])
        self.assertEqual(inbox.conversation(self.one['id'])['total'],3)

    def test_batch_handles_more_than_a_thousand_messages(self):
        messages=[self.message(f'many-{i}') for i in range(1200)]
        times={m['external_id']:self.stamp+i*100 for i,m in enumerate(messages)}
        item=self.item | {'external_id':'large','display_name':'较长会话'}
        inbox._save_conversation('xhs01','1'*64,item,messages,times,True,1)
        large=next(c for c in inbox.list_conversations(alias='xhs01')['conversations']
                   if c['display_name']=='较长会话')
        selected=inbox.message_selection(large['id'])['messages']
        self.assertEqual(len(selected),1200)
        self.assertEqual(inbox.delete_messages(large['id'],selected)['deleted'],1200)
        self.assertEqual(inbox.conversation(large['id'])['total'],0)

    def test_select_all_covers_filtered_results_beyond_the_first_page(self):
        with db() as con:
            inbox.ensure_schema(con)
            con.executemany('''INSERT INTO inbox_conversations(alias,identity_hash,external_id,location,display_name)
                               VALUES('xhs01',? ,?,'main',?)''',
                            [('1'*64,'extra-'+str(i),'匹配客户 '+str(i)) for i in range(125)])
        page=inbox.list_conversations(alias='xhs01',query='匹配',limit=100)
        selected=inbox.conversation_selection(alias='xhs01',query='匹配')['conversations']
        self.assertEqual((page['total'],len(page['conversations']),len(selected)),(126,100,126))
        self.assertEqual(inbox.delete_conversations(selected)['deleted'],126)
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],0)
        self.assertEqual(inbox.list_conversations(alias='xhs02')['total'],1)

    def test_batch_conversations_rejects_stale_snapshot_without_partial_deletion(self):
        selected=inbox.conversation_selection()['conversations']
        new=self.message('new')
        self.save(self.messages+[new],self.times | {'new':self.stamp+10000})
        with self.assertRaisesRegex(ValueError,'刷新|变化'): inbox.delete_conversations(selected)
        self.assertEqual(inbox.list_conversations()['total'],2)
        self.assertEqual(inbox.conversation(self.one['id'])['total'],4)

    def test_select_all_is_a_snapshot_not_a_live_filter(self):
        selected=inbox.conversation_selection(alias='xhs01')['conversations']
        other=self.item | {'external_id':'later','display_name':'稍后出现'}
        inbox._save_conversation('xhs01','1'*64,other,self.messages,self.times,True,1)
        inbox.delete_conversations(selected)
        remaining=inbox.list_conversations(alias='xhs01')['conversations']
        self.assertEqual([r['display_name'] for r in remaining],['稍后出现'])

    def test_batch_deleted_conversations_reopen_only_with_verified_new_messages(self):
        selected=inbox.conversation_selection()['conversations']
        cutoff=self.stamp+20000
        with patch('inbox.time.time',return_value=cutoff/1000):
            inbox.delete_conversations(selected)
        for mode in ('quick','full'):
            self.save(self.messages,mode=mode)
            self.assertEqual(inbox.list_conversations()['total'],0)
        new=self.message('new-after-delete')
        times=self.times | {'new-after-delete':cutoff+10000}
        for mode in ('quick','full'):
            self.save(self.messages+[new],times,mode=mode)
        data=inbox.conversation(self.one['id'])
        self.assertEqual([m['platform_message_id'] for m in data['messages']],['new-after-delete'])
        self.assertEqual(data['conversation']['new_count'],1)
        self.assertEqual(inbox.list_conversations(alias='xhs02')['total'],0)

    def test_two_aliases_of_one_account_can_be_selected_together(self):
        add_account('xhs03','xiaohongshu')
        save_account_identity('xhs03','xhs01','1'*64)
        inbox._save_conversation('xhs03','1'*64,self.item,self.messages,self.times,True,1)
        selected=[s for s in inbox.conversation_selection()['conversations']
                  if s['id'] != inbox.list_conversations(alias='xhs02')['conversations'][0]['id']]
        self.assertEqual(inbox.delete_conversations(selected)['deleted'],2)
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],0)
        self.assertEqual(inbox.list_conversations(alias='xhs03')['total'],0)
        self.assertEqual(inbox.list_conversations(alias='xhs02')['total'],1)

    def test_http_batch_requires_local_session_and_explicit_confirmation(self):
        client=TestClient(app,base_url='http://127.0.0.1:8765')
        selected=inbox.message_selection(self.one['id'])['messages']
        url=f"/api/inbox/conversations/{self.one['id']}/delete-messages"
        body={'confirm':'delete','messages':selected}
        self.assertEqual(client.post(url,json=body).status_code,403)
        client.get('/')
        headers={'origin':'http://127.0.0.1:8765'}
        self.assertEqual(client.post(url,json=body|{'confirm':'no'},headers=headers).status_code,400)
        self.assertEqual(client.post(url,json=body,headers={'origin':'https://evil.example'}).status_code,403)
        self.assertEqual(client.post(url,json=body,headers=headers).json()['deleted'],3)
        conversations=client.get('/api/inbox/conversation-selection?alias=xhs01').json()['conversations']
        delete_url='/api/inbox/delete-conversations'
        self.assertEqual(client.post(delete_url,json={'confirm':'no','conversations':conversations},headers=headers).status_code,400)
        self.assertEqual(client.post(delete_url,json={'confirm':'delete','conversations':conversations},headers=headers).json()['deleted'],1)
        self.assertEqual(inbox.list_conversations(alias='xhs02')['total'],1)

    def test_batch_delete_waits_for_a_running_browser_operation(self):
        client=TestClient(app,base_url='http://127.0.0.1:8765')
        client.get('/')
        headers={'origin':'http://127.0.0.1:8765'}
        selected=inbox.conversation_selection(alias='xhs01')['conversations']
        with browser_lock():
            result=client.post('/api/inbox/delete-conversations',headers=headers,
                               json={'confirm':'delete','conversations':selected})
        self.assertEqual(result.status_code,400)
        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],1)


if __name__=='__main__':
    unittest.main()
