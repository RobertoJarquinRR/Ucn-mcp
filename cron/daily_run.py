"""Daily deep run — intended to be executed by Windows Task Scheduler at ~05:00.

    python cron/daily_run.py

It performs the full flow (deep scan of every course, dedup downloads, report,
Desktop notice) and writes a log line to storage/logs/daily.log.

Register the scheduled task (runs as your user, so Documents stays writable):

    schtasks /create /tn "UCN Agent Daily" /sc daily /st 05:00 /f ^
      /tr "\"C:\\Proyects\\ucn-agent\\.venv\\Scripts\\python.exe\" \"C:\\Proyects\\ucn-agent\\cron\\daily_run.py\""
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "storage" / "logs"


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = LOG_DIR / "daily.log"
    stamp = datetime.now().isoformat(timespec="seconds")

    try:
        from core.orchestrator import run_daily
        report = run_daily(deep=True, download=True, write_desktop=True)

        # Write the plain-text report too
        from core.orchestrator import build_report_md
        (PROJECT_ROOT / "storage" / "reports").mkdir(parents=True, exist_ok=True)
        (PROJECT_ROOT / "storage" / "reports" / "latest.md").write_text(
            build_report_md(), encoding="utf-8")

        line = (f"[{stamp}] OK deep={len(report.get('deep', []))} "
                f"downloads={report.get('downloads')} "
                f"desktop={'ok' if report.get('desktop') else 'skip'}")
        print(line)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return 0
    except Exception as e:
        err = f"[{stamp}] FAIL {e}\n{traceback.format_exc()}"
        print(err)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(err + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
