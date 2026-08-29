"""
RecoverOS evaluation runner.

This calls the REAL diagnosis heuristics (app/ai/mock_provider.py) and the
REAL policy engine (app/policy/engine.py) -- the exact same code that runs
in the live API's /diagnose and /plan endpoints -- rather than a separate,
simplified re-implementation for evaluation purposes. If those modules
change, this evaluation automatically reflects the change; there is no
second copy of the decision logic to keep in sync.

Outcome classification (mirrors baseline.py's shape for a fair comparison):
  - AI recommends STOP -> policy engine BLOCKs (ai_recommended_stop) ->
    NO_ACTION. If ground truth was actually recoverable, this is a missed
    recovery (false negative); otherwise it's a correct refusal to act.
  - Policy ALLOWs a gated action -> action is "taken." Recovered if ground
    truth says recoverable (true positive), else an unnecessary
    intervention (false positive) -- exactly the cost the product spec's
    "knowing when not to act" feature exists to avoid.
  - Policy ESCALATEs -> routed to a human, counted separately. Not treated
    as an automated recovery either way, since a human -- not RecoverOS's
    automation -- makes the actual call. This is a capability the static
    baseline structurally cannot have (it has no policy engine, so its
    escalation rate is always exactly 0, which is itself a fair point of
    comparison, not a gap in the baseline's numbers).
"""
from dataclasses import dataclass

from app.ai.provider import AIContext
from app.ai.mock_provider import MockAIProvider
from app.policy import engine as policy_engine
from app.db.models import MerchantPolicy, RecommendedAction, PolicyDecision as PolicyDecisionEnum
from app.evaluation.generator import SyntheticEvent
from app.core.config import get_settings


@dataclass
class _WorkflowStub:
    """Duck-typed stand-in for RecoveryWorkflow. The policy engine only
    reads .retry_count and .communication_count -- see app/policy/engine.py
    Rules 4/5 -- so a full ORM-persisted workflow is unnecessary for a
    1,000+ event batch and would only add DB round-trip cost with no
    additional correctness. previous_attempts is used as a reasonable
    proxy for "recovery attempts already made" for both counters, since
    the synthetic dataset doesn't separately model attempt type."""
    retry_count: int
    communication_count: int


def _default_policy() -> MerchantPolicy:
    """An unsaved MerchantPolicy instance using the same defaults the demo
    merchant is seeded with (see app/db/seed.py) -- so evaluation results
    are reproducible without depending on database seed state."""
    settings = get_settings()
    return MerchantPolicy(
        max_retry_attempts=settings.max_retry_attempts,
        min_retry_cooldown_seconds=settings.min_retry_cooldown_seconds,
        max_communication_attempts=settings.max_communication_attempts,
        confidence_threshold=settings.confidence_threshold,
        max_incentive_percent=settings.max_incentive_percent,
        allow_incentives=True,
        escalation_amount_threshold_minor=5_000_000,
    )


@dataclass
class RecoverOSOutcome:
    action: str  # AI's recommended_action value
    policy_decision: str  # ALLOW | BLOCK | ESCALATE
    rule_triggered: str
    confidence: float
    recoverability_score: float
    recovered: bool
    was_unnecessary: bool
    was_escalated: bool
    was_missed_recovery: bool  # ground truth recoverable, but we didn't act (STOP/BLOCK)


def run_recoveros(
    event: SyntheticEvent,
    *,
    provider: MockAIProvider | None = None,
    policy: MerchantPolicy | None = None,
) -> RecoverOSOutcome:
    provider = provider or MockAIProvider()
    policy = policy or _default_policy()

    context = AIContext(
        risk_event_type=event.event_type,
        failure_reason=event.failure_reason,
        amount_minor=event.amount_minor,
        currency="INR",
        previous_attempts=event.previous_attempts,
        customer_historical_successful_payments=event.customer_historical_successful_payments,
        customer_historical_failed_payments=event.customer_historical_failed_payments,
        customer_is_opted_out=event.customer_is_opted_out,
        merchant_max_retry_attempts=policy.max_retry_attempts,
        merchant_allows_incentives=policy.allow_incentives,
    )

    result = provider.diagnose(context)
    diagnosis = result.output

    verdict = policy_engine.evaluate(
        workflow=_WorkflowStub(retry_count=event.previous_attempts, communication_count=event.previous_attempts),
        policy=policy,
        recommended_action=diagnosis.recommended_action,
        confidence=diagnosis.confidence,
        amount_minor=event.amount_minor,
        customer_is_opted_out=event.customer_is_opted_out,
    )

    action_taken = verdict.decision == PolicyDecisionEnum.ALLOW
    was_escalated = verdict.decision == PolicyDecisionEnum.ESCALATE
    no_action = verdict.decision == PolicyDecisionEnum.BLOCK  # includes AI STOP and policy blocks alike

    recovered = action_taken and event.ground_truth_recoverable
    was_unnecessary = action_taken and not event.ground_truth_recoverable
    was_missed_recovery = no_action and event.ground_truth_recoverable

    return RecoverOSOutcome(
        action=diagnosis.recommended_action.value,
        policy_decision=verdict.decision.value,
        rule_triggered=verdict.rule_triggered,
        confidence=diagnosis.confidence,
        recoverability_score=diagnosis.recoverability_score,
        recovered=recovered,
        was_unnecessary=was_unnecessary,
        was_escalated=was_escalated,
        was_missed_recovery=was_missed_recovery,
    )
