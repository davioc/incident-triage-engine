# Architecture Decision Record: Incident Triage Pipeline

## Decision

Implement a modular, high-performance incident triage pipeline that ingests log streams, extracts anomalies, retrieves runbook context, performs AI-driven analysis, and renders real-time HTML fragments via HTMX.

## Context

Our current logging and alerting infrastructure lacks a unified analysis layer, making it difficult to correlate events, extract actionable insights, and present SREs with a clear, real-time view of system health. As observability requirements grow, we need an architecture that can scale ingestion, integrate with external LLMs, and deliver instant UI updates without page reloads.

## Decision Drivers

* **Scalability**: Ingest and analyze high-volume log streams in near real-time.
* **Maintainability**: Modular pipeline stages (extraction, parsing, retrieval, analysis, rendering) for easier updates and debugging.
* **Flexibility**: Support multiple analysis backends (mock, local Ollama, OpenAI) without changing the core flow.

## Alternatives Considered

* **Monolithic Logging Analyzer**: A single service handling ingestion, parsing, and analysis. Simpler initially, but harder to scale independently and update without downtime.
* **Event-Driven Microservices Mesh**: Multiple services (collector, parser, retriever, analyzer, notifier) communicating via a message broker. Higher complexity and operational overhead for our use case.
* **Selected Approach**: A structured five-stage pipeline within a FastAPI-backed service, coupled with HTMX for client-side rendering.

## Rationale

The chosen pipeline architecture balances scalability and simplicity. By separating concerns into five distinct stages—Extraction, Parsing, Retrieval, Analysis, and Rendering—we gain:

* Independent scaling of ingestion vs. retrieval vs. analysis vs. UI.
* Easy swapping of analysis backends (e.g., mock, Ollama, OpenAI) without touching core logic.
* Grounded recommendations: retrieved runbook text constrains what the model may propose.
* Tight coupling to HTMX for instant, fragment-based UI updates, ideal for SRE dashboards.

## Consequences

* **Short-term**: Initial setup requires defining schemas, implementing pipeline stages, indexing runbooks, and wiring HTMX/Pydantic validation.
* **Long-term**: A robust, extensible foundation for adding new log sources, analysis models, or real-time visualization widgets.
* **Trade-off**: Retrieval is opt-in, so tests and the CLI stay offline and fast, but a retrieval failure must be reported rather than silently skipped.

## Implementation

The pipeline is implemented as follows:

1. **Stage 1: Extraction**
   - FastAPI router with a Pydantic-validated JSON payload (`raw_logs`, `max_context_lines`).
   - Accepts raw log payloads via `POST /api/v1/triage`.

2. **Stage 2: Parsing Engine**
   - Pre-compiled RegEx and ISO-8601 timestamp extractor.
   - Normalizes log formats for downstream analysis; lines that do not parse are counted in `metadata.unparseable_lines`.

3. **Stage 3: Runbook Retrieval**
   - ChromaDB similarity search over `docs/runbooks/*.md`, enabled with `engine(use_runbooks=True)`.
   - Matched runbook blocks are passed to the prompt; retrieval failures are recorded in `metadata.runbook_warning`.

4. **Stage 4: LLM Analysis**
   - Routes on `TRIAGE_MODE`: `mock` (heuristic, no network), `ollama` (local Llama 3.2), `openai` (GPT-4o tool calling).
   - Output is constrained to the `IncidentReport` schema through tool calling.

5. **Stage 5: Response and Alerting**
   - Pydantic validation, Slack/PagerDuty payload formatting, and HTMX fragment rendering for `HX-Request` clients.
   - Provider failures degrade to heuristic analysis, labelled and without escalation; schema failures return HTTP 502.

Each stage is a pure function or service, enabling unit testing and mock injection.

## Monitoring and Feedback

* Track ingestion rate, parsing success rate, analysis latency, and error counts via FastAPI middleware.
* Log structured events (JSON) for each stage to correlate issues across the pipeline.
* Gather SRE feedback through the dashboard UI to refine anomaly detection rules and analysis prompts.

## Review

This architecture decision record will be reviewed:

* Quarterly, or whenever a major pipeline change is proposed.
* When introducing new log formats, analysis models, or UI components.

The implementation plan will be updated as required to keep pace with evolving requirements and technologies.
