"""
app/models_provisioning.py

SentinelOS Service Provisioning & Automated Service Delivery Domain Models.
Implements carrier-grade activation requests, network device assignments,
state transitions, and audit records with strict separation between:
1. Customer Lifecycle Status (PROSPECT, PENDING_VERIFICATION, ACTIVE, etc.)
2. Subscription Status (PENDING, ACTIVE, SUSPENDED)
3. Provisioning Execution Status (REQUESTED, VALIDATING, APPROVED, PROVISIONING, ACTIVE, FAILED, CANCELLED)
"""

from datetime import datetime
from typing import Optional, Dict, Any
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, JSON, Index
)
from sqlalchemy.orm import relationship
from app.database import Base
from app.models_crm import GUID


# ─── Provisioning Domain Constants & States ──────────────────────────────────

class ProvisioningStatus:
    REQUESTED = "REQUESTED"
    VALIDATING = "VALIDATING"
    APPROVED = "APPROVED"
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    ALL = (REQUESTED, VALIDATING, APPROVED, PROVISIONING, ACTIVE, FAILED, CANCELLED)
    TERMINAL = (ACTIVE, CANCELLED)
    ACTIVE_OR_PENDING = (REQUESTED, VALIDATING, APPROVED, PROVISIONING)


class ExecutionMode:
    SIMULATED = "SIMULATED"
    REAL_VENDOR = "REAL_VENDOR"
    ALL = (SIMULATED, REAL_VENDOR)


# ─── Service Activation Request / Provisioning Order ─────────────────────────

class ServiceActivationOrder(Base):
    """
    Authoritative order record for broadband service activation, ONT allocation,
    and network profile synchronization.
    """
    __tablename__ = 'service_provisioning_orders'

    id = Column(Integer, primary_key=True, index=True)
    order_code = Column(String(50), unique=True, index=True, nullable=False)  # e.g. ACT-MUM-2026-0001
    idempotency_key = Column(String(100), unique=True, index=True, nullable=False)  # Client idempotency key
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)

    # Customer & CRM linkage
    customer_id = Column(Integer, ForeignKey('customers.id', ondelete='CASCADE'), index=True, nullable=False)
    crm_customer_id = Column(GUID(), ForeignKey('crm_customers.id', ondelete='SET NULL'), nullable=True, index=True)
    customer_name = Column(String(150), nullable=False)
    customer_code = Column(String(50), nullable=False)
    account_type = Column(String(50), default='RESIDENTIAL', nullable=False)

    # Physical premises & serviceability
    service_address = Column(String(255), nullable=True)
    locality = Column(String(100), index=True, nullable=False)
    serviceability_status = Column(String(50), default='SERVICEABLE', nullable=False)  # FEASIBLE, SERVICEABLE, PENDING_SURVEY, UNSERVICEABLE
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    # Tariff & plan specification
    plan_id = Column(String(50), nullable=False)  # e.g. PLAN-PRE-HERO-1.5G
    plan_name = Column(String(100), nullable=False)
    plan_price = Column(Float, nullable=False)
    speed_mbps = Column(Integer, default=100, nullable=False)
    data_quota_gb = Column(Float, default=100.0, nullable=False)
    validity_days = Column(Integer, default=28, nullable=False)

    # Network topology assignments
    assigned_device_id = Column(Integer, ForeignKey('network_devices.id', ondelete='SET NULL'), nullable=True, index=True)
    assigned_device_code = Column(String(50), nullable=True)  # e.g. ONT-MUM-0001
    assigned_port_id = Column(Integer, ForeignKey('olt_ports.id', ondelete='SET NULL'), nullable=True)
    olt_device_code = Column(String(50), nullable=True)  # e.g. OLT-MUM-01
    service_vlan = Column(Integer, nullable=True)  # 100 - 4094
    radius_username = Column(String(100), nullable=True)

    # Distinct Lifecycle & Execution State separation (Never collapsed into one field)
    status = Column(String(50), default=ProvisioningStatus.REQUESTED, index=True, nullable=False)
    customer_lifecycle_status = Column(String(50), default='ACTIVE', nullable=False)
    subscription_status = Column(String(50), default='PENDING', nullable=False)

    # Safe Execution Architecture
    execution_mode = Column(String(50), default=ExecutionMode.SIMULATED, nullable=False)  # SIMULATED vs REAL_VENDOR
    provider_name = Column(String(100), default='SyntheticProvisioningProvider', nullable=False)
    telemetry_source = Column(String(100), default='Synthetic / Demo Telemetry', nullable=False)
    failure_code = Column(String(100), nullable=True)
    failure_reason = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)
    max_retries = Column(Integer, default=3, nullable=False)

    # Actor identity & timestamps
    requested_by_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    requested_by_email = Column(String(150), nullable=True)
    approved_by_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    approved_by_email = Column(String(150), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    provisioned_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancellation_reason = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    customer = relationship('Customer')
    crm_customer = relationship('CRMCustomer')
    assigned_device = relationship('NetworkDevice', foreign_keys=[assigned_device_id])
    assigned_port = relationship('OLTPort', foreign_keys=[assigned_port_id])
    requested_by = relationship('User', foreign_keys=[requested_by_id])
    approved_by = relationship('User', foreign_keys=[approved_by_id])
    logs = relationship('ProvisioningLog', back_populates='order', cascade='all, delete-orphan')


# ─── Provisioning Audit & State Transition Log ────────────────────────────────

class ProvisioningLog(Base):
    """
    Immutable audit trail recording state transitions, actor identity,
    execution payloads, and sanitized failure reasons.
    """
    __tablename__ = 'provisioning_logs'

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey('service_provisioning_orders.id', ondelete='CASCADE'), index=True, nullable=False)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)

    from_status = Column(String(50), nullable=False)
    to_status = Column(String(50), nullable=False)
    action = Column(String(100), nullable=False)  # REQUEST, VALIDATE, APPROVE, PROVISION, RETRY, RECONCILE, CANCEL

    actor_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    actor_email = Column(String(150), nullable=True)
    actor_role = Column(String(50), nullable=True)

    message = Column(Text, nullable=False)
    execution_mode = Column(String(50), default=ExecutionMode.SIMULATED, nullable=False)
    details = Column(JSON, default=dict)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    # Relationships
    order = relationship('ServiceActivationOrder', back_populates='logs')
    actor = relationship('User')
