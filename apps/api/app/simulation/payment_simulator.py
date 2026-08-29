"""
Payment/recovery simulator.

Stands in for Razorpay's real payment/notification/link APIs so the whole
system is demoable and testable with zero external dependency and zero
production money movement, per the product spec's "Demo Mode" and
"Simulation" requirements.

Determinism is the whole point here: given the same seed and the same
idempotency_key, this always returns the same outcome. That's what makes
the 1,000-event batch (Phase 5) reproducible, and what lets the Failure Lab
force a specific failure on demand instead of hoping for one.

Supported outcomes (matches the product spec's simulator requirements):
    SUCCESS, TRANSIENT_FAILURE, PERMANENT_FAILURE, TIMEOUT,
    DUPLICATE_WEBHOOK, RATE_LIMITED, DELAYED_RESPONSE
"""
import enum
import hashlib
import time
from dataclasses import dataclass


class SimulatedOutcome(str, enum.Enum):
    SUCCESS = "SUCCESS"
    TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    DELAYED_RESPONSE = "DELAYED_RESPONSE"


@dataclass
class SimulatedResult:
    outcome: SimulatedOutcome
    external_reference: str | None
    latency_ms: int
    error_detail: str | None = None


class TimeoutSimulated(Exception):
    """Raised (not returned) for TIMEOUT, because a real HTTP timeout is
    exactly this: the caller never learns the outcome, which is the entire
    point of the PENDING_VERIFICATION / reconciliation pattern. Returning a
    normal result for a timeout would defeat the scenario we're testing."""
    pass


class RateLimited(Exception):
    """Raised for RATE_LIMITED so callers apply backoff rather than treat
    it as a normal failure outcome."""
    pass


def _deterministic_bucket(idempotency_key: str, seed: int, buckets: int = 1000) -> int:
    """Stable hash -> [0, buckets). Same key + seed always lands in the same
    bucket, which is what makes forced outcomes and batch runs reproducible."""
    h = hashlib.sha256(f"{seed}:{idempotency_key}".encode()).hexdigest()
    return int(h, 16) % buckets


def simulate_payment_retry(
    idempotency_key: str,
    *,
    seed: int = 0,
    force_outcome: SimulatedOutcome | None = None,
    transient_rate: float = 0.15,
    permanent_rate: float = 0.05,
    timeout_rate: float = 0.05,
) -> SimulatedResult:
    """
    Simulate one payment retry attempt.

    force_outcome bypasses the probability distribution entirely — this is
    the Failure Lab's hook for "make this specific call time out" on demand.
    Without it, outcome is derived deterministically from
    (idempotency_key, seed) so re-running the same batch reproduces
    identical results.
    """
    start = time.monotonic()

    outcome = force_outcome
    if outcome is None:
        bucket = _deterministic_bucket(idempotency_key, seed) / 1000.0
        if bucket < timeout_rate:
            outcome = SimulatedOutcome.TIMEOUT
        elif bucket < timeout_rate + permanent_rate:
            outcome = SimulatedOutcome.PERMANENT_FAILURE
        elif bucket < timeout_rate + permanent_rate + transient_rate:
            outcome = SimulatedOutcome.TRANSIENT_FAILURE
        else:
            outcome = SimulatedOutcome.SUCCESS

    latency_ms = int((time.monotonic() - start) * 1000) + 40  # simulated network floor

    if outcome == SimulatedOutcome.TIMEOUT:
        raise TimeoutSimulated(
            f"Simulated timeout for idempotency_key={idempotency_key}: "
            f"outcome on the external side is genuinely unknown."
        )
    if outcome == SimulatedOutcome.RATE_LIMITED:
        raise RateLimited(f"Simulated rate limit for idempotency_key={idempotency_key}.")

    if outcome == SimulatedOutcome.SUCCESS:
        return SimulatedResult(
            outcome=outcome,
            external_reference=f"sim_pay_{idempotency_key[:12]}",
            latency_ms=latency_ms,
        )

    if outcome == SimulatedOutcome.DELAYED_RESPONSE:
        # Succeeds, but simulate a slow external call -- exercises timeout
        # -handling boundaries (e.g. "did we give up too early") without
        # actually being a failure.
        return SimulatedResult(
            outcome=outcome,
            external_reference=f"sim_pay_{idempotency_key[:12]}",
            latency_ms=latency_ms + 5000,
        )

    return SimulatedResult(
        outcome=outcome,
        external_reference=None,
        latency_ms=latency_ms,
        error_detail=(
            "Simulated transient decline (e.g. bank timeout, temporary issuer decline)."
            if outcome == SimulatedOutcome.TRANSIENT_FAILURE
            else "Simulated permanent decline (e.g. expired card, mandate revoked)."
        ),
    )


def reconcile_uncertain_action(idempotency_key: str, *, seed: int = 0) -> SimulatedResult:
    """
    Simulates checking the *actual* external state after a timeout, i.e.
    what a real reconciliation job would do: query Razorpay's API for the
    true status of a payment whose retry call timed out.

    Deliberately uses a DIFFERENT deterministic bucketing than the original
    attempt (salted with "reconcile") so it doesn't just always re-derive
    "still unknown" — it simulates that the payment genuinely did resolve
    on the provider's side even though our HTTP call never got a response.
    """
    bucket = _deterministic_bucket(f"reconcile:{idempotency_key}", seed) / 1000.0
    if bucket < 0.5:
        return SimulatedResult(
            outcome=SimulatedOutcome.SUCCESS,
            external_reference=f"sim_pay_{idempotency_key[:12]}",
            latency_ms=60,
            error_detail="Reconciliation found the original attempt had actually succeeded.",
        )
    return SimulatedResult(
        outcome=SimulatedOutcome.TRANSIENT_FAILURE,
        external_reference=None,
        latency_ms=60,
        error_detail="Reconciliation confirmed the original attempt did not succeed.",
    )
