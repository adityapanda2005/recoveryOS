from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import (
    RevenueRiskEvent, RecoveryWorkflow, WorkflowTransition, WorkflowState,
    Customer, Merchant, MerchantPolicy, AIDecision, PolicyDecisionRecord,
    PolicyDecision as PolicyDecisionEnum, Action, ActionStatus, RecommendedAction,
)
from app.domain.state_machine import create_workflow, transition, InvalidTransitionError
from app.api.schemas import RiskEventCreate, RiskEventOut, WorkflowOut, TransitionOut, AIDecisionOut
from app.ai.provider import AIContext
from app.ai.diagnosis_service import diagnose_and_persist
from app.policy import engine as policy_engine
from app.actions.executor import execute_action, reconcile_action, DuplicateActionError
from app.core.audit import log_audit_event

router = APIRouter()


@router.post("/risk-events", response_model=RiskEventOut, status_code=201)
def ingest_risk_event(payload: RiskEventCreate, db: Session = Depends(get_db)):
    """
    Ingest a revenue-risk event and immediately create its recovery workflow.

    Idempotent: source_event_id is unique per merchant at the DB level, so a
    duplicate webhook delivery (Failure Mode #8 from the failure matrix)
    cannot create a second event or a second workflow — it returns 409
    instead of silently duplicating state.
    """
    event = RevenueRiskEvent(**payload.model_dump())
    db.add(event)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Duplicate risk event: source_event_id already processed for this merchant.",
        )

    create_workflow(db, risk_event_id=event.id, merchant_id=event.merchant_id)
    db.commit()
    db.refresh(event)
    return event


@router.get("/workflows/{workflow_id}", response_model=WorkflowOut)
def get_workflow(workflow_id: str, db: Session = Depends(get_db)):
    wf = db.query(RecoveryWorkflow).filter_by(id=workflow_id).first()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return wf


