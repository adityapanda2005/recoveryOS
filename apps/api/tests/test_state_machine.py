import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import WorkflowState
from app.domain.state_machine import (
    transition, create_workflow, can_transition, InvalidTransitionError,
)


class TestStateGraph:
    def test_valid_transition_is_allowed(self):
        assert can_transition(WorkflowState.DETECTED, WorkflowState.ENRICHING) is True

    def test_invalid_transition_is_rejected(self):
        # Cannot jump straight from DETECTED to RECOVERED, skipping the
        # entire diagnose/plan/policy/execute/verify pipeline.
        assert can_transition(WorkflowState.DETECTED, WorkflowState.RECOVERED) is False

    def test_terminal_states_have_no_outgoing_transitions(self):
        for terminal in [WorkflowState.RECOVERED, WorkflowState.EXHAUSTED,
                          WorkflowState.ABANDONED, WorkflowState.FAILED]:
            for candidate in WorkflowState:
                assert can_transition(terminal, candidate) is False, (
                    f"{terminal} should have no outgoing transitions, "
                    f"but {candidate} is allowed"
                )


class TestWorkflowCreation:
    def test_create_workflow_starts_detected(self, workflow):
        assert workflow.current_state == WorkflowState.DETECTED
        assert workflow.is_terminal is False

    def test_create_workflow_persists_initial_transition(self, db, workflow):
        from app.db.models import WorkflowTransition
        transitions = db.query(WorkflowTransition).filter_by(workflow_id=workflow.id).all()
        assert len(transitions) == 1
        assert transitions[0].from_state is None
        assert transitions[0].to_state == WorkflowState.DETECTED

    def test_duplicate_workflow_for_same_risk_event_is_rejected_by_db(self, db, workflow, risk_event, merchant_with_policy):
        """Critical test #1 (adapted to workflow-creation dedup): a second
        workflow cannot be created for a risk event that already has one.
        This is enforced by the UNIQUE constraint on
        recovery_workflows.risk_event_id at the DB level."""
        merchant, _ = merchant_with_policy
        with pytest.raises(IntegrityError):
            create_workflow(db, risk_event_id=risk_event.id, merchant_id=merchant.id)
        db.rollback()


class TestStateTransitions:
    def test_valid_transition_updates_state_and_persists_row(self, db, workflow):
        from app.db.models import WorkflowTransition

        updated = transition(db, workflow, WorkflowState.ENRICHING, reason="starting enrichment")
        assert updated.current_state == WorkflowState.ENRICHING

        transitions = (
            db.query(WorkflowTransition)
            .filter_by(workflow_id=workflow.id)
            .order_by(WorkflowTransition.created_at)
            .all()
        )
        assert len(transitions) == 2  # initial DETECTED + this one
        assert transitions[-1].from_state == WorkflowState.DETECTED
        assert transitions[-1].to_state == WorkflowState.ENRICHING
        assert transitions[-1].reason == "starting enrichment"

    def test_invalid_transition_raises_and_does_not_mutate_state(self, db, workflow):
        original_state = workflow.current_state
        with pytest.raises(InvalidTransitionError):
            transition(db, workflow, WorkflowState.RECOVERED)  # illegal jump

        db.refresh(workflow)
        assert workflow.current_state == original_state, (
            "An invalid transition attempt must not mutate workflow state"
        )

    def test_full_happy_path_reaches_recovered(self, db, workflow):
        """Walks the complete DETECTED -> ... -> RECOVERED path exactly as
        the real engine would drive it, proving the graph supports the
        actual product's core success scenario end to end."""
        path = [
            WorkflowState.ENRICHING,
            WorkflowState.DIAGNOSING,
            WorkflowState.SCORING,
            WorkflowState.PLANNING,
            WorkflowState.POLICY_CHECK,
            WorkflowState.APPROVED,
            WorkflowState.EXECUTING,
            WorkflowState.VERIFYING,
            WorkflowState.RECOVERED,
        ]
        for state in path:
            workflow = transition(db, workflow, state, reason=f"advancing to {state.value}")

        assert workflow.current_state == WorkflowState.RECOVERED
        assert workflow.is_terminal is True

    def test_stop_path_reaches_exhausted_with_reason(self, db, workflow):
        """Walks the path where policy decides to stop recovery — proves
        'knowing when not to act' is representable and auditable in the
        state machine, not just a policy-engine concept floating outside it."""
        path = [
            WorkflowState.ENRICHING,
            WorkflowState.DIAGNOSING,
            WorkflowState.SCORING,
            WorkflowState.PLANNING,
            WorkflowState.POLICY_CHECK,
        ]
        for state in path:
            workflow = transition(db, workflow, state)

        workflow = transition(
            db, workflow, WorkflowState.EXHAUSTED,
            reason="Low recoverability score and retry limit reached; stopping."
        )
        assert workflow.current_state == WorkflowState.EXHAUSTED
        assert workflow.is_terminal is True
        assert "stopping" in workflow.stop_reason.lower()

    def test_pending_verification_path_for_uncertain_payment_status(self, db, workflow):
        """Proves the PENDING_VERIFICATION safety valve exists in the graph:
        when an external call times out, the workflow must NOT be able to
        jump straight back to EXECUTING (which would risk a duplicate
        financial action) — it must go through VERIFYING first."""
        path = [
            WorkflowState.ENRICHING, WorkflowState.DIAGNOSING, WorkflowState.SCORING,
            WorkflowState.PLANNING, WorkflowState.POLICY_CHECK, WorkflowState.APPROVED,
            WorkflowState.EXECUTING,
        ]
        for state in path:
            workflow = transition(db, workflow, state)

        workflow = transition(
            db, workflow, WorkflowState.PENDING_VERIFICATION,
            reason="External payment API timed out; outcome unknown"
        )
        assert workflow.current_state == WorkflowState.PENDING_VERIFICATION

        # From here, the ONLY legal move is to VERIFYING (reconciliation),
        # never directly back to EXECUTING (which would risk a duplicate charge).
        assert can_transition(WorkflowState.PENDING_VERIFICATION, WorkflowState.EXECUTING) is False
        assert can_transition(WorkflowState.PENDING_VERIFICATION, WorkflowState.VERIFYING) is True

        workflow = transition(db, workflow, WorkflowState.VERIFYING, reason="reconciliation job ran")
        assert workflow.current_state == WorkflowState.VERIFYING
