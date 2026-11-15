import asyncio
import hashlib
import re
import secrets
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from browser import account_session
from publisher import acknowledge_verification, add_account, batch_status, browser_lock, cancel_verification, data_root, db, get_account, prepare_batch_data, run_batch
from xiaohongshu import BYTE_LIMIT, IMAGE_FORMATS, IMAGE_LIMIT, image_info
import inbox
import app_config
import database_backup
import restore_guard
from browser_queue import QueueCancelled, queue_status


ROOT = Path(__file__).resolve().parent
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount('/static', StaticFiles(directory=ROOT / 'web', check_dir=False), name='static')
session_key = secrets.token_urlsafe(32)
jobs = {}
jobs_lock = threading.Lock()
maintenance_lock = asyncio.Lock()
_scheduler_gate = threading.RLock()
_scheduler_stop = threading.Event()
_scheduler_thread = None
_scheduler_tokens = set()


@app.middleware('http')
async def local_only(request, call_next):
    host = request.headers.get('host', '')
    if not re.fullmatch(r'(?:127\.0\.0\.1|localhost):[0-9]{1,5}', host):
        return JSONResponse({'error': '无效的本机访问地址。'}, status_code=403)
    if request.method not in {'GET', 'HEAD'} or request.url.path.startswith(('/api/inbox', '/api/settings', '/api/backups', '/api/browser-queue', '/api/restore-guard')):
        origin = request.headers.get('origin', '')
        cookie_name = 'workbench_' + host.rsplit(':', 1)[1]
        if (request.cookies.get(cookie_name) != session_key or
                (request.method not in {'GET', 'HEAD'} and origin != f'http://{host}') or
                (origin and origin != f'http://{host}')):
            return JSONResponse({'error': '请从本机工作台操作。'}, status_code=403)
    if request.method not in {'GET', 'HEAD'}:
        async with maintenance_lock:
            return await call_next(request)
    return await call_next(request)


@app.exception_handler(ValueError)
async def invalid_request(request, exc):
    return JSONResponse({'error': str(exc)}, status_code=400)


@app.get('/')
def index(request: Request):
    theme = app_config.settings()['theme']
    html = (ROOT / 'web' / 'index.html').read_text(encoding='utf-8')
    if theme:
        html = html.replace('<html lang="zh-CN" data-theme="light">',
                            f'<html lang="zh-CN" data-theme="{theme}" data-theme-source="saved">', 1)
    response = HTMLResponse(html, headers={'Cache-Control': 'no-store'})
    response.set_cookie('workbench_' + request.headers['host'].rsplit(':', 1)[1],
                        session_key, httponly=True, samesite='strict')
    return response


@app.get('/api/settings')
def get_settings():
    return app_config.settings()


@app.post('/api/settings/theme')
async def update_theme(request: Request):
    data = await request.json()
    return app_config.set_theme(data.get('theme') if isinstance(data, dict) else None)


@app.post('/api/settings/schedule')
async def update_schedule(request: Request):
    data = await request.json()
    return app_config.set_interval(data.get('minutes') if isinstance(data, dict) else None)


@app.get('/api/backups')
def backups():
    return database_backup.list_backups()


@app.post('/api/backups')
def create_backup():
    return database_backup.create_backup()


@app.post('/api/backups/restore')
async def restore_backup(request: Request):
    data = await request.json()
    if not isinstance(data, dict) or data.get('confirm') != data.get('name'):
        raise ValueError('请确认要恢复的备份。')
    return await asyncio.to_thread(_restore_guarded, data.get('name'))


@app.get('/api/restore-guard')
def restore_review_status():
    return restore_guard.status()


