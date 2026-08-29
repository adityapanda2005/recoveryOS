import pytest

from app.evaluation.generator import generate_batch
from app.evaluation.baseline import run_baseline, BASELINE_MAX_RETRIES
from app.evaluation.recoveros_runner import run_recoveros, _default_policy
from app.evaluation.metrics import compute_metrics
from app.ai.mock_provider import MockAIProvider


class TestGeneratorDeterminism:
    def test_same_seed_produces_identical_batch(self):
        b1 = generate_batch(n=100, seed=7)
        b2 = generate_batch(n=100, seed=7)
        assert [e.ref for e in b1] == [e.ref for e in b2]
        assert [e.ground_truth_recoverable for e in b1] == [e.ground_truth_recoverable for e in b2]
        assert [e.customer_tier for e in b1] == [e.customer_tier for e in b2]

    def test_different_seed_produces_different_batch(self):
        b1 = generate_batch(n=100, seed=1)
        b2 = generate_batch(n=100, seed=2)
        assert [e.ground_truth_recoverable for e in b1] != [e.ground_truth_recoverable for e in b2]

    def test_batch_has_requested_size(self):
        assert len(generate_batch(n=250, seed=1)) == 250

    def test_all_customer_tiers_present_in_realistic_batch(self):
        batch = generate_batch(n=500, seed=1)
        tiers = {e.customer_tier for e in batch}
        assert tiers == {"loyal", "mixed", "unreliable"}

    def test_ground_truth_recovery_probability_is_bounded(self):
        batch = generate_batch(n=500, seed=1)
        assert all(0.0 <= e.ground_truth_recovery_probability <= 1.0 for e in batch)

    def test_opted_out_customers_have_zero_recovery_probability(self):
        batch = generate_batch(n=2000, seed=1)  # large enough to guarantee some opt-outs
        opted_out = [e for e in batch if e.customer_is_opted_out]
        assert len(opted_out) > 0
        assert all(e.ground_truth_recovery_probability == 0.0 for e in opted_out)
        assert all(e.ground_truth_recoverable is False for e in opted_out)


class TestBaselinePolicy:
    def test_permanent_failure_reason_is_never_retried(self):
        batch = generate_batch(n=200, seed=1)
        permanent = [e for e in batch if e.failure_reason in {"CARD_EXPIRED", "MANDATE_REVOKED"}]
        assert permanent  # sanity: dataset actually has some
        for e in permanent:
            outcome = run_baseline(e)
            assert outcome.action == "NO_ACTION"

    def test_opted_out_customer_is_never_retried(self):
        batch = generate_batch(n=2000, seed=1)
        opted_out = [e for e in batch if e.customer_is_opted_out]
        for e in opted_out:
            outcome = run_baseline(e)
            assert outcome.action == "NO_ACTION"

    def test_retry_exhausted_is_not_retried_again(self):
        batch = generate_batch(n=500, seed=1)
        exhausted = [e for e in batch if e.previous_attempts >= BASELINE_MAX_RETRIES
                     and e.failure_reason not in {"CARD_EXPIRED", "MANDATE_REVOKED"}
                     and not e.customer_is_opted_out]
        assert exhausted
        for e in exhausted:
            outcome = run_baseline(e)
            assert outcome.action == "NO_ACTION"

    def test_recovered_outcome_matches_ground_truth_when_retried(self):
        batch = generate_batch(n=500, seed=1)
        for e in batch:
            outcome = run_baseline(e)
            if outcome.action == "RETRY":
                assert outcome.recovered == e.ground_truth_recoverable
                assert outcome.was_unnecessary == (not e.ground_truth_recoverable)


