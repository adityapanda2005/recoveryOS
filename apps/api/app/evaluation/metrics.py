"""
Evaluation metrics.

Exact formulas, computed from real per-event outcomes -- nothing here is
asserted, everything is derived from the EvaluationEvent rows a batch run
actually produces. If RecoverOS does not beat the baseline on some metric,
that is reported as-is; there is no code path that adjusts or hides an
unfavorable result.
"""
from dataclasses import dataclass


@dataclass
class MetricsSummary:
    total_events: int
    total_amount_at_risk_minor: int

    recovered_count: int
    recovered_amount_minor: int
    recovery_rate: float  # recovered_count / total_events

    unnecessary_intervention_count: int
    unnecessary_intervention_rate: float  # unnecessary / total_events

    false_positive_count: int  # acted, but not actually recoverable (== unnecessary interventions)
    false_positive_rate: float
    false_negative_count: int  # didn't act, but was actually recoverable
    false_negative_rate: float

    escalation_count: int
    escalation_rate: float


def compute_metrics(
    *,
    total_amount_at_risk_minor: int,
    recovered_flags: list[bool],
    recovered_amounts_minor: list[int],
    unnecessary_flags: list[bool],
    missed_recovery_flags: list[bool],
    escalated_flags: list[bool] | None = None,
) -> MetricsSummary:
    n = len(recovered_flags)
    if n == 0:
        raise ValueError("Cannot compute metrics over an empty batch.")

    escalated_flags = escalated_flags or [False] * n

    recovered_count = sum(recovered_flags)
    recovered_amount = sum(a for a, r in zip(recovered_amounts_minor, recovered_flags) if r)
    unnecessary_count = sum(unnecessary_flags)
    false_negative_count = sum(missed_recovery_flags)
    escalation_count = sum(escalated_flags)

    return MetricsSummary(
        total_events=n,
        total_amount_at_risk_minor=total_amount_at_risk_minor,
        recovered_count=recovered_count,
        recovered_amount_minor=recovered_amount,
        recovery_rate=round(recovered_count / n, 4),
        unnecessary_intervention_count=unnecessary_count,
        unnecessary_intervention_rate=round(unnecessary_count / n, 4),
        false_positive_count=unnecessary_count,  # acted on a non-recoverable case
        false_positive_rate=round(unnecessary_count / n, 4),
        false_negative_count=false_negative_count,
        false_negative_rate=round(false_negative_count / n, 4),
        escalation_count=escalation_count,
        escalation_rate=round(escalation_count / n, 4),
    )
