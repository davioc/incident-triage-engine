# Design contract

This file describes the surface the repository actually exposes: the dashboard page, the
API routes, how failures are reported, and what gets stored. It lives at the repository
root rather than in `docs/runbooks/`, because `docs/runbooks/` is the corpus that gets
embedded and retrieved by the analysis stage.

A pull request that changes the dashboard markup, the failure policy, or the storage
behaviour updates this file in the same commit. That rule exists so the contract never
drifts from the code it describes.

## One router, two applications

`src/api/routes.py` holds every route. `src/main.py` mounts it and also serves
`src/web/index.html` at `/`; `src/api/__init__.py` mounts the same router without the
page. A route added once is reachable from both applications, so there is no second copy
of a handler to keep in sync.

Each route that the dashboard calls checks the `HX-Request` header. When it is `"true"`
the handler returns an HTML fragment; otherwise it returns JSON. Keeping both shapes in
one handler is what makes the dashboard and the API agree: a fragment and a JSON body
cannot disagree about what happened, because they come from the same branch logic.

## Failure policy

A transport or provider problem during analysis is not a failure of the request. The
engine falls back to heuristics, records the reason in `metadata.degraded`, and the
dashboard names it in amber. `requires_escalation` is suppressed on a degraded report:
a triage that did not reach a model is not evidence that a human should be paged.

A response that does not match the report schema is a failure. Those requests return 502
and nothing is substituted. The rule is that a report is never invented: whatever appears
on screen either came from the model or is labelled as a heuristic.

## Storage semantics

Report storage is opt-in. `DATABASE_URL` unset means disabled, and the API says so in
plain words rather than pretending there is an empty database.

- `POST /api/v1/triage` always returns the analysis. When storage is disabled, or when a
  configured database cannot be reached, the response carries a `metadata.storage` block
  with `stored: false` and the reason or error. The analysis is not lost because storage
  failed.
- `GET /api/v1/reports` returns 503 when storage is disabled and 200 with an empty list
  when it is enabled and holds nothing. Those two states are different facts, and a
  caller should be able to tell them apart.
- `GET /api/v1/reports/{id}` returns 404 for an unknown id and 503 when storage is
  disabled. `DELETE` behaves the same way.
- `GET /api/v1/reports/summary` answers 200 in both states. Its job is to report a state,
  so it names the reason when nothing is being kept. It returns 503 only when storage is
  configured and unreachable, which is a real failure.

Storage URLs in responses have the password removed. The health endpoint reports the
backend, the masked URL, and the table name; it never echoes credentials.

The same endpoint names the process that answered: `server.hostname`, `server.pid`, and
the address the request was addressed to. Two servers can run from this repository at once
— the Compose container and a host dev server — and they read different configuration, so
a response that looks wrong may simply have come from the other one. The published stack
keeps port 8000; the dev server listens on 8001 (`Settings.TRIAGE_PORT`) so they never
compete for the same address.

## Backend switching

`GET /api/v1/mode` reports the backend this process is using. `POST /api/v1/mode`
rebuilds the analyzer for `mock`, `ollama` or `openai` and keeps the change for the
lifetime of the process. Nothing is written to `.env`, so two servers started from the
same file can serve different backends.

Switching to `ollama` probes the endpoint's model list. A missing model is a warning
shown in the Serving menu, not a rejected switch: the request may still succeed, and the
operator sees which models the endpoint actually offered. Switching to `openai` without
an API key is rejected and the previous backend keeps serving.

## The stored-report notice

The dashboard's right column carries one element with `id="storage-status"` that is
rendered by `/api/v1/reports/summary`. It shows how many reports are stored, how many of
them came out of a degraded analysis, and the newest few with their severity, the backend
and model that produced them, and their timestamp. When storage is disabled the same
element says so and names the reason, so the absence is explained instead of looking like
an empty panel.

It is filled on page load and refreshed after each analysis. There is no polling: the
page dispatches `storage-refresh` when the triage request completes, and the element
fetches the fragment again. A notice that refreshed on a timer would show a number
changing for no reason.

The counts come from `report_counts()` in the repository, which reads two counts instead
of pulling every stored payload. The recent rows come from `list_reports()` with a small
limit, newest first, so the notice is a preview and not a summary of the whole table.

## The sample burst

The *Load Sample Error Burst* button writes lines the parser accepts, one string per line
joined afterwards. `parse_log_line` anchors at the start of the line and
`process_raw_logs` strips only the whole string, so a sample written as an indented
template literal reaches the textarea with leading spaces and every line but the first is
counted in `metadata.unparseable_lines`. `tests/test_dashboard.py` parses the lines the
button loads, so the example cannot drift away from the pinned format unnoticed.

## Rules for changing this surface

- Anything rendered from report content is escaped (`_esc` in `src/api/routes.py`).
- A route the dashboard calls returns both shapes, and the JSON shape carries the same
  reason the fragment shows.
- A change to the failure policy, the notice, or the storage states updates this file in
  the same commit, together with the tests that pin the behaviour.
- Offline tests stay offline: no API keys, no model download, no database service. The
  suite runs the same repository code against SQLite in memory, and an unreachable
  database is simulated with a path SQLite cannot open.
- Severity chip classes live in `SEVERITY_CHIP_CLASSES` so the report view and the notice
  colour the same levels the same way.
