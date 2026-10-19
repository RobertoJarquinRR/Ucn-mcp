"""Submitter: upload files to a Moodle assignment draft area and submit.

Moodle file upload flow:
  1. GET  /mod/assign/view.php?id=<cmid>&action=editsubmission
         -> scrape sesskey + draft itemid (from filemanager JSON config)
  2. POST /repository/draftfiles_ajax.php?action=upload  (multipart)
         -> returns JSON with the new file info
  3. POST /mod/assign/view.php  (the submission form)
         -> files_filemanager=<itemid>, submitbutton=...
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from .models import Task, TaskType
from .session import MoodleSession

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Submitter:
    def __init__(self, ms: MoodleSession):
        self.ms = ms
        self.base = ms.base

    # ------------------------------------------------------------------
    def _load_edit_form(self, cmid: str) -> dict:
        """Fetch the submission form and extract its control fields."""
        url = f"{self.base}/mod/assign/view.php?id={cmid}&action=editsubmission"
        r = self.ms.session.get(url, timeout=30)
        soup = BeautifulSoup(r.text, "lxml")

        form = soup.find("form", attrs={"action": re.compile(r"/mod/assign/view\.php")})
        if not form:
            raise RuntimeError("Submission form not found (already submitted, or closed?)")

        fields = {}
        for inp in form.find_all("input", {"type": "hidden"}):
            if inp.get("name"):
                fields[inp["name"]] = inp.get("value", "")

        # Draft item id from the filemanager JSON config
        m = re.search(r'"itemid":(\d+),', r.text)
        if not m:
            m = re.search(r"itemid=(\d+)&amp;subdirs", r.text)
        draft_itemid = m.group(1) if m else fields.get("files_filemanager", "")

        sesskey = fields.get("sesskey") or soup.find("input", {"name": "sesskey"})["value"]
        return {
            "url": url,
            "fields": fields,
            "draft_itemid": draft_itemid,
            "sesskey": sesskey,
            "soup": soup,
        }

    # ------------------------------------------------------------------
    def upload_to_draft(self, itemid: str, file_path: Path, sesskey: str,
                        ctx_id: str = "", repo_id: str = "4") -> dict:
        """Upload a local file into a Moodle draft file area.

        Uses the filepicker AJAX endpoint. The multipart field MUST be named
        'repo_upload_file' (that is what Moodle's upload repository reads).
        Returns the JSON result (contains 'url', 'id', 'file').
        """
        upload_url = f"{self.base}/repository/repository_ajax.php?action=upload"
        data = {
            "sesskey": sesskey,
            "itemid": str(itemid),
            "repo_id": repo_id,
            "title": file_path.name,
            "savepath": "/",
            "env": "filepicker",
            "maxbytes": "52428800",
            "areamaxbytes": "-1",
        }
        if ctx_id:
            data["ctx_id"] = ctx_id
            data["contextid"] = ctx_id
        with open(file_path, "rb") as fh:
            files = {"repo_upload_file": (file_path.name, fh, "application/octet-stream")}
            r = self.ms.session.post(upload_url, data=data, files=files, timeout=120)
        try:
            return r.json()
        except Exception:
            return {"raw": r.text[:500], "status": r.status_code}

    # ------------------------------------------------------------------
    def submit_assignment(self, cmid: str, files: list[Path] | None = None,
                          online_text: str = "", dry_run: bool = True) -> dict:
        """Submit an assignment with the given files / online text.

        WARNING: with dry_run=False this REALLY submits to the live course.
        Only call it after the user has explicitly approved the submission.
        """
        files = files or []
        form = self._load_edit_form(cmid)
        sesskey = form["sesskey"]
        itemid = form["draft_itemid"]

        # Context id (for the upload call)
        page = self.ms.session.get(form["url"], timeout=30).text
        m = re.search(r"ctx_id=(\d+)", page)
        ctx_id = m.group(1) if m else ""

        uploaded = []
        for fp in files:
            res = self.upload_to_draft(itemid, Path(fp), sesskey, ctx_id=ctx_id)
            uploaded.append({"file": str(fp), "result": res})

        payload = dict(form["fields"])
        payload["files_filemanager"] = itemid
        if online_text:
            payload["onlinetext_editor[text]"] = online_text
            payload["onlinetext_editor[format]"] = "1"
        payload["submitbutton"] = "Guardar cambios"

        if dry_run:
            return {
                "dry_run": True,
                "cmid": cmid,
                "draft_itemid": itemid,
                "ctx_id": ctx_id,
                "uploaded": uploaded,
                "would_post_fields": sorted(k for k in payload if k != "sesskey"),
            }

        r = self.ms.session.post(form["url"], data=payload, timeout=120)
        ok = "error" not in r.text.lower() and ("view.php" in r.url or r.status_code in (200, 303))
        return {
            "dry_run": False,
            "cmid": cmid,
            "status": r.status_code,
            "final_url": r.url,
            "ok": ok,
            "uploaded": uploaded,
        }
