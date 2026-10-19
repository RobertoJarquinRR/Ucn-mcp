"""Document library: download course materials into a dated folder tree.

Layout (inside the user's Documents, safe for scheduled tasks):
    <DOCS_ROOT>/UCN-Agent/<YYYY-MM-DD>/<Materia>/<Semana>/<archivo>

Every download is recorded in the SQLite `downloads` table keyed by the source
URL hash, so a repeated scan never re-downloads the same file. Text is extracted
from PDF/DOCX/TXT so the agent can answer questions without opening the file.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime
from pathlib import Path

from storage.db import DB

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Primary destination: the user's Documents folder (always writable by the
# scheduled task running as the user; never needs elevation).
DOCS_ROOT = Path(os.environ.get("UCN_DOCS_ROOT", r"C:\Users\Rober\Documents")) / "UCN-Agent"
# Desktop notice area
DESKTOP = Path(os.environ.get("UCN_DESKTOP", r"C:\Users\Rober\Desktop"))
DESKTOP_NOTICE_DIR = DESKTOP / "UCN-Tareas"


def _safe(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\w\s\.\-áéíóúÁÉÍÓÚñÑüÜ]", "", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:maxlen] or "sin-nombre").strip(". ")


def short_course_name(name: str) -> str:
    """'Diseño Gráfico II - Regular Grupo 1 - LAB-INF2P - Campus ... IIC2026'
    -> 'Diseño Gráfico II'. Used for folder names so a course never creates two
    different folders (short vs full name)."""
    base = re.split(r"\s+-\s+", (name or "").strip(), maxsplit=1)[0].strip()
    return base or (name or "sin-materia").strip()


def _extract_text(path: Path) -> str:
    """Best-effort text extraction for later querying."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            return "\n".join((pg.extract_text() or "") for pg in reader.pages)[:200_000]
        if suffix in (".docx", ".doc"):
            import docx
            d = docx.Document(str(path))
            return "\n".join(p.text for p in d.paragraphs)[:200_000]
        if suffix in (".txt", ".md", ".csv", ".html"):
            return path.read_text(encoding="utf-8", errors="replace")[:200_000]
    except Exception:
        pass
    return ""


class Library:
    def __init__(self, day: str | None = None):
        self.db = DB()
        self.day = day or datetime.now().strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    def day_dir(self) -> Path:
        return DOCS_ROOT / self.day

    def target_path(self, course_name: str, section_name: str, filename: str) -> Path:
        return (self.day_dir() / _safe(short_course_name(course_name)) / _safe(section_name, 40)
                / _safe(filename, 80))

    # ------------------------------------------------------------------
    def download_item(self, session, item, force: bool = False) -> dict:
        """Download one item's file. Returns a result dict.

        `item` is a DB row (or dict) with file_url, course_name, section_name, key, title.
        """
        url = item["file_url"] if not isinstance(item, dict) else item.get("file_url")
        if not url:
            return {"status": "skipped", "reason": "no file_url"}

        # Dedup: already downloaded successfully?
        if not force:
            seen = self.db.download_seen(url)
            if seen and Path(seen["path"]).exists():
                return {"status": "cached", "path": seen["path"], "bytes": seen["bytes"]}

        course = item["course_name"] if not isinstance(item, dict) else item.get("course_name")
        section = item["section_name"] if not isinstance(item, dict) else item.get("section_name")
        title = item["title"] if not isinstance(item, dict) else item.get("title")
        key = item["key"] if not isinstance(item, dict) else item.get("key")

        # Filename: prefer the Content-Disposition name, else the title + ext
        try:
            r = session.get(url, timeout=120, stream=True)
            r.raise_for_status()
            cdisp = r.headers.get("Content-Disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cdisp)
            if m:
                from urllib.parse import unquote
                raw = unquote(m.group(1))
                # requests decodes headers as latin-1; repair UTF-8 filenames
                try:
                    raw = raw.encode("latin-1").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
                fname = raw
            else:
                fname = Path(url.split("?")[0]).name or f"{_safe(title, 60)}.bin"

            dest = self.target_path(course or "?", section or "?", fname)
            dest.parent.mkdir(parents=True, exist_ok=True)

            h = hashlib.sha256()
            n = 0
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
                        h.update(chunk)
                        n += len(chunk)

            text = _extract_text(dest)
            if text:
                (dest.parent / (dest.name + ".txt")).write_text(text, encoding="utf-8")

            self.db.record_download(key, url, str(dest), n, h.hexdigest(), "ok")
            return {"status": "ok", "path": str(dest), "bytes": n,
                    "text_chars": len(text)}
        except Exception as e:
            self.db.record_download(key or "?", url, "", 0, "", "failed")
            return {"status": "failed", "error": str(e), "url": url}

    # ------------------------------------------------------------------
    def download_course_files(self, session, course_id: str, force: bool = False) -> dict:
        rows = self.db.items_with_files(course_id)
        results = {"ok": 0, "cached": 0, "failed": 0, "skipped": 0, "files": []}
        for r in rows:
            res = self.download_item(session, r, force=force)
            st = res["status"]
            results[st] = results.get(st, 0) + 1
            if st in ("ok", "cached"):
                results["files"].append(res.get("path"))
        return results

    def download_all_files(self, session, force: bool = False) -> dict:
        rows = self.db.items_with_files()
        results = {"ok": 0, "cached": 0, "failed": 0, "files": []}
        for r in rows:
            res = self.download_item(session, r, force=force)
            st = res["status"]
            results[st] = results.get(st, 0) + 1
        return results
