"""Main scan: crawl Moodle, enrich tasks, save to DB, print pending list.

Usage:
    python scan.py            # full scan
    python scan.py --brief    # only print summary
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.models import TaskStatus
from core.parser import Parser
from core.scraper import Scraper
from core.session import MoodleSession
from storage.db import DB


def run_scan(enrich_pending: bool = True) -> dict:
    print(f"=== UCN scan @ {datetime.now():%Y-%m-%d %H:%M} ===\n")

    ms = MoodleSession.from_env()
    print("Logging in...")
    ms.login()
    print("OK\n")

    scraper = Scraper(ms)
    parser = Parser(ms)
    db = DB()

    print("Discovering courses...")
    courses, tasks = scraper.scrape_all()
    db.upsert_courses(courses)
    print(f"\nTotal submittable activities: {len(tasks)}")

    # Save raw tasks first
    for t in tasks:
        db.upsert_task(t)

    # Enrich tasks: fetch details. Always refresh tasks that are not yet
    # completed/graded (their status can change day to day).
    enriched = 0
    if enrich_pending:
        print("\nEnriching tasks (fetching details)...")
        for t in tasks:
            row = db.get_task(t.cmid)
            if row:
                has_detail = bool(row["submission_status"] or row["due_date"])
                completed = (row["status"] in ("graded", "skipped"))
                # skip only if we already have detail AND it's terminal
                if has_detail and completed:
                    continue
                if has_detail and row["status"] == "done":
                    continue
            try:
                parser.enrich(t)
                db.upsert_task(t)
                enriched += 1
            except Exception as e:
                print(f"  ! {t.cmid} {t.title[:40]}: {e}")
    print(f"Enriched {enriched} tasks")

    pending = db.pending_tasks()
    db.log("scan", "full_scan", f"pending={len(pending)} total={len(tasks)}")

    return {
        "courses": len(courses),
        "tasks": len(tasks),
        "pending": len(pending),
    }


def print_pending() -> None:
    from rich.console import Console
    from rich.table import Table

    db = DB()
    pending = db.pending_tasks()
    console = Console()

    if not pending:
        console.print("[green]No hay tareas pendientes.[/green]")
        return

    table = Table(title=f"Tareas pendientes ({len(pending)})", show_lines=True)
    table.add_column("#", style="dim", width=3)
    table.add_column("Curso", style="cyan", max_width=28)
    table.add_column("Tarea", style="white", max_width=34)
    table.add_column("Tipo", style="magenta", width=10)
    table.add_column("Vence", style="yellow", width=17)

    for i, r in enumerate(pending, 1):
        due = r["due_iso"] or r["due_date"] or "—"
        table.add_row(
            str(i),
            (r["course_name"] or r["course_id"])[:28],
            (r["title"] or "?")[:34],
            r["type"],
            str(due)[:17],
        )
    console.print(table)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-enrich", action="store_true", help="skip fetching task details")
    ap.add_argument("--brief", action="store_true", help="summary only")
    args = ap.parse_args()

    stats = run_scan(enrich_pending=not args.no_enrich)
    print(f"\n=== Done: {stats} ===")
    if not args.brief:
        print_pending()


if __name__ == "__main__":
    main()
