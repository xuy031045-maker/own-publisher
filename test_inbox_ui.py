"""Exercise the real local workbench against a disposable, synthetic database."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import URLError
from urllib.request import build_opener, ProxyHandler
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import sync_playwright

import inbox
from publisher import add_account, prepare_batch_data, save_account_identity


class InboxUiTests(unittest.TestCase):
    def test_names_icons_and_confirmed_deletion_in_the_workbench(self):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'OWN_PUBLISHER_HOME':folder}):
            add_account('xhs01','xiaohongshu','本地标签甲')
            save_account_identity('xhs01','真实昵称甲','1'*64)
            add_account('xhs02','xiaohongshu','尚未登录')
            now = int(time.time()*1000)
            item = {'external_id':'fixture-user','location':'main','display_name':'测试会话',
                    'last_preview':'旧消息二','last_time_label':'今天','unread_before':0}
            old1 = {'external_id':'old1','store_id':'1','direction':'incoming','content_type':'1',
                    'body':'旧消息一','time_label':'今天'}
            old2 = old1 | {'external_id':'old2','store_id':'2','body':'旧消息二'}
            stamps = {'old1':now-60000,'old2':now-30000}
            inbox._save_conversation('xhs01','1'*64,item,[old1,old2],stamps,True,1)
            image = Path(folder)/'fixture.png'
            Image.new('RGB',(4,4),'red').save(image)
            prepare_batch_data({'post':{'title':'测试图文','body':'测试','tags':[],
                                        'images':[str(image)]},'targets':['xhs01']},Path(folder))
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',0))
                port = listener.getsockname()[1]
            env = os.environ.copy()
            env['OWN_PUBLISHER_HOME'] = folder
            server = subprocess.Popen([sys.executable,'-X','utf8','-m','uvicorn','workbench:app',
                                       '--host','127.0.0.1','--port',str(port),'--log-level','error'],
                                      cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                base=f'http://127.0.0.1:{port}'
                op=build_opener(ProxyHandler({}))
                deadline=time.monotonic()+15
                while True:
                    if server.poll() is not None:
                        self.fail('隔离工作台未能启动。')
                    try:
                        with op.open(base+'/',timeout=1):
                            break
                    except (URLError,OSError):
                        if time.monotonic() >= deadline:
                            self.fail('隔离工作台启动超时。')
                        time.sleep(.1)
                with sync_playwright() as pw:
                    browser=pw.chromium.launch(channel='msedge',headless=True)
                    try:
                        page=browser.new_page(viewport={'width':1440,'height':900})
                        page.goto(base+'/#compose',wait_until='domcontentloaded')
                        page.locator('#target-list .target-row').first.wait_for()
                        targets=page.locator('#target-list .target-row')
                        self.assertEqual(targets.count(),2)
                        self.assertEqual(targets.nth(0).locator('strong').inner_text(),'真实昵称甲')
                        self.assertIn('xhs01',targets.nth(0).inner_text())
                        self.assertEqual(targets.nth(1).locator('strong').inner_text(),'未核验')
                        self.assertIn('请先检查账号',targets.nth(1).inner_text())
                        icons=page.locator('.rail .nav-icon').evaluate_all('''items => items.map(e => ({
                          width:e.getBoundingClientRect().width,stroke:getComputedStyle(e).strokeWidth}))''')
                        self.assertEqual(len({(i['width'],i['stroke']) for i in icons}),1)
                        self.assertGreater(icons[0]['width'],0)
                        targets.nth(0).locator('input').check()
                        self.assertIn('真实昵称甲',page.locator('#individual-list summary').inner_text())
                        page.locator('.rail a[href="#history"]').click()
                        page.locator('#history-list .record-task').first.wait_for()
                        record=page.locator('#history-list .record-account').first
                        self.assertEqual(record.locator('strong').inner_text(),'真实昵称甲')
                        self.assertIn('xhs01',record.inner_text())
                        page.locator('.rail a[href="#inbox"]').click()
                        page.locator('#inbox-list .inbox-item').first.wait_for()
                        source=page.locator('#inbox-list .inbox-item-meta').first.inner_text()
                        self.assertIn('真实昵称甲',source)
                        self.assertIn('xhs01',source)
                        self.assertIn('真实昵称甲',page.locator('#inbox-account').inner_text())
                        def legacy_thread(route):
                            response=route.fetch()
                            payload=response.json()
                            if 'conversation' in payload:
                                for key in ('history_locally_filtered','account_nickname','identity_verified'):
                                    payload['conversation'].pop(key,None)
                            route.fulfill(response=response,json=payload)
                        page.route('**/api/inbox/conversations/*?*',legacy_thread)
                        page.locator('#inbox-list .inbox-item').first.click()
                        page.get_by_text('本机删除功能需要重启工作台后启用。').wait_for()
                        self.assertEqual(page.locator('#inbox-thread .delete-message, #inbox-thread .delete-thread').count(),0)
                        self.assertIn('真实昵称甲',page.locator('#inbox-thread .inbox-thread-head').inner_text())
                        page.unroute('**/api/inbox/conversations/*?*',legacy_thread)
                        page.locator('#inbox-list .inbox-item').first.click()
                        page.locator('#inbox-thread .delete-message').first.wait_for()
                        self.assertEqual(page.locator('#inbox-thread .inbox-message').count(),2)
                        prompts=[]
                        actions=iter((False,True,False,True))
                        def answer(dialog):
                            prompts.append(dialog.message)
                            dialog.accept() if next(actions) else dialog.dismiss()
                        page.on('dialog',answer)
                        page.locator('#inbox-thread .delete-message').last.click()
                        self.assertEqual(page.locator('#inbox-thread .inbox-message').count(),2)
                        page.locator('#inbox-thread .delete-message').last.click()
                        page.wait_for_function("document.querySelectorAll('#inbox-thread .inbox-message').length === 1")
                        self.assertIn('旧消息一',page.locator('#inbox-thread .inbox-messages').inner_text())
                        self.assertNotIn('旧消息二',page.locator('#inbox-thread .inbox-messages').inner_text())
                        page.reload(wait_until='domcontentloaded')
                        page.locator('#inbox-list .inbox-item').first.wait_for()
                        page.locator('#inbox-list .inbox-item').first.click()
                        page.locator('#inbox-thread .delete-thread').wait_for()
                        self.assertEqual(page.locator('#inbox-thread .inbox-message').count(),1)
                        page.locator('#inbox-thread .delete-thread').click()
                        self.assertEqual(page.locator('#inbox-list .inbox-item').count(),1)
                        page.locator('#inbox-thread .delete-thread').click()
                        page.wait_for_function("document.querySelectorAll('#inbox-list .inbox-item').length === 0")
                        self.assertEqual(len(prompts),4)
                        self.assertTrue(all('小红书原会话不受影响' in prompt for prompt in prompts))
                        new = old1 | {'external_id':'new','store_id':'3','body':'删除后的新消息'}
                        new_time=int(time.time()*1000)+10000
                        order=['new','old2','old1']
                        event={'ok':True,'ids':set(order),'ordered_ids':order,'order_verified':True,
                               'render_verified':True,'count':3,'limit':30,'invalid_ids':False}
                        inbox._save_conversation('xhs01','1'*64,item,[old1,old2,new],
                                                 stamps|{'new':new_time},True,1,observations=[event],sync_mode='full')
                        page.reload(wait_until='domcontentloaded')
                        page.locator('#inbox-list .inbox-item').first.wait_for()
                        page.locator('#inbox-list .inbox-item').first.click()
                        page.locator('#inbox-thread .inbox-message').first.wait_for()
                        text=page.locator('#inbox-thread .inbox-messages').inner_text()
                        self.assertIn('删除后的新消息',text)
                        self.assertNotIn('旧消息一',text)
                        self.assertNotIn('旧消息二',text)
                        page.set_viewport_size({'width':390,'height':844})
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                        self.assertTrue(page.locator('.rail .nav-icon').first.is_visible())
                    finally:
                        browser.close()
            finally:
                server.terminate()
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
