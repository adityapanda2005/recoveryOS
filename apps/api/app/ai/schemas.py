"""
The AI decision contract.

This is the ONLY shape of data the rest of the system will ever accept from
an AI provider. No provider implementation (Anthropic, Mock, or any future
one) is trusted to hand back arbitrary text — every response is validated
against this Pydantic schema before it touches a workflow, and a response
that fails validation is treated as a provider failure (triggers fallback),
not silently coerced into something usable.

This directly implements the product spec's structured-output requirement:
    {
      "diagnosis": "...",
      "evidence": [],
      "recoverability_score": 0.0,
      "recommended_action": "...",
      "confidence": 0.0,
      "expected_recovery": 0,
      "risk_level": "...",
      "recommended_delay_seconds": 0,
      "customer_message_intent": "...",
      "stop_reason": null
    }
"""
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models import RecommendedAction, RiskLevel


class AIDecisionOutput(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    diagnosis: str = Field(..., min_length=1, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=20)

    recoverability_score: float = Field(..., ge=0.0, le=1.0)
    recommended_action: RecommendedAction
    confidence: float = Field(..., ge=0.0, le=1.0)

    expected_recovery_minor: int = Field(..., ge=0)
    risk_level: RiskLevel
    recommended_delay_seconds: int = Field(default=0, ge=0)

    customer_message_intent: str | None = Field(default=None, max_length=500)
    stop_reason: str | None = Field(default=None, max_length=500)

    @field_validator("stop_reason")
    @classmethod
    def stop_reason_required_when_stopping(cls, v, info):
        # Business-rule validation, not just type validation: if the AI
        # recommends STOP, it must explain why. An unexplained STOP is
        # exactly the kind of "trust the model blindly" failure the spec
        # explicitly warns against.
        action = info.data.get("recommended_action")
        if action == RecommendedAction.STOP and not v:
            raise ValueError("stop_reason is required when recommended_action is STOP")
        return v

