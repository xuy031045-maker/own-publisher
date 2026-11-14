"""Local, read-only view of conversations already available in the user's browser."""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
from time import monotonic
from urllib.parse import parse_qs, unquote, urlparse

from browser import (LoginNeeded, PAGES, check_challenge, creator_page,
                     observe_xiaohongshu_identity, require_xiaohongshu_identity, verify_origin)
from publisher import browser_lock, db, get_account
from browser_queue import QueueCancelled, queued_browser

CHAT_URL = 'https://www.xiaohongshu.com/chat'
MAX_ACCOUNTS = 10
HISTORY_PAGES_PER_RUN = 20
MAX_HISTORY_PAGES = 200
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_CONVERSATION_MEDIA_BYTES = 64 * 1024 * 1024
QUICK_RECHECK_SECONDS = 3 * 60 * 60


class IdentityMismatch(ValueError):
    pass


def ensure_schema(con):
    con.executescript('''
        CREATE TABLE IF NOT EXISTS inbox_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT NOT NULL REFERENCES accounts(alias), identity_hash TEXT NOT NULL,
            external_id TEXT NOT NULL, location TEXT NOT NULL,
            display_name TEXT NOT NULL, last_preview TEXT NOT NULL DEFAULT '',
            last_time_label TEXT NOT NULL DEFAULT '', last_timestamp_ms INTEGER,
            unread_before INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending', important INTEGER NOT NULL DEFAULT 0,
            history_complete INTEGER NOT NULL DEFAULT 0, history_pages INTEGER NOT NULL DEFAULT 0,
            synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(alias, identity_hash, external_id)
        );
        CREATE TABLE IF NOT EXISTS inbox_messages (
            conversation_id INTEGER NOT NULL REFERENCES inbox_conversations(id),
            external_id TEXT NOT NULL, store_id TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL, content_type TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
            time_label TEXT NOT NULL DEFAULT '', timestamp_ms INTEGER, position INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(conversation_id, external_id)
        );
        CREATE TABLE IF NOT EXISTS inbox_accounts (
            alias TEXT PRIMARY KEY REFERENCES accounts(alias), state TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '', synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            conversation_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS inbox_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES inbox_conversations(id),
            message_external_id TEXT NOT NULL, mime_type TEXT NOT NULL, data BLOB NOT NULL,
            UNIQUE(conversation_id, message_external_id)
        );
        CREATE TABLE IF NOT EXISTS inbox_message_proofs (
            conversation_id INTEGER NOT NULL, message_external_id TEXT NOT NULL,
            PRIMARY KEY(conversation_id, message_external_id),
            FOREIGN KEY(conversation_id, message_external_id)
                REFERENCES inbox_messages(conversation_id, external_id)
        );
        CREATE TABLE IF NOT EXISTS inbox_alert_cursors (
            conversation_id INTEGER PRIMARY KEY REFERENCES inbox_conversations(id),
            baseline_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS inbox_alert_anchor_state (
            conversation_id INTEGER PRIMARY KEY REFERENCES inbox_conversations(id)
        );
        CREATE TABLE IF NOT EXISTS inbox_alert_anchor_ids (
            conversation_id INTEGER NOT NULL REFERENCES inbox_conversations(id),
            message_external_id TEXT NOT NULL,
            PRIMARY KEY(conversation_id,message_external_id)
        );
        CREATE TABLE IF NOT EXISTS inbox_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES inbox_conversations(id),
            alert_key TEXT NOT NULL, kind TEXT NOT NULL,
            message_external_id TEXT, acknowledged INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(conversation_id, alert_key)
        );
        CREATE TABLE IF NOT EXISTS inbox_account_checkpoints (
            alias TEXT NOT NULL REFERENCES accounts(alias), identity_hash TEXT NOT NULL,
            list_sync_ms INTEGER, PRIMARY KEY(alias, identity_hash)
        );
        CREATE TABLE IF NOT EXISTS inbox_quick_checkpoints (
            conversation_id INTEGER PRIMARY KEY REFERENCES inbox_conversations(id),
            signature TEXT NOT NULL, verified_at_ms INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inbox_sync_metrics (
            alias TEXT PRIMARY KEY REFERENCES accounts(alias), identity_hash TEXT NOT NULL,
            mode TEXT NOT NULL, elapsed_ms INTEGER NOT NULL, wait_ms INTEGER NOT NULL,
            opened INTEGER NOT NULL, reused INTEGER NOT NULL, recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS inbox_messages_order ON inbox_messages(conversation_id, timestamp_ms, position);
        CREATE INDEX IF NOT EXISTS inbox_conversations_account ON inbox_conversations(alias, identity_hash);
        CREATE INDEX IF NOT EXISTS inbox_alerts_unseen ON inbox_alerts(conversation_id, acknowledged, kind);
    ''')


def current_identity(con, alias):
    row = con.execute('SELECT identity_hash FROM account_identities WHERE alias=?', (alias,)).fetchone()
    if not row:
        raise ValueError('请先在账号管理中检查并核验该账号。')
    return row['identity_hash']


def verify_web_identity(page, expected):
    current = urlparse(page.url)
    if (current.scheme, current.netloc) != ('https', 'www.xiaohongshu.com'):
        raise LoginNeeded('私信页面已跳转，请检查该账号的登录状态。')
    check_challenge(page)
    hrefs = page.evaluate('''() => [...new Set([...document.querySelectorAll('a')]
        .filter(a => a.innerText.trim() === '我' && a.getAttribute('href')?.startsWith('/user/profile/'))
        .map(a => a.getAttribute('href')))]''')
    if len(hrefs) != 1:
        raise LoginNeeded('无法确认私信页面的账号身份，请重新登录并检查账号。')
    path = urlparse(hrefs[0]).path
    match = re.fullmatch(r'/user/profile/([A-Za-z0-9_-]{1,100})/?', path)
    if not match or hashlib.sha256(match.group(1).encode()).hexdigest() != expected:
        raise IdentityMismatch('私信页面的账号与已核验账号不一致，已停止同步。')


def _read_list(page):
    return page.locator('.xhs-im-conv-item[data-conv-kind="c2c"]').evaluate_all('''items => items.map(e => ({
        external_id: e.dataset.convId || '',
        display_name: e.querySelector('.xhs-im-conv-item__name')?.innerText?.trim() || '',
        last_preview: e.querySelector('.xhs-im-conv-item__summary-text')?.innerText?.trim() || '',
        last_time_label: e.querySelector('.xhs-im-conv-item__time')?.innerText?.trim() || '',
        unread_before: Number.parseInt(e.querySelector('.xhs-im-conv-item__badge')?.innerText || '0', 10) || 0
    }))''')


def _scroll_conversation_list(page):
    found = {}
    scroll = page.locator('.xhs-im-conv-list__scroll').first
    if scroll.count():
        scroll.evaluate('(e) => {e.scrollTop=0; e.dispatchEvent(new Event("scroll"));}')
        page.wait_for_timeout(150)
    stable_at_end = 0
    def remember_visible():
        top = scroll.evaluate('e => e.scrollTop') if scroll.count() else 0
        for item in _read_list(page):
            cid = item['external_id']
            if cid and len(cid) <= 256 and not re.search(r'[\x00-\x1f/]', cid):
                position = found[cid]['_scroll_top'] if cid in found else top
                found[cid] = item | {'_scroll_top': position}
    for _ in range(300):
        remember_visible()
        if not scroll.count():
            break
        before = len(found)
        state = scroll.evaluate('''e => {
          const maximum=Math.max(0,e.scrollHeight-e.clientHeight);
          e.scrollTop=Math.min(maximum,e.scrollTop+Math.max(40,Math.floor(e.clientHeight*0.7)));
          e.dispatchEvent(new Event('scroll'));
          return {top:e.scrollTop,maximum};
        }''')
        page.wait_for_timeout(350 if state['top'] >= state['maximum'] - 1 else 120)
        remember_visible()
        after = scroll.evaluate('e => ({top:e.scrollTop, maximum:Math.max(0,e.scrollHeight-e.clientHeight)})')
        at_end = after['top'] >= after['maximum'] - 1
        stable_at_end = stable_at_end + 1 if at_end and before == len(found) else 0
        if stable_at_end >= 3:
            break
    return found


