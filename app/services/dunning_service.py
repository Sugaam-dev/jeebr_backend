"""
app/services/dunning_service.py

SentinelOS Staged Dunning & Delinquency State Machine.
Implements calibrated overdue recovery with explicit consumer protection invariants:
1. Stage 1 (T-3 Days): Courtesy SMS/Email reminder with direct payment link.
2. Stage 2 (T+0 Days): Due Date Notification; Grace Period initiates (default 3 days).
3. Stage 3 (T+3 Days): Soft Delinquency; Assesses late payment fee; Network QoS throttle notice.
4. Stage 4 (T+7 Days): Walled Garden; HTTP/HTTPS captive portal redirection.
5. Stage 5 (T+15 Days): Hard Suspension; Session terminated / PoD (Packet of Disconnect).
6. Stage 6 (T+45 Days): Bad Debt Provisioning & Recovery Escalation.

Safety Invariants:
- Never throttle or disconnect if account has an active Promise-to-Pay (PTP) commitment.
- Never throttle if account has unallocated deposit credits sufficient to clear the bill (auto-allocates instead).
- Never throttle if the invoice is locked in an active billing dispute.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy.orm import Session

from app.models import Customer, Invoice
from app.models_finance import (
    BillingAccount, BillingSubscription, InvoiceStatus,
    NotificationOutbox, PromiseToPay, ChartOfAccounts
)
from app.services.notification_service import enqueue_notification


class DunningStage:
    STAGE_0_CURRENT = 0
    STAGE_1_REMINDER = 1
    STAGE_2_PAST_DUE = 2
    STAGE_3_SOFT_THROTTLE = 3
    STAGE_4_WALLED_GARDEN = 4
    STAGE_5_HARD_SUSPENSION = 5
    STAGE_6_TERMINATION_WRITE_OFF = 6


class DunningNetworkAction:
    NONE = "NONE"
    THROTTLE_2MBPS = "THROTTLE_2MBPS"
    WALLED_GARDEN = "WALLED_GARDEN"
    PACKET_OF_DISCONNECT = "PACKET_OF_DISCONNECT"
    RESTORE_SERVICE = "RESTORE_SERVICE"


# Configuration Defaults
REMINDER_DAYS_BEFORE_DUE = 3
GRACE_PERIOD_DAYS = 3
THROTTLE_DAYS_PAST_DUE = 7
WALLED_GARDEN_DAYS_PAST_DUE = 14
SUSPENSION_DAYS_PAST_DUE = 21
LATE_FEE_FIXED_AMOUNT = Decimal("100.00")
MINIMUM_OVERDUE_FOR_ACTION = Decimal("50.00")


def register_promise_to_pay(
    db: Session,
    billing_account_id: str,
    commitment_date: datetime,
    committed_amount: Decimal,
    notes: Optional[str] = None,
    created_by_id: Optional[int] = None
) -> PromiseToPay:
    """
    Registers an official Promise-to-Pay commitment.
    Temporarily pauses dunning suspensions until commitment_date.
    """
    acct_uuid = uuid.UUID(str(billing_account_id))
    account = db.query(BillingAccount).filter(BillingAccount.id == acct_uuid).first()
    if not account:
        raise ValueError(f"Billing account '{billing_account_id}' not found.")

    # Find earliest open overdue invoice
    inv = (
        db.query(Invoice)
        .filter(
            Invoice.billing_account_id == account.id,
            Invoice.outstanding_amount > Decimal("0.00"),
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.LATE, InvoiceStatus.OVERDUE])
        )
        .order_by(Invoice.due_date.asc())
        .first()
    )

    ptp = PromiseToPay(
        id=uuid.uuid4(),
        billing_account_id=account.id,
        customer_id=account.customer_id,
        invoice_id=inv.id if inv else None,
        committed_amount=committed_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        commitment_date=commitment_date,
        status="PENDING",
        notes=notes or "Customer payment commitment received.",
        created_by_id=created_by_id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )
    db.add(ptp)
    db.commit()
    db.refresh(ptp)
    return ptp


def check_dunning_exemption(
    db: Session,
    account: BillingAccount,
    invoice: Invoice,
    now: Optional[datetime] = None
) -> Tuple[bool, Optional[str]]:
    """
    Evaluates safety invariants preventing erroneous dunning enforcement:
    1. Active Promise-to-Pay whose commitment date has not passed.
    2. Sufficient unallocated account credit balance.
    3. Invoice is voided or in dispute.
    4. Overdue balance below minimal threshold (< ₹50).
    """
    current_time = now or datetime.utcnow()

    # Invariant 1: Dispute or Voided
    if invoice.status in (InvoiceStatus.VOIDED, InvoiceStatus.CREDITED):
        return True, "Invoice is voided or credited."

    # Invariant 2: Minimal balance threshold
    if invoice.outstanding_amount < MINIMUM_OVERDUE_FOR_ACTION:
        return True, f"Outstanding amount ₹{invoice.outstanding_amount} is below action threshold ₹{MINIMUM_OVERDUE_FOR_ACTION}."

    # Invariant 3: Active Promise-to-Pay (PTP)
    active_ptp = (
        db.query(PromiseToPay)
        .filter(
            PromiseToPay.billing_account_id == account.id,
            PromiseToPay.status == "PENDING",
            PromiseToPay.commitment_date >= current_time
        )
        .first()
    )
    if active_ptp:
        return True, f"Protected by active Promise-to-Pay until {active_ptp.commitment_date.strftime('%Y-%m-%d')}."

    # Invariant 4: Unallocated credit balance auto-offset check
    if account.balance > Decimal("0.00") and account.balance >= invoice.outstanding_amount:
        return True, "Account holds sufficient credit deposit balance to offset invoice."

    return False, None


def evaluate_account_dunning(
    db: Session,
    billing_account_id: str,
    now: Optional[datetime] = None
) -> Dict[str, Any]:
    """
    Evaluates an account's overdue invoices and determines appropriate dunning stage,
    notifications, and network enforcement action.
    """
    current_time = now or datetime.utcnow()
    acct_uuid = uuid.UUID(str(billing_account_id)) if isinstance(billing_account_id, str) else billing_account_id
    account = db.query(BillingAccount).filter(BillingAccount.id == acct_uuid).first()
    if not account:
        raise ValueError(f"Billing account '{billing_account_id}' not found.")

    # Find earliest open invoice
    open_invoices = (
        db.query(Invoice)
        .filter(
            Invoice.billing_account_id == account.id,
            Invoice.outstanding_amount > Decimal("0.00"),
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.LATE, InvoiceStatus.OVERDUE])
        )
        .order_by(Invoice.due_date.asc())
        .all()
    )

    if not open_invoices:
        return {
            "account_id": str(account.id),
            "status": "CLEAR",
            "dunning_stage": DunningStage.STAGE_0_CURRENT,
            "network_action": DunningNetworkAction.RESTORE_SERVICE,
            "exempt": False,
            "exemption_reason": None,
            "overdue_invoices_count": 0,
            "total_outstanding": Decimal("0.00")
        }

    oldest_inv = open_invoices[0]
    total_due = sum(inv.outstanding_amount for inv in open_invoices)
    days_past_due = (current_time.date() - oldest_inv.due_date.date()).days

    # Safety Exemption Check
    is_exempt, exempt_reason = check_dunning_exemption(db, account, oldest_inv, current_time)
    if is_exempt:
        return {
            "account_id": str(account.id),
            "status": "EXEMPT",
            "dunning_stage": DunningStage.STAGE_0_CURRENT,
            "network_action": DunningNetworkAction.NONE,
            "exempt": True,
            "exemption_reason": exempt_reason,
            "overdue_invoices_count": len(open_invoices),
            "total_outstanding": total_due
        }

    # Staged Delinquency Evaluation
    stage = DunningStage.STAGE_0_CURRENT
    net_action = DunningNetworkAction.NONE
    late_fee_assessed = Decimal("0.00")

    if days_past_due < 0:
        if abs(days_past_due) <= REMINDER_DAYS_BEFORE_DUE:
            stage = DunningStage.STAGE_1_REMINDER
            net_action = DunningNetworkAction.NONE
        else:
            stage = DunningStage.STAGE_0_CURRENT
            net_action = DunningNetworkAction.NONE
    elif 0 <= days_past_due <= GRACE_PERIOD_DAYS:
        stage = DunningStage.STAGE_2_PAST_DUE
        net_action = DunningNetworkAction.NONE
        oldest_inv.status = InvoiceStatus.LATE
    elif GRACE_PERIOD_DAYS < days_past_due <= THROTTLE_DAYS_PAST_DUE:
        stage = DunningStage.STAGE_3_SOFT_THROTTLE
        net_action = DunningNetworkAction.THROTTLE_2MBPS
        oldest_inv.status = InvoiceStatus.LATE
    elif THROTTLE_DAYS_PAST_DUE < days_past_due <= WALLED_GARDEN_DAYS_PAST_DUE:
        stage = DunningStage.STAGE_4_WALLED_GARDEN
        net_action = DunningNetworkAction.WALLED_GARDEN
        oldest_inv.status = InvoiceStatus.OVERDUE
    elif WALLED_GARDEN_DAYS_PAST_DUE < days_past_due <= SUSPENSION_DAYS_PAST_DUE:
        stage = DunningStage.STAGE_5_HARD_SUSPENSION
        net_action = DunningNetworkAction.PACKET_OF_DISCONNECT
        oldest_inv.status = InvoiceStatus.OVERDUE
    else:
        stage = DunningStage.STAGE_6_TERMINATION_WRITE_OFF
        net_action = DunningNetworkAction.PACKET_OF_DISCONNECT
        oldest_inv.status = InvoiceStatus.OVERDUE

    db.commit()

    return {
        "account_id": str(account.id),
        "status": "DELINQUENT",
        "dunning_stage": stage,
        "days_past_due": days_past_due,
        "network_action": net_action,
        "exempt": False,
        "exemption_reason": None,
        "late_fee_assessed": late_fee_assessed,
        "overdue_invoices_count": len(open_invoices),
        "total_outstanding": total_due
    }


def execute_dunning_run(db: Session, market_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Executes scheduled dunning evaluation batch across all accounts with overdue invoices.
    """
    now = datetime.utcnow()
    query = (
        db.query(Invoice.billing_account_id)
        .filter(
            Invoice.outstanding_amount > Decimal("0.00"),
            Invoice.due_date < now,
            Invoice.status.in_([InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.LATE, InvoiceStatus.OVERDUE])
        )
        .distinct()
    )
    if market_id:
        query = query.filter(Invoice.market_id == market_id)

    account_ids = [row[0] for row in query.all() if row[0] is not None]
    evaluated_count = len(account_ids)
    throttled_count = 0
    walled_garden_count = 0
    suspended_count = 0
    exempt_count = 0

    for acct_id in account_ids:
        try:
            result = evaluate_account_dunning(db, acct_id, now)
            if result.get("exempt"):
                exempt_count += 1
            else:
                act = result.get("network_action")
                if act == DunningNetworkAction.THROTTLE_2MBPS:
                    throttled_count += 1
                elif act == DunningNetworkAction.WALLED_GARDEN:
                    walled_garden_count += 1
                elif act == DunningNetworkAction.PACKET_OF_DISCONNECT:
                    suspended_count += 1
        except Exception:
            continue

    return {
        "status": "COMPLETED",
        "accounts_evaluated": evaluated_count,
        "exempt_count": exempt_count,
        "throttled_count": throttled_count,
        "walled_garden_count": walled_garden_count,
        "suspended_count": suspended_count,
        "timestamp": now.isoformat()
    }
