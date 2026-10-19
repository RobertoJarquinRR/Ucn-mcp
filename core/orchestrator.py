"""Orchestrator: the full daily flow, and the query helpers the agent uses.

    run_daily()  ->  deep scan every course, download new material (dedup via DB),
                     build a report, drop the Desktop notice, record the run.

Query helpers (fast, read from the local DB; no network):
    course_overview(query)      -> sections + items for a course (fuzzy name match)
    pending_report()            -> pending tasks grouped by course
    week_report(course, week)   -> everything in one week
    find_downloaded(query)      -> files already on disk matching a text query
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from core.deep_scraper import DeepScraper
from core.library import DOCS_ROOT, Library
from core.models import Course
from core.scraper import Scraper
from core.session import MoodleSession
from storage.db import DB

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------
def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", " ", s.lower())


def _match_course(db: DB, query: str):
    """Fuzzy match a course by name (or code). Returns (course_row, candidates)."""
    q = _norm(query)
    rows = db.conn.execute("SELECT * FROM courses").fetchall()
    if not q:
        return None, rows
    # exact-ish: all tokens present
    toks = [t for t in q.split() if t]
    hits = [r for r in rows if all(t in _norm(r["name"]) for t in toks)]
    if not hits:
        hits = [r for r in rows if any(t in _norm(r["name"]) for t in toks)]
    return (hits[0] if hits else None), hits


# ----------------------------------------------------------------------
def run_daily(deep: bool = True, download: bool = True,
              write_desktop: bool = True) -> dict:
    """Full daily run. Returns a summary dict."""
    db = DB()
    run_id = db.start_scan("daily-deep" if deep else "daily")
    started = datetime.now()
    report: dict = {"started": started.isoformat(timespec="seconds")}

    ms = MoodleSession.from_env()
    ms.login()

    scraper = Scraper(ms)
    courses = scraper.discover_courses()
    db.upsert_courses(courses)

    # 1. shallow task scan (keeps the pending detection fresh)
    from core.parser import Parser
    parser = Parser(ms)
    all_tasks = []
    for c in courses:
        try:
            all_tasks.extend(scraper.scrape_course_activities(c))
        except Exception:
            pass
    for t in all_tasks:
        db.upsert_task(t)
    for t in all_tasks:
        try:
            parser.enrich(t)
            db.upsert_task(t)
        except Exception:
            pass

    # 2. deep scan (sections + all item types)
    deep_stats = []
    if deep:
        ds = DeepScraper(ms)
        for c in courses:
            try:
                deep_stats.append(ds.scan_course(c, enrich=False))
            except Exception as e:
                deep_stats.append({"course": c.name, "error": str(e)})
    report["deep"] = deep_stats

    # 3. downloads (dedup: already-downloaded files are skipped)
    dl = {"ok": 0, "cached": 0, "failed": 0}
    if download:
        lib = Library()
        for c in courses:
            r = lib.download_course_files(ms.session, c.id)
            for k in ("ok", "cached", "failed"):
                dl[k] = dl.get(k, 0) + r.get(k, 0)
    report["downloads"] = dl

    # 4. report + desktop notice
    summary_md = build_report_md()
    report["report_md"] = summary_md
    if write_desktop:
        from core.notifier import write_notice
        report["desktop"] = write_notice(summary_md)

    db.finish_scan(run_id, json.dumps({"downloads": dl,
                                       "courses": len(courses)}, ensure_ascii=False))
    report["finished"] = datetime.now().isoformat(timespec="seconds")
    return report


# ----------------------------------------------------------------------
def build_report_md() -> str:
    db = DB()
    day = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# UCN — Informe {day}", ""]

    pending = db.pending_tasks()
    lines.append(f"## Pendientes ({len(pending)})")
    if not pending:
        lines.append("_Nada pendiente._")
    else:
        by = defaultdict(list)
        for r in pending:
            by[r["course_name"] or r["course_id"]].append(r)
        for course, items in sorted(by.items()):
            lines.append(f"### {course}")
            for r in sorted(items, key=lambda x: x["due_iso"] or "9"):
                due = r["due_iso"] or r["due_date"] or "sin fecha"
                lines.append(f"- **{r['title']}** — vence {due}  (`{r['cmid']}`)")
            lines.append("")

    # Materials per course, by week
    lines.append("## Material por curso y semana")
    for c in db.conn.execute("SELECT id, name FROM courses ORDER BY name"):
        rows = db.items_for_course(c["id"])
        if not rows:
            continue
        lines.append(f"### {c['name']}")
        weeks = defaultdict(lambda: defaultdict(int))
        for r in rows:
            weeks[r["section_name"] or "?"][r["item_type"]] += 1
        for sec, kinds in weeks.items():
            desc = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
            lines.append(f"- {sec}: {desc}")
        lines.append("")

    return "\n".join(lines)


# ----------------------------------------------------------------------
def refresh_notice(extra_md: str = "") -> dict:
    """Rebuild the daily report and refresh the Desktop notice.

    Call this after ANY download so the user always sees the aviso.
    `extra_md` is prepended (e.g. a task brief)."""
    from core.notifier import write_notice
    md = build_report_md()
    if extra_md:
        md = extra_md.rstrip() + "\n\n---\n\n" + md
    return write_notice(md)


def prepare_task_brief(cmids: list[str]) -> dict:
    """Build a working brief (what to do) for the given tasks, save it into the
    day folder, and refresh the Desktop notice. This is the 'prepare them so I
    can go do them' flow."""
    db = DB()
    day = datetime.now().strftime("%Y-%m-%d")
    lib = Library()

    lines = [f"# Guía de trabajo — {day}", ""]
    found = 0
    for cmid in cmids:
        r = db.get_task(cmid)
        if not r:
            lines.append(f"## ⚠️ tarea desconocida: {cmid}\n")
            continue
        found += 1
        payload = json.loads(r["payload"])
        lines.append(f"## {r['title']}")
        lines.append(f"- **Materia**: {r['course_name']}")
        lines.append(f"- **Vence**: {r['due_iso'] or r['due_date'] or 'sin fecha'}")
        lines.append(f"- **cmid**: {cmid}")
        if r["description"]:
            lines.append(f"- **Qué pide**: {r['description'][:1200]}")
        atts = payload.get("attachments") or []
        if atts:
            lines.append("- **Documentos**:")
            for a in atts:
                name = a.split(" | ")[0]
                lines.append(f"  - {name}")
        lines.append("")

    lines.append("## Checklist")
    for i, cmid in enumerate(cmids, 1):
        lines.append(f"- [ ] Tarea {i} ({cmid})")

    md = "\n".join(lines)

    # Save the brief into the day folder
    out_dir = lib.day_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"GUIA-{day}.md"
    path.write_text(md, encoding="utf-8")

    notice = refresh_notice(extra_md=md)
    return {"brief": str(path), "tasks": found, "notice": notice}


