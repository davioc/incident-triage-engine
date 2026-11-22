# Architecture Decision Record: Triage Report Persistence

## Decision

Store analysed triage reports in PostgreSQL through SQLAlchemy 2.0, schema-managed by
Alembic migrations, and run the repository layer's tests against SQLite in memory so the
test suite and CI stay offline with no database service.

## Context

Every triage run currently returns a report and then forgets it. The dashboard can analyse
a burst, but nothing is retrievable afterwards: there is no history, no way to compare
reports across runs, and no place to record which backend produced a report or whether it
was degraded. Week 7 adds the storage layer that makes reports addressable.

## Decision Drivers

* **Offline-first development**: the suite runs without API keys, without a model download,
  and without a database service; CI must not depend on Postgres being reachable.
* **Production realism**: the container stack should use a real server database, not a file
  that only one process can write.
* **Report shape**: a report is nested (`anomalies`, `affected_services`,
  `recommended_actions`) plus pipeline metadata (`analysis_mode`, `model`, `degraded`), so
  the row needs both queryable columns and a document-shaped payload.
* **Schema discipline**: table changes should arrive as migrations, not as someone editing a
  database by hand.

## Alternatives Considered

* **SQLite everywhere**: simplest to run and already available, but a single-writer file
  database is not what an incident-history service runs on, and it would leave the container
  stack without a real store.
* **Postgres for tests too**: the most faithful test environment, but every CI run and every
  local `pytest` would need a reachable database, which breaks the offline-first guarantee.
* **A document store (MongoDB, Redis)**: matches the nested payload naturally, but adds a
  second server and loses the relational columns worth querying (severity, incident id,
  escalation flag).
* **SQLModel**: less boilerplate, but it layers its own pydantic coupling over SQLAlchemy and
  adds a version-alignment risk against the pydantic 2 contract this project already pins.

## Rationale

Postgres is the store the deployment stack runs on; SQLite is a test double for the same
SQLAlchemy code, not a second storage design. Keeping the dialect difference inside
SQLAlchemy's types (`String`, `Integer`, `JSON`, `DateTime(timezone=True)`) means the
repository layer is written once and exercised on both. Queryable fields stay in columns so
severity, incident id and escalation can be filtered without reading the payload; the full
validated report and its metadata are stored as JSON documents beside them, so nothing a
model produced is dropped or reshaped by the storage layer.

Persistence is opt-in: `DATABASE_URL` is empty by default, so the stack still runs with no
database. A storage failure never changes the analysis result — it is reported alongside the
report instead of replacing it.

## Consequences

* A Postgres service joins `docker-compose.yml`, with its data on a named volume.
* `alembic upgrade head` becomes part of deployment; migrations are the only way to change
  the table.
* The reports endpoints return `503` when storage is disabled and `404` when an id is
  unknown, so "not configured" is never mistaken for "not found".
* Two dialects to keep honest: migrations are authored against generic SQLAlchemy types and
  verified on SQLite locally, with Postgres exercised in the container stack.
* The driver is `psycopg[binary]`, because plain psycopg needs a system `libpq` that dev
  hosts on Windows do not have.

## Migration Plan

1. `alembic revision` creates the initial table; `alembic upgrade head` applies it.
2. Verify locally against SQLite (`DATABASE_URL=sqlite:///...`) before shipping.
3. The container applies migrations at startup; a failed migration stops the app rather than
   serving reports from a mismatched schema.