def _wait_for_list(page):
    deadline = monotonic() + 12
    while monotonic() < deadline:
        if page.locator('.xhs-im-conv-item').count() or page.get_by_text('暂无消息', exact=False).count():
            return
        page.wait_for_timeout(250)
    raise ValueError('私信列表未能加载，已保留此前同步的数据。')


def _gather_conversations(page):
    _wait_for_list(page)
    result = []
    main = _scroll_conversation_list(page)
    for item in main.values():
        result.append(item | {'location': 'main'})
    folder = page.locator('.xhs-im-conv-item[data-conv-kind="stranger-folder"]')
    if folder.count():
        folder.first.click()
        page.wait_for_timeout(800)
        stranger = _scroll_conversation_list(page)
        for cid, item in stranger.items():
            if cid not in main:
                result.append(item | {'location': 'stranger'})
        back = page.locator('.xhs-im-conv-list__back-btn')
        if not back.count():
            raise ValueError('无法返回私信列表，已保留此前同步的数据。')
        back.first.click()
        page.wait_for_timeout(500)
    return result


def _select_conversation(page, item, current_location):
    cid = item.get('external_id')
    if not isinstance(cid, str) or not 1 <= len(cid) <= 256 or re.search(r'[\x00-\x1f/]', cid):
        raise ValueError('会话标识无效。')
    # A virtualized list can replace and reorder its DOM while Playwright scrolls.
    # Select by the immutable identifier, rather than a positional index.
    escaped_cid = cid.replace('\\', '\\\\').replace('"', '\\"')
    target = page.locator(f'.xhs-im-conv-item[data-conv-kind="c2c"][data-conv-id="{escaped_cid}"]')
    back_button = page.locator('.xhs-im-conv-list__back-btn')
    current_location = 'stranger' if back_button.count() and back_button.first.is_visible() else 'main'
    if item['location'] != current_location:
        if item['location'] == 'stranger':
            folder = page.locator('.xhs-im-conv-item[data-conv-kind="stranger-folder"]')
            if not folder.count():
                raise ValueError('陌生人私信列表暂不可用。')
            folder.first.click()
        else:
            back = page.locator('.xhs-im-conv-list__back-btn')
            if not back.count():
                raise ValueError('无法返回私信列表。')
            back.first.click()
        page.wait_for_timeout(500)
    locator = page.locator('.xhs-im-conv-item[data-conv-kind="c2c"]')
    scroll = page.locator('.xhs-im-conv-list__scroll').first
    identifiers = locator.evaluate_all('items => items.map(e => e.dataset.convId || "")')
    hint = item.get('_scroll_top')
    started_at_top = not isinstance(hint, (int, float)) or hint <= 0
    if item['external_id'] not in identifiers and scroll.count():
        scroll.evaluate('(e, top) => {e.scrollTop=top; e.dispatchEvent(new Event("scroll"));}',
                        hint if not started_at_top else 0)
        page.wait_for_timeout(150)
    stable_at_end = 0
    previous_ids = None
    for _ in range(300):
        identifiers = locator.evaluate_all('items => items.map(e => e.dataset.convId || "")')
        if cid in identifiers:
            if identifiers.count(cid) != 1:
                raise ValueError('会话标识不唯一，已停止读取。')
            target.click()
            deadline = monotonic() + 8
            while monotonic() < deadline:
                u = urlparse(page.url)
                parts = u.path.split('/')
                if (u.scheme, u.netloc) == ('https', 'www.xiaohongshu.com') and len(parts) == 3 and parts[1] == 'chat' and unquote(parts[2]) == cid:
                    return item['location']
                page.wait_for_timeout(150)
            raise ValueError('未能核对选中的私信会话，已停止读取该会话。')
        if not scroll.count():
            break
        state = scroll.evaluate('''e => {
          const maximum=Math.max(0,e.scrollHeight-e.clientHeight);
          e.scrollTop=Math.min(maximum,e.scrollTop+Math.max(40,Math.floor(e.clientHeight*0.7)));
          e.dispatchEvent(new Event('scroll'));
          return {top:e.scrollTop,maximum};
        }''')
        page.wait_for_timeout(350 if state['top'] >= state['maximum'] - 1 else 120)
        after = scroll.evaluate('e => ({top:e.scrollTop, maximum:Math.max(0,e.scrollHeight-e.clientHeight)})')
        at_end = after['top'] >= after['maximum'] - 1
        stable_at_end = stable_at_end + 1 if at_end and identifiers == previous_ids else 0
        previous_ids = identifiers
        if stable_at_end >= 3:
            if not started_at_top:
                scroll.evaluate('(e) => {e.scrollTop=0; e.dispatchEvent(new Event("scroll"));}')
                page.wait_for_timeout(150)
                stable_at_end, previous_ids, started_at_top = 0, None, True
            else:
                break
    raise ValueError('该会话未在当前列表中加载，请稍后重试。')


def _read_messages(page):
    return page.evaluate('''() => {
      const result=[]; let label='';
      const list=document.querySelector('.xhs-im-msg-list');
      if (!list) return null;
      const inline = node => {
        if (node.nodeType === Node.TEXT_NODE) return node.textContent;
        if (node.nodeType !== Node.ELEMENT_NODE) return '';
        if (node.tagName === 'BR') return '\\n';
        if (node.tagName === 'IMG') return node.alt || '[表情]';
        return [...node.childNodes].map(inline).join('');
      };
      for (const e of list.querySelectorAll('.xhs-im-msg-list__time-divider, .chat-item')) {
        if (e.classList.contains('xhs-im-msg-list__time-divider')) {label=e.innerText.trim();continue;}
        const b=e.querySelector('.chat-item__body'), t=e.dataset.contentType || '';
        const direction=e.querySelector('.chat-item__content--left')?'incoming':
          e.querySelector('.chat-item__content--right')?'outgoing':'system';
        const textElement=b?.querySelector('.xhs-im-bubble__text');
        let body=(b?.innerText || '').trim();
        if (t==='1' && textElement) {
          const original=textElement.innerText || '', enhanced=inline(textElement);
          if (original && body.includes(original)) body=body.replace(original, enhanced);
          else if (!body) body=enhanced;
        }
        const hasImages=!!b?.querySelector('img');
        const image = t==='2' ? b?.querySelector('.xhs-im-bubble__image') :
          (['13','16'].includes(t) ? b?.querySelector('img') : null);
        if (!body) body= t==='9' ? '[语音]' : (hasImages ? '[图片或表情]' : '[暂不支持的消息]');
        else if (t==='9') body='[语音] ' + body;
        result.push({external_id:e.dataset.messageId || '', store_id:e.dataset.storeId || '',
          content_type:t, direction, body, time_label:label, image_src:image?.currentSrc || image?.src || ''});
      }
      return result;
    }''')


def _existing_history(alias, identity, external_id):
    with db() as con:
        ensure_schema(con)
        row = con.execute('''SELECT id, history_complete, history_pages FROM inbox_conversations
                             WHERE alias=? AND identity_hash=? AND external_id=?''',
                          (alias, identity, external_id)).fetchone()
        if not row:
            return False, 0, set()
        initialized = con.execute('SELECT 1 FROM inbox_alert_anchor_state WHERE conversation_id=?', (row['id'],)).fetchone()
        table = 'inbox_alert_anchor_ids' if initialized else 'inbox_message_proofs'
        ids = {r['message_external_id'] for r in con.execute(
            f'SELECT message_external_id FROM {table} WHERE conversation_id=?', (row['id'],))}
        return bool(row['history_complete']), row['history_pages'], ids


