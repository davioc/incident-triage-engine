# 🛠️ Incident Triage Engine

> **Status:** Active - In Progress  
> A modular, high-performance SRE incident triage pipeline and dashboard: it parses raw
> log telemetry, retrieves runbook context, and produces schema-validated root-cause
> reports with escalation alerts, built with **FastAPI**, **HTMX**, **Tailwind CSS**,
> **ChromaDB**, and **Pydantic**.

![CI](https://github.com/davioc/incident-triage-engine/actions/workflows/main.yml/badge.svg)

---

## 📸 System UI

![SRE Triage Dashboard](docs/images/dashboard-ui-ver1.1.0.png)
*Figure 1: Real-time SRE Log Triage Dashboard rendering extracted anomalies, affected services, and recommended actions.*

---

## 🏗️ Architecture & Pipeline Flow

A request body is a JSON payload (`{"raw_logs": "...", "max_context_lines": 50}`). The
HTMX dashboard posts it with the `json-enc` extension, and the same endpoint serves
API clients and webhooks.

```text
[ JSON Payload: raw_logs ]
            │
            ▼
┌──────────────────────────┐
│  Stage 1: Extraction     │  FastAPI router, Pydantic payload validation
└───────────┬──────────────┘
            │
            ▼
┌──────────────────────────┐
│  Stage 2: Parsing Engine │  Pre-compiled RegEx & ISO-8601 Extractor
└───────────┬──────────────┘   (unparseable lines are counted, not dropped)
            │
            ▼
┌──────────────────────────┐
│  Stage 3: Runbook Match  │  ChromaDB similarity search over docs/runbooks/*.md
└───────────┬──────────────┘   (opt-in: engine(use_runbooks=True))
            │
            ▼
┌──────────────────────────┐  TRIAGE_MODE Routing (switchable at runtime):
│  Stage 4: LLM Analysis   │  ├── mock   -> Heuristic extraction, no network
└───────────┬──────────────┘  ├── ollama -> Local Llama 3.2, json_schema decoding
            │                 └── openai -> GPT-4o tool calling
            ▼
┌──────────────────────────┐
│  Stage 5: Response       │  IncidentReport validation, Slack payload, HTMX fragment
└──────────────────────────┘
```

**Failure policy:** provider/network failures degrade to heuristic analysis and the
report is marked degraded (`requires_escalation = False`, `metadata.degraded` set), so a
fabricated alert never looks like LLM analysis. Output that fails `IncidentReport`
validation is rejected with HTTP 502 rather than silently replaced.

---

## 🗂️ Repository Structure

```text
src/api/routes.py   # The /api/v1 contract + HTMX HTML fragment rendering
src/api/__init__.py # Standalone API app (mounts that router)
src/main.py         # Dashboard app (router + src/web/index.html at /)
src/engine.py       # Parse -> runbook retrieval -> analyze -> alert payload
src/llm_client.py   # mock / ollama / openai modes; json_schema for Ollama, tools for OpenAI
src/log_parser.py   # Log line regex, metrics, synthetic log generator
src/schema.py       # SeverityLevel, LogAnomaly, IncidentReport (Pydantic v2)
src/vector_store.py # ChromaDB runbook index (lazy, created on first use)
src/db/models.py    # triage_reports table (SQLAlchemy 2.0, generic column types)
src/db/session.py   # engine + session scope; DATABASE_URL decides enabled/disabled
src/db/repository.py# save / get / list / delete for stored reports
alembic/            # Schema migrations; the URL comes from DATABASE_URL, like the app
alembic.ini         # Migration settings; deliberately carries no database URL
src/web/index.html  # Dashboard UI (HTMX + Tailwind via CDN)
scripts/incident_cli.py         # CLI: JSON-lines log file -> incident report
scripts/ingest_runbooks.py      # CLI: ingest runbooks and probe similarity queries
docs/runbooks/*.md    # Remediation runbooks indexed for retrieval
docs/adr/             # Architecture decision records (pipeline, database choice)
tests/                # pytest suite (parser, schema, llm client, engine, api, cli, db)
Dockerfile            # API + dashboard image (non-root, ONNX model pre-warmed)
docker-compose.yml    # app + Postgres + optional Ollama profile + data volumes
setup.cfg             # flake8 settings aligned with black's 88-char limit
```

---

## 🧰 Tech Stack

- **Backend:** Python 3.11+, FastAPI, Pydantic v2, `python-dotenv`
- **Frontend:** HTMX + Tailwind CSS (CDN), server-rendered HTML fragments
- **Retrieval:** ChromaDB with the default local embedding function (CPU)
- **AI / LLM Integration:** OpenAI API (`gpt-4o`), Ollama (Llama 3.2), heuristic fallback
- **Persistence:** PostgreSQL via SQLAlchemy 2.0 + Alembic; SQLite as the test double

---

## 🧪 Testing & Local Execution

This project can be run locally with Python.

### 1. Install Python

Install Python 3.11 or newer and verify that it is available:

```bash
python --version
```

On Windows, use `py --version` if the `python` command is not available.

### 2. Create a virtual environment

Run these commands from the repository root:

**Windows PowerShell**

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux/macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

If PowerShell blocks activation, either use the direct Python path shown below or
allow scripts for the current user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 3. Install dependencies

With the virtual environment activated, install the pinned dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Using `python -m pip` ensures that packages are installed into the same Python
environment used to run the application. On Windows, the commands can also be run
without activation:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Coverage tooling lives in `requirements-dev.txt` so the runtime image installs
`requirements.txt` alone:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q --cov=src --cov-report=term-missing
```

### 4. Run the API and dashboard

Start the development server:

```bash
python -m uvicorn src.main:app --reload
```

Then open <http://127.0.0.1:8000> in a browser. The application provides:

- Dashboard: <http://127.0.0.1:8000>
- Health check: <http://127.0.0.1:8000/api/v1/health>
- Interactive API documentation: <http://127.0.0.1:8000/docs>
- Triage endpoint: `POST /api/v1/triage`
- Analysis backend: `GET /api/v1/mode` (current backend) and `POST /api/v1/mode?mode=...`
  (switch it for this process — see "Switching the backend at runtime")
- Stored reports: `GET /api/v1/reports`, `GET /api/v1/reports/{id}`,
  `DELETE /api/v1/reports/{id}` — see "Store triage reports"

`TRIAGE_MODE` defaults to `mock` when unset, so the dashboard analyzes logs offline with
the heuristic analyzer. Note that the committed root `.env` currently sets
`TRIAGE_MODE=openai`, so triage calls out to OpenAI unless you change it.

### 5. Index the runbooks (optional, enables retrieval)

The served app queries ChromaDB for runbooks matching the error messages, which grounds
`recommended_actions` in your own remediation docs. The index is built lazily on first
use; ingest it once:

```bash
python scripts/ingest_runbooks.py
```

The database lives in `.chroma_db/` (created automatically). Tests and `run_triage.py`
leave retrieval off, so the suite stays fast and offline.

### 6. Store triage reports (optional)

Report storage is opt-in. With `DATABASE_URL` unset the stack behaves exactly as before
and the report endpoints answer **503** with the reason, which is deliberately different
from the **404** an unknown report id gets. Set a URL to enable it:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://triage:triage@localhost:5432/triage"
python -m alembic upgrade head    # the app never migrates itself outside the container
python -m uvicorn src.main:app
```

Every triage then writes a row, and the response says whether it landed:
`metadata.storage` is `{"enabled": true, "stored": true, "report_id": "..."}`. A database
that cannot be reached never changes the analysis — the report still comes back and
`metadata.storage.stored` is `false` alongside the error.

- `GET /api/v1/reports?limit=50&severity=HIGH` — newest first, short form
- `GET /api/v1/reports/{id}` — the validated report plus provenance (mode, model,
  degraded reason, raw log, unparseable line count)
- `DELETE /api/v1/reports/{id}` — drop one report

For a quick local look without Postgres, point it at a file:
`$env:DATABASE_URL = "sqlite:///./reports.db"`. The test suite uses in-memory SQLite so it
needs no database service — see `docs/adr/0002-database-selection.md`.

### 7. Run the command-line demo

In a second terminal, activate the same virtual environment and run:

```bash
python run_triage.py            # synthetic logs through a mocked analyzer
python scripts/incident_cli.py path/to/logs.jsonl   # one JSON object per line
```

The CLI runs the same pipeline as the API and prints the report, then the metadata:

```bash
python scripts/incident_cli.py logs.jsonl --mode ollama --model llama3.2 --runbooks
```

Exit codes: `0` success, `1` unreadable or empty log file or an unusable backend,
`2` analyzer output that failed schema validation (the API answers 502 for the same case).
Storage is the API's job, so the CLI never writes to the database.

### 8. Run the tests

```bash
python -m pytest
```

`tests/test_log_parser.py` pins the strict log format on purpose: a missing `[LEVEL]`, a
status that is not exactly three digits, or `45 ms` with a space must be rejected.

### Optional: configure an LLM provider

Edit the root `.env` file and set one of the supported modes:

```dotenv
TRIAGE_MODE=mock
```

For OpenAI, set `TRIAGE_MODE=openai` and provide `OPENAI_API_KEY`. For Ollama, set
`TRIAGE_MODE=ollama`, start Ollama locally, and optionally set `OLLAMA_MODEL` and
`OLLAMA_BASE_URL`. An unknown `TRIAGE_MODE` is a startup error, not a silent fallback.

If a provider call fails at runtime, the engine degrades to heuristic analysis: the
report is labelled `[DEGRADED]`, `metadata.degraded` carries the reason, and escalation
is suppressed. The dashboard shows that reason above the report, plus a line naming the
backend that answered (e.g. `Analysis: ollama · llama3.2`). Keep `.env` (and any real
key) out of version control.

### Switching the backend at runtime

The dashboard's **Serving** button (left of *Analyze & Triage Payload*) toggles between
`llama 3.2`, `Other OpenAI` and `Mock mode (default)` without editing `.env` or restarting
the server. The same control is an ordinary API endpoint:

```bash
curl "http://127.0.0.1:8000/api/v1/mode"                      # what is active now
curl -X POST "http://127.0.0.1:8000/api/v1/mode?mode=ollama&model=llama3.2"
curl -X POST "http://127.0.0.1:8000/api/v1/mode?mode=mock"
```

The switch rebuilds the analyzer inside the running process only; `.env` stays the
default, and `OLLAMA_MODEL` / `OPENAI_MODEL` still supply the model when you omit
`model=`. Switching to `openai` without `OPENAI_API_KEY` is a `400` that leaves the
previous backend in place rather than a silent fallback.

---

## 🐳 Docker

One command runs the whole stack:

```bash
docker compose up
```

Then open <http://localhost:8000>. The image is built from `Dockerfile` and:

- installs the pinned dependencies, then copies only `src/`, `scripts/` and
  `docs/runbooks/` — `.env` is excluded by `.dockerignore`, so no key is baked into a layer;
- pre-warms ChromaDB's ONNX embedding model during the build, so container start does
  not download ~80 MB;
- ingests the runbooks before serving, because the served app turns retrieval on and an
  empty index would return zero matches (upsert is idempotent, so restarting re-ingests);
- persists the index in a named volume (`CHROMA_DB_DIR=/app/.chroma_db`) rather than
  inside the image;
- applies Alembic migrations before serving when `DATABASE_URL` is set, and stops the
  container if a migration fails rather than storing reports in a mismatched schema;
- runs as a non-root user and reports readiness through `/api/v1/health`.

The stack also starts `postgres:16-alpine`, and the app points at it by default with
`DATABASE_URL=postgresql+psycopg://triage:triage@postgres:5432/triage`, so report storage
is on in the container and off when you run locally without a database. Those credentials
are development defaults; override `POSTGRES_PASSWORD` for anything real, and set
`DATABASE_URL=` (empty) to run the container with storage disabled.

`TRIAGE_MODE` defaults to `mock`, so the stack needs no API key. To use OpenAI, pass the
key through the environment instead of editing the image:

```bash
docker compose up --build -d
# or with a key resolved from your shell / .env
$env:TRIAGE_MODE="openai"; $env:OPENAI_API_KEY="sk-..."; docker compose up --build -d
```

Note that Compose resolves `${...}` from your shell **and** the project `.env`, so values
there reach the container even though `.env` is never copied into the image. A leftover
`TRIAGE_MODE=openai` with an old key makes the stack run OpenAI and return `[DEGRADED]`
reports; start with `$env:TRIAGE_MODE="mock"` to get offline heuristic analysis.

To point the app at a local Ollama (Llama 3.2, no external API), start Ollama and set:

```bash
$env:TRIAGE_MODE="ollama"; docker compose up -d --force-recreate app
```

The `ollama` service is defined behind a `llm` profile so the default stack stays small;
`docker compose --profile llm up -d` pulls and starts it alongside the app. The app reaches
it over `OLLAMA_BASE_URL=http://ollama:11434/v1`, the container-network address. If Ollama
is not answering, triage degrades to heuristics rather than failing.

Useful commands:

```bash
docker compose ps        # status and health
docker compose logs app  # ingest output + uvicorn logs
docker compose down      # stop; the volumes keep the index and stored reports
docker compose down -v   # stop and delete the index and the database
```
