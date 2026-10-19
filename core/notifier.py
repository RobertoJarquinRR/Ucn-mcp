"""Desktop notice: leave a visible summary + shortcut on the user's Desktop.

The user is forgetful but always sees the Desktop, so each run drops:
    Desktop/UCN-Tareas/LEEME.txt                 (explains the folder)
    Desktop/UCN-Tareas/<date> resumen.md         (the day's summary)
    Desktop/UCN-Tareas/<date> documentos.lnk     (shortcut to the day's folder)
All best-effort: if any step fails the run still succeeds (source of truth is
the Documents tree + SQLite).
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from core.library import DESKTOP, DOCS_ROOT, Library

NOTICE_DIR = DESKTOP / "UCN-Tareas"

README = """UCN Agente — carpeta de avisos
================================

Aquí el agente deja, cada día:
  · "<fecha> resumen.md"       — resumen de lo pendiente y lo descargado
  · "<fecha> documentos.lnk"   — acceso directo a los archivos de ese día

Los archivos reales viven en:
  %s\\<fecha>\\<Materia>\\<Semana>\\

Esta carpeta es solo el aviso. Nada aquí se borra automáticamente.
""" % str(DOCS_ROOT)


def _shortcut(lnk_path: Path, target: Path, description: str = "") -> bool:
    """Create a Windows .lnk via PowerShell (best-effort)."""
    try:
        ps = (
            "$ws = New-Object -ComObject WScript.Shell; "
            f"$sc = $ws.CreateShortcut('{lnk_path}'); "
            f"$sc.TargetPath = '{target}'; "
            f"$sc.Description = '{description}'; "
            "$sc.Save()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=45)
        return lnk_path.exists()
    except Exception:
        return False


def write_notice(summary_md: str, day: str | None = None,
                 day_folder: Path | None = None) -> dict:
    """Drop the day's notice + shortcut on the Desktop. Returns what it did."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    day_folder = day_folder or Library(day).day_dir()
    out: dict = {"notice_dir": str(NOTICE_DIR), "created": []}

    try:
        NOTICE_DIR.mkdir(parents=True, exist_ok=True)
        readme = NOTICE_DIR / "LEEME.txt"
        if not readme.exists():
            readme.write_text(README, encoding="utf-8")
            out["created"].append(str(readme))

        resumen = NOTICE_DIR / f"{day} resumen.md"
        resumen.write_text(summary_md, encoding="utf-8")
        out["created"].append(str(resumen))

        # Extensionless "aviso" file — some prefer grabbing it without an app
        aviso = NOTICE_DIR / day
        aviso.write_text(summary_md, encoding="utf-8")
        out["created"].append(str(aviso))

        if day_folder.exists():
            lnk = NOTICE_DIR / f"{day} documentos.lnk"
            if _shortcut(lnk, day_folder, "Documentos UCN del día"):
                out["created"].append(str(lnk))
            else:
                out["shortcut_failed"] = True
    except Exception as e:
        out["error"] = str(e)

    return out