def _validate_image(data):
    from PIL import Image
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        return None
    types = {'JPEG': 'image/jpeg', 'PNG': 'image/png', 'WEBP': 'image/webp', 'GIF': 'image/gif'}
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in types or image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_IMAGE_PIXELS:
                return None
            mime = types[image.format]
            image.verify()
            return mime
    except Exception:
        return None


def _fetch_media(page, alias, identity, item, messages):
    with db() as con:
        ensure_schema(con)
        existing = {row['message_external_id'] for row in con.execute('''
            SELECT m.message_external_id FROM inbox_media m JOIN inbox_conversations c ON c.id=m.conversation_id
            WHERE c.alias=? AND c.identity_hash=? AND c.external_id=?''', (alias, identity, item['external_id']))}
    result = {}
    total_bytes = 0
    for message in messages:
        if len(result) >= 120 or total_bytes >= MAX_CONVERSATION_MEDIA_BYTES:
            break
        mid, source = message['external_id'], message.get('image_src', '')
        if not mid or mid in existing or not source:
            continue
        try:
            u = urlparse(source)
            host = u.hostname or ''
            if (u.scheme != 'https' or u.username or u.password or u.port not in (None, 443) or
                    not (host.endswith('.xhscdn.com') or host == 'picasso-static.xiaohongshu.com')):
                continue
            response = page.context.request.get(source, headers={'Referer': 'https://www.xiaohongshu.com/'},
                                                timeout=10000, max_redirects=0)
            length = response.headers.get('content-length', '')
            if not response.ok or (length.isdecimal() and int(length) > MAX_IMAGE_BYTES):
                continue
            data = response.body()
            mime = _validate_image(data)
            if mime and total_bytes + len(data) <= MAX_CONVERSATION_MEDIA_BYTES:
                result[mid] = (mime, data)
                total_bytes += len(data)
        except Exception:
            continue
    return result


def _recent_incoming(observations, messages, known_ids, timestamps, baseline_ms):
    """Compare a newest-first platform page with IDs saved before this sync."""
    if observations is None:
        return [], []
    order = []
    seen = set()
    for event in observations:
        ids = event.get('ordered_ids')
        if not event.get('ok') or event.get('invalid_ids') or event.get('order_verified') is not True or not isinstance(ids, list):
            candidates = sorted({str(x) for x in ids or [] if x})
            marker = hashlib.sha256(json.dumps(candidates,ensure_ascii=False).encode('utf-8')).hexdigest() if candidates else 'unknown'
            return [], ['order:' + marker]
        for mid in ids:
            if mid not in seen:
                order.append(mid)
                seen.add(mid)
    if not order:
        return [], []
    available = {message['external_id']:message for message in messages}
    missing = ['unrendered:' + mid for mid in order if mid not in available]
    observed_marker = hashlib.sha256(json.dumps(sorted(set(order)),ensure_ascii=False).encode('utf-8')).hexdigest()
    boundary = next((index for index, mid in enumerate(order) if mid in known_ids), None)
    if known_ids and boundary is None:
        return [], ['overlap:' + observed_marker] + missing
    candidates = order[:boundary] if boundary is not None else order
    if not candidates:
        return [], missing
    if baseline_ms is None:
        return [], ['baseline:' + observed_marker] + missing
    new_ids, review = [], missing[:]
    for mid in candidates:
        message = available.get(mid)
        if not message:
            continue
        if message.get('direction') == 'outgoing':
            continue
        if message.get('direction') != 'incoming':
            review.append('direction:' + mid)
            continue
        stamp = timestamps.get(mid)
        if not isinstance(stamp, int):
            review.append('time:' + mid)
        elif stamp > baseline_ms + 2000:
            new_ids.append(mid)
        elif stamp >= baseline_ms - 2000:
            review.append('boundary:' + mid)
    return new_ids, review


def _observations_complete(observations, accepted_ids):
    if not observations:
        return False
    for event in observations:
        ordered = event.get('ordered_ids')
        ids = event.get('ids')
        if (not event.get('ok') or event.get('invalid_ids') or event.get('order_verified') is not True or
                event.get('render_verified') is not True or
                not isinstance(ordered, list) or not isinstance(ids, set) or
                set(ordered) != ids or not ids.issubset(accepted_ids)):
            return False
    return True


def _coverage_complete(observations, known_ids, history_complete):
    observed = {mid for event in observations or [] for mid in event.get('ordered_ids',[]) if mid}
    return bool(observed.intersection(known_ids)) or bool(history_complete)


def _list_signature(item):
    stamp, store, updated = (item.get('_last_timestamp'), item.get('_max_store_id'), item.get('_list_update_time'))
    if (type(stamp) is not int or not 1_000_000_000_000 <= stamp < 10_000_000_000_000 or
            type(store) is not int or store <= 0 or type(updated) is not int or updated <= 0 or
            not isinstance(item.get('last_preview'), str)):
        return None
    # Pre-fix cache entries did not attest that every rendered row was verified.
    value = ['render-verified-v2', stamp, str(store), str(updated), item['last_preview']]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def _metadata_matches_latest(item, observations, messages, timestamps):
    newest = next((mid for event in observations or [] for mid in event.get('ordered_ids',[]) if mid), None)
    message = next((m for m in messages if m.get('external_id') == newest), None)
    stamp = timestamps.get(newest)
    return bool(message and type(stamp) is int and type(item.get('_last_timestamp')) is int and
                abs(stamp-item['_last_timestamp']) <= 2000 and
                str(item.get('_max_store_id')) == str(message.get('store_id','')))


def _reuse_unchanged_conversation(alias, identity, item):
    signature = _list_signature(item)
    if not signature or item.get('unread_before'):
        return None
    now = int(time.time() * 1000)
    with db() as con:
        ensure_schema(con)
        if current_identity(con,alias) != identity:
            raise IdentityMismatch('账号身份发生变化，已停止同步。')
        row = con.execute('''SELECT c.id,c.history_complete,q.signature,q.verified_at_ms
                    FROM inbox_conversations c JOIN inbox_quick_checkpoints q ON q.conversation_id=c.id
                    WHERE c.alias=? AND c.identity_hash=? AND c.external_id=?''',
                    (alias,identity,item['external_id'])).fetchone()
        if not row or row['signature'] != signature or not 0 <= now-row['verified_at_ms'] <= QUICK_RECHECK_SECONDS*1000:
            return None
        con.execute('''UPDATE inbox_conversations SET location=?,display_name=?,last_preview=?,last_time_label=?,
                       unread_before=?,synced_at=CURRENT_TIMESTAMP WHERE id=?''',
                    (item['location'],item['display_name'],item['last_preview'],item['last_time_label'],
                     item['unread_before'],row['id']))
        return {'history_complete':bool(row['history_complete'])}


def _has_unresolved_review(alias, identity, external_id):
    with db() as con:
        ensure_schema(con)
        return bool(con.execute('''SELECT 1 FROM inbox_alerts a JOIN inbox_conversations c ON c.id=a.conversation_id
                 WHERE c.alias=? AND c.identity_hash=? AND c.external_id=?
                   AND a.kind='review' AND a.acknowledged=0 LIMIT 1''',(alias,identity,external_id)).fetchone())


def _ensure_alert_anchor(con, conversation_id):
    created = con.execute('INSERT OR IGNORE INTO inbox_alert_anchor_state(conversation_id) VALUES(?)',
                          (conversation_id,)).rowcount
    if created:
        con.execute('''INSERT OR IGNORE INTO inbox_alert_anchor_ids(conversation_id,message_external_id)
                       SELECT conversation_id,message_external_id FROM inbox_message_proofs WHERE conversation_id=?''',
                    (conversation_id,))


