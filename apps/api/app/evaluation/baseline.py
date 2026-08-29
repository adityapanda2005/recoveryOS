"""
Static baseline policy.

The dumb comparison point the product spec explicitly asks for: "retry
every retryable failure up to N times." No customer history, no failure
reason classification, no confidence, no STOP-when-appropriate — just a
fixed rule. This exists specifically so we can honestly answer "does
intelligence actually improve outcomes" rather than assert it.

A "retryable" failure here means anything that isn't a permanent failure
reason (expired card / mandate revoked) or an opted-out customer -- the
absolute minimum discrimination a non-AI system would apply in practice,
since retrying a definitionally-doomed payment method is not a realistic
baseline even for "dumb" systems.
"""
from dataclasses import dataclass

from app.evaluation.generator import SyntheticEvent, PERMANENT_FAILURE_REASONS

BASELINE_MAX_RETRIES = 3


@dataclass
class BaselineOutcome:
    action: str  # "RETRY" | "NO_ACTION"
    recovered: bool
    was_unnecessary: bool  # attempted an action on a genuinely unrecoverable case


def run_baseline(event: SyntheticEvent, *, max_retries: int = BASELINE_MAX_RETRIES) -> BaselineOutcome:
    """
    Deterministic given the event and its ground truth -- this is
    evaluating "if we retried this, would it have worked," using the
    same ground_truth_recoverable label RecoverOS's outcome is also
    judged against, so the comparison is apples-to-apples.
    """
    is_permanent = event.failure_reason in PERMANENT_FAILURE_REASONS
    retryable = (not is_permanent) and (not event.customer_is_opted_out) and event.previous_attempts < max_retries

    if not retryable:
        return BaselineOutcome(action="NO_ACTION", recovered=False, was_unnecessary=False)

    # The baseline retries blindly -- it "succeeds" exactly when the case
    # was genuinely recoverable, and wastes an attempt (unnecessary
    # intervention) whenever it retries a case that was never going to
    # recover, which is precisely the cost RecoverOS's STOP logic exists
    # to avoid.
    if event.ground_truth_recoverable:
        return BaselineOutcome(action="RETRY", recovered=True, was_unnecessary=False)
    return BaselineOutcome(action="RETRY", recovered=False, was_unnecessary=True)
