"""
Action executor.

This is the only code path that executes an approved recovery action. It
receives a PolicyVerdict that already says ALLOW (never called for
BLOCK/ESCALATE) and:

  1. Generates a deterministic idempotency_key for this exact
     (workflow, action_type, attempt_number) so retrying the same logical
     action never creates two Action rows -- enforced at the DB level via
     the unique constraint on actions.idempotency_key, not just here.
  2. Calls the simulator (or, in a real deployment, the real Razorpay API
     behind the same interface).
  3. On SUCCESS: records the attempt, marks the Action SUCCEEDED.
  4. On TRANSIENT_FAILURE/PERMANENT_FAILURE: records the attempt, marks
     the Action FAILED -- the AI/policy loop decides what happens next on
     the *next* cycle, this layer does not retry on its own.
  5. On TIMEOUT: the external outcome is genuinely unknown. The Action is
     marked UNCERTAIN and the workflow moves to PENDING_VERIFICATION.
     Critically: this code does NOT retry the payment. A blind retry here
     could double-charge a customer if the original attempt actually
     succeeded on the provider's side. Reconciliation must run first.
"""
import logging

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    Action, ActionAttempt, ActionType, ActionStatus, RecommendedAction,
)
from app.policy.engine import PolicyVerdict
from app.core.audit import log_audit_event
from app.simulation.payment_simulator import (
    simulate_payment_retry, reconcile_uncertain_action,
    SimulatedOutcome, TimeoutSimulated, RateLimited,
)

logger = logging.getLogger("recoveros.actions.executor")

_ACTION_TYPE_FOR_RECOMMENDATION = {
    RecommendedAction.RETRY_PAYMENT: ActionType.RETRY_PAYMENT,
    RecommendedAction.DELAY_AND_RETRY: ActionType.RETRY_PAYMENT,
    RecommendedAction.SEND_PAYMENT_LINK: ActionType.SEND_PAYMENT_LINK,
    RecommendedAction.SEND_REMINDER: ActionType.SEND_NOTIFICATION,
    RecommendedAction.REQUEST_CUSTOMER_ACTION: ActionType.SEND_NOTIFICATION,
    RecommendedAction.OFFER_INCENTIVE: ActionType.APPLY_INCENTIVE,
    RecommendedAction.ESCALATE_TO_HUMAN: ActionType.ESCALATE,
}


def build_idempotency_key(workflow_id: str, action_type: ActionType, attempt_number: int) -> str:
    """Deterministic, human-legible idempotency key. Same inputs always
    produce the same key, so calling execute_action twice for the same
    logical attempt (e.g. a retried HTTP request from an upstream caller)
    hits the DB unique constraint instead of creating a duplicate Action."""
    return f"{workflow_id}:{action_type.value}:{attempt_number}"


class DuplicateActionError(Exception):
    pass


