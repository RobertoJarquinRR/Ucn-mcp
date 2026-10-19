"""Parser: extract assignment/quiz details from a task page."""
from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup

from .models import Task, TaskType
from .session import MoodleSession

PLUGINFILE_RE = re.compile(r"pluginfile\.php")

# Spanish month names -> number
_MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def _parse_spanish_date(text: str) -> str | None:
    """Parse 'viernes, 25 de septiembre de 2026, 23:00' -> ISO string."""
    if not text:
        return None
    m = re.search(
        r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})(?:,\s*(\d{1,2}):(\d{2}))?",
        text, re.IGNORECASE,
    )
    if not m:
        return None
    day, month_name, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
    month = _MONTHS.get(month_name)
    if not month:
        return None
    hour = int(m.group(4)) if m.group(4) else 0
    minute = int(m.group(5)) if m.group(5) else 0
    try:
        return datetime(year, month, day, hour, minute).isoformat(timespec="minutes")
    except ValueError:
        return None


class Parser:
    def __init__(self, ms: MoodleSession):
        self.ms = ms

    def enrich(self, task: Task) -> Task:
        """Fetch the task page and fill in detail fields."""
        soup = self.ms.get_soup(task.url)
        if task.type == TaskType.QUIZ:
            self._parse_quiz(task, soup)
        elif task.type == TaskType.FORUM:
            self._parse_forum(task, soup)
        else:
            self._parse_assignment(task, soup)
        task.updated_at = datetime.now().isoformat(timespec="seconds")
        return task

    # ------------------------------------------------------------------
    def _parse_forum(self, task: Task, soup: BeautifulSoup) -> None:
        """Forums have no submission table; extract intro + due date (if any)."""
        intro = soup.select_one(".foruminfo, .post, #intro, .box.generalbox")
        if intro:
            task.description = intro.get_text(" ", strip=True)[:1500]

        # Due date sometimes appears as a "Vence:" / "Fecha límite" label
        text = soup.get_text(" ", strip=True)
        m = re.search(r"(?:Vence|Fecha l[íi]mite|Cierre|Due)[:\s]+([^.\n]{5,60}\d{4}[^.\n]{0,20})",
                      text, re.IGNORECASE)
        if m:
            task.due_date = m.group(1).strip()
            task.due_iso = _parse_spanish_date(task.due_date)

        # A forum is never "submitted" through the assignment flow
        task.requires_submission = False
        if "no se permite" in text.lower() or "cerrado" in text.lower():
            task.completed = True

    # ------------------------------------------------------------------
    def _parse_assignment(self, task: Task, soup: BeautifulSoup) -> None:
        # Intro / description
        intro = soup.select_one(".assignintro, [data-region='intro'], .box.generalbox")
        if intro:
            task.description = intro.get_text(" ", strip=True)

        # Submission status table
        for table in soup.find_all("table"):
            for tr in table.find_all("tr"):
                cells = tr.find_all(["th", "td"])
                if len(cells) < 2:
                    continue
                k = cells[0].get_text(" ", strip=True).lower()
                v = cells[1].get_text(" ", strip=True)
                if "estado de la entrega" in k:
                    task.submission_status = v
                elif "estado de la calificaci" in k:
                    task.grading_status = v
                elif "fecha de entrega" in k:
                    task.due_date = v
                    task.due_iso = _parse_spanish_date(v)
                elif "archivos enviados" in k:
                    task.submitted_files = [v]
                elif "calificaci" in k and "/" in v and not task.extra.get("grade"):
                    task.extra["grade"] = v

        # Attachments (instruction PDFs etc.)
        for a in soup.find_all("a", href=PLUGINFILE_RE):
            href = a["href"]
            name = a.get_text(" ", strip=True)
            # submission_files are what we sent; introattachment/intro are the task files
            if "submission_files" in href:
                continue
            task.attachments.append(f"{name} | {href}")

        # Derive flags
        sub = (task.submission_status or "").lower()
        if "no requiere" in sub:
            task.requires_submission = False
        grade = (task.grading_status or "").lower()
        if "calificado" in grade and "sin calificar" not in grade:
            task.completed = True

    # ------------------------------------------------------------------
    def _parse_quiz(self, task: Task, soup: BeautifulSoup) -> None:
        # Description — scoped to the intro box only
        intro = soup.select_one("#intro") or soup.select_one(".box.generalbox")
        if intro:
            task.description = intro.get_text(" ", strip=True)

        # Quiz info: attempts allowed, close date, time limit
        qi = soup.select_one(".quizinfo")
        info_text = qi.get_text(" ", strip=True) if qi else ""

        m = re.search(r"Intentos permitidos:\s*(\d+)", info_text)
        if m:
            task.extra["attempts_allowed"] = m.group(1)

        m = re.search(r"L[íi]mite de tiempo:\s*([^\n]+?)(?:\s*Intentos|\s*Este|\s*$) ", info_text)
        if m:
            task.extra["time_limit"] = m.group(1).strip()
        else:
            m2 = re.search(r"L[íi]mite de tiempo:\s*([0-9]+\s*horas?\s*[0-9]*\s*minutos?)", info_text)
            if m2:
                task.extra["time_limit"] = m2.group(1).strip()

        m = re.search(r"se cerr[oó]\s+el\s+([^.\n]+?\d{2}:\d{2})", info_text, re.IGNORECASE)
        if m:
            task.due_date = m.group(1).strip()
            task.due_iso = _parse_spanish_date(task.due_date)

        # Attempt summary table — authoritative for completion state
        summary = soup.select_one("table.quizattemptsummary, table.generaltable.quizattemptsummary")
        if summary:
            rows = summary.find_all("tr")
            if len(rows) >= 2:
                state_cell = rows[-1].find_all(["th", "td"])[0].get_text(" ", strip=True)
                task.submission_status = state_cell
                if "finalizado" in state_cell.lower():
                    task.completed = True
                if "sin intentos" in state_cell.lower():
                    task.submission_status = "Sin intentos"
