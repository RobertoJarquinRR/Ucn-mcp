"""SQLite storage for tasks, courses and submission history."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from core.models import Course, Task, TaskStatus

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "storage" / "tasks.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY,
    name TEXT,
    url TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    cmid TEXT PRIMARY KEY,
    type TEXT,
    course_id TEXT,
    course_name TEXT,
    title TEXT,
    url TEXT,
    description TEXT,
    due_date TEXT,
    due_iso TEXT,
    submission_status TEXT,
    grading_status TEXT,
    status TEXT,
    discovered_at TEXT,
    updated_at TEXT,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cmid TEXT,
    event TEXT,
    detail TEXT,
    ts TEXT
);
-- Sections (semanas) of a course
CREATE TABLE IF NOT EXISTS sections (
    key TEXT PRIMARY KEY,           -- "<course_id>:<sectionid>"
    course_id TEXT,
    sectionid TEXT,
    name TEXT,
    position INTEGER,
    summary TEXT,
    updated_at TEXT
);
-- Any course item: tasks, resources, urls, labels, forums, attendance
CREATE TABLE IF NOT EXISTS items (
    key TEXT PRIMARY KEY,           -- cmid if module, else "<course>:<section>:label:<n>"
    course_id TEXT,
    course_name TEXT,
    section_key TEXT,
    section_name TEXT,
    item_type TEXT,                 -- assign|quiz|forum|resource|url|label|attendance
    title TEXT,
    url TEXT,
    description TEXT,
    file_url TEXT,                  -- direct download URL (resources)
    due_iso TEXT,
    status TEXT,                    -- info|pending|downloaded|done|skipped
    updated_at TEXT
);
-- Download dedup + audit
CREATE TABLE IF NOT EXISTS downloads (
    dkey TEXT PRIMARY KEY,          -- sha1 of the source URL
    item_key TEXT,
    source_url TEXT,
    path TEXT,
    bytes INTEGER,
    sha256 TEXT,
    status TEXT,                    -- ok|failed|skipped
    ts TEXT
);
-- Scan audit
CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT,
    finished TEXT,
    scope TEXT,
    summary TEXT
);
"""


