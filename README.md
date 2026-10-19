# UCN Agent — automatización de tareas del Aula Virtual (Moodle)

Agente diario que scrapea [UCN Virtual](https://ucnvirtual.edu.ni/grado/) (Moodle),
detecta todas las tareas pendientes por curso, permite que una IA proponga
soluciones, y las envía **solo tras aprobación explícita** (human-in-the-loop).

## Estado actual

| Componente | Estado | Notas |
|---|---|---|
| Login HTTP (sin browser) | ✅ verificado | requests + logintoken, cookies persistidas |
| Descubrimiento de cursos | ✅ verificado | 6 cursos |
| Scraping de tareas/actividades | ✅ verificado | 57 actividades |
| Parser de detalles (fechas, estado, adjuntos) | ✅ verificado | fechas ES → ISO |
| Detección de pendientes | ✅ verificado | 16 pendientes reales |
| Storage SQLite | ✅ verificado | `storage/tasks.db` |
| Upload de archivos al draft de Moodle | ✅ verificado | endpoint `repository_ajax.php` |
| Envío del formulario (submit real) | ✅ verificado | dry-run + confirmación explícita |
| CLI interactiva | ✅ verificado | `cli.py` |
| Cron diario + reporte | ✅ verificado | `cron/daily_check.py` |
| Resolver IA | ✅ implementado | OpenRouter u OpenCode |
| Notificación Telegram/email | 🔲 hook listo | configurar en `.env` |

## Instalación

```bash
cd C:\Proyects\ucn-agent
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
cp .env.example .env      # y rellena credenciales
```

`.env`:
```
UCN_BASE_URL=https://ucnvirtual.edu.ni/grado
UCN_USERNAME=usuario
UCN_PASSWORD=password
# opcional — resolver IA
OPENROUTER_API_KEY=sk-or-...
# opcional — notificación
UCN_NOTIFY=1
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## Uso

```bash
# Escanear Moodle y guardar todo en la BD
python scan.py

# CLI interactiva
python cli.py list                 # lista pendientes
python cli.py show <cmid>          # detalle completo de una tarea
python cli.py resolve <cmid>       # la IA propone una solución
python cli.py submit <cmid> archivo1.pdf archivo2.zip   # dry-run + confirmación antes de enviar
python cli.py skip <cmid>          # marca como hecha manualmente
```

## Arquitectura

```
ucn-agent/
├── core/
│   ├── session.py        # login HTTP + cookies persistentes
│   ├── scraper.py        # cursos + actividades (tareas/quiz/foro)
│   ├── deep_scraper.py   # recorrido PROFUNDO: cada semana → todos los items
│   ├── parser.py         # detalles de tarea (fechas, estado, adjuntos)
│   ├── library.py        # descargas con dedup + extracción de texto
│   ├── notifier.py       # aviso + acceso directo en el Escritorio
│   ├── packager.py       # texto → PDF/DOCX (entregas)
│   ├── orchestrator.py   # flujo diario + consultas rápidas (por nivel)
│   ├── submitter.py      # upload a draft + envío de formulario
│   └── models.py         # Course, Task, estados
├── agents/
│   └── resolver.py       # propuesta IA (OpenRouter / OpenCode)
├── cron/
│   ├── daily_run.py      # ejecución diaria profunda (Task Scheduler 05:00)
│   └── daily_check.py    # escaneo rápido + reporte
├── storage/
│   ├── tasks.db          # SQLite: courses, tasks, sections, items,
│   │                     #         downloads (dedup), scan_runs (auditoría)
│   ├── artifacts/        # propuestas IA, archivos a enviar
│   ├── reports/          # informes diarios
│   └── logs/             # daily.log
├── mcp_server.py         # 15 tools MCP (ver abajo)
└── cli.py                # interfaz de revisión humana
```

### Destinos en disco

| Qué | Dónde | Por qué |
|---|---|---|
| Documentos descargados | `~\Documents\UCN-Agent\<fecha>\<Materia>\<Semana>\` | Fuente de verdad; el cron nunca pide permisos |
| Texto extraído | `<archivo>.txt` junto al original | Permite buscar dentro del contenido |
| Aviso | `~\Desktop\UCN-Tareas\` (resumen .md + .lnk) | Lo que ves siempre |
| Papelera | `~\Desktop\UCN-Papelera\<ts>\` | `safe_delete` nunca borra directo |

### Modelo de datos (SQLite)

- `courses` · `tasks` (con lógica de pendientes) · `history`
- `sections` — semanas de cada curso
- `items` — **todo** lo del curso (assign/quiz/foro/resource/url/label/attendance) con su semana
- `downloads` — dedup por hash de URL (no re-descarga) + sha256
- `scan_runs` — auditoría de cada ejecución


## Flujo diario (human-in-the-loop)

```
06:00  cron → login → crawl → parse → detecta pendientes → guarda BD
       → reporte (storage/reports/latest.txt) [+ Telegram si configurado]

Usuario:
  python cli.py list                     # ve todo lo pendiente
  python cli.py show 4165170             # lee el enunciado + adjuntos
  python cli.py resolve 4165170          # la IA propone
       ↓ revisa la propuesta
  python cli.py submit 4165170 trabajo.pdf  # dry-run → confirma → envía
       ↓
  BD marca DONE + historial
```

## Integración con Hermes (MCP + Discord)

El proyecto se expone como **MCP server** en `mcp_server.py`, registrado en Hermes
como `ucn`. Sus tools quedan disponibles en **todos** los canales (incluido Discord):

| Tool MCP | Qué hace |
|---|---|
| `list_pending_tasks` | Lista tareas pendientes (JSON) |
| `get_task(cmid)` | Detalle completo de una tarea |
| `scan_courses` | Re-escanea Moodle ahora |
| `propose_solution(cmid)` | La IA propone una solución |
| `submit_task(cmid, files, confirm)` | Envía (dry-run salvo `confirm=True`) |
| `skip_task(cmid)` | Marca como hecha manualmente |
| `list_courses` | Lista los cursos matriculados |

### Arquitectura

```
Discord  →  Hermes gateway  →  agente Hermes (LLM + memoria + tools)
                                    │
                                    └── MCP server "ucn" (stdio)
                                            │
                                            └── C:\Proyects\ucn-agent
```

Así, se chatea con el agente en Discord y este **actúa** sobre el Moodle (listar,
resolver, enviar). El agente responde inteligentemente porque es el propio
Hermes (con memoria, skills y razonamiento), no un bot básico.

### Registro del MCP (ya hecho)

```bash
hermes mcp add ucn \
  --command "C:/Proyects/ucn-agent/.venv/Scripts/python.exe" \
  --args "C:/Proyects/ucn-agent/mcp_server.py"
hermes mcp test ucn        # verifica conexión + tools
hermes mcp list            # estado
```

Tras añadirlo, **reinicia Hermes o ejecuta `/reload-mcp`** para cargar las tools
en la sesión actual.

### Instalación standalone del server

```bash
.venv/Scripts/pip install "mcp<2"   # FastMCP clásico (mcp 2.x renombró la API)
.venv/Scripts/python mcp_server.py  # corre sobre stdio
```

## Programar el cron diario (Windows)

```bat
schtasks /create /tn "UCN Agent Daily" /tr "C:\Proyects\ucn-agent\.venv\Scripts\python.exe C:\Proyects\ucn-agent\cron\daily_check.py" /sc daily /st 06:00
```

## Seguridad

- Las credenciales viven en `.env` (gitignored). Para producción, usar el
  **vault de Hermes** o variables de entorno del sistema.
- El submitter **nunca** envía nada sin `dry_run=False` explícito, que la CLI
  solo activa tras confirmación en prompt.
- Todos los cambios de estado se registran en la tabla `history`.

## Roadmap

- [ ] Dashboard web local (FastAPI + HTMX) para aprobar desde el navegador
- [ ] Bot de Telegram para aprobar desde el móvil
- [ ] Resolver multi-agente (varias propuestas en paralelo + reviewer)
- [ ] Descarga automática de los PDFs adjuntos y extracción de texto
- [ ] Soporte de quizzes (autocompletar respuestas)
- [ ] Notificación proactiva de tareas nuevas / vencimientos

## Notas técnicas (Moodle)

- **Login**: `GET /login/index.php` → extraer `logintoken` → `POST` con
  `username`, `password`, `logintoken`.
- **Upload de archivo**: `POST /repository/repository_ajax.php?action=upload`
  con `repo_id=4`, `itemid=<draft>`, y el archivo en el campo **`repo_upload_file`**.
- **Envío**: `POST /mod/assign/view.php` con `files_filemanager=<draft itemid>`
  y `submitbutton`.
- El `itemid` del draft se obtiene de la página `?action=editsubmission`.