def _save_conversation(alias, identity, item, messages, timestamps, complete, pages, media=None,
                       observations=None, account_cutoff_ms=None):
    accepted = []
    accepted_ids = set()
    for message in messages:
        mid = message.get('external_id')
        if not isinstance(mid, str) or not 1 <= len(mid) <= 256 or mid in accepted_ids:
            complete = False
            continue
        accepted.append(message)
        accepted_ids.add(mid)
    with db() as con:
        ensure_schema(con)
        if current_identity(con, alias) != identity:
            raise IdentityMismatch('账号身份发生变化，已停止保存。')
        previous = con.execute('''SELECT id,last_timestamp_ms FROM inbox_conversations
                    WHERE alias=? AND identity_hash=? AND external_id=?''',
                    (alias, identity, item['external_id'])).fetchone()
        known_ids = set()
        baseline_ms = account_cutoff_ms
        if previous:
            _ensure_alert_anchor(con, previous['id'])
            known_ids = {row['message_external_id'] for row in con.execute(
                'SELECT message_external_id FROM inbox_alert_anchor_ids WHERE conversation_id=?', (previous['id'],))}
            cursor = con.execute('SELECT baseline_ms FROM inbox_alert_cursors WHERE conversation_id=?',
                                 (previous['id'],)).fetchone()
            baseline_ms = cursor['baseline_ms'] if cursor else previous['last_timestamp_ms']
        valid_times = [timestamps[m['external_id']] for m in accepted if m['external_id'] in timestamps]
        list_timestamp = item.get('_last_timestamp')
        last_timestamp = (list_timestamp if isinstance(list_timestamp, int)
                          and 1_000_000_000_000 <= list_timestamp < 10_000_000_000_000
                          else max(valid_times) if valid_times else None)
        con.execute('''INSERT INTO inbox_conversations(alias,identity_hash,external_id,location,display_name,
                       last_preview,last_time_label,last_timestamp_ms,unread_before,history_complete,history_pages)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(alias,identity_hash,external_id) DO UPDATE SET
                       location=excluded.location, display_name=excluded.display_name,
                       last_preview=excluded.last_preview, last_time_label=excluded.last_time_label,
                       last_timestamp_ms=COALESCE(excluded.last_timestamp_ms,inbox_conversations.last_timestamp_ms),
                       unread_before=excluded.unread_before, history_complete=excluded.history_complete,
                       history_pages=MAX(inbox_conversations.history_pages,excluded.history_pages), synced_at=CURRENT_TIMESTAMP''',
                    (alias, identity, item['external_id'], item['location'], item['display_name'], item['last_preview'],
                     item['last_time_label'], last_timestamp, item['unread_before'], int(complete), pages))
        row = con.execute('SELECT id FROM inbox_conversations WHERE alias=? AND identity_hash=? AND external_id=?',
                          (alias, identity, item['external_id'])).fetchone()
        _ensure_alert_anchor(con, row['id'])
        initial_baseline = baseline_ms
        if initial_baseline is None and observations is None and accepted:
            initial_baseline = last_timestamp
        con.execute('INSERT OR IGNORE INTO inbox_alert_cursors(conversation_id,baseline_ms) VALUES(?,?)',
                    (row['id'], initial_baseline))
        new_ids, review = _recent_incoming(observations, accepted, known_ids, timestamps, baseline_ms)
        if observations:
            unmatched = sorted({mid for event in observations for mid in event.get('render_unmatched', [])})
            if unmatched:
                marker = hashlib.sha256(json.dumps([unmatched,_list_signature(item),last_timestamp],
                    ensure_ascii=False).encode('utf-8')).hexdigest()
                review.append('render-unmatched:' + marker)
            if not _observations_complete(observations, accepted_ids) and not review:
                ids = sorted({mid for event in observations for mid in event.get('ordered_ids',[]) if mid})
                marker = hashlib.sha256(json.dumps([ids,_list_signature(item),last_timestamp],
                    ensure_ascii=False).encode('utf-8')).hexdigest()
                review.append('render-window:' + marker)
        if observations and not known_ids and not complete:
            ids = sorted({mid for event in observations for mid in event.get('ordered_ids',[]) if mid})
            marker = hashlib.sha256(json.dumps(ids,ensure_ascii=False).encode('utf-8')).hexdigest()
            review.append('coverage:' + marker)
        for index, m in enumerate(accepted):
            con.execute('''INSERT INTO inbox_messages(conversation_id,external_id,store_id,direction,content_type,
                           body,time_label,timestamp_ms,position) VALUES(?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(conversation_id,external_id) DO UPDATE SET
                           store_id=excluded.store_id,direction=excluded.direction,content_type=excluded.content_type,
                           body=excluded.body,time_label=excluded.time_label,
                           timestamp_ms=COALESCE(excluded.timestamp_ms,inbox_messages.timestamp_ms), position=excluded.position''',
                        (row['id'], m['external_id'], m['store_id'], m['direction'], m['content_type'], m['body'],
                         m['time_label'], timestamps.get(m['external_id']), index))
            con.execute('''INSERT OR IGNORE INTO inbox_message_proofs(conversation_id,message_external_id)
                           VALUES(?,?)''', (row['id'], m['external_id']))
        inserted = 0
        for mid in new_ids:
            inserted += con.execute('''INSERT OR IGNORE INTO inbox_alerts(conversation_id,alert_key,kind,message_external_id)
                                       VALUES(?,?,'new',?)''', (row['id'], 'new:' + mid, mid)).rowcount
        review_inserted = 0
        unresolved_review = False
        for token in review:
            if token == 'order:unknown' and last_timestamp is not None:
                token += ':' + str(last_timestamp)
            key = 'review:' + hashlib.sha256(token.encode('utf-8')).hexdigest()
            review_inserted += con.execute('''INSERT OR IGNORE INTO inbox_alerts(conversation_id,alert_key,kind)
                                              VALUES(?,?,'review')''', (row['id'], key)).rowcount
            saved_review = con.execute('SELECT acknowledged FROM inbox_alerts WHERE conversation_id=? AND alert_key=?',
                                       (row['id'], key)).fetchone()
            unresolved_review |= not bool(saved_review['acknowledged'])
        unresolved_review |= bool(con.execute('''SELECT 1 FROM inbox_alerts WHERE conversation_id=?
                     AND kind='review' AND acknowledged=0 LIMIT 1''',(row['id'],)).fetchone())
        if inserted or review_inserted:
            con.execute("UPDATE inbox_conversations SET status='pending' WHERE id=?", (row['id'],))
        if observations is None and not previous and accepted:
            con.executemany('''INSERT OR IGNORE INTO inbox_alert_anchor_ids(conversation_id,message_external_id)
                               VALUES(?,?)''', [(row['id'], mid) for mid in accepted_ids])
        safe_window = (_observations_complete(observations, accepted_ids) and not unresolved_review and
                       _coverage_complete(observations,known_ids,complete))
        if safe_window and last_timestamp is not None:
            con.execute('''UPDATE inbox_alert_cursors SET baseline_ms=CASE WHEN baseline_ms IS NULL THEN ?
                       ELSE MAX(baseline_ms,?) END WHERE conversation_id=?''', (last_timestamp,last_timestamp,row['id']))
            con.executemany('''INSERT OR IGNORE INTO inbox_alert_anchor_ids(conversation_id,message_external_id)
                               VALUES(?,?)''', [(row['id'], mid) for mid in accepted_ids])
        signature = _list_signature(item)
        if safe_window and signature and _metadata_matches_latest(item,observations,accepted,timestamps):
            con.execute('''INSERT INTO inbox_quick_checkpoints(conversation_id,signature,verified_at_ms) VALUES(?,?,?)
                           ON CONFLICT(conversation_id) DO UPDATE SET signature=excluded.signature,
                           verified_at_ms=excluded.verified_at_ms''', (row['id'],signature,int(time.time()*1000)))
        else:
            con.execute('DELETE FROM inbox_quick_checkpoints WHERE conversation_id=?',(row['id'],))
        for mid, (mime, data) in (media or {}).items():
            if mid in accepted_ids:
                con.execute('''INSERT OR IGNORE INTO inbox_media(conversation_id,message_external_id,mime_type,data)
                               VALUES(?,?,?,?)''', (row['id'], mid, mime, data))
        unverified = con.execute('''SELECT COUNT(*) FROM inbox_messages m LEFT JOIN inbox_message_proofs p
                    ON p.conversation_id=m.conversation_id AND p.message_external_id=m.external_id
                    WHERE m.conversation_id=? AND p.message_external_id IS NULL''', (row['id'],)).fetchone()[0]
        if unverified:
            con.execute('UPDATE inbox_conversations SET history_complete=0 WHERE id=?', (row['id'],))
        return bool(complete and not unverified)


