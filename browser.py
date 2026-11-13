import json
from contextlib import contextmanager
from functools import partial
from time import monotonic
from urllib.parse import parse_qs, urlparse

from publisher import browser_lock, data_root, get_account


# Creator-page selectors were researched against the MIT-licensed social-auto-upload
# project. See README for the reference revision.
PAGES = {
    "xiaohongshu": "https://creator.xiaohongshu.com/publish/publish?from=homepage&target=image",
    "douyin": "https://creator.douyin.com/creator-micro/content/upload",
}


class LoginNeeded(Exception):
    pass


class PublishUncertain(Exception):
    pass


@contextmanager
def creator_page(account, headed=False):
    from playwright.sync_api import Error, sync_playwright

    profile = data_root() / "profiles" / f"{account['platform']}-{account['alias']}"
    with sync_playwright() as pw:
        try:
            context = pw.chromium.launch_persistent_context(str(profile), channel="msedge", headless=not headed)
        except Error as exc:
            raise ValueError("无法启动独立浏览器会话，请检查 Microsoft Edge 是否可用。") from exc
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(15000)
            yield page
        finally:
            try:
                context.close()
            except Error:
                pass


def entry_controls(page, platform):
    if platform == "xiaohongshu":
        return page.locator('input[type="file"][accept*="image"], input.upload-input').first, "attached"
    return page.get_by_text("发布图文", exact=True).first, "visible"


def verify_origin(page, platform):
    actual, expected = urlparse(page.url), urlparse(PAGES[platform])
    if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
        raise LoginNeeded("创作者页面已跳转，请先在对应账号的独立窗口登录。")
    if "/login" in actual.path:
        raise LoginNeeded("登录已过期，请先重新登录。")


def check_challenge(page, post_attempt=False):
    for prompt in ("手机号登录", "扫码登录", "短信验证码", "安全验证", "滑块验证"):
        if page.get_by_text(prompt, exact=True).first.is_visible():
            if post_attempt:
                raise PublishUncertain("提交后页面要求人工验证。请先到平台核对作品状态。")
            raise LoginNeeded("平台要求登录或人工验证，请在该账号的登录窗口处理。")


def account_session(alias, login=False):
    from playwright.sync_api import Error

    account = get_account(alias)
    with browser_lock(), creator_page(account, headed=login) as page:
        try:
            page.goto(PAGES[account["platform"]], wait_until="domcontentloaded", timeout=30000)
            control, state = entry_controls(page, account["platform"])
            if login:
                print(f"请在打开的浏览器里登录 {alias}，然后进入图文发布页。", flush=True)
            else:
                verify_origin(page, account["platform"])
                check_challenge(page)
            control.wait_for(state=state, timeout=240000 if login else 12000)
            verify_origin(page, account["platform"])
            check_challenge(page)
        except (Error, LoginNeeded):
            return {"account": alias, "state": "unverified", "detail": "未能确认已登录的创作者图文页。"}
    return {"account": alias, "state": "ready"}


def fill_post(page, platform, post):
    from playwright.sync_api import Error

    control, state = entry_controls(page, platform)
    control.wait_for(state=state, timeout=15000)
    if platform == "douyin":
        control.click()
        control = page.locator('div[class^="container"] input[type="file"][accept*="image"]').first
    control.set_input_files([image["path"] for image in post["images"]], timeout=30000)
    if platform == "douyin":
        page.wait_for_url("**/creator-micro/content/post/image?**", timeout=90000)
        title = page.get_by_placeholder("填写作品标题", exact=False).first
        editor = page.locator('div.zone-container[contenteditable="true"]').first
    else:
        title = page.get_by_placeholder("填写标题", exact=False).first
        editor = page.locator('[contenteditable="true"]').filter(
            has=page.locator('p[data-placeholder*="输入正文描述"]')
        ).first
    verify_origin(page, platform)
    check_challenge(page)
    title.fill(post["title"], timeout=90000)
    editor.fill(post["body"])
    for tag in post["tags"]:
        page.keyboard.press("End")
        page.keyboard.type(" #" + tag)
        if platform == "xiaohongshu":
            topic = page.locator("#creator-editor-topic-container .item").get_by_text(tag, exact=True).first
            try:
                topic.wait_for(state="visible", timeout=2500)
                topic.click()
                continue
            except Error:
                pass
        page.keyboard.press("Space")
        page.keyboard.press("Escape")


