# Contributing

Everything here is specific to this repository. The suite is meant to run with nothing
external available, so most rules exist to keep that true.

## Running the tests

    .venv/Scripts/python.exe -m pytest -q

The offline suite passes with no API key, no model download and no database service. Tests
that would open a socket use in-memory SQLite or a stubbed client instead. `vector_store.py`
is the one module with no coverage; runbook retrieval is not exercised offline yet.

Lint and format:

    .venv/Scripts/python.exe -m black --check src tests scripts
    .venv/Scripts/python.exe -m flake8 src tests scripts

Both agree on an 88-column limit; flake8's config is `setup.cfg`, with per-file `E501`
ignores where a long template or prompt line is clearer than a wrapped one.

## What the project guarantees

- A provider or network failure degrades to heuristic analysis, marks the report degraded in
  `metadata.degraded`, and suppresses escalation. A schema validation failure raises HTTP 502
  and is never replaced with a heuristic report.
- The dashboard and the API mount the same router. `HX-Request` selects the HTML fragment;
  every other client gets JSON. A change to the fragment has to keep the JSON contract intact.
- Report storage is opt-in. An empty `DATABASE_URL` means storage is disabled, which answers
  503 with a reason; an unknown id answers 404. A storage failure is reported next to the
  report and never changes the analysis.
- The version is defined once in `src/config.py`. `tests/test_version.py` fails if a copy of
  the number appears elsewhere, so a release bump must not leave one surface stale.

## Containers

`docker compose up` alone reuses the existing image. After a code change, build first:

    docker compose build app
    docker compose up -d app

The Compose stack owns port 8000, and Docker relays loopback requests there, so the development
server listens on 8001 instead (`Settings.TRIAGE_PORT`, and `--port 8001` in the documented
commands). The two read different configuration: the container gets its Compose environment, a
host server reads the project `.env`. When you are unsure which one answered,
`/api/v1/health` reports `server.hostname` and `server.pid`. A backend switch made through the
UI is process-local, so `--reload` resets it to the `.env` default.

## Documents

- `docs/adr/` records a decision and the constraints behind it. A claim in an ADR has to match
  the code; when it stops matching, correct it or open an issue rather than leaving it as an
  aspiration.
- `docs/runbooks/` is the retrieval corpus: `scripts/ingest_runbooks.py` turns every markdown
  file there into embeddings. Design documents belong elsewhere, not in that directory.
- A change to a UI surface or to the failure policy updates the design contract in the same
  commit.

## Commits, issues and pull requests

Commits are conventional: a subject under 72 characters and a body that explains why the change
was needed, not what lines moved. PR descriptions are plain prose — what changed, what it costs,
what you ran. Skip comparison tables and editorial sections.

Keep credentials, tokens and machine-specific measurements out of public issues, PR bodies and
docs. Benchmark numbers are machine-specific; re-measure before quoting them anywhere.

Labels: `ui`, `storage`, `llm`, `ops` alongside the default `bug` and `enhancement`. A defect
that a merged commit already fixed can still be filed as an issue, naming the commit, so the
defect and its fix stay findable without reading through commit messages.

CI runs flake8, `black --check`, the offline suite and a coverage report across Python 3.11
through 3.14. Coverage is reported, not gated.
