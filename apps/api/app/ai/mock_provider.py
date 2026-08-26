"""
MockAIProvider: deterministic, zero-cost, zero-latency-variance AI provider.

This is not a dumb stub. It encodes the same reasoning heuristics described
in the product spec (customer history, transient vs permanent failure,
retry exhaustion) as explicit, readable rules — so:

  1. The 1,000-event batch evaluation (Phase 5) is fully reproducible: same
     input always produces the same diagnosis, which is required to compare
     RecoverOS against a static baseline fairly.
  2. Every test in this codebase can run without an API key or network
     access.
  3. Failure-injection (timeout, malformed output) is deterministic and
     triggerable on demand for the Failure Lab (Phase 4), rather than
     relying on randomly hoping a real provider fails during a demo.
"""
import hashlib
import time

from app.ai.provider import AIProvider, AIContext, AIDecisionResult, AIProviderError
from app.ai.schemas import AIDecisionOutput
from app.db.models import RecommendedAction, RiskLevel

PROMPT_VERSION = "mock-v1"
MODEL_VERSION = "mock-heuristic-v1"

TRANSIENT_FAILURE_REASONS = {"BANK_TIMEOUT", "NETWORK_ERROR", "ISSUER_DECLINE"}
PERMANENT_FAILURE_REASONS = {"CARD_EXPIRED", "MANDATE_REVOKED"}