def _read_history(page, expected_id):
    events = []
    timestamps = {}

    def observe(response):
        u = urlparse(response.url)
        if (u.scheme, u.netloc, u.path) != ('https', 'edith.xiaohongshu.com', '/api/im/web/messages/history'):
            return
        if parse_qs(u.query).get('chat_user_id') != [expected_id]:
            return
        try:
            data = response.json().get('data') or {}
            result = data.get('result') or {}
            rows = data.get('out_message_list')
            limit = int(parse_qs(u.query).get('limit', ['30'])[0])
            if response.status != 200 or result.get('success') is not True or not isinstance(rows, list) or not 1 <= limit <= 100:
                events.append({'ok': False})
                return
            ids = set()
            ordered_ids = []
            for row in rows:
                raw_id = row.get('id')
                mid = str(raw_id) if isinstance(raw_id, (str, int)) and raw_id else ''
                if mid:
                    ids.add(mid)
                    ordered_ids.append(mid)
                    stamp = row.get('created_at')
                    if isinstance(stamp, int) and 1_000_000_000_000 <= stamp < 10_000_000_000_000:
                        timestamps[mid] = stamp
            events.append({'ok': True, 'ids': ids, 'ordered_ids': ordered_ids, 'count': len(rows), 'limit': limit,
                           'invalid_ids': len(ids) != len(rows), 'render_verified':False})
        except Exception:
            events.append({'ok': False})

    page.on('response', observe)
    return events, timestamps, observe


def _finish_history(page, events, timestamps, previous_complete, previous_pages, existing_ids, max_pages, expected_id=None, *, quick=False):
    deadline = monotonic() + 10
    while monotonic() < deadline and not events:
        page.wait_for_timeout(200)
    collected = {}
    complete = False
    # Repeated runs progressively go further if a conversation was too long to finish.
    page_budget = min(MAX_HISTORY_PAGES, max_pages if quick or not previous_pages else max(max_pages, previous_pages + max_pages))
    seen_count = 0
    for _ in range(page_budget):
        deadline = monotonic() + 5
        while monotonic() < deadline and len(events) <= seen_count:
            page.wait_for_timeout(150)
        if len(events) <= seen_count or not events[-1].get('ok'):
            break
        seen_count = len(events)
        page.wait_for_timeout(200)
        if expected_id is not None:
            u = urlparse(page.url)
            parts = u.path.split('/')
            if (u.scheme, u.netloc) != ('https', 'www.xiaohongshu.com') or len(parts) != 3 or parts[1] != 'chat' or unquote(parts[2]) != expected_id:
                raise ValueError('私信会话发生变化，已停止读取。')
        relevant = set().union(*(ev['ids'] for ev in events if ev.get('ok'))) if events else set()
        for event in events:
            if event.get('ok'):
                event['render_verified'] = False
        rows = _read_messages(page)
        if rows is None:
            break
        for event in events:
            if not event.get('ok') or 'ordered_ids' not in event or event.get('order_verified') is True:
                continue
            dom_ids = [row['external_id'] for row in rows if row['external_id'] in event['ids']]
            dom_set = set(dom_ids)
            api_ids = [mid for mid in event['ordered_ids'] if mid in dom_set]
            if dom_ids:
                event['order_verified'] = api_ids == list(reversed(dom_ids))
            elif event['count'] == 0:
                event['order_verified'] = True
        rendered = set()
        for row in rows:
            rendered.add(row['external_id'])
            if row['external_id'] and row['external_id'] in relevant:
                collected[row['external_id']] = row
        if (not relevant.issubset(collected) or '' in rendered or (rendered - relevant)
                or any(ev.get('invalid_ids') for ev in events if ev.get('ok'))):
            # Some non-rendered or unsupported platform records cannot be claimed as complete.
            missing = True
        else:
            missing = False
        last = events[-1]
        for event in events:
            if event.get('ok'):
                event['render_verified'] = not missing
        last['render_unmatched'] = sorted(rendered - relevant)
        exhausted = last['count'] < last['limit']
        if exhausted or ((quick or previous_complete) and relevant.intersection(existing_ids)):
            complete = not missing and (exhausted or previous_complete)
            break
        scroller = page.locator('.xhs-im-msg-list').first
        if not scroller.count():
            break
        scroller.evaluate('(e) => {e.scrollTop=0; e.dispatchEvent(new Event("scroll"));}')
    rows = list(collected.values())
    rows.sort(key=lambda m: (timestamps.get(m['external_id'], 0), m.get('store_id', '')))
    return rows, complete, sum(1 for e in events if e.get('ok'))


def _record_account(alias, state, detail, count, *, identity=None, mode=None, elapsed_ms=None, wait_ms=0, opened=0, reused=0):
    with db() as con:
        ensure_schema(con)
        con.execute('''INSERT INTO inbox_accounts(alias,state,detail,conversation_count) VALUES(?,?,?,?)
                       ON CONFLICT(alias) DO UPDATE SET state=excluded.state, detail=excluded.detail,
                       conversation_count=excluded.conversation_count, synced_at=CURRENT_TIMESTAMP''',
                    (alias, state, detail, count))
        if identity is not None and mode is not None and elapsed_ms is not None:
            con.execute('''INSERT INTO inbox_sync_metrics(alias,identity_hash,mode,elapsed_ms,wait_ms,opened,reused)
                           VALUES(?,?,?,?,?,?,?) ON CONFLICT(alias) DO UPDATE SET identity_hash=excluded.identity_hash,
                           mode=excluded.mode,elapsed_ms=excluded.elapsed_ms,wait_ms=excluded.wait_ms,
                           opened=excluded.opened,reused=excluded.reused,recorded_at=CURRENT_TIMESTAMP''',
                        (alias,identity,mode,elapsed_ms,wait_ms,opened,reused))


def _account_checkpoint(alias, identity):
    with db() as con:
        ensure_schema(con)
        row = con.execute('''SELECT list_sync_ms FROM inbox_account_checkpoints
                             WHERE alias=? AND identity_hash=?''', (alias, identity)).fetchone()
        return row['list_sync_ms'] if row else None


def _advance_account_checkpoint(alias, identity, timestamp):
    with db() as con:
        ensure_schema(con)
        if current_identity(con, alias) != identity:
            raise IdentityMismatch('账号身份发生变化，已停止保存。')
        con.execute('''INSERT INTO inbox_account_checkpoints(alias,identity_hash,list_sync_ms) VALUES(?,?,?)
                       ON CONFLICT(alias,identity_hash) DO UPDATE SET
                       list_sync_ms=MAX(inbox_account_checkpoints.list_sync_ms,excluded.list_sync_ms)''',
                    (alias, identity, timestamp))


