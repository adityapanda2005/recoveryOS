import pytest
from pydantic import ValidationError

from app.ai.provider import AIContext
from app.ai.mock_provider import MockAIProvider
from app.ai.schemas import AIDecisionOutput
from app.ai.diagnosis_service import diagnose_and_persist
from app.db.models import RecommendedAction, AIDecision


def _ctx(**overrides) -> AIContext:
    defaults = dict(
        risk_event_type="PAYMENT_FAILURE",
        failure_reason="BANK_TIMEOUT",
        amount_minor=499900,
        currency="INR",
        previous_attempts=0,
        customer_historical_successful_payments=7,
        customer_historical_failed_payments=1,
        customer_is_opted_out=False,
        merchant_max_retry_attempts=3,
        merchant_allows_incentives=True,
    )
    defaults.update(overrides)
    return AIContext(**defaults)


class TestMockProviderDeterminism:
    def test_same_input_produces_identical_output(self):
        provider = MockAIProvider()
        ctx = _ctx()
        r1 = provider.diagnose(ctx)
        r2 = provider.diagnose(ctx)
        assert r1.output.recommended_action == r2.output.recommended_action
        assert r1.output.confidence == r2.output.confidence
        assert r1.output.recoverability_score == r2.output.recoverability_score

    def test_different_input_can_produce_different_output(self):
        provider = MockAIProvider()
        loyal = provider.diagnose(_ctx(customer_historical_successful_payments=12, customer_historical_failed_payments=0))
        unreliable = provider.diagnose(_ctx(
            customer_historical_successful_payments=0,
            customer_historical_failed_payments=4,
            previous_attempts=3,
            merchant_max_retry_attempts=3,
        ))
        assert loyal.output.recommended_action != unreliable.output.recommended_action


class TestMockProviderHeuristics:
    """These directly verify the product spec's two worked examples."""

    def test_transient_failure_strong_history_recommends_retry_with_high_confidence(self):
        # Spec example: 7 successful payments, transient failure, one retry so far -> retry.
        provider = MockAIProvider()
        result = provider.diagnose(_ctx(
            failure_reason="BANK_TIMEOUT",
            customer_historical_successful_payments=7,
            customer_historical_failed_payments=0,
            previous_attempts=1,
        ))
        assert result.output.recommended_action == RecommendedAction.RETRY_PAYMENT
        assert result.output.confidence >= 0.65

    def test_exhausted_retries_with_poor_history_recommends_stop(self):
        # Spec example: 4 failed attempts, repeated decline, low recovery -> STOP.
        provider = MockAIProvider()
        result = provider.diagnose(_ctx(
            failure_reason="ISSUER_DECLINE",
            customer_historical_successful_payments=0,
            customer_historical_failed_payments=4,
            previous_attempts=3,
            merchant_max_retry_attempts=3,
        ))
        assert result.output.recommended_action == RecommendedAction.STOP
        assert result.output.stop_reason is not None

    def test_stop_recommendation_always_includes_a_reason(self):
        provider = MockAIProvider()
        result = provider.diagnose(_ctx(previous_attempts=5, merchant_max_retry_attempts=3))
        if result.output.recommended_action == RecommendedAction.STOP:
            assert result.output.stop_reason


class TestSchemaValidation:
    def test_valid_payload_constructs_cleanly(self):
        AIDecisionOutput(
            diagnosis="ok",
            evidence=[],
            recoverability_score=0.5,
            recommended_action=RecommendedAction.RETRY_PAYMENT,
            confidence=0.5,
            expected_recovery_minor=100,
            risk_level="LOW",
            recommended_delay_seconds=0,
        )

    def test_confidence_out_of_range_is_rejected(self):
        with pytest.raises(ValidationError):
            AIDecisionOutput(
                diagnosis="ok", evidence=[], recoverability_score=0.5,
                recommended_action=RecommendedAction.RETRY_PAYMENT,
                confidence=1.5,  # invalid
                expected_recovery_minor=100, risk_level="LOW",
                recommended_delay_seconds=0,
            )

    def test_stop_without_stop_reason_is_rejected(self):
        # Business-rule validation, not just type checking: an unexplained
        # STOP is exactly the "trust the model blindly" failure mode the
        # spec warns against.
        with pytest.raises(ValidationError):
            AIDecisionOutput(
                diagnosis="ok", evidence=[], recoverability_score=0.1,
                recommended_action=RecommendedAction.STOP,
                confidence=0.8, expected_recovery_minor=0, risk_level="LOW",
                recommended_delay_seconds=0, stop_reason=None,
            )

    def test_negative_expected_recovery_is_rejected(self):
        with pytest.raises(ValidationError):
            AIDecisionOutput(
                diagnosis="ok", evidence=[], recoverability_score=0.5,
                recommended_action=RecommendedAction.RETRY_PAYMENT,
                confidence=0.5, expected_recovery_minor=-100, risk_level="LOW",
                recommended_delay_seconds=0,
            )


class TestDiagnosisServiceFallback:
    """These exercise real injected failures, not hypothetical ones."""

    def test_successful_diagnosis_is_persisted_with_validation_passed_true(self, db, workflow):
        provider = MockAIProvider()
        decision = diagnose_and_persist(db, workflow_id=workflow.id, context=_ctx(), provider=provider)

        assert decision.validation_passed is True
        assert decision.was_fallback is False
        assert decision.provider == "mock"
        assert decision.id is not None

        reloaded = db.query(AIDecision).filter_by(id=decision.id).first()
        assert reloaded is not None
        assert reloaded.recommended_action == decision.recommended_action

        db.query(AIDecision).filter_by(id=decision.id).delete()
        db.commit()

    def test_provider_timeout_triggers_fallback_to_escalation(self, db, workflow):
        # Real injected failure: MockAIProvider(force_timeout=True) actually
        # raises AIProviderError, exactly as a real network timeout would.
        provider = MockAIProvider(force_timeout=True)
        decision = diagnose_and_persist(db, workflow_id=workflow.id, context=_ctx(), provider=provider)

        assert decision.was_fallback is True
        assert decision.validation_passed is False
        assert decision.recommended_action == RecommendedAction.ESCALATE_TO_HUMAN
        assert decision.confidence == 0  # fallback must not claim false confidence

        db.query(AIDecision).filter_by(id=decision.id).delete()
        db.commit()

    def test_malformed_output_triggers_fallback_not_a_crash(self, db, workflow):
        # Real injected failure: provider returns output that fails
        # AIDecisionOutput validation. The service must not raise — a
        # financial workflow must always reach a defined state.
        provider = MockAIProvider(force_malformed=True)
        decision = diagnose_and_persist(db, workflow_id=workflow.id, context=_ctx(), provider=provider)

        assert decision.was_fallback is True
        assert decision.recommended_action == RecommendedAction.ESCALATE_TO_HUMAN

        db.query(AIDecision).filter_by(id=decision.id).delete()
        db.commit()

    def test_fallback_decision_is_still_a_valid_enum_value_policy_engine_can_consume(self, db, workflow):
        # Guards against a subtle bug: the fallback path must produce a
        # decision the policy engine can process like any other, not a
        # special-cased shape that would need separate handling downstream.
        provider = MockAIProvider(force_timeout=True)
        decision = diagnose_and_persist(db, workflow_id=workflow.id, context=_ctx(), provider=provider)

        assert decision.recommended_action in RecommendedAction
        assert 0.0 <= float(decision.confidence) <= 1.0

        db.query(AIDecision).filter_by(id=decision.id).delete()
        db.commit()
