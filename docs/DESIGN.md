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

## Phase 2 — State machine & policy engine

_Pending._

## Phase 3 — AI layer

_Pending._

## Phase 4 — Action execution & failure handling

_Pending._

## Phase 5 — Evaluation

_Pending._

## Phase 6 — Dashboard

_Pending._