def execute_action(
    db: Session,
    *,
    workflow_id: str,
    policy_decision_id: str,
    verdict: PolicyVerdict,
    recommended_action: RecommendedAction,
    amount_minor: int | None,
    attempt_number: int,
    seed: int = 0,
) -> Action:
    from app.db.models import PolicyDecision as PolicyDecisionEnum
    if verdict.decision != PolicyDecisionEnum.ALLOW:
        raise ValueError(
            f"execute_action called with a non-ALLOW verdict ({verdict.decision}); "
            f"this must never happen -- only the policy engine's ALLOW result may "
            f"reach the action executor."
        )

    action_type = _ACTION_TYPE_FOR_RECOMMENDATION.get(recommended_action)
    if action_type is None:
        raise ValueError(f"No ActionType mapping for {recommended_action}")

    idempotency_key = build_idempotency_key(workflow_id, action_type, attempt_number)

    action = Action(
        workflow_id=workflow_id,
        policy_decision_id=policy_decision_id,
        action_type=action_type,
        idempotency_key=idempotency_key,
        status=ActionStatus.PENDING,
        amount_minor=amount_minor,
        attempt_number=attempt_number,
    )
    db.add(action)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        # This is the duplicate-action-does-not-execute-twice guarantee,
        # enforced structurally: a second call with the same
        # (workflow, action_type, attempt_number) cannot get a new row.
        log_audit_event(
            db, actor="system", event_type="action.duplicate_blocked",
            description=f"Refused duplicate execution for idempotency_key={idempotency_key}",
            workflow_id=workflow_id, metadata={"idempotency_key": idempotency_key},
        )
        db.commit()
        raise DuplicateActionError(
            f"Action with idempotency_key={idempotency_key} already exists; "
            f"refusing to execute twice."
        )

    action.status = ActionStatus.IN_FLIGHT
    db.commit()

    try:
        result = simulate_payment_retry(idempotency_key, seed=seed)
    except TimeoutSimulated as e:
        logger.warning("Action %s timed out: %s", action.id, e)
        action.status = ActionStatus.UNCERTAIN
        db.add(ActionAttempt(
            action_id=action.id,
            attempt_id=f"{idempotency_key}:attempt:{attempt_number}",
            outcome="TIMEOUT",
            error_detail=str(e),
        ))
        log_audit_event(
            db, actor="system", event_type="action.uncertain_timeout",
            description=f"Action {action.id} timed out; external outcome unknown, "
                        f"moved to UNCERTAIN pending reconciliation (no blind retry).",
            workflow_id=workflow_id, metadata={"action_id": action.id, "idempotency_key": idempotency_key},
        )
        db.commit()
        db.refresh(action)
        return action
    except RateLimited as e:
        logger.warning("Action %s rate limited: %s", action.id, e)
        action.status = ActionStatus.FAILED
        db.add(ActionAttempt(
            action_id=action.id,
            attempt_id=f"{idempotency_key}:attempt:{attempt_number}",
            outcome="RATE_LIMITED",
            error_detail=str(e),
        ))
        log_audit_event(
            db, actor="system", event_type="action.rate_limited",
            description=f"Action {action.id} was rate limited by the external provider.",
            workflow_id=workflow_id, metadata={"action_id": action.id, "idempotency_key": idempotency_key},
        )
        db.commit()
        db.refresh(action)
        return action

    db.add(ActionAttempt(
        action_id=action.id,
        attempt_id=f"{idempotency_key}:attempt:{attempt_number}",
        outcome=result.outcome.value,
        external_reference=result.external_reference,
        error_detail=result.error_detail,
        latency_ms=result.latency_ms,
    ))

    action.status = (
        ActionStatus.SUCCEEDED if result.outcome == SimulatedOutcome.SUCCESS
        else ActionStatus.FAILED
    )
    log_audit_event(
        db, actor="system", event_type=f"action.{action.status.value.lower()}",
        description=f"Action {action.id} ({action_type.value}) completed with status {action.status.value}.",
        workflow_id=workflow_id,
        metadata={"action_id": action.id, "idempotency_key": idempotency_key, "outcome": result.outcome.value},
    )
    db.commit()
    db.refresh(action)
    return action


def reconcile_action(db: Session, action: Action, *, seed: int = 0) -> Action:
    """
    Called for any Action stuck in UNCERTAIN (i.e. its execution attempt
    timed out). This is the reconciliation step the spec requires before
    any further action is taken -- we ask "what actually happened" rather
    than assuming failure and blindly retrying, which could double-charge
    a customer if the original attempt had in fact succeeded.
    """
    if action.status != ActionStatus.UNCERTAIN:
        raise ValueError(
            f"reconcile_action called on Action {action.id} with status "
            f"{action.status}, expected UNCERTAIN."
        )

    result = reconcile_uncertain_action(action.idempotency_key, seed=seed)

    db.add(ActionAttempt(
        action_id=action.id,
        attempt_id=f"{action.idempotency_key}:reconcile",
        outcome=f"RECONCILED_{result.outcome.value}",
        external_reference=result.external_reference,
        error_detail=result.error_detail,
        latency_ms=result.latency_ms,
    ))

    action.status = (
        ActionStatus.SUCCEEDED if result.outcome == SimulatedOutcome.SUCCESS
        else ActionStatus.RECONCILED  # reconciled-as-failed: safe to retry a *new* attempt now
    )
    log_audit_event(
        db, actor="system", event_type="action.reconciled",
        description=f"Reconciliation for action {action.id} resolved UNCERTAIN -> "
                    f"{action.status.value} (external outcome: {result.outcome.value}).",
        workflow_id=action.workflow_id,
        metadata={"action_id": action.id, "resolved_outcome": result.outcome.value},
    )
    db.commit()
    db.refresh(action)
    return action
