from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path


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
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS tasks (
                batch TEXT REFERENCES batches(id), alias TEXT REFERENCES accounts(alias),
                content TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                detail TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (batch, alias)
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


def hash_file(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def prepare_post(raw, base):
    if not isinstance(raw, dict) or raw.keys() - {"title", "body", "tags", "images"}:
        raise ValueError("图文只接受 title、body、tags、images 字段。")
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
        if not path.is_file() or path.suffix.lower() not in IMAGE_TYPES:
            raise ValueError(f"找不到 JPG 或 PNG 图片：{image}")
        if not 0 < path.stat().st_size <= 20 * 1024 * 1024:
            raise ValueError(f"单张图片须在 20 MiB 以内：{image}")
        files.append({"path": str(path), "sha256": hash_file(path)})
    return {"title": title.strip(), "body": body, "tags": tags, "images": files}


def prepare_batch(manifest):
    manifest = Path(manifest).resolve()
    raw = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or raw.keys() - {"post", "targets"}:
        raise ValueError("任务文件只接受 post 和 targets 字段。")
    shared, targets = raw.get("post", {}), raw.get("targets")
    if not isinstance(shared, dict) or not isinstance(targets, list) or not 1 <= len(targets) <= LIMIT:
        raise ValueError("一个批次需要 1 到 10 个目标账号。")
    tasks = {}
    for target in targets:
        if isinstance(target, str):
            target = {"account": target}
        if not isinstance(target, dict) or target.keys() - {"account", "post"}:
            raise ValueError("目标须为账号别名或包含 account、post 的对象。")
        alias, override = target.get("account"), target.get("post", {})
        if not isinstance(alias, str) or not isinstance(override, dict) or alias in tasks:
            raise ValueError("每个账号别名在同一批次只能出现一次。")
        get_account(alias)
        tasks[alias] = prepare_post(shared | override, manifest.parent)
    fingerprint_input = {
        alias: post | {"images": [image["sha256"] for image in post["images"]]}
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
        else:
            batch = con.execute("SELECT id FROM batches WHERE fingerprint=?", (fingerprint,)).fetchone()["id"]
    return batch_status(batch, preview=True)


def batch_status(batch, preview=False):
    with db() as con:
        rows = con.execute("""
            SELECT alias, platform, label, state, detail, updated, content
            FROM tasks JOIN accounts USING(alias) WHERE batch=? ORDER BY tasks.rowid
        """, (batch,)).fetchall()
    if not rows:
        raise ValueError(f"未找到批次：{batch}")
    tasks = []
    for row in rows:
        task = dict(row)
        post = json.loads(task.pop("content"))
        task["title"] = post["title"]
        task["image_count"] = len(post["images"])
        if preview:
            task["post"] = post
        tasks.append(task)
    return {"batch": batch, "tasks": tasks}


def set_state(batch, alias, state, detail=""):
    with db() as con:
        con.execute("UPDATE tasks SET state=?, detail=?, updated=CURRENT_TIMESTAMP WHERE batch=? AND alias=?",
                    (state, detail, batch, alias))


def sent_before(batch, alias, post):
    def content_key(content):
        return content | {"images": [image["sha256"] for image in content["images"]]}

    key = content_key(post)
    with db() as con:
        rows = con.execute("""
            SELECT batch, state, content FROM tasks WHERE alias=? AND batch<>?
            AND state IN ('running', 'submitted', 'uncertain')
        """, (alias, batch))
        for row in rows:
            if content_key(json.loads(row["content"])) == key:
                return row["batch"], row["state"]
    return None


def run_batch(batch, confirmation, retry=False, adapter=None):
    from browser import LoginNeeded, PublishUncertain, publish

    if confirmation != batch:
        raise ValueError("发布前请检查批次预览，并传入与批次相同的确认编号。")
    adapter = adapter or publish
    with browser_lock():
        tasks = batch_status(batch, preview=True)["tasks"]
        for task in tasks:
            alias, state = task["alias"], task["state"]
            if state == "running":
                set_state(batch, alias, "uncertain", "上次执行中断，请到对应平台核对。")
                continue
            if state != "queued" and not (retry and state in {"failed", "login_needed"}):
                continue
            post = task["post"]
            previous = sent_before(batch, alias, post)
            if previous:
                set_state(batch, alias, "duplicate", f"相同图文已有记录：批次 {previous[0]}，状态 {previous[1]}。")
                continue
            set_state(batch, alias, "running")
            try:
                for image in post["images"]:
                    if hash_file(Path(image["path"])) != image["sha256"]:
                        raise ValueError("图片与预览时不同，请重新准备批次。")
                adapter(task, post)
            except LoginNeeded as exc:
                set_state(batch, alias, "login_needed", str(exc))
            except PublishUncertain as exc:
                set_state(batch, alias, "uncertain", str(exc))
            except (ValueError, OSError) as exc:
                set_state(batch, alias, "failed", str(exc))
            except Exception:
                set_state(batch, alias, "uncertain", "执行异常，无法确认是否提交。请核对平台作品记录。")
            else:
                set_state(batch, alias, "submitted", "平台页面出现提交确认。审核与公开状态可在创作者后台查看。")
    return batch_status(batch)