@app.post('/api/restore-guard/audit')
async def restore_review_audit(request: Request):
    data = await request.json()
    alias = data.get('alias') if isinstance(data,dict) else None
    generation = data.get('generation') if isinstance(data,dict) else None
    if not isinstance(alias,str) or not isinstance(generation,str):
        raise ValueError('请选择要核对的账号。')
    from platform_sync import audit_restored_account
    return start_job(lambda update, acknowledge, cancelled, on_queue: audit_restored_account(
        alias, generation, cancel_requested=cancelled, on_queue=on_queue), queued=True, kind='platform')


@app.post('/api/restore-guard/manual')
async def restore_review_manual(request: Request):
    data = await request.json()
    if not isinstance(data,dict) or data.get('confirm') != 'reviewed':
        raise ValueError('请先在对应平台核对作品。')
    return restore_guard.record_manual_audit(data.get('generation'),data.get('alias'))


@app.post('/api/restore-guard/confirm')
async def restore_review_confirm(request: Request):
    data = await request.json()
    if not isinstance(data,dict) or data.get('confirm') != 'reviewed':
        raise ValueError('请先核对平台作品并确认。')
    return restore_guard.confirm_review(data.get('generation'))


def _restore_guarded(name):
    with _scheduler_gate:
        with jobs_lock:
            if any(item['state'] in ('waiting', 'running') for item in jobs.values()):
                raise ValueError('请先等待或取消工作台任务，再恢复备份。')
        if app_config.settings()['schedule']['state'] == 'running':
            raise ValueError('自动同步正在结束，请稍后再恢复备份。')
        return database_backup.restore_backup(name)


@app.get('/api/accounts')
def accounts():
    with db() as con:
        rows = con.execute("""
            SELECT accounts.alias, accounts.platform, accounts.label, account_profiles.nickname,
                   COALESCE(account_identities.checked_at, account_profiles.checked_at) AS checked_at,
                   COALESCE(account_notes.note, '') AS note,
                   account_identities.identity_hash IS NOT NULL AS identity_verified
            FROM accounts LEFT JOIN account_profiles USING(alias)
            LEFT JOIN account_identities USING(alias)
            LEFT JOIN account_notes USING(alias)
            WHERE accounts.platform='xiaohongshu' ORDER BY accounts.alias
        """)
        return {'accounts': [dict(row) for row in rows]}


@app.get('/api/inbox/accounts')
def message_accounts():
    return inbox.inbox_accounts()


@app.get('/api/inbox/summary')
def message_summary():
    return inbox.inbox_summary()


@app.get('/api/inbox/conversations')
def message_conversations(alias: str = '', query: str = '', status: str = 'all', limit: int = 100, offset: int = 0):
    return inbox.list_conversations(alias=alias, query=query, status=status, limit=limit, offset=offset)


@app.get('/api/inbox/conversation-selection')
def message_conversation_selection(alias: str = '', query: str = '', status: str = 'all'):
    return inbox.conversation_selection(alias=alias,query=query,status=status)


@app.get('/api/inbox/conversations/{identifier}')
def message_conversation(identifier: int, limit: int = 200, offset: int = 0):
    return inbox.conversation(identifier, limit=limit, offset=offset)


@app.get('/api/inbox/conversations/{identifier}/message-selection')
def message_batch_selection(identifier: int):
    return inbox.message_selection(identifier)


