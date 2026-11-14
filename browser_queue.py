"""Cross-process FIFO for browser work; never resumes an interrupted operation."""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager


class QueueCancelled(Exception):
    pass


def _process_birth(pid):
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                       ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                       ctypes.POINTER(wintypes.FILETIME))
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return -1 if ctypes.get_last_error() == 87 else None
    try:
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(system), ctypes.byref(user)):
            return None
        if exited.dwHighDateTime or exited.dwLowDateTime:
            return -1
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def _alive(pid, birth):
    if pid == os.getpid():
        return True
    if os.name == 'nt':
        observed = _process_birth(pid)
        return observed != -1 and (observed is None or birth is None or str(observed) == birth)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _connect():
    from publisher import data_root
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / 'browser-queue.sqlite3', timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('''CREATE TABLE IF NOT EXISTS browser_queue (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
        pid INTEGER NOT NULL, birth TEXT, state TEXT NOT NULL, created REAL NOT NULL)''')
    return con


def _remove_dead(con):
    for row in con.execute('SELECT id,pid,birth FROM browser_queue').fetchall():
        if not _alive(row['pid'], row['birth']):
            con.execute('DELETE FROM browser_queue WHERE id=?', (row['id'],))


def queue_status():
    with closing(_connect()) as con, con:
        con.execute('BEGIN IMMEDIATE')
        _remove_dead(con)
        rows = con.execute('SELECT id,kind,state,created FROM browser_queue ORDER BY seq').fetchall()
        return {'tasks': [dict(row) for row in rows]}


@contextmanager
def queued_browser(kind, *, cancel_requested=None, on_state=None):
    """Wait for the FIFO turn and hold the same lock used by all browser paths."""
    from publisher import browser_lock
    if kind not in {'publish', 'sync', 'open', 'login', 'check', 'platform', 'restore'}:
        raise ValueError('浏览器任务类型无效。')
    identifier = uuid.uuid4().hex
    birth = _process_birth(os.getpid())
    with closing(_connect()) as con, con:
        con.execute('INSERT INTO browser_queue(id,kind,pid,birth,state,created) VALUES(?,?,?,?,?,?)',
                    (identifier, kind, os.getpid(), str(birth) if birth is not None else None, 'waiting', time.time()))
    lock_context = None
    announced = None
    try:
        while True:
            if cancel_requested and cancel_requested():
                raise QueueCancelled('任务已取消，尚未使用浏览器。')
            with closing(_connect()) as con, con:
                con.execute('BEGIN IMMEDIATE')
                _remove_dead(con)
                first = con.execute('SELECT id FROM browser_queue ORDER BY seq LIMIT 1').fetchone()
                if first and first['id'] == identifier:
                    candidate = browser_lock()
                    try:
                        candidate.__enter__()
                    except ValueError:
                        pass
                    else:
                        lock_context = candidate
                        con.execute("UPDATE browser_queue SET state='running' WHERE id=?", (identifier,))
            if lock_context:
                if on_state:
                    on_state('running', '正在使用浏览器。')
                break
            if announced != 'waiting' and on_state:
                on_state('waiting', '正在排队，等待其他浏览器任务结束。')
                announced = 'waiting'
            time.sleep(0.25)
        yield
    finally:
        if lock_context:
            lock_context.__exit__(None, None, None)
        with closing(_connect()) as con, con:
            con.execute('DELETE FROM browser_queue WHERE id=?', (identifier,))
