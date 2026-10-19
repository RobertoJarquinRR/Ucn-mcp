"""MCP server exposing the UCN Moodle agent as tools.

Run standalone:  python mcp_server.py
Registered with Hermes via:  hermes mcp add ucn --command <python> --args mcp_server.py

IMPORTANT: MCP stdio servers communicate over stdout. NOTHING may be printed to
stdout except the protocol. All our modules print progress — so we redirect
stdout to stderr for the whole process.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp.server.fastmcp import FastMCP

from core.models import TaskStatus
from core.parser import Parser
from core.scraper import Scraper
from core.session import MoodleSession
from storage.db import DB

mcp = FastMCP("ucn-agent")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _silent(fn, *args, **kwargs):
    """Run fn while swallowing stdout (keeps the MCP channel clean)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return fn(*args, **kwargs)


def _task_brief(r) -> dict:
    return {
        "cmid": r["cmid"],
        "course": r["course_name"] or r["course_id"],
        "title": r["title"],
        "type": r["type"],
        "due": r["due_iso"] or r["due_date"],
        "status": r["submission_status"],
        "grading": r["grading_status"],
        "url": r["url"],
    }


# ----------------------------------------------------------------------
# tools
# ----------------------------------------------------------------------
@mcp.tool()
def list_pending_tasks() -> str:
    """List all pending (unsubmitted, not graded) UCN Moodle tasks.

    Returns a JSON array of tasks with cmid, course, title, due date and status.
    """
    db = DB()
    rows = db.pending_tasks()
    return json.dumps([_task_brief(r) for r in rows], ensure_ascii=False, indent=2)


