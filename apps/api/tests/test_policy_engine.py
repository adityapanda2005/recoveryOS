from app.db.models import RecommendedAction, PolicyDecision
from app.policy.engine import evaluate


class TestPolicyEngineCoreCases:
    def test_high_confidence_retry_within_limits_is_allowed(self, workflow, merchant_with_policy):
        _, policy = merchant_with_policy
        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.RETRY_PAYMENT,
            confidence=0.91, amount_minor=499900,
        )
        assert verdict.decision == PolicyDecision.ALLOW
        assert verdict.rule_triggered == "all_checks_passed"

    def test_ai_recommended_stop_is_always_honored(self, workflow, merchant_with_policy):
        """This is the core 'knowing when NOT to act' behavior — STOP is
        never overridden regardless of confidence or amount."""
        _, policy = merchant_with_policy
        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.STOP,
            confidence=0.99, amount_minor=100,
        )
        assert verdict.decision == PolicyDecision.BLOCK
        assert verdict.rule_triggered == "ai_recommended_stop"

    def test_low_confidence_is_escalated_not_silently_allowed(self, workflow, merchant_with_policy):
        """Critical test #5: low confidence escalates."""
        _, policy = merchant_with_policy
        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.RETRY_PAYMENT,
            confidence=0.40,  # below the 0.65 threshold
            amount_minor=499900,
        )
        assert verdict.decision == PolicyDecision.ESCALATE
        assert verdict.rule_triggered == "confidence_below_threshold"

    def test_retry_limit_is_enforced(self, db, workflow, merchant_with_policy):
        """Critical test #8: retry limit is enforced."""
        _, policy = merchant_with_policy
        workflow.retry_count = policy.max_retry_attempts  # already at the cap
        db.commit()

        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.RETRY_PAYMENT,
            confidence=0.95, amount_minor=499900,
        )
        assert verdict.decision == PolicyDecision.BLOCK
        assert verdict.rule_triggered == "max_retry_attempts_exceeded"

    def test_retry_below_limit_still_allowed(self, db, workflow, merchant_with_policy):
        """Sanity counterpart to the above: being one under the cap must
        still pass, proving the boundary check is >= not > (off-by-one)."""
        _, policy = merchant_with_policy
        workflow.retry_count = policy.max_retry_attempts - 1
        db.commit()

        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.RETRY_PAYMENT,
            confidence=0.95, amount_minor=499900,
        )
        assert verdict.decision == PolicyDecision.ALLOW

    def test_communication_limit_is_enforced(self, db, workflow, merchant_with_policy):
        """Critical test #9: communication limit is enforced."""
        _, policy = merchant_with_policy
        workflow.communication_count = policy.max_communication_attempts
        db.commit()

        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.SEND_REMINDER,
            confidence=0.90, amount_minor=499900,
        )
        assert verdict.decision == PolicyDecision.BLOCK
        assert verdict.rule_triggered == "max_communication_attempts_exceeded"

    def test_high_value_amount_escalates_regardless_of_confidence(self, workflow, merchant_with_policy):
        _, policy = merchant_with_policy
        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.RETRY_PAYMENT,
            confidence=0.99,  # very confident...
            amount_minor=policy.escalation_amount_threshold_minor,  # ...but ₹50,000+
        )
        assert verdict.decision == PolicyDecision.ESCALATE
        assert verdict.rule_triggered == "amount_exceeds_escalation_threshold"

    def test_opted_out_customer_blocks_communication_unconditionally(self, workflow, merchant_with_policy):
        _, policy = merchant_with_policy
        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.SEND_REMINDER,
            confidence=0.95, amount_minor=1000,
            customer_is_opted_out=True,
        )
        assert verdict.decision == PolicyDecision.BLOCK
        assert verdict.rule_triggered == "customer_opted_out"

    def test_incentive_above_cap_is_blocked(self, workflow, merchant_with_policy):
        _, policy = merchant_with_policy
        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.OFFER_INCENTIVE,
            confidence=0.90, amount_minor=10000,
            incentive_percent=25,  # merchant cap is 10%
        )
        assert verdict.decision == PolicyDecision.BLOCK
        assert verdict.rule_triggered == "incentive_exceeds_max_percent"

    def test_incentive_within_cap_is_allowed(self, workflow, merchant_with_policy):
        _, policy = merchant_with_policy
        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.OFFER_INCENTIVE,
            confidence=0.90, amount_minor=10000,
            incentive_percent=5,
        )
        assert verdict.decision == PolicyDecision.ALLOW

    def test_incentives_disabled_for_merchant_blocks_even_within_cap(self, db, workflow, merchant_with_policy):
        _, policy = merchant_with_policy
        policy.allow_incentives = False
        db.commit()

        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.OFFER_INCENTIVE,
            confidence=0.90, amount_minor=10000,
            incentive_percent=5,
        )
        assert verdict.decision == PolicyDecision.BLOCK
        assert verdict.rule_triggered == "incentives_disabled_for_merchant"

    def test_explicit_escalate_recommendation_is_honored(self, workflow, merchant_with_policy):
        _, policy = merchant_with_policy
        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.ESCALATE_TO_HUMAN,
            confidence=0.95, amount_minor=1000,
        )
        assert verdict.decision == PolicyDecision.ESCALATE
        assert verdict.rule_triggered == "ai_requested_escalation"

    def test_every_verdict_has_a_human_readable_explanation(self, workflow, merchant_with_policy):
        """Every policy_decisions row must be explainable without re-deriving
        it from code — enforce that explanation is always non-empty."""
        _, policy = merchant_with_policy
        verdict = evaluate(
            workflow=workflow, policy=policy,
            recommended_action=RecommendedAction.RETRY_PAYMENT,
            confidence=0.91, amount_minor=499900,
        )
        assert len(verdict.explanation) > 10
        assert verdict.rule_triggered  # never empty
