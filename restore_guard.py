"""Restore safety state stored outside the database that is being restored."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone


def _connect():
    from publisher import data_root
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / 'restore-guard.sqlite3', timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA synchronous=FULL')
    con.executescript('''
        CREATE TABLE IF NOT EXISTS restore_state (
            id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL,
            pending INTEGER NOT NULL, backup_name TEXT NOT NULL, started_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS retained_publications (
            platform TEXT NOT NULL, alias TEXT NOT NULL, identity_hash TEXT NOT NULL DEFAULT '',
            batch TEXT NOT NULL, core_hash TEXT NOT NULL, full_hash TEXT NOT NULL, state TEXT NOT NULL,
            PRIMARY KEY(platform,alias,identity_hash,batch,core_hash,full_hash)
        );
        CREATE TABLE IF NOT EXISTS restore_audits (
            generation TEXT NOT NULL, alias TEXT NOT NULL, identity_hash TEXT NOT NULL,
            checked_at TEXT NOT NULL, note_count INTEGER NOT NULL,
            PRIMARY KEY(generation,alias,identity_hash)
        );
        CREATE TABLE IF NOT EXISTS restored_platform_notes (
            generation TEXT NOT NULL, alias TEXT NOT NULL, identity_hash TEXT NOT NULL,
            note_id TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
            PRIMARY KEY(generation,alias,identity_hash,note_id)
        );
        CREATE TABLE IF NOT EXISTS restore_title_history (
            identity_hash TEXT NOT NULL,note_id TEXT NOT NULL,title TEXT NOT NULL,
            PRIMARY KEY(identity_hash,note_id,title)
        );
    ''')
    return con


def _hash(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def _post_hashes(post, platform):
    from publisher import content_key
    return (_hash(content_key(post, include_options=platform != 'xiaohongshu')),
            _hash(content_key(post)))


def write_main_marker(con, generation):
    con.execute('CREATE TABLE IF NOT EXISTS restore_guard_meta (id INTEGER PRIMARY KEY CHECK(id=1), generation TEXT NOT NULL)')
    con.execute('''INSERT INTO restore_guard_meta(id,generation) VALUES(1,?)
                   ON CONFLICT(id) DO UPDATE SET generation=excluded.generation''',(generation,))


def _main_generation():
    from publisher import db
    with db() as con:
        exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='restore_guard_meta'").fetchone()
        row = con.execute('SELECT generation FROM restore_guard_meta WHERE id=1').fetchone() if exists else None
        return row['generation'] if row else None


def begin_restore(backup_name):
    """Retain prior submissions and close the publishing gate before replacement."""
    from publisher import db
    main_generation = _main_generation()
    with closing(_connect()) as guard:
        existing = guard.execute('SELECT generation FROM restore_state WHERE id=1').fetchone()
    if main_generation and not existing:
        raise ValueError('独立防重复记录缺失，请先恢复 restore-guard.sqlite3 后再恢复数据库。')
    with db() as con:
        rows = con.execute('''SELECT t.batch,t.alias,t.state,t.content,a.platform,
                     COALESCE(i.identity_hash,'') AS identity_hash,p.note_id
                 FROM tasks t JOIN accounts a ON a.alias=t.alias
                 LEFT JOIN task_identities i ON i.batch=t.batch AND i.alias=t.alias
                 LEFT JOIN platform_observations p ON p.batch=t.batch AND p.alias=t.alias
                 WHERE t.state IN ('running','verifying','submitted','uncertain') OR p.note_id IS NOT NULL''').fetchall()
    retained = []
    try:
        for row in rows:
            core, full = _post_hashes(json.loads(row['content']), row['platform'])
            state = 'submitted' if row['state'] == 'submitted' or row['note_id'] else 'uncertain'
            retained.append((row['platform'],row['alias'],row['identity_hash'],row['batch'],core,full,state))
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError('现有发布记录无法完整保护，请先修复记录后再恢复备份。') from exc
    generation = uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    with closing(_connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        con.executemany('''INSERT INTO retained_publications(platform,alias,identity_hash,batch,core_hash,full_hash,state)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(platform,alias,identity_hash,batch,core_hash,full_hash)
            DO UPDATE SET state=CASE WHEN retained_publications.state='uncertain' OR excluded.state='uncertain'
                                     THEN 'uncertain' ELSE 'submitted' END''', retained)
        con.execute('''INSERT INTO restore_state(id,generation,pending,backup_name,started_at) VALUES(1,?,1,?,?)
            ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,pending=1,
            backup_name=excluded.backup_name,started_at=excluded.started_at''', (generation,backup_name,now))
    return generation


def require_review_complete():
    with closing(_connect()) as con:
        row = con.execute('SELECT generation,pending FROM restore_state WHERE id=1').fetchone()
        if row and row['pending']:
            raise ValueError('数据库恢复后请先在账号管理中核对平台作品，再确认解除发布保护。')
        main_generation = _main_generation()
        if (row and main_generation != row['generation']) or (not row and main_generation):
            raise ValueError('恢复保护记录与数据库不一致，请先恢复防重复记录并核对平台作品。')


def require_account_audited(alias, identity_hash, platform):
    with closing(_connect()) as con:
        state = con.execute('SELECT generation,pending FROM restore_state WHERE id=1').fetchone()
        if not state:
            return
        if state['pending']:
            raise ValueError('数据库恢复后请先核对平台作品，再确认解除发布保护。')
        identity = identity_hash or ''
        audited = con.execute('''SELECT 1 FROM restore_audits WHERE generation=? AND identity_hash=?
                                  AND (alias=? OR (?='xiaohongshu' AND identity_hash<>''))''',
                              (state['generation'],identity,alias,platform)).fetchone()
        if not audited:
            raise ValueError('此账号在恢复后尚未核对平台作品，请先到账号管理完成核对。')


def duplicate_matches(alias, identity_hash, platform, post):
    core, full = _post_hashes(post, platform)
    identity_hash = identity_hash or ''
    result = []
    with closing(_connect()) as con:
        rows = con.execute('''SELECT alias,identity_hash,batch,full_hash,state FROM retained_publications
                    WHERE platform=? AND core_hash=? AND (alias=? OR (identity_hash<>'' AND identity_hash=?))''',
                           (platform,core,alias,identity_hash)).fetchall()
        for row in rows:
            if row['identity_hash'] and identity_hash and row['identity_hash'] != identity_hash:
                continue
            result.append({'batch':row['batch'],'alias':row['alias'],'state':row['state'],
                           'same_options':row['full_hash']==full,'source':'restore_history'})
        if platform == 'xiaohongshu' and identity_hash:
            notes = con.execute('''SELECT DISTINCT note_id FROM (
                         SELECT identity_hash,note_id,title FROM restored_platform_notes
                         UNION ALL SELECT identity_hash,note_id,title FROM restore_title_history)
                         WHERE identity_hash=? AND title=?''', (identity_hash,post['title'])).fetchall()
            for row in notes:
                result.append({'batch':'平台作品 '+row['note_id'],'alias':alias,'state':'submitted',
                               'same_options':False,'source':'platform_title'})
    return result


def record_audit(generation, alias, identity_hash, notes):
    if not isinstance(notes, list):
        raise ValueError('平台作品列表无效，无法完成核对。')
    checked = []
    seen = set()
    for note in notes:
        if not isinstance(note, dict):
            raise ValueError('平台作品信息不完整，无法完成核对。')
        note_id, title = note.get('id'), note.get('display_title')
        if not isinstance(note_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',note_id) or not isinstance(title,str):
            raise ValueError('平台作品信息不完整，无法完成核对。')
        if note_id in seen:
            raise ValueError('平台作品列表存在重复，无法完成核对。')
        seen.add(note_id)
        checked.append((generation,alias,identity_hash,note_id,title[:500],str(note.get('tab_status',''))))
    now = datetime.now(timezone.utc).isoformat()
    with closing(_connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        state = con.execute('SELECT generation,pending FROM restore_state WHERE id=1').fetchone()
        if not state or state['generation'] != generation:
            raise ValueError('恢复状态已改变，请重新核对。')
        con.execute('DELETE FROM restored_platform_notes WHERE generation=? AND alias=? AND identity_hash=?',
                    (generation,alias,identity_hash))
        con.executemany('''INSERT INTO restored_platform_notes(generation,alias,identity_hash,note_id,title,status)
                           VALUES(?,?,?,?,?,?)''',checked)
        con.executemany('''INSERT OR IGNORE INTO restore_title_history(identity_hash,note_id,title) VALUES(?,?,?)''',
                        [(identity_hash,note[3],note[4]) for note in checked])
        con.execute('''INSERT INTO restore_audits(generation,alias,identity_hash,checked_at,note_count)
                       VALUES(?,?,?,?,?) ON CONFLICT(generation,alias,identity_hash)
                       DO UPDATE SET checked_at=excluded.checked_at,note_count=excluded.note_count''',
                    (generation,alias,identity_hash,now,len(checked)))
    return {'alias':alias,'note_count':len(checked),'checked_at':now}


def record_manual_audit(generation, alias):
    from publisher import db
    with db() as main:
        account = main.execute('''SELECT a.platform,COALESCE(i.identity_hash,'') AS identity_hash
                  FROM accounts a LEFT JOIN account_identities i ON i.alias=a.alias WHERE a.alias=?''',(alias,)).fetchone()
    if not account or account['platform'] == 'xiaohongshu':
        raise ValueError('该账号须使用平台作品核对。')
    with closing(_connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        state = con.execute('SELECT generation FROM restore_state WHERE id=1').fetchone()
        if not state or state['generation'] != generation:
            raise ValueError('恢复状态已改变，请重新核对。')
        con.execute('''INSERT INTO restore_audits(generation,alias,identity_hash,checked_at,note_count)
            VALUES(?,?,?,?,?) ON CONFLICT(generation,alias,identity_hash)
            DO UPDATE SET checked_at=excluded.checked_at,note_count=excluded.note_count''',
            (generation,alias,account['identity_hash'],datetime.now(timezone.utc).isoformat(),-1))
    return {'alias':alias,'recorded':True}


def status():
    from publisher import db
    with closing(_connect()) as con:
        state = con.execute('SELECT generation,pending,backup_name,started_at FROM restore_state WHERE id=1').fetchone()
        if not state:
            return {'pending':bool(_main_generation()),'broken':bool(_main_generation()),'accounts':[]}
        result = dict(state)
        result['pending'] = bool(result['pending'])
        result['broken'] = _main_generation() != state['generation']
        result['pending'] |= result['broken']
        with db() as main:
            accounts = main.execute('''SELECT a.alias,a.platform,a.label,p.nickname,COALESCE(i.identity_hash,'') AS identity_hash
                    FROM accounts a LEFT JOIN account_identities i ON i.alias=a.alias
                    LEFT JOIN account_profiles p ON p.alias=a.alias ORDER BY a.alias''').fetchall()
        output = []
        for account in accounts:
            audit = con.execute('''SELECT checked_at,note_count FROM restore_audits
                         WHERE generation=? AND alias=? AND identity_hash=?''',
                                (state['generation'],account['alias'],account['identity_hash'])).fetchone()
            notes = con.execute('''SELECT note_id,title,status FROM restored_platform_notes
                         WHERE generation=? AND alias=? AND identity_hash=? ORDER BY title,note_id''',
                                (state['generation'],account['alias'],account['identity_hash'])).fetchall() if audit else []
            output.append({'alias':account['alias'],'platform':account['platform'],'label':account['label'],
                           'nickname':account['nickname'],
                           'identity_verified':bool(account['identity_hash']),'audited':bool(audit),
                           'note_count':audit['note_count'] if audit else None,
                           'checked_at':audit['checked_at'] if audit else None,'notes':[dict(n) for n in notes]})
        result['accounts'] = output
        return result


def confirm_review(generation):
    from publisher import browser_lock, db
    with browser_lock(), db() as main, closing(_connect()) as con, con:
        accounts = main.execute('''SELECT a.alias,a.platform,COALESCE(i.identity_hash,'') AS identity_hash
                    FROM accounts a LEFT JOIN account_identities i ON i.alias=a.alias''').fetchall()
        con.execute('BEGIN IMMEDIATE')
        state = con.execute('SELECT generation,pending FROM restore_state WHERE id=1').fetchone()
        if not state or not state['pending'] or state['generation'] != generation:
            raise ValueError('恢复状态已改变，请重新核对。')
        marker = main.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='restore_guard_meta'").fetchone()
        current = main.execute('SELECT generation FROM restore_guard_meta WHERE id=1').fetchone() if marker else None
        if not current or current['generation'] != generation:
            raise ValueError('恢复保护记录与数据库不一致，请重新恢复后核对。')
        for account in accounts:
            if (account['platform'] == 'xiaohongshu' and not account['identity_hash']) or not con.execute('''SELECT 1 FROM restore_audits
                    WHERE generation=? AND alias=? AND identity_hash=?''',
                    (generation,account['alias'],account['identity_hash'])).fetchone():
                raise ValueError('请先核对所有账号的平台作品。')
        con.execute('UPDATE restore_state SET pending=0 WHERE id=1 AND generation=?', (generation,))
    return {'confirmed':True}
