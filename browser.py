import hashlib
import json
import re
from contextlib import contextmanager
from functools import partial
from time import monotonic
from urllib.parse import parse_qs, urlparse

from publisher import browser_lock, data_root, get_account, save_account_identity
from xiaohongshu import DECLARATIONS, schedule_time


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


class VerificationStopped(Exception):
    pass


class VerificationUncertain(PublishUncertain):
    pass


VERIFICATION_TIMEOUT_SECONDS = 600


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
    prompts = ("手机号登录", "扫码登录", "短信验证码", "安全验证", "滑块验证",
               "请完成验证", "请完成安全验证", "请滑动滑块", "拖动滑块")
    visible = False
    for prompt in prompts:
        for match in page.get_by_text(prompt, exact=False).all():
            if match.is_visible() and not match.evaluate(
                "el => !!el.closest('[contenteditable=true], .publish-page-preview, .chat-item, .xhs-im-conv-item')"
            ):
                visible = True
                break
        if visible:
            break
    if not visible:
        frames = page.locator('iframe[src*="captcha" i], iframe[src*="verify" i]')
        visible = any(frame.is_visible() for frame in frames.all())
    if visible:
        if post_attempt:
            raise PublishUncertain("提交后页面要求人工验证。请先到平台核对作品状态。")
        raise LoginNeeded("平台要求登录或人工验证，请在该账号的登录窗口处理。")


def observe_xiaohongshu_identity(page):
    observed = {"identity_hash": None}

    def capture(response):
        url = urlparse(response.url)
        if (url.scheme, url.netloc, url.path) != (
            "https", "creator.xiaohongshu.com", "/api/galaxy/user/info"
        ):
            return
        try:
            data = response.json()
            user_id = data.get("data", {}).get("userId") if response.status == 200 else None
            observed["identity_hash"] = (hashlib.sha256(user_id.encode("utf-8")).hexdigest()
                                         if isinstance(user_id, str) and user_id else None)
        except Exception:
            observed["identity_hash"] = None

    page.on("response", capture)
    return observed


def require_xiaohongshu_identity(page, observed, expected=None):
    deadline = monotonic() + 8
    while not observed["identity_hash"] and monotonic() < deadline:
        if page.is_closed():
            raise LoginNeeded("浏览器窗口已关闭，请重新检查账号。")
        page.wait_for_timeout(200)
    actual = observed["identity_hash"]
    if not actual:
        raise ValueError("无法核验小红书账号身份，请检查登录状态后重试。")
    if expected and actual != expected:
        raise ValueError("当前登录的小红书账号与预览时不同，请检查账号并重新生成预览。")
    return actual


def account_session(alias, login=False, cancel_requested=None, on_queue=None):
    from playwright.sync_api import Error
    from browser_queue import queued_browser

    account = get_account(alias)
    with queued_browser('login' if login else 'check', cancel_requested=cancel_requested, on_state=on_queue), creator_page(account, headed=login) as page:
        observed = observe_xiaohongshu_identity(page) if account["platform"] == "xiaohongshu" else None
        try:
            page.goto(PAGES[account["platform"]], wait_until="domcontentloaded", timeout=30000)
            control, state = entry_controls(page, account["platform"])
            if login:
                print(f"请在打开的浏览器里登录 {alias}，然后进入图文发布页。", flush=True)
            else:
                verify_origin(page, account["platform"])
                check_challenge(page)
            if login and account["platform"] == "xiaohongshu":
                wait_for_account_login(page, account["platform"], control, state, cancel_requested)
            else:
                control.wait_for(state=state, timeout=0 if login else 12000)
            verify_origin(page, account["platform"])
            check_challenge(page)
            nickname = ""
            if account["platform"] == "xiaohongshu":
                name = page.locator(".user-info .name-box").first
                name.wait_for(state="visible", timeout=5000)
                nickname = name.inner_text().strip()
                if not nickname:
                    raise ValueError("无法读取当前账号昵称，请重新检查账号。")
                identity = require_xiaohongshu_identity(page, observed)
                save_account_identity(alias, nickname, identity)
        except (Error, LoginNeeded):
            return {"account": alias, "state": "unverified", "detail": "未能确认已登录的创作者图文页。"}
        except VerificationStopped as exc:
            return {"account": alias, "state": "cancelled", "detail": str(exc)}
        except ValueError as exc:
            return {"account": alias, "state": "unverified", "detail": str(exc)}
    result = {"account": alias, "state": "ready"}
    if nickname:
        result["nickname"] = nickname
    return result


