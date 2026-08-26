from pydantic import BaseModel, Field

from app.db.models import RiskEventType, FailureReason, WorkflowState


class RiskEventCreate(BaseModel):
    merchant_id: str
    customer_id: str
    event_type: RiskEventType
    failure_reason: FailureReason
    amount_minor: int = Field(gt=0)
    currency: str = "INR"
    reference_type: str
    reference_id: str
    previous_attempts: int = 0
    source_event_id: str


class RiskEventOut(BaseModel):
    id: str
    merchant_id: str
    customer_id: str
    event_type: RiskEventType
    amount_minor: int
    currency: str

    class Config:
        from_attributes = True


class WorkflowOut(BaseModel):
    id: str
    risk_event_id: str
    current_state: WorkflowState
    retry_count: int
    communication_count: int
    recovered_amount_minor: int
    is_terminal: bool
    stop_reason: str | None = None

    class Config:
        from_attributes = True


class TransitionOut(BaseModel):
    from_state: WorkflowState | None
    to_state: WorkflowState
    reason: str | None
    created_at: str

    class Config:
        from_attributes = True
