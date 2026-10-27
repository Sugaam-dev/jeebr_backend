"""
app/models_finance.py

SentinelOS Finance & Billing Domain Models.
Integrates with CRM Customer Master, Canonical Tariff Plans, Service Provisioning,
and Balanced Double-Entry General Ledger.

Enforces:
1. Fixed-precision Numeric/Decimal types for all monetary fields.
2. Explicit currency definitions (never assuming all customers share one currency).
3. Strict separation between Customer Lifecycle, Subscription Status, Billing Status, and Network Provisioning Status.
4. Concurrency-safe idempotency keys and unique references.
5. Balanced double-entry ledger invariants.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional, List
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Text, JSON, Index, Numeric, CheckConstraint, text
)
from sqlalchemy.orm import relationship
from app.database import Base
from app.models_crm import GUID


# ─── Finance Domain Constants & States ────────────────────────────────────────

class BillingAccountStatus:
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CLOSED = "CLOSED"
    ALL = (ACTIVE, SUSPENDED, CLOSED)


class BillingType:
    PREPAID = "PREPAID"
    POSTPAID = "POSTPAID"
    ALL = (PREPAID, POSTPAID)


class SubscriptionStatus:
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    ALL = (PENDING, ACTIVE, SUSPENDED, CANCELLED, EXPIRED)


class InvoiceStatus:
    DRAFT = "Draft"
    ISSUED = "Issued"
    PAID = "Paid"
    PARTIALLY_PAID = "Partially_Paid"
    LATE = "Late"
    OVERDUE = "Overdue"
    VOIDED = "Voided"
    CREDITED = "Credited"
    ALL = (DRAFT, ISSUED, PAID, PARTIALLY_PAID, LATE, OVERDUE, VOIDED, CREDITED)


class PaymentStatus:
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    PARTIALLY_ALLOCATED = "PARTIALLY_ALLOCATED"
    ALLOCATED = "ALLOCATED"
    ALL = (PENDING, COMPLETED, FAILED, REFUNDED, PARTIALLY_ALLOCATED, ALLOCATED)


class PaymentMethod:
    UPI = "UPI"
    NET_BANKING = "NET_BANKING"
    CARD = "CARD"
    CASH = "CASH"
    WALLET = "WALLET"
    AUTOPAY = "AUTOPAY"
    ALL = (UPI, NET_BANKING, CARD, CASH, WALLET, AUTOPAY)


class VoucherType:
    SPEED_BOOST = "SPEED_BOOST"
    DATA_TOPUP = "DATA_TOPUP"
    BILL_DISCOUNT = "BILL_DISCOUNT"
    OTT_PASS = "OTT_PASS"
    PERCENT_DISCOUNT = "PERCENT_DISCOUNT"
    ALL = (SPEED_BOOST, DATA_TOPUP, BILL_DISCOUNT, OTT_PASS, PERCENT_DISCOUNT)


class VoucherRedemptionStatus:
    PENDING = "PENDING"
    FULFILLED = "FULFILLED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    ALL = (PENDING, FULFILLED, FAILED, RECONCILIATION_REQUIRED)


class ChartOfAccounts:
    """Standardized Chart of Accounts for SentinelOS ISP Billing."""
    CASH_CLEARING = "1100"          # Asset: Cash & Gateway Clearing
    ACCOUNTS_RECEIVABLE = "1200"    # Asset: Customer Receivables
    CUSTOMER_DEPOSITS = "2100"      # Liability: Unallocated Prepaid / Overpayment Credits
    TAX_PAYABLE_GST = "2200"        # Liability: GST Output Tax Payable (CGST+SGST/IGST 18%)
    BROADBAND_REVENUE = "4100"      # Revenue: Billed Broadband Services
    VOUCHER_DISCOUNTS = "4200"      # Contra-Revenue / Expense: Promotional & Retention Concessions

    NAMES = {
        CASH_CLEARING: "Cash & Gateway Clearing",
        ACCOUNTS_RECEIVABLE: "Accounts Receivable",
        CUSTOMER_DEPOSITS: "Customer Credit Deposits",
        TAX_PAYABLE_GST: "GST Output Tax Payable",
        BROADBAND_REVENUE: "Broadband Service Revenue",
        VOUCHER_DISCOUNTS: "Voucher & Retention Discounts",
    }


# ─── 1. Customer Billing Account ──────────────────────────────────────────────

class BillingAccount(Base):
    """
    Authoritative financial account for an ISP subscriber.
    Linked to canonical CRMCustomer and legacy Customer.
    """
    __tablename__ = 'billing_accounts'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    account_number = Column(String(50), unique=True, index=True, nullable=False)  # e.g. BA-MUM-2026-0001
    customer_id = Column(Integer, ForeignKey('customers.id', ondelete='RESTRICT'), index=True, nullable=True)
    crm_customer_id = Column(GUID(), ForeignKey('crm_customers.id', ondelete='SET NULL'), index=True, nullable=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)

    billing_type = Column(String(32), default=BillingType.POSTPAID, nullable=False)  # PREPAID, POSTPAID
    currency = Column(String(3), default='INR', nullable=False)  # Explicit currency (ISO 4217)
    balance = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)  # Positive = prepaid/deposit credit
    credit_limit = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    status = Column(String(32), default=BillingAccountStatus.ACTIVE, index=True, nullable=False)
    payment_term_days = Column(Integer, default=15, nullable=False)
    auto_pay_enabled = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    customer = relationship('Customer')
    crm_customer = relationship('CRMCustomer')
    subscriptions = relationship('BillingSubscription', back_populates='billing_account', cascade='all, delete-orphan')
    invoices = relationship('Invoice', back_populates='billing_account')
    payments = relationship('Payment', back_populates='billing_account')
    credit_notes = relationship('CreditNote', back_populates='billing_account')

    __table_args__ = (
        CheckConstraint(f"billing_type IN {BillingType.ALL}", name="ck_billing_account_type"),
        CheckConstraint(f"status IN {BillingAccountStatus.ALL}", name="ck_billing_account_status"),
        Index("ix_billing_accounts_market_status", "market_id", "status"),
    )


# ─── 2. Service Subscriptions ─────────────────────────────────────────────────

class BillingSubscription(Base):
    """
    Effective-dated service subscription linking a customer's billing account
    to canonical telecom tariff plans (provisioning catalog).
    """
    __tablename__ = 'billing_subscriptions'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    subscription_code = Column(String(50), unique=True, index=True, nullable=False)  # e.g. SUB-MUM-2026-0001
    billing_account_id = Column(GUID(), ForeignKey('billing_accounts.id', ondelete='RESTRICT'), index=True, nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id', ondelete='RESTRICT'), index=True, nullable=False)
    crm_customer_id = Column(GUID(), ForeignKey('crm_customers.id', ondelete='SET NULL'), index=True, nullable=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)

    # Canonical tariff plan association
    plan_id = Column(String(50), index=True, nullable=False)  # e.g. PLAN-PRE-HERO-1.5G, PLAN-POST-INFINITY-75G
    plan_name = Column(String(100), nullable=False)
    billing_type = Column(String(32), default=BillingType.POSTPAID, nullable=False)
    billing_frequency = Column(String(32), default='MONTHLY', nullable=False)  # MONTHLY, QUARTERLY, ANNUAL, 28_DAYS, 56_DAYS, 84_DAYS, 365_DAYS
    unit_price = Column(Numeric(12, 2), nullable=False)  # Base catalog pack price in specified currency
    currency = Column(String(3), default='INR', nullable=False)

    # Distinct lifecycle state (Never collapsed with customer lifecycle or network provisioning state)
    status = Column(String(32), default=SubscriptionStatus.ACTIVE, index=True, nullable=False)

    # Effective dating
    start_date = Column(DateTime, default=datetime.utcnow, nullable=False)
    current_period_start = Column(DateTime, default=datetime.utcnow, nullable=False)
    current_period_end = Column(DateTime, nullable=False)
    next_billing_date = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancellation_reason = Column(Text, nullable=True)

    # Optional linkage to Service Activation Order
    provisioning_order_id = Column(Integer, ForeignKey('service_provisioning_orders.id', ondelete='SET NULL'), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    billing_account = relationship('BillingAccount', back_populates='subscriptions')
    customer = relationship('Customer')
    provisioning_order = relationship('ServiceActivationOrder', back_populates='billing_subscription')
    line_items = relationship('InvoiceLineItem', back_populates='subscription')

    __table_args__ = (
        CheckConstraint(f"status IN {SubscriptionStatus.ALL}", name="ck_billing_subscription_status"),
        CheckConstraint(f"billing_type IN {BillingType.ALL}", name="ck_billing_subscription_type"),
        Index("ix_billing_subscriptions_market_status", "market_id", "status"),
        Index(
            "ix_billing_subscriptions_provisioning_order_id",
            "provisioning_order_id",
            unique=True,
            sqlite_where=text("provisioning_order_id IS NOT NULL"),
            postgresql_where=text("provisioning_order_id IS NOT NULL")
        ),
    )


# ─── 3. Invoice Line Items ────────────────────────────────────────────────────

class InvoiceLineItem(Base):
    """
    Detailed, traceable charge item on an invoice referencing the originating
    subscription or one-time charge.
    """
    __tablename__ = 'invoice_line_items'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    invoice_id = Column(Integer, ForeignKey('invoices.id', ondelete='RESTRICT'), index=True, nullable=False)
    subscription_id = Column(GUID(), ForeignKey('billing_subscriptions.id', ondelete='SET NULL'), index=True, nullable=True)

    description = Column(String(255), nullable=False)
    quantity = Column(Numeric(10, 2), default=Decimal('1.00'), nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)  # quantity * unit_price
    tax_rate = Column(Numeric(5, 2), default=Decimal('18.00'), nullable=False)  # GST 18.00%
    tax_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    discount_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    total_amount = Column(Numeric(12, 2), nullable=False)  # amount - discount + tax
    currency = Column(String(3), default='INR', nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    invoice = relationship('Invoice', back_populates='line_items')
    subscription = relationship('BillingSubscription', back_populates='line_items')


# ─── 4. Payments ──────────────────────────────────────────────────────────────

class Payment(Base):
    """
    Authorized payment record linked to a customer's billing account.
    Supports provider simulation and future real gateway adapters.
    """
    __tablename__ = 'payments'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    payment_reference = Column(String(50), unique=True, index=True, nullable=False)  # e.g. PAY-MUM-2026-0001
    billing_account_id = Column(GUID(), ForeignKey('billing_accounts.id', ondelete='RESTRICT'), index=True, nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id', ondelete='RESTRICT'), index=True, nullable=False)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)

    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), default='INR', nullable=False)
    payment_method = Column(String(50), default=PaymentMethod.UPI, nullable=False)

    # Provider & Gateway Tracking
    provider_name = Column(String(100), default='SIMULATED_GATEWAY', nullable=False)
    provider_transaction_id = Column(String(100), unique=True, index=True, nullable=True)
    gateway_order_id = Column(String(100), unique=True, index=True, nullable=True)
    idempotency_key = Column(String(100), unique=True, index=True, nullable=False)
    status = Column(String(32), default=PaymentStatus.PENDING, index=True, nullable=False)

    # Allocation state
    allocated_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    unallocated_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)

    failure_reason = Column(Text, nullable=True)
    is_simulated = Column(Boolean, default=True, nullable=False)  # Explicit transparency
    payment_date = Column(DateTime, default=datetime.utcnow, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    billing_account = relationship('BillingAccount', back_populates='payments')
    customer = relationship('Customer')
    allocations = relationship('PaymentAllocation', back_populates='payment', cascade='all, delete-orphan')
    refunds = relationship('RefundRecord', back_populates='payment', cascade='all, delete-orphan')

    __table_args__ = (
        CheckConstraint(f"status IN {PaymentStatus.ALL}", name="ck_payment_status"),
        Index("ix_payments_market_status", "market_id", "status"),
    )


# ─── 5. Payment Allocations ───────────────────────────────────────────────────

class PaymentAllocation(Base):
    """
    Traceable allocation linking a Payment to one or more Invoices.
    """
    __tablename__ = 'payment_allocations'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    payment_id = Column(GUID(), ForeignKey('payments.id', ondelete='RESTRICT'), index=True, nullable=False)
    invoice_id = Column(Integer, ForeignKey('invoices.id', ondelete='RESTRICT'), index=True, nullable=False)

    allocated_amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), default='INR', nullable=False)
    allocated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    payment = relationship('Payment', back_populates='allocations')
    invoice = relationship('Invoice', back_populates='payment_allocations')


# ─── 6. Credit Notes ──────────────────────────────────────────────────────────

class CreditNote(Base):
    """
    Traceable credit adjustment correcting issued invoices or granting courtesy credit.
    """
    __tablename__ = 'credit_notes'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    credit_note_code = Column(String(50), unique=True, index=True, nullable=False)  # e.g. CN-MUM-2026-0001
    billing_account_id = Column(GUID(), ForeignKey('billing_accounts.id', ondelete='RESTRICT'), index=True, nullable=False)
    invoice_id = Column(Integer, ForeignKey('invoices.id', ondelete='SET NULL'), index=True, nullable=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)

    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), default='INR', nullable=False)
    reason = Column(String(255), nullable=False)
    status = Column(String(32), default='ISSUED', nullable=False)  # ISSUED, APPLIED, VOIDED

    created_by_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    billing_account = relationship('BillingAccount', back_populates='credit_notes')
    invoice = relationship('Invoice', back_populates='credit_notes')
    created_by = relationship('User')


# ─── 7. Refunds ───────────────────────────────────────────────────────────────

class RefundRecord(Base):
    """
    Auditable refund transaction linked to an authorized Payment.
    """
    __tablename__ = 'refund_records'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    refund_code = Column(String(50), unique=True, index=True, nullable=False)  # e.g. REF-MUM-2026-0001
    payment_id = Column(GUID(), ForeignKey('payments.id', ondelete='RESTRICT'), index=True, nullable=False)
    billing_account_id = Column(GUID(), ForeignKey('billing_accounts.id', ondelete='RESTRICT'), index=True, nullable=False)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)

    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), default='INR', nullable=False)
    reason = Column(String(255), nullable=False)
    status = Column(String(32), default='COMPLETED', nullable=False)  # PENDING, COMPLETED, FAILED
    provider_refund_id = Column(String(100), nullable=True)

    created_by_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    payment = relationship('Payment', back_populates='refunds')
    billing_account = relationship('BillingAccount')
    created_by = relationship('User')


# ─── 8. Balanced General Ledger: Journal Entries & Lines ──────────────────────

class JournalEntry(Base):
    """
    Immutable double-entry journal posting header.
    Every posted entry must satisfy: sum(debit) == sum(credit) per currency.
    """
    __tablename__ = 'journal_entries'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    entry_number = Column(String(50), unique=True, index=True, nullable=False)  # e.g. JRN-MUM-2026-0001
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)

    reference_type = Column(String(50), nullable=False)  # INVOICE, PAYMENT, ALLOCATION, CREDIT_NOTE, REFUND, VOUCHER_REDEMPTION
    reference_id = Column(String(100), index=True, nullable=False)  # e.g. INV-001, PAY-001, VCH-001
    description = Column(Text, nullable=False)
    posted_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    is_reversed = Column(Boolean, default=False, nullable=False)
    reversal_entry_id = Column(GUID(), ForeignKey('journal_entries.id', ondelete='SET NULL'), nullable=True)

    created_by_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    lines = relationship('JournalLine', back_populates='entry', cascade='all, delete-orphan')
    reversal_entry = relationship('JournalEntry', remote_side=[id])
    created_by = relationship('User')

    __table_args__ = (
        Index("ix_journal_entries_ref", "reference_type", "reference_id"),
        Index("ix_journal_entries_market", "market_id"),
    )


class JournalLine(Base):
    """
    Single balanced debit or credit line of a General Ledger Journal Entry.
    """
    __tablename__ = 'journal_lines'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    entry_id = Column(GUID(), ForeignKey('journal_entries.id', ondelete='CASCADE'), index=True, nullable=False)

    account_code = Column(String(20), index=True, nullable=False)  # e.g. "1100", "1200", "2100", "2200", "4100", "4200"
    account_name = Column(String(100), nullable=False)
    debit = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    credit = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    currency = Column(String(3), default='INR', nullable=False)

    # Relationships
    entry = relationship('JournalEntry', back_populates='lines')


# ─── 9. CRM Voucher Redemption & Fulfillment ─────────────────────────────────

class VoucherRedemption(Base):
    """
    Immutable redemption & fulfillment record linking CRM promotional/retention vouchers
    to billing ledger credits or network provisioning benefits without shadow accounting.
    """
    __tablename__ = 'voucher_redemptions'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    voucher_code = Column(String(64), index=True, nullable=False)
    voucher_type = Column(String(50), nullable=False)  # SPEED_BOOST, DATA_TOPUP, BILL_DISCOUNT, OTT_PASS
    customer_id = Column(Integer, ForeignKey('customers.id', ondelete='RESTRICT'), index=True, nullable=False)
    crm_customer_id = Column(GUID(), ForeignKey('crm_customers.id', ondelete='SET NULL'), index=True, nullable=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)

    status = Column(String(32), default=VoucherRedemptionStatus.PENDING, index=True, nullable=False)
    benefit_details = Column(JSON, default=dict)
    financial_credit_amount = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)

    # Fulfillment linkages
    voucher_id = Column(GUID(), ForeignKey('vouchers.id', ondelete='SET NULL'), nullable=True, index=True)
    invoice_id = Column(Integer, ForeignKey('invoices.id', ondelete='SET NULL'), nullable=True)
    provisioning_order_id = Column(Integer, ForeignKey('service_provisioning_orders.id', ondelete='SET NULL'), nullable=True)

    idempotency_key = Column(String(100), unique=True, index=True, nullable=False)
    redeemed_by_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    redeemed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    failure_reason = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    customer = relationship('Customer')
    crm_customer = relationship('CRMCustomer')
    invoice = relationship('Invoice')
    provisioning_order = relationship('ServiceActivationOrder')
    redeemed_by = relationship('User')

    __table_args__ = (
        CheckConstraint(f"voucher_type IN {VoucherType.ALL}", name="ck_voucher_redemption_type"),
        CheckConstraint(f"status IN {VoucherRedemptionStatus.ALL}", name="ck_voucher_redemption_status"),
        Index("ix_voucher_redemptions_cust_code", "customer_id", "voucher_code"),
    )


# ─── 10. Gateway Webhook Deduplication ────────────────────────────────────────

class PaymentWebhookEvent(Base):
    """
    Durable deduplication and audit journal for incoming gateway callbacks/webhooks.
    Enforces unique event identity to guarantee idempotency under retries and network replays.
    """
    __tablename__ = 'payment_webhook_events'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    event_id = Column(String(100), unique=True, index=True, nullable=False)  # e.g. evt_xxxx from Razorpay
    provider = Column(String(50), default='razorpay', nullable=False)
    event_type = Column(String(100), nullable=False)  # payment.captured, payment.failed, refund.processed
    payment_id = Column(GUID(), ForeignKey('payments.id', ondelete='SET NULL'), nullable=True)
    order_id = Column(String(100), index=True, nullable=True)
    payload = Column(JSON, nullable=False)
    status = Column(String(32), default='PROCESSED', nullable=False)  # PROCESSED, IGNORED, FAILED
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_webhook_events_provider_type", "provider", "event_type"),
    )


# ─── 11. Automated Billing & Dunning Job Execution Records ────────────────────

class BillingJobRun(Base):
    """
    Durable execution history for scheduled recurring billing and dunning cycles.
    Provides operational visibility, attempt tracking, and restart recovery.
    """
    __tablename__ = 'billing_job_runs'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    job_type = Column(String(50), index=True, nullable=False)  # RECURRING_BILLING, OVERDUE_DUNNING
    market_id = Column(String(50), nullable=True, index=True)
    status = Column(String(32), default='RUNNING', index=True, nullable=False)  # RUNNING, COMPLETED, FAILED, RETRIED
    attempt_count = Column(Integer, default=1, nullable=False)
    start_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    finish_time = Column(DateTime, nullable=True)

    records_processed = Column(Integer, default=0, nullable=False)
    records_succeeded = Column(Integer, default=0, nullable=False)
    records_failed = Column(Integer, default=0, nullable=False)
    records_skipped = Column(Integer, default=0, nullable=False)

    error_details = Column(Text, nullable=True)
    summary_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_billing_job_runs_type_status", "job_type", "status"),
    )


# ─── 12. Durable Customer Notification Outbox ─────────────────────────────────

class NotificationOutbox(Base):
    """
    Durable transactional outbox for customer SMS, email, and app notifications.
    Decouples financial transaction execution from third-party provider availability.
    """
    __tablename__ = 'notification_outbox'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id', ondelete='SET NULL'), nullable=True, index=True)
    recipient = Column(String(100), nullable=False)  # Phone number or email
    channel = Column(String(32), default='SMS', nullable=False)  # SMS, EMAIL, WHATSAPP
    event_type = Column(String(50), nullable=False)  # OTP, PAYMENT_CONFIRMATION, OVERDUE_REMINDER, SUSPENSION_WARNING, RESTORATION
    message = Column(Text, nullable=False)

    status = Column(String(32), default='QUEUED', index=True, nullable=False)  # QUEUED, SENT, DELIVERED, FAILED, RETRYING
    attempt_count = Column(Integer, default=0, nullable=False)
    max_attempts = Column(Integer, default=3, nullable=False)
    last_error = Column(Text, nullable=True)
    idempotency_key = Column(String(100), unique=True, index=True, nullable=False)
    is_simulated = Column(Boolean, default=True, nullable=False)

    scheduled_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_notif_outbox_status_sched", "status", "scheduled_at"),
    )
