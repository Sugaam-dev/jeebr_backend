from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, ForeignKey, JSON, Text
)
from sqlalchemy.orm import relationship
from app.database import Base

class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False)  # Executive, NOC, Care, Revenue, Admin
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    reviews = relationship('Recommendation', back_populates='reviewer')
    audit_logs = relationship('AuditLog', back_populates='user')


class Node(Base):
    __tablename__ = 'nodes'

    id = Column(Integer, primary_key=True, index=True)
    node_code = Column(String(50), unique=True, index=True, nullable=False)
    node_name = Column(String(100), nullable=False)
    area = Column(String(100), index=True, nullable=False)  # Mumbai locality
    node_type = Column(String(50), nullable=False)  # OLT, ONT, Core Switch, FDH
    utilization_pct = Column(Float, default=0.0)
    packet_loss_pct = Column(Float, default=0.0)
    latency_ms = Column(Float, default=0.0)
    optical_power_dbm = Column(Float, default=-19.0)  # Normal ~ -18 to -24 dBm; degraded < -27 dBm
    alarm_count = Column(Integer, default=0)
    health_score = Column(Float, default=100.0)  # 0 to 100
    status = Column(String(50), default='Healthy')  # Healthy, Degraded, Critical, Maintenance
    last_telemetry_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    customers = relationship('Customer', back_populates='node')
    tickets = relationship('Ticket', back_populates='node')


class Customer(Base):
    __tablename__ = 'customers'

    id = Column(Integer, primary_key=True, index=True)
    customer_code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(150), nullable=False)
    email = Column(String(150), nullable=False)
    phone = Column(String(50), nullable=False)
    locality = Column(String(100), index=True, nullable=False)
    segment = Column(String(50), index=True, nullable=False)  # Prepaid - Daily Unlimited, Long-term Bundle, Postpaid Family, etc.
    customer_type = Column(String(50), index=True, default='Prepaid')  # Prepaid, Postpaid
    plan_name = Column(String(100), nullable=False)
    plan_price = Column(Float, nullable=False, default=299.0)  # Nominal pack/plan price in INR (₹)
    revenue_30d = Column(Float, nullable=False, default=295.0)  # Actual customer-level revenue generated over the last 30 days
    actual_arpu = Column(Float, nullable=False, default=295.0)  # Synchronized with revenue_30d
    arpu = Column(Float, nullable=False)  # Synchronized with revenue_30d for backwards compatibility
    recharge_validity_days = Column(Integer, default=28)
    days_to_expiry = Column(Integer, default=14)
    validity_status = Column(String(50), default='Active')  # Active, Expiring Soon, Grace Period, Expired
    daily_data_quota_gb = Column(Float, default=1.5)
    daily_data_used_gb = Column(Float, default=0.8)
    last_recharge_date = Column(DateTime, nullable=True)
    last_recharge_amount = Column(Float, nullable=True)
    payment_method = Column(String(50), default='UPI')
    tenure_months = Column(Integer, default=1)
    signup_date = Column(DateTime, nullable=False)
    status = Column(String(50), index=True, default='Active')  # Active, At-Risk, Churned
    node_id = Column(Integer, ForeignKey('nodes.id'), nullable=True)
    current_stage = Column(String(50), default='Use')  # Acquisition, Install, Use, Renewal, Complaint, Win-back
    nps_score = Column(Integer, default=8)  # 1 to 10
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    node = relationship('Node', back_populates='customers')
    usage_records = relationship('UsageRecord', back_populates='customer', cascade='all, delete-orphan')
    tickets = relationship('Ticket', back_populates='customer', cascade='all, delete-orphan')
    invoices = relationship('Invoice', back_populates='customer', cascade='all, delete-orphan')


class UsageRecord(Base):
    __tablename__ = 'usage_records'

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False)
    monthly_gb = Column(Float, default=0.0)
    quota_gb = Column(Float, default=500.0)
    usage_trend = Column(String(50), default='Stable')  # Declining, Stable, Growing
    trend_pct = Column(Float, default=0.0)  # e.g. -35.5% or +12.0%
    ott_streaming_flag = Column(Boolean, default=True)
    gaming_flag = Column(Boolean, default=False)
    last_active_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship('Customer', back_populates='usage_records')