@mcp.tool()
def get_task(cmid: str) -> str:
    """Get the full detail of one task by its cmid: description, due date,
    submission status, grading status and instruction attachments."""
    db = DB()
    r = db.get_task(cmid)
    if not r:
        return json.dumps({"error": f"no task with cmid {cmid}"})
    payload = json.loads(r["payload"])
    return json.dumps({
        **_task_brief(r),
        "description": r["description"],
        "attachments": payload.get("attachments") or [],
        "extra": payload.get("extra") or {},
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def scan_courses() -> str:
    """Re-crawl UCN Moodle now: refresh all courses and tasks in the database.
    Use this before listing when the data may be stale."""
    def _run():
        from scan import run_scan
        return run_scan(enrich_pending=True)
    stats = _silent(_run)
    return json.dumps({"scanned": stats}, ensure_ascii=False)


@mcp.tool()
def propose_solution(cmid: str) -> str:
    """Ask the AI to propose a solution/answer for the task with the given cmid.
    Saves the proposal to storage/artifacts/ and returns it."""
    try:
        from agents.resolver import propose
        result = _silent(propose, cmid)
        return json.dumps({
            "engine": result["engine"],
            "saved": result["saved"],
            "proposal": result["proposal"],
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "error": f"No se pudo generar la propuesta: {e}",
            "hint": "Configura OPENROUTER_API_KEY en C:/Proyects/ucn-agent/.env "
                    "para usar el motor openrouter (rápido y fiable).",
        }, ensure_ascii=False)


@mcp.tool()
def skip_task(cmid: str) -> str:
    """Mark a task as done manually (the user did it themselves) so it stops
    appearing in the pending list."""
    db = DB()
    if not db.get_task(cmid):
        return json.dumps({"error": f"no task with cmid {cmid}"})
    db.set_status(cmid, TaskStatus.SKIPPED, "manual via MCP")
    return json.dumps({"ok": True, "cmid": cmid, "status": "skipped"})


@mcp.tool()
def submit_task(cmid: str, files: list[str], confirm: bool = False) -> str:
    """Submit an assignment with the given local file paths.

    SAFETY: with confirm=False (default) this only does a DRY RUN — it uploads
    the files to Moodle's private draft area and returns the plan WITHOUT
    submitting. Only pass confirm=True after the user has explicitly approved
    the real submission.
    """
    from core.submitter import Submitter
    db = DB()
    r = db.get_task(cmid)
    if not r:
        return json.dumps({"error": f"no task with cmid {cmid}"})

    paths = [Path(f) for f in files]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        return json.dumps({"error": "files not found", "missing": missing})

    ms = MoodleSession.from_env()
    ms.login()
    sub = Submitter(ms)

    result = _silent(sub.submit_assignment, cmid, files=paths, dry_run=not confirm)

    if confirm and result.get("ok"):
        db.set_status(cmid, TaskStatus.DONE, f"submitted via MCP: {[str(p) for p in paths]}")
    elif confirm and not result.get("ok"):
        db.set_status(cmid, TaskStatus.FAILED, json.dumps(result)[:300])

    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def list_courses() -> str:
    """List all enrolled UCN Moodle courses (id + name)."""
    db = DB()
    rows = db.conn.execute("SELECT id, name, url FROM courses ORDER BY name").fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2)


@mcp.tool()
def draft_submission(cmid: str, text: str, title: str = "", fmt: str = "pdf") -> str:
    """Package the user's plain-text answer into a submittable file (PDF or DOCX)
    WITHOUT submitting. Returns the generated file path so the user can review it.
    Use this first when the user gives you their answer as text."""
    from core.packager import package_text
    db = DB()
    r = db.get_task(cmid)
    if not r:
        return json.dumps({"error": f"no task with cmid {cmid}"})
    author = os.environ.get("UCN_AUTHOR", "Roberto José Jarquín Romero")
    path = _silent(package_text, text, title or r["title"], author, fmt)
    return json.dumps({
        "ok": True,
        "cmid": cmid,
        "file": str(path),
        "bytes": path.stat().st_size,
        "next": "Review the file, then call submit_text_task with confirm=true to send it.",
    }, ensure_ascii=False)


@mcp.tool()
def submit_text_task(cmid: str, text: str, title: str = "", fmt: str = "pdf",
                     confirm: bool = False) -> str:
    """Submit the user's answer given as PLAIN TEXT. The text is packaged into a
    PDF (or DOCX if fmt='docx') and uploaded to the assignment.

    SAFETY: with confirm=False (default) this is a DRY RUN — it packages the text,
    uploads to Moodle's private draft area, and returns the plan WITHOUT submitting.
    Only set confirm=True after the user has explicitly approved."""
    from core.packager import package_text
    from core.submitter import Submitter

    db = DB()
    r = db.get_task(cmid)
    if not r:
        return json.dumps({"error": f"no task with cmid {cmid}"})

    author = os.environ.get("UCN_AUTHOR", "Roberto José Jarquín Romero")
    path = _silent(package_text, text, title or r["title"], author, fmt)

    ms = MoodleSession.from_env()
    ms.login()
    sub = Submitter(ms)
    result = _silent(sub.submit_assignment, cmid, files=[path], dry_run=not confirm)
    result["packaged_file"] = str(path)

    if confirm and result.get("ok"):
        db.set_status(cmid, TaskStatus.DONE, f"submitted text via MCP -> {path.name}")
    elif confirm and not result.get("ok"):
        db.set_status(cmid, TaskStatus.FAILED, json.dumps(result)[:300])

    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def scan_course_deep(course: str) -> str:
    """Deep-scan ONE course: walk every week/section and catalogue all items
    (tasks, forums, files, links, headings). Use when the user asks about a
    specific subject ('revisa diseño gráfico', 'qué hay esta semana')."""
    from core.deep_scraper import DeepScraper
    from core.orchestrator import _match_course

    db = DB()
    row, hits = _match_course(db, course)
    if not row:
        return json.dumps({"error": f"materia no encontrada: {course}",
                           "available": [r["name"] for r in hits]},
                          ensure_ascii=False)
    ms = MoodleSession.from_env(); ms.login()
    res = _silent(DeepScraper(ms).scan_course,
                  Course(id=row["id"], name=row["name"]))
    return json.dumps(res, ensure_ascii=False)


@mcp.tool()
def course_overview(course: str) -> str:
    """Show everything in a course organised BY WEEK (tasks, files, links,
    headings). Fast — reads the local database, no crawling."""
    from core.orchestrator import course_overview as _ov
    return json.dumps(_ov(course), ensure_ascii=False)


@mcp.tool()
def week_report(course: str, week: str) -> str:
    """Show everything inside one week/section of a course.
    Example: week_report('diseño', 'semana 12')."""
    from core.orchestrator import week_report as _wk
    return json.dumps(_wk(course, week), ensure_ascii=False)


@mcp.tool()
def download_course_materials(course: str, force: bool = False) -> str:
    """Download every file of a course into the dated Documents tree
    (dedup: files already downloaded are skipped). Refreshes the Desktop notice."""
    from core.library import Library
    from core.orchestrator import _match_course, refresh_notice

    db = DB()
    row, hits = _match_course(db, course)
    if not row:
        return json.dumps({"error": f"materia no encontrada: {course}"}, ensure_ascii=False)
    ms = MoodleSession.from_env(); ms.login()
    res = _silent(Library().download_course_files, ms.session, row["id"], force=force)
    notice = _silent(refresh_notice)
    return json.dumps({"course": row["name"], **res, "desktop_notice": notice.get("created")},
                      ensure_ascii=False, default=str)


@mcp.tool()
def download_task_attachments(cmid: str, force: bool = False) -> str:
    """Download the instruction documents attached to ONE task (by cmid).
    Refreshes the Desktop notice so the user sees it."""
    from core.library import Library
    from core.orchestrator import refresh_notice

    db = DB()
    r = db.get_task(cmid)
    if not r:
        return json.dumps({"error": f"tarea no encontrada: {cmid}"}, ensure_ascii=False)
    payload = json.loads(r["payload"])
    atts = payload.get("attachments") or []

    ms = MoodleSession.from_env(); ms.login()
    lib = Library()
    out = []
    for att in atts:
        # stored as "name | url"
        if " | " not in att:
            continue
        name, url = att.split(" | ", 1)
        item = {"key": cmid, "file_url": url.strip(), "title": name.strip(),
                "course_name": r["course_name"], "section_name": "Adjuntos de tarea"}
        out.append(_silent(lib.download_item, ms.session, item, force=force))
    notice = _silent(refresh_notice)
    return json.dumps({"task": r["title"], "results": out,
                       "desktop_notice": notice.get("created")},
                      ensure_ascii=False, default=str)


@mcp.tool()
def prepare_tasks(cmids: list[str]) -> str:
    """Prepare a working brief for the given tasks: builds a 'GUIA-<fecha>.md'
    with what each task asks for (description, due date, documents, checklist),
    saves it in the day folder and refreshes the Desktop notice.
    Use this for 'prepárame las tareas para hacerlas'."""
    from core.orchestrator import prepare_task_brief
    res = _silent(prepare_task_brief, cmids)
    return json.dumps(res, ensure_ascii=False, default=str)


@mcp.tool()
def find_materials(query: str) -> str:
    """Search already-downloaded files by filename OR by their extracted text
    (PDF/DOCX). Use to answer '¿dónde está el documento de X?'."""
    from core.orchestrator import find_downloaded
    return json.dumps(find_downloaded(query), ensure_ascii=False, default=str)


@mcp.tool()
def run_daily_scan(deep: bool = True, download: bool = True) -> str:
    """Run the full daily flow now: deep-scan every course, download new
    material, rebuild the report and refresh the Desktop notice."""
    from core.orchestrator import run_daily
    res = _silent(run_daily, deep=deep, download=download, write_desktop=True)
    return json.dumps({k: v for k, v in res.items() if k != "report_md"},
                      ensure_ascii=False, default=str)


@mcp.tool()
def safe_delete(paths: list[str], note: str = "") -> str:
    """Move files/folders to a recycle bin folder on the Desktop instead of
    deleting them. NEVER delete directly — the user purges manually."""
    import shutil
    from datetime import datetime as _dt

    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    desktop_bin = os.environ.get("UCN_DESKTOP_BIN", r"C:\Users\Rober\Desktop\UCN-Papelera")
    bin_dir = Path(desktop_bin) / ts
    moved, missing = [], []
    for p in paths:
        src = Path(p)
        if not src.exists():
            missing.append(p)
            continue
        bin_dir.mkdir(parents=True, exist_ok=True)
        dest = bin_dir / src.name
        i = 1
        while dest.exists():
            dest = bin_dir / f"{src.stem}_{i}{src.suffix}"
            i += 1
        try:
            shutil.move(str(src), str(dest))
            moved.append({"from": str(src), "to": str(dest)})
        except Exception as e:
            missing.append(f"{p}: {e}")
    if note:
        (bin_dir / "_MOTIVO.txt").write_text(note, encoding="utf-8")
    return json.dumps({"recycle_bin": str(bin_dir), "moved": moved, "issues": missing},
                      ensure_ascii=False, default=str)


if __name__ == "__main__":
    mcp.run()