class TestRecoverOSRunner:
    def test_stop_recommendation_results_in_no_automated_action(self):
        # A customer profile the mock provider's heuristics should reliably STOP on.
        batch = generate_batch(n=500, seed=1)
        provider = MockAIProvider()
        policy = _default_policy()
        stopped_any = False
        for e in batch:
            outcome = run_recoveros(e, provider=provider, policy=policy)
            if outcome.action == "STOP":
                stopped_any = True
                assert outcome.policy_decision == "BLOCK"
                assert outcome.recovered is False
                assert outcome.was_unnecessary is False
        assert stopped_any  # sanity: the heuristic does STOP on some cases in this batch

    def test_missed_recovery_only_flagged_when_ground_truth_says_recoverable(self):
        batch = generate_batch(n=500, seed=1)
        provider = MockAIProvider()
        policy = _default_policy()
        for e in batch:
            outcome = run_recoveros(e, provider=provider, policy=policy)
            if outcome.was_missed_recovery:
                assert e.ground_truth_recoverable is True
                assert outcome.policy_decision == "BLOCK"

    def test_escalated_cases_are_never_counted_as_recovered_or_unnecessary(self):
        batch = generate_batch(n=500, seed=1)
        provider = MockAIProvider()
        policy = _default_policy()
        for e in batch:
            outcome = run_recoveros(e, provider=provider, policy=policy)
            if outcome.was_escalated:
                assert outcome.recovered is False
                assert outcome.was_unnecessary is False

    def test_deterministic_given_same_event_and_provider(self):
        batch = generate_batch(n=50, seed=1)
        provider = MockAIProvider()
        policy = _default_policy()
        for e in batch:
            o1 = run_recoveros(e, provider=provider, policy=policy)
            o2 = run_recoveros(e, provider=provider, policy=policy)
            assert o1.action == o2.action
            assert o1.policy_decision == o2.policy_decision
            assert o1.recovered == o2.recovered


class TestMetricsFormulas:
    def test_recovery_rate_is_recovered_over_total(self):
        m = compute_metrics(
            total_amount_at_risk_minor=1000,
            recovered_flags=[True, True, False, False],
            recovered_amounts_minor=[100, 200, 300, 400],
            unnecessary_flags=[False, False, True, False],
            missed_recovery_flags=[False, False, False, True],
        )
        assert m.total_events == 4
        assert m.recovered_count == 2
        assert m.recovered_amount_minor == 300  # 100 + 200
        assert m.recovery_rate == 0.5
        assert m.unnecessary_intervention_count == 1
        assert m.unnecessary_intervention_rate == 0.25
        assert m.false_negative_count == 1
        assert m.false_negative_rate == 0.25

    def test_escalation_rate_defaults_to_zero_when_not_provided(self):
        m = compute_metrics(
            total_amount_at_risk_minor=100,
            recovered_flags=[True],
            recovered_amounts_minor=[100],
            unnecessary_flags=[False],
            missed_recovery_flags=[False],
        )
        assert m.escalation_rate == 0.0

    def test_empty_batch_raises_rather_than_divide_by_zero(self):
        with pytest.raises(ValueError):
            compute_metrics(
                total_amount_at_risk_minor=0,
                recovered_flags=[],
                recovered_amounts_minor=[],
                unnecessary_flags=[],
                missed_recovery_flags=[],
            )


class TestFullBatchEvaluation:
    """Runs the actual full evaluation pipeline (not persisted) on a modest
    batch, verifying the end-to-end wiring produces internally-consistent,
    real numbers -- not that any particular number is 'good', since the
    product spec explicitly requires honest reporting even of unfavorable
    results."""

    def test_full_evaluation_produces_internally_consistent_metrics(self):
        from app.evaluation.run_evaluation import run

        result = run(n=300, seed=99, persist=False)

        assert result["baseline"].total_events == 300
        assert result["recoveros"].total_events == 300
        assert result["total_amount_at_risk_minor"] > 0

        # Recovered amount can never exceed total amount at risk, for either system.
        assert result["baseline"].recovered_amount_minor <= result["total_amount_at_risk_minor"]
        assert result["recoveros"].recovered_amount_minor <= result["total_amount_at_risk_minor"]

        # All rates are valid probabilities.
        for m in (result["baseline"], result["recoveros"]):
            assert 0.0 <= m.recovery_rate <= 1.0
            assert 0.0 <= m.unnecessary_intervention_rate <= 1.0
            assert 0.0 <= m.false_negative_rate <= 1.0
            assert 0.0 <= m.escalation_rate <= 1.0

        # Baseline structurally has no escalation concept.
        assert result["baseline"].escalation_rate == 0.0

    def test_full_evaluation_is_reproducible_given_same_seed(self):
        from app.evaluation.run_evaluation import run

        r1 = run(n=200, seed=123, persist=False)
        r2 = run(n=200, seed=123, persist=False)

        assert r1["baseline"].recovered_amount_minor == r2["baseline"].recovered_amount_minor
        assert r1["recoveros"].recovered_amount_minor == r2["recoveros"].recovered_amount_minor
        assert r1["recoveros"].recovery_rate == r2["recoveros"].recovery_rate
