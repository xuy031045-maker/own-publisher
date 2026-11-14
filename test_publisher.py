import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from browser import LoginNeeded, PublishUncertain, VerificationStopped, check_challenge, confirmed, fill_post, prepare_submit, submit_once
from publisher import acknowledge_verification, add_account, batch_status, browser_lock, cancel_verification, consume_verification_ack, db, prepare_batch, prepare_batch_data, run_batch, save_account_identity, set_state, verification_cancel_requested
from xiaohongshu import BEIJING, image_info, post_options, schedule_time


class Batches(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"OWN_PUBLISHER_HOME": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        Image.new("RGB", (2, 2), "red").save(self.root / "sample.jpg")
        for index in range(1, 12):
            add_account(f"a{index}", "xiaohongshu" if index < 6 else "douyin")
            if index < 6:
                save_account_identity(f"a{index}", f"账号{index}", f"{index:064x}")

    def batch(self, aliases):
        manifest = self.root / "post.json"
        manifest.write_text(json.dumps({
            "post": {"title": "测试", "images": ["sample.jpg"]},
            "targets": aliases,
        }), encoding="utf-8")
        return prepare_batch(manifest)["batch"]

    def variant(self, aliases, **options):
        post = {"title": "测试", "images": ["sample.jpg"], **options}
        return prepare_batch_data({"post": post, "targets": aliases}, self.root)

    def test_changed_schedule_requires_explicit_duplicate_approval(self):
        first_time = (datetime.now(BEIJING) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
        later_time = (datetime.now(BEIJING) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M")
        first = self.variant(["a1"], schedule=first_time)["batch"]
        run_batch(first, first, adapter=lambda *_: None)
        preview = self.variant(["a1"], schedule=later_time)
        second = preview["batch"]
        self.assertEqual(preview["tasks"][0]["duplicate_matches"][0]["batch"], first)
        self.assertFalse(preview["tasks"][0]["duplicate_matches"][0]["same_options"])
        result = run_batch(second, second, adapter=lambda *_: self.fail("duplicate ran"))["tasks"][0]
        self.assertEqual(result["state"], "duplicate")
        called = []
        result = run_batch(second, second, retry=True, allow_duplicate_accounts=["a1"],
                           adapter=lambda task, _: called.append(task["alias"]))["tasks"][0]
        self.assertEqual((called, result["state"]), (["a1"], "submitted"))
        with db() as con:
            row = con.execute("SELECT previous_batch FROM duplicate_confirmations WHERE batch=? AND alias=?",
                              (second, "a1")).fetchone()
        self.assertEqual(row["previous_batch"], first)

    def test_changed_declaration_is_not_silently_republished(self):
        first = self.variant(["a1"])["batch"]
        run_batch(first, first, adapter=lambda *_: None)
        second = self.variant(["a1"], declaration="ai")["batch"]
        result = run_batch(second, second, adapter=lambda *_: self.fail("duplicate ran"))["tasks"][0]
        self.assertEqual(result["state"], "duplicate")
        self.assertIn("声明", result["detail"])

    def test_unconfirmed_or_identical_previous_post_cannot_be_overridden(self):
        first = self.variant(["a1", "a2"])["batch"]
        set_state(first, "a1", "uncertain", "需要核对")
        changed = self.variant(["a1"], declaration="ai")["batch"]
        result = run_batch(changed, changed, allow_duplicate_accounts=["a1"],
                           adapter=lambda *_: self.fail("uncertain duplicate ran"))["tasks"][0]
        self.assertEqual(result["state"], "duplicate")
        set_state(first, "a1", "submitted")
        identical = self.variant(["a1"])["batch"]
        result = run_batch(identical, identical, allow_duplicate_accounts=["a1"],
                           adapter=lambda *_: self.fail("identical duplicate ran"))["tasks"][0]
        self.assertEqual(result["state"], "duplicate")

    def test_ten_accounts_and_duplicate_detection(self):
        aliases = [f"a{index}" for index in range(1, 11)]
        batch = self.batch(aliases)
        self.assertEqual(batch, self.batch(aliases[::-1]))
        with self.assertRaises(ValueError):
            self.batch(aliases + ["a11"])
        called = []
        adapter = lambda task, _: called.append(task["alias"])
        with self.assertRaises(ValueError):
            run_batch(batch, "", adapter=adapter)
        run_batch(batch, batch, adapter=adapter)
        run_batch(batch, batch, adapter=adapter)
        subset = self.batch(["a1"])
        run_batch(subset, subset, adapter=adapter)
        self.assertEqual(called, aliases)
        self.assertEqual(batch_status(subset)["tasks"][0]["state"], "duplicate")

    def test_retry_skips_uncertain_and_interrupted(self):
        batch = self.batch(["a1", "a2", "a3"])
        set_state(batch, "a3", "running")

        def first(task, _):
            if task["alias"] == "a1":
                raise PublishUncertain("请核对")
            raise LoginNeeded("请登录")

        results = run_batch(batch, batch, adapter=first)["tasks"]
        self.assertEqual([r["state"] for r in results], ["uncertain", "login_needed", "uncertain"])
        retried = []
        run_batch(batch, batch, retry=True, adapter=lambda task, _: retried.append(task["alias"]))
        self.assertEqual(retried, ["a2"])

    def test_changed_image_stops_before_platform(self):
        batch = self.batch(["a1"])
        (self.root / "sample.jpg").write_bytes(b"changed")
        result = run_batch(batch, batch, adapter=lambda *_: self.fail("adapter ran"))["tasks"][0]
        self.assertEqual(result["state"], "failed")

    def test_same_platform_account_cannot_be_selected_twice(self):
        save_account_identity("a2", "账号1", f"{1:064x}")
        with self.assertRaisesRegex(ValueError, "同一个小红书账号"):
            self.batch(["a1", "a2"])

    def test_same_account_under_another_alias_is_not_republished(self):
        first = self.batch(["a1"])
        called = []
        run_batch(first, first, adapter=lambda task, _: called.append(task["alias"]))
        save_account_identity("a2", "账号1", f"{1:064x}")
        second = self.batch(["a2"])
        result = run_batch(second, second, adapter=lambda task, _: called.append(task["alias"]))
        self.assertEqual(called, ["a1"])
        self.assertEqual(result["tasks"][0]["state"], "duplicate")

    def test_old_alias_records_still_protect_against_duplicates(self):
        add_account("legacy", "xiaohongshu")
        old = self.batch(["legacy"])
        set_state(old, "legacy", "submitted")
        save_account_identity("legacy", "原账号", "a" * 64)
        current = self.batch(["legacy"])
        self.assertNotEqual(old, current)
        result = run_batch(current, current, adapter=lambda *_: self.fail("adapter ran"))["tasks"][0]
        self.assertEqual(result["state"], "duplicate")

    def test_new_identity_on_same_alias_gets_a_new_preview(self):
        first = self.batch(["a1"])
        called = []
        run_batch(first, first, adapter=lambda task, _: called.append(task["expected_identity"]))
        save_account_identity("a1", "另一个账号", "f" * 64)
        second = self.batch(["a1"])
        self.assertNotEqual(first, second)
        run_batch(second, second, adapter=lambda task, _: called.append(task["expected_identity"]))
        self.assertEqual(called, [f"{1:064x}", "f" * 64])

    def test_unverified_account_requires_check_and_new_preview(self):
        add_account("new", "xiaohongshu")
        original = self.batch(["new"])
        result = run_batch(original, original, adapter=lambda *_: self.fail("adapter ran"))["tasks"][0]
        self.assertEqual(result["state"], "failed")
        self.assertIn("重新生成预览", result["detail"])
        save_account_identity("new", "新账号", "f" * 64)
        checked = self.batch(["new"])
        self.assertNotEqual(checked, original)
        called = []
        run_batch(checked, checked, adapter=lambda task, _: called.append(task["alias"]))
        self.assertEqual(called, ["new"])

    def test_cli_and_mcp_preview_exposes_the_confirmed_account(self):
        original = self.batch(["a1"])
        preview = batch_status(original, preview=True)["tasks"][0]
        self.assertEqual(preview["nickname"], "账号1")
        self.assertTrue(preview["identity_verified"])
        self.assertNotIn("identity_hash", preview)
        save_account_identity("a1", "另一个账号", "f" * 64)
        self.assertEqual(batch_status(original, preview=True)["tasks"][0]["nickname"], "账号1")
        changed = self.batch(["a1"])
        self.assertEqual(batch_status(changed, preview=True)["tasks"][0]["nickname"], "另一个账号")
        add_account("unchecked", "xiaohongshu")
        unchecked = self.batch(["unchecked"])
        self.assertFalse(batch_status(unchecked, preview=True)["tasks"][0]["identity_verified"])
        self.assertNotIn("identity_verified", batch_status(original)["tasks"][0])

    def test_verification_ack_is_single_use_and_cleared_on_state_change(self):
        batch = self.batch(["a1"])
        set_state(batch, "a1", "verifying", "等待验证")
        with self.assertRaisesRegex(ValueError, "没有运行中"):
            acknowledge_verification(batch)
        with browser_lock():
            self.assertEqual(acknowledge_verification(batch), {"accepted": True})
            self.assertTrue(consume_verification_ack(batch, "a1"))
            self.assertFalse(consume_verification_ack(batch, "a1"))
            acknowledge_verification(batch)
            set_state(batch, "a1", "running")
            set_state(batch, "a1", "verifying", "再次验证")
            self.assertFalse(consume_verification_ack(batch, "a1"))

    def test_cancel_before_submit_releases_lock_and_leaves_other_accounts_queued(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        import browser

        batch = self.batch(["a1", "a2"])
        page = Mock()
        page.is_closed.return_value = False

        @contextmanager
        def creator(*args, **kwargs):
            yield page

        def user_cancel(alias, detail):
            if detail:
                self.assertEqual(cancel_verification(batch), {"accepted": True})

        with patch.object(browser, "creator_page", creator), patch.object(browser, "verify_origin"), \
             patch.object(browser, "check_challenge", side_effect=LoginNeeded("请登录")), \
             patch.object(browser, "prepare_submit") as submit:
            result = run_batch(batch, batch, on_verification=user_cancel)
        self.assertEqual([task["state"] for task in result["tasks"]], ["cancelled", "queued"])
        self.assertFalse(verification_cancel_requested(batch, "a1"))
        submit.assert_not_called()
        with browser_lock():
            pass

    def test_cancel_after_submit_requires_reconciliation_and_does_not_repeat(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        import browser

        batch = self.batch(["a1", "a2"])
        page, click = Mock(), Mock()

        @contextmanager
        def creator(*args, **kwargs):
            yield page

        @contextmanager
        def guard(*args, **kwargs):
            yield {"allowed": 1, "blocked": False}

        def waiting(page, platform, on_verification, guard, acknowledgement, cancelled, retry_submit=None):
            on_verification("提交后需要验证")
            self.assertTrue(cancelled())
            raise VerificationStopped("已取消等待验证。")

        with patch.object(browser, "creator_page", creator), \
             patch.object(browser, "verify_origin"), patch.object(browser, "check_challenge"), \
             patch.object(browser, "require_xiaohongshu_identity", return_value=f"{1:064x}"), \
             patch.object(browser, "fill_post"), patch.object(browser, "set_xiaohongshu_options"), \
             patch.object(browser, "prepare_submit", return_value=click), \
             patch.object(browser, "wait_for_upload_evidence"), \
             patch.object(browser, "verify_xiaohongshu_submission", guard), \
             patch.object(browser, "wait_for_submission", side_effect=waiting):
            result = run_batch(batch, batch, on_verification=lambda alias, detail: cancel_verification(batch) if detail else None)
        self.assertEqual([task["state"] for task in result["tasks"]], ["uncertain", "queued"])
        click.assert_called_once_with()
        self.assertFalse(verification_cancel_requested(batch, "a1"))

    def test_cli_can_resume_a_publisher_in_another_process(self):
        batch = self.batch(["a1"])
        script = r'''
import json, sys, time
import browser
from publisher import run_batch
def simulated_publish(account, post, on_verification=None, verification_acknowledged=None, verification_cancelled=None):
    on_verification("等待人工验证")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if verification_acknowledged():
            on_verification(None)
            return
        time.sleep(0.05)
    raise TimeoutError("verification was not acknowledged")
browser.publish = simulated_publish
print(json.dumps(run_batch(sys.argv[1], sys.argv[1], on_verification=lambda *_: None)))
'''
        worker = subprocess.Popen([sys.executable, "-X", "utf8", "-c", script, batch],
                                  cwd=Path(__file__).parent, env=os.environ.copy(),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.addCleanup(lambda: worker.kill() if worker.poll() is None else None)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and worker.poll() is None:
            if batch_status(batch)["tasks"][0]["state"] == "verifying":
                break
            time.sleep(0.05)
        self.assertEqual(batch_status(batch)["tasks"][0]["state"], "verifying")
        result = subprocess.run([sys.executable, "-X", "utf8", "cli.py", "continue", batch],
                                cwd=Path(__file__).parent, env=os.environ.copy(),
                                capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["accepted"])
        out, err = worker.communicate(timeout=10)
        self.assertEqual(worker.returncode, 0, err)
        self.assertEqual(json.loads(out)["tasks"][0]["state"], "submitted")

    def test_mcp_can_resume_its_own_waiting_batch(self):
        import asyncio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        batch = self.batch(["a1"])
        server = r'''
import time
import browser
import agent_server
def simulated_publish(account, post, on_verification=None, verification_acknowledged=None, verification_cancelled=None):
    on_verification("等待人工验证")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if verification_acknowledged():
            on_verification(None)
            return
        time.sleep(0.05)
    raise TimeoutError("verification was not acknowledged")
browser.publish = simulated_publish
agent_server.server.run(transport="stdio")
'''

        async def exercise():
            params = StdioServerParameters(command=sys.executable, args=["-X", "utf8", "-c", server],
                                           env=os.environ.copy(), cwd=str(Path(__file__).parent))
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    publishing = asyncio.create_task(session.call_tool(
                        "publish_batch", {"batch_id": batch, "confirmation": batch, "interactive": True}))
                    try:
                        for _ in range(100):
                            if batch_status(batch)["tasks"][0]["state"] == "verifying":
                                break
                            await asyncio.sleep(0.05)
                        self.assertEqual(batch_status(batch)["tasks"][0]["state"], "verifying")
                        resumed = await asyncio.wait_for(session.call_tool(
                            "continue_verification", {"batch_id": batch}), timeout=5)
                        self.assertFalse(resumed.isError)
                        finished = await asyncio.wait_for(publishing, timeout=5)
                        self.assertFalse(finished.isError)
                    finally:
                        if not publishing.done():
                            publishing.cancel()
            self.assertEqual(batch_status(batch)["tasks"][0]["state"], "submitted")

        asyncio.run(exercise())

    def test_changed_account_is_stopped_before_filling_or_submitting(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        import browser

        batch = self.batch(["a1"])
        page = Mock()

        @contextmanager
        def creator(*args, **kwargs):
            yield page

        with patch.object(browser, "creator_page", creator), \
             patch.object(browser, "verify_origin"), patch.object(browser, "check_challenge"), \
             patch.object(browser, "observe_xiaohongshu_identity", return_value={"identity_hash": f"{2:064x}"}), \
             patch.object(browser, "fill_post") as fill, patch.object(browser, "prepare_submit") as submit:
            result = run_batch(batch, batch, on_verification=lambda *_: None)["tasks"][0]
        self.assertEqual(result["state"], "failed")
        self.assertIn("账号与预览时不同", result["detail"])
        fill.assert_not_called()
        submit.assert_not_called()

    def test_exception_after_submit_cannot_be_retried_without_reconciliation(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        import browser

        batch = self.batch(["a1"])
        page, click = Mock(), Mock()

        @contextmanager
        def creator(*args, **kwargs):
            yield page

        @contextmanager
        def guard(*args, **kwargs):
            yield {"allowed": 1, "blocked": False}

        with patch.object(browser, "creator_page", creator), \
             patch.object(browser, "verify_origin"), patch.object(browser, "check_challenge"), \
             patch.object(browser, "require_xiaohongshu_identity", return_value=f"{1:064x}"), \
             patch.object(browser, "fill_post"), patch.object(browser, "set_xiaohongshu_options"), \
             patch.object(browser, "prepare_submit", return_value=click), \
             patch.object(browser, "wait_for_upload_evidence"), \
             patch.object(browser, "verify_xiaohongshu_submission", guard), \
             patch.object(browser, "wait_for_submission", side_effect=OSError("connection lost")):
            result = run_batch(batch, batch, on_verification=lambda *_: None)["tasks"][0]
            self.assertEqual(result["state"], "uncertain")
            self.assertIn("核对", result["detail"])
            run_batch(batch, batch, retry=True, on_verification=lambda *_: None)
        click.assert_called_once_with()

    def test_cleanup_error_after_submit_is_uncertain(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        import browser

        batch = self.batch(["a1"])
        page, click = Mock(), Mock()

        @contextmanager
        def creator(*args, **kwargs):
            yield page
            raise OSError("browser cleanup failed")

        @contextmanager
        def guard(*args, **kwargs):
            yield {"allowed": 1, "blocked": False}

        with patch.object(browser, "creator_page", creator), \
             patch.object(browser, "verify_origin"), patch.object(browser, "check_challenge"), \
             patch.object(browser, "require_xiaohongshu_identity", return_value=f"{1:064x}"), \
             patch.object(browser, "fill_post"), patch.object(browser, "set_xiaohongshu_options"), \
             patch.object(browser, "prepare_submit", return_value=click), \
             patch.object(browser, "wait_for_upload_evidence"), \
             patch.object(browser, "verify_xiaohongshu_submission", guard), \
             patch.object(browser, "wait_for_submission"):
            result = run_batch(batch, batch, on_verification=lambda *_: None)["tasks"][0]
        self.assertEqual(result["state"], "uncertain")
        click.assert_called_once_with()

    def test_form_error_before_submit_remains_retryable(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        import browser

        batch = self.batch(["a1"])
        page, click = Mock(), Mock()

        @contextmanager
        def creator(*args, **kwargs):
            yield page

        with patch.object(browser, "creator_page", creator), \
             patch.object(browser, "verify_origin"), patch.object(browser, "check_challenge"), \
             patch.object(browser, "require_xiaohongshu_identity", return_value=f"{1:064x}"), \
             patch.object(browser, "fill_post", side_effect=ValueError("图片未就绪")), \
             patch.object(browser, "prepare_submit", return_value=click):
            result = run_batch(batch, batch, on_verification=lambda *_: None)["tasks"][0]
        self.assertEqual(result["state"], "failed")
        click.assert_not_called()

    def test_proven_intercepted_submission_is_retryable(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        import browser

        batch = self.batch(["a1"])
        page, click = Mock(), Mock()

        @contextmanager
        def creator(*args, **kwargs):
            yield page

        @contextmanager
        def guard(*args, **kwargs):
            yield {"allowed": 0, "blocked": True, "reason": "提交内容与预览不一致"}

        with patch.object(browser, "creator_page", creator), \
             patch.object(browser, "verify_origin"), patch.object(browser, "check_challenge"), \
             patch.object(browser, "require_xiaohongshu_identity", return_value=f"{1:064x}"), \
             patch.object(browser, "fill_post"), patch.object(browser, "set_xiaohongshu_options"), \
             patch.object(browser, "prepare_submit", return_value=click), \
             patch.object(browser, "wait_for_upload_evidence"), \
             patch.object(browser, "verify_xiaohongshu_submission", guard), \
             patch.object(browser, "wait_for_submission", side_effect=PublishUncertain("已拦截")):
            result = run_batch(batch, batch, on_verification=lambda *_: None)["tasks"][0]
        self.assertEqual(result["state"], "failed")
        self.assertIn("已中止", result["detail"])
        click.assert_called_once_with()


class CreatorPages(unittest.TestCase):
    def test_login_and_post_verification_have_bounded_waits(self):
        from unittest.mock import Mock
        import browser

        page = Mock()
        page.is_closed.return_value = False
        page.url = browser.PAGES["xiaohongshu"]
        with self.assertRaisesRegex(VerificationStopped, "超时"):
            browser.wait_for_account_login(page, "xiaohongshu", Mock(), "attached", timeout_seconds=0)
        with patch.object(browser, "entry_controls", return_value=(Mock(), "attached")):
            with self.assertRaisesRegex(VerificationStopped, "取消"):
                browser.wait_for_verification(page, "xiaohongshu", lambda _: None, lambda: True)
        response = Mock(status=403)
        response.json.return_value = {"success": False, "msg": "请完成安全验证"}
        guard = {"allowed": 1, "blocked": False, "response": response}
        with patch.object(browser, "confirmed", return_value=False), \
             patch.object(browser, "check_challenge", return_value=None):
            with self.assertRaisesRegex(VerificationStopped, "超时"):
                browser.wait_for_submission(page, "xiaohongshu", lambda _: None, guard, timeout_seconds=0)

    def test_identity_observation_is_bound_to_creator_origin(self):
        import hashlib
        from unittest.mock import Mock
        from browser import observe_xiaohongshu_identity

        page = Mock()
        observed = observe_xiaohongshu_identity(page)
        capture = page.on.call_args.args[1]
        response = Mock(status=200, url="https://other.example/api/galaxy/user/info")
        response.json.return_value = {"data": {"userId": "test-account"}}
        capture(response)
        self.assertIsNone(observed["identity_hash"])
        response.url = "https://creator.xiaohongshu.com/api/galaxy/user/info"
        capture(response)
        self.assertEqual(observed["identity_hash"], hashlib.sha256(b"test-account").hexdigest())
        response.json.return_value = {"data": {}}
        capture(response)
        self.assertIsNone(observed["identity_hash"])

    def test_login_to_a_different_account_does_not_resume_submission(self):
        from contextlib import contextmanager
        from unittest.mock import Mock
        import browser

        page = Mock()
        page.url = browser.PAGES["xiaohongshu"]
        observed = {"identity_hash": None}
        identities = iter([f"{1:064x}", f"{2:064x}"])
        page.goto.side_effect = lambda *args, **kwargs: observed.update(identity_hash=next(identities))

        @contextmanager
        def creator(*args, **kwargs):
            yield page

        with patch.object(browser, "creator_page", creator), \
             patch.object(browser, "observe_xiaohongshu_identity", return_value=observed), \
             patch.object(browser, "check_challenge", side_effect=[LoginNeeded("登录"), None, None]), \
             patch.object(browser, "wait_for_verification"), \
             patch.object(browser, "fill_post") as fill, patch.object(browser, "prepare_submit") as submit:
            with self.assertRaisesRegex(ValueError, "账号与预览时不同"):
                browser.publish({"platform": "xiaohongshu", "expected_identity": f"{1:064x}"}, {},
                                on_verification=lambda _: None)
        fill.assert_not_called()
        submit.assert_not_called()

    def test_visible_verification_prompts_require_user_action(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge")
            try:
                page = browser.new_page()
                page.set_content('<div>请完成安全验证后继续</div>')
                with self.assertRaises(LoginNeeded):
                    check_challenge(page)
                page.set_content('<iframe src="about:blank#captcha" style="width:100px;height:100px"></iframe>')
                with self.assertRaises(LoginNeeded):
                    check_challenge(page)
                page.set_content('<p>创作者发布页</p>')
                check_challenge(page)
                page.set_content('<div contenteditable="true">请完成安全验证后继续</div>'
                                 '<div class="publish-page-preview">请完成安全验证</div>'
                                 '<div class="chat-item">请完成安全验证</div>'
                                 '<div class="xhs-im-conv-item">短信验证码</div>')
                check_challenge(page)
            finally:
                browser.close()

    def test_platform_confirmation_predicates(self):
        self.assertTrue(confirmed("https://creator.xiaohongshu.com/publish/success?id=1", "xiaohongshu"))
        self.assertFalse(confirmed("https://other.example/publish/success", "xiaohongshu"))
        self.assertFalse(confirmed("https://creator.douyin.com/creator-micro/content/manage", "douyin"))
        self.assertTrue(confirmed("https://creator.douyin.com/creator-micro/content/manage?enter_from=publish", "douyin"))

    def test_submit_timeout_does_not_click_twice(self):
        from playwright.sync_api import TimeoutError
        from unittest.mock import Mock

        page, button = Mock(), Mock()
        page.get_by_text.return_value.all.return_value = []
        page.locator.return_value.all.return_value = []
        page.wait_for_url.side_effect = TimeoutError("page did not redirect")
        with self.assertRaises(PublishUncertain):
            submit_once(page, button, "xiaohongshu")
        button.assert_called_once_with()

    def test_login_challenge_resumes_before_submit(self):
        from contextlib import contextmanager
        from unittest.mock import Mock, call
        import browser

        page = Mock()
        page.url = browser.PAGES["xiaohongshu"]
        page.is_closed.return_value = False

        @contextmanager
        def fake_creator(*args, **kwargs):
            yield page

        @contextmanager
        def fake_guard(*args, **kwargs):
            yield {"allowed": 1, "blocked": False}

        notices = []
        with patch.object(browser, "creator_page", fake_creator), \
             patch.object(browser, "check_challenge", side_effect=[LoginNeeded("登录"), None, None, None]), \
             patch.object(browser, "require_xiaohongshu_identity", return_value=f"{1:064x}"), \
             patch.object(browser, "entry_controls", return_value=(Mock(), "attached")), \
             patch.object(browser, "fill_post") as fill, \
             patch.object(browser, "set_xiaohongshu_options"), \
             patch.object(browser, "prepare_submit", return_value=Mock()), \
             patch.object(browser, "verify_xiaohongshu_submission", fake_guard), \
             patch.object(browser, "submit_once") as submit:
            browser.publish({"platform": "xiaohongshu", "expected_identity": f"{1:064x}"}, {}, on_verification=notices.append)
        self.assertEqual(len(notices), 2)
        self.assertIsNone(notices[-1])
        fill.assert_called_once()
        submit.assert_called_once()

    def test_submission_verification_closed_window_is_uncertain(self):
        from unittest.mock import Mock
        import browser

        page, click = Mock(), Mock()
        page.url = "https://creator.xiaohongshu.com/login"
        page.locator.return_value.all.return_value = []
        page.is_closed.side_effect = [False, False, True]
        with self.assertRaises(PublishUncertain):
            submit_once(page, click, "xiaohongshu", lambda _: None)
        click.assert_called_once_with()

    def test_verification_response_keeps_browser_open_until_user_finishes(self):
        from unittest.mock import Mock
        import browser

        page = Mock()
        page.url = browser.PAGES["xiaohongshu"]
        page.is_closed.return_value = False
        page.locator.return_value.all.return_value = []
        response = Mock(status=403)
        response.json.return_value = {"success": False, "msg": "请完成安全验证"}
        guard = {"allowed": 1, "blocked": False, "response": response}
        notifications = []
        with patch.object(browser, "confirmed", side_effect=[False, False, False, True]), \
             patch.object(browser, "check_challenge", side_effect=[None, LoginNeeded("验证"), None]):
            browser.wait_for_submission(page, "xiaohongshu", notifications.append, guard)
        self.assertIn("验证", notifications[0])
        self.assertIsNone(notifications[-1])

    def test_creator_duplicate_posts_only_reach_local_server_once(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from playwright.sync_api import sync_playwright
        from browser import verify_xiaohongshu_submission

        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'<html></html>')
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
                self.wfile.write(b'{"success":true}')
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        post = {'title':'标题','body':'正文','tags':[],'images':[{'sha256':'a'*64}]}
        payload = {'common':{'title':'标题','desc':'正文','business_binds':'{}',
                             'privacy_info':{'type':0,'op_type':1}},
                   'image_info':{'images':[{'file_id':'file1'}]}}
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='msedge')
                try:
                    page = browser.new_page()
                    page.goto(f'http://127.0.0.1:{server.server_port}/')
                    with verify_xiaohongshu_submission(page, post, {'file1':'a'*64}) as guard:
                        page.evaluate('''payload => Promise.allSettled([1,2].map(() =>
                          fetch('/web_api/sns/v2/note',{method:'POST',headers:{'Content-Type':'application/json'},
                            body:JSON.stringify(payload)})))''', payload)
                        self.assertEqual(guard['allowed'], 1)
                        self.assertEqual(len(received), 1)
                finally:
                    browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_verification_rejected_submission_can_retry_once_after_user_finishes(self):
        from unittest.mock import Mock
        import browser
        page = Mock()
        page.url = browser.PAGES['xiaohongshu']
        page.is_closed.return_value = False
        page.locator.return_value.all.return_value = []
        response = Mock(status=403)
        response.json.return_value = {'success':False, 'msg':'请完成安全验证'}
        guard = {'allowed':1,'blocked':False,'response':response}
        retry = Mock()
        with patch.object(browser,'confirmed',side_effect=[False,False,True]), \
             patch.object(browser,'check_challenge',return_value=None):
            browser.wait_for_submission(page,'xiaohongshu',lambda _:None,guard,
                                        verification_acknowledged=lambda:True,retry_submit=retry)
        retry.assert_called_once_with()
        self.assertTrue(guard['retry_armed'])

    def test_guard_allows_only_one_authorized_verification_retry(self):
        from unittest.mock import Mock
        from browser import verify_xiaohongshu_submission
        page = Mock()
        post = {'title':'标题','body':'正文','tags':[],'images':[{'sha256':'a'*64}]}
        payload = {'common':{'title':'标题','desc':'正文','business_binds':'{}',
                             'privacy_info':{'type':0,'op_type':1}},
                   'image_info':{'images':[{'file_id':'file1'}]}}
        def request():
            route = Mock()
            route.request.method = 'POST'
            route.request.url = 'https://creator.xiaohongshu.com/web_api/sns/v2/note'
            route.request.post_data_json = payload
            return route
        with verify_xiaohongshu_submission(page,post,{'file1':'a'*64}) as guard:
            handler = page.route.call_args.args[1]
            first = request(); handler(first)
            blocked = request(); handler(blocked)
            blocked.abort.assert_called_once()
            guard['retry_armed'] = True
            retry = request(); handler(retry)
            retry.continue_.assert_called_once()
            third = request(); handler(third)
            third.abort.assert_called_once()
            self.assertEqual(guard['allowed'],2)
            self.assertEqual(guard['duplicates_blocked'],2)

    def test_ambiguous_verification_response_never_arms_retry(self):
        from unittest.mock import Mock
        import browser
        page = Mock()
        page.url = browser.PAGES['xiaohongshu']
        page.is_closed.return_value = False
        page.locator.return_value.all.return_value = []
        response = Mock(status=403)
        response.json.return_value = {'msg':'请完成安全验证'}
        guard = {'allowed':1,'blocked':False,'response':response}
        retry = Mock()
        with patch.object(browser,'check_challenge',return_value=None):
            with self.assertRaises(PublishUncertain):
                browser.wait_for_submission(page,'xiaohongshu',lambda _:None,guard,retry_submit=retry)
        retry.assert_not_called()
        self.assertFalse(guard.get('retry_armed',False))

    def test_user_can_resume_when_verification_widget_was_not_detected(self):
        from unittest.mock import Mock
        import browser

        page = Mock()
        page.url = browser.PAGES["xiaohongshu"]
        page.is_closed.return_value = False
        page.locator.return_value.all.return_value = []
        response = Mock(status=403)
        response.json.return_value = {"success": False, "msg": "请完成安全验证"}
        guard = {"allowed": 1, "blocked": False, "response": response}
        notifications = []
        acknowledge = Mock(return_value=True)
        with patch.object(browser, "confirmed", side_effect=[False, False, True]), \
             patch.object(browser, "check_challenge", return_value=None):
            browser.wait_for_submission(page, "xiaohongshu", notifications.append, guard, acknowledge)
        acknowledge.assert_called_once_with()
        self.assertIn("验证", notifications[0])
        self.assertIsNone(notifications[-1])

    def test_uploaded_image_identity_and_order_are_required(self):
        from unittest.mock import Mock
        import hashlib
        from browser import observe_xiaohongshu_uploads, verify_xiaohongshu_submission

        page = Mock()
        uploaded = observe_xiaohongshu_uploads(page)
        record = page.on.call_args.args[1]
        for host, file_id, body, status in [('ros-upload.xiaohongshu.com', 'first', b'one', 200),
                                            ('ros-upload-d4.xhscdn.com', 'second', b'two', 200),
                                            ('ros-upload-d4.xhscdn.com.evil.test', 'evil', b'one', 200),
                                            ('ros-upload.xiaohongshu.com', 'bad', b'bad', 403)]:
            request = Mock(method='PUT', url='https://' + host + '/spectrum/' + file_id,
                           post_data_buffer=body)
            request.response.return_value = Mock(status=status)
            record(request)
        self.assertNotIn('spectrum/bad', uploaded)
        self.assertNotIn('spectrum/evil', uploaded)
        partial = Mock(method='PUT', url='https://ros-upload-d4.xhscdn.com/spectrum/partial?partNumber=1',
                       post_data_buffer=b'one')
        partial.response.return_value = Mock(status=200)
        record(partial)
        self.assertNotIn('spectrum/partial', uploaded)
        first, second = (hashlib.sha256(data).hexdigest() for data in (b'one', b'two'))
        post = {'title': '标题', 'body': '正文', 'tags': [],
                'images': [{'sha256': first}, {'sha256': second}]}
        cases = [(['spectrum/first', 'spectrum/second'], True),
                 (['spectrum/second', 'spectrum/first'], False),
                 (['spectrum/first', 'spectrum/unknown'], False)]
        for ids, allowed in cases:
            route = Mock()
            route.request.method = 'POST'
            route.request.url = 'https://creator.xiaohongshu.com/web_api/sns/v2/note'
            route.request.post_data_json = {'common': {'title': '标题', 'desc': '正文', 'business_binds': '{}', 'privacy_info': {'type': 0, 'op_type': 1}},
                                           'image_info': {'images': [{'file_id': value} for value in ids]}}
            with verify_xiaohongshu_submission(page, post, uploaded) as result:
                page.route.call_args.args[1](route)
                self.assertEqual(bool(result['allowed']), allowed)
                self.assertEqual(result['blocked'], not allowed)

    def test_rejects_nonpublic_or_unknown_visibility(self):
        from unittest.mock import Mock
        from browser import verify_xiaohongshu_submission

        post = {'title': '标题', 'body': '正文', 'tags': [], 'images': [{'sha256': 'a' * 64}]}
        for privacy, allowed in [({'type': 0, 'op_type': 1}, True),
                                 ({'type': 1, 'op_type': 1}, False),
                                 ({'type': False, 'op_type': 1}, False),
                                 ({'type': 0, 'op_type': 1, 'user_ids': ['someone']}, False),
                                 (None, False)]:
            page, route = Mock(), Mock()
            route.request.method = 'POST'
            route.request.url = 'https://edith.xiaohongshu.com/web_api/sns/v2/note'
            route.request.post_data_json = {'common': {'title': '标题', 'desc': '正文', 'business_binds': '{}',
                                                        'privacy_info': privacy},
                                            'image_info': {'images': [{'file_id': 'file1'}]}}
            with verify_xiaohongshu_submission(page, post, {'file1': 'a' * 64}) as result:
                page.route.call_args.args[1](route)
                self.assertEqual(bool(result['allowed']), allowed)
                self.assertEqual(result['blocked'], not allowed)

    def test_rejects_unsynchronized_platform_title(self):
        from unittest.mock import Mock
        from browser import verify_xiaohongshu_submission

        page = Mock()
        post = {"title": "标题", "body": "正文", "tags": [], "images": [{"sha256": "a" * 64}]}
        with verify_xiaohongshu_submission(page, post, {"file1": "a" * 64}) as result:
            handler = page.route.call_args.args[1]
            route = Mock()
            route.request.method = "POST"
            route.request.url = "https://creator.xiaohongshu.com/web_api/sns/v2/note"
            route.request.post_data_json = {"common": {"title": "", "desc": "正文", "business_binds": "{}", "privacy_info": {"type": 0, "op_type": 1}},
                                            "image_info": {"images": [{"file_id": "file1"}]}}
            handler(route)
            route.abort.assert_called_once()
            self.assertTrue(result["blocked"])
            self.assertEqual(result["allowed"], 0)
            route = Mock()
            route.request.method = "POST"
            route.request.url = "https://creator.xiaohongshu.com/web_api/sns/v2/note"
            route.request.post_data_json = {"common": {"title": "标题", "desc": "正文", "business_binds": "{}", "privacy_info": {"type": 0, "op_type": 1}},
                                            "image_info": {"images": [{"file_id": "file1"}]}}
            handler(route)
            route.continue_.assert_called_once()
            self.assertEqual(result["allowed"], 1)

    def test_rejects_mismatched_declaration(self):
        from unittest.mock import Mock
        from browser import verify_xiaohongshu_submission

        page = Mock()
        post = {"title": "标题", "body": "正文", "tags": [], "images": [{"sha256": "a" * 64}], "declaration": "ai"}
        with verify_xiaohongshu_submission(page, post, {"file1": "a" * 64}) as result:
            handler = page.route.call_args.args[1]
            route = Mock()
            route.request.method = "POST"
            route.request.url = "https://creator.xiaohongshu.com/web_api/sns/v2/note"
            route.request.post_data_json = {"common": {"title": "标题", "desc": "正文", "business_binds": "{}", "privacy_info": {"type": 0, "op_type": 1}},
                                            "image_info": {"images": [{"file_id": "file1"}]}}
            handler(route)
            route.abort.assert_called_once()
            self.assertTrue(result["blocked"])

    def test_failed_abort_is_not_treated_as_proven_blocked(self):
        from unittest.mock import Mock
        from browser import verify_xiaohongshu_submission

        page = Mock()
        post = {"title": "标题", "body": "正文", "tags": [], "images": [{"sha256": "a" * 64}]}
        with verify_xiaohongshu_submission(page, post, {"file1": "a" * 64}) as result:
            handler = page.route.call_args.args[1]
            route = Mock()
            route.request.method = "POST"
            route.request.url = "https://creator.xiaohongshu.com/web_api/sns/v2/note"
            route.request.post_data_json = {"common": {"title": "不同", "desc": "正文", "business_binds": "{}", "privacy_info": {"type": 0, "op_type": 1}},
                                            "image_info": {"images": [{"file_id": "file1"}]}}
            route.abort.side_effect = OSError("abort failed")
            with self.assertRaises(OSError):
                handler(route)
            self.assertFalse(result["blocked"])

    def test_rejects_missing_tag(self):
        from unittest.mock import Mock
        from browser import verify_xiaohongshu_submission

        page = Mock()
        post = {"title": "标题", "body": "正文", "tags": ["话题"], "images": [{"sha256": "a" * 64}]}
        with verify_xiaohongshu_submission(page, post, {"file1": "a" * 64}) as result:
            handler = page.route.call_args.args[1]
            route = Mock()
            route.request.method = "POST"
            route.request.url = "https://creator.xiaohongshu.com/web_api/sns/v2/note"
            route.request.post_data_json = {"common": {"title": "标题", "desc": "正文", "business_binds": "{}", "privacy_info": {"type": 0, "op_type": 1}},
                                            "image_info": {"images": [{"file_id": "file1"}]}}
            handler(route)
            route.abort.assert_called_once()
            self.assertTrue(result["blocked"])

    def test_topic_text_must_match_the_confirmed_preview(self):
        from unittest.mock import Mock
        from browser import verify_xiaohongshu_submission

        post = {"title": "标题", "body": "正文", "tags": ["周末记录", "生活灵感"], "images": [{"sha256": "a" * 64}]}
        examples = [
            ("正文 #周末记录 #生活灵感", True),
            ("正文 #周末记录[话题]#\u00a0 #生活灵感[话题]#\u00a0", True),
            ("正文 #周末记录加长 #生活灵感", False),
            ("正文 #周末记录 #生活灵感 多余文字", False),
            ("正文 #生活灵感 #周末记录", False),
            ("正文 #周末记录 #生活灵感 #未确认", False),
        ]
        for description, allowed in examples:
            with self.subTest(description=description):
                page, route = Mock(), Mock()
                route.request.method = "POST"
                route.request.url = "https://creator.xiaohongshu.com/web_api/sns/v2/note"
                route.request.post_data_json = {
                    "common": {"title": "标题", "desc": description, "business_binds": "{}", "privacy_info": {"type": 0, "op_type": 1}},
                    "image_info": {"images": [{"file_id": "file1"}]},
                }
                with verify_xiaohongshu_submission(page, post, {"file1": "a" * 64}) as result:
                    page.route.call_args.args[1](route)
                    self.assertEqual(bool(result["allowed"]), allowed)
                    self.assertEqual(result["blocked"], not allowed)

    def test_rejects_mismatched_schedule_and_original(self):
        from unittest.mock import Mock
        from browser import verify_xiaohongshu_submission

        date = (datetime.now(BEIJING) + timedelta(hours=3)).replace(second=0, microsecond=0)
        post = {"title": "标题", "body": "正文", "tags": [], "images": [{"sha256": "a" * 64}],
                "schedule": date.strftime("%Y-%m-%dT%H:%M"), "original": True}
        bindings = {"notePostTiming": {"postTime": int(date.timestamp() * 1000)},
                    "optionRelationList": [{"type": "ORIGINAL_STATEMENT"}]}
        page = Mock()
        with verify_xiaohongshu_submission(page, post, {"file1": "a" * 64}) as result:
            handler = page.route.call_args.args[1]
            route = Mock()
            route.request.method = "POST"
            route.request.url = "https://creator.xiaohongshu.com/web_api/sns/v2/note"
            route.request.post_data_json = {"common": {"title": "标题", "desc": "正文", "business_binds": json.dumps(bindings), "privacy_info": {"type": 0, "op_type": 1}},
                                            "image_info": {"images": [{"file_id": "file1"}]}}
            handler(route)
            route.continue_.assert_called_once()
            bindings["notePostTiming"]["postTime"] += 60000
            route = Mock()
            route.request.method = "POST"
            route.request.url = "https://creator.xiaohongshu.com/web_api/sns/v2/note"
            route.request.post_data_json = {"common": {"title": "标题", "desc": "正文", "business_binds": json.dumps(bindings), "privacy_info": {"type": 0, "op_type": 1}},
                                            "image_info": {"images": [{"file_id": "file1"}]}}
            handler(route)
            route.abort.assert_called_once()
            self.assertTrue(result["blocked"])


class EntryPointCompatibility(unittest.TestCase):
    def test_cli_interactive_verification_uses_stderr(self):
        import cli

        def fake_run(batch, confirmation, retry=False, on_verification=None):
            self.assertEqual((batch, confirmation), ("batch", "batch"))
            on_verification("xhs01", "请完成验证")
            return {"batch": batch}

        out, err = io.StringIO(), io.StringIO()
        with patch.object(cli, "run_batch", side_effect=fake_run), \
             patch("sys.argv", ["cli.py", "publish", "batch", "--confirm", "batch", "--interactive"]), \
             patch("sys.stdout", out), patch("sys.stderr", err):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(out.getvalue())["batch"], "batch")
        self.assertIn("请完成验证", err.getvalue())

    def test_mcp_interactive_verification_does_not_write_stdout(self):
        import asyncio
        import agent_server

        def fake_run(batch, confirmation, retry=False, on_verification=None):
            on_verification("xhs01", "请完成验证")
            return {"batch": batch}

        out, err = io.StringIO(), io.StringIO()
        with patch.object(agent_server, "run_batch", side_effect=fake_run), \
             patch("sys.stdout", out), patch("sys.stderr", err):
            result = asyncio.run(agent_server.publish_batch("batch", "batch", interactive=True))
            self.assertEqual(result["batch"], "batch")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("请完成验证", err.getvalue())


class XiaohongshuRules(unittest.TestCase):
    def test_actual_browser_upload_event_contains_exact_bytes(self):
        import hashlib
        from browser import observe_xiaohongshu_uploads
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page()
                page.route('https://creator.xiaohongshu.com/**',
                           lambda route: route.fulfill(content_type='text/html', body='<html></html>'))
                page.route('https://ros-upload.xiaohongshu.com/**',
                           lambda route: route.fulfill(status=200, headers={
                               'Access-Control-Allow-Origin': 'https://creator.xiaohongshu.com',
                               'Access-Control-Allow-Methods': 'PUT, OPTIONS',
                               'Access-Control-Allow-Headers': 'content-type'}, body='ok'))
                page.route('https://ros-upload-d4.xhscdn.com/**',
                           lambda route: route.fulfill(status=200, headers={
                               'Access-Control-Allow-Origin': 'https://creator.xiaohongshu.com',
                               'Access-Control-Allow-Methods': 'PUT, OPTIONS',
                               'Access-Control-Allow-Headers': 'content-type'}, body='ok'))
                page.goto('https://creator.xiaohongshu.com/test')
                uploaded = observe_xiaohongshu_uploads(page)
                page.evaluate("async () => { await fetch('https://ros-upload.xiaohongshu.com/spectrum/fixture', "
                              "{method:'PUT', body:new Uint8Array([1,2,3,4])}); }")
                page.evaluate("async () => { await fetch('https://ros-upload-d4.xhscdn.com/spectrum/second', "
                              "{method:'PUT', body:new Uint8Array([4,3,2,1])}); }")
                page.evaluate("async () => { await fetch('https://ros-upload-d4.xhscdn.com/spectrum/partial?partNumber=1', "
                              "{method:'PUT', body:new Uint8Array([1,2,3,4])}); }")
                page.wait_for_timeout(250)
                self.assertEqual(uploaded.get('spectrum/fixture'), hashlib.sha256(bytes([1, 2, 3, 4])).hexdigest())
                self.assertEqual(uploaded.get('spectrum/second'), hashlib.sha256(bytes([4, 3, 2, 1])).hexdigest())
                self.assertNotIn('spectrum/partial', uploaded)
            finally:
                browser.close()

    def test_image_format_and_size_validation(self):
        contents = BytesIO()
        Image.new("RGB", (4, 3), "red").save(contents, format="WEBP")
        contents.seek(0)
        self.assertEqual(image_info(contents, "example.webp"), {"width": 4, "height": 3})
        contents.seek(0)
        with self.assertRaises(ValueError):
            image_info(contents, "example.jpg")
        with patch("xiaohongshu.BYTE_LIMIT", 1):
            contents.seek(0)
            with self.assertRaises(ValueError):
                image_info(contents, "example.webp")

    def test_schedule_and_declaration_constraints(self):
        now = datetime(2026, 11, 14, 12, tzinfo=BEIJING)
        self.assertEqual(schedule_time("2026-11-14T13:00", now), now + timedelta(hours=1))
        for value in ("2026-11-14T12:59", "2026-11-28T12:01", "bad"):
            with self.assertRaises(ValueError):
                schedule_time(value, now)
        with self.assertRaises(ValueError):
            post_options({"original": True, "declaration": "repost", "source_name": "来源"})
        with self.assertRaises(ValueError):
            post_options({"declaration": "repost"})
        with self.assertRaises(ValueError):
            post_options({"declaration": "self_shot", "shoot_date": "2020-01-01"})
        result = post_options({"declaration": "self_shot", "shoot_place": "北京站",
                               "shoot_date": "2020-01-01"})
        self.assertEqual(result["shoot_place"], "北京站")

    def test_two_platform_form_paths_using_local_fixtures(self):
        from playwright.sync_api import sync_playwright

        xhs = "https://creator.xiaohongshu.com/publish/publish?from=homepage&target=image"
        dy = "https://creator.douyin.com/creator-micro/content/upload"
        xhs_html = """
            <input type=file accept="image/*"><input placeholder="填写标题">
            <div contenteditable="true" style="min-height: 24px"><p data-placeholder="输入正文描述"><br></p></div>
            <button aria-label="关闭新功能引导" onclick="this.remove()">×</button>
            <div class="publish-video" onclick="location.href='/wrong'">发布笔记</div>
            <div class="publish-page-content"><xhs-publish-btn></xhs-publish-btn></div>
            <script>
                const footer = document.querySelector('xhs-publish-btn');
                const shadow = footer.attachShadow({mode: 'closed'});
                shadow.innerHTML = '<button>暂存离开</button><button>发布</button>';
                shadow.lastElementChild.onclick = () => location.href = '/publish/success';
            </script>
        """
        dy_html = """
            <button>发布图文</button><div class="container">
            <input type=file accept="image/*" onchange="setTimeout(() => location.href='/creator-micro/content/post/image?a=1', 100)">
            </div>
        """
        dy_edit = """
            <input placeholder="填写作品标题"><div class="zone-container" contenteditable="true" style="min-height: 24px"></div>
            <button onclick="location.href='/creator-micro/content/manage?enter_from=publish'">发布</button>
        """
        with tempfile.TemporaryDirectory() as folder, sync_playwright() as pw:
            path = Path(folder) / "test.png"
            path.write_bytes(bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
                "0000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082"
            ))
            browser = pw.chromium.launch(channel="msedge")
            try:
                page = browser.new_page()
                page.set_default_timeout(5000)
                # Fulfill all URLs locally, including any missing route.
                page.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body={
                    xhs: xhs_html,
                    dy: dy_html,
                    "https://creator.douyin.com/creator-micro/content/post/image?a=1": dy_edit,
                }.get(route.request.url, "<p>fixture destination</p>")))
                post = {"title": "样例标题", "body": "第一段\n第二段", "tags": [], "images": [{"path": str(path)}]}
                for platform, url in (("xiaohongshu", xhs), ("douyin", dy)):
                    page.goto(url)
                    fill_post(page, platform, post)
                    self.assertEqual(page.locator("input[placeholder]").input_value(), post["title"])
                    self.assertIn("第二段", page.locator('[contenteditable="true"]').inner_text())
                    submit_once(page, prepare_submit(page, platform), platform)
                    self.assertTrue(confirmed(page.url, platform))
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
