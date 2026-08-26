import pytest

from app.db.session import SessionLocal
from app.db.models import Merchant, MerchantPolicy, Customer, RevenueRiskEvent, RiskEventType, FailureReason
from app.domain.state_machine import create_workflow


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def merchant_with_policy(db):
    """A merchant with a standard policy, cleaned up after the test."""
    merchant = Merchant(name="Test Merchant Co")
    db.add(merchant)
    db.flush()

    policy = MerchantPolicy(
        merchant_id=merchant.id,
        max_retry_attempts=3,
        min_retry_cooldown_seconds=1800,
        max_communication_attempts=4,
        confidence_threshold=0.65,
        max_incentive_percent=10,
        allow_incentives=True,
        escalation_amount_threshold_minor=5_000_000,  # ₹50,000
    )
    db.add(policy)
    db.commit()
    db.refresh(merchant)
    db.refresh(policy)

    yield merchant, policy

    # cleanup
    db.query(MerchantPolicy).filter_by(id=policy.id).delete()
    db.query(Merchant).filter_by(id=merchant.id).delete()
    db.commit()


@pytest.fixture
def customer(db, merchant_with_policy):
    merchant, _ = merchant_with_policy
    cust = Customer(
        merchant_id=merchant.id,
        external_customer_id="test_cust_001",
        historical_successful_payments=5,
        historical_failed_payments=1,
        is_opted_out=False,
    )
    db.add(cust)
    db.commit()
    db.refresh(cust)

    yield cust

    db.query(Customer).filter_by(id=cust.id).delete()
    db.commit()


@pytest.fixture
def risk_event(db, merchant_with_policy, customer):
    merchant, _ = merchant_with_policy
    event = RevenueRiskEvent(
        merchant_id=merchant.id,
        customer_id=customer.id,
        event_type=RiskEventType.PAYMENT_FAILURE,
        failure_reason=FailureReason.BANK_TIMEOUT,
        amount_minor=499900,  # ₹4,999.00
        currency="INR",
        reference_type="payment",
        reference_id="pay_test_001",
        previous_attempts=0,
        source_event_id="evt_test_001",
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    yield event

    db.query(RevenueRiskEvent).filter_by(id=event.id).delete()
    db.commit()


@pytest.fixture
def workflow(db, risk_event, merchant_with_policy):
    merchant, _ = merchant_with_policy
    wf = create_workflow(db, risk_event_id=risk_event.id, merchant_id=merchant.id)
    yield wf
    # workflow_transitions cascade via FK but we have no ON DELETE CASCADE,
    # so clean up children explicitly to avoid FK violations on teardown.
    from app.db.models import WorkflowTransition
    db.query(WorkflowTransition).filter_by(workflow_id=wf.id).delete()
    db.query(type(wf)).filter_by(id=wf.id).delete()
    db.commit()