def wait_for_account_login(page, platform, control, state, cancel_requested=None, timeout_seconds=None):
    from playwright.sync_api import Error

    timeout = VERIFICATION_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    deadline = monotonic() + timeout
    while not page.is_closed():
        check_verification_stop(deadline, cancel_requested)
        try:
            verify_origin(page, platform)
            check_challenge(page)
            control.wait_for(state=state, timeout=500)
        except (Error, LoginNeeded):
            if page.is_closed():
                break
        else:
            return
        page.wait_for_timeout(350)
    raise VerificationStopped("登录窗口已关闭，可重新登录。")


def check_verification_stop(deadline, cancel_requested=None):
    if cancel_requested and cancel_requested():
        raise VerificationStopped("已取消等待验证。")
    if deadline is not None and monotonic() >= deadline:
        raise VerificationStopped("等待验证超时，请重新处理。")


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


def xiaohongshu_submit_point(page, host_selector, label="发布"):
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
                       and not node.get("ignored") and node.get("name", {}).get("value") == label]
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


def prepare_submit(page, platform, scheduled=False):
    label = "定时发布" if scheduled else "发布"
    if platform == "xiaohongshu":
        close_guide = page.get_by_role("button", name="关闭新功能引导", exact=True)
        if close_guide.is_visible():
            close_guide.click(timeout=5000)
        footer = ".publish-page-content > xhs-publish-btn"
        if page.locator(footer).count() == 1:
            x, y = xiaohongshu_submit_point(page, footer, label)
            return partial(page.mouse.click, x, y)
    button = page.get_by_role("button", name=label, exact=True)
    button.click(trial=True, timeout=15000)
    return partial(button.click, timeout=10000)


def submit_once(page, click, platform, on_verification=None, guard=None, verification_acknowledged=None,
                verification_cancelled=None, retry_submit=None):
    from playwright.sync_api import Error

    try:
        click()
        if on_verification or guard is not None:
            wait_for_submission(page, platform, on_verification, guard, verification_acknowledged,
                                verification_cancelled, retry_submit=retry_submit)
        else:
            page.wait_for_url(lambda url: confirmed(url, platform), timeout=45000)
    except Error as exc:
        try:
            check_challenge(page, post_attempt=True)
        except Error:
            pass
        raise PublishUncertain("已尝试提交，未确认结果。请核对平台作品记录后再处理。") from exc


def matches_xiaohongshu_description(actual, body, tags):
    if not isinstance(actual, str) or not actual.startswith(body):
        return False
    suffix = actual[len(body):]
    if not tags:
        return not suffix
    topics = "".join(r"\s*#" + re.escape(tag) + r"(?:\[话题\]#)?" for tag in tags)
    return re.fullmatch(topics + r"\s*", suffix) is not None


def observe_xiaohongshu_uploads(page, images=None):
    """Bind complete successful uploads to preview images; retain no image bytes."""
    from upload_evidence import UploadEvidence

    uploaded = UploadEvidence(images)
    uploaded.install(page)
    page.on('requestfinished', uploaded.finished)
    return uploaded


def wait_for_upload_evidence(page, images, uploaded, timeout_seconds=60):
    required = {image['sha256'] for image in images}
    deadline = monotonic() + timeout_seconds
    while not required.issubset(set(uploaded.values())):
        if page.is_closed():
            raise ValueError('浏览器窗口已关闭，无法核对上传图片。')
        verify_origin(page, 'xiaohongshu')
        check_challenge(page)
        if monotonic() >= deadline:
            raise ValueError('无法核实平台上传的全部图片，请检查素材或稍后重试。')
        page.wait_for_timeout(250)


def images_match_uploads(actual, expected, uploaded):
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    return all(isinstance(image, dict) and isinstance(image.get('file_id'), str)
               and isinstance(wanted, dict) and uploaded.get(image['file_id']) == wanted.get('sha256')
               and isinstance(wanted.get('sha256'), str) and len(wanted['sha256']) == 64
               for image, wanted in zip(actual, expected))


def is_verification_rejection(data):
    if not isinstance(data, dict) or data.get('success') is not False:
        return False
    reason = str(data.get('msg') or data.get('message') or '').lower()
    return any(word in reason for word in ('安全验证', '人机验证', '验证码', '滑块', '登录', 'captcha'))


