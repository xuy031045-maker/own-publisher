"""Read-only reconciliation with the currently logged-in creator note list."""
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import re
from time import monotonic
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageChops, ImageOps, ImageStat

from browser import (check_challenge, creator_page, observe_xiaohongshu_identity,
                     require_xiaohongshu_identity, verify_origin)
from publisher import browser_lock, db, get_account, hash_file
from xiaohongshu import BEIJING

MANAGER = 'https://creator.xiaohongshu.com/new/note-manager'
MAX_THUMBNAIL_BYTES = 12 * 1024 * 1024
MAX_NOTE_PAGES = 20


def epoch_seconds(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    return value / 1000 if value > 100_000_000_000 else value


def candidate_status(row, post, updated, known_note_id=None):
    """Require an actual matching platform timestamp, never infer state from local time."""
    if not isinstance(row, dict) or row.get('display_title') != post.get('title'):
        return None
    images = row.get('images_list')
    if not isinstance(images, list) or len(images) != len(post.get('images', [])):
        return None
    note_id = row.get('id')
    if not isinstance(note_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', note_id):
        return None
    if known_note_id and note_id != known_note_id:
        return None
    scheduled = epoch_seconds(row.get('schedule_post_time'))
    visible = epoch_seconds(row.get('visible_time'))
    present = datetime.now(timezone.utc).timestamp()
    platform_state = row.get('tab_status')
    try:
        if known_note_id:
            if platform_state in (2, 3):
                return 'auditing' if platform_state == 2 else 'rejected'
            if platform_state == 1 and visible and visible <= present:
                return 'published'
            if platform_state == 4 and scheduled and post.get('schedule'):
                expected = datetime.strptime(post['schedule'], '%Y-%m-%dT%H:%M').replace(tzinfo=BEIJING).timestamp()
                return 'scheduled' if abs(scheduled - expected) <= 60 else None
            return None
        if post.get('schedule'):
            expected = datetime.strptime(post['schedule'], '%Y-%m-%dT%H:%M').replace(tzinfo=BEIJING).timestamp()
            if platform_state == 4 and scheduled and abs(scheduled - expected) <= 60:
                return 'scheduled'
            if platform_state in (2, 3) and any(timestamp and abs(timestamp - expected) <= 60
                                                for timestamp in (scheduled, visible)):
                return 'auditing' if platform_state == 2 else 'rejected'
            if platform_state == 1 and visible and visible <= present and abs(visible - expected) <= 60:
                return 'published'
            return None
        if scheduled or not visible or visible > present or platform_state not in (1, 2, 3):
            return None
        submitted = datetime.strptime(updated, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
        if abs(visible - submitted) <= 20 * 60:
            return {1: 'published', 2: 'auditing', 3: 'rejected'}[platform_state]
        return None
    except (ValueError, TypeError, OverflowError):
        return None


def safe_thumbnail_url(raw):
    if not isinstance(raw, str):
        raise ValueError('平台图片地址无效。')
    url = urlparse(raw)
    host = url.hostname or ''
    if (url.scheme not in ('http', 'https') or not host.endswith('.xhscdn.com')
            or url.username or url.password or url.port not in (None, 80, 443)):
        raise ValueError('平台图片地址不在允许的范围内。')
    return url._replace(scheme='https', netloc=host).geturl()


def thumbnail_matches(page, local, remote):
    path = Path(local['path'])
    if not path.is_file() or hash_file(path) != local['sha256']:
        raise ValueError('预览素材已丢失或发生变化，无法匹配平台记录。')
    if not isinstance(remote, dict):
        return False
    response = page.request.get(safe_thumbnail_url(remote.get('url')), timeout=15000, max_redirects=0)
    if not response.ok:
        raise ValueError('未能读取平台图片缩略图。')
    length = response.headers.get('content-length', '')
    if length.isdigit() and int(length) > MAX_THUMBNAIL_BYTES:
        raise ValueError('平台图片缩略图过大。')
    content = response.body()
    if len(content) > MAX_THUMBNAIL_BYTES:
        raise ValueError('平台图片缩略图过大。')
    try:
        with Image.open(path) as source, Image.open(BytesIO(content)) as actual:
            source = ImageOps.exif_transpose(source).convert('RGB')
            actual = ImageOps.exif_transpose(actual).convert('RGB')
            if abs(source.width / source.height - actual.width / actual.height) > 0.02:
                return False
            diff = ImageChops.difference(source.resize((128, 128)), actual.resize((128, 128)))
            return sum(ImageStat.Stat(diff).mean) / 3 <= 1.5
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError('无法读取平台图片缩略图。') from exc


def match_records(page, records, post, updated, known_note_id=None):
    matches = {}
    for row in records:
        status = candidate_status(row, post, updated, known_note_id)
        if status and all(thumbnail_matches(page, local, remote)
                          for local, remote in zip(post['images'], row['images_list'])):
            old = matches.get(row['id'])
            if old and old['status'] != status:
                raise ValueError('平台列表中同一作品的状态不一致，请手动核对。')
            matches[row['id']] = {'note_id': row['id'], 'status': status}
    if len(matches) != 1:
        raise ValueError('笔记列表中没有唯一匹配项，请手动在小红书核对。')
    return next(iter(matches.values()))


def collect_creator_notes(page, captured, *, timeout_seconds=10, max_pages=MAX_NOTE_PAGES, known_note_id=None,
                          cancel_requested=None):
    """Follow the creator page's own infinite-scroll list until its end marker."""
    records = []
    index = 0
    expected = '0'
    seen = set()
    while len(seen) < max_pages:
        if cancel_requested and cancel_requested():
            from browser_queue import QueueCancelled
            raise QueueCancelled('平台核对已取消。')
        deadline = monotonic() + timeout_seconds
        scrolled = False
        while True:
            if cancel_requested and cancel_requested():
                from browser_queue import QueueCancelled
                raise QueueCancelled('平台核对已取消。')
            while index < len(captured) and captured[index]['requested'] in seen:
                index += 1
            if index < len(captured):
                break
            if monotonic() >= deadline:
                raise ValueError('未能完整读取小红书笔记列表，请在平台手动核对。')
            if seen and not scrolled:
                try:
                    page.locator('#notes-request:visible').first.scroll_into_view_if_needed(timeout=5000)
                except Exception as exc:
                    if index < len(captured):
                        continue
                    raise ValueError('无法读取更多平台笔记，请在平台手动核对。') from exc
                scrolled = True
            page.wait_for_timeout(200)
        chunk = captured[index]
        index += 1
        if chunk['requested'] != expected:
            raise ValueError('平台笔记翻页顺序发生变化，请手动核对。')
        seen.add(expected)
        records.extend(chunk['notes'])
        if known_note_id and any(isinstance(item, dict) and item.get('id') == known_note_id for item in chunk['notes']):
            return records
        next_page = chunk['next']
        if next_page == -1:
            return records
        if type(next_page) is not int or next_page < 0 or str(next_page) in seen:
            raise ValueError('平台笔记分页信息异常，请手动核对。')
        expected = str(next_page)
    raise ValueError(f'平台笔记超过 {max_pages} 页，未能完整核对，请在平台手动检查。')


def observe_creator_notes(page):
    captured = []

    def capture(response):
        url = urlparse(response.url)
        if (url.scheme, url.netloc, url.path) != ('https', 'creator.xiaohongshu.com',
                                                   '/api/galaxy/v2/creator/note/user/posted'):
            return
        try:
            result = response.json()
            data = result.get('data', {})
            notes, next_page = data.get('notes'), data.get('page')
            query = parse_qs(url.query)
            requested = query.get('page', [None])[-1]
            if (response.status == 200 and result.get('success') is True and isinstance(notes, list)
                    and type(next_page) is int and query.get('tab') == ['0'] and requested is not None):
                captured.append({'requested': requested, 'next': next_page, 'notes': notes})
        except Exception:
            pass

    page.on('response', capture)
    return captured


def synchronize(batch, alias, cancel_requested=None, on_queue=None):
    import json
    from browser_queue import queued_browser

    account = get_account(alias)
    if account['platform'] != 'xiaohongshu':
        raise ValueError('只支持核对小红书作品。')
    with db() as con:
        row = con.execute('''SELECT tasks.content, tasks.updated, tasks.state, task_identities.identity_hash,
            platform_observations.note_id AS known_note_id
            FROM tasks LEFT JOIN task_identities ON task_identities.batch=tasks.batch
            AND task_identities.alias=tasks.alias LEFT JOIN platform_observations ON
            platform_observations.batch=tasks.batch AND platform_observations.alias=tasks.alias
            WHERE tasks.batch=? AND tasks.alias=?''', (batch, alias)).fetchone()
    if not row or row['state'] not in ('submitted', 'uncertain') or not row['identity_hash']:
        raise ValueError('该任务没有可核对的账号身份或提交记录。')
    post, updated, expected_identity, known_id = json.loads(row['content']), row['updated'], row['identity_hash'], row['known_note_id']
    with queued_browser('platform', cancel_requested=cancel_requested, on_state=on_queue), creator_page(account) as page:
        observed = observe_xiaohongshu_identity(page)
        captured = observe_creator_notes(page)
        page.goto(MANAGER, wait_until='domcontentloaded', timeout=30000)
        verify_origin(page, 'xiaohongshu')
        check_challenge(page)
        require_xiaohongshu_identity(page, observed, expected_identity)
        records = collect_creator_notes(page, captured, known_note_id=known_id)
        result = match_records(page, records, post, updated, known_note_id=known_id)
        with db() as con:
            current = con.execute('SELECT state, content, updated FROM tasks WHERE batch=? AND alias=?', (batch, alias)).fetchone()
            if (not current or current['state'] not in ('submitted', 'uncertain')
                    or json.loads(current['content']) != post or current['updated'] != updated):
                raise ValueError('任务状态发生变化，请重新核对。')
            con.execute('''INSERT INTO platform_observations(batch, alias, note_id, status) VALUES (?, ?, ?, ?)
                ON CONFLICT(batch, alias) DO UPDATE SET note_id=excluded.note_id,
                    status=excluded.status, observed_at=CURRENT_TIMESTAMP''',
                        (batch, alias, result['note_id'], result['status']))
    return result


def audit_restored_account(alias, generation, cancel_requested=None, on_queue=None):
    """Read every accessible creator-list page; never create or modify a platform note."""
    from browser_queue import QueueCancelled, queued_browser
    import restore_guard
    account = get_account(alias)
    if account['platform'] != 'xiaohongshu':
        raise ValueError('该平台请在平台网站手动核对。')
    with db() as con:
        identity = con.execute('SELECT identity_hash FROM account_identities WHERE alias=?',(alias,)).fetchone()
    if not identity:
        raise ValueError('请先检查并核验该账号。')
    expected = identity['identity_hash']
    state = restore_guard.status()
    if state.get('generation') != generation or state.get('broken'):
        raise ValueError('恢复保护状态已改变，请刷新后重试。')
    with queued_browser('platform', cancel_requested=cancel_requested, on_state=on_queue), creator_page(account) as page:
        observed = observe_xiaohongshu_identity(page)
        captured = observe_creator_notes(page)
        page.goto(MANAGER, wait_until='domcontentloaded', timeout=30000)
        verify_origin(page, 'xiaohongshu')
        check_challenge(page)
        require_xiaohongshu_identity(page, observed, expected)
        notes = collect_creator_notes(page, captured, cancel_requested=cancel_requested)
        if cancel_requested and cancel_requested():
            raise QueueCancelled('平台核对已取消。')
        require_xiaohongshu_identity(page, observed, expected)
        with db() as con:
            current = con.execute('SELECT identity_hash FROM account_identities WHERE alias=?',(alias,)).fetchone()
        if not current or current['identity_hash'] != expected:
            raise ValueError('账号身份发生变化，请重新检查账号。')
        return restore_guard.record_audit(generation, alias, expected, notes)
