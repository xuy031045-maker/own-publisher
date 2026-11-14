from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

import xiaohongshu as xhs

PLATFORMS = ("xiaohongshu", "douyin")
LIMIT = 10
IMAGE_TYPES = {".jpg", ".jpeg", ".png"}


def data_root():
    default = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "OwnPublisher"
    return Path(os.environ.get("OWN_PUBLISHER_HOME", default)).resolve()


@contextmanager
def db():
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "publisher.sqlite3", timeout=15)) as con, con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                alias TEXT PRIMARY KEY, platform TEXT NOT NULL, label TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS account_profiles (
                alias TEXT PRIMARY KEY REFERENCES accounts(alias),
                nickname TEXT NOT NULL, checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS account_notes (
                alias TEXT PRIMARY KEY REFERENCES accounts(alias), note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS tasks (
                batch TEXT REFERENCES batches(id), alias TEXT REFERENCES accounts(alias),
                content TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                detail TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (batch, alias)
            );
            CREATE TABLE IF NOT EXISTS account_identities (
                alias TEXT PRIMARY KEY REFERENCES accounts(alias),
                identity_hash TEXT NOT NULL, checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS task_identities (
                batch TEXT NOT NULL, alias TEXT NOT NULL, identity_hash TEXT NOT NULL,
                nickname TEXT NOT NULL DEFAULT '', PRIMARY KEY (batch, alias),
                FOREIGN KEY (batch, alias) REFERENCES tasks(batch, alias)
            );
            CREATE TABLE IF NOT EXISTS verification_signals (
                batch TEXT NOT NULL, alias TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (batch, alias),
                FOREIGN KEY (batch, alias) REFERENCES tasks(batch, alias)
            );
            CREATE TABLE IF NOT EXISTS verification_cancellations (
                batch TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (batch, alias),
                FOREIGN KEY (batch, alias) REFERENCES tasks(batch, alias)
            );
            CREATE TABLE IF NOT EXISTS duplicate_confirmations (
                batch TEXT NOT NULL, alias TEXT NOT NULL, previous_batch TEXT NOT NULL,
                confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (batch, alias, previous_batch),
                FOREIGN KEY (batch, alias) REFERENCES tasks(batch, alias)
            );
            CREATE TABLE IF NOT EXISTS platform_observations (
                batch TEXT NOT NULL, alias TEXT NOT NULL, note_id TEXT NOT NULL,
                status TEXT NOT NULL, observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (batch, alias),
                FOREIGN KEY (batch, alias) REFERENCES tasks(batch, alias)
            );
        """)
        yield con


@contextmanager
def browser_lock():
    import msvcrt

    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "browser.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ValueError("已有登录、检查或发布任务正在使用浏览器，请稍后再试。") from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def add_account(alias, platform, label=""):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", alias):
        raise ValueError("账号别名须以小写字母开头，后面可用小写字母、数字、下划线或连字符。")
    if platform not in PLATFORMS:
        raise ValueError("平台须为 xiaohongshu 或 douyin。")
    with db() as con:
        current = con.execute("SELECT platform FROM accounts WHERE alias=?", (alias,)).fetchone()
        if current and current["platform"] != platform:
            raise ValueError("这个别名已经绑定另一平台，请使用新别名。")
        con.execute("INSERT INTO accounts VALUES (?,?,?) ON CONFLICT(alias) DO UPDATE SET label=excluded.label",
                    (alias, platform, label.strip() or alias))
    return get_account(alias)


def get_account(alias):
    with db() as con:
        row = con.execute("SELECT * FROM accounts WHERE alias=?", (alias,)).fetchone()
    if row is None:
        raise ValueError(f"未知账号别名：{alias}")
    return dict(row)


def list_accounts():
    with db() as con:
        return [dict(row) for row in con.execute("SELECT * FROM accounts ORDER BY alias")]


def save_account_nickname(alias, nickname):
    if not nickname:
        return
    with db() as con:
        con.execute("""
            INSERT INTO account_profiles(alias, nickname) VALUES (?, ?)
            ON CONFLICT(alias) DO UPDATE SET nickname=excluded.nickname, checked_at=CURRENT_TIMESTAMP
        """, (alias, nickname))


def save_account_identity(alias, nickname, identity_hash):
    if not isinstance(identity_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", identity_hash):
        raise ValueError("账号身份信息无效，请重新检查账号。")
    with db() as con:
        if nickname:
            con.execute("""
                INSERT INTO account_profiles(alias, nickname) VALUES (?, ?)
                ON CONFLICT(alias) DO UPDATE SET nickname=excluded.nickname, checked_at=CURRENT_TIMESTAMP
            """, (alias, nickname))
        con.execute("""
            INSERT INTO account_identities(alias, identity_hash) VALUES (?, ?)
            ON CONFLICT(alias) DO UPDATE SET identity_hash=excluded.identity_hash, checked_at=CURRENT_TIMESTAMP
        """, (alias, identity_hash))


def hash_file(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def prepare_post(raw, base, platform):
    fields = {"title", "body", "tags", "images"}
    if platform == "xiaohongshu":
        fields.update({"schedule", "declaration", "original", "source_name", "shoot_place", "shoot_address", "shoot_date"})
    if not isinstance(raw, dict) or raw.keys() - fields:
        raise ValueError("图文包含当前平台不支持的字段。")
    title, body = raw.get("title"), raw.get("body", "")
    tags, images = raw.get("tags", []), raw.get("images")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 20:
        raise ValueError("首版标题长度为 1 到 20 字。")
    if not isinstance(body, str) or len(body) > 1000:
        raise ValueError("首版正文最多 1000 字。")
    if not isinstance(tags, list) or len(tags) > 10 or any(
        not isinstance(t, str) or not re.fullmatch(r"[^#\s]{1,40}", t) for t in tags
    ):
        raise ValueError("话题最多 10 个，使用不含空白和 # 的名称。")
    if not isinstance(images, list) or not 1 <= len(images) <= 18:
        raise ValueError("首版每条图文需要 1 到 18 张本地图片。")
    files = []
    for image in images:
        if not isinstance(image, str) or not image or "://" in image:
            raise ValueError("图片须使用本地路径。")
        path = (base / image).resolve()
        if not path.is_file():
            raise ValueError(f"找不到图片：{image}")
        if platform == "xiaohongshu":
            xhs.image_info(path, path.name)
        elif path.suffix.lower() not in IMAGE_TYPES:
            raise ValueError(f"找不到 JPG 或 PNG 图片：{image}")
        elif not 0 < path.stat().st_size <= 20 * 1024 * 1024:
            raise ValueError(f"单张图片须在 20 MiB 以内：{image}")
        files.append({"path": str(path), "sha256": hash_file(path)})
    options = xhs.post_options(raw) if platform == "xiaohongshu" else {}
    return {"title": title.strip(), "body": body, "tags": tags, "images": files, **options}


def prepare_batch(manifest):
    manifest = Path(manifest).resolve()
    raw = json.loads(manifest.read_text(encoding="utf-8-sig"))
    return prepare_batch_data(raw, manifest.parent)


def prepare_batch_data(raw, base):
    if not isinstance(raw, dict) or raw.keys() - {"post", "targets"}:
        raise ValueError("任务文件只接受 post 和 targets 字段。")
    shared, targets = raw.get("post", {}), raw.get("targets")
    if not isinstance(shared, dict) or not isinstance(targets, list) or not 1 <= len(targets) <= LIMIT:
        raise ValueError("一个批次需要 1 到 10 个目标账号。")
    tasks = {}
    platforms = {}
    for target in targets:
        if isinstance(target, str):
            target = {"account": target}
        if not isinstance(target, dict) or target.keys() - {"account", "post"}:
            raise ValueError("目标须为账号别名或包含 account、post 的对象。")
        alias, override = target.get("account"), target.get("post", {})
        if not isinstance(alias, str) or not isinstance(override, dict) or alias in tasks:
            raise ValueError("每个账号别名在同一批次只能出现一次。")
        platform = get_account(alias)["platform"]
        platforms[alias] = platform
        tasks[alias] = prepare_post(shared | override, base, platform)
    with db() as con:
        identities = {row["alias"]: dict(row) for row in con.execute("""
            SELECT account_identities.alias, identity_hash, COALESCE(nickname, '') AS nickname
            FROM account_identities LEFT JOIN account_profiles USING(alias)
        """) if row["alias"] in tasks and platforms[row["alias"]] == "xiaohongshu"}
    seen_identities = {}
    for alias, identity in identities.items():
        previous = seen_identities.setdefault(identity["identity_hash"], alias)
        if previous != alias:
            raise ValueError(f"账号 {previous} 和 {alias} 上次核验为同一个小红书账号，请只选择一个。")
    fingerprint_input = {
        alias: post | {"images": [image["sha256"] for image in post["images"]]}
        | ({"_account_identity": identities[alias]["identity_hash"]} if alias in identities else {})
        for alias, post in tasks.items()
    }
    fingerprint = hashlib.sha256(json.dumps(fingerprint_input, sort_keys=True).encode()).hexdigest()
    with db() as con:
        batch = uuid.uuid4().hex[:16]
        inserted = con.execute("INSERT OR IGNORE INTO batches VALUES (?,?)", (batch, fingerprint))
        if inserted.rowcount:
            con.executemany("INSERT INTO tasks(batch,alias,content) VALUES (?,?,?)", [
                (batch, alias, json.dumps(post, ensure_ascii=False)) for alias, post in tasks.items()
            ])
            con.executemany("""
                INSERT INTO task_identities(batch, alias, identity_hash, nickname) VALUES (?, ?, ?, ?)
            """, [(batch, alias, identity["identity_hash"], identity["nickname"])
                  for alias, identity in identities.items()])
        else:
            batch = con.execute("SELECT id FROM batches WHERE fingerprint=?", (fingerprint,)).fetchone()["id"]
    return batch_status(batch, preview=True)


def batch_status(batch, preview=False):
    with db() as con:
        rows = con.execute("""
            SELECT tasks.alias, accounts.platform, accounts.label, tasks.state, tasks.detail,
                   tasks.updated, tasks.content, task_identities.nickname AS preview_nickname,
                   task_identities.identity_hash IS NOT NULL AS preview_verified,
                   task_identities.identity_hash AS preview_identity_hash,
                   platform_observations.note_id AS platform_note_id,
                   platform_observations.status AS platform_status,
                   platform_observations.observed_at AS platform_checked_at
            FROM tasks JOIN accounts ON accounts.alias=tasks.alias
            LEFT JOIN task_identities ON task_identities.batch=tasks.batch
                AND task_identities.alias=tasks.alias
            LEFT JOIN platform_observations ON platform_observations.batch=tasks.batch
                AND platform_observations.alias=tasks.alias
            WHERE tasks.batch=? ORDER BY tasks.rowid
        """, (batch,)).fetchall()
    if not rows:
        raise ValueError(f"未找到批次：{batch}")
    tasks = []
    for row in rows:
        task = dict(row)
        preview_nickname = task.pop("preview_nickname")
        preview_verified = task.pop("preview_verified")
        preview_identity_hash = task.pop("preview_identity_hash")
        note_id = task.pop("platform_note_id")
        platform_status = task.pop("platform_status")
        checked_at = task.pop("platform_checked_at")
        if note_id:
            task["platform_result"] = {"note_id": note_id, "status": platform_status, "checked_at": checked_at}
        post = json.loads(task.pop("content"))
        task["title"] = post["title"]
        task["image_count"] = len(post["images"])
        if "schedule" in post:
            task["schedule"] = post["schedule"]
        if preview:
            task["post"] = post
            if task["platform"] == "xiaohongshu":
                task["identity_verified"] = bool(preview_verified)
                if preview_nickname:
                    task["nickname"] = preview_nickname
                task["duplicate_matches"] = duplicate_matches(batch, task["alias"], post, preview_identity_hash,
                                                               task["platform"])
        tasks.append(task)
    return {"batch": batch, "tasks": tasks}


def set_state(batch, alias, state, detail=""):
    with db() as con:
        changed = con.execute("UPDATE tasks SET state=?, detail=?, updated=CURRENT_TIMESTAMP WHERE batch=? AND alias=?",
                              (state, detail, batch, alias))
        if changed.rowcount != 1:
            return
        if state == "verifying":
            platform = con.execute("SELECT platform FROM accounts WHERE alias=?", (alias,)).fetchone()["platform"]
            if platform == "xiaohongshu":
                con.execute("""
                    INSERT INTO verification_signals(batch, alias, acknowledged) VALUES (?, ?, 0)
                    ON CONFLICT(batch, alias) DO UPDATE SET acknowledged=0
                """, (batch, alias))
                con.execute("DELETE FROM verification_cancellations WHERE batch=? AND alias=?", (batch, alias))
        else:
            con.execute("DELETE FROM verification_signals WHERE batch=? AND alias=?", (batch, alias))
            con.execute("DELETE FROM verification_cancellations WHERE batch=? AND alias=?", (batch, alias))


def acknowledge_verification(batch):
    try:
        with browser_lock():
            active = False
    except ValueError:
        active = True
    if not active:
        raise ValueError("当前没有运行中的发布任务，请先核对平台记录。")
    with db() as con:
        rows = con.execute("""
            SELECT verification_signals.alias FROM verification_signals
            JOIN tasks ON tasks.batch=verification_signals.batch AND tasks.alias=verification_signals.alias
            JOIN accounts ON accounts.alias=tasks.alias
            WHERE tasks.batch=? AND tasks.state='verifying' AND accounts.platform='xiaohongshu'
        """, (batch,)).fetchall()
        if len(rows) != 1:
            raise ValueError("当前批次没有等待验证的任务。")
        con.execute("UPDATE verification_signals SET acknowledged=1 WHERE batch=? AND alias=?",
                    (batch, rows[0]["alias"]))
    return {"accepted": True}


def consume_verification_ack(batch, alias):
    with db() as con:
        changed = con.execute("""
            UPDATE verification_signals SET acknowledged=0
            WHERE batch=? AND alias=? AND acknowledged=1
            AND EXISTS (SELECT 1 FROM tasks WHERE tasks.batch=? AND tasks.alias=? AND tasks.state='verifying')
        """, (batch, alias, batch, alias))
        return changed.rowcount == 1


def cancel_verification(batch):
    try:
        with browser_lock():
            active = False
    except ValueError:
        active = True
    if not active:
        raise ValueError("当前没有运行中的发布任务，请先核对平台记录。")
    with db() as con:
        rows = con.execute("""
            SELECT verification_signals.alias FROM verification_signals
            JOIN tasks ON tasks.batch=verification_signals.batch AND tasks.alias=verification_signals.alias
            JOIN accounts ON accounts.alias=tasks.alias
            WHERE tasks.batch=? AND tasks.state='verifying' AND accounts.platform='xiaohongshu'
        """, (batch,)).fetchall()
        if len(rows) != 1:
            raise ValueError("当前批次没有等待验证的任务。")
        con.execute("INSERT OR IGNORE INTO verification_cancellations(batch, alias) VALUES (?, ?)",
                    (batch, rows[0]["alias"]))
    return {"accepted": True}


def verification_cancel_requested(batch, alias):
    with db() as con:
        return con.execute("""
            SELECT 1 FROM verification_cancellations WHERE batch=? AND alias=?
        """, (batch, alias)).fetchone() is not None


def content_key(post, *, include_options=True):
    images = [image["sha256"] for image in post["images"]]
    if include_options:
        return post | {"images": images}
    return {"title": post["title"], "body": post["body"], "tags": post["tags"], "images": images}


def duplicate_matches(batch, alias, post, identity_hash=None, platform="xiaohongshu"):
    key = content_key(post, include_options=platform != "xiaohongshu")
    matches = []
    with db() as con:
        rows = con.execute("""
            SELECT tasks.batch, tasks.alias, tasks.state, tasks.content, task_identities.identity_hash
            FROM tasks LEFT JOIN task_identities ON task_identities.batch=tasks.batch
                AND task_identities.alias=tasks.alias
            WHERE (tasks.alias=? OR task_identities.identity_hash=?) AND tasks.batch<>?
            AND tasks.state IN ('running', 'verifying', 'submitted', 'uncertain')
            ORDER BY tasks.updated DESC, tasks.rowid DESC
        """, (alias, identity_hash, batch))
        for row in rows:
            if row["identity_hash"] and identity_hash and row["identity_hash"] != identity_hash:
                continue
            previous = json.loads(row["content"])
            if content_key(previous, include_options=platform != "xiaohongshu") == key:
                matches.append({"batch": row["batch"], "alias": row["alias"], "state": row["state"],
                                "same_options": content_key(previous) == content_key(post)})
    import restore_guard
    retained = restore_guard.duplicate_matches(alias, identity_hash, platform, post)
    for saved in retained:
        key = (saved['batch'],saved['alias'],saved['state'],saved['same_options'])
        existing = next((m for m in matches if (m['batch'],m['alias'],m['state'],m['same_options']) == key),None)
        if existing and saved.get('source') == 'restore_history':
            existing['source'] = 'restore_history'
        elif not existing:
            matches.append(saved)
    return matches


def sent_before(batch, alias, post, identity_hash=None):
    matches = duplicate_matches(batch, alias, post, identity_hash)
    return (matches[0]["batch"], matches[0]["state"]) if matches else None


def run_batch(batch, confirmation, retry=False, adapter=None, on_verification=None, verification_acknowledged=None,
              allow_duplicate_accounts=None, cancel_requested=None, on_queue=None):
    from browser import LoginNeeded, PublishUncertain, VerificationStopped, VerificationUncertain, publish
    from browser_queue import queued_browser
    import restore_guard

    if confirmation != batch:
        raise ValueError("发布前请检查批次预览，并传入与批次相同的确认编号。")
    allowed = [] if allow_duplicate_accounts is None else allow_duplicate_accounts
    if not isinstance(allowed, list) or any(not isinstance(alias, str) for alias in allowed) or len(set(allowed)) != len(allowed):
        raise ValueError("重复发布确认须提供账号别名列表。")
    with queued_browser('publish', cancel_requested=cancel_requested, on_state=on_queue):
        restore_guard.require_review_complete()
        tasks = batch_status(batch, preview=True)["tasks"]
        if set(allowed) - {task["alias"] for task in tasks}:
            raise ValueError("重复发布确认包含不属于此批次的账号。")
        for task in tasks:
            if cancel_requested and cancel_requested():
                break
            alias, state = task["alias"], task["state"]
            if state in {"running", "verifying"}:
                set_state(batch, alias, "uncertain", "上次执行中断，请到对应平台核对。")
                continue
            if task.get('platform_result') and state in {'queued', 'failed', 'login_needed', 'cancelled', 'duplicate'}:
                set_state(batch, alias, 'uncertain', '平台已找到对应作品，不能再次提交；请到平台核对。')
                continue
            if state != "queued" and not (retry and (state in {"failed", "login_needed", "cancelled"}
                                                       or (state == "duplicate" and alias in allowed))):
                continue
            post = task["post"]
            identity_hash = None
            if task["platform"] == "xiaohongshu":
                with db() as con:
                    identity = con.execute("""
                        SELECT identity_hash FROM task_identities WHERE batch=? AND alias=?
                    """, (batch, alias)).fetchone()
                if identity is None:
                    set_state(batch, alias, "failed", "账号尚未核验身份，请先检查账号并重新生成预览。")
                    continue
                identity_hash = identity["identity_hash"]
                task["expected_identity"] = identity_hash
            restore_guard.require_account_audited(alias, identity_hash, task['platform'])
            previous = duplicate_matches(batch, alias, post, identity_hash, task["platform"])
            if previous:
                restored = next((item for item in previous if item.get('source') == 'restore_history'), None)
                if restored:
                    set_state(batch, alias, 'duplicate',
                              f"恢复前已有相同图文的提交或待核对记录，已阻止再次提交。批次 {restored['batch']}。")
                    continue
                unresolved = next((item for item in previous if item["state"] != "submitted"), None)
                identical = next((item for item in previous if item["same_options"]), None)
                if unresolved or identical or alias not in allowed or task["platform"] != "xiaohongshu":
                    match = unresolved or identical or previous[0]
                    reason = ("已有待核对或执行中的相同图文，请先核对。" if unresolved else
                              "完全相同的发布已有记录。" if identical else
                              "相同图文已发布，时间或声明不同；请确认是否再次发布。")
                    set_state(batch, alias, "duplicate", f"{reason} 批次 {match['batch']}，状态 {match['state']}。")
                    continue
                with db() as con:
                    con.executemany("""
                        INSERT OR IGNORE INTO duplicate_confirmations(batch, alias, previous_batch) VALUES (?, ?, ?)
                    """, [(batch, alias, item["batch"]) for item in previous])
            set_state(batch, alias, "running")
            try:
                for image in post["images"]:
                    if hash_file(Path(image["path"])) != image["sha256"]:
                        raise ValueError("图片与预览时不同，请重新准备批次。")
                if "schedule" in post:
                    xhs.schedule_time(post["schedule"])
                if adapter:
                    adapter(task, post)
                else:
                    def verification(detail):
                        set_state(batch, alias, "verifying" if detail else "running", detail or "")
                        if on_verification:
                            on_verification(alias, detail)

                    def acknowledgement():
                        if task["platform"] == "xiaohongshu" and consume_verification_ack(batch, alias):
                            return True
                        return bool(verification_acknowledged and verification_acknowledged())

                    options = {"on_verification": verification if on_verification else None,
                               "verification_acknowledged": acknowledgement if on_verification else verification_acknowledged}
                    if on_verification and task["platform"] == "xiaohongshu":
                        options["verification_cancelled"] = lambda: verification_cancel_requested(batch, alias)
                    publish(task, post, **options)
            except VerificationStopped as exc:
                set_state(batch, alias, "cancelled", str(exc))
                break
            except VerificationUncertain as exc:
                set_state(batch, alias, "uncertain", str(exc))
                break
            except LoginNeeded as exc:
                set_state(batch, alias, "login_needed", str(exc))
            except PublishUncertain as exc:
                set_state(batch, alias, "uncertain", str(exc))
            except (ValueError, OSError) as exc:
                set_state(batch, alias, "failed", str(exc))
            except Exception:
                set_state(batch, alias, "uncertain", "执行异常，无法确认是否提交。请核对平台作品记录。")
            else:
                if "schedule" in post:
                    set_state(batch, alias, "submitted", "平台已接收定时发布。请在创作者后台核对计划时间。")
                else:
                    set_state(batch, alias, "submitted", "平台页面出现提交确认。审核与公开状态可在创作者后台查看。")
    return batch_status(batch)
