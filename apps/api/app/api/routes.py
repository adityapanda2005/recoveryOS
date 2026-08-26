from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import RevenueRiskEvent, RecoveryWorkflow, WorkflowTransition
from app.domain.state_machine import create_workflow, transition, InvalidTransitionError
from app.api.schemas import RiskEventCreate, RiskEventOut, WorkflowOut, TransitionOut

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
