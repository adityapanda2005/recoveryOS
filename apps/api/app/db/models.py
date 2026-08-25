"""
RecoverOS database schema.

Design principles enforced here:
- UUID primary keys everywhere (no sequential IDs leaking business volume)
- Every financial amount stored as integer minor units (paise), never float
- Every workflow-affecting write is auditable via audit_events
- Idempotency keys are unique-constrained at the DB level, not just app level
- Enums are DB-level constraints, not just Python-level convention
"""
import enum
import uuid

from sqlalchemy import (
    Column, String, Integer, Numeric, ForeignKey, DateTime, Enum, Boolean,
    Text, UniqueConstraint, Index, CheckConstraint, JSON, func
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.session import Base


def gen_uuid():
    return str(uuid.uuid4())


# ==================================================
# ENUMS
# ==================================================

class RiskEventType(str, enum.Enum):
    PAYMENT_FAILURE = "PAYMENT_FAILURE"
    CHECKOUT_ABANDONMENT = "CHECKOUT_ABANDONMENT"
    RECURRING_PAYMENT_FAILURE = "RECURRING_PAYMENT_FAILURE"
    MANDATE_FAILURE = "MANDATE_FAILURE"
    OVERDUE_INVOICE = "OVERDUE_INVOICE"


class FailureReason(str, enum.Enum):
    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    CARD_EXPIRED = "CARD_EXPIRED"
    BANK_TIMEOUT = "BANK_TIMEOUT"
    ISSUER_DECLINE = "ISSUER_DECLINE"
    NETWORK_ERROR = "NETWORK_ERROR"
    USER_ABANDONED = "USER_ABANDONED"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    UNKNOWN = "UNKNOWN"


class WorkflowState(str, enum.Enum):
    DETECTED = "DETECTED"
    ENRICHING = "ENRICHING"
    DIAGNOSING = "DIAGNOSING"
    SCORING = "SCORING"
    PLANNING = "PLANNING"
    POLICY_CHECK = "POLICY_CHECK"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    RECOVERED = "RECOVERED"
    ESCALATED = "ESCALATED"
    EXHAUSTED = "EXHAUSTED"
    ABANDONED = "ABANDONED"
    FAILED = "FAILED"


class RecommendedAction(str, enum.Enum):
    RETRY_PAYMENT = "RETRY_PAYMENT"
    DELAY_AND_RETRY = "DELAY_AND_RETRY"
    SEND_PAYMENT_LINK = "SEND_PAYMENT_LINK"
    SEND_REMINDER = "SEND_REMINDER"
    OFFER_INCENTIVE = "OFFER_INCENTIVE"
    REQUEST_CUSTOMER_ACTION = "REQUEST_CUSTOMER_ACTION"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    STOP = "STOP"


class PolicyDecision(str, enum.Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    ESCALATE = "ESCALATE"


class ActionType(str, enum.Enum):
    RETRY_PAYMENT = "RETRY_PAYMENT"
    SEND_PAYMENT_LINK = "SEND_PAYMENT_LINK"
    SEND_NOTIFICATION = "SEND_NOTIFICATION"
    APPLY_INCENTIVE = "APPLY_INCENTIVE"
    ESCALATE = "ESCALATE"


class ActionStatus(str, enum.Enum):
    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"  # external call timed out; must reconcile before retry
    RECONCILED = "RECONCILED"


class RiskLevel(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


# ==================================================
# CORE ENTITIES
# ==================================================

class Merchant(Base):
    __tablename__ = "merchants"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    name = Column(String(255), nullable=False)
    razorpay_account_id = Column(String(64), unique=True, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    policies = relationship("MerchantPolicy", back_populates="merchant", uselist=False)


class Customer(Base):
    __tablename__ = "customers"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    merchant_id = Column(UUID(as_uuid=False), ForeignKey("merchants.id"), nullable=False, index=True)
    external_customer_id = Column(String(128), nullable=False)
    # PII minimization: store only what's needed for recovery communication
    contact_email_hash = Column(String(128), nullable=True)
    contact_phone_hash = Column(String(128), nullable=True)
    historical_successful_payments = Column(Integer, nullable=False, default=0)
    historical_failed_payments = Column(Integer, nullable=False, default=0)
    is_opted_out = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("merchant_id", "external_customer_id", name="uq_customer_per_merchant"),
    )


class MerchantPolicy(Base):
    """Deterministic, merchant-specific policy overrides. AI cannot alter these."""
    __tablename__ = "merchant_policies"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    merchant_id = Column(UUID(as_uuid=False), ForeignKey("merchants.id"), unique=True, nullable=False)
    max_retry_attempts = Column(Integer, nullable=False, default=3)
    min_retry_cooldown_seconds = Column(Integer, nullable=False, default=1800)
    max_communication_attempts = Column(Integer, nullable=False, default=4)
    confidence_threshold = Column(Numeric(4, 3), nullable=False, default=0.65)
    max_incentive_percent = Column(Integer, nullable=False, default=10)
    allow_incentives = Column(Boolean, nullable=False, default=False)
    escalation_amount_threshold_minor = Column(Integer, nullable=False, default=5000000)  # 50,000.00 in paise

    merchant = relationship("Merchant", back_populates="policies")

    __table_args__ = (
        CheckConstraint("max_retry_attempts >= 0", name="ck_max_retry_nonneg"),
        CheckConstraint("max_incentive_percent >= 0 AND max_incentive_percent <= 100", name="ck_incentive_pct_range"),
    )


class RevenueRiskEvent(Base):
    """A unified revenue-at-risk event. One row per detected leak."""
    __tablename__ = "revenue_risk_events"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    merchant_id = Column(UUID(as_uuid=False), ForeignKey("merchants.id"), nullable=False, index=True)
    customer_id = Column(UUID(as_uuid=False), ForeignKey("customers.id"), nullable=False, index=True)

    event_type = Column(Enum(RiskEventType), nullable=False)
    failure_reason = Column(Enum(FailureReason), nullable=False)

    # Money stored in minor units (paise), always integer, never float
    amount_minor = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default="INR")

    reference_type = Column(String(32), nullable=False)  # "payment" | "subscription" | "invoice" | "checkout"
    reference_id = Column(String(128), nullable=False)

    previous_attempts = Column(Integer, nullable=False, default=0)
    source_event_id = Column(String(128), nullable=True)  # e.g. Razorpay webhook event id, for dedup

    detected_at = Column(DateTime(timezone=True), server_default=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("merchant_id", "source_event_id", name="uq_source_event_per_merchant"),
        CheckConstraint("amount_minor > 0", name="ck_amount_positive"),
        Index("ix_risk_event_ref", "reference_type", "reference_id"),
    )


class RecoveryWorkflow(Base):
    """The stateful workflow tracking one risk event through recovery."""
    __tablename__ = "recovery_workflows"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    risk_event_id = Column(UUID(as_uuid=False), ForeignKey("revenue_risk_events.id"), unique=True, nullable=False)
    merchant_id = Column(UUID(as_uuid=False), ForeignKey("merchants.id"), nullable=False, index=True)

    current_state = Column(Enum(WorkflowState), nullable=False, default=WorkflowState.DETECTED, index=True)
    retry_count = Column(Integer, nullable=False, default=0)
    communication_count = Column(Integer, nullable=False, default=0)

    recovered_amount_minor = Column(Integer, nullable=False, default=0)
    recovery_cost_minor = Column(Integer, nullable=False, default=0)

    is_terminal = Column(Boolean, nullable=False, default=False)
    stop_reason = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    transitions = relationship("WorkflowTransition", back_populates="workflow", order_by="WorkflowTransition.created_at")


class WorkflowTransition(Base):
    """Immutable log of every state transition. Never updated, only inserted."""
    __tablename__ = "workflow_transitions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    workflow_id = Column(UUID(as_uuid=False), ForeignKey("recovery_workflows.id"), nullable=False, index=True)
    from_state = Column(Enum(WorkflowState), nullable=True)
    to_state = Column(Enum(WorkflowState), nullable=False)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    workflow = relationship("RecoveryWorkflow", back_populates="transitions")


class AIDecision(Base):
    """Every structured AI output, validated, stored verbatim for audit."""
    __tablename__ = "ai_decisions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    workflow_id = Column(UUID(as_uuid=False), ForeignKey("recovery_workflows.id"), nullable=False, index=True)

    provider = Column(String(32), nullable=False)  # "mock" | "anthropic"
    model_version = Column(String(64), nullable=False)
    prompt_version = Column(String(32), nullable=False)

    diagnosis = Column(Text, nullable=False)
    evidence = Column(JSON, nullable=False, default=list)
    recoverability_score = Column(Numeric(4, 3), nullable=False)
    recommended_action = Column(Enum(RecommendedAction), nullable=False)
    confidence = Column(Numeric(4, 3), nullable=False)
    expected_recovery_minor = Column(Integer, nullable=False, default=0)
    risk_level = Column(Enum(RiskLevel), nullable=False)
    recommended_delay_seconds = Column(Integer, nullable=False, default=0)
    customer_message_intent = Column(Text, nullable=True)
    stop_reason = Column(Text, nullable=True)

    latency_ms = Column(Integer, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    estimated_cost_usd = Column(Numeric(10, 6), nullable=True)

    was_fallback = Column(Boolean, nullable=False, default=False)
    validation_passed = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("recoverability_score >= 0 AND recoverability_score <= 1", name="ck_recoverability_range"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_confidence_range"),
    )


class PolicyDecisionRecord(Base):
    """Deterministic policy engine verdict on an AI-recommended action."""
    __tablename__ = "policy_decisions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    workflow_id = Column(UUID(as_uuid=False), ForeignKey("recovery_workflows.id"), nullable=False, index=True)
    ai_decision_id = Column(UUID(as_uuid=False), ForeignKey("ai_decisions.id"), nullable=False)

    requested_action = Column(Enum(RecommendedAction), nullable=False)
    decision = Column(Enum(PolicyDecision), nullable=False)
    rule_triggered = Column(String(128), nullable=False)  # which rule caused ALLOW/BLOCK/ESCALATE
    explanation = Column(Text, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Action(Base):
    """A single execution attempt of an approved action. Idempotent by design."""
    __tablename__ = "actions"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    workflow_id = Column(UUID(as_uuid=False), ForeignKey("recovery_workflows.id"), nullable=False, index=True)
    policy_decision_id = Column(UUID(as_uuid=False), ForeignKey("policy_decisions.id"), nullable=False)

    action_type = Column(Enum(ActionType), nullable=False)
    idempotency_key = Column(String(128), nullable=False, unique=True)
    status = Column(Enum(ActionStatus), nullable=False, default=ActionStatus.PENDING)

    amount_minor = Column(Integer, nullable=True)  # for retry/incentive actions
    attempt_number = Column(Integer, nullable=False, default=1)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ActionAttempt(Base):
    """Every low-level call attempt for an Action (for retry/backoff/reconciliation history)."""
    __tablename__ = "action_attempts"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    action_id = Column(UUID(as_uuid=False), ForeignKey("actions.id"), nullable=False, index=True)
    attempt_id = Column(String(64), nullable=False, unique=True)

    outcome = Column(String(32), nullable=False)  # SUCCESS | FAILURE | TIMEOUT | RATE_LIMITED
    external_reference = Column(String(128), nullable=True)
    error_detail = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    workflow_id = Column(UUID(as_uuid=False), ForeignKey("recovery_workflows.id"), nullable=False, index=True)
    action_id = Column(UUID(as_uuid=False), ForeignKey("actions.id"), nullable=False)

    channel = Column(String(16), nullable=False)  # "email" | "sms" | "whatsapp"
    template_id = Column(String(64), nullable=False)
    language = Column(String(8), nullable=False, default="en")
    rendered_body_hash = Column(String(128), nullable=False)  # store hash, not raw PII-laden content
    status = Column(String(16), nullable=False, default="SENT")

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    """Append-only audit trail. Every meaningful action across the system lands here."""
    __tablename__ = "audit_events"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    workflow_id = Column(UUID(as_uuid=False), ForeignKey("recovery_workflows.id"), nullable=True, index=True)

    actor = Column(String(32), nullable=False)  # "system" | "ai" | "policy_engine" | "operator:<id>"
    event_type = Column(String(64), nullable=False)
    description = Column(Text, nullable=False)
    metadata_json = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class EvaluationRun(Base):
    """One full batch evaluation run: RecoverOS vs static baseline over synthetic data."""
    __tablename__ = "evaluation_runs"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    label = Column(String(128), nullable=False)
    dataset_seed = Column(Integer, nullable=False)
    total_events = Column(Integer, nullable=False)

    baseline_recovered_minor = Column(Integer, nullable=False, default=0)
    recoveros_recovered_minor = Column(Integer, nullable=False, default=0)
    baseline_recovery_rate = Column(Numeric(5, 4), nullable=True)
    recoveros_recovery_rate = Column(Numeric(5, 4), nullable=True)
    baseline_unnecessary_actions = Column(Integer, nullable=False, default=0)
    recoveros_unnecessary_actions = Column(Integer, nullable=False, default=0)

    started_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)


class EvaluationEvent(Base):
    """Per-event ground truth + outcome for both baseline and RecoverOS, for auditability."""
    __tablename__ = "evaluation_events"

    id = Column(UUID(as_uuid=False), primary_key=True, default=gen_uuid)
    evaluation_run_id = Column(UUID(as_uuid=False), ForeignKey("evaluation_runs.id"), nullable=False, index=True)

    synthetic_event_ref = Column(String(64), nullable=False)
    ground_truth_recoverable = Column(Boolean, nullable=False)
    amount_minor = Column(Integer, nullable=False)

    baseline_action = Column(String(32), nullable=True)
    baseline_recovered = Column(Boolean, nullable=False, default=False)

    recoveros_action = Column(String(32), nullable=True)
    recoveros_recovered = Column(Boolean, nullable=False, default=False)
    recoveros_confidence = Column(Numeric(4, 3), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
