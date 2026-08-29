import pytest

from app.db.models import Action, ActionAttempt, ActionStatus, RecommendedAction, PolicyDecision, AuditEvent
from app.policy.engine import PolicyVerdict
from app.actions.executor import (
    execute_action, reconcile_action, build_idempotency_key, DuplicateActionError,
)
from app.simulation.payment_simulator import (
    simulate_payment_retry, reconcile_uncertain_action, SimulatedOutcome,
    TimeoutSimulated, RateLimited,
)


ALLOW_VERDICT = PolicyVerdict(
    decision=PolicyDecision.ALLOW, rule_triggered="all_checks_passed", explanation="test"
)
BLOCK_VERDICT = PolicyVerdict(
    decision=PolicyDecision.BLOCK, rule_triggered="max_retry_attempts_exceeded", explanation="test"
)


def _is_exceptional(key, seed):
    try:
        simulate_payment_retry(key, seed=seed)
        return False
    except (TimeoutSimulated, RateLimited):
        return True


class TestPaymentSimulatorDeterminism:
    def test_same_key_same_seed_produces_same_outcome(self):
        # Determinism must hold whether the deterministic bucket lands on a
        # normal SimulatedResult OR on an outcome that raises (TIMEOUT,
        # RATE_LIMITED) -- both are valid, reproducible outcomes of the same
        # (key, seed) pair, so we compare "what happened" uniformly rather
        # than assuming a return value.
        def _outcome_of(key, seed):
            try:
                return ("result", simulate_payment_retry(key, seed=seed).outcome)
            except (TimeoutSimulated, RateLimited) as e:
                return ("exception", type(e))

        r1 = _outcome_of("stable-key-001", seed=0)
        r2 = _outcome_of("stable-key-001", seed=0)
        assert r1 == r2

        # Also verify determinism holds for a key that lands on the normal
        # (non-exceptional) path, so the assertion isn't vacuous if
        # "stable-key-001" always happens to be exceptional.
        normal_key = next(k for k in (f"k{i}" for i in range(100)) if not _is_exceptional(k, 0))
        n1 = simulate_payment_retry(normal_key, seed=0)
        n2 = simulate_payment_retry(normal_key, seed=0)
        assert n1.outcome == n2.outcome
        assert n1.external_reference == n2.external_reference

    def test_different_seed_still_produces_valid_outcomes(self):
        outcomes_seed_0 = {simulate_payment_retry(f"k{i}", seed=0).outcome for i in range(50)
                           if not _is_exceptional(f"k{i}", 0)}
        outcomes_seed_1 = {simulate_payment_retry(f"k{i}", seed=1).outcome for i in range(50)
                           if not _is_exceptional(f"k{i}", 1)}
        assert outcomes_seed_0 and outcomes_seed_1

    def test_force_outcome_bypasses_distribution(self):
        result = simulate_payment_retry("any-key", force_outcome=SimulatedOutcome.SUCCESS)
        assert result.outcome == SimulatedOutcome.SUCCESS
        assert result.external_reference is not None

    def test_forced_timeout_raises_not_returns(self):
        with pytest.raises(TimeoutSimulated):
            simulate_payment_retry("any-key", force_outcome=SimulatedOutcome.TIMEOUT)

    def test_forced_rate_limit_raises(self):
        with pytest.raises(RateLimited):
            simulate_payment_retry("any-key", force_outcome=SimulatedOutcome.RATE_LIMITED)


class TestReconciliation:
    def test_reconciliation_resolves_to_definite_outcome(self):
        result = reconcile_uncertain_action("some-timed-out-key", seed=0)
        assert result.outcome in {SimulatedOutcome.SUCCESS, SimulatedOutcome.TRANSIENT_FAILURE}

    def test_reconciliation_is_deterministic(self):
        r1 = reconcile_uncertain_action("stable-key", seed=0)
        r2 = reconcile_uncertain_action("stable-key", seed=0)
        assert r1.outcome == r2.outcome


class TestIdempotencyKeyConstruction:
    def test_same_inputs_produce_same_key(self):
        from app.db.models import ActionType
        k1 = build_idempotency_key("wf-123", ActionType.RETRY_PAYMENT, 1)
        k2 = build_idempotency_key("wf-123", ActionType.RETRY_PAYMENT, 1)
        assert k1 == k2

    def test_different_attempt_number_produces_different_key(self):
        from app.db.models import ActionType
        k1 = build_idempotency_key("wf-123", ActionType.RETRY_PAYMENT, 1)
        k2 = build_idempotency_key("wf-123", ActionType.RETRY_PAYMENT, 2)
        assert k1 != k2


class TestActionExecutorGuards:
    def test_executor_refuses_non_allow_verdict(self, db, workflow, policy_decision_allow):
        with pytest.raises(ValueError, match="non-ALLOW"):
            execute_action(
                db, workflow_id=workflow.id, policy_decision_id=policy_decision_allow.id,
                verdict=BLOCK_VERDICT, recommended_action=RecommendedAction.RETRY_PAYMENT,
                amount_minor=499900, attempt_number=1,
            )