# ----------------------------------------------------------------------
def course_overview(query: str) -> dict:
    db = DB()
    row, hits = _match_course(db, query)
    if not row:
        return {"error": f"sin coincidencias para {query!r}",
                "courses": [r["name"] for r in hits]}
    rows = db.items_for_course(row["id"])
    by_week = defaultdict(list)
    for r in rows:
        by_week[r["section_name"] or "?"].append({
            "type": r["item_type"], "title": r["title"],
            "due": r["due_iso"], "cmid": r["key"],
            "file": bool(r["file_url"]),
        })
    return {"course": row["name"], "course_id": row["id"], "weeks": dict(by_week)}


def week_report(query: str, week: str) -> dict:
    db = DB()
    row, _ = _match_course(db, query)
    if not row:
        return {"error": f"materia no encontrada: {query}"}
    w = _norm(week)
    rows = [r for r in db.items_for_course(row["id"])
            if w in _norm(r["section_name"])]
    return {"course": row["name"], "week": week,
            "items": [{"type": r["item_type"], "title": r["title"],
                       "cmid": r["key"], "file": bool(r["file_url"])} for r in rows]}


def find_downloaded(query: str) -> list[dict]:
    """Search downloaded files (paths + extracted text) for a term."""
    import unicodedata
    db = DB()
    q = _norm(query)
    out = []
    for r in db.conn.execute("SELECT * FROM downloads WHERE status='ok'"):
        p = Path(r["path"] or "")
        if not p.exists():
            continue
        txt = ""
        t = Path(str(p) + ".txt")
        if t.exists():
            try:
                txt = t.read_text(encoding="utf-8", errors="replace")
            except Exception:
                txt = ""
        if q in _norm(p.name) or q in _norm(txt):
            out.append({"file": str(p), "bytes": r["bytes"],
                        "snippet": (txt[:200] if txt else "")})
    return out
