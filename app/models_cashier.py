"""
app/models_cashier.py

SentinelOS Cashier Desk, Collection Shift & Daily Collection Register (DCR) Domain Models.
Governs cash collection counters, shift drawer balances, physical denomination counts,
and supervisor-audited daily collection closing.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey, Text, JSON, Numeric, Index, CheckConstraint
)
from sqlalchemy.orm import relationship
from app.database import Base
from app.models_crm import GUID


class ShiftStatus:
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    RECONCILED = "RECONCILED"
    DISCREPANCY = "DISCREPANCY"
    ALL = (OPEN, CLOSED, RECONCILED, DISCREPANCY)


class CashierShift(Base):
    """
    Cashier work shift tracking opening float, collected cash, denomination count, and closing balance.
    """
    __tablename__ = 'cashier_shifts'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    shift_code = Column(String(50), unique=True, index=True, nullable=False)  # e.g. SHF-MUM-2026-00001
    cashier_user_id = Column(Integer, ForeignKey('users.id', ondelete='RESTRICT'), index=True, nullable=False)
    supervisor_user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
    branch_name = Column(String(100), default='Main Branch', nullable=False)

    opening_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    closing_time = Column(DateTime, nullable=True)
    status = Column(String(32), default=ShiftStatus.OPEN, index=True, nullable=False)

    # Balances
    opening_float = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    total_cash_collected = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    total_upi_collected = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    total_refunds_paid = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    expected_cash_in_drawer = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    actual_cash_counted = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)
    cash_variance = Column(Numeric(12, 2), default=Decimal('0.00'), nullable=False)  # actual - expected

    # Denomination breakdown: {"500": 10, "200": 5, "100": 20, "50": 10}
    denominations_breakdown = Column(JSON, default=dict, nullable=False)
    closing_notes = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    cashier = relationship('User', foreign_keys=[cashier_user_id])
    supervisor = relationship('User', foreign_keys=[supervisor_user_id])
    transactions = relationship('CashierShiftTransaction', back_populates='shift', cascade='all, delete-orphan')

    __table_args__ = (
        CheckConstraint(f"status IN {ShiftStatus.ALL}", name="ck_cashier_shift_status"),
        Index("ix_cashier_shift_cashier_status", "cashier_user_id", "status"),
    )


class CashierShiftTransaction(Base):
    """
    Individual cash/counter transaction logged within a cashier shift.
    """
    __tablename__ = 'cashier_shift_transactions'

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    shift_id = Column(GUID(), ForeignKey('cashier_shifts.id', ondelete='CASCADE'), index=True, nullable=False)
    receipt_code = Column(String(50), unique=True, index=True, nullable=False)  # e.g. RCP-MUM-2026-00001
    payment_id = Column(GUID(), ForeignKey('payments.id', ondelete='RESTRICT'), index=True, nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id', ondelete='RESTRICT'), index=True, nullable=False)

    amount = Column(Numeric(12, 2), nullable=False)
    payment_method = Column(String(32), default='CASH', nullable=False)  # CASH, UPI_COUNTER, CHEQUE
    transaction_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    payer_name = Column(String(150), nullable=False)
    notes = Column(String(255), nullable=True)

    # Relationships
    shift = relationship('CashierShift', back_populates='transactions')
    payment = relationship('Payment')
    customer = relationship('Customer')