class MockAIProvider(AIProvider):
    name = "mock"

    def __init__(self, *, force_timeout: bool = False, force_malformed: bool = False):
        # Injection hooks used only by the Failure Lab / failure tests —
        # never set from normal application code paths.
        self._force_timeout = force_timeout
        self._force_malformed = force_malformed

    def diagnose(self, context: AIContext, *, timeout_seconds: float = 8.0) -> AIDecisionResult:
        start = time.monotonic()

        if self._force_timeout:
            raise AIProviderError("Simulated AI provider timeout (Failure Lab injection).")

        if self._force_malformed:
            # Deliberately return something that will fail AIDecisionOutput
            # validation, to exercise the schema-validation-rejects-bad-output
            # path with a real (not imagined) failure.
            latency_ms = int((time.monotonic() - start) * 1000)
            return AIDecisionResult(
                output=None,  # type: ignore[arg-type]
                provider=self.name,
                model_version=MODEL_VERSION,
                prompt_version=PROMPT_VERSION,
                latency_ms=latency_ms,
                validation_passed=False,
                raw_error="Simulated malformed output (Failure Lab injection): "
                          "missing required fields.",
            )

        output = self._heuristic_diagnosis(context)
        latency_ms = int((time.monotonic() - start) * 1000)

        return AIDecisionResult(
            output=output,
            provider=self.name,
            model_version=MODEL_VERSION,
            prompt_version=PROMPT_VERSION,
            latency_ms=latency_ms,
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
            was_fallback=False,
            validation_passed=True,
        )

    def _heuristic_diagnosis(self, ctx: AIContext) -> AIDecisionOutput:
        total_history = ctx.customer_historical_successful_payments + ctx.customer_historical_failed_payments
        success_rate = (
            ctx.customer_historical_successful_payments / total_history
            if total_history > 0 else 0.5  # no history: neutral prior
        )

        evidence = [
            f"{ctx.customer_historical_successful_payments} successful vs "
            f"{ctx.customer_historical_failed_payments} failed historical payments "
            f"(success rate {success_rate:.2f})",
            f"Failure reason classified as "
            f"{'transient' if ctx.failure_reason in TRANSIENT_FAILURE_REASONS else 'permanent/behavioral'}",
            f"{ctx.previous_attempts} previous recovery attempts on this event",
        ]

        # --- Rule: permanent failure reasons are rarely worth retrying the
        # same instrument; recommend a payment link (new instrument) instead,
        # or stop if the customer also has poor history.
        if ctx.failure_reason in PERMANENT_FAILURE_REASONS:
            if success_rate < 0.3 or ctx.customer_is_opted_out:
                return AIDecisionOutput(
                    diagnosis="Permanent failure reason with weak recovery signal; "
                              "further automated attempts unlikely to succeed.",
                    evidence=evidence,
                    recoverability_score=round(0.15 + success_rate * 0.1, 3),
                    recommended_action=RecommendedAction.STOP,
                    confidence=0.8,
                    expected_recovery_minor=0,
                    risk_level=RiskLevel.LOW,
                    recommended_delay_seconds=0,
                    stop_reason="Permanent failure reason (e.g. expired card/revoked mandate) "
                                "combined with low historical success rate; retrying the same "
                                "instrument is not expected to recover revenue.",
                )
            return AIDecisionOutput(
                diagnosis="Permanent failure reason but customer has reasonable payment history; "
                          "recommend a fresh payment link rather than retrying the failed instrument.",
                evidence=evidence,
                recoverability_score=round(0.4 + success_rate * 0.3, 3),
                recommended_action=RecommendedAction.SEND_PAYMENT_LINK,
                confidence=round(0.55 + success_rate * 0.2, 3),
                expected_recovery_minor=ctx.amount_minor,
                risk_level=RiskLevel.MEDIUM,
                recommended_delay_seconds=0,
                customer_message_intent="Inform the customer their payment method needs updating "
                                         "and provide a secure link to complete payment.",
            )

        # --- Rule: retry limit essentially exhausted -> stop regardless of
        # how the model might otherwise feel about it. "Knowing when not to
        # act" must not be overridden just because more attempts remain
        # technically possible.
        if ctx.previous_attempts >= ctx.merchant_max_retry_attempts:
            return AIDecisionOutput(
                diagnosis="Retry attempts have reached the merchant's configured limit with no "
                          "successful recovery; continuing would only add unnecessary cost.",
                evidence=evidence + [
                    f"previous_attempts ({ctx.previous_attempts}) >= "
                    f"merchant_max_retry_attempts ({ctx.merchant_max_retry_attempts})"
                ],
                recoverability_score=round(max(0.05, 0.3 - ctx.previous_attempts * 0.05), 3),
                recommended_action=RecommendedAction.STOP,
                confidence=0.85,
                expected_recovery_minor=0,
                risk_level=RiskLevel.LOW,
                recommended_delay_seconds=0,
                stop_reason=f"Reached merchant retry limit ({ctx.merchant_max_retry_attempts}) "
                            f"without recovery; further attempts have low expected value.",
            )

        # --- Rule: transient failure + strong customer history -> high
        # confidence retry after a short cooldown. This is the "textbook
        # good case" from the product spec.
        if ctx.failure_reason in TRANSIENT_FAILURE_REASONS and success_rate >= 0.6:
            confidence = round(min(0.95, 0.55 + success_rate * 0.4 - ctx.previous_attempts * 0.05), 3)
            return AIDecisionOutput(
                diagnosis="Transient failure reason with strong historical payment reliability; "
                          "high probability the retry succeeds without further intervention.",
                evidence=evidence,
                recoverability_score=round(min(0.95, 0.5 + success_rate * 0.45), 3),
                recommended_action=RecommendedAction.RETRY_PAYMENT,
                confidence=confidence,
                expected_recovery_minor=ctx.amount_minor,
                risk_level=RiskLevel.LOW,
                recommended_delay_seconds=1800,
            )

        # --- Rule: transient failure but weak/ambiguous history -> lower
        # confidence, delayed retry, so the policy engine's confidence
        # threshold has genuine ambiguous cases to escalate.
        if ctx.failure_reason in TRANSIENT_FAILURE_REASONS:
            confidence = round(0.35 + success_rate * 0.35, 3)
            return AIDecisionOutput(
                diagnosis="Transient failure reason but customer payment history is mixed or "
                          "limited; recommend a delayed retry with moderate confidence.",
                evidence=evidence,
                recoverability_score=round(0.3 + success_rate * 0.4, 3),
                recommended_action=RecommendedAction.DELAY_AND_RETRY,
                confidence=confidence,
                expected_recovery_minor=ctx.amount_minor,
                risk_level=RiskLevel.MEDIUM,
                recommended_delay_seconds=3600,
            )

        # --- Fallback rule: checkout abandonment / unknown reason with
        # decent history -> a reminder is the least invasive next step.
        return AIDecisionOutput(
            diagnosis="No strong signal for automated retry; a reminder is the lowest-risk "
                      "next step to prompt the customer to complete the payment themselves.",
            evidence=evidence,
            recoverability_score=round(0.25 + success_rate * 0.3, 3),
            recommended_action=RecommendedAction.SEND_REMINDER,
            confidence=round(0.4 + success_rate * 0.2, 3),
            expected_recovery_minor=ctx.amount_minor,
            risk_level=RiskLevel.MEDIUM,
            recommended_delay_seconds=7200,
            customer_message_intent="Friendly reminder that checkout was not completed, "
                                     "with a direct link to finish payment.",
        )


def deterministic_seed_for_context(ctx: AIContext) -> int:
    """Utility for the batch generator (Phase 5): a stable hash of context
    fields, so synthetic data generation is reproducible across runs given
    the same base seed, independent of dict ordering or object identity."""
    key = f"{ctx.risk_event_type}|{ctx.failure_reason}|{ctx.amount_minor}|{ctx.previous_attempts}"
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % (10 ** 8)