def sync_account(alias, *, max_conversations=None, max_pages=HISTORY_PAGES_PER_RUN, progress=None,
                 force_full_history=False, mode='quick', cancel_requested=None, on_queue=None):
    from playwright.sync_api import Error as BrowserError
    started = monotonic()
    browser_started = None
    if max_conversations is not None and (not isinstance(max_conversations, int) or max_conversations < 1):
        raise ValueError('会话数量无效。')
    if not isinstance(max_pages, int) or not 1 <= max_pages <= MAX_HISTORY_PAGES:
        raise ValueError('历史记录页数无效。')
    if mode not in ('quick', 'full'):
        raise ValueError('同步方式无效。')
    account = get_account(alias)
    if account['platform'] != 'xiaohongshu':
        raise ValueError('私信同步目前只支持小红书账号。')
    with db() as con:
        ensure_schema(con)
        identity = current_identity(con, alias)
    account_cutoff = _account_checkpoint(alias, identity)
    count = 0
    opened = 0
    reused = 0
    partial = 0
    errors = 0
    incomplete_windows = 0
    reported_total = []
    list_metadata = {}
    def queue_state(state, detail):
        nonlocal browser_started
        if state == 'running' and browser_started is None:
            browser_started = monotonic()
        if on_queue:
            on_queue(state,detail)
    def finish(state, detail):
        now = monotonic()
        elapsed_ms = max(0,round((now-started)*1000))
        wait_ms = max(0,round(((browser_started if browser_started is not None else now)-started)*1000))
        _record_account(alias,state,detail,count,identity=identity,mode=mode,elapsed_ms=elapsed_ms,
                        wait_ms=wait_ms,opened=opened,reused=reused)
        return {'alias':alias,'state':state,'conversations':count,'partial':partial,'errors':errors,
                'elapsed_ms':elapsed_ms,'wait_ms':wait_ms,'opened':opened,'reused':reused}
    try:
        with queued_browser('sync', cancel_requested=cancel_requested, on_state=queue_state), creator_page(account) as page:
            observed = observe_xiaohongshu_identity(page)
            page.goto(PAGES['xiaohongshu'], wait_until='domcontentloaded', timeout=30000)
            verify_origin(page, 'xiaohongshu')
            check_challenge(page)
            require_xiaohongshu_identity(page, observed, identity)

            def capture_total(response):
                u = urlparse(response.url)
                if (u.scheme,u.netloc,u.path) != ('https','edith.xiaohongshu.com','/api/im/web/v3/chats'):
                    return
                try:
                    data = response.json().get('data') or {}
                    if response.status == 200 and isinstance(data.get('total'), int):
                        reported_total.append(data['total'])
                    for entry in data.get('chats', []):
                        cid, stamp = entry.get('chat_user_id'), entry.get('last_msg_time')
                        if (isinstance(cid, str) and cid and isinstance(stamp, int)
                                and 1_000_000_000_000 <= stamp < 10_000_000_000_000):
                            list_metadata[cid] = {'_last_timestamp':stamp,'_max_store_id':entry.get('max_store_id'),
                                                  '_list_update_time':entry.get('update_time')}
                except Exception:
                    pass

            page.on('response', capture_total)
            list_started_ms = int(time.time() * 1000)
            page.goto(CHAT_URL, wait_until='domcontentloaded', timeout=30000)
            _wait_for_list(page)
            verify_web_identity(page, identity)
            items = _gather_conversations(page)
            verify_web_identity(page, identity)
            for item in items:
                if item['external_id'] in list_metadata:
                    item.update(list_metadata[item['external_id']])
            complete_list = bool(reported_total) and len(items) == reported_total[-1]
            if max_conversations is not None:
                items = items[-max_conversations:]
                complete_list = False
            current_location = 'main'
            for index, item in enumerate(items):
                if cancel_requested and cancel_requested():
                    raise QueueCancelled('同步已取消，已读取的会话保留在本机。')
                if progress:
                    progress(f'{alias}：正在同步第 {index + 1} / {len(items)} 个会话。')
                if mode == 'quick' and not force_full_history:
                    cached = _reuse_unchanged_conversation(alias,identity,item)
                    if cached is not None:
                        count += 1
                        reused += 1
                        partial += not cached['history_complete']
                        continue
                opened += 1
                previous_complete, previous_pages, existing_ids = _existing_history(alias, identity, item['external_id'])
                if force_full_history or mode == 'full':
                    previous_complete = False
                events, timestamps, observer = _read_history(page, item['external_id'])
                try:
                    current_location = _select_conversation(page, item, current_location)
                    messages, complete, pages = _finish_history(page, events, timestamps, previous_complete,
                                                                  previous_pages, existing_ids, max_pages, item['external_id'],
                                                                  quick=mode == 'quick' and not force_full_history)
                    verify_web_identity(page, identity)
                    if not events or not any(e.get('ok') for e in events):
                        raise ValueError('历史消息未能加载。')
                    media = _fetch_media(page, alias, identity, item, messages)
                    complete = _save_conversation(alias, identity, item, messages, timestamps, complete, pages, media,
                                                  observations=events, account_cutoff_ms=account_cutoff)
                    if (not _observations_complete(events, {m.get('external_id') for m in messages}) or
                            not _coverage_complete(events,existing_ids,complete) or
                            _has_unresolved_review(alias,identity,item['external_id'])):
                        incomplete_windows += 1
                    count += 1
                    partial += not complete
                except IdentityMismatch:
                    raise
                except LoginNeeded:
                    raise
                except (ValueError, BrowserError):
                    # List metadata has already been read under the verified account.
                    # Keep the conversation visible even if its history fails to load.
                    verify_web_identity(page, identity)
                    _save_conversation(alias, identity, item, [], {}, False, 0, account_cutoff_ms=account_cutoff)
                    errors += 1
                finally:
                    page.remove_listener('response', observer)
            verify_web_identity(page, identity)
            if complete_list and not errors and not incomplete_windows:
                _advance_account_checkpoint(alias, identity, list_started_ms)
        state = 'complete' if complete_list and not partial and not errors else 'partial'
        detail = ('同步完成。' if state == 'complete' else '部分会话或历史记录未完整加载，可稍后再次同步。')
        return finish(state,detail) | {'list_complete':complete_list}
    except QueueCancelled:
        finish('partial', '同步已取消，已读取的会话保留在本机。')
        raise
    except IdentityMismatch:
        return finish('failed', '账号身份发生变化，已停止同步。')
    except LoginNeeded:
        return finish('login_needed', '请先重新登录并检查账号。')
    except Exception:
        return finish('failed', '同步未完成，请检查登录状态和浏览器是否正在使用。')


def sync_accounts(aliases, progress=None, *, mode='quick', cancel_requested=None, on_queue=None):
    if (not isinstance(aliases, list) or not 1 <= len(aliases) <= MAX_ACCOUNTS
            or any(not isinstance(alias, str) for alias in aliases) or len(set(aliases)) != len(aliases)):
        raise ValueError('请选择 1 到 10 个不重复的账号。')
    if mode not in ('quick', 'full'):
        raise ValueError('同步方式无效。')
    results = []
    identities = {}
    for alias in aliases:
        try:
            with db() as con:
                row = con.execute('SELECT identity_hash FROM account_identities WHERE alias=?', (alias,)).fetchone()
            if row:
                prior = identities.get(row['identity_hash'])
                if prior:
                    detail = f'与账号 {prior} 核验为同一小红书账号，请检查别名。'
                    _record_account(alias, 'failed', detail, 0)
                    results.append({'alias': alias, 'state': 'failed', 'detail': detail, 'conversations': 0})
                    continue
            result = sync_account(alias, progress=progress, mode=mode, cancel_requested=cancel_requested, on_queue=on_queue)
            results.append(result)
            if row and result.get('state') in {'complete', 'partial'}:
                identities[row['identity_hash']] = alias
        except QueueCancelled:
            raise
        except ValueError as exc:
            results.append({'alias': alias, 'state': 'failed', 'detail': str(exc), 'conversations': 0})
        except Exception:
            results.append({'alias': alias, 'state': 'failed', 'detail': '同步未完成。', 'conversations': 0})
    return {'accounts': results}