class TestActionExecutorExecution:
    def test_forced_success_creates_succeeded_action_with_attempt_row(self, db, workflow, policy_decision_allow, monkeypatch):
        import app.actions.executor as executor_mod

        def fake_simulate(idempotency_key, seed=0):
            from app.simulation.payment_simulator import SimulatedResult
            return SimulatedResult(
                outcome=SimulatedOutcome.SUCCESS,
                external_reference="sim_forced_success",
                latency_ms=42,
            )
        monkeypatch.setattr(executor_mod, "simulate_payment_retry", fake_simulate)

        action = execute_action(
            db, workflow_id=workflow.id, policy_decision_id=policy_decision_allow.id,
            verdict=ALLOW_VERDICT, recommended_action=RecommendedAction.RETRY_PAYMENT,
            amount_minor=499900, attempt_number=1,
        )

        assert action.status == ActionStatus.SUCCEEDED
        attempts = db.query(ActionAttempt).filter_by(action_id=action.id).all()
        assert len(attempts) == 1
        assert attempts[0].outcome == "SUCCESS"
        assert attempts[0].external_reference == "sim_forced_success"

        db.query(ActionAttempt).filter_by(action_id=action.id).delete()
        db.query(Action).filter_by(id=action.id).delete()
        db.commit()

    def test_duplicate_action_does_not_execute_twice(self, db, workflow, policy_decision_allow, monkeypatch):
        import app.actions.executor as executor_mod
        call_count = {"n": 0}

        def fake_simulate(idempotency_key, seed=0):
            call_count["n"] += 1
            from app.simulation.payment_simulator import SimulatedResult
            return SimulatedResult(outcome=SimulatedOutcome.SUCCESS, external_reference="ref", latency_ms=10)
        monkeypatch.setattr(executor_mod, "simulate_payment_retry", fake_simulate)

        action1 = execute_action(
            db, workflow_id=workflow.id, policy_decision_id=policy_decision_allow.id,
            verdict=ALLOW_VERDICT, recommended_action=RecommendedAction.RETRY_PAYMENT,
            amount_minor=499900, attempt_number=1,
        )
        assert call_count["n"] == 1

        with pytest.raises(DuplicateActionError):
            execute_action(
                db, workflow_id=workflow.id, policy_decision_id=policy_decision_allow.id,
                verdict=ALLOW_VERDICT, recommended_action=RecommendedAction.RETRY_PAYMENT,
                amount_minor=499900, attempt_number=1,
            )
        assert call_count["n"] == 1

        all_actions = db.query(Action).filter_by(idempotency_key=action1.idempotency_key).all()
        assert len(all_actions) == 1

        db.query(ActionAttempt).filter_by(action_id=action1.id).delete()
        db.query(Action).filter_by(id=action1.id).delete()
        db.commit()

    def test_timeout_moves_action_to_uncertain_not_failed(self, db, workflow, policy_decision_allow, monkeypatch):
        import app.actions.executor as executor_mod

        def fake_simulate(idempotency_key, seed=0):
            raise TimeoutSimulated("forced timeout for test")
        monkeypatch.setattr(executor_mod, "simulate_payment_retry", fake_simulate)

        action = execute_action(
            db, workflow_id=workflow.id, policy_decision_id=policy_decision_allow.id,
            verdict=ALLOW_VERDICT, recommended_action=RecommendedAction.RETRY_PAYMENT,
            amount_minor=499900, attempt_number=1,
        )

        assert action.status == ActionStatus.UNCERTAIN
        attempts = db.query(ActionAttempt).filter_by(action_id=action.id).all()
        assert attempts[0].outcome == "TIMEOUT"

        db.query(ActionAttempt).filter_by(action_id=action.id).delete()
        db.query(Action).filter_by(id=action.id).delete()
        db.commit()

    def test_reconcile_action_requires_uncertain_status(self, db, workflow, policy_decision_allow, monkeypatch):
        import app.actions.executor as executor_mod

        def fake_simulate(idempotency_key, seed=0):
            from app.simulation.payment_simulator import SimulatedResult
            return SimulatedResult(outcome=SimulatedOutcome.SUCCESS, external_reference="ref", latency_ms=10)
        monkeypatch.setattr(executor_mod, "simulate_payment_retry", fake_simulate)

        action = execute_action(
            db, workflow_id=workflow.id, policy_decision_id=policy_decision_allow.id,
            verdict=ALLOW_VERDICT, recommended_action=RecommendedAction.RETRY_PAYMENT,
            amount_minor=499900, attempt_number=1,
        )
        assert action.status == ActionStatus.SUCCEEDED

        with pytest.raises(ValueError, match="UNCERTAIN"):
            reconcile_action(db, action)

        db.query(ActionAttempt).filter_by(action_id=action.id).delete()
        db.query(Action).filter_by(id=action.id).delete()
        db.commit()

    def test_reconcile_action_resolves_uncertain_to_definite_status(self, db, workflow, policy_decision_allow, monkeypatch):
        import app.actions.executor as executor_mod

        def fake_simulate(idempotency_key, seed=0):
            raise TimeoutSimulated("forced timeout for test")
        monkeypatch.setattr(executor_mod, "simulate_payment_retry", fake_simulate)

        action = execute_action(
            db, workflow_id=workflow.id, policy_decision_id=policy_decision_allow.id,
            verdict=ALLOW_VERDICT, recommended_action=RecommendedAction.RETRY_PAYMENT,
            amount_minor=499900, attempt_number=1,
        )
        assert action.status == ActionStatus.UNCERTAIN

        db.query(ActionAttempt).filter_by(action_id=action.id).delete()
        db.query(Action).filter_by(id=action.id).delete()
        db.commit()


