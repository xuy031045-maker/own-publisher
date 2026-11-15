"""Exercise bulk selection in a real browser with disposable local data."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from playwright.sync_api import sync_playwright
import inbox
from publisher import add_account,db,save_account_identity


class InboxBulkUiTests(unittest.TestCase):
    def test_bulk_messages_and_filtered_conversations(self):
        root=Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'OWN_PUBLISHER_HOME':folder}):
            add_account('xhs01','xiaohongshu')
            save_account_identity('xhs01','测试昵称','1'*64)
            item={'external_id':'bulk-main','location':'main','display_name':'批量会话 000',
                  'last_preview':'测试','last_time_label':'今天','unread_before':0}
            messages=[{'external_id':f'm{i:03}','store_id':str(i),'direction':'incoming',
                       'content_type':'1','body':f'测试消息 {i}','time_label':'今天'} for i in range(220)]
            stamps={m['external_id']:1794640000000+i*1000 for i,m in enumerate(messages)}
            inbox._save_conversation('xhs01','1'*64,item,messages,stamps,True,1)
            with db() as con:
                con.executemany('''INSERT INTO inbox_conversations(alias,identity_hash,external_id,location,display_name)
                                   VALUES('xhs01',?,?,'main',?)''',
                    [('1'*64,f'bulk-{i}',f'批量会话 {i:03}') for i in range(1,105)])
                con.execute('''INSERT INTO inbox_conversations(alias,identity_hash,external_id,location,display_name)
                               VALUES('xhs01',?,'keep','main','保留会话')''',('1'*64,))
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0))
                port=listener.getsockname()[1]
            server=subprocess.Popen([sys.executable,'-X','utf8','-m','uvicorn','workbench:app',
                '--host','127.0.0.1','--port',str(port),'--log-level','error'],cwd=root,env=os.environ.copy(),
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                base=f'http://127.0.0.1:{port}'
                op=build_opener(ProxyHandler({}))
                deadline=time.monotonic()+15
                while True:
                    if server.poll() is not None: self.fail('隔离工作台未启动。')
                    try:
                        with op.open(base+'/',timeout=1): break
                    except (URLError,OSError):
                        if time.monotonic()>=deadline: self.fail('隔离工作台启动超时。')
                        time.sleep(.1)
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':900})
                        page.goto(base+'/#inbox',wait_until='domcontentloaded')
                        page.get_by_role('button',name='批量选择',exact=True).wait_for()
                        page.locator('#inbox-list .inbox-item').first.click()
                        page.get_by_role('button',name='批量选择消息').click()
                        page.set_viewport_size({'width':390,'height':844})
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                        self.assertTrue(page.evaluate("document.querySelector('.inbox-message-bulk').scrollWidth <= document.querySelector('.inbox-message-bulk').clientWidth"))
                        page.set_viewport_size({'width':1440,'height':900})
                        checks=page.locator('#inbox-thread [data-select-message]')
                        self.assertEqual(checks.count(),200)
                        checks.first.check()
                        checks.last.check()
                        self.assertIn('已选 2 条',page.locator('.inbox-message-bulk').inner_text())
                        actions=iter([False,True,True,False,True])
                        prompts=[]
                        def answer(dialog):
                            prompts.append(dialog.message)
                            dialog.accept() if next(actions) else dialog.dismiss()
                        page.on('dialog',answer)
                        page.locator('[data-thread-action="delete-messages"]').click()
                        self.assertIn('已选 2 条',page.locator('.inbox-message-bulk').inner_text())
                        page.locator('[data-thread-action="delete-messages"]').click()
                        page.get_by_role('button',name='批量选择消息').wait_for()
                        self.assertEqual(inbox.conversation(1)['total'],218)
                        page.get_by_role('button',name='批量选择消息').click()
                        page.get_by_role('button',name='全选本地消息').click()
                        page.get_by_text('已选 218 条').wait_for()
                        page.locator('[data-thread-action="delete-messages"]').click()
                        page.get_by_role('button',name='批量选择消息').wait_for()
                        self.assertEqual(inbox.conversation(1)['total'],0)
                        page.locator('#inbox-search').fill('批量会话')
                        page.wait_for_function("document.querySelector('#inbox-count').textContent === '105 个'")
                        page.get_by_role('button',name='批量选择',exact=True).click()
                        page.locator('[data-bulk-conversation]').first.check()
                        self.assertIn('已选 1 个',page.locator('#inbox-bulk-bar').inner_text())
                        page.locator('#inbox-search').fill('不匹配')
                        page.wait_for_function("document.querySelector('#inbox-count').textContent === '0 个'")
                        self.assertIn('已选 0 个',page.locator('#inbox-bulk-bar').inner_text())
                        page.locator('#inbox-search').fill('批量会话')
                        page.wait_for_function("document.querySelector('#inbox-count').textContent === '105 个'")
                        page.get_by_role('button',name='全选筛选结果').click()
                        page.get_by_text('已选 105 个').wait_for()
                        page.set_viewport_size({'width':390,'height':844})
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                        self.assertTrue(page.evaluate("document.querySelector('#inbox-bulk-bar').scrollWidth <= document.querySelector('#inbox-bulk-bar').clientWidth"))
                        page.set_viewport_size({'width':1440,'height':900})
                        # A newly appearing matching conversation was never part of the selection snapshot.
                        later=item | {'external_id':'bulk-later','display_name':'批量会话 新来的'}
                        inbox._save_conversation('xhs01','1'*64,later,[],{},True,1)
                        page.get_by_role('button',name='删除所选').last.click()
                        self.assertIn('已选 105 个',page.locator('#inbox-bulk-bar').inner_text())
                        page.get_by_role('button',name='删除所选').last.click()
                        page.wait_for_function("document.querySelector('#inbox-count').textContent === '1 个'")
                        self.assertEqual(inbox.list_conversations(alias='xhs01')['total'],2)
                        self.assertIn('小红书原会话不受影响',prompts[-1])
                        self.assertEqual(len(prompts),5)
                        page.set_viewport_size({'width':390,'height':844})
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    finally:
                        browser.close()
            finally:
                server.terminate()
                try: server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__=='__main__':
    unittest.main()
