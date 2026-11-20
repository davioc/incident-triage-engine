# 🛠️ Incident Triage Engine

> **Status:** Active - In Progress  
> A modular, high-performance SRE incident triage pipeline and dashboard: it parses raw
> log telemetry, retrieves runbook context, and produces schema-validated root-cause
> reports with escalation alerts, built with **FastAPI**, **HTMX**, **Tailwind CSS**,
> **ChromaDB**, and **Pydantic**.

---

## 📸 System UI

![SRE Triage Dashboard](docs/images/dashboard-ui.png)
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
┌──────────────────────────┐  TRIAGE_MODE Routing:
│  Stage 4: LLM Analysis   │  ├── mock   -> Heuristic extraction, no network
└───────────┬──────────────┘  ├── ollama -> Local Llama 3.2 (OpenAI-compatible)
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
src/api/routes.py     # The /api/v1 contract + HTMX HTML fragment rendering
src/api.py            # Standalone API app (mounts that router)
src/main.py           # Dashboard app (router + src/web/index.html at /)
src/engine.py         # Parse -> runbook retrieval -> analyze -> alert payload
src/llm_client.py     # mock / ollama / openai modes, schema-enforced tool calling
src/log_parser.py     # Log line regex, metrics, synthetic log generator
src/schema.py         # SeverityLevel, LogAnomaly, IncidentReport (Pydantic v2)
src/vector_store.py   # ChromaDB runbook index (lazy, created on first use)
src/web/index.html    # Dashboard UI (HTMX + Tailwind via CDN)
scripts/incident_cli.py         # CLI: JSON-lines log file -> incident report
scripts/test_vector_store.py    # CLI: ingest runbooks and probe similarity queries
docs/runbooks/*.md    # Remediation runbooks indexed for retrieval
tests/                # pytest suite (parser, schema, llm client, engine, api, cli)
```

---

## 🧰 Tech Stack

- **Backend:** Python 3.11+, FastAPI, Pydantic v2, `python-dotenv`
- **Frontend:** HTMX + Tailwind CSS (CDN), server-rendered HTML fragments
- **Retrieval:** ChromaDB with the default local embedding function (CPU)
- **AI / LLM Integration:** OpenAI API (`gpt-4o`), Ollama (Llama 3.2), heuristic fallback

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

`TRIAGE_MODE` defaults to `mock` when unset, so the dashboard analyzes logs offline with
the heuristic analyzer. Note that the committed root `.env` currently sets
`TRIAGE_MODE=openai`, so triage calls out to OpenAI unless you change it.

### 5. Index the runbooks (optional, enables retrieval)

The served app queries ChromaDB for runbooks matching the error messages, which grounds
`recommended_actions` in your own remediation docs. The index is built lazily on first
use; ingest it once:

```bash
python scripts/test_vector_store.py
```

The database lives in `.chroma_db/` (created automatically). Tests and `run_triage.py`
leave retrieval off, so the suite stays fast and offline.

### 6. Run the command-line demo

In a second terminal, activate the same virtual environment and run:

```bash
python run_triage.py            # synthetic logs through a mocked analyzer
python scripts/incident_cli.py path/to/logs.jsonl   # one JSON object per line
```

### 7. Run the tests

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
is suppressed. Keep `.env` (and any real key) out of version control.