@contextmanager
def verify_xiaohongshu_submission(page, post, uploaded=None):
    """Reject a creator request if its contents differ from the preview."""
    result = {"allowed": 0, "blocked": False, "reason": "", "response": None,
              "duplicates_blocked": 0, "verification_rejected": False, "retry_armed": False}
    pattern = "**/web_api/sns/v2/note*"

    def inspect(route):
        request = route.request
        if request.method != "POST" or urlparse(request.url).path != "/web_api/sns/v2/note":
            route.continue_()
            return
        try:
            payload = request.post_data_json
            content = payload["common"]
            title, body = content.get("title"), content.get("desc")
            valid = title == post["title"] and matches_xiaohongshu_description(body, post["body"], post["tags"])
            privacy = content.get('privacy_info')
            valid = (valid and isinstance(privacy, dict) and type(privacy.get('type')) is int
                     and privacy['type'] == 0 and privacy.get('op_type') == 1
                     and privacy.get('user_ids', []) == [])
            binds = json.loads(content["business_binds"])
            declaration = post.get("declaration")
            expected_origins = {"fiction": 1, "ai": 2, "advertising": 3, "self_shot": 4, "repost": 5}
            declared = binds.get("userDeclarationBind") or {}
            valid = valid and declared.get("origin") == expected_origins.get(declaration)
            if declaration == "repost":
                valid = valid and declared.get("repostInfo", {}).get("source") == post["source_name"]
            if declaration == "self_shot":
                photo = declared.get("photoInfo", {})
                place = photo.get("photoPlace", {})
                valid = valid and place.get("name") == post["shoot_place"] and photo.get("photoTime") == post["shoot_date"]
                if post.get("shoot_address"):
                    valid = valid and place.get("subname") == post["shoot_address"]
            original = any(item.get("type") == "ORIGINAL_STATEMENT" for item in binds.get("optionRelationList", []))
            valid = valid and original == bool(post.get("original"))
            actual_time = (binds.get("notePostTiming") or {}).get("postTime")
            expected_time = int(schedule_time(post["schedule"]).timestamp() * 1000) if post.get("schedule") else None
            valid = valid and actual_time == expected_time
            valid = valid and images_match_uploads(payload["image_info"]["images"], post["images"], uploaded or {})
        except (TypeError, ValueError, KeyError, AttributeError, json.JSONDecodeError):
            valid = False
        if not valid:
            result["reason"] = "无法确认平台待提交的内容、图片及顺序、公开范围、声明或时间与预览一致"
            route.abort()
            result["blocked"] = True
            return
        if result['allowed'] and not (result['allowed'] == 1 and result['retry_armed']):
            route.abort()
            result['duplicates_blocked'] += 1
            return
        if result['allowed']:
            result['retry_armed'] = False
        result["allowed"] += 1
        route.continue_()

    page.route(pattern, inspect)
    def capture(response):
        if urlparse(response.url).path == "/web_api/sns/v2/note" and response.request.method == "POST":
            result["response"] = response
            if result['allowed'] == 1:
                try:
                    result['verification_rejected'] = is_verification_rejection(response.json())
                except Exception:
                    result['verification_rejected'] = False
    page.on("response", capture)
    try:
        yield result
    finally:
        page.remove_listener("response", capture)
        page.unroute(pattern, inspect)


def wait_for_submission(page, platform, on_verification, guard=None, verification_acknowledged=None,
                        verification_cancelled=None, timeout_seconds=None, retry_submit=None):
    deadline = monotonic() + 45
    verifying = False
    verification_seen = False
    verification_deadline = None
    timeout = VERIFICATION_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    while not page.is_closed():
        if confirmed(page.url, platform):
            return
        if guard and guard.get("blocked") and not guard.get("allowed"):
            raise PublishUncertain(guard["reason"])
        if verifying and platform == "xiaohongshu":
            check_verification_stop(verification_deadline, verification_cancelled)
        try:
            verify_origin(page, platform)
            check_challenge(page)
        except LoginNeeded:
            verification_seen = True
            if not verifying:
                if not on_verification:
                    raise PublishUncertain("提交后需要人工验证，请到笔记管理核对。")
                verifying = True
                verification_deadline = monotonic() + timeout
                on_verification("提交后需要在 Edge 窗口处理验证，等待平台确认。")
            if guard:
                guard["response"] = None
            page.wait_for_timeout(350)
            continue
        if verifying:
            if not verification_seen:
                if not verification_acknowledged or not verification_acknowledged():
                    page.wait_for_timeout(350)
                    continue
            verifying = False
            verification_seen = False
            verification_deadline = None
            if guard:
                guard["response"] = None
            deadline = monotonic() + 45
            on_verification(None)
            if guard and guard.get('verification_rejected') and guard.get('allowed') == 1:
                guard['verification_rejected'] = False
                guard['retry_armed'] = True
                if retry_submit is not None:
                    retry_submit()
        if guard and guard.get("response") is not None:
            response, guard["response"] = guard["response"], None
            try:
                data = response.json()
            except Exception:
                data = {}
            if response.status >= 400 or (isinstance(data, dict) and data.get("success") is False):
                reason = data.get("msg") or data.get("message") if isinstance(data, dict) else ""
                if on_verification and is_verification_rejection(data):
                    if guard:
                        guard['verification_rejected'] = True
                    verifying = True
                    verification_deadline = monotonic() + timeout
                    on_verification("平台要求验证，请在 Edge 窗口处理，随后等待结果。")
                    page.wait_for_timeout(350)
                    continue
                raise PublishUncertain(f"平台未确认提交：{str(reason or f'HTTP {response.status}')[:150]}。请到笔记管理核对。")
        notices = page.locator('[role="alert"], [class*="toast"], [class*="message"]')
        for notice in notices.all():
            if notice.is_visible():
                message = notice.inner_text().strip()
                if message and any(word in message for word in ("发布失败", "提交失败", "网络错误")):
                    raise PublishUncertain(f"平台提示：{message[:150]}。请在笔记管理核对结果。")
        if monotonic() >= deadline:
            break
        page.wait_for_timeout(350)
    raise PublishUncertain("平台未确认提交。请在笔记管理核对结果。")


