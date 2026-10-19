"""Resolver agent: propose a solution/answer for a task using an LLM.

Engines (--engine):
    openrouter   call OpenRouter chat completions (needs OPENROUTER_API_KEY)
    opencode     delegate to the local OpenCode CLI (`opencode run`)
    auto         pick openrouter if key present, else opencode
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import DB

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = PROJECT_ROOT / "storage" / "artifacts"

SYSTEM_PROMPT = (
    "Eres un asistente académico que ayuda a un estudiante universitario a resolver "
    "sus tareas. Analiza el enunciado y produce una solución completa, bien redactada "
    "y en español (salvo que la tarea pida otro idioma). Estructura la respuesta con: "
    "1) resumen de lo que pide la tarea, 2) la solución paso a paso, 3) el entregable "
    "final listo para copiar/subir. Sé concreto y no inventes datos que no puedas deducir."
)


def _task_context(cmid: str) -> tuple[dict, str]:
    db = DB()
    r = db.get_task(cmid)
    if not r:
        raise SystemExit(f"No existe la tarea {cmid}")
    payload = json.loads(r["payload"])
    ctx = (
        f"Curso: {r['course_name']}\n"
        f"Tarea: {r['title']}\n"
        f"Tipo: {r['type']}\n"
        f"Vence: {r['due_iso'] or r['due_date'] or '—'}\n"
        f"Enunciado:\n{r['description'] or '(sin descripción)'}\n"
    )
    atts = payload.get("attachments") or []
    if atts:
        ctx += "\nAdjuntos:\n" + "\n".join(f"- {a}" for a in atts)
    return dict(r), ctx


def _call_openrouter(prompt: str, model: str = "google/gemini-2.5-flash") -> str:
    import requests
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY no configurada")
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "max_tokens": 1400,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _call_opencode(prompt: str) -> str:
    full = f"{SYSTEM_PROMPT}\n\n---\n{prompt}"
    try:
        p = subprocess.run(["opencode", "run", full], capture_output=True, text=True,
                           timeout=180, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise RuntimeError(
            "engine 'opencode' no encontrado y OPENROUTER_API_KEY no configurada. "
            "Añade OPENROUTER_API_KEY al .env del proyecto o instala opencode en el PATH."
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("opencode no respondió en 180s (timeout).")
    if p.returncode != 0:
        raise RuntimeError(f"opencode falló: {(p.stderr or '')[:400]}")
    return (p.stdout or "").strip()


def propose(cmid: str, engine: str = "auto") -> dict:
    # Load the project .env so OPENROUTER_API_KEY is available in this process
    # (Hermes passes MCP subprocesses a filtered env, so we read it ourselves).
    from core.session import _load_dotenv
    _load_dotenv()

    row, ctx = _task_context(cmid)

    if engine == "auto":
        engine = "openrouter" if os.environ.get("OPENROUTER_API_KEY") else "opencode"

    if engine == "openrouter":
        proposal = _call_openrouter(ctx)
    elif engine == "opencode":
        proposal = _call_opencode(ctx)
    else:
        raise SystemExit(f"engine desconocido: {engine}")

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() else "_" for c in (row["title"] or cmid))[:40]
    saved = ARTIFACTS / f"proposal_{cmid}_{safe}_{ts}.md"
    saved.write_text(
        f"# Propuesta para {row['title']}\n\n"
        f"- Curso: {row['course_name']}\n- cmid: {cmid}\n- engine: {engine}\n"
        f"- generado: {datetime.now().isoformat(timespec='seconds')}\n\n---\n\n{proposal}\n",
        encoding="utf-8",
    )
    return {"proposal": proposal, "engine": engine, "saved": str(saved)}
