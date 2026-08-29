# RecoverOS

**From revenue at risk to revenue recovered.**

An AI-assisted revenue recovery operating system built for the Razorpay AI
Buildathon — Track 3: AI Revenue Recovery.

Most systems detect that a payment failed. RecoverOS closes the loop:

```
DETECT → UNDERSTAND → DECIDE → ACT → VERIFY → RECOVER → STOP / ESCALATE
```

## Core principle

**AI proposes. A deterministic policy engine validates. An action executor
executes.** The AI never directly controls a financial action — every retry,
payment link, or incentive is gated by explicit, auditable, human-legible
rules (max attempts, cooldowns, confidence thresholds, amount caps). See
`docs/DESIGN.md` for the full architecture, schema, state machine, policy
rules, and failure matrix.

A core feature is the system knowing **when not to act** — low-recoverability
cases are stopped, not endlessly retried, and every stop is explained.

## Status

This repo is being built incrementally, phase by phase, with each phase
verified against a live Postgres database before moving to the next.

- [x] **Phase 1 — Foundation**: repo structure, Postgres schema (14 tables,
      enums, FK/unique/check constraints), Alembic migrations, seed data,
      Docker Compose, health/readiness endpoints — all verified against a
      running database.
- [ ] Phase 2 — Core engine: workflow state machine + policy engine + tests
- [x] **Phase 3 — AI layer**: provider abstraction (Anthropic + Mock),
      structured output contract with business-rule validation, bounded
      retry + fallback, both product-spec worked examples reproduced
      exactly — all verified with real injected failures (forced timeout,
      forced malformed output) and a live end-to-end API call.
- [x] **Phase 4 — Action execution & failure handling**: deterministic
      payment simulator, idempotent action executor (DB-enforced, proven
      with a call-count test), timeout → `PENDING_VERIFICATION` →
      mandatory reconciliation (never a blind retry), full `/plan` →
      `/execute` → `/reconcile` pipeline wired into the API, and a
      cross-cutting audit trail (found and closed a real gap: the
      `audit_events` table existed but had zero writes before this
      phase). Verified live: a complete happy-path recovery and a
      correct-refusal-to-act case, both through the real running API.
- [x] **Phase 5 — Evaluation**: 1,000-event synthetic generator with real,
      deterministic ground truth; static baseline vs RecoverOS run over
      the identical dataset using the actual diagnosis heuristics and
      policy engine (not a re-implementation); honest, unadjusted results
      — RecoverOS wins clearly on false-negative rate (2.4% vs 7.2%) but
      does *not* win on raw automated recovered revenue or
      precision-when-acting on this dataset, both reported as-is, with
      the escalation-queue context needed to interpret the headline
      number correctly. Two real bugs found and fixed while building
      this (a false-negative formula error, and a missing-context gap on
      the headline comparison). See `docs/DESIGN.md` for full numbers.
- [ ] Phase 6 — Dashboard: Overview, Risk Queue, Workflow Detail, Failure Lab

## Running locally

### Option A — Docker Compose (recommended)

```bash
docker compose up --build
```

This starts Postgres, runs migrations, seeds demo data, and starts the API
on `http://localhost:8000`.

### Option B — Manual (what was used to build/verify this repo)

```bash
cd apps/api
pip install -r requirements.txt
cp .env.example .env          # edit DATABASE_URL if not using Docker's Postgres
alembic upgrade head
python -m app.db.seed
uvicorn app.main:app --reload
```

Verify:

```bash
curl http://localhost:8000/health   # {"status": "ok"}
curl http://localhost:8000/ready    # {"status": "ready", "database": "connected"}
```

## Repository structure

```
apps/
  api/            FastAPI backend (domain logic, AI layer, policy engine)
  web/             Dashboard frontend (Phase 6)
packages/
  shared/          Types/contracts shared between api and web (as needed)
docs/              Design docs, architecture, failure matrix, evaluation methodology
infrastructure/    Deployment-related config
scripts/           One-off / dev scripts
```

## AI provider

Defaults to `MockAIProvider` — deterministic, no API cost, used for all bulk
synthetic evaluation. Set `AI_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` in
`.env` to use the real Claude API for live demo calls. See `.env.example`.

## Why Postgres, not a JSON blob

Financial workflow state, AI decisions, policy verdicts, and action attempts
are all normalized relational tables with real foreign keys and constraints
(e.g. `actions.idempotency_key` is DB-level unique — duplicate execution is
structurally impossible, not just app-logic-prevented). See `docs/DESIGN.md`
for the full schema rationale.