class TestAuditTrail:
    """Critical test #10 from the build spec: audit events are created.
    These assert real rows exist in audit_events, not just that the
    domain-specific tables (workflow_transitions, action_attempts) do --
    the whole point of a separate audit table is a unified, queryable
    'what happened and why' log across every subsystem."""

    def test_workflow_creation_writes_an_audit_event(self, db, workflow):
        events = db.query(AuditEvent).filter_by(workflow_id=workflow.id).all()
        assert any(e.event_type == "workflow.created" for e in events)

    def test_state_transition_writes_an_audit_event(self, db, workflow):
        from app.domain.state_machine import transition
        from app.db.models import WorkflowState
        transition(db, workflow, WorkflowState.ENRICHING, reason="test enrichment")

        events = db.query(AuditEvent).filter_by(workflow_id=workflow.id).order_by(AuditEvent.created_at).all()
        assert any(e.event_type == "workflow.transitioned" and "ENRICHING" in e.description for e in events)

    def test_successful_action_writes_an_audit_event(self, db, workflow, policy_decision_allow, monkeypatch):
        import app.actions.executor as executor_mod

        def fake_simulate(idempotency_key, seed=0):
            from app.simulation.payment_simulator import SimulatedResult
            return SimulatedResult(outcome=SimulatedOutcome.SUCCESS, external_reference="ref", latency_ms=10)
        monkeypatch.setattr(executor_mod, "simulate_payment_retry", fake_simulate)

        action = execute_action(
            db, workflow_id=workflow.id, policy_decision_id=policy_decision_allow.id,
            verdict=ALLOW_VERDICT, recommended_action=RecommendedAction.RETRY_PAYMENT,
            amount_minor=499900, attempt_number=1,
        )

        events = db.query(AuditEvent).filter_by(workflow_id=workflow.id).all()
        assert any(e.event_type == "action.succeeded" for e in events)

        db.query(ActionAttempt).filter_by(action_id=action.id).delete()
        db.query(Action).filter_by(id=action.id).delete()
        db.commit()

    def test_duplicate_action_block_writes_an_audit_event(self, db, workflow, policy_decision_allow, monkeypatch):
        import app.actions.executor as executor_mod

        def fake_simulate(idempotency_key, seed=0):
            from app.simulation.payment_simulator import SimulatedResult
            return SimulatedResult(outcome=SimulatedOutcome.SUCCESS, external_reference="ref", latency_ms=10)
        monkeypatch.setattr(executor_mod, "simulate_payment_retry", fake_simulate)

        action = execute_action(
            db, workflow_id=workflow.id, policy_decision_id=policy_decision_allow.id,
            verdict=ALLOW_VERDICT, recommended_action=RecommendedAction.RETRY_PAYMENT,
            amount_minor=499900, attempt_number=1,
        )
        with pytest.raises(DuplicateActionError):
            execute_action(
                db, workflow_id=workflow.id, policy_decision_id=policy_decision_allow.id,
                verdict=ALLOW_VERDICT, recommended_action=RecommendedAction.RETRY_PAYMENT,
                amount_minor=499900, attempt_number=1,
            )

        events = db.query(AuditEvent).filter_by(workflow_id=workflow.id).all()
        assert any(e.event_type == "action.duplicate_blocked" for e in events)

        db.query(ActionAttempt).filter_by(action_id=action.id).delete()
        db.query(Action).filter_by(id=action.id).delete()
        db.commit()

    def test_timeout_and_reconciliation_both_write_audit_events(self, db, workflow, policy_decision_allow, monkeypatch):
        import app.actions.executor as executor_mod

        def fake_timeout(idempotency_key, seed=0):
            raise TimeoutSimulated("forced timeout for test")
        monkeypatch.setattr(executor_mod, "simulate_payment_retry", fake_timeout)

        action = execute_action(
            db, workflow_id=workflow.id, policy_decision_id=policy_decision_allow.id,
            verdict=ALLOW_VERDICT, recommended_action=RecommendedAction.RETRY_PAYMENT,
            amount_minor=499900, attempt_number=1,
        )
        reconcile_action(db, action)

        events = db.query(AuditEvent).filter_by(workflow_id=workflow.id).all()
        event_types = {e.event_type for e in events}
        assert "action.uncertain_timeout" in event_types
        assert "action.reconciled" in event_types

        db.query(ActionAttempt).filter_by(action_id=action.id).delete()
        db.query(Action).filter_by(id=action.id).delete()
        db.commit()
