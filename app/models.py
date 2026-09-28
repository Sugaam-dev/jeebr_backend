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
    role = Column(String(50), nullable=False)  # Executive, NOC, Care, Revenue, Admin, Viewer
    phone = Column(String(50), nullable=True, default='+91 98200 12345')
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    reviews = relationship('Recommendation', back_populates='reviewer')
    audit_logs = relationship('AuditLog', back_populates='user')


class Node(Base):
    __tablename__ = 'nodes'

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
    node_code = Column(String(50), unique=True, index=True, nullable=False)
    node_name = Column(String(100), nullable=False)
    area = Column(String(100), index=True, nullable=False)  # Regional locality
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
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
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
    arpu = Column(Float, nullable=False, default=295.0)  # Synchronized with revenue_30d for backwards compatibility
    recharge_validity_days = Column(Integer, default=28)
    days_to_expiry = Column(Integer, default=14)
    validity_status = Column(String(50), default='Active')  # Active, Expiring Soon, Grace Period, Expired
    daily_data_quota_gb = Column(Float, default=1.5)
    daily_data_used_gb = Column(Float, default=0.8)
    last_recharge_date = Column(DateTime, nullable=True)
    last_recharge_amount = Column(Float, nullable=True)
    payment_method = Column(String(50), default='UPI')
    tenure_months = Column(Integer, default=1)
    signup_date = Column(DateTime, nullable=False, default=datetime.utcnow)
    status = Column(String(50), index=True, default='Active')  # Active, At-Risk, Churned
    node_id = Column(Integer, ForeignKey('nodes.id'), nullable=True)
    current_stage = Column(String(50), default='Use')  # Acquisition, Install, Use, Renewal, Complaint, Win-back
    nps_score = Column(Integer, default=8)  # 1 to 10
    service_latitude = Column(Float, nullable=True)
    service_longitude = Column(Float, nullable=True)
    service_address = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    node = relationship('Node', back_populates='customers')
    usage_records = relationship('UsageRecord', back_populates='customer', cascade='all, delete-orphan')
    tickets = relationship('Ticket', back_populates='customer', cascade='all, delete-orphan')
    invoices = relationship('Invoice', back_populates='customer', cascade='all, delete-orphan')


class UsageRecord(Base):
    __tablename__ = 'usage_records'

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=False)
    monthly_gb = Column(Float, default=0.0)
    quota_gb = Column(Float, default=500.0)
    usage_trend = Column(String(50), default='Stable')  # Declining, Stable, Growing
    trend_pct = Column(Float, default=0.0)  # e.g. -35.5% or +12.0%
    ott_streaming_flag = Column(Boolean, default=True)
    gaming_flag = Column(Boolean, default=False)
    last_active_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship('Customer', back_populates='usage_records')


class Resource(Base):
    __tablename__ = 'resources'

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
    name = Column(String(150), nullable=False)
    email = Column(String(150), nullable=True)
    phone = Column(String(50), nullable=True)
    resource_type = Column(String(50), default='FIELD', nullable=False)  # FIELD or INTERNAL
    region = Column(String(100), index=True, nullable=False)  # Locality/Area e.g. Bandra West, or "Internal NOC"
    status = Column(String(50), default='Available')  # Available, Busy, Offline
    active_tickets_count = Column(Integer, default=0)
    max_capacity = Column(Integer, default=5)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    current_latitude = Column(Float, nullable=True)
    current_longitude = Column(Float, nullable=True)
    last_ping_at = Column(DateTime, nullable=True)
    location_status = Column(String(50), default='UNAVAILABLE')  # ACTIVE, STALE, UNAVAILABLE
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    user = relationship('User', foreign_keys=[user_id])
    tickets = relationship('Ticket', back_populates='assigned_resource')
    field_assignments = relationship('FieldAssignment', back_populates='engineer', cascade='all, delete-orphan')
    tracking_sessions = relationship('TrackingSession', back_populates='engineer', cascade='all, delete-orphan')