@router.get("/workflows/{workflow_id}/transitions", response_model=list[TransitionOut])
def get_workflow_transitions(workflow_id: str, db: Session = Depends(get_db)):
    wf = db.query(RecoveryWorkflow).filter_by(id=workflow_id).first()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    rows = (
        db.query(WorkflowTransition)
        .filter_by(workflow_id=workflow_id)
        .order_by(WorkflowTransition.created_at)
        .all()
    )
    return [
        TransitionOut(
            from_state=r.from_state, to_state=r.to_state, reason=r.reason,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


@router.post("/workflows/{workflow_id}/advance/{to_state}", response_model=WorkflowOut)
def advance_workflow(workflow_id: str, to_state: str, reason: str | None = None, db: Session = Depends(get_db)):
    """
    Manual/debug endpoint to drive a workflow's state directly. The real
    engine (Phase 3+) will call the state machine internally as part of the
    diagnose -> plan -> policy -> execute -> verify pipeline; this endpoint
    exists so the state machine is independently exercisable via the API
    right now, and so the Failure Lab (Phase 4) has a hook to force specific
    states for demo scenarios.
    """
    from app.db.models import WorkflowState

    wf = db.query(RecoveryWorkflow).filter_by(id=workflow_id).first()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")

    try:
        target = WorkflowState(to_state)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown state: {to_state}")

    try:
        wf = transition(db, wf, target, reason=reason)
    except InvalidTransitionError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return wf


@router.post("/workflows/{workflow_id}/diagnose", response_model=AIDecisionOut)
def diagnose_workflow(workflow_id: str, db: Session = Depends(get_db)):
    """
    Run AI diagnosis for a workflow currently in ENRICHING, and advance it
    to DIAGNOSING then SCORING on success (or FAILED if even the fallback
    path can't be persisted — which should not happen in practice, since
    diagnose_and_persist always returns a decision, real or fallback).

    Requires the workflow to be in ENRICHING so this can't be called twice
    on the same workflow at the wrong point in its lifecycle — the state
    machine, not this endpoint, is the source of truth for what's legal.
    """
    wf = db.query(RecoveryWorkflow).filter_by(id=workflow_id).first()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if wf.current_state != WorkflowState.ENRICHING:
        raise HTTPException(
            status_code=409,
            detail=f"Workflow must be in ENRICHING to diagnose; currently {wf.current_state.value}.",
        )

    risk_event = db.query(RevenueRiskEvent).filter_by(id=wf.risk_event_id).first()
    customer = db.query(Customer).filter_by(id=risk_event.customer_id).first()
    policy = db.query(MerchantPolicy).filter_by(merchant_id=wf.merchant_id).first()

    context = AIContext(
        risk_event_type=risk_event.event_type.value,
        failure_reason=risk_event.failure_reason.value,
        amount_minor=risk_event.amount_minor,
        currency=risk_event.currency,
        previous_attempts=risk_event.previous_attempts,
        customer_historical_successful_payments=customer.historical_successful_payments,
        customer_historical_failed_payments=customer.historical_failed_payments,
        customer_is_opted_out=customer.is_opted_out,
        merchant_max_retry_attempts=policy.max_retry_attempts,
        merchant_allows_incentives=policy.allow_incentives,
    )

    try:
        wf = transition(db, wf, WorkflowState.DIAGNOSING, reason="AI diagnosis started")
    except InvalidTransitionError as e:
        raise HTTPException(status_code=422, detail=str(e))

    decision = diagnose_and_persist(db, workflow_id=wf.id, context=context)

    # Diagnosis always produces a decision (real or fallback) -> SCORING.
    # A hard AI-layer crash that somehow escapes diagnose_and_persist would
    # leave the workflow correctly stuck in DIAGNOSING rather than silently
    # advancing past a failure we don't actually understand.
    transition(db, wf, WorkflowState.SCORING, reason="AI decision recorded")

    return decision


@router.post("/workflows/{workflow_id}/plan", response_model=WorkflowOut)
def plan_and_check_policy(workflow_id: str, db: Session = Depends(get_db)):
    """
    Take the most recent AI decision for this workflow (must be in SCORING)
    and run it through the deterministic policy engine. This is the
    structural enforcement of "AI proposes, policy validates": the policy
    engine here has no knowledge of prompts or models, only the already-
    persisted recommended_action + confidence + amount + customer state.

    Advances: SCORING -> PLANNING -> POLICY_CHECK -> one of
    APPROVED / ESCALATED / EXHAUSTED, per the policy verdict.
    """
    wf = db.query(RecoveryWorkflow).filter_by(id=workflow_id).first()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if wf.current_state != WorkflowState.SCORING:
        raise HTTPException(
            status_code=409,
            detail=f"Workflow must be in SCORING to plan; currently {wf.current_state.value}.",
        )

    decision = (
        db.query(AIDecision)
        .filter_by(workflow_id=wf.id)
        .order_by(AIDecision.created_at.desc())
        .first()
    )
    if not decision:
        raise HTTPException(status_code=500, detail="No AI decision found for this workflow.")

    risk_event = db.query(RevenueRiskEvent).filter_by(id=wf.risk_event_id).first()
    customer = db.query(Customer).filter_by(id=risk_event.customer_id).first()
    policy = db.query(MerchantPolicy).filter_by(merchant_id=wf.merchant_id).first()

    transition(db, wf, WorkflowState.PLANNING, reason="Recovery plan formed from AI decision")
    transition(db, wf, WorkflowState.POLICY_CHECK, reason="Evaluating against deterministic policy")

    verdict = policy_engine.evaluate(
        workflow=wf,
        policy=policy,
        recommended_action=decision.recommended_action,
        confidence=float(decision.confidence),
        amount_minor=risk_event.amount_minor,
        customer_is_opted_out=customer.is_opted_out,
    )

    record = PolicyDecisionRecord(
        workflow_id=wf.id,
        ai_decision_id=decision.id,
        requested_action=decision.recommended_action,
        decision=verdict.decision,
        rule_triggered=verdict.rule_triggered,
        explanation=verdict.explanation,
    )
    db.add(record)
    log_audit_event(
        db, actor="policy_engine", event_type="policy.decision",
        description=f"Policy verdict {verdict.decision.value} (rule: {verdict.rule_triggered}): "
                    f"{verdict.explanation}",
        workflow_id=wf.id,
        metadata={"rule_triggered": verdict.rule_triggered, "requested_action": decision.recommended_action.value},
    )
    db.commit()

    if verdict.decision == PolicyDecisionEnum.ALLOW:
        wf = transition(db, wf, WorkflowState.APPROVED, reason=verdict.explanation)
    elif verdict.decision == PolicyDecisionEnum.ESCALATE:
        wf = transition(db, wf, WorkflowState.ESCALATED, reason=verdict.explanation)
    else:  # BLOCK
        wf = transition(db, wf, WorkflowState.EXHAUSTED, reason=verdict.explanation)

    return wf


@router.post("/workflows/{workflow_id}/execute", response_model=WorkflowOut)
def execute_approved_action(workflow_id: str, db: Session = Depends(get_db)):
    """
    Execute the approved action for a workflow in APPROVED state.

    Advances: APPROVED -> EXECUTING -> one of:
      - VERIFYING -> RECOVERED   (action succeeded)
      - VERIFYING -> DETECTED    (action failed but retries remain --
                                   re-enters the loop for the next attempt)
      - PENDING_VERIFICATION     (action outcome unknown -- external call
                                   timed out; caller must reconcile before
                                   anything retries)
    """
    wf = db.query(RecoveryWorkflow).filter_by(id=workflow_id).first()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if wf.current_state != WorkflowState.APPROVED:
        raise HTTPException(
            status_code=409,
            detail=f"Workflow must be in APPROVED to execute; currently {wf.current_state.value}.",
        )

    record = (
        db.query(PolicyDecisionRecord)
        .filter_by(workflow_id=wf.id, decision=PolicyDecisionEnum.ALLOW)
        .order_by(PolicyDecisionRecord.created_at.desc())
        .first()
    )
    if not record:
        raise HTTPException(status_code=500, detail="No ALLOW policy decision found for this workflow.")

    risk_event = db.query(RevenueRiskEvent).filter_by(id=wf.risk_event_id).first()
    verdict = policy_engine.PolicyVerdict(
        decision=record.decision, rule_triggered=record.rule_triggered, explanation=record.explanation,
    )

    wf = transition(db, wf, WorkflowState.EXECUTING, reason="Executing approved action")

    try:
        action = execute_action(
            db, workflow_id=wf.id, policy_decision_id=record.id, verdict=verdict,
            recommended_action=record.requested_action, amount_minor=risk_event.amount_minor,
            attempt_number=wf.retry_count + 1,
        )
    except DuplicateActionError as e:
        raise HTTPException(status_code=409, detail=str(e))

    if action.status == ActionStatus.UNCERTAIN:
        wf = transition(
            db, wf, WorkflowState.PENDING_VERIFICATION,
            reason="Action outcome unknown (simulated timeout); reconciliation required before any retry.",
        )
        return wf

    if action.action_type.value in {"RETRY_PAYMENT"}:
        wf.retry_count += 1
        db.commit()
        db.refresh(wf)

    wf = transition(db, wf, WorkflowState.VERIFYING, reason="Verifying action outcome")

    if action.status == ActionStatus.SUCCEEDED:
        wf.recovered_amount_minor = risk_event.amount_minor
        db.commit()
        db.refresh(wf)
        wf = transition(db, wf, WorkflowState.RECOVERED, reason="Action succeeded; revenue recovered.")
    else:
        # Retry/communication limits are enforced by the policy engine at
        # the next plan cycle (Rule 4/5 in app/policy/engine.py), not here --
        # re-entering DETECTED just means "try the loop again"; if the
        # limit is actually exhausted, the next /plan call will BLOCK and
        # the workflow will land on EXHAUSTED instead of APPROVED.
        wf = transition(
            db, wf, WorkflowState.DETECTED,
            reason="Action failed; re-entering detection loop for possible next attempt.",
        )

    return wf


@router.post("/workflows/{workflow_id}/reconcile", response_model=WorkflowOut)
def reconcile_pending_workflow(workflow_id: str, db: Session = Depends(get_db)):
    """
    For a workflow in PENDING_VERIFICATION: find its UNCERTAIN action,
    reconcile the actual external outcome, and resume the workflow.
    This is the code path that guarantees an uncertain payment status is
    never resolved by blindly retrying -- reconciliation always runs
    first, per the product spec's payment-safety requirement.
    """
    wf = db.query(RecoveryWorkflow).filter_by(id=workflow_id).first()
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    if wf.current_state != WorkflowState.PENDING_VERIFICATION:
        raise HTTPException(
            status_code=409,
            detail=f"Workflow must be in PENDING_VERIFICATION to reconcile; currently {wf.current_state.value}.",
        )

    action = (
        db.query(Action)
        .filter_by(workflow_id=wf.id, status=ActionStatus.UNCERTAIN)
        .order_by(Action.created_at.desc())
        .first()
    )
    if not action:
        raise HTTPException(status_code=500, detail="No UNCERTAIN action found for this workflow.")

    action = reconcile_action(db, action)
    risk_event = db.query(RevenueRiskEvent).filter_by(id=wf.risk_event_id).first()

    wf = transition(db, wf, WorkflowState.VERIFYING, reason="Reconciliation complete; verifying outcome")

    if action.status == ActionStatus.SUCCEEDED:
        wf.recovered_amount_minor = risk_event.amount_minor
        db.commit()
        db.refresh(wf)
        wf = transition(db, wf, WorkflowState.RECOVERED, reason="Reconciliation confirmed success; revenue recovered.")
    else:
        wf = transition(
            db, wf, WorkflowState.DETECTED,
            reason="Reconciliation confirmed the original attempt did not succeed; re-entering detection loop.",
        )

    return wf
