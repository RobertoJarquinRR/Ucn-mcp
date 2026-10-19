"""Deep scraper: walk every section (week) of a course and catalogue ALL items.

Moodle courses are inconsistent: the same material may live in the "pending"
area, in a weekly section, as a label (heading), as a resource (file) or as an
external URL. This module walks the whole course page so nothing is missed.
"""
from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup

from .models import Course
from .session import MoodleSession
from storage.db import DB

MOD_RE = re.compile(r"/mod/(\w+)/view\.php\?id=(\d+)")
PLUGINFILE_RE = re.compile(r"pluginfile\.php")

# Only these get the full assignment/quiz treatment (due dates, submission state)
GRADED = {"assign", "quiz"}

# Moodle mod name -> internal TaskType value
_MOD_TO_TASKTYPE = {"assign": "assignment", "quiz": "quiz", "forum": "forum"}


class DeepScraper:
    def __init__(self, ms: MoodleSession):
        self.ms = ms
        self.base = ms.base
        self.db = DB()

    # ------------------------------------------------------------------
    def scan_course(self, course: Course, enrich: bool = True) -> dict:
        """Walk every section of a course and record items + sections."""
        soup = self.ms.get_soup(course.url)

        counts: dict[str, int] = {}
        sections = soup.select("li.section")

        for pos, sec in enumerate(sections):
            sectionid = sec.get("id") or f"section-{pos}"
            name_el = sec.select_one("h3.sectionname, .sectionname")
            section_name = name_el.get_text(" ", strip=True) if name_el else sectionid
            sum_el = sec.select_one(".summary")
            summary = sum_el.get_text(" ", strip=True)[:2000] if sum_el else ""

            section_key = f"{course.id}:{sectionid}"
            self.db.upsert_section(section_key, course.id, sectionid,
                                   section_name, pos, summary)

            # Each <li class="activity modtype_X"> is one item
            for act in sec.select("li.activity, div.activity"):
                cls = " ".join(act.get("class", []))
                mt = re.search(r"modtype_(\w+)", cls)
                item_type = mt.group(1) if mt else "unknown"

                link = act.find("a", href=MOD_RE)
                cmid = ""
                url = ""
                if link:
                    m = MOD_RE.search(link["href"])
                    cmid = m.group(2)
                    url = link["href"]

                # Title: prefer the instance name, else the whole text
                title_el = act.select_one(".instancename, .activityname")
                title = title_el.get_text(" ", strip=True) if title_el else ""
                title = re.sub(r"\s*(Archivo|File|Foro|Forum|Tarea|Assignment|URL|Cuestionario)\s*$",
                               "", title).strip()
                if not title:
                    title = act.get_text(" ", strip=True)[:120]

                desc = act.get_text(" ", strip=True)[:1000]

                key = cmid or f"{course.id}:{sectionid}:{item_type}:{abs(hash(title)) % 10**8}"
                record = {
                    "key": key,
                    "course_id": course.id,
                    "course_name": course.name,
                    "section_key": section_key,
                    "section_name": section_name,
                    "item_type": item_type,
                    "title": title,
                    "url": url,
                    "description": desc,
                    "file_url": "",
                    "due_iso": None,
                    "status": "info",
                }

                # Resources: resolve the real file URL now (needed for download)
                if item_type == "resource" and url:
                    try:
                        record["file_url"] = self.resolve_resource_file(url) or ""
                    except Exception:
                        record["file_url"] = ""

                self.db.upsert_item(record)
                counts[item_type] = counts.get(item_type, 0) + 1

            # Standalone labels (etiquetas) carry the class plan / headings
            for lab in sec.select(".modtype_label"):
                txt = lab.get_text(" ", strip=True)
                if not txt or len(txt) < 12:
                    continue
                key = f"{course.id}:{sectionid}:label:{abs(hash(txt[:200])) % 10**8}"
                self.db.upsert_item({
                    "key": key, "course_id": course.id, "course_name": course.name,
                    "section_key": section_key, "section_name": section_name,
                    "item_type": "label", "title": txt[:80],
                    "url": "", "description": txt[:3000], "file_url": "",
                    "due_iso": None, "status": "info",
                })
                counts["label"] = counts.get("label", 0) + 1

        # Enrich graded activities (due dates + submission status) via the task parser
        enriched = 0
        if enrich:
            from .parser import Parser
            from .models import Task, TaskType
            parser = Parser(self.ms)
            rows = [r for r in self.db.items_for_course(course.id)
                    if r["item_type"] in GRADED and r["url"]]
            for r in rows:
                try:
                    t = Task(cmid=r["key"], type=TaskType(_MOD_TO_TASKTYPE[r["item_type"]]),
                             course_id=course.id, course_name=course.name,
                             title=r["title"], url=r["url"])
                    parser.enrich(t)
                    self.db.upsert_task(t)
                    self.db.upsert_item({
                        "key": r["key"], "course_id": course.id,
                        "course_name": course.name, "section_key": r["section_key"],
                        "section_name": r["section_name"], "item_type": r["item_type"],
                        "title": r["title"], "url": r["url"],
                        "description": r["description"], "file_url": r["file_url"] or "",
                        "due_iso": t.due_iso, "status": r["status"],
                    })
                    enriched += 1
                except Exception:
                    pass

        return {"course": course.name, "sections": len(sections),
                "items": counts, "enriched": enriched}

    # ------------------------------------------------------------------
    def resolve_resource_file(self, resource_url: str) -> str | None:
        """Follow a mod/resource/view.php link and return the pluginfile URL."""
        r = self.ms.session.get(resource_url, timeout=30)
        # Direct redirect to the file?
        if PLUGINFILE_RE.search(r.url):
            return r.url
        soup = BeautifulSoup(r.text, "lxml")
        # <object>/<embed> or a download link
        for tag in soup.find_all(["a", "object", "embed", "source"]):
            cand = tag.get("href") or tag.get("data") or tag.get("src")
            if cand and PLUGINFILE_RE.search(cand):
                return cand
        return None

    # ------------------------------------------------------------------
    def scan_all(self, enrich: bool = True) -> list[dict]:
        from .scraper import Scraper
        courses = Scraper(self.ms).discover_courses()
        out = []
        for c in courses:
            try:
                out.append(self.scan_course(c, enrich=enrich))
            except Exception as e:
                out.append({"course": c.name, "error": str(e)})
        return out
