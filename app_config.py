"""Small durable preferences and the optional local inbox scheduler."""
from __future__ import annotations

import os
import time
import uuid
from browser_queue import _alive, _process_birth
from publisher import db

INTERVALS = (0, 30, 60, 120, 360)


def _schema(con):
    con.execute('CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    con.execute('''CREATE TABLE IF NOT EXISTS inbox_schedule (
        id INTEGER PRIMARY KEY CHECK(id=1), interval_minutes INTEGER NOT NULL DEFAULT 0,
        next_run REAL NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'idle',
        detail TEXT NOT NULL DEFAULT '', pid INTEGER, birth TEXT, token TEXT,
        last_started REAL, last_finished REAL)''')
    con.execute('INSERT OR IGNORE INTO inbox_schedule(id) VALUES(1)')


def settings():
    with db() as con:
        _schema(con)
        theme = con.execute("SELECT value FROM app_settings WHERE key='theme'").fetchone()
        schedule = dict(con.execute('''SELECT interval_minutes,next_run,state,detail,last_started,last_finished
                                       FROM inbox_schedule WHERE id=1''').fetchone())
        return {'theme': theme['value'] if theme and theme['value'] in ('light', 'dark') else None,
                'schedule': schedule}


def set_theme(theme):
    if theme not in ('light', 'dark'):
        raise ValueError('主题设置无效。')
    with db() as con:
        _schema(con)
        con.execute("INSERT INTO app_settings(key,value) VALUES('theme',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (theme,))
    return {'theme': theme}


def set_interval(minutes):
    if type(minutes) is not int or minutes not in INTERVALS:
        raise ValueError('请选择支持的自动同步间隔。')
    now = time.time()
    with db() as con:
        _schema(con)
        con.execute('UPDATE inbox_schedule SET interval_minutes=?,next_run=? WHERE id=1',
                    (minutes, now + minutes * 60 if minutes else 0))
    return settings()['schedule']


def claim_due_sync(now=None):
    now = time.time() if now is None else now
    with db() as con:
        _schema(con)
        con.commit()
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM inbox_schedule WHERE id=1').fetchone()
        if row['state'] == 'running' and row['pid'] and _alive(row['pid'], row['birth']):
            return None
        if row['state'] == 'running':
            con.execute("UPDATE inbox_schedule SET state='interrupted',detail='上次自动同步中断，请检查账号状态。',pid=NULL,birth=NULL,token=NULL WHERE id=1")
        interval = row['interval_minutes']
        if interval not in INTERVALS or not interval or row['next_run'] > now:
            return None
        aliases = [r['alias'] for r in con.execute('''SELECT a.alias FROM accounts a JOIN account_identities i ON i.alias=a.alias
                                                   WHERE a.platform='xiaohongshu' ORDER BY a.alias LIMIT 11''')]
        if not aliases:
            con.execute("UPDATE inbox_schedule SET next_run=?,state='attention',detail='没有已核验的账号。' WHERE id=1",
                        (now + interval * 60,))
            return None
        if len(aliases) > 10:
            con.execute("UPDATE inbox_schedule SET next_run=?,state='attention',detail='已核验账号超过 10 个，请分组手动同步。' WHERE id=1",
                        (now + interval * 60,))
            return None
        token = uuid.uuid4().hex
        birth = _process_birth(os.getpid())
        con.execute('''UPDATE inbox_schedule SET next_run=?,state='running',detail='正在等待同步。',pid=?,birth=?,
                       token=?,last_started=? WHERE id=1''',
                    (now + interval * 60, os.getpid(), str(birth) if birth is not None else None, token, now))
        return {'token': token, 'aliases': aliases}


def finish_sync(token, state, detail):
    if state not in ('complete', 'attention', 'cancelled'):
        raise ValueError('自动同步状态无效。')
    with db() as con:
        _schema(con)
        con.execute('''UPDATE inbox_schedule SET state=?,detail=?,pid=NULL,birth=NULL,token=NULL,last_finished=?
                       WHERE id=1 AND token=?''', (state, detail, time.time(), token))


def recover_orphaned_run(active_tokens):
    """An in-process worker that ended without recording a result must not stall forever."""
    with db() as con:
        _schema(con)
        row = con.execute('SELECT state,pid,token FROM inbox_schedule WHERE id=1').fetchone()
        if row['state'] == 'running' and row['pid'] == os.getpid() and row['token'] not in active_tokens:
            con.execute('''UPDATE inbox_schedule SET state='attention',
                           detail='上次自动同步未能记录结果，请检查账号状态。',pid=NULL,birth=NULL,token=NULL
                           WHERE id=1 AND token=?''',(row['token'],))