def wait_for_verification(page, platform, on_verification, verification_cancelled=None, timeout_seconds=None):
    from playwright.sync_api import Error

    on_verification("请在打开的 Edge 窗口完成登录或验证，回到图文发布页。")
    control, state = entry_controls(page, platform)
    timeout = VERIFICATION_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    deadline = monotonic() + timeout if platform == "xiaohongshu" else None
    while not page.is_closed():
        check_verification_stop(deadline, verification_cancelled)
        try:
            verify_origin(page, platform)
            check_challenge(page)
            control.wait_for(state=state, timeout=500)
        except (Error, LoginNeeded):
            if page.is_closed():
                break
        else:
            on_verification(None)
            return
        page.wait_for_timeout(350)
    raise LoginNeeded("登录或验证尚未完成，可处理后重试。")


def xiaohongshu_switch(page, label):
    return page.locator(".custom-switch-card").filter(has=page.get_by_text(label, exact=True))


def set_xiaohongshu_options(page, post):
    editor = page.locator(".publish-page-content")
    if post.get("original"):
        original = xiaohongshu_switch(page, "原创声明")
        checkbox = original.locator("input[type=checkbox]")
        if not checkbox.is_checked():
            original.locator(".d-switch").click()
            dialog = page.locator(".originalContainer")
            dialog.locator(".d-checkbox-label").click()
            dialog.get_by_role("button", name="声明原创", exact=True).click()
        if not checkbox.is_checked():
            raise ValueError("平台未确认原创声明。")
    if declaration := post.get("declaration"):
        label = DECLARATIONS[declaration]
        editor.get_by_text("添加内容类型声明", exact=True).click()
        if declaration in {"repost", "self_shot"}:
            page.get_by_text("内容来源声明", exact=True).hover()
        page.get_by_text(label, exact=True).click()
        if declaration == "repost":
            modal = page.locator(".d-modal-mask:visible")
            modal.get_by_placeholder("请输入媒体名称", exact=True).fill(post["source_name"])
            modal.get_by_role("button", name="确认", exact=True).click()
            modal.wait_for(state="hidden", timeout=5000)
        elif declaration == "self_shot":
            from playwright.sync_api import TimeoutError

            modal = page.locator(".self-shooting-contain:visible")
            location = modal.get_by_placeholder("下拉选择地点", exact=True)
            location.fill(post["shoot_place"])
            names = page.locator('[role="option"] .name').filter(
                has_text=re.compile(r"^" + re.escape(post["shoot_place"]) + r"$")
            )
            try:
                names.first.wait_for(state="visible", timeout=10000)
            except TimeoutError as exc:
                raise ValueError("平台没有找到这个拍摄地点，请使用小红书显示的准确名称。") from exc
            options = names.locator("xpath=ancestor::*[@role='option'][1]")
            if post.get("shoot_address"):
                options = options.filter(has=page.locator(".subname").filter(
                    has_text=re.compile(r"^" + re.escape(post["shoot_address"]) + r"$")
                ))
            if options.count() != 1:
                raise ValueError("平台找到多个同名地点，请填写小红书显示的完整地址。")
            options.first.click()
            modal.get_by_placeholder("下拉选择日期", exact=True).fill(post["shoot_date"])
            if modal.locator(".date-picker input").input_value() != post["shoot_date"]:
                raise ValueError("平台显示的拍摄日期与选择不一致。")
            modal.get_by_role("button", name="确认", exact=True).click()
            modal.wait_for(state="hidden", timeout=5000)
        editor.get_by_text(label, exact=True).wait_for(state="visible", timeout=5000)
    if schedule := post.get("schedule"):
        expected = schedule_time(schedule).strftime("%Y-%m-%d %H:%M")
        switch = xiaohongshu_switch(page, "定时发布")
        checkbox = switch.locator("input[type=checkbox]")
        if not checkbox.is_checked():
            switch.locator(".d-switch").click()
        field = editor.locator(".d-datepicker-input-filter input.d-text")
        field.fill(expected)
        field.press("Enter")
        field.blur()
        if field.input_value() != expected or not checkbox.is_checked():
            raise ValueError("平台显示的定时时间与批次不一致。")


