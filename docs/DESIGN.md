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

## Phase 3 — AI layer (complete)

### Provider abstraction (`app/ai/provider.py`)

`AIProvider` is the only interface the rest of the system depends on.
`AIContext` is the explicit, flat input surface — every signal the AI is
allowed to reason over is visible in one dataclass, not implicitly derived
from an ORM object. Two implementations:

- **`MockAIProvider`** (`app/ai/mock_provider.py`) — the default. Zero cost,
  zero network dependency, and genuinely deterministic (same input always
  produces the same output — verified by test), which is required for the
  1,000-event batch evaluation in Phase 5 to be reproducible. It encodes
  the same reasoning heuristics as the product spec's two worked examples,
  not just random/fake output:
  - Transient failure + strong customer history (≥0.6 success rate) →
    `RETRY_PAYMENT` with confidence ≥0.65 — verified against the spec's
    literal "₹4,999, 7 successful payments" example.
  - Retries exhausted (`previous_attempts >= merchant_max_retry_attempts`)
    → `STOP` with a required, non-empty `stop_reason` — verified against
    the spec's "4 failed attempts, retry limit exhausted" example.
  - Also has explicit rules for permanent failure reasons (card
    expired/mandate revoked → payment link or stop depending on history)
    and low-signal cases (→ reminder), so all 8 `RecommendedAction` values
    are reachable, not just retry/stop.
  - Supports deterministic failure injection (`force_timeout`,
    `force_malformed`) used by both the test suite and, later, the
    Failure Lab — so "AI times out" and "AI returns malformed output" are
    real, triggerable code paths, not just described in a document.

- **`AnthropicProvider`** (`app/ai/anthropic_provider.py`) — real Claude API
  calls using forced tool-use (`tool_choice: {"type": "tool", ...}`) so the
  model is structurally constrained to the decision shape, rather than
  parsing free-text JSON out of prose. Still re-validated through
  `AIDecisionOutput` after the call — a well-formed tool-use block is not
  automatically trusted. Inactive unless `AI_PROVIDER=anthropic` and
  `ANTHROPIC_API_KEY` are set in `.env`; `MockAIProvider` remains the
  default so the whole system runs with zero API cost out of the box.

### Structured output contract (`app/ai/schemas.py`)

`AIDecisionOutput` is a Pydantic model matching the spec's JSON contract
exactly (diagnosis, evidence, recoverability_score, recommended_action,
confidence, expected_recovery_minor, risk_level,
recommended_delay_seconds, customer_message_intent, stop_reason), plus one
business-rule validator beyond basic type-checking: **a `STOP`
recommendation without a `stop_reason` is rejected at construction time** —
an unexplained stop is exactly the "trust the model blindly" failure mode
the product spec warns against, so it's structurally impossible, not just
discouraged by prompt wording.

### Diagnosis service (`app/ai/diagnosis_service.py`)

The only entry point the rest of the app uses. Owns:
- Provider selection (reads `AI_PROVIDER` from settings — nothing else in
  the codebase hardcodes which provider is active)
- One bounded retry on provider failure (not unlimited — the failure
  matrix requires bounded retries everywhere, including here)
- Fallback to a conservative default (`ESCALATE_TO_HUMAN`, confidence
  `0.0`, so it never falsely claims certainty) if both attempts fail or
  return output that fails schema validation
- Persisting every attempt — successful or fallback — to `ai_decisions`,
  so a fallback shows up in the audit trail as a fallback, not silently
  disappears

Confidence-threshold policy (is 0.85 confidence good enough to act on
unsupervised) deliberately stays out of this service and lives only in the
Phase 2 policy engine — this keeps "did we get a trustworthy decision at
all" and "is it good enough to act on" as two separately testable concerns.

### Verified

- 37/37 tests passing (13 added for Phase 3): mock-provider determinism,
  both product-spec worked examples reproduced exactly, schema validation
  (confidence range, stop-reason requirement, non-negative recovery
  amount), and both real injected-failure fallback paths (forced timeout,
  forced malformed output) — not just described, actually triggered and
  asserted on.
- Live end-to-end via running API + Postgres: created a risk event,
  confirmed `/diagnose` correctly rejects a workflow not yet in
  `ENRICHING` (409), advanced it, called `/diagnose` again — got back a
  real `RETRY_PAYMENT` recommendation at 0.85 confidence reasoning
  correctly over the seeded customer's actual 7-success/1-failure history,
  and confirmed the workflow state advanced to `SCORING`.

## Phase 4 — Action execution & failure handling (complete)

### Payment/recovery simulator (`app/simulation/payment_simulator.py`)

