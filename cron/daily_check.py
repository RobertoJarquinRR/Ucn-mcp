"""Daily check: scan Moodle, report NEW pending tasks, optionally notify.

Run manually:
    python cron/daily_check.py

Schedule (Windows Task Scheduler), every day at 06:00:
    schtasks /create /tn "UCN Agent Daily" /tr "C:\\Proyects\\ucn-agent\\.venv\\Scripts\\python.exe C:\\Proyects\\ucn-agent\\cron\\daily_check.py" /sc daily /st 06:00
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scan import run_scan
from storage.db import DB

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = PROJECT_ROOT / "storage" / "reports"


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Fresh crawl
    stats = run_scan(enrich_pending=True)

    # 2. Pending tasks
    db = DB()
    pending = db.pending_tasks()

    # 3. Build report
    lines = [f"UCN Agent — reporte diario {datetime.now():%Y-%m-%d %H:%M}", "=" * 60, ""]
    if not pending:
        lines.append("No hay tareas pendientes. 🎉")
    else:
        lines.append(f"{len(pending)} tareas pendientes:\n")
        for i, r in enumerate(pending, 1):
            due = r["due_iso"] or r["due_date"] or "sin fecha"
            lines.append(f"{i}. [{r['course_name']}] {r['title']}")
            lines.append(f"   vence: {due}  |  tipo: {r['type']}  |  cmid: {r['cmid']}")
            lines.append(f"   {r['url']}")
    report = "\n".join(lines)

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out = REPORT_DIR / f"daily_{ts}.txt"
    out.write_text(report, encoding="utf-8")
    (REPORT_DIR / "latest.txt").write_text(report, encoding="utf-8")

    print(report)
    print(f"\n[reported -> {out}]")

    # 4. Notification hook (optional). Wire to Telegram/email here.
    _notify(report)


def _notify(report: str) -> None:
    """Optional: send `report` to a messaging channel.

    Left as a hook — configure as you prefer:
      - Telegram bot  (requests.post to api.telegram.org)
      - email         (the himalaya skill)
    Enable by setting UCN_NOTIFY=1 and the relevant credentials in .env.
    """
    import os
    if os.environ.get("UCN_NOTIFY") != "1":
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    import requests
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat, "text": report[:4000]},
        timeout=30,
    )


if __name__ == "__main__":
    main()