class Ticket(Base):
    __tablename__ = 'tickets'

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
    ticket_code = Column(String(50), unique=True, index=True, nullable=False)
    source = Column(String(50), default='CUSTOMER', nullable=False)  # CUSTOMER or INTERNAL
    region = Column(String(100), index=True, nullable=True)  # Regional locality
    customer_id = Column(Integer, ForeignKey('customers.id'), nullable=True)
    node_id = Column(Integer, ForeignKey('nodes.id'), nullable=True)
    category = Column(String(50), nullable=False)  # Outage, Speed, Billing, Install, Hardware, Core Network
    priority = Column(String(50), default='P3')  # P1, P2, P3, P4
    status = Column(String(50), default='Open')  # Open, Pending Approval, Assigned, In-Progress, Resolved, Closed, Rejected
    created_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)
    repeat_flag = Column(Boolean, default=False)
    description = Column(Text, nullable=False)
    ai_triage_action = Column(String(100), nullable=True)
    sla_deadline = Column(DateTime, nullable=True)

    # Assignment & Approval
    assigned_resource_id = Column(Integer, ForeignKey('resources.id'), nullable=True)
    assigned_at = Column(DateTime, nullable=True)
    approval_status = Column(String(50), default='NOT_REQUIRED')  # NOT_REQUIRED, PENDING_APPROVAL, APPROVED, REJECTED
    approved_by_id = Column(Integer, ForeignKey('users.id'), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    approval_notes = Column(Text, nullable=True)

    customer = relationship('Customer', back_populates='tickets')
    node = relationship('Node', back_populates='tickets')
    assigned_resource = relationship('Resource', back_populates='tickets')
    approved_by = relationship('User', foreign_keys=[approved_by_id])
    call_logs = relationship('ApprovalCallLog', back_populates='ticket', cascade='all, delete-orphan')
    field_assignment = relationship('FieldAssignment', back_populates='ticket', uselist=False, cascade='all, delete-orphan')


class Invoice(Base):
    __tablename__ = 'invoices'

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
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
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
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
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
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


class ApprovalCallLog(Base):
    __tablename__ = 'approval_call_logs'

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
    ticket_id = Column(Integer, ForeignKey('tickets.id', ondelete='CASCADE'), index=True, nullable=False)
    recipient_name = Column(String(150), nullable=False)
    recipient_phone = Column(String(50), nullable=False)
    recipient_role = Column(String(50), nullable=False)
    priority = Column(String(10), nullable=False)
    voice_script = Column(Text, nullable=False)
    status = Column(String(50), default='DELIVERED')  # INITIATED, RINGING, CONNECTED, DELIVERED, COMPLETED
    call_sid = Column(String(100), nullable=True)
    duration_seconds = Column(Integer, default=32)
    created_at = Column(DateTime, default=datetime.utcnow)

    ticket = relationship('Ticket', back_populates='call_logs')


class FieldAssignment(Base):
    __tablename__ = 'field_assignments'

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
    ticket_id = Column(Integer, ForeignKey('tickets.id', ondelete='CASCADE'), unique=True, index=True, nullable=False)
    engineer_id = Column(Integer, ForeignKey('resources.id', ondelete='CASCADE'), index=True, nullable=False)
    status = Column(String(50), default='ASSIGNED', index=True, nullable=False)
    # Lifecycle states: ASSIGNED, ACCEPTED, EN_ROUTE, ARRIVED, WORKING, OTP_REQUESTED, OTP_VERIFIED, COMPLETED, REJECTED, CANCELLED, ON_HOLD, FAILED, REASSIGNED
    assigned_at = Column(DateTime, default=datetime.utcnow)
    accepted_at = Column(DateTime, nullable=True)
    en_route_at = Column(DateTime, nullable=True)
    arrived_at = Column(DateTime, nullable=True)
    work_started_at = Column(DateTime, nullable=True)
    otp_requested_at = Column(DateTime, nullable=True)
    otp_verified_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    ticket = relationship('Ticket', back_populates='field_assignment')
    engineer = relationship('Resource', back_populates='field_assignments')
    tracking_sessions = relationship('TrackingSession', back_populates='field_assignment', cascade='all, delete-orphan')
    otps = relationship('FieldOtp', back_populates='field_assignment', cascade='all, delete-orphan')


class TrackingSession(Base):
    __tablename__ = 'tracking_sessions'

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
    field_assignment_id = Column(Integer, ForeignKey('field_assignments.id', ondelete='CASCADE'), index=True, nullable=False)
    engineer_id = Column(Integer, ForeignKey('resources.id', ondelete='CASCADE'), index=True, nullable=False)
    status = Column(String(50), default='ACTIVE', index=True, nullable=False)  # ACTIVE, STOPPED, PAUSED
    started_at = Column(DateTime, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)
    last_location_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    field_assignment = relationship('FieldAssignment', back_populates='tracking_sessions')
    engineer = relationship('Resource', back_populates='tracking_sessions')
    pings = relationship('LocationPing', back_populates='tracking_session', cascade='all, delete-orphan')


class LocationPing(Base):
    __tablename__ = 'location_pings'

    id = Column(Integer, primary_key=True, index=True)
    tracking_session_id = Column(Integer, ForeignKey('tracking_sessions.id', ondelete='CASCADE'), index=True, nullable=False)
    engineer_id = Column(Integer, ForeignKey('resources.id', ondelete='CASCADE'), index=True, nullable=False)
    market_id = Column(String(50), default='mumbai', index=True, nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    accuracy = Column(Float, default=10.0)  # accuracy radius in meters
    speed = Column(Float, default=0.0)  # speed in km/h or m/s
    heading = Column(Float, default=0.0)  # degrees 0-360
    recorded_at = Column(DateTime, default=datetime.utcnow, index=True)
    received_at = Column(DateTime, default=datetime.utcnow)
    is_mock = Column(Boolean, default=False, nullable=False, index=True)

    # Relationships
    tracking_session = relationship('TrackingSession', back_populates='pings')
    engineer = relationship('Resource')


class FieldOtp(Base):
    __tablename__ = 'field_otps'

    id = Column(Integer, primary_key=True, index=True)
    field_assignment_id = Column(Integer, ForeignKey('field_assignments.id', ondelete='CASCADE'), index=True, nullable=False)
    ticket_id = Column(Integer, ForeignKey('tickets.id', ondelete='CASCADE'), index=True, nullable=False)
    customer_id = Column(Integer, ForeignKey('customers.id', ondelete='CASCADE'), index=True, nullable=False)
    otp_hash = Column(String(255), nullable=False)  # Salted PBKDF2/SHA-256 hash; RAW OTP NEVER STORED
    salt = Column(String(64), nullable=False)
    dispatch_code = Column(String(16), nullable=True)  # Secure dispatch token for customer self-service retrieval
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, default=0)
    is_used = Column(Boolean, default=False)
    is_locked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    verified_at = Column(DateTime, nullable=True)

    # Relationships
    field_assignment = relationship('FieldAssignment', back_populates='otps')
    ticket = relationship('Ticket')
    customer = relationship('Customer')


class Role(Base):
    __tablename__ = 'roles'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), unique=True, index=True, nullable=False)
    display_name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    rank = Column(Integer, default=50, nullable=False)  # 100: Super Admin, 80: Admin, 50: Operational, 20: Customer, 10: Viewer
    is_system = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    permissions = relationship('RolePermission', back_populates='role', cascade='all, delete-orphan')


class Permission(Base):
    __tablename__ = 'permissions'

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(100), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=False)
    category = Column(String(50), nullable=False)
    description = Column(Text, nullable=True)

    # Relationships
    role_permissions = relationship('RolePermission', back_populates='permission', cascade='all, delete-orphan')


class RolePermission(Base):
    __tablename__ = 'role_permissions'

    id = Column(Integer, primary_key=True, index=True)
    role_id = Column(Integer, ForeignKey('roles.id', ondelete='CASCADE'), index=True, nullable=False)
    permission_code = Column(String(100), ForeignKey('permissions.code', ondelete='CASCADE'), index=True, nullable=False)

    # Relationships
    role = relationship('Role', back_populates='permissions')
    permission = relationship('Permission', back_populates='role_permissions')