class Ticket(Base):
    __tablename__ = 'tickets'

    id = Column(Integer, primary_key=True, index=True)
    ticket_code = Column(String(50), unique=True, index=True, nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False)
    node_id = Column(Integer, ForeignKey('nodes.id'), nullable=True)
    category = Column(String(50), nullable=False)  # Outage, Speed, Billing, Install, Hardware
    priority = Column(String(50), default='Medium')  # Low, Medium, High, Critical
    status = Column(String(50), default='Open')  # Open, In-Progress, Resolved, Closed
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)
    repeat_flag = Column(Boolean, default=False)
    description = Column(Text, nullable=False)
    ai_triage_action = Column(String(100), nullable=True)
    sla_deadline = Column(DateTime, nullable=True)

    customer = relationship('Customer', back_populates='tickets')
    node = relationship('Node', back_populates='tickets')


class Invoice(Base):
    __tablename__ = 'invoices'

    id = Column(Integer, primary_key=True, index=True)
    invoice_code = Column(String(50), unique=True, index=True, nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False)
    plan_name = Column(String(100), nullable=False)
    transaction_type = Column(String(50), default='Recharge')  # Recharge Pack, Data Booster, OTT Add-on, Postpaid Bill
    payment_method = Column(String(50), default='UPI')  # UPI (PhonePe/GPay), Net Banking, Card, Autopay
    billed_amount = Column(Float, nullable=False)
    expected_amount = Column(Float, nullable=False)
    due_date = Column(DateTime, nullable=False)
    paid_date = Column(DateTime, nullable=True)
    status = Column(String(50), default='Paid')  # Paid, Late, Failed, Unpaid
    waiver_amount = Column(Float, default=0.0)
    waiver_reason = Column(String(200), nullable=True)
    renewal_date = Column(DateTime, nullable=True)
    anomaly_flag = Column(Boolean, default=False)
    anomaly_type = Column(String(100), nullable=True)  # Plan Mismatch, Duplicate Credit, Unbilled Usage, Dunning Failure
    leakage_amount = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship('Customer', back_populates='invoices')


class Recommendation(Base):
    __tablename__ = 'recommendations'

    id = Column(Integer, primary_key=True, index=True)
    source_module = Column(String(100), nullable=False)  # Predictive Service Assurance, Churn Prediction, Intelligent Journeys, OSS/BSS Orchestration, Revenue Assurance
    target_entity_type = Column(String(50), nullable=False)  # Node, Customer, Ticket, Invoice
    target_entity_id = Column(Integer, nullable=False)
    target_entity_label = Column(String(200), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    recommended_action = Column(String(200), nullable=False)
    action_payload = Column(JSON, default=dict)
    confidence_score = Column(Float, nullable=False)  # 0.0 to 1.0
    status = Column(String(50), default='PENDING')  # PENDING, APPROVED, REJECTED, EXECUTED
    created_at = Column(DateTime, default=datetime.utcnow)
    reviewed_by_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_notes = Column(Text, nullable=True)

    reviewer = relationship('User', back_populates='reviews')
    audit_logs = relationship('AuditLog', back_populates='recommendation')


class AuditLog(Base):
    __tablename__ = 'audit_logs'

    id = Column(Integer, primary_key=True, index=True)
    recommendation_id = Column(Integer, ForeignKey('recommendations.id'), nullable=True)
    source_module = Column(String(100), nullable=False)
    action_taken = Column(String(200), nullable=False)
    decision = Column(String(50), nullable=False)  # APPROVED, REJECTED, EXECUTED
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    user_name = Column(String(150), nullable=False)
    user_role = Column(String(50), nullable=False)
    confidence_score = Column(Float, nullable=False)
    original_signals = Column(JSON, default=dict)
    execution_result = Column(JSON, default=dict)
    timestamp = Column(DateTime, default=datetime.utcnow)

    recommendation = relationship('Recommendation', back_populates='audit_logs')
    user = relationship('User', back_populates='audit_logs')