def confirmed(url, platform):
    actual, expected = urlparse(url), urlparse(PAGES[platform])
    if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
        return False
    if platform == "xiaohongshu":
        return actual.path.rstrip("/") == "/publish/success"
    return (actual.path.rstrip("/") == "/creator-micro/content/manage"
            and "publish" in parse_qs(actual.query).get("enter_from", []))


def xiaohongshu_submit_point(page, host_selector):
    from playwright.sync_api import Error

    # The current creator footer exposes its controls through accessibility,
    # while its closed shadow root keeps them out of Playwright locators.
    host = page.locator(host_selector)
    host.scroll_into_view_if_needed(timeout=10000)
    cdp = page.context.new_cdp_session(page)
    try:
        remote = cdp.send("Runtime.evaluate", {
            "expression": f"document.querySelector({json.dumps(host_selector)})",
        })["result"]
        deadline = monotonic() + 12
        while monotonic() < deadline:
            host_node = cdp.send("DOM.describeNode", {
                "objectId": remote["objectId"], "depth": -1, "pierce": True,
            })["node"]
            descendants = host_node.get("shadowRoots", [])[:]
            button_ids = set()
            while descendants:
                node = descendants.pop()
                if node.get("localName") == "button":
                    button_ids.add(node["backendNodeId"])
                descendants.extend(node.get("children", []))
            nodes = cdp.send("Accessibility.getFullAXTree")["nodes"]
            buttons = [node for node in nodes if node.get("backendDOMNodeId") in button_ids
                       and not node.get("ignored") and node.get("name", {}).get("value") == "发布"]
            if len(buttons) == 1:
                button = buttons[0]
                disabled = any(p["name"] in {"disabled", "busy"} and p["value"]["value"]
                               for p in button.get("properties", []))
                if not disabled:
                    target = button["backendDOMNodeId"]
                    quads = cdp.send("DOM.getContentQuads", {"backendNodeId": target})["quads"]
                    if len(quads) == 1:
                        quad = quads[0]
                        x, y = round(sum(quad[::2]) / 4), round(sum(quad[1::2]) / 4)
                        hit = cdp.send("DOM.getNodeForLocation", {
                            "x": x, "y": y, "includeUserAgentShadowDOM": True,
                        })
                        if hit["backendNodeId"] == target:
                            return x, y
            page.wait_for_timeout(250)
    except Error as exc:
        raise ValueError("无法定位小红书表单底部的发布按钮。") from exc
    finally:
        cdp.detach()
    raise ValueError("小红书表单底部的发布按钮未就绪。")


def prepare_submit(page, platform):
    if platform == "xiaohongshu":
        close_guide = page.get_by_role("button", name="关闭新功能引导", exact=True)
        if close_guide.is_visible():
            close_guide.click(timeout=5000)
        footer = ".publish-page-content > xhs-publish-btn"
        if page.locator(footer).count() == 1:
            x, y = xiaohongshu_submit_point(page, footer)
            return partial(page.mouse.click, x, y)
    button = page.get_by_role("button", name="发布", exact=True)
    button.click(trial=True, timeout=15000)
    return partial(button.click, timeout=10000)


def submit_once(page, click, platform):
    from playwright.sync_api import Error

    try:
        click()
        page.wait_for_url(lambda url: confirmed(url, platform), timeout=45000)
    except Error as exc:
        try:
            check_challenge(page, post_attempt=True)
        except Error:
            pass
        raise PublishUncertain("已尝试提交，未确认结果。请核对平台作品记录后再处理。") from exc


def publish(account, post):
    from playwright.sync_api import Error

    platform = account["platform"]
    with creator_page(account) as page:
        try:
            page.goto(PAGES[platform], wait_until="domcontentloaded", timeout=30000)
            verify_origin(page, platform)
            check_challenge(page)
            fill_post(page, platform, post)
            click = prepare_submit(page, platform)
            verify_origin(page, platform)
            check_challenge(page)
        except Error as exc:
            verify_origin(page, platform)
            check_challenge(page)
            raise ValueError("发布页或素材未准备完成，请检查对应创作者页面。") from exc
        submit_once(page, click, platform)
