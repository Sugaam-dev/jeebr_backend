"""
app/services/proration_service.py

SentinelOS High-Precision Telecom Proration & Mid-Cycle Adjustment Engine.
Calculates exact-day proration for:
1. Mid-cycle service activations.
2. Mid-cycle tariff upgrades & downgrades.
3. Mid-cycle cancellations and service disconnects.
4. Temporary vacation holds / service suspensions.

Enforces:
- Fixed-precision Decimal arithmetic (Decimal('0.01') with ROUND_HALF_UP).
- Leap year and variable month length awareness (28, 29, 30, 31 days).
"""

from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional


@dataclass
class ProrationResult:
    total_period_days: int
    active_days: int
    daily_rate: Decimal
    base_charge: Decimal
    prorated_charge: Decimal
    effective_start: datetime
    effective_end: datetime


@dataclass
class PlanChangeAdjustmentResult:
    total_period_days: int
    elapsed_days: int
    remaining_days: int
    old_plan_credit: Decimal
    new_plan_debit: Decimal
    net_adjustment_amount: Decimal  # Positive = customer owes; Negative = credit due to customer
    change_effective_date: datetime


def calculate_daily_rate(base_amount: Decimal, total_days: int) -> Decimal:
    """Computes daily rate with 4-decimal intermediate precision."""
    if total_days <= 0:
        return Decimal("0.0000")
    return (base_amount / Decimal(str(total_days))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def calculate_activation_proration(
    recurring_price: Decimal,
    activation_date: datetime,
    cycle_start: datetime,
    cycle_end: datetime
) -> ProrationResult:
    """
    Computes prorated charge for mid-cycle customer onboarding:
    From activation_date until cycle_end.
    """
    act_d = activation_date.date() if isinstance(activation_date, datetime) else activation_date
    start_d = cycle_start.date() if isinstance(cycle_start, datetime) else cycle_start
    end_d = cycle_end.date() if isinstance(cycle_end, datetime) else cycle_end

    total_days = max(1, (end_d - start_d).days)
    
    # Active days = from activation date to end of cycle (inclusive of activation day)
    active_days = max(0, min(total_days, (end_d - act_d).days))
    if act_d <= start_d:
        active_days = total_days

    daily_rate = calculate_daily_rate(recurring_price, total_days)
    prorated = (daily_rate * Decimal(str(active_days))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return ProrationResult(
        total_period_days=total_days,
        active_days=active_days,
        daily_rate=daily_rate.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        base_charge=recurring_price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        prorated_charge=prorated,
        effective_start=activation_date,
        effective_end=cycle_end
    )


def calculate_plan_change_adjustment(
    old_plan_price: Decimal,
    new_plan_price: Decimal,
    change_date: datetime,
    cycle_start: datetime,
    cycle_end: datetime
) -> PlanChangeAdjustmentResult:
    """
    Computes mid-cycle upgrade/downgrade adjustment:
    1. Credits remaining unused days of old plan.
    2. Debits prorated days of new plan for the same remaining window.
    3. Net adjustment = new_plan_debit - old_plan_credit.
    """
    chg_d = change_date.date() if isinstance(change_date, datetime) else change_date
    start_d = cycle_start.date() if isinstance(cycle_start, datetime) else cycle_start
    end_d = cycle_end.date() if isinstance(cycle_end, datetime) else cycle_end

    total_days = max(1, (end_d - start_d).days)
    elapsed_days = max(0, min(total_days, (chg_d - start_d).days))
    remaining_days = max(0, total_days - elapsed_days)

    old_daily = calculate_daily_rate(old_plan_price, total_days)
    new_daily = calculate_daily_rate(new_plan_price, total_days)

    old_credit = (old_daily * Decimal(str(remaining_days))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    new_debit = (new_daily * Decimal(str(remaining_days))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    net_adj = (new_debit - old_credit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return PlanChangeAdjustmentResult(
        total_period_days=total_days,
        elapsed_days=elapsed_days,
        remaining_days=remaining_days,
        old_plan_credit=old_credit,
        new_plan_debit=new_debit,
        net_adjustment_amount=net_adj,
        change_effective_date=change_date
    )


def calculate_cancellation_refund(
    plan_price: Decimal,
    cancellation_date: datetime,
    cycle_start: datetime,
    cycle_end: datetime
) -> Decimal:
    """
    Computes refundable credit for early subscription cancellation before period end.
    """
    canc_d = cancellation_date.date() if isinstance(cancellation_date, datetime) else cancellation_date
    start_d = cycle_start.date() if isinstance(cycle_start, datetime) else cycle_start
    end_d = cycle_end.date() if isinstance(cycle_end, datetime) else cycle_end

    total_days = max(1, (end_d - start_d).days)
    unused_days = max(0, (end_d - canc_d).days)
    daily_rate = calculate_daily_rate(plan_price, total_days)

    return (daily_rate * Decimal(str(unused_days))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_vacation_hold_adjustment(
    bandwidth_charge: Decimal,
    equipment_fee: Decimal,
    hold_start: datetime,
    hold_end: datetime,
    cycle_start: datetime,
    cycle_end: datetime
) -> Decimal:
    """
    Calculates billable amount during temporary service suspension:
    Bandwidth charges are credited/waived for the hold duration, but fixed equipment
    rental fees remain billable to hold the port and router lease.
    """
    hs = hold_start.date() if isinstance(hold_start, datetime) else hold_start
    he = hold_end.date() if isinstance(hold_end, datetime) else hold_end
    cs = cycle_start.date() if isinstance(cycle_start, datetime) else cycle_start
    ce = cycle_end.date() if isinstance(cycle_end, datetime) else cycle_end

    total_days = max(1, (ce - cs).days)
    hold_days = max(0, min(total_days, (min(he, ce) - max(hs, cs)).days))
    active_days = max(0, total_days - hold_days)

    bw_daily = calculate_daily_rate(bandwidth_charge, total_days)
    active_bw_charge = (bw_daily * Decimal(str(active_days))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Full equipment fee is retained
    return (active_bw_charge + equipment_fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
