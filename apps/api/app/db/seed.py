"""
Deterministic seed data for local development and demos.

Run: python -m app.db.seed

Creates one merchant with a realistic policy, and a handful of customers
with varied payment histories, so the dashboard has something real to show
before you run the full 1,000-event batch generator (Phase 5).
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.db.session import SessionLocal
from app.db.models import Merchant, Customer, MerchantPolicy


SEED_MERCHANT_ID = "00000000-0000-0000-0000-000000000001"


def run():
    db = SessionLocal()
    try:
        existing = db.query(Merchant).filter_by(id=SEED_MERCHANT_ID).first()
        if existing:
            print("Seed data already present. Skipping.")
            return

        merchant = Merchant(
            id=SEED_MERCHANT_ID,
            name="Demo Kirana Superstore",
            razorpay_account_id="acc_demo_kirana",
        )
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
            escalation_amount_threshold_minor=5000000,  # ₹50,000
        )
        db.add(policy)

        customers = [
            Customer(
                merchant_id=merchant.id,
                external_customer_id=f"cust_{i:03d}",
                historical_successful_payments=hp,
                historical_failed_payments=fp,
                is_opted_out=False,
            )
            for i, (hp, fp) in enumerate([
                (7, 1),   # loyal, one recent hiccup -> should recover easily
                (0, 4),   # new/unreliable customer -> should be a STOP case
                (12, 0),  # perfect history -> high confidence retry
                (2, 3),   # mixed history -> ambiguous, may need human review
                (5, 5),   # borderline -> tests threshold behavior
            ], start=1)
        ]
        db.add_all(customers)
        db.commit()
        print(f"Seeded merchant {merchant.id} with policy and {len(customers)} customers.")
    finally:
        db.close()


if __name__ == "__main__":
    run()
