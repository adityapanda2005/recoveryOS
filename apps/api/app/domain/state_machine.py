"""
Recovery workflow state machine.

This is deterministic, non-AI code. The AI layer (Phase 3) recommends
actions; this module is what actually moves a workflow between states, and
it is the ONLY code allowed to do so. Every transition is validated against
an explicit graph and persisted as an immutable row in workflow_transitions
before the workflow's current_state is updated.

Design choice: transitions are defined as a dict of
    {from_state: {allowed_to_states}}
rather than scattered if/else checks throughout the codebase, so the full
state graph is readable in one place and testable in isolation.
"""
from sqlalchemy.orm import Session

from app.db.models import RecoveryWorkflow, WorkflowTransition, WorkflowState


class InvalidTransitionError(Exception):
    """Raised when code attempts to move a workflow through a transition
    that isn't allowed by the state graph. This should never happen in
    correct application code — if it does, something upstream (AI layer,
    policy engine, action executor) has a bug, and we want a loud failure,
    not a silently corrupted workflow."""
    pass


TERMINAL_STATES = {
    WorkflowState.RECOVERED,
    WorkflowState.EXHAUSTED,
    WorkflowState.ABANDONED,
    WorkflowState.FAILED,
}

# The full state graph. Every key is a state a workflow can be in;
# every value is the set of states it's legal to move to from there.
ALLOWED_TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.DETECTED: {
        WorkflowState.ENRICHING,
        WorkflowState.ABANDONED,  # e.g. customer opted out immediately
    },
    WorkflowState.ENRICHING: {
        WorkflowState.DIAGNOSING,
        WorkflowState.FAILED,  # enrichment itself can fail (e.g. DB error)
    },
    WorkflowState.DIAGNOSING: {
        WorkflowState.SCORING,
        WorkflowState.FAILED,  # AI provider failure with no usable fallback
    },
    WorkflowState.SCORING: {
        WorkflowState.PLANNING,
    },
    WorkflowState.PLANNING: {
        WorkflowState.POLICY_CHECK,
    },
    WorkflowState.POLICY_CHECK: {
        WorkflowState.APPROVED,
        WorkflowState.ESCALATED,   # policy engine escalates to human
        WorkflowState.EXHAUSTED,   # policy engine says STOP (retry limit, low confidence, etc.)
    },
    WorkflowState.APPROVED: {
        WorkflowState.EXECUTING,
    },
    WorkflowState.EXECUTING: {
        WorkflowState.VERIFYING,
        WorkflowState.PENDING_VERIFICATION,  # external call timed out, outcome unknown
        WorkflowState.FAILED,                # action executor raised an unrecoverable error
    },
    WorkflowState.PENDING_VERIFICATION: {
        WorkflowState.VERIFYING,  # reconciliation job picks it back up
    },
    WorkflowState.VERIFYING: {
        WorkflowState.RECOVERED,
        WorkflowState.DETECTED,   # verification shows still-failed -> re-enter loop for next attempt
        WorkflowState.EXHAUSTED,  # verification shows failed AND retry limit now hit
        WorkflowState.ESCALATED,
    },
    # Terminal states: no outgoing transitions.
    WorkflowState.RECOVERED: set(),
    WorkflowState.ESCALATED: set(),
    WorkflowState.EXHAUSTED: set(),
    WorkflowState.ABANDONED: set(),
    WorkflowState.FAILED: set(),
}


def can_transition(from_state: WorkflowState, to_state: WorkflowState) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, set())


def transition(
    db: Session,
    workflow: RecoveryWorkflow,
    to_state: WorkflowState,
    reason: str | None = None,
) -> RecoveryWorkflow:
    """
    The only sanctioned way to move a workflow's state.

    1. Validates the transition against ALLOWED_TRANSITIONS.
    2. Persists an immutable WorkflowTransition row (audit trail).
    3. Updates the workflow's current_state and is_terminal flag.
    4. Commits.

    Raises InvalidTransitionError if the transition isn't legal — this is
    a hard stop, not a warning, because an invalid transition means a
    financial workflow's state would become inconsistent with reality.
    """
    from_state = workflow.current_state

    if not can_transition(from_state, to_state):
        raise InvalidTransitionError(
            f"Illegal transition: {from_state.value} -> {to_state.value} "
            f"for workflow {workflow.id}. Allowed from {from_state.value}: "
            f"{sorted(s.value for s in ALLOWED_TRANSITIONS.get(from_state, set()))}"
        )

    db.add(WorkflowTransition(
        workflow_id=workflow.id,
        from_state=from_state,
        to_state=to_state,
        reason=reason,
    ))

    workflow.current_state = to_state
    workflow.is_terminal = to_state in TERMINAL_STATES
    if to_state == WorkflowState.EXHAUSTED and reason:
        workflow.stop_reason = reason

    db.commit()
    db.refresh(workflow)
    return workflow


def create_workflow(db: Session, risk_event_id: str, merchant_id: str) -> RecoveryWorkflow:
    """Create a new workflow in its initial DETECTED state.
    Relies on the DB unique constraint on risk_event_id to make this
    idempotent — calling this twice for the same risk event raises an
    IntegrityError rather than silently creating a duplicate workflow."""
    workflow = RecoveryWorkflow(
        risk_event_id=risk_event_id,
        merchant_id=merchant_id,
        current_state=WorkflowState.DETECTED,
    )
    db.add(workflow)
    db.flush()  # get workflow.id without committing yet

    db.add(WorkflowTransition(
        workflow_id=workflow.id,
        from_state=None,
        to_state=WorkflowState.DETECTED,
        reason="Workflow created from revenue risk event",
    ))
    db.commit()
    db.refresh(workflow)
    return workflow
