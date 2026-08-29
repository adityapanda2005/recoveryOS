"""
Central audit logging.

Every meaningful system action — a workflow being created, a state
transition, an AI decision (including fallbacks), a policy verdict, an
action execution attempt, a reconciliation — writes one row here, in
addition to whatever domain-specific table already records it
(workflow_transitions, ai_decisions, policy_decisions, action_attempts).

This is deliberately a thin, single-purpose function rather than scattered
db.add(AuditEvent(...)) calls throughout the codebase, so "does every
domain event actually produce an audit trail" is answerable by grepping
for one function name, not by auditing every module by hand.
"""
from sqlalchemy.orm import Session

from app.db.models import AuditEvent


def log_audit_event(
    db: Session,
    *,
    actor: str,
    event_type: str,
    description: str,
    workflow_id: str | None = None,
    metadata: dict | None = None,
) -> AuditEvent:
    """
    Writes an audit event. Does NOT commit — callers control their own
    transaction boundary (usually committing the audit row together with
    the domain change it describes, so the two can never diverge).

    actor: "system" | "ai" | "policy_engine" | "operator:<id>"
    event_type: short machine-readable label, e.g. "workflow.created",
        "action.executed", "action.duplicate_blocked", "ai.fallback"
    """
    event = AuditEvent(
        workflow_id=workflow_id,
        actor=actor,
        event_type=event_type,
        description=description,
        metadata_json=metadata,
    )
    db.add(event)
    return event