class DB:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- courses -------------------------------------------------------
    def upsert_courses(self, courses: list[Course]) -> None:
        self.conn.executemany(
            "INSERT INTO courses (id, name, url) VALUES (?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, url=excluded.url",
            [(c.id, c.name, c.url) for c in courses],
        )
        self.conn.commit()

    # ---- tasks ---------------------------------------------------------
    def upsert_task(self, task: Task) -> None:
        existing = self.get_task(task.cmid)
        # preserve agent status if already set
        status = existing["status"] if existing else task.status.value
        if task.is_submitted and status in (TaskStatus.NEW.value, TaskStatus.PENDING.value):
            status = TaskStatus.SUBMITTED.value
        ts = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            """INSERT INTO tasks
               (cmid,type,course_id,course_name,title,url,description,due_date,due_iso,
                submission_status,grading_status,status,discovered_at,updated_at,payload)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cmid) DO UPDATE SET
                 type=excluded.type, course_id=excluded.course_id, course_name=excluded.course_name,
                 title=excluded.title, url=excluded.url, description=excluded.description,
                 due_date=excluded.due_date, due_iso=excluded.due_iso,
                 submission_status=excluded.submission_status, grading_status=excluded.grading_status,
                 updated_at=excluded.updated_at, payload=excluded.payload""",
            (task.cmid, task.type.value, task.course_id, task.course_name, task.title, task.url,
             task.description, task.due_date, task.due_iso, task.submission_status,
             task.grading_status, status, task.discovered_at or ts, ts,
             json.dumps(task.to_dict(), ensure_ascii=False)),
        )
        self.conn.commit()

    def set_status(self, cmid: str, status: TaskStatus, detail: str = "") -> None:
        self.conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE cmid=?",
                          (status.value, datetime.now().isoformat(timespec="seconds"), cmid))
        self.conn.execute(
            "INSERT INTO history (cmid,event,detail,ts) VALUES (?,?,?,?)",
            (cmid, status.value, detail, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def get_task(self, cmid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tasks WHERE cmid=?", (cmid,)).fetchone()

    def all_tasks(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tasks ORDER BY due_iso IS NULL, due_iso, course_name"
        ).fetchall()

    def pending_tasks(self) -> list[sqlite3.Row]:
        """Rows whose full Task model reports is_pending."""
        import json as _json
        from core.models import Task as _Task
        out = []
        for r in self.all_tasks():
            try:
                task = _Task.from_dict(_json.loads(r["payload"]))
                # sync agent status from DB (payload may be stale)
                task.status = TaskStatus(r["status"])
            except Exception:
                continue
            if task.is_pending:
                out.append(r)
        return out

    def log(self, cmid: str, event: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO history (cmid,event,detail,ts) VALUES (?,?,?,?)",
            (cmid, event, detail, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    # ---- sections ------------------------------------------------------
    def upsert_section(self, key: str, course_id: str, sectionid: str,
                       name: str, position: int, summary: str = "") -> None:
        self.conn.execute(
            """INSERT INTO sections (key,course_id,sectionid,name,position,summary,updated_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 name=excluded.name, position=excluded.position,
                 summary=excluded.summary, updated_at=excluded.updated_at""",
            (key, course_id, sectionid, name, position, summary,
             datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def sections_for_course(self, course_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sections WHERE course_id=? ORDER BY position", (course_id,)
        ).fetchall()

    # ---- items ---------------------------------------------------------
    def upsert_item(self, item: dict) -> None:
        item = dict(item)
        item.setdefault("updated_at", datetime.now().isoformat(timespec="seconds"))
        cols = ["key", "course_id", "course_name", "section_key", "section_name",
                "item_type", "title", "url", "description", "file_url", "due_iso",
                "status", "updated_at"]
        vals = [item.get(c) for c in cols]
        self.conn.execute(
            f"""INSERT INTO items ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})
                ON CONFLICT(key) DO UPDATE SET
                  section_key=excluded.section_key, section_name=excluded.section_name,
                  title=excluded.title, url=excluded.url, description=excluded.description,
                  file_url=excluded.file_url, due_iso=excluded.due_iso,
                  status=excluded.status, updated_at=excluded.updated_at""",
            vals,
        )
        self.conn.commit()

    def items_for_course(self, course_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM items WHERE course_id=? ORDER BY section_key, item_type",
            (course_id,),
        ).fetchall()

    def items_with_files(self, course_id: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM items WHERE file_url IS NOT NULL AND file_url != ''"
        args: tuple = ()
        if course_id:
            q += " AND course_id=?"
            args = (course_id,)
        return self.conn.execute(q, args).fetchall()

    # ---- downloads (dedup) ---------------------------------------------
    @staticmethod
    def _dkey(url: str) -> str:
        import hashlib
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def download_seen(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM downloads WHERE dkey=? AND status='ok'", (self._dkey(url),)
        ).fetchone()

    def record_download(self, item_key: str, url: str, path: str,
                        nbytes: int = 0, sha256: str = "", status: str = "ok") -> None:
        self.conn.execute(
            """INSERT INTO downloads (dkey,item_key,source_url,path,bytes,sha256,status,ts)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(dkey) DO UPDATE SET
                 path=excluded.path, bytes=excluded.bytes, sha256=excluded.sha256,
                 status=excluded.status, ts=excluded.ts""",
            (self._dkey(url), item_key, url, path, nbytes, sha256, status,
             datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def download_stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b FROM downloads WHERE status='ok'"
        ).fetchone()
        return {"files": row["n"], "bytes": row["b"]}

    # ---- scan audit ----------------------------------------------------
    def start_scan(self, scope: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO scan_runs (started, scope) VALUES (?,?)",
            (datetime.now().isoformat(timespec="seconds"), scope),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_scan(self, run_id: int, summary: str) -> None:
        self.conn.execute(
            "UPDATE scan_runs SET finished=?, summary=? WHERE id=?",
            (datetime.now().isoformat(timespec="seconds"), summary, run_id),
        )
        self.conn.commit()

    def last_scan(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