Stands in for Razorpay's real APIs — zero external dependency, zero
production money movement, matching the spec's Demo Mode requirement.
Determinism is the core design choice: outcome is derived from a stable
hash of `(idempotency_key, seed)`, so the same inputs always produce the
same outcome, which is what makes the Phase 5 batch evaluation
reproducible and lets the future Failure Lab force a specific failure on
demand (`force_outcome=...`) instead of hoping one occurs randomly.

**Timeout is modeled as an exception, not a return value** —
`TimeoutSimulated` is raised, not returned as `SimulatedResult(outcome=TIMEOUT)`.
This is deliberate: a real HTTP timeout means the caller never learns the
outcome. Returning a normal result would silently misrepresent the actual
failure mode the system needs to handle.

### Action executor (`app/actions/executor.py`)

The only code path that executes an approved action. Three properties
enforced structurally, not just by convention:

1. **Idempotency is a DB constraint, not an app-logic check.** Every
   action gets a deterministic key
   (`{workflow_id}:{action_type}:{attempt_number}`). A second call with
   the same key hits `actions.idempotency_key`'s unique constraint and
   raises `DuplicateActionError` — verified by test with a call-count
   assertion proving the simulator itself was only invoked once, not just
   that the DB has one row.
2. **A timeout never triggers a blind retry.** On `TimeoutSimulated`, the
   `Action` is marked `UNCERTAIN` and the workflow moves to
   `PENDING_VERIFICATION`. There is no code path anywhere in the executor
   that retries an uncertain action directly — `reconcile_action()` must
   run first and resolve the actual external state.
3. **Only an `ALLOW` policy verdict may reach this code.** `execute_action`
   raises immediately if called with anything else — this is the
   structural half of "AI proposes, policy validates, action executor
   executes."

### Full pipeline wired into the API (`app/api/routes.py`)

Added `/plan`, `/execute`, and `/reconcile` to complete the loop the
earlier phases only had pieces of:

```
DETECT → ENRICHING → DIAGNOSING → SCORING → PLANNING → POLICY_CHECK
  → APPROVED → EXECUTING → VERIFYING → RECOVERED
                    ↳ PENDING_VERIFICATION → (reconcile) → VERIFYING → RECOVERED / DETECTED
  → ESCALATED / EXHAUSTED (policy engine's verdict, not a crash)
```

`/plan` fetches the latest `AIDecision`, runs it through the real Phase 2
policy engine (not a stub), persists the verdict, and transitions the
workflow accordingly. `/execute` calls the real action executor and
verified the outcome. `/reconcile` is the only path out of
`PENDING_VERIFICATION`.

### Audit trail gap found and closed

Auditing this phase surfaced a real gap: the `audit_events` table (defined
in the Phase 1 schema) had zero writes anywhere in the codebase —
`workflow_transitions`, `ai_decisions`, and `policy_decisions` all
recorded their own domain data, but there was no unified cross-cutting
log, and critical test #10 from the build spec ("audit events are
created") was not actually satisfied. Added `app/core/audit.py` — a single
`log_audit_event()` function — and wired it into workflow creation, every
state transition, every policy verdict, and every action outcome
(succeeded, duplicate-blocked, timed-out-to-uncertain, rate-limited,
reconciled). Verified live: a single successful workflow run now produces
a complete 12-row audit trail from `workflow.created` through
`action.succeeded` to the final `RECOVERED` transition.

### Verified

- 57/57 tests passing (20 new): payment simulator determinism (including
  a genuine bug fix — the original determinism test crashed rather than
  compared when its chosen key landed in the `TIMEOUT` bucket, now fixed
  to handle both normal and exceptional deterministic outcomes),
  idempotency key construction, duplicate-action rejection (with a
  call-count proof the simulator was only actually invoked once),
  timeout → `UNCERTAIN` (not `FAILED`), reconciliation requiring
  `UNCERTAIN` status, and 5 new audit-trail tests asserting real
  `audit_events` rows exist for each event type.
- Live end-to-end via running API + Postgres, two full scenarios:
  1. **Happy path**: risk event → enrich → diagnose (0.95 confidence
     `RETRY_PAYMENT`) → plan (policy `ALLOW`) → execute → `RECOVERED`,
     with a complete, verified 12-row audit trail.
  2. **Correct refusal to act**: a customer with 0 successful / 4 failed
     payments and a permanent failure reason → diagnosis correctly
     returns `STOP` with a real `stop_reason` → plan correctly lands on
     `EXHAUSTED`, never `APPROVED` — proving "knowing when not to act"
     holds end-to-end, not just inside the mock provider's unit tests.

## Phase 5 — Evaluation

_Pending._

## Phase 6 — Dashboard

_Pending._
