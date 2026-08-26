"""
Diagnosis service.

This is the only entry point the rest of the system uses to get an AI
decision. It owns:

  - provider selection (mock vs anthropic, from settings)
  - calling the provider and catching AIProviderError
  - one safe retry on transient failure
  - falling back to a conservative default (ESCALATE_TO_HUMAN) if the
    provider fails twice or returns output that fails schema validation
  - persisting every attempt (successful or not) to ai_decisions, so the
    audit trail includes fallbacks, not just successes

Nothing downstream (policy engine, workflow) ever calls a provider directly.
"""
import logging

from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, AIContext, AIDecisionResult, AIProviderError
from app.ai.schemas import AIDecisionOutput
from app.ai.mock_provider import MockAIProvider
from app.core.config import get_settings
from app.db.models import AIDecision, RecommendedAction, RiskLevel

logger = logging.getLogger("recoveros.ai.diagnosis")

FALLBACK_PROMPT_VERSION = "fallback-v1"


def get_provider() -> AIProvider:
    """Provider selection is centralized here so the rest of the app never
    hardcodes which provider is active — swapping AI_PROVIDER in .env is
    the only thing required to change behavior everywhere."""
    settings = get_settings()
    if settings.ai_provider == "anthropic":
        from app.ai.anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    return MockAIProvider()


def _fallback_output(reason: str) -> AIDecisionOutput:
    """The safe default when the AI layer cannot produce a trustworthy
    decision at all. Deliberately conservative: escalate to a human rather
    than guess an action, and recoverability/confidence reflect genuine
    uncertainty (not 0, which would falsely claim certainty the case is
    unrecoverable; not high, which would falsely claim confidence)."""
    return AIDecisionOutput(
        diagnosis=f"AI diagnosis unavailable: {reason}. Falling back to human review "
                  f"rather than proceeding without a validated recommendation.",
        evidence=[],
        recoverability_score=0.5,
        recommended_action=RecommendedAction.ESCALATE_TO_HUMAN,
        confidence=0.0,
        expected_recovery_minor=0,
        risk_level=RiskLevel.MEDIUM,
        recommended_delay_seconds=0,
        stop_reason=None,
    )


def diagnose_and_persist(
    db: Session,
    *,
    workflow_id: str,
    context: AIContext,
    provider: AIProvider | None = None,
) -> AIDecision:
    """
    Run diagnosis, applying retry-then-fallback, and persist the result
    (success or fallback) as a row in ai_decisions. Returns the persisted
    ORM row so callers can read the recommendation immediately.
    """
    settings = get_settings()
    provider = provider or get_provider()

    result: AIDecisionResult | None = None
    last_error: str | None = None

    # One safe retry on provider failure. Not more — the spec's failure
    # matrix requires bounded retries everywhere, including here; endless
    # AI retries would just delay the fallback without improving reliability.
    for attempt in range(2):
        try:
            candidate = provider.diagnose(context)
        except AIProviderError as e:
            last_error = str(e)
            logger.warning("AI provider attempt %d failed: %s", attempt + 1, last_error)
            continue

        if not candidate.validation_passed or candidate.output is None:
            last_error = candidate.raw_error or "AI output failed schema validation."
            logger.warning("AI provider attempt %d produced invalid output: %s", attempt + 1, last_error)
            continue

        # Confidence-threshold-driven fallback belongs to the policy engine
        # (Phase 2), not here — this service's only job is "did we get a
        # trustworthy structured decision at all," not "is it good enough
        # to act on." Keeping that separation means confidence policy stays
        # in exactly one place.
        result = candidate
        break

    if result is None:
        fallback_output = _fallback_output(last_error or "unknown provider failure")
        decision = AIDecision(
            workflow_id=workflow_id,
            provider=provider.name,
            model_version="fallback",
            prompt_version=FALLBACK_PROMPT_VERSION,
            diagnosis=fallback_output.diagnosis,
            evidence=fallback_output.evidence,
            recoverability_score=fallback_output.recoverability_score,
            recommended_action=fallback_output.recommended_action,
            confidence=fallback_output.confidence,
            expected_recovery_minor=fallback_output.expected_recovery_minor,
            risk_level=fallback_output.risk_level,
            recommended_delay_seconds=fallback_output.recommended_delay_seconds,
            customer_message_intent=fallback_output.customer_message_intent,
            stop_reason=fallback_output.stop_reason,
            latency_ms=None,
            was_fallback=True,
            validation_passed=False,
        )
    else:
        out = result.output
        decision = AIDecision(
            workflow_id=workflow_id,
            provider=result.provider,
            model_version=result.model_version,
            prompt_version=result.prompt_version,
            diagnosis=out.diagnosis,
            evidence=out.evidence,
            recoverability_score=out.recoverability_score,
            recommended_action=out.recommended_action,
            confidence=out.confidence,
            expected_recovery_minor=out.expected_recovery_minor,
            risk_level=out.risk_level,
            recommended_delay_seconds=out.recommended_delay_seconds,
            customer_message_intent=out.customer_message_intent,
            stop_reason=out.stop_reason,
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            estimated_cost_usd=result.estimated_cost_usd,
            was_fallback=False,
            validation_passed=True,
        )

    db.add(decision)
    db.commit()
    db.refresh(decision)
    return decision
