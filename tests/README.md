# Tests

This directory is for **cross-cutting tests**: end-to-end workflow tests,
evaluation-run tests (baseline vs RecoverOS over the synthetic batch), and
anything that spans multiple apps (api + web) once the dashboard exists.

Unit and integration tests for the API itself are co-located with the
service they test, at `apps/api/tests/`, following standard practice for a
FastAPI/SQLAlchemy service — this keeps domain tests next to the domain code
they verify (policy engine tests next to the policy engine, state machine
tests next to the state machine, etc.) rather than duplicating the module
path here.

Populated starting Phase 4 (idempotency/failure tests) and Phase 5
(evaluation tests).
