"""
AnthropicProvider: real Claude API calls for AI diagnosis.

Uses tool-use (forced tool_choice) rather than free-text JSON parsing, so
the model is structurally constrained to return the AIDecisionOutput shape
rather than us hoping it follows instructions and regex-parsing a JSON
blob out of prose. Even so, the response is still re-validated through
AIDecisionOutput before use — never trust a provider's output just because
it came back in the right shape at the transport level.
"""
import time

from app.ai.provider import AIProvider, AIContext, AIDecisionResult, AIProviderError
from app.ai.schemas import AIDecisionOutput
from app.core.config import get_settings

PROMPT_VERSION = "anthropic-v1"

DIAGNOSIS_TOOL = {
    "name": "record_diagnosis",
    "description": "Record a structured revenue-recovery diagnosis for this risk event.",
    "input_schema": {
        "type": "object",
        "properties": {
            "diagnosis": {"type": "string", "description": "Concise reasoning, 1-3 sentences."},
            "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            "recoverability_score": {"type": "number", "minimum": 0, "maximum": 1},
            "recommended_action": {
                "type": "string",
                "enum": [
                    "RETRY_PAYMENT", "DELAY_AND_RETRY", "SEND_PAYMENT_LINK",
                    "SEND_REMINDER", "OFFER_INCENTIVE", "REQUEST_CUSTOMER_ACTION",
                    "ESCALATE_TO_HUMAN", "STOP",
                ],
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "expected_recovery_minor": {"type": "integer", "minimum": 0},
            "risk_level": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
            "recommended_delay_seconds": {"type": "integer", "minimum": 0},
            "customer_message_intent": {"type": ["string", "null"]},
            "stop_reason": {"type": ["string", "null"]},
        },
        "required": [
            "diagnosis", "evidence", "recoverability_score", "recommended_action",
            "confidence", "expected_recovery_minor", "risk_level", "recommended_delay_seconds",
        ],
    },
}

SYSTEM_PROMPT = (
    "You are the diagnosis component inside RecoverOS, a revenue-recovery "
    "system. You NEVER execute financial actions yourself — you only "
    "produce a structured recommendation that a separate deterministic "
    "policy engine will validate against merchant rules before anything "
    "happens. Be conservative: if the evidence doesn't support confident "
    "recovery, recommend STOP or ESCALATE_TO_HUMAN rather than guessing. "
    "Call the record_diagnosis tool exactly once with your assessment."
)


def _build_user_message(ctx: AIContext) -> str:
    total = ctx.customer_historical_successful_payments + ctx.customer_historical_failed_payments
    success_rate = ctx.customer_historical_successful_payments / total if total else None
    return (
        f"Revenue risk event:\n"
        f"- type: {ctx.risk_event_type}\n"
        f"- failure_reason: {ctx.failure_reason}\n"
        f"- amount_minor: {ctx.amount_minor} {ctx.currency}\n"
        f"- previous_attempts: {ctx.previous_attempts}\n"
        f"- customer historical successful payments: {ctx.customer_historical_successful_payments}\n"
        f"- customer historical failed payments: {ctx.customer_historical_failed_payments}\n"
        f"- customer historical success rate: "
        f"{f'{success_rate:.2f}' if success_rate is not None else 'no history'}\n"
        f"- customer opted out of communication: {ctx.customer_is_opted_out}\n"
        f"- merchant max retry attempts: {ctx.merchant_max_retry_attempts}\n"
        f"- merchant allows incentives: {ctx.merchant_allows_incentives}\n"
    )


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self):
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise AIProviderError(
                "AnthropicProvider requires ANTHROPIC_API_KEY to be set in .env. "
                "Use MockAIProvider (AI_PROVIDER=mock) if no key is available."
            )
        # Imported lazily so the anthropic package + API key are only
        # required when this provider is actually selected.
        import anthropic
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model

    def diagnose(self, context: AIContext, *, timeout_seconds: float = 8.0) -> AIDecisionResult:
        start = time.monotonic()
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=[DIAGNOSIS_TOOL],
                tool_choice={"type": "tool", "name": "record_diagnosis"},
                messages=[{"role": "user", "content": _build_user_message(context)}],
                timeout=timeout_seconds,
            )
        except Exception as e:
            # Covers network errors, API timeouts, rate limits, 5xx — all
            # collapse to AIProviderError so the caller has one failure path
            # to handle (trigger fallback), regardless of root cause.
            raise AIProviderError(f"Anthropic API call failed: {e}") from e

        latency_ms = int((time.monotonic() - start) * 1000)

        tool_use_block = next(
            (b for b in response.content if getattr(b, "type", None) == "tool_use"), None
        )
        if tool_use_block is None:
            return AIDecisionResult(
                output=None,  # type: ignore[arg-type]
                provider=self.name,
                model_version=self._model,
                prompt_version=PROMPT_VERSION,
                latency_ms=latency_ms,
                input_tokens=getattr(response.usage, "input_tokens", None),
                output_tokens=getattr(response.usage, "output_tokens", None),
                validation_passed=False,
                raw_error="Anthropic response did not include a tool_use block.",
            )

        try:
            output = AIDecisionOutput(**tool_use_block.input)
            validation_passed = True
            raw_error = None
        except Exception as e:
            # Malformed-output path: the model returned something, but it
            # doesn't satisfy our contract. We do NOT trust it partially —
            # the whole decision is treated as invalid and the caller falls
            # back, per the spec's "never trust arbitrary model output" rule.
            output = None
            validation_passed = False
            raw_error = f"Schema validation failed: {e}"

        input_cost = (getattr(response.usage, "input_tokens", 0) or 0) * 3.0 / 1_000_000
        output_cost = (getattr(response.usage, "output_tokens", 0) or 0) * 15.0 / 1_000_000

        return AIDecisionResult(
            output=output,
            provider=self.name,
            model_version=self._model,
            prompt_version=PROMPT_VERSION,
            latency_ms=latency_ms,
            input_tokens=getattr(response.usage, "input_tokens", None),
            output_tokens=getattr(response.usage, "output_tokens", None),
            estimated_cost_usd=round(input_cost + output_cost, 6),
            was_fallback=False,
            validation_passed=validation_passed,
            raw_error=raw_error,
        )
