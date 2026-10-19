"""Interactive CLI for the UCN agent — human-in-the-loop review & submit.

Commands:
    list                 show pending tasks
    show <cmid>          show full detail of a task
    resolve <cmid>       ask the AI to propose a solution (agents/resolver)
    submit <cmid> <file> stage file(s) and submit (asks confirmation)
    skip <cmid>          mark a task as done manually
    scan                 re-crawl Moodle
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm

from core.models import TaskStatus
from core.parser import Parser
from core.session import MoodleSession
from core.submitter import Submitter
from storage.db import DB

console = Console()


# ----------------------------------------------------------------------
def cmd_list(args):
    db = DB()
    pending = db.pending_tasks()
    if not pending:
        console.print("[green]No hay tareas pendientes.[/green]")
        return
    t = Table(title=f"Tareas pendientes ({len(pending)})", show_lines=True)
    t.add_column("#", style="dim", width=3)
    t.add_column("cmid", style="dim")
    t.add_column("Curso", style="cyan", max_width=24)
    t.add_column("Tarea", max_width=32)
    t.add_column("Tipo", style="magenta", width=10)
    t.add_column("Vence", style="yellow", width=17)
    for i, r in enumerate(pending, 1):
        t.add_row(str(i), r["cmid"], (r["course_name"] or "")[:24], (r["title"] or "")[:32],
                  r["type"], str(r["due_iso"] or r["due_date"] or "—")[:17])
    console.print(t)


def cmd_show(args):
    db = DB()
    r = db.get_task(args.cmid)
    if not r:
        console.print(f"[red]No existe la tarea {args.cmid}[/red]")
        return
    payload = json.loads(r["payload"])
    body = (
        f"[bold]{r['title']}[/bold]\n"
        f"Curso: {r['course_name']}  ({r['course_id']})\n"
        f"Tipo: {r['type']}   cmid: {r['cmid']}\n"
        f"Vence: {r['due_iso'] or r['due_date'] or '—'}\n"
        f"Entrega: {r['submission_status'] or '—'}   Calificación: {r['grading_status'] or '—'}\n"
        f"URL: {r['url']}\n\n"
        f"[bold]Descripción:[/bold]\n{(r['description'] or '(sin descripción)')[:1500]}\n"
    )
    atts = payload.get("attachments") or []
    if atts:
        body += "\n[bold]Adjuntos:[/bold]\n" + "\n".join(f"  - {a}" for a in atts)
    console.print(Panel(body, title=f"Tarea {args.cmid}"))


def cmd_resolve(args):
    from agents.resolver import propose
    console.print("[cyan]Pidiendo propuesta a la IA...[/cyan]")
    result = propose(args.cmid, engine=args.engine)
    console.print(Panel(result["proposal"], title=f"Propuesta ({result['engine']})"))
    console.print(f"\n[dim]Guardada en: {result['saved']}[/dim]")


def cmd_submit(args):
    db = DB()
    r = db.get_task(args.cmid)
    if not r:
        console.print(f"[red]No existe la tarea {args.cmid}[/red]")
        return
    files = [Path(f) for f in args.files]
    for f in files:
        if not f.exists():
            console.print(f"[red]Archivo no encontrado: {f}[/red]")
            return

    ms = MoodleSession.from_env()
    ms.login()
    sub = Submitter(ms)

    # Show the dry-run plan first
    plan = sub.submit_assignment(args.cmid, files=files, dry_run=True)
    console.print(Panel(json.dumps(plan, indent=2, ensure_ascii=False),
                        title="Plan de envío (dry-run)"))

    if not Confirm.ask(f"[yellow]¿Enviar REALMENTE a {r['title']}?[/yellow]", default=False):
        console.print("[dim]Cancelado.[/dim]")
        return

    result = sub.submit_assignment(args.cmid, files=files, dry_run=False)
    console.print(Panel(json.dumps(result, indent=2, ensure_ascii=False), title="Resultado"))
    if result.get("ok"):
        db.set_status(args.cmid, TaskStatus.DONE, f"submitted files={[str(f) for f in files]}")
        console.print("[green]Marcada como DONE en la BD.[/green]")
    else:
        db.set_status(args.cmid, TaskStatus.FAILED, json.dumps(result)[:300])


def cmd_text(args):
    """Package a text answer into a PDF/DOCX and submit it (with confirmation)."""
    from core.packager import package_text

    db = DB()
    r = db.get_task(args.cmid)
    if not r:
        console.print(f"[red]No existe la tarea {args.cmid}[/red]")
        return

    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    else:
        console.print("[cyan]Pega la respuesta. Termina con una línea que diga solo 'EOF':[/cyan]")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == "EOF":
                break
            lines.append(line)
        text = "\n".join(lines)

    if not text.strip():
        console.print("[red]Texto vacío.[/red]")
        return

    author = os.environ.get("UCN_AUTHOR", "Roberto José Jarquín Romero")
    path = package_text(text, title=args.title or r["title"],
                        author=author, fmt=args.fmt)
    console.print(f"[green]Empaquetado en: {path}[/green]")

    ms = MoodleSession.from_env()
    ms.login()
    sub = Submitter(ms)
    plan = sub.submit_assignment(args.cmid, files=[path], dry_run=True)
    console.print(Panel(json.dumps(plan, indent=2, ensure_ascii=False, default=str),
                        title="Plan de envío (dry-run)"))

    if not Confirm.ask(f"[yellow]¿Enviar REALMENTE a {r['title']}?[/yellow]", default=False):
        console.print("[dim]Cancelado (el archivo quedó guardado).[/dim]")
        return

    result = sub.submit_assignment(args.cmid, files=[path], dry_run=False)
    console.print(Panel(json.dumps(result, indent=2, ensure_ascii=False, default=str),
                        title="Resultado"))
    if result.get("ok"):
        db.set_status(args.cmid, TaskStatus.DONE, f"submitted text -> {path.name}")
        console.print("[green]Marcada como DONE.[/green]")
    else:
        db.set_status(args.cmid, TaskStatus.FAILED, json.dumps(result)[:300])


def cmd_skip(args):
    db = DB()
    db.set_status(args.cmid, TaskStatus.SKIPPED, "manual")
    console.print(f"[green]{args.cmid} marcada como SKIPPED.[/green]")


def cmd_scan(args):
    from scan import run_scan
    run_scan(enrich_pending=True)


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(prog="ucn", description="UCN Moodle agent")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)
    sub.add_parser("scan").set_defaults(func=cmd_scan)

    p = sub.add_parser("show"); p.add_argument("cmid"); p.set_defaults(func=cmd_show)
    p = sub.add_parser("resolve"); p.add_argument("cmid")
    p.add_argument("--engine", default="auto"); p.set_defaults(func=cmd_resolve)
    p = sub.add_parser("submit"); p.add_argument("cmid"); p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_submit)
    p = sub.add_parser("text"); p.add_argument("cmid")
    p.add_argument("--file", help="read the answer from this text file (else paste stdin)")
    p.add_argument("--title", default="")
    p.add_argument("--fmt", default="pdf", choices=["pdf", "docx"])
    p.set_defaults(func=cmd_text)
    p = sub.add_parser("skip"); p.add_argument("cmid"); p.set_defaults(func=cmd_skip)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
