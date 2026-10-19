"""Scraper: discover courses and their activities from UCN Moodle."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

from .models import Course, Task, TaskType
from .session import MoodleSession

COURSE_LINK_RE = re.compile(r"/course/view\.php\?id=(\d+)")
ACTIVITY_RE = re.compile(r"/mod/(assign|quiz|forum|lesson|scorm|resource|url|folder|page)/view\.php\?id=(\d+)")

_MOD_TO_TYPE = {
    "assign": TaskType.ASSIGNMENT,
    "quiz": TaskType.QUIZ,
    "forum": TaskType.FORUM,
    "lesson": TaskType.LESSON,
    "scorm": TaskType.SCORM,
}

# Activity types the agent cares about (submittable / gradable)
SUBMITTABLE = {"assign", "quiz", "forum"}


class Scraper:
    def __init__(self, ms: MoodleSession):
        self.ms = ms
        self.base = ms.base

    # ------------------------------------------------------------------
    def discover_courses(self) -> list[Course]:
        """Return every course the logged-in user is enrolled in, with real names."""
        soup = self.ms.get_soup(f"{self.base}/my/")
        courses: dict[str, Course] = {}

        for a in soup.find_all("a", href=COURSE_LINK_RE):
            m = COURSE_LINK_RE.search(a["href"])
            if not m:
                continue
            cid = m.group(1)
            if cid in courses and courses[cid].name and courses[cid].name != cid:
                continue
            title = a.get("title") or a.get_text(" ", strip=True) or cid
            # Skip generic summary links that only show the shortname as text
            if title and len(title) > 2:
                courses[cid] = Course(id=cid, name=title)

        # Resolve the real course NAME from each course page title
        # (the dashboard link text is only the short code, e.g. "CJ-IS-1900-...").
        for cid, course in courses.items():
            try:
                page = self.ms.get_soup(course.url)
                if page.title:
                    name = re.sub(r"^.*?Curso:\s*", "", page.title.get_text(strip=True)).strip()
                    if name:
                        course.name = name
            except Exception:
                pass

        return list(courses.values())

    # ------------------------------------------------------------------
    def scrape_course_activities(self, course: Course) -> list[Task]:
        """Return all submittable activities in a course."""
        soup = self.ms.get_soup(course.url)
        tasks: list[Task] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=ACTIVITY_RE):
            m = ACTIVITY_RE.search(a["href"])
            if not m:
                continue
            mod, cmid = m.group(1), m.group(2)
            if mod not in SUBMITTABLE or cmid in seen:
                continue
            seen.add(cmid)

            title = a.get_text(" ", strip=True)
            title = re.sub(r"\s*(Tarea|Cuestionario|Foro)\s*$", "", title).strip() or title

            tasks.append(Task(
                cmid=cmid,
                type=_MOD_TO_TYPE.get(mod, TaskType.OTHER),
                course_id=course.id,
                course_name=course.name,
                title=title,
                url=f"{self.base}/mod/{mod}/view.php?id={cmid}",
                discovered_at=datetime.now().isoformat(timespec="seconds"),
            ))

        return tasks

    # ------------------------------------------------------------------
    def scrape_all(self) -> tuple[list[Course], list[Task]]:
        """Full crawl: all courses + all activities."""
        courses = self.discover_courses()
        all_tasks: list[Task] = []
        for c in courses:
            try:
                tasks = self.scrape_course_activities(c)
                all_tasks.extend(tasks)
                print(f"  [{c.id}] {c.name[:50]}: {len(tasks)} activities")
            except Exception as e:
                print(f"  [{c.id}] ERROR: {e}")
        return courses, all_tasks
