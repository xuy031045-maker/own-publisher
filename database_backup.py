"""Create validated SQLite backups and restore with an automatic pre-restore copy."""
from __future__ import annotations

import re
import sqlite3
import os
import json
import subprocess
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from browser_queue import queue_status
from publisher import browser_lock, data_root, db


def _project_processes(rows, own_pid, parent_pid):
    root = str(Path(__file__).resolve().parent).casefold()
    found = []
    for row in rows:
        pid = row.get('ProcessId')
        command = row.get('CommandLine')
        if not isinstance(pid,int) or pid in (own_pid,parent_pid):
            continue
        if command is None:
            raise ValueError('无法检查部分后台 Python 进程，请关闭其他发布台和 Codex 后重试。')
        text = str(command).casefold()
        if (('uvicorn' in text and 'workbench:app' in text) or 'agent_server.py' in text or
                re.search(r'\s-m\s+agent_server(?:\s|$)',text) or
                (root in text and any(name in text for name in ('start_workbench.py','cli.py')))):
            found.append(pid)
    return found


def _other_project_processes():
    if os.name != 'nt':
        raise ValueError('当前系统无法核对后台发布程序，请在 Windows 上恢复。')
    script = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; "
              "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
              "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
                                capture_output=True,encoding='utf-8',errors='replace',timeout=20,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=True)
        parsed = json.loads(result.stdout or '[]')
        rows = parsed if isinstance(parsed,list) else [parsed]
        return _project_processes(rows,os.getpid(),os.getppid())
    except (OSError,subprocess.SubprocessError,ValueError,TypeError) as exc:
        raise ValueError('无法核对后台发布程序，请关闭其他工作台和 Codex 后重试。') from exc


def _backup_dir():
    folder = data_root() / 'backups'
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=15)


def _validate_database(path):
    with closing(_readonly(path)) as con:
        result = con.execute('PRAGMA integrity_check').fetchone()
        if not result or result[0] != 'ok':
            raise ValueError('备份文件未通过完整性检查。')
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'accounts', 'account_identities', 'batches', 'tasks'} <= tables:
            raise ValueError('该文件不是可恢复的发布台数据库。')
        required = {'accounts': {'alias', 'platform', 'label'}, 'tasks': {'batch', 'alias', 'content', 'state'},
                    'account_identities': {'alias', 'identity_hash'}, 'batches': {'id', 'fingerprint'}}
        for table, columns in required.items():
            actual = {row[1] for row in con.execute(f'PRAGMA table_info({table})')}
            if not columns <= actual:
                raise ValueError('备份数据库结构不完整。')
        if con.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('备份数据库存在关联错误。')
    return True


def _validate(path):
    try:
        return _validate_database(path)
    except (sqlite3.DatabaseError, OSError) as exc:
        raise ValueError('备份文件损坏或无法读取。') from exc


def create_backup(prefix='backup'):
    if prefix not in ('backup', 'before-restore'):
        raise ValueError('备份类型无效。')
    with db():
        pass
    folder = _backup_dir()
    name = f'{prefix}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.sqlite3'
    final = folder / name
    temporary = folder / (name + '.tmp')
    try:
        with closing(_readonly(data_root() / 'publisher.sqlite3')) as source, closing(sqlite3.connect(temporary)) as dest:
            source.backup(dest)
        _validate(temporary)
        temporary.replace(final)
    finally:
        temporary.unlink(missing_ok=True)
    return {'name': name, 'bytes': final.stat().st_size}


def list_backups():
    folder = _backup_dir()
    rows = []
    for path in sorted(folder.glob('*.sqlite3'), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.is_file() and not path.is_symlink() and path.resolve().parent == folder.resolve():
            rows.append({'name': path.name, 'bytes': path.stat().st_size})
    return {'backups': rows}


def restore_backup(name):
    if not isinstance(name, str) or not re.fullmatch(r'(?:backup|before-restore)-\d{8}-\d{6}-[0-9a-f]{8}\.sqlite3', name):
        raise ValueError('备份名称无效。')
    folder = _backup_dir()
    source = folder / name
    if source.is_symlink() or not source.is_file() or source.resolve().parent != folder.resolve():
        raise ValueError('备份文件不存在或位置无效。')
    if _other_project_processes():
        raise ValueError('检测到其他发布台或 Codex 发布接口仍在运行，请关闭后再恢复。')
    _validate(source)
    if queue_status()['tasks']:
        raise ValueError('请先等待或取消浏览器任务，再恢复备份。')
    with browser_lock():
        # SQLite's backup API replaces the destination in a transaction; it does not
        # replace an open database file underneath other SQLite connections.
        prior = create_backup('before-restore')
        import restore_guard
        generation = restore_guard.begin_restore(name)
        target = data_root() / 'publisher.sqlite3'
        try:
            with closing(_readonly(source)) as src, closing(sqlite3.connect(target, timeout=15)) as dest:
                src.backup(dest)
                with dest:
                    restore_guard.write_main_marker(dest, generation)
                    if dest.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbox_schedule'").fetchone():
                        dest.execute('''UPDATE inbox_schedule SET interval_minutes=0,next_run=0,state='attention',
                                     detail='恢复后自动同步已关闭，请按需重新开启。',pid=NULL,birth=NULL,token=NULL WHERE id=1''')
            _validate(target)
        except Exception as exc:
            try:
                with closing(_readonly(folder / prior['name'])) as src, closing(sqlite3.connect(target, timeout=15)) as dest:
                    src.backup(dest)
                    with dest:
                        restore_guard.write_main_marker(dest, generation)
                _validate(target)
            except Exception as rollback_exc:
                raise RuntimeError(f'恢复失败，自动回退也未完成。安全备份为 {prior["name"]}。') from rollback_exc
            raise ValueError(f'恢复失败，已回退到恢复前状态。安全备份为 {prior["name"]}；发布前仍需核对平台。') from exc
    return {'restored': name, 'safety_backup': prior['name'], 'review_required':True, 'generation':generation}
