"""
Deterministic policy engine.

This module NEVER calls an AI provider and has no knowledge of prompts,
models, or confidence-generation. It receives a structured AI recommendation
(already validated against the AI output schema in Phase 3) and applies
hard, explainable, merchant-configurable rules to decide:

    ALLOW     - the action executor may proceed
    BLOCK     - the action is rejected; workflow moves toward EXHAUSTED
    ESCALATE  - a human must review; workflow moves to ESCALATED

The AI cannot bypass this. Every verdict records exactly which rule fired,
so "why did the system do X" is always answerable by reading one row in
policy_decisions, not by re-running the AI call.
"""
from dataclasses import dataclass

from app.db.models import (
    RecommendedAction, PolicyDecision, MerchantPolicy, RecoveryWorkflow,
)


@dataclass
class PolicyVerdict:
    decision: PolicyDecision
    rule_triggered: str
    explanation: str


# Actions that move real money or make a customer-facing promise.
# Every one of these MUST pass through policy checks. There is no code
# path that lets an action of these types execute without a verdict.
GATED_ACTIONS = {
    RecommendedAction.RETRY_PAYMENT,
    RecommendedAction.DELAY_AND_RETRY,
    RecommendedAction.SEND_PAYMENT_LINK,
    RecommendedAction.SEND_REMINDER,
    RecommendedAction.OFFER_INCENTIVE,
    RecommendedAction.REQUEST_CUSTOMER_ACTION,
}


def evaluate(
    *,
    workflow: RecoveryWorkflow,
    policy: MerchantPolicy,
    recommended_action: RecommendedAction,
    confidence: float,
    amount_minor: int,
    incentive_percent: int | None = None,
    customer_is_opted_out: bool = False,
) -> PolicyVerdict:
    """
    Apply deterministic rules in a fixed priority order. The FIRST rule
    that fires wins — this makes behavior predictable and testable: given
    the same inputs, you always get the same rule name in the explanation,
    not "whichever check happened to run last."
    """

    # Rule 0: AI explicitly recommended STOP. Always honored, always allowed
    # through as the terminal signal — this is what lets the system say
    # "we choose not to act," which is a first-class outcome, not a failure.
    if recommended_action == RecommendedAction.STOP:
        return PolicyVerdict(
            decision=PolicyDecision.BLOCK,
            rule_triggered="ai_recommended_stop",
            explanation="AI diagnosis concluded recovery is not worthwhile; "
                        "honoring STOP recommendation, workflow will be exhausted.",
        )

    # Rule 1: customer opted out of communication. Hard stop, no exceptions,
    # regardless of AI confidence or expected recovery value.
    if customer_is_opted_out and recommended_action in {
        RecommendedAction.SEND_REMINDER,
        RecommendedAction.SEND_PAYMENT_LINK,
        RecommendedAction.OFFER_INCENTIVE,
        RecommendedAction.REQUEST_CUSTOMER_ACTION,
    }:
        return PolicyVerdict(
            decision=PolicyDecision.BLOCK,
            rule_triggered="customer_opted_out",
            explanation="Customer has opted out of recovery communication. "
                        "Communication-based actions are blocked unconditionally.",
        )

    # Rule 2: high-value ambiguous case -> escalate to human before any
    # automated action, regardless of what the AI recommended.
    if amount_minor >= policy.escalation_amount_threshold_minor:
        return PolicyVerdict(
            decision=PolicyDecision.ESCALATE,
            rule_triggered="amount_exceeds_escalation_threshold",
            explanation=f"Amount {amount_minor} minor units meets/exceeds merchant "
                        f"escalation threshold {policy.escalation_amount_threshold_minor}. "
                        f"Routing to human review regardless of AI confidence.",
        )

    # Rule 3: confidence below merchant-configured threshold -> escalate.
    # We escalate rather than block here because low confidence doesn't
    # mean the action is wrong, it means the AI isn't sure enough to let
    # it proceed unsupervised.
    if confidence < float(policy.confidence_threshold):
        return PolicyVerdict(
            decision=PolicyDecision.ESCALATE,
            rule_triggered="confidence_below_threshold",
            explanation=f"AI confidence {confidence:.3f} is below merchant threshold "
                        f"{float(policy.confidence_threshold):.3f}. Escalating to human review.",
        )

    # Rule 4: retry-specific limits.
    if recommended_action in {RecommendedAction.RETRY_PAYMENT, RecommendedAction.DELAY_AND_RETRY}:
        if workflow.retry_count >= policy.max_retry_attempts:
            return PolicyVerdict(
                decision=PolicyDecision.BLOCK,
                rule_triggered="max_retry_attempts_exceeded",
                explanation=f"Workflow has {workflow.retry_count} retries; merchant policy "
                            f"caps retries at {policy.max_retry_attempts}. Blocking further retries.",
            )

    # Rule 5: communication-specific limits.
    if recommended_action in {
        RecommendedAction.SEND_REMINDER,
        RecommendedAction.SEND_PAYMENT_LINK,
        RecommendedAction.REQUEST_CUSTOMER_ACTION,
    }:
        if workflow.communication_count >= policy.max_communication_attempts:
            return PolicyVerdict(
                decision=PolicyDecision.BLOCK,
                rule_triggered="max_communication_attempts_exceeded",
                explanation=f"Workflow has {workflow.communication_count} communications; "
                            f"merchant policy caps at {policy.max_communication_attempts}. "
                            f"Blocking further outreach.",
            )

    # Rule 6: incentive-specific limits.
    if recommended_action == RecommendedAction.OFFER_INCENTIVE:
        if not policy.allow_incentives:
            return PolicyVerdict(
                decision=PolicyDecision.BLOCK,
                rule_triggered="incentives_disabled_for_merchant",
                explanation="Merchant policy does not permit incentive-based recovery.",
            )
        if incentive_percent is None or incentive_percent > policy.max_incentive_percent:
            return PolicyVerdict(
                decision=PolicyDecision.BLOCK,
                rule_triggered="incentive_exceeds_max_percent",
                explanation=f"Requested incentive {incentive_percent}% exceeds merchant cap "
                            f"of {policy.max_incentive_percent}%.",
            )

    # Rule 7: explicit escalation request from AI is always honored.
    if recommended_action == RecommendedAction.ESCALATE_TO_HUMAN:
        return PolicyVerdict(
            decision=PolicyDecision.ESCALATE,
            rule_triggered="ai_requested_escalation",
            explanation="AI diagnosis explicitly recommended human review.",
        )

    # No rule blocked or escalated -> allow.
    return PolicyVerdict(
        decision=PolicyDecision.ALLOW,
        rule_triggered="all_checks_passed",
        explanation=f"Action {recommended_action.value} passed all deterministic policy checks "
                    f"(confidence {confidence:.3f} >= threshold "
                    f"{float(policy.confidence_threshold):.3f}, within retry/communication/"
                    f"amount limits).",
    )
