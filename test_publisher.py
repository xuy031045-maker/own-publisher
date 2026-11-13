import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from browser import LoginNeeded, PublishUncertain, confirmed, fill_post, prepare_submit, submit_once
from publisher import add_account, batch_status, prepare_batch, run_batch, set_state


class Batches(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"OWN_PUBLISHER_HOME": self.temp.name})
        env.start()
        self.addCleanup(env.stop)
        (self.root / "sample.jpg").write_bytes(b"test image bytes")
        for index in range(1, 12):
            add_account(f"a{index}", "xiaohongshu" if index < 6 else "douyin")

    def batch(self, aliases):
        manifest = self.root / "post.json"
        manifest.write_text(json.dumps({
            "post": {"title": "测试", "images": ["sample.jpg"]},
            "targets": aliases,
        }), encoding="utf-8")
        return prepare_batch(manifest)["batch"]

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


class CreatorPages(unittest.TestCase):
    def test_platform_confirmation_predicates(self):
        self.assertTrue(confirmed("https://creator.xiaohongshu.com/publish/success?id=1", "xiaohongshu"))
        self.assertFalse(confirmed("https://other.example/publish/success", "xiaohongshu"))
        self.assertFalse(confirmed("https://creator.douyin.com/creator-micro/content/manage", "douyin"))
        self.assertTrue(confirmed("https://creator.douyin.com/creator-micro/content/manage?enter_from=publish", "douyin"))

    def test_submit_timeout_does_not_click_twice(self):
        from playwright.sync_api import TimeoutError
        from unittest.mock import Mock

        page, button = Mock(), Mock()
        page.get_by_text.return_value.first.is_visible.return_value = False
        page.wait_for_url.side_effect = TimeoutError("page did not redirect")
        with self.assertRaises(PublishUncertain):
            submit_once(page, button, "xiaohongshu")
        button.assert_called_once_with()

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
