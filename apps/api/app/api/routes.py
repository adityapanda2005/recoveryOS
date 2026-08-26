from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import (
    RevenueRiskEvent, RecoveryWorkflow, WorkflowTransition, WorkflowState,
    Customer, Merchant, MerchantPolicy,
)
from app.domain.state_machine import create_workflow, transition, InvalidTransitionError
from app.api.schemas import RiskEventCreate, RiskEventOut, WorkflowOut, TransitionOut, AIDecisionOut
from app.ai.provider import AIContext
from app.ai.diagnosis_service import diagnose_and_persist

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
