# RecoverOS — Design

This document is filled in phase by phase, matching the build order. Each
section is written only once the corresponding phase is implemented and
verified against a live database — nothing here is aspirational.

## Phase 1 — Schema (complete)

See `apps/api/app/db/models.py` for the authoritative schema. Summary:

**14 domain tables**, grouped by concern:

- **Core entities**: `merchants`, `customers`, `merchant_policies`
- **Risk & workflow**: `revenue_risk_events`, `recovery_workflows`,
  `workflow_transitions`
- **AI & policy**: `ai_decisions`, `policy_decisions`
- **Execution**: `actions`, `action_attempts`, `notifications`
- **Audit**: `audit_events`
- **Evaluation**: `evaluation_runs`, `evaluation_events`

**Design decisions and why:**

- **UUID primary keys everywhere** — avoids leaking sequential business
  volume (row count) to anyone who sees an ID, and avoids merge conflicts
  across environments.
- **Money as integer minor units (paise), never float** — `amount_minor`
  columns throughout. Floating point money math is a classic source of
  silent reconciliation bugs; this makes the class of bug structurally
  impossible.
- **Idempotency enforced at the DB level, not just app logic**:
  - `actions.idempotency_key` — `UNIQUE`. A duplicate action execution
    cannot be inserted twice, full stop, regardless of application bugs.
  - `revenue_risk_events(merchant_id, source_event_id)` — `UNIQUE`. A
    duplicate webhook delivery cannot create a second risk event.
- **`workflow_transitions` is append-only** — every state change is a new
  row, never an update. This is the audit trail for "how did this workflow
  get from DETECTED to RECOVERED," independent of the general
  `audit_events` table.
- **`ai_decisions` stores the full structured output verbatim** (diagnosis,
  evidence, recoverability score, confidence, recommended action, latency,
  token counts, cost) — every AI call is fully reconstructable after the
  fact, including whether it was a fallback response.
- **`policy_decisions` records which rule fired**, not just the verdict —
  `rule_triggered` + `explanation` columns mean an ALLOW/BLOCK/ESCALATE is
  always explainable without re-deriving it from code.
- **Enums are DB-level types**, not just Python convention — an invalid
  workflow state or action type is rejected by Postgres itself.

Verified: migration applies cleanly from a dropped/recreated database
(`alembic upgrade head`), 15 tables (14 domain + `alembic_version`) confirmed
via `\dt`, constraints confirmed via `\d actions`.

## Phase 2 — State machine & policy engine (complete)

### State machine (`app/domain/state_machine.py`)

The full 15-state graph from the product spec, encoded as an explicit
`ALLOWED_TRANSITIONS` dict — the entire state graph is readable in one
place rather than scattered across if/else checks.

**Key safety property proven by test**: `PENDING_VERIFICATION` cannot
transition directly back to `EXECUTING`. When an external payment API call
times out and the outcome is unknown, the only legal next state is
`VERIFYING` (reconciliation). This makes it structurally impossible for the
code to "just retry" after an uncertain external call — the exact
paranoia the product spec required for payment safety.

`transition()` is the **only** sanctioned way to change a workflow's state.
It validates against the graph, writes an immutable `workflow_transitions`
row, updates `current_state`/`is_terminal`, and commits — all in one
function, so there's no code path that can silently corrupt workflow state.

`create_workflow()` relies on the DB `UNIQUE` constraint on
`recovery_workflows.risk_event_id` for idempotency: calling it twice for
the same risk event raises `IntegrityError` rather than creating a second
workflow. Proven by test, not assumed.

### Policy engine (`app/policy/engine.py`)

Pure deterministic function: `evaluate(workflow, policy, recommended_action,
confidence, amount_minor, ...) -> PolicyVerdict`. Has **zero** dependency on
any AI provider, prompt, or model — it only sees a structured recommendation
the AI layer (Phase 3) will produce and validate first.

Rules apply in a fixed priority order (first match wins), so behavior is
always reproducible and every verdict names the exact `rule_triggered`:

1. `ai_recommended_stop` — always honored (this is "knowing when NOT to act")
2. `customer_opted_out` — hard block on communication actions
3. `amount_exceeds_escalation_threshold` — high-value cases go to a human
   regardless of AI confidence
4. `confidence_below_threshold` — low-confidence AI output escalates
   rather than silently proceeding
5. `max_retry_attempts_exceeded` / `max_communication_attempts_exceeded`
6. `incentives_disabled_for_merchant` / `incentive_exceeds_max_percent`
7. `ai_requested_escalation`
8. `all_checks_passed` — only reached if nothing above fired

### API (`app/api/routes.py`)

`POST /api/v1/risk-events` — ingests an event, creates its workflow.
Duplicate `source_event_id` for the same merchant returns **409**, verified
live (posted the same payload twice, second call correctly rejected, no
duplicate row created).

`POST /api/v1/workflows/{id}/advance/{state}` — drives the state machine
via HTTP. An illegal transition returns **422** with the exact allowed-states
list, verified live (`ENRICHING -> RECOVERED` correctly rejected).

`GET /api/v1/workflows/{id}/transitions` — full audit history with
timestamps and reasons, verified live.

### Verification performed

- 24 automated tests, all passing, run against the live Postgres DB (no
  mocking) — covering the state graph, transition persistence, invalid
  transition rejection, the full happy-path walk to `RECOVERED`, the
  stop-path walk to `EXHAUSTED`, and the `PENDING_VERIFICATION` safety
  valve, plus every policy rule (retry/communication limits, confidence
  threshold, amount escalation, opt-out, incentive caps, STOP handling).
- Test isolation confirmed: ran the suite twice consecutively, zero
  leftover rows in the DB afterward.
- Full HTTP round-trip exercised manually against a live running server:
  create event → workflow auto-created → valid transition succeeds →
  invalid transition returns 422 → duplicate webhook returns 409 →
  transition history retrievable.

## Phase 3 — AI layer

_Pending._

## Phase 4 — Action execution & failure handling

_Pending._

## Phase 5 — Evaluation

_Pending._

## Phase 6 — Dashboard

_Pending._
