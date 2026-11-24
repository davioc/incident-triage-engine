# Architecture Decision Record: Incident Triage Pipeline

## Decision

Implement a five-stage triage pipeline — extraction, parsing, retrieval, analysis,
rendering — inside one FastAPI service, with HTMX rendering report fragments, rather than
a single monolithic analyzer or a broker-connected mesh of services.

## Context

This project is a portfolio build exercised against realistic sample log bursts. No
production logging or alerting stack is being replaced, and there is no SRE audience being
served; the point is to prove a pipeline works and to prove it fails honestly. The
constraints that actually shaped the architecture:

* **Offline-first.** The pipeline must run with no API key, no model download and no
  database service, so `pytest` and CI never depend on a provider being reachable.
* **A report must never be invented.** Provider and network failures degrade to heuristic
  analysis, labelled in `metadata.degraded`, with escalation suppressed. A schema
  validation failure raises HTTP 502 and is never substituted with a heuristic report.
* **One implementation, two consumers.** The dashboard and the API mount the same router,
  so the UI cannot drift from the contract: `HX-Request` selects the HTML fragment, every
  other client gets JSON.
* **Backend chosen at runtime.** `mock`, local Ollama and OpenAI are switched inside the
  running process rather than by editing `.env` and restarting, so backends can be compared
  against the same log input.
* **Retrieval is opt-in.** Runbooks ground the prompt when Chroma is configured, without
  forcing an embedding store into the test suite or the CLI.

## Decision Drivers

* **Offline-first development**: no keys, no downloads, no services required to run tests.
* **Testability**: each stage is a pure function or service, so a stage can be stubbed
  individually instead of the whole pipeline being mocked.
* **Backend flexibility**: swapping mock / Ollama / OpenAI must not touch core flow.
* **Alert trustworthiness**: a heuristic fallback has to look like a fallback. Suppressing
  escalation on degraded output matters more than reporting more incidents.
* **Modularity**: new log sources attach at extraction, new models attach at analysis.

## Alternatives Considered

* **Monolithic Logging Analyzer**: one service handling ingestion, parsing and analysis.
  Simpler to start, but a provider failure and a parse failure land in the same function,
  which makes the degrade-versus-fail distinction hard to express and hard to test.
* **Event-Driven Microservices Mesh**: collector, parser, retriever, analyzer and notifier
  as separate services over a message broker. It matches a scaling narrative, but adds
  operational overhead a single-operator service does not need and makes offline testing
  impossible.
* **Selected Approach**: a structured five-stage pipeline within one FastAPI service, with
  HTMX for fragment-level rendering.

## Rationale

Separating the stages buys four things that the alternatives do not:

* Each stage is stubbable in tests, so the suite stays offline and fast (81 tests, ~3s).
* Analysis backends swap without touching parsing or retrieval.
* Retrieved runbook text constrains what the model may propose, so recommendations are
  grounded in the operator's own runbooks rather than model recall.
* The report arrives as a fragment swap, so the dashboard updates without a page reload.

Keeping it in one process is the deliberate half of the trade-off: ingestion, retrieval and
analysis share one event loop, and independent scaling is not available today. Splitting the
service is a later decision, not a current capability.

## Consequences

* **Short-term**: schemas defined, stages implemented, runbooks indexed, HTMX and Pydantic
  validation wired.
* **Long-term**: new log sources attach at stages 1–2 (GitHub/Jira webhook ingestion is
  planned as Week 8), new analysis models attach at stage 4.
* **Trade-off**: retrieval is opt-in, so tests and the CLI stay offline and fast, but a
  retrieval failure is reported in `metadata.runbook_warning` rather than skipped silently.
* **Trade-off**: one process limits throughput; the mitigation is bounded input
  (`max_context_lines`) rather than horizontal scaling.

## Implementation

1. **Stage 1: Extraction** — FastAPI router with a Pydantic-validated payload
   (`raw_logs`, `max_context_lines`) on `POST /api/v1/triage`. Webhook sources are planned,
   not implemented.
2. **Stage 2: Parsing Engine** — pre-compiled RegEx and ISO-8601 timestamp extraction.
   Normalises log formats for downstream analysis; lines that do not parse are counted in
   `metadata.unparseable_lines` instead of being dropped quietly.
3. **Stage 3: Runbook Retrieval** — ChromaDB similarity search over `docs/runbooks/*.md`,
   enabled with `engine(use_runbooks=True)`. Matched blocks are passed to the prompt;
   retrieval failures land in `metadata.runbook_warning`.
4. **Stage 4: LLM Analysis** — routes on `TRIAGE_MODE`: `mock` (heuristics, no network),
   `ollama` (local Llama 3.2), `openai` (tool calling). Output is constrained to the
   `IncidentReport` schema, but by different means per backend: OpenAI uses tool calling,
   while Ollama sends `response_format={"type": "json_schema", "strict": True}` because
   local models write tool calls as plain text. Switching to an Ollama model probes
   `/v1/models` and names a missing model on the control, so a configuration mistake is not
   reported as an outage.
5. **Stage 5: Response and Alerting** — Pydantic validation, a Slack channel payload, and
   an HTMX fragment for `HX-Request` clients. Provider failures degrade to labelled
   heuristics without escalation; schema failures return HTTP 502. PagerDuty routing is not
   implemented.

Each stage is a pure function or service, enabling unit testing and mock injection.

## Monitoring and Feedback

Currently in place:

* `/api/v1/health` reports service version and storage state (enabled or disabled, backend,
  masked URL), so a disabled store is distinguishable from an unreachable one.
* `logger` warnings for degraded analysis, storage failures, retrieval warnings and schema
  failures, naming the backend and endpoint involved.
* The rendered report carries its provenance: the backend and model that produced it, and
  whether it was stored or why it was not.
* CI reports a coverage table per Python version without gating on the number.

Planned, not implemented: `/ready`, structured JSON logs per stage, and request tracing
(Week 11). The feedback loop is the project author reading the dashboard's provenance and
degraded notes, not a team's incident review.

## Review

This record is revised when a pipeline stage changes, when a backend contract changes, or
when a claim in it stops matching the code — not on a fixed calendar. A claim that no longer
holds is corrected or removed rather than left as aspiration.