@app.get('/api/inbox/media/{identifier}')
def message_media(identifier: int):
    mime, content = inbox.media_file(identifier)
    return Response(content=content, media_type=mime,
                    headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@app.post('/api/inbox/conversations/{identifier}/state')
async def message_state(identifier: int, request: Request):
    data = await request.json()
    if not isinstance(data, dict) or set(data) - {'status', 'important', 'alerts_version'}:
        raise ValueError('处理状态无效。')
    return inbox.update_conversation(identifier, status=data.get('status'), important=data.get('important'),
                                     alerts_version=data.get('alerts_version', 0))


@app.post('/api/inbox/conversations/{identifier}/acknowledge')
async def message_acknowledge(identifier: int, request: Request):
    data = await request.json()
    if not isinstance(data, dict) or set(data) != {'alerts'}:
        raise ValueError('提醒信息无效。')
    return inbox.acknowledge_alerts(identifier, data['alerts'])


@app.post('/api/inbox/conversations/{identifier}/delete-message')
async def message_delete(identifier: int, request: Request):
    data = await request.json()
    if (not isinstance(data, dict) or set(data) != {'confirm','message_id','platform_message_id'} or
            data['confirm'] != 'delete'):
        raise ValueError('请确认删除这条本地消息。')
    with browser_lock():
        return inbox.delete_message(identifier,data['message_id'],data['platform_message_id'])


@app.post('/api/inbox/conversations/{identifier}/delete-messages')
async def message_batch_delete(identifier: int, request: Request):
    data = await request.json()
    if not isinstance(data,dict) or set(data) != {'confirm','messages'} or data['confirm'] != 'delete':
        raise ValueError('请确认批量删除本地消息。')
    with browser_lock():
        return inbox.delete_messages(identifier,data['messages'])


@app.post('/api/inbox/delete-conversations')
async def conversation_batch_delete(request: Request):
    data = await request.json()
    if not isinstance(data,dict) or set(data) != {'confirm','conversations'} or data['confirm'] != 'delete':
        raise ValueError('请确认批量删除本地会话。')
    with browser_lock():
        return inbox.delete_conversations(data['conversations'])


@app.post('/api/inbox/conversations/{identifier}/delete')
async def conversation_delete(identifier: int, request: Request):
    data = await request.json()
    if (not isinstance(data, dict) or set(data) != {'confirm','messages_version','alerts_version'} or
            data['confirm'] != 'delete'):
        raise ValueError('请确认删除本地会话。')
    with browser_lock():
        return inbox.delete_conversation(identifier,messages_version=data['messages_version'],
                                         alerts_version=data['alerts_version'])


@app.post('/api/inbox/conversations/{identifier}/open')
def message_open(identifier: int):
    return start_job(lambda update, acknowledge, cancelled, on_queue: inbox.open_conversation(
        identifier, cancel_requested=cancelled, on_queue=on_queue), queued=True, kind='open', cancellable=True)


@app.post('/api/inbox/sync')
async def message_sync(request: Request):
    data = await request.json()
    aliases = data.get('accounts') if isinstance(data, dict) else None
    mode = data.get('mode', 'quick') if isinstance(data, dict) else None
    if (not isinstance(aliases, list) or not 1 <= len(aliases) <= inbox.MAX_ACCOUNTS or
            any(not isinstance(alias, str) for alias in aliases) or len(set(aliases)) != len(aliases)):
        raise ValueError('请选择 1 到 10 个不重复的账号。')
    if mode not in ('quick', 'full'):
        raise ValueError('同步方式无效。')
    return start_sync_job(aliases,mode)


@app.post('/api/accounts')
async def register(request: Request):
    data = await request.json()
    return add_account(data.get('alias', ''), 'xiaohongshu', data.get('label', ''))


@app.post('/api/accounts/{alias}/note')
async def account_note(alias: str, request: Request):
    data = await request.json()
    note = data.get('note') if isinstance(data, dict) else None
    if not isinstance(note, str) or len(note.strip()) > 120:
        raise ValueError('账号备注须为不超过 120 字的文字。')
    if get_account(alias)['platform'] != 'xiaohongshu':
        raise ValueError('请选择小红书账号。')
    with db() as con:
        con.execute('''INSERT INTO account_notes(alias, note) VALUES (?, ?)
                       ON CONFLICT(alias) DO UPDATE SET note=excluded.note''', (alias, note.strip()))
    return {'alias': alias, 'note': note.strip()}


def start_job(fn, batch=None, *, cancellable=False, queued=False, kind='', on_finish=None, dedupe_key=None):
    identifier = uuid.uuid4().hex
    acknowledged = threading.Event()
    cancellation = threading.Event()
    with jobs_lock:
        if dedupe_key is not None:
            for existing_id, item in jobs.items():
                if (item.get('_dedupe_key') == dedupe_key and item['state'] == 'waiting'
                        and not item['_cancellation'].is_set()):
                    if on_finish:
                        item['_finish_handlers'].append(on_finish)
                    return {'job':existing_id,'merged':True}
        jobs[identifier] = {'state': 'waiting' if queued else 'running', 'detail': '', 'verification': False,
                            'batch': batch, 'cancellable': cancellable or queued, 'kind': kind,
                            '_acknowledged': acknowledged, '_cancellation': cancellation,
                            '_dedupe_key':dedupe_key, '_finish_handlers':[on_finish] if on_finish else []}

    def update(detail):
        with jobs_lock:
            jobs[identifier]['detail'] = detail or ''
            if batch is not None:
                jobs[identifier]['verification'] = bool(detail)
                acknowledged.clear()

    def queue_update(state, detail):
        with jobs_lock:
            jobs[identifier].update(state=state, detail=detail, verification=False)

    def take_acknowledgement():
        with jobs_lock:
            result = acknowledged.is_set()
            acknowledged.clear()
            return result

    def work():
        try:
            result = (fn(update, take_acknowledgement, cancellation.is_set, queue_update) if queued else
                      fn(update, take_acknowledgement, cancellation.is_set) if cancellable else
                      fn(update, take_acknowledgement))
        except QueueCancelled as exc:
            with jobs_lock:
                jobs[identifier].update(state='cancelled', detail=str(exc), verification=False)
        except Exception as exc:
            with jobs_lock:
                jobs[identifier].update(state='failed', error=str(exc), verification=False)
        else:
            with jobs_lock:
                jobs[identifier].update(state='done', result=result, detail='', verification=False)
        finally:
            with jobs_lock:
                finished = {key: value for key, value in jobs[identifier].items() if not key.startswith('_')}
                handlers = jobs[identifier]['_finish_handlers'][:]
                jobs[identifier]['_finish_handlers'].clear()
            for handler in handlers:
                try:
                    handler(finished)
                except Exception:
                    pass

    threading.Thread(target=work, daemon=True).start()
    return {'job': identifier}


def start_sync_job(aliases, mode='quick', on_finish=None):
    key = ('sync',tuple(sorted(aliases)),mode)
    return start_job(lambda update, acknowledge, cancelled, on_queue: inbox.sync_accounts(
        aliases,progress=update,mode=mode,cancel_requested=cancelled,on_queue=on_queue),
        queued=True,kind='sync',cancellable=True,on_finish=on_finish,dedupe_key=key)


def _scheduler_tick():
    with _scheduler_gate:
        _scheduler_tick_locked()


def _scheduler_tick_locked():
    app_config.recover_orphaned_run(_scheduler_tokens)
    claim = app_config.claim_due_sync()
    if not claim:
        return
    _scheduler_tokens.add(claim['token'])
    def finished(job):
        with _scheduler_gate:
            try:
                if job['state'] == 'cancelled':
                    app_config.finish_sync(claim['token'], 'cancelled', '上次自动同步已取消。')
                elif job['state'] != 'done':
                    app_config.finish_sync(claim['token'], 'attention', '自动同步未完成，请检查账号状态。')
                else:
                    results = job.get('result', {}).get('accounts', [])
                    attention = any(item.get('state') != 'complete' for item in results)
                    app_config.finish_sync(claim['token'], 'attention' if attention else 'complete',
                                           '部分账号或历史需要核对。' if attention else '自动同步完成。')
            finally:
                _scheduler_tokens.discard(claim['token'])
    try:
        start_sync_job(claim['aliases'],mode='quick',on_finish=finished)
    except Exception:
        try:
            app_config.finish_sync(claim['token'], 'attention', '自动同步未能启动。')
        finally:
            _scheduler_tokens.discard(claim['token'])
        raise


@app.on_event('startup')
def start_scheduler():
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    def run():
        while not _scheduler_stop.wait(5):
            try:
                _scheduler_tick()
            except Exception:
                # Keep the workbench alive; the next tick can recover a stale lease.
                pass
    _scheduler_thread = threading.Thread(target=run, name='inbox-scheduler', daemon=True)
    _scheduler_thread.start()


@app.on_event('shutdown')
def stop_scheduler():
    _scheduler_stop.set()


@app.get('/api/jobs/{identifier}')
def job(identifier: str):
    with jobs_lock:
        if identifier not in jobs:
            raise ValueError('任务不存在，可能是工作台已重启。')
        return {key: value for key, value in jobs[identifier].items() if not key.startswith('_')}


@app.get('/api/browser-queue')
def browser_tasks():
    with jobs_lock:
        active = [{key: value for key, value in item.items() if not key.startswith('_')} | {'id': identifier}
                  for identifier, item in jobs.items() if item['state'] in ('waiting', 'running')]
    return {'jobs': active, 'queue': queue_status()['tasks']}


@app.post('/api/jobs/{identifier}/cancel')
def cancel_job(identifier: str):
    with jobs_lock:
        item = jobs.get(identifier)
        if (not item or item['state'] not in ('waiting', 'running') or not item['cancellable'] or
                (item['state'] == 'running' and item['kind'] not in ('sync', 'open', 'login'))):
            raise ValueError('任务正在执行关键步骤，无法在此处取消。')
        item['_cancellation'].set()
    return {'accepted': True}


@app.post('/api/accounts/{alias}/{action}')
def account_action(alias: str, action: str):
    account = get_account(alias)
    if account['platform'] != 'xiaohongshu' or action not in {'login', 'check'}:
        raise ValueError('账号或操作无效。')
    if action == 'login':
        return start_job(lambda update, acknowledge, cancelled, on_queue: account_session(
            alias, login=True, cancel_requested=cancelled, on_queue=on_queue),
            queued=True, kind='login', cancellable=True)
    return start_job(lambda update, acknowledge, cancelled, on_queue: account_session(
        alias, cancel_requested=cancelled, on_queue=on_queue), queued=True, kind='check')


def media_path(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}\.(jpg|jpeg|png|webp)', identifier):
        raise ValueError('图片标识无效。')
    path = data_root() / 'media' / identifier
    if not path.is_file():
        raise ValueError('素材不存在，请重新上传。')
    return path


@app.post('/api/images')
async def upload(request: Request):
    form = await request.form(max_files=IMAGE_LIMIT, max_fields=IMAGE_LIMIT)
    files = [value for _, value in form.multi_items() if isinstance(value, UploadFile) or hasattr(value, 'filename')]
    if not 1 <= len(files) <= IMAGE_LIMIT:
        raise ValueError('每次请选择 1 至 18 张图片。')
    images = []
    folder = data_root() / 'media'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        for file in files:
            suffix = Path(file.filename or '').suffix.lower()
            if suffix not in IMAGE_FORMATS:
                raise ValueError('支持 JPG、PNG 和 WebP 图片。')
            content = await file.read(BYTE_LIMIT + 1)
            if not 0 < len(content) <= BYTE_LIMIT:
                raise ValueError('单张图片最大 32 MB。')
            import io
            info = await asyncio.to_thread(image_info, io.BytesIO(content), file.filename)
            identifier = hashlib.sha256(content).hexdigest() + suffix
            path = folder / identifier
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != identifier.split('.')[0]:
                temporary = folder / (uuid.uuid4().hex + '.tmp')
                try:
                    temporary.write_bytes(content)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            images.append({'id': identifier, 'name': Path(file.filename).name, 'url': '/api/media/' + identifier, **info})
    finally:
        await form.close()
    return {'images': images}


@app.get('/api/media/{identifier}')
def media(identifier: str):
    return FileResponse(media_path(identifier))


@app.post('/api/preview')
async def preview(request: Request):
    data = await request.json()
    if not isinstance(data, dict) or not isinstance(data.get('post'), dict) or not isinstance(data.get('targets'), list):
        raise ValueError('图文和账号选择无效。')
    targets = []
    for target in data['targets']:
        if isinstance(target, str):
            alias, override = target, {}
        elif isinstance(target, dict) and not target.keys() - {'account', 'post'}:
            alias, override = target.get('account'), target.get('post', {})
        else:
            raise ValueError('账号设置无效。')
        if not isinstance(alias, str) or get_account(alias)['platform'] != 'xiaohongshu':
            raise ValueError('请选择小红书账号。')
        if not isinstance(override, dict) or override.keys() - {'title', 'body', 'tags', 'schedule'}:
            raise ValueError('单账号只可单独设置标题、正文、话题和时间。')
        targets.append({'account': alias, 'post': override})
    post = dict(data['post'])
    images = post.get('images')
    if not isinstance(images, list):
        raise ValueError('请选择图片。')
    post['images'] = [str(media_path(identifier)) for identifier in images]
    return add_nicknames(prepare_batch_data({'post': post, 'targets': targets}, data_root()))


def add_nicknames(batch):
    with db() as con:
        names = {row['alias']: row['nickname'] for row in con.execute(
            'SELECT alias, nickname FROM account_profiles'
        )}
        identities = {row['alias']: row['nickname'] for row in con.execute(
            'SELECT alias, nickname FROM task_identities WHERE batch=?', (batch['batch'],)
        )}
    for task in batch['tasks']:
        alias = task['alias']
        task['identity_verified'] = alias in identities
        nickname = identities.get(alias, names.get(alias, ''))
        if nickname:
            task['nickname'] = nickname
    return batch


@app.get('/api/batches')
def batches():
    recover_interrupted()
    with db() as con:
        ids = [row['batch'] for row in con.execute("""
            SELECT tasks.batch, MAX(tasks.updated) AS last_update FROM tasks
            JOIN accounts USING(alias) WHERE accounts.platform='xiaohongshu'
            GROUP BY tasks.batch ORDER BY last_update DESC
        """)]
    result = []
    for identifier in ids:
        item = batch_status(identifier)
        item['tasks'] = [task for task in item['tasks'] if task['platform'] == 'xiaohongshu']
        result.append(add_nicknames(item))
    return {'batches': result}


@app.get('/api/batches/{identifier}')
def batch(identifier: str):
    recover_interrupted()
    result = batch_status(identifier, preview=True)
    result['tasks'] = [task for task in result['tasks'] if task['platform'] == 'xiaohongshu']
    if not result['tasks']:
        raise ValueError('未找到小红书批次。')
    return add_nicknames(result)


def recover_interrupted():
    try:
        with browser_lock(), db() as con:
            con.execute("""
                UPDATE tasks SET state='uncertain',
                detail='程序在任务执行中断开，请先到小红书笔记管理核对。', updated=CURRENT_TIMESTAMP
                WHERE state IN ('running', 'verifying')
                AND EXISTS (SELECT 1 FROM accounts WHERE accounts.alias=tasks.alias AND platform='xiaohongshu')
            """)
            con.execute("""
                DELETE FROM verification_signals WHERE NOT EXISTS (
                    SELECT 1 FROM tasks WHERE tasks.batch=verification_signals.batch
                    AND tasks.alias=verification_signals.alias AND tasks.state='verifying'
                )
            """)
            con.execute("""
                DELETE FROM verification_cancellations WHERE NOT EXISTS (
                    SELECT 1 FROM tasks WHERE tasks.batch=verification_cancellations.batch
                    AND tasks.alias=verification_cancellations.alias AND tasks.state='verifying'
                )
            """)
    except ValueError:
        # Another publisher owns the lock, so its tasks may still be running.
        pass


@app.get('/api/batches/{identifier}/images/{alias}/{index}')
def batch_image(identifier: str, alias: str, index: int):
    tasks = batch_status(identifier, preview=True)['tasks']
    task = next((t for t in tasks if t['alias'] == alias and t['platform'] == 'xiaohongshu'), None)
    if task is None or index < 0 or index >= len(task['post']['images']):
        raise ValueError('图片不存在。')
    path = Path(task['post']['images'][index]['path'])
    if not path.is_file():
        raise ValueError('图片不存在，请重新准备素材。')
    return FileResponse(path)


@app.post('/api/batches/{identifier}/publish')
async def publish(identifier: str, request: Request):
    data = await request.json()
    if data.get('confirm') != identifier:
        raise ValueError('请先确认预览的批次。')
    if any(t['platform'] != 'xiaohongshu' for t in batch_status(identifier)['tasks']):
        raise ValueError('工作台仅发布小红书批次。')
    retry = data.get('retry', False)
    if type(retry) is not bool:
        raise ValueError('重试参数无效。')
    allowed = data.get('allow_duplicate_accounts', [])
    if not isinstance(allowed, list) or any(not isinstance(alias, str) for alias in allowed):
        raise ValueError('重复发布确认须为账号列表。')
    return start_job(lambda update, acknowledge, cancelled, on_queue: run_batch(
        identifier, identifier, retry=retry,
        on_verification=lambda alias, detail: update(f'{alias} {detail}' if detail else ''),
        verification_acknowledged=acknowledge, allow_duplicate_accounts=allowed,
        cancel_requested=cancelled, on_queue=on_queue), batch=identifier, queued=True, kind='publish')


@app.post('/api/batches/{identifier}/continue')
def continue_verification(identifier: str):
    with jobs_lock:
        active = [item for item in jobs.values() if item['batch'] == identifier
                  and item['state'] == 'running' and item['verification']]
        if len(active) == 1:
            active[0]['_acknowledged'].set()
    try:
        acknowledge_verification(identifier)
    except ValueError:
        if len(active) != 1:
            raise
    return {'accepted': True}


@app.post('/api/batches/{identifier}/cancel-verification')
def stop_verification(identifier: str):
    return cancel_verification(identifier)


@app.post('/api/batches/{identifier}/sync')
async def sync_platform(identifier: str, request: Request):
    data = await request.json()
    alias = data.get('alias') if isinstance(data, dict) else None
    if not isinstance(alias, str):
        raise ValueError('请选择要核对的账号。')
    from platform_sync import synchronize
    return start_job(lambda update, acknowledge, cancelled, on_queue: synchronize(
        identifier, alias, cancel_requested=cancelled, on_queue=on_queue), queued=True, kind='platform')


@app.post('/api/batches/{identifier}/reconcile')
async def reconcile(identifier: str, request: Request):
    data = await request.json()
    alias, outcome = data.get('alias'), data.get('outcome')
    if outcome not in {'submitted', 'failed'} or not isinstance(alias, str):
        raise ValueError('请选择已提交或确认未提交。')
    detail = ('已在平台核对并确认提交。' if outcome == 'submitted'
              else '已在平台核对并确认未提交，可以重试。')
    with browser_lock(), db() as con:
        if outcome == 'failed' and con.execute('''SELECT 1 FROM platform_observations
                WHERE batch=? AND alias=?''', (identifier, alias)).fetchone():
            raise ValueError('平台已找到对应作品，不能标记为未提交或再次发送；请在小红书核对。')
        changed = con.execute("""
            UPDATE tasks SET state=?, detail=?, updated=CURRENT_TIMESTAMP
            WHERE batch=? AND alias=? AND state='uncertain'
            AND EXISTS (SELECT 1 FROM accounts WHERE accounts.alias=tasks.alias AND platform='xiaohongshu')
        """, (outcome, detail, identifier, alias))
        if changed.rowcount != 1:
            raise ValueError('只有待核对的任务可以记录核对结果。')
    return batch_status(identifier)
