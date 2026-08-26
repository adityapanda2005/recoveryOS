"""
AI provider abstraction.

Every provider (MockAIProvider, AnthropicProvider) implements the same
interface: given a structured context about a revenue-risk event, return a
validated AIDecisionOutput plus call metadata (latency, tokens, cost,
whether this was a fallback). The rest of the system never imports a
concrete provider directly — it depends on this interface, so swapping or
adding providers never touches diagnosis/policy/workflow code.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.ai.schemas import AIDecisionOutput


@dataclass
class AIContext:
    """Everything the AI is allowed to reason over for one diagnosis call.
    Deliberately explicit and flat rather than 'just pass the ORM object' —
    this is the actual prompt input surface, and keeping it a plain
    dataclass means the exact signals available to the model are visible
    in one place, auditable, and easy to unit test without a DB."""
    risk_event_type: str
    failure_reason: str
    amount_minor: int
    currency: str
    previous_attempts: int
    customer_historical_successful_payments: int
    customer_historical_failed_payments: int
    customer_is_opted_out: bool
    merchant_max_retry_attempts: int
    merchant_allows_incentives: bool


@dataclass
class AIDecisionResult:
    output: AIDecisionOutput
    provider: str
    model_version: str
    prompt_version: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    was_fallback: bool = False
    validation_passed: bool = True
    raw_error: str | None = None


class AIProviderError(Exception):
    """Raised by a provider on timeout, transport failure, or unrecoverable
    error. Callers (the diagnosis service) catch this specifically to
    trigger the fallback path — this is never allowed to propagate up as
    an unhandled 500, because a revenue-risk workflow must always reach a
    defined state, even when the AI is unavailable."""
    pass


class AIProvider(ABC):
    name: str = "unset"

    @abstractmethod
    def diagnose(self, context: AIContext, *, timeout_seconds: float = 8.0) -> AIDecisionResult:
        """Return a validated AIDecisionResult, or raise AIProviderError."""
        raise NotImplementedError