def inbox_accounts():
    with db() as con:
        ensure_schema(con)
        rows = con.execute('''SELECT a.alias, a.label, p.nickname,
                CASE WHEN i.identity_hash IS NULL THEN 0 ELSE 1 END AS identity_verified,
                s.state, s.detail, s.synced_at,
                m.mode AS last_sync_mode,m.elapsed_ms,m.wait_ms,m.opened,m.reused,
                (SELECT COUNT(*) FROM inbox_conversations c WHERE c.alias=a.alias
                 AND c.identity_hash=i.identity_hash) AS conversations
            FROM accounts a LEFT JOIN account_profiles p ON p.alias=a.alias
            LEFT JOIN account_identities i ON i.alias=a.alias
            LEFT JOIN inbox_accounts s ON s.alias=a.alias
            LEFT JOIN inbox_sync_metrics m ON m.alias=a.alias AND m.identity_hash=i.identity_hash
            WHERE a.platform='xiaohongshu' ORDER BY a.alias''')
        return {'accounts': [dict(row) for row in rows]}


def _conversation_key(identity_hash, external_id):
    value = ('xiaohongshu\0' + identity_hash + '\0' + external_id).encode('utf-8')
    return 'xhs1_' + hashlib.sha256(value).hexdigest()


def _message_key(conversation_key, external_id):
    return 'xhm1_' + hashlib.sha256((conversation_key+'\0'+external_id).encode('utf-8')).hexdigest()


def conversation_id_for_key(key):
    if not isinstance(key,str) or not re.fullmatch(r'xhs1_[0-9a-f]{64}',key):
        raise ValueError('会话标识无效。')
    with db() as con:
        ensure_schema(con)
        rows = con.execute('''SELECT c.id,c.identity_hash,c.external_id FROM inbox_conversations c
                JOIN account_identities i ON i.alias=c.alias AND i.identity_hash=c.identity_hash''').fetchall()
        matches = [row['id'] for row in rows if _conversation_key(row['identity_hash'],row['external_id']) == key]
        if len(matches) != 1:
            raise ValueError('会话不存在或账号身份已更换。')
        return matches[0]


def list_conversations(alias='', query='', status='all', limit=100, offset=0):
    if not isinstance(alias, str) or not isinstance(query, str) or len(query) > 200:
        raise ValueError('筛选条件无效。')
    if status not in ('all', 'pending', 'processed', 'important', 'new'):
        raise ValueError('处理状态无效。')
    if not isinstance(limit, int) or not 1 <= limit <= 200 or not isinstance(offset, int) or offset < 0:
        raise ValueError('分页参数无效。')
    where = ['i.identity_hash=c.identity_hash']
    params = []
    if alias:
        where.append('c.alias=?')
        params.append(alias)
    if status == 'important':
        where.append('c.important=1')
    elif status == 'new':
        where.append("EXISTS (SELECT 1 FROM inbox_alerts n WHERE n.conversation_id=c.id AND n.kind='new' AND n.acknowledged=0)")
    elif status != 'all':
        where.append('c.status=?')
        params.append(status)
    if query:
        term = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        where.append('''(c.display_name LIKE ? ESCAPE '\\' OR c.last_preview LIKE ? ESCAPE '\\'
                     OR EXISTS (SELECT 1 FROM inbox_messages m JOIN inbox_message_proofs p
                         ON p.conversation_id=m.conversation_id AND p.message_external_id=m.external_id
                         WHERE m.conversation_id=c.id AND m.body LIKE ? ESCAPE '\\'))''')
        params.extend([term, term, term])
    condition = ' AND '.join(where)
    base = ''' FROM inbox_conversations c JOIN account_identities i ON i.alias=c.alias
               JOIN accounts a ON a.alias=c.alias'''
    with db() as con:
        ensure_schema(con)
        total = con.execute('SELECT COUNT(*)' + base + ' WHERE ' + condition, params).fetchone()[0]
        rows = con.execute('''SELECT c.id,c.alias,c.identity_hash,c.external_id,a.label AS account_label,c.display_name,c.location,
                     c.last_preview,c.last_time_label,c.last_timestamp_ms,c.status,c.important,
                     c.history_complete,c.history_pages,c.synced_at,
                     (SELECT COUNT(*) FROM inbox_alerts n WHERE n.conversation_id=c.id
                        AND n.kind='new' AND n.acknowledged=0) AS new_count,
                     EXISTS(SELECT 1 FROM inbox_alerts n WHERE n.conversation_id=c.id
                        AND n.kind='review' AND n.acknowledged=0) AS review_needed,
                     (SELECT COUNT(*) FROM inbox_messages m JOIN inbox_message_proofs p
                         ON p.conversation_id=m.conversation_id AND p.message_external_id=m.external_id
                         WHERE m.conversation_id=c.id) AS message_count'''
                     + base + ' WHERE ' + condition + ''' ORDER BY COALESCE(c.last_timestamp_ms,0) DESC,
                     c.synced_at DESC,c.id DESC LIMIT ? OFFSET ?''', (*params, limit, offset))
        conversations = []
        for row in rows:
            entry = dict(row)
            entry['conversation_key'] = _conversation_key(entry.pop('identity_hash'),entry.pop('external_id'))
            conversations.append(entry)
        return {'conversations': conversations, 'total': total, 'limit': limit, 'offset': offset}


def conversation(conversation_id, limit=200, offset=0):
    if not isinstance(conversation_id, int) or conversation_id <= 0:
        raise ValueError('会话不存在。')
    if not isinstance(limit, int) or not 1 <= limit <= 500 or not isinstance(offset, int) or offset < 0:
        raise ValueError('分页参数无效。')
    with db() as con:
        ensure_schema(con)
        row = con.execute('''SELECT c.id,c.alias,c.identity_hash,c.external_id,a.label AS account_label,c.display_name,c.location,
                     c.last_preview,c.last_time_label,c.last_timestamp_ms,c.status,c.important,
                     c.history_complete,c.history_pages,c.synced_at,
                     (SELECT COUNT(*) FROM inbox_alerts n WHERE n.conversation_id=c.id
                        AND n.kind='new' AND n.acknowledged=0) AS new_count,
                     EXISTS(SELECT 1 FROM inbox_alerts n WHERE n.conversation_id=c.id
                        AND n.kind='review' AND n.acknowledged=0) AS review_needed,
                     COALESCE((SELECT MAX(n.id) FROM inbox_alerts n WHERE n.conversation_id=c.id),0) AS alerts_version
                     FROM inbox_conversations c JOIN account_identities i
                       ON i.alias=c.alias AND i.identity_hash=c.identity_hash
                     JOIN accounts a ON a.alias=c.alias WHERE c.id=?''', (conversation_id,)).fetchone()
        if not row:
            raise ValueError('会话不存在或账号身份已更换。')
        total = con.execute('''SELECT COUNT(*) FROM inbox_messages m JOIN inbox_message_proofs p
                    ON p.conversation_id=m.conversation_id AND p.message_external_id=m.external_id
                    WHERE m.conversation_id=?''', (conversation_id,)).fetchone()[0]
        messages = con.execute('''SELECT m.external_id,m.direction,m.content_type,m.body,m.time_label,m.timestamp_ms,
                                         media.id AS media_id
                                  FROM inbox_messages m JOIN inbox_message_proofs p
                                    ON p.conversation_id=m.conversation_id AND p.message_external_id=m.external_id
                                  LEFT JOIN inbox_media media
                                    ON media.conversation_id=m.conversation_id AND media.message_external_id=m.external_id
                                  WHERE m.conversation_id=?
                                  ORDER BY CASE WHEN m.timestamp_ms IS NULL THEN 1 ELSE 0 END,
                                           m.timestamp_ms,m.position,m.external_id LIMIT ? OFFSET ?''',
                               (conversation_id, limit, offset))
        info = dict(row)
        stable_key = _conversation_key(info.pop('identity_hash'),info.pop('external_id'))
        info['conversation_key'] = stable_key
        message_rows = [dict(r) for r in messages]
        page_ids = set()
        for message in message_rows:
            external_id = message.pop('external_id')
            page_ids.add(external_id)
            message['message_id'] = _message_key(stable_key,external_id)
            message['platform_message_id'] = external_id
            message['id_verified'] = True
            message['content_complete'] = None if message['content_type'] == '1' else False
        alerts = list(con.execute('''SELECT id,kind,message_external_id FROM inbox_alerts
                      WHERE conversation_id=? AND acknowledged=0''', (conversation_id,)))
        return {'conversation': info, 'messages': message_rows,
                'unseen_notifications': [a['id'] for a in alerts
                                         if a['kind'] == 'new' and a['message_external_id'] in page_ids],
                'review_alerts': [a['id'] for a in alerts if a['kind'] == 'review'][:500],
                'total': total, 'limit': limit, 'offset': offset}