def publish(account, post, on_verification=None, verification_acknowledged=None, verification_cancelled=None):
    phase = {"attempted": False, "guard": None}
    try:
        return _publish(account, post, on_verification, verification_acknowledged, verification_cancelled, phase)
    except Exception as exc:
        if account["platform"] == "xiaohongshu" and phase["attempted"]:
            guard = phase["guard"]
            if guard and guard["blocked"] and not guard["allowed"]:
                raise ValueError(guard["reason"] + "，提交已中止。") from exc
            if isinstance(exc, VerificationStopped):
                raise VerificationUncertain(f"{exc} 已尝试提交，请先到小红书核对结果。") from exc
            if not isinstance(exc, PublishUncertain):
                raise PublishUncertain("已尝试提交，结果尚未确认。请到笔记管理核对后再处理。") from exc
        raise


def _publish(account, post, on_verification, verification_acknowledged, verification_cancelled, phase):
    from playwright.sync_api import Error

    platform = account["platform"]
    expected_identity = account.get("expected_identity")
    if platform == "xiaohongshu" and not expected_identity:
        raise ValueError("账号尚未核验身份，请先检查账号并重新生成预览。")
    with creator_page(account, headed=on_verification is not None) as page:
        observed = observe_xiaohongshu_identity(page) if platform == "xiaohongshu" else None
        uploaded = observe_xiaohongshu_uploads(page, post.get("images", [])) if platform == "xiaohongshu" else None
        try:
            page.goto(PAGES[platform], wait_until="domcontentloaded", timeout=30000)
            for attempt in range(2):
                try:
                    verify_origin(page, platform)
                    check_challenge(page)
                    if observed is not None:
                        require_xiaohongshu_identity(page, observed, expected_identity)
                    fill_post(page, platform, post)
                    if platform == "xiaohongshu":
                        set_xiaohongshu_options(page, post)
                        # The creator form updates its internal title model after
                        # the visible input changes.
                        page.wait_for_timeout(3000)
                        wait_for_upload_evidence(page, post.get('images', []), uploaded)
                    click = prepare_submit(page, platform, scheduled="schedule" in post)
                    verify_origin(page, platform)
                    check_challenge(page)
                    if observed is not None:
                        require_xiaohongshu_identity(page, observed, expected_identity)
                except (Error, ValueError, LoginNeeded) as exc:
                    try:
                        if isinstance(exc, LoginNeeded):
                            raise exc
                        verify_origin(page, platform)
                        check_challenge(page)
                    except LoginNeeded:
                        if attempt or not on_verification:
                            raise
                        wait_for_verification(page, platform, on_verification, verification_cancelled)
                        if observed is not None:
                            observed["identity_hash"] = None
                        page.goto(PAGES[platform], wait_until="domcontentloaded", timeout=30000)
                        continue
                    if isinstance(exc, ValueError):
                        raise
                    raise ValueError("发布页或素材未准备完成，请检查对应创作者页面。") from exc
                break
        except Error as exc:
            raise ValueError("未能打开小红书发布表单，请检查网络后重试。") from exc

        def attempt_submit():
            phase["attempted"] = True
            return click()

        if platform != "xiaohongshu":
            submit_once(page, attempt_submit, platform, on_verification,
                        verification_acknowledged=verification_acknowledged,
                        verification_cancelled=verification_cancelled)
            return
        with verify_xiaohongshu_submission(page, post, uploaded) as guard:
            phase["guard"] = guard
            def retry_after_verification():
                verify_origin(page, platform)
                check_challenge(page)
                require_xiaohongshu_identity(page, observed, expected_identity)
                prepare_submit(page, platform, scheduled="schedule" in post)()
            submit_once(page, attempt_submit, platform, on_verification, guard, verification_acknowledged,
                        verification_cancelled, retry_submit=retry_after_verification if on_verification else None)
            if not guard["allowed"]:
                raise PublishUncertain("未能核对平台提交内容，请在笔记管理检查。")
