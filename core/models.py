"""Data models for tasks, courses, and submissions."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Optional


class TaskType(str, Enum):
    ASSIGNMENT = "assignment"   # subir archivo / texto online
    QUIZ = "quiz"               # cuestionario
    FORUM = "forum"             # foro de discusión
    LESSON = "lesson"           # lección
    SCORM = "scorm"             # paquete scorm
    OTHER = "other"


class TaskStatus(str, Enum):
    NEW = "new"                 # recién descubierta
    PENDING = "pending"         # pendiente, sin entregar
    SUBMITTED = "submitted"     # ya entregada por el usuario
    AWAITING_REVIEW = "awaiting_review"   # esperando confirmación del usuario
    APPROVED = "approved"       # aprobada para envío
    SUBMITTING = "submitting"   # en proceso de envío
    DONE = "done"               # enviada con éxito por el agente
    GRADED = "graded"           # ya calificada
    FAILED = "failed"           # error al enviar
    SKIPPED = "skipped"         # el usuario la hizo manualmente


@dataclass
class Course:
    id: str
    name: str
    url: str = ""

    def __post_init__(self):
        if not self.url:
            self.url = f"https://ucnvirtual.edu.ni/grado/course/view.php?id={self.id}"


@dataclass
class Task:
    # identity
    cmid: str                       # course module id (id= param on mod/*/view.php)
    type: TaskType
    course_id: str
    course_name: str = ""
    title: str = ""
    url: str = ""

    # detail
    description: str = ""
    due_date: Optional[str] = None          # raw Moodle string
    due_iso: Optional[str] = None           # parsed ISO if possible
    submission_status: str = ""             # "Enviado para calificar" etc.
    grading_status: str = ""
    submitted_files: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)   # instruction files (URLs)
    extra: dict = field(default_factory=dict)

    # agent state
    status: TaskStatus = TaskStatus.NEW
    discovered_at: Optional[str] = None
    updated_at: Optional[str] = None

    # derived flags (filled by parser)
    requires_submission: bool = True   # False for offline/in-class tasks
    completed: bool = False            # quiz attempt finished, or graded

    def _status_blob(self) -> str:
        return f"{self.submission_status} {self.grading_status}".lower()

    @property
    def is_submitted(self) -> bool:
        s = (self.submission_status or "").lower()
        # guard: "no entregado" / "sin entregar" must NOT match
        if any(neg in s for neg in ("no entregado", "sin entregar", "no enviado", "no enviada")):
            return False
        return any(k in s for k in ("enviado", "submitted", "entregado", "para calificar"))

    @property
    def is_offline(self) -> bool:
        """Task that doesn't require an online submission (done in class)."""
        return "no requiere" in (self.submission_status or "").lower()

    @property
    def is_graded(self) -> bool:
        return "calificado" in (self.grading_status or "").lower() and \
               "sin calificar" not in (self.grading_status or "").lower()

    @property
    def is_pending(self) -> bool:
        """Needs attention: not submitted, not offline, not graded, not finished."""
        if self.status in (TaskStatus.DONE, TaskStatus.GRADED, TaskStatus.SKIPPED):
            return False
        # Forums: no reliable completion signal over plain HTTP (the assign-style
        # submission table does not exist), so they are tracked but not flagged
        # as pending — otherwise every forum ever created shows up as noise.
        if self.type == TaskType.FORUM:
            return False
        if self.completed or self.is_graded:
            return False
        if self.is_offline and not self.requires_submission:
            return False
        if self.is_submitted:
            return False
        return True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = self.type.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d = dict(d)
        d["type"] = TaskType(d.get("type", "other"))
        d["status"] = TaskStatus(d.get("status", "new"))
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