def media_file(media_id):
    if not isinstance(media_id, int) or media_id <= 0:
        raise ValueError('图片不存在。')
    with db() as con:
        ensure_schema(con)
        row = con.execute('''SELECT m.mime_type,m.data FROM inbox_media m
                JOIN inbox_conversations c ON c.id=m.conversation_id
                JOIN account_identities i ON i.alias=c.alias AND i.identity_hash=c.identity_hash
                JOIN inbox_message_proofs p ON p.conversation_id=m.conversation_id AND p.message_external_id=m.message_external_id
                WHERE m.id=?''', (media_id,)).fetchone()
        if not row:
            raise ValueError('图片不存在或账号身份已更换。')
        return row['mime_type'], row['data']


def update_conversation(conversation_id, *, status=None, important=None, alerts_version=0):
    if status is None and important is None:
        raise ValueError('请选择要更新的状态。')
    if status is not None and status not in ('pending', 'processed'):
        raise ValueError('处理状态无效。')
    if important is not None and not isinstance(important, bool):
        raise ValueError('重要客户标记无效。')
    if type(alerts_version) is not int or alerts_version < 0:
        raise ValueError('提醒版本无效，请刷新会话。')
    with db() as con:
        ensure_schema(con)
        con.execute('BEGIN IMMEDIATE')
        visible = con.execute('''SELECT c.id FROM inbox_conversations c JOIN account_identities i
             ON i.alias=c.alias AND i.identity_hash=c.identity_hash WHERE c.id=?''', (conversation_id,)).fetchone()
        if not visible:
            raise ValueError('会话不存在或账号身份已更换。')
        if status is not None:
            if status == 'processed' and con.execute('''SELECT 1 FROM inbox_alerts
                    WHERE conversation_id=? AND acknowledged=0 AND id>? LIMIT 1''',
                    (conversation_id, alerts_version)).fetchone():
                raise ValueError('会话有新的提醒，请刷新并查看后再标记已处理。')
            con.execute('UPDATE inbox_conversations SET status=? WHERE id=?', (status, conversation_id))
            if status == 'processed':
                con.execute('UPDATE inbox_alerts SET acknowledged=1 WHERE conversation_id=?', (conversation_id,))
        if important is not None:
            con.execute('UPDATE inbox_conversations SET important=? WHERE id=?', (int(important), conversation_id))
    return {'updated': True}


def acknowledge_alerts(conversation_id, alert_ids):
    if (not isinstance(alert_ids, list) or len(alert_ids) > 500 or
            any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in alert_ids)):
        raise ValueError('提醒信息无效。')
    with db() as con:
        ensure_schema(con)
        visible = con.execute('''SELECT c.id FROM inbox_conversations c JOIN account_identities i
             ON i.alias=c.alias AND i.identity_hash=c.identity_hash WHERE c.id=?''', (conversation_id,)).fetchone()
        if not visible:
            raise ValueError('会话不存在或账号身份已更换。')
        count = 0
        for alert_id in set(alert_ids):
            count += con.execute('''UPDATE inbox_alerts SET acknowledged=1
                                    WHERE id=? AND conversation_id=? AND acknowledged=0''',
                                 (alert_id, conversation_id)).rowcount
    return {'acknowledged': count}


def inbox_summary():
    with db() as con:
        ensure_schema(con)
        row = con.execute('''SELECT
            SUM(CASE WHEN n.kind='new' THEN 1 ELSE 0 END) AS new_count,
            COUNT(DISTINCT CASE WHEN n.kind='review' THEN n.conversation_id END) AS review_count,
            MAX(CASE WHEN n.kind='new' THEN n.id END) AS latest_new_id,
            MAX(CASE WHEN n.kind='review' THEN n.id END) AS latest_review_id
            FROM inbox_alerts n JOIN inbox_conversations c ON c.id=n.conversation_id
            JOIN account_identities i ON i.alias=c.alias AND i.identity_hash=c.identity_hash
            WHERE n.acknowledged=0''').fetchone()
        return {'new_count': row['new_count'] or 0, 'review_count': row['review_count'] or 0,
                'latest_new_id': row['latest_new_id'] or 0, 'latest_review_id': row['latest_review_id'] or 0}


def open_conversation(conversation_id, *, until_closed=True, cancel_requested=None, on_queue=None):
    from playwright.sync_api import Error, TimeoutError as BrowserTimeout
    from urllib.parse import quote
    with db() as con:
        ensure_schema(con)
        row = con.execute('''SELECT c.alias,c.identity_hash,c.external_id FROM inbox_conversations c
                JOIN account_identities i ON i.alias=c.alias AND i.identity_hash=c.identity_hash
                WHERE c.id=?''', (conversation_id,)).fetchone()
        if not row:
            raise ValueError('会话不存在或账号身份已更换。')
        saved = dict(row)
    if not saved['external_id'] or re.search(r'[\x00-\x1f/]', saved['external_id']):
        raise ValueError('会话地址无效。')
    account = get_account(saved['alias'])
    with queued_browser('open', cancel_requested=cancel_requested, on_state=on_queue), creator_page(account, headed=True) as page:
        observed = observe_xiaohongshu_identity(page)
        page.goto(PAGES['xiaohongshu'], wait_until='domcontentloaded', timeout=30000)
        verify_origin(page, 'xiaohongshu')
        check_challenge(page)
        require_xiaohongshu_identity(page, observed, saved['identity_hash'])
        page.goto(CHAT_URL + '/' + quote(saved['external_id'], safe=''), wait_until='domcontentloaded', timeout=30000)
        _wait_for_list(page)
        verify_web_identity(page, saved['identity_hash'])
        actual = urlparse(page.url)
        parts = actual.path.split('/')
        if (actual.scheme, actual.netloc) != ('https', 'www.xiaohongshu.com') or len(parts) != 3 or parts[1] != 'chat' or unquote(parts[2]) != saved['external_id']:
            raise ValueError('无法确认原会话地址，请在小红书中手动查找。')
        if until_closed:
            while not page.is_closed():
                if cancel_requested and cancel_requested():
                    raise QueueCancelled('原会话窗口已关闭。')
                try:
                    page.wait_for_event('close', timeout=1000)
                except BrowserTimeout:
                    continue
                except Error:
                    if not page.is_closed():
                        raise
    return {'closed': True}
