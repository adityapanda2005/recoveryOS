"""
Synthetic revenue-risk event generator for evaluation.

The critical design constraint here: every generated event has an explicit
`ground_truth_recoverable` label, deterministically derived from the same
underlying "true" customer/failure profile that also drives what AI
diagnosis and baseline policy see. This is what lets Phase 5 compute real
precision/recall-style metrics (false positive rate, false negative rate)
instead of just "we recovered some revenue" — a claim that would be
worthless without a known-correct answer to compare against.

Ground truth model (deliberately explicit and readable, not a black box):
  - A synthetic customer has a "true" reliability tier (loyal / mixed /
    unreliable) that determines their real recovery probability.
  - A synthetic event's failure_reason and previous_attempts modify that
    base probability (permanent failure reasons and near-exhausted retries
    push recoverability down, regardless of customer tier).
  - ground_truth_recoverable = True if the resulting recoverability
    crosses a fixed threshold. This is what "actually happened" in the
    simulated world -- the AI/baseline don't get to see this value, only
    the same signals (event_type, failure_reason, previous_attempts,
    customer history) a real system would have.

Fully deterministic given a seed: regenerating with the same seed produces
the exact same 1,000 events, which is what makes a baseline-vs-RecoverOS
comparison fair and reproducible.
"""
import hashlib
import random
from dataclasses import dataclass

TRANSIENT_FAILURE_REASONS = ["BANK_TIMEOUT", "NETWORK_ERROR", "ISSUER_DECLINE"]
PERMANENT_FAILURE_REASONS = ["CARD_EXPIRED", "MANDATE_REVOKED"]
ALL_FAILURE_REASONS = TRANSIENT_FAILURE_REASONS + PERMANENT_FAILURE_REASONS

CUSTOMER_TIERS = {
    # tier: (base_recovery_probability, weight in population)
    "loyal": (0.85, 0.30),
    "mixed": (0.50, 0.45),
    "unreliable": (0.15, 0.25),
}


@dataclass
class SyntheticEvent:
    ref: str
    event_type: str
    failure_reason: str
    amount_minor: int
    previous_attempts: int
    customer_tier: str
    customer_historical_successful_payments: int
    customer_historical_failed_payments: int
    customer_is_opted_out: bool
    ground_truth_recoverable: bool
    ground_truth_recovery_probability: float


def _weighted_tier(rng: random.Random) -> str:
    tiers = list(CUSTOMER_TIERS.keys())
    weights = [CUSTOMER_TIERS[t][1] for t in tiers]
    return rng.choices(tiers, weights=weights, k=1)[0]


def _history_for_tier(tier: str, rng: random.Random) -> tuple[int, int]:
    if tier == "loyal":
        success = rng.randint(6, 15)
        failed = rng.randint(0, 2)
    elif tier == "mixed":
        success = rng.randint(1, 6)
        failed = rng.randint(1, 6)
    else:  # unreliable
        success = rng.randint(0, 2)
        failed = rng.randint(2, 8)
    return success, failed


def generate_batch(n: int = 1000, *, seed: int = 42) -> list[SyntheticEvent]:
    """
    Generate n synthetic revenue-risk events with known ground truth.
    Deterministic: same n + seed always produces the identical batch.
    """
    rng = random.Random(seed)
    events: list[SyntheticEvent] = []

    for i in range(n):
        ref = f"synth_{seed}_{i:05d}"
        tier = _weighted_tier(rng)
        base_prob, _ = CUSTOMER_TIERS[tier]
        success_hist, failed_hist = _history_for_tier(tier, rng)

        # Event type distribution roughly matching the product spec's
        # named failure modes.
        event_type = rng.choices(
            ["PAYMENT_FAILURE", "CHECKOUT_ABANDONMENT", "RECURRING_PAYMENT_FAILURE"],
            weights=[0.55, 0.25, 0.20], k=1,
        )[0]

        failure_reason = rng.choice(ALL_FAILURE_REASONS)
        previous_attempts = rng.choices([0, 1, 2, 3, 4], weights=[0.45, 0.25, 0.15, 0.10, 0.05], k=1)[0]
        amount_minor = rng.choice([9900, 19900, 49900, 99900, 199900, 499900, 999900])
        is_opted_out = rng.random() < 0.05

        # Ground truth recoverability derivation: start from the
        # customer's true tier probability, then apply real-world
        # modifiers a genuine system would also implicitly capture.
        prob = base_prob
        if failure_reason in PERMANENT_FAILURE_REASONS:
            prob *= 0.35  # permanent failures are much harder to recover regardless of tier
        if previous_attempts >= 3:
            prob *= 0.4  # exhausted-looking cases are genuinely less recoverable
        if is_opted_out:
            prob = 0.0  # cannot recover via communication if the customer opted out
        prob = max(0.0, min(1.0, prob))

        # Deterministic "coin flip" derived from the event ref, not the
        # shared rng stream, so ground truth is independently reproducible
        # per-event (useful later if events are regenerated individually).
        draw = int(hashlib.sha256(f"truth:{ref}".encode()).hexdigest(), 16) % 10000 / 10000.0
        ground_truth_recoverable = draw < prob

        events.append(SyntheticEvent(
            ref=ref,
            event_type=event_type,
            failure_reason=failure_reason,
            amount_minor=amount_minor,
            previous_attempts=previous_attempts,
            customer_tier=tier,
            customer_historical_successful_payments=success_hist,
            customer_historical_failed_payments=failed_hist,
            customer_is_opted_out=is_opted_out,
            ground_truth_recoverable=ground_truth_recoverable,
            ground_truth_recovery_probability=round(prob, 4),
        ))

    return events
