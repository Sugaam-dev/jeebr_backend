from decimal import Decimal
from datetime import datetime
from typing import Optional, List, Any, Dict
from pydantic import BaseModel, EmailStr, Field, model_validator

# Auth Schemas
class Token(BaseModel):
    access_token: str
    token_type: str = 'bearer'
    role: str
    user_name: str
    email: str
    rank: int = 10
    permissions: List[str] = []

class TokenData(BaseModel):
    email: Optional[str] = None
    role: Optional[str] = None

class LoginRequest(BaseModel):
    email: str
    password: str

class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    role: Optional[str] = "Viewer"

class UserResponse(BaseModel):
    id: int
    email: str
    full_name: str
    role: str
    is_active: bool = True
    rank: int = 10
    permissions: List[str] = []
    market_ids: List[str] = []
    primary_market: Optional[str] = None

    class Config:
        from_attributes = True

class AdminUserCreateRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    name: Optional[str] = None
    role: str = "Viewer"
    is_active: bool = True
    phone: Optional[str] = None
    region: Optional[str] = None
    market_id: Optional[str] = None
    market_ids: Optional[List[str]] = None
    primary_market: Optional[str] = None

    @model_validator(mode='after')
    def resolve_name(self):
        if not self.full_name and not self.name:
            raise ValueError("Full name is required.")
        if not self.full_name:
            self.full_name = self.name
        return self

class AdminUserUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    phone: Optional[str] = None
    region: Optional[str] = None
    market_id: Optional[str] = None
    market_ids: Optional[List[str]] = None
    primary_market: Optional[str] = None

class UserStatusUpdateRequest(BaseModel):
    is_active: bool

# RBAC Schemas
class PermissionResponse(BaseModel):
    id: Optional[int] = None
    code: str
    name: str
    category: str
    description: Optional[str] = None

    class Config:
        from_attributes = True

class RoleResponse(BaseModel):
    id: int
    name: str
    display_name: str
    description: Optional[str] = None
    rank: int
    is_system: bool
    permissions: List[str] = []

    class Config:
        from_attributes = True

class RoleCreateRequest(BaseModel):
    name: str
    display_name: str
    description: Optional[str] = None
    rank: Optional[int] = 50
    permissions: List[str] = []

class RoleUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[List[str]] = None

class UserRoleAssignRequest(BaseModel):
    role: str


# Standard Contributing Signal Schema used across all 4 scored modules
class ContributingSignal(BaseModel):
    signal: str
    value: str
    weight: str
    detail: str
    impact_type: str = 'negative'  # 'negative', 'positive', 'neutral'


# Node Telemetry & Predictive Service Assurance Schemas
class NodeResponse(BaseModel):
    id: int
    node_code: str
    node_name: str
    area: str
    node_type: str
    utilization_pct: float
    packet_loss_pct: float
    latency_ms: float
    optical_power_dbm: float
    alarm_count: int
    health_score: float
    status: str
    last_telemetry_at: datetime
    impacted_customers_count: Optional[int] = 0

    class Config:
        from_attributes = True

class NodeDegradationPrediction(BaseModel):
    node_id: int
    node_code: str
    node_name: str
    area: str
    node_type: str
    health_score: float
    degradation_risk_score: float  # 0 to 100
    risk_level: str  # Critical, High, Medium, Low
    confidence_score: float
    optical_power_dbm: float
    utilization_pct: float
    packet_loss_pct: float
    alarm_count: int
    impacted_customers_count: int
    impacted_corporate_count: int
    open_tickets_count: int
    contributing_signals: List[ContributingSignal]
    suggested_action: str
    suggested_eta: str
    has_pending_recommendation: bool = False

class RecommendAssuranceRequest(BaseModel):
    node_id: int
    action_type: Optional[str] = None
    custom_notes: Optional[str] = None


# Customer & Churn Prediction Schemas
class UsageSummary(BaseModel):
    monthly_gb: float
    quota_gb: float
    usage_trend: str
    trend_pct: float
    ott_streaming_flag: bool
    gaming_flag: bool

class TicketSummary(BaseModel):
    id: int
    ticket_code: str
    category: str
    priority: str
    status: str
    created_at: datetime
    repeat_flag: bool
    description: str
    assigned_resource_name: Optional[str] = None
    region: Optional[str] = None

class InvoiceSummary(BaseModel):
    id: int
    invoice_code: str
    transaction_type: Optional[str] = "Recharge"
    payment_method: Optional[str] = "UPI"
    billed_amount: float
    due_date: datetime
    paid_date: Optional[datetime]
    status: str
    waiver_amount: float
    anomaly_flag: bool
    anomaly_type: Optional[str]

class CustomerListResponse(BaseModel):
    id: int
    market_id: Optional[str] = "mumbai"
    customer_code: str
    name: str
    email: str
    phone: str
    locality: str
    segment: str
    customer_type: str = "Prepaid"  # Prepaid (~70%), Postpaid (~30%)
    plan_name: str
    plan_price: float  # Nominal pack price or monthly rental
    revenue_30d: float = 295.0  # Actual customer-level revenue generated over the last 30 days
    actual_arpu: float  # Synchronized with revenue_30d
    arpu: float  # Synchronized with revenue_30d
    recharge_validity_days: Optional[int] = 28
    days_to_expiry: Optional[int] = 14
    validity_status: Optional[str] = "Active"
    daily_data_quota_gb: Optional[float] = 1.5
    daily_data_used_gb: Optional[float] = 0.8
    last_recharge_date: Optional[datetime] = None
    last_recharge_amount: Optional[float] = None
    payment_method: Optional[str] = "UPI"
    tenure_months: int
    status: str
    current_stage: str
    nps_score: int
    node_id: Optional[int]
    node_name: Optional[str] = None

    class Config:
        from_attributes = True

class ChurnCustomerPrediction(BaseModel):
    customer_id: int
    customer_code: str
    name: str
    locality: str
    segment: str
    customer_type: str = "Prepaid"
    plan_name: str
    plan_price: float = 299.0
    revenue_30d: float = 295.0
    actual_arpu: float = 295.0
    arpu: float
    recharge_validity_days: Optional[int] = 28
    days_to_expiry: Optional[int] = 14
    validity_status: Optional[str] = "Active"
    tenure_months: int
    churn_risk_score: float  # 0 to 100
    risk_level: str  # Critical, High, Medium, Low
    confidence_score: float
    top_factors: List[ContributingSignal]
    suggested_retention_action: str
    estimated_revenue_at_risk: float
    has_pending_recommendation: bool = False

class RecommendRetentionRequest(BaseModel):
    customer_id: int
    action_type: Optional[str] = None
    custom_notes: Optional[str] = None

class Customer360Response(BaseModel):
    customer: CustomerListResponse
    node: Optional[NodeResponse] = None
    usage: Optional[UsageSummary] = None
    recent_tickets: List[TicketSummary] = []
    recent_invoices: List[InvoiceSummary] = []
    churn_risk_score: float
    churn_risk_level: str
    churn_factors: List[ContributingSignal] = []
    next_best_action: Optional[Dict[str, Any]] = None
    active_recommendations: List[Dict[str, Any]] = []


# ============================================================================
# CRM Foundation & Completion Schemas (Phase 1 Stage 2 / CRM Completion Sprint)
# ============================================================================

class CRMContactBase(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=128)
    last_name: Optional[str] = Field(None, max_length=128)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=32)
    role: str = Field("PRIMARY", max_length=64)  # PRIMARY, BILLING, TECHNICAL, EMERGENCY
    is_primary: bool = False

class CRMContactCreate(CRMContactBase):
    pass

class CRMContactUpdate(BaseModel):
    first_name: Optional[str] = Field(None, min_length=1, max_length=128)
    last_name: Optional[str] = Field(None, max_length=128)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=32)
    role: Optional[str] = None
    is_primary: Optional[bool] = None
    is_archived: Optional[bool] = None

class CRMContactResponse(BaseModel):
    id: str
    crm_customer_id: str
    first_name: str
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    role: str
    is_primary: bool
    is_verified: bool
    is_archived: bool
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class CRMAddressBase(BaseModel):
    address_type: str = Field("INSTALLATION", max_length=32)  # INSTALLATION, BILLING, REGISTERED
    address_line1: str = Field(..., min_length=3, max_length=255)
    address_line2: Optional[str] = Field(None, max_length=255)
    city: str = Field(..., min_length=2, max_length=100)
    state: str = Field(..., min_length=2, max_length=100)
    postal_code: str = Field(..., min_length=3, max_length=20)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    serviceability_status: Optional[str] = "PENDING_SURVEY"

class CRMAddressCreate(CRMAddressBase):
    pass

class CRMAddressUpdate(BaseModel):
    address_type: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    serviceability_status: Optional[str] = None
    is_archived: Optional[bool] = None

class CRMAddressResponse(BaseModel):
    id: str
    crm_customer_id: str
    address_type: str
    address_line1: str
    address_line2: Optional[str] = None
    city: str
    state: str
    postal_code: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    serviceability_status: str
    candidate_node_id: Optional[int] = None
    distance_to_node_meters: Optional[float] = None
    survey_ticket_code: Optional[str] = None
    survey_ticket_status: Optional[str] = None
    is_archived: bool
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class CRMDocumentResponse(BaseModel):
    id: str
    crm_customer_id: str
    document_type: str
    document_identifier: Optional[str] = None
    file_name: str
    file_size_bytes: int
    mime_type: str
    upload_status: str
    verification_status: str
    verified_by_id: Optional[int] = None
    verified_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    is_archived: bool
    created_at: datetime
    updated_at: Optional[datetime] = None
    download_url: Optional[str] = None

    class Config:
        from_attributes = True

class CRMDocumentVerifyRequest(BaseModel):
    verification_status: str = Field(..., pattern="^(VERIFIED|REJECTED)$")
    rejection_reason: Optional[str] = None

class CRMSurveyCompleteRequest(BaseModel):
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    serviceability_status: Optional[str] = "SERVICEABLE"
    candidate_node_id: Optional[int] = None
    distance_to_node_meters: Optional[float] = None
    ticket_id: Optional[int] = None
    notes: Optional[str] = None

class CRMActivityResponse(BaseModel):
    id: str
    crm_customer_id: str
    activity_type: str
    summary: str
    details: Optional[str] = None
    performed_by_id: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True

class CRMCustomerCreate(BaseModel):
    customer_number: Optional[str] = Field(None, max_length=32)
    legal_name: str = Field(..., min_length=2, max_length=255)
    trade_name: Optional[str] = Field(None, max_length=255)
    account_type: str = Field("RESIDENTIAL", max_length=32)  # RESIDENTIAL, ENTERPRISE, SME
    status: str = Field("ACTIVE", max_length=32)  # PROSPECT, PENDING_VERIFICATION, ACTIVE, etc.
    market_id: Optional[str] = Field(None, max_length=32)
    email: EmailStr
    phone: str = Field(..., min_length=6, max_length=32)
    locality: Optional[str] = "Colaba"
    segment: Optional[str] = "Prepaid - Daily Unlimited"
    customer_type: Optional[str] = "Prepaid"
    plan_name: Optional[str] = "Unlimited 30D"
    plan_price: Optional[float] = 299.0
    tax_id: Optional[str] = Field(None, max_length=64)
    billing_currency: Optional[str] = "INR"
    primary_contact: Optional[CRMContactCreate] = None
    installation_address: Optional[CRMAddressCreate] = None

class CRMCustomerUpdate(BaseModel):
    legal_name: Optional[str] = Field(None, min_length=2, max_length=255)
    trade_name: Optional[str] = Field(None, max_length=255)
    account_type: Optional[str] = None
    status: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    locality: Optional[str] = None
    segment: Optional[str] = None
    customer_type: Optional[str] = None
    plan_name: Optional[str] = None
    plan_price: Optional[float] = None
    tax_id: Optional[str] = None
    expected_version: Optional[str] = None

class CRMCustomerDetailResponse(CustomerListResponse):
    crm_id: Optional[str] = None
    legal_name: Optional[str] = None
    trade_name: Optional[str] = None
    account_type: Optional[str] = None
    tax_id: Optional[str] = None
    billing_currency: Optional[str] = "INR"
    service_latitude: Optional[float] = None
    service_longitude: Optional[float] = None
    service_address: Optional[str] = None
    is_archived: bool = False
    contacts: List[CRMContactResponse] = []
    addresses: List[CRMAddressResponse] = []
    documents: List[CRMDocumentResponse] = []
    activities: List[CRMActivityResponse] = []

class PaginatedCustomersResponse(BaseModel):
    items: List[CustomerListResponse]
    total: int
    page: int
    page_size: int
    total_pages: int

# Leads
class CRMLeadCreate(BaseModel):
    legal_name: str = Field(..., min_length=2, max_length=255)
    trade_name: Optional[str] = Field(None, max_length=255)
    email: Optional[EmailStr] = None
    phone: Optional[str] = Field(None, max_length=32)
    market_id: Optional[str] = None
    account_type: str = Field("RESIDENTIAL", max_length=32)
    locality: Optional[str] = "Colaba"
    address_line1: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    notes: Optional[str] = None

class CRMLeadResponse(BaseModel):
    id: str
    customer_number: str
    legal_name: str
    trade_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    market_id: str
    account_type: str
    status: str
    is_converted: bool = False
    legacy_customer_id: Optional[int] = None
    created_at: datetime
    contacts: List[CRMContactResponse] = []
    addresses: List[CRMAddressResponse] = []
    activities: List[CRMActivityResponse] = []

class CRMLeadConvertRequest(BaseModel):
    plan_name: Optional[str] = "Fast 100M"
    plan_price: Optional[float] = 499.0
    customer_type: Optional[str] = "Prepaid"
    locality: Optional[str] = None
    segment: Optional[str] = "Residential - Standard"

# Customer Maps
class CustomerMapLocationItem(BaseModel):
    id: int
    crm_id: Optional[str] = None
    customer_code: str
    name: str
    customer_type: str
    status: str
    locality: str
    plan_name: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    address: Optional[str] = None
    has_coordinates: bool
    serviceability_status: Optional[str] = "SERVICEABLE"

class CustomerMapSummaryResponse(BaseModel):
    market_id: str
    total_customers: int
    with_coordinates_count: int
    missing_coordinates_count: int
    locations: List[CustomerMapLocationItem]

# Vouchers
class CRMVoucherResponse(BaseModel):
    id: str
    voucher_code: str
    customer_id: int
    customer_code: str
    customer_name: str
    title: str
    benefit: str
    type: str
    status: str
    source_module: str
    expiry_days: int
    provisioning_dependency: str = "OSS/BSS Billing & Provisioning Engine (Phase 2)"
    created_at: datetime


# Intelligent Journeys Schemas
class JourneyCustomerItem(BaseModel):
    customer_id: int
    customer_code: str
    name: str
    locality: str
    segment: str
    customer_type: str = "Prepaid"
    plan_name: str
    plan_price: float = 299.0
    revenue_30d: float = 295.0
    actual_arpu: float = 295.0
    current_stage: str
    tenure_months: int
    nps_score: int
    next_best_action: str
    action_reason: str
    suggested_channel: str
    confidence_score: float
    urgency_level: str = "Medium"  # Critical, High, Medium, Low
    contributing_signals: List[ContributingSignal] = []
    has_pending_recommendation: bool = False

class JourneyStageStat(BaseModel):
    stage: str
    count: int
    percentage: float
    avg_nps: float
    total_arpu: float
    health_status: str

class JourneyFunnelSummaryResponse(BaseModel):
    total_customers: int
    stages: List[JourneyStageStat]
    top_nba_channels: List[Dict[str, Any]]
    active_proposals_count: int

class RecommendJourneyRequest(BaseModel):
    customer_id: int
    action_type: Optional[str] = None


# AI-driven OSS/BSS Orchestration Schemas (Full Scored Engine)
class OrchestrationTicketItem(BaseModel):
    ticket_id: int
    ticket_code: str
    customer_id: Optional[int] = None
    customer_name: str
    customer_segment: str
    locality: str
    category: str
    priority: str
    status: str
    created_at: datetime
    repeat_flag: bool
    description: str
    triage_priority_score: float  # 0 to 100
    priority_level: str  # Critical, High, Medium, Low
    recommended_orchestration: str
    workflow_type: str  # Automated TR-069 Reboot, Field Splicing Dispatch, BRAS QoS Sync, Billing Credit SLA Adjustment
    confidence_score: float
    sla_deadline: Optional[datetime] = None
    sla_breach_risk: str  # Imminent, High, Nominal
    contributing_signals: List[ContributingSignal] = []
    has_pending_recommendation: bool = False

class RecommendOrchestrationRequest(BaseModel):
    ticket_id: int
    workflow_action: Optional[str] = None


# Revenue Assurance & Leakage Analytics Schemas (Full Scored Engine)
class RevenueLeakageItem(BaseModel):
    invoice_id: int
    invoice_code: str
    customer_id: int
    customer_name: str
    locality: str
    segment: str
    plan_name: str
    billed_amount: float
    expected_amount: float
    waiver_amount: float
    status: str
    anomaly_type: str
    leakage_amount: float
    leakage_risk_score: float  # 0 to 100
    risk_level: str  # Critical, High, Medium, Low
    confidence_score: float
    description: str
    recommended_action: str
    contributing_signals: List[ContributingSignal] = []
    has_pending_recommendation: bool = False

class RecommendRevenueRequest(BaseModel):
    invoice_id: int
    remediation_action: Optional[str] = None


# Governance & Audit Schemas
class RecommendationResponse(BaseModel):
    id: int
    recommendation_code: Optional[str] = None
    market_id: Optional[str] = "mumbai"
    source_module: str
    source_alarm_id: Optional[int] = None
    source_alarm_code: Optional[str] = None
    incident_type: Optional[str] = None
    target_entity_type: str
    target_entity_id: int
    target_entity_label: str
    title: str
    description: str
    recommended_action: str
    proposed_action: Optional[str] = None
    action_payload: Any = None
    confidence_score: float
    confidence_basis: Optional[str] = None
    risk_level: Optional[str] = "MEDIUM"
    evidence: Optional[Any] = None
    impact_assessment: Optional[Any] = None
    ai_provider: Optional[str] = "DETERMINISTIC_RULES_ENGINE_FALLBACK"
    is_ai_generated: Optional[bool] = False
    status: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    reviewed_by_id: Optional[int] = None
    reviewed_at: Optional[datetime] = None
    review_notes: Optional[str] = None
    rejection_reason: Optional[str] = None
    action_fingerprint: Optional[str] = None
    executed_by_id: Optional[int] = None
    executed_at: Optional[datetime] = None
    execution_status: Optional[str] = None
    execution_result: Optional[Any] = None
    execution_idempotency_key: Optional[str] = None
    retry_count: Optional[int] = 0

    class Config:
        from_attributes = True

class ApproveRejectRequest(BaseModel):
    recommendation_id: int
    notes: Optional[str] = None
    auto_execute: Optional[bool] = False

class ExecuteRecommendationRequest(BaseModel):
    recommendation_id: int
    idempotency_key: Optional[str] = None

class GenerateRecommendationRequest(BaseModel):
    incident_type: str  # FIBER_CUT, OPTICAL_DEGRADE, PEAK_TRAFFIC, TICKET_OUTAGE
    target_type: str    # LINK, DEVICE, PORT, TICKET
    target_id: str
    alarm_id: Optional[int] = None
    market_id: Optional[str] = None

class AuditLogResponse(BaseModel):
    id: int
    market_id: Optional[str] = "mumbai"
    recommendation_id: Optional[int]
    source_module: str
    action_taken: str
    decision: str
    user_id: Optional[int]
    user_name: str
    user_role: str
    confidence_score: float
    original_signals: Any = None
    execution_result: Any = None
    timestamp: datetime

    class Config:
        from_attributes = True


# Executive Cockpit Schemas
class CockpitKPISummary(BaseModel):
    total_active_customers: int
    total_at_risk_customers: int
    at_risk_monthly_revenue: float
    overall_arpu: float = 512.0  # Aggregate ARPU = Total 30D Revenue / Total Active Subscribers
    total_monthly_revenue: float = 512000.0  # Total recognized 30-day revenue across active subscriber base
    prepaid_subscribers_count: int = 700
    postpaid_subscribers_count: int = 300
    prepaid_revenue_30d: float = 206000.0
    postpaid_revenue_30d: float = 306000.0
    avg_prepaid_arpu: float = 295.0
    avg_postpaid_arpu: float = 1020.0
    open_degraded_nodes: int
    customers_impacted_by_degradation: int
    total_detected_leakage_inr: float
    open_tickets_count: int
    pending_governance_approvals: int
    approved_actions_today: int
    avg_approval_turnaround_mins: float

class ModuleHealthStatus(BaseModel):
    module_name: str
    status: str
    active_alerts: int
    confidence_avg: float

class CockpitSummaryResponse(BaseModel):
    kpis: CockpitKPISummary
    module_statuses: List[ModuleHealthStatus]
    locality_risk_distribution: List[Dict[str, Any]]
    churn_risk_distribution: List[Dict[str, Any]]
    leakage_by_category: List[Dict[str, Any]]
    recent_audit_events: List[AuditLogResponse]


# Pilot Bundle E2E Connected Trace Schemas
class PilotBundleTraceStep(BaseModel):
    step_number: int
    loop_phase: str  # Observe, Predict, Recommend, Approve, Execute, Learn
    module_name: str
    title: str
    subtitle: str
    status: str  # Triggered, Scored, Queued, Approved, Executed, Completed
    primary_metric: str
    primary_metric_label: str
    confidence_score: float
    description: str
    entity_label: str
    signals: List[Dict[str, Any]] = []
    actions_available: List[Dict[str, Any]] = []
    execution_receipt: Optional[Dict[str, Any]] = None

class PilotBundleScenarioResponse(BaseModel):
    scenario_id: str
    scenario_title: str
    scenario_summary: str
    node: NodeResponse
    impacted_customer: CustomerListResponse
    churn_prediction: ChurnCustomerPrediction
    journey_item: JourneyCustomerItem
    related_tickets: List[TicketSummary]
    related_recommendations: List[RecommendationResponse]
    related_audit_logs: List[AuditLogResponse]
    trace_steps: List[PilotBundleTraceStep]


# Resource & Automated Ticketing Schemas
class ResourceBase(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    resource_type: str = "FIELD"  # FIELD or INTERNAL
    region: str
    status: str = "Available"  # Available, Busy, Offline
    max_capacity: int = 5

class ResourceCreate(ResourceBase):
    market_id: str = "mumbai"

class ResourceResponse(ResourceBase):
    id: int
    market_id: str
    active_tickets_count: int = 0
    created_at: datetime

    class Config:
        from_attributes = True

class TicketCreateRequest(BaseModel):
    source: str = "CUSTOMER"  # CUSTOMER or INTERNAL
    category: str
    priority: str = "P3"  # P1, P2, P3, P4
    description: str
    region: Optional[str] = None  # Locality/Area
    customer_id: Optional[int] = None
    node_id: Optional[int] = None

class TicketApproveRequest(BaseModel):
    notes: Optional[str] = None
    resource_id: Optional[int] = None  # Manual technician assignment override

class TicketRejectRequest(BaseModel):
    notes: Optional[str] = None

class TicketResolveRequest(BaseModel):
    notes: Optional[str] = None

class ApprovalCallLogResponse(BaseModel):
    id: int
    market_id: str
    ticket_id: int
    recipient_name: str
    recipient_phone: str
    recipient_role: str
    priority: str
    voice_script: str
    status: str
    call_sid: Optional[str] = None
    duration_seconds: int
    created_at: datetime

    class Config:
        from_attributes = True

class TicketDetailResponse(BaseModel):
    id: int
    market_id: str
    ticket_code: str
    source: str
    region: Optional[str] = None
    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    node_id: Optional[int] = None
    node_code: Optional[str] = None
    category: str
    priority: str
    status: str
    created_at: datetime
    resolved_at: Optional[datetime] = None
    repeat_flag: bool = False
    description: str
    ai_triage_action: Optional[str] = None
    sla_deadline: Optional[datetime] = None

    assigned_resource_id: Optional[int] = None
    assigned_resource_name: Optional[str] = None
    assigned_resource_region: Optional[str] = None
    assigned_resource_type: Optional[str] = None
    assigned_at: Optional[datetime] = None

    approval_status: str
    approved_by_id: Optional[int] = None
    approved_by_name: Optional[str] = None
    approved_at: Optional[datetime] = None
    approval_notes: Optional[str] = None

    voice_call_dispatched: bool = False
    last_call_recipient: Optional[str] = None
    last_call_script: Optional[str] = None
    last_call_status: Optional[str] = None

    class Config:
        from_attributes = True

class TicketingStatsResponse(BaseModel):
    total_tickets: int
    auto_assigned_p3_p4: int
    pending_approval_p1_p2: int
    internal_auto_assigned: int
    resolved_tickets: int
    total_resources: int
    available_resources: int


class ResourceTimelineItem(BaseModel):
    ticket_id: int
    ticket_code: str
    category: str
    priority: str
    status: str
    customer_name: Optional[str] = None
    locality: Optional[str] = None
    description: str
    assigned_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    sla_deadline: Optional[datetime] = None
    minutes_to_sla: Optional[int] = None
    urgency_level: str  # 'Critical', 'Warning', 'Nominal', 'Overdue'


class ResourceTimelineResponse(BaseModel):
    resource: ResourceResponse
    active_tickets: List[ResourceTimelineItem]
    resolved_tickets: List[ResourceTimelineItem]
    total_active: int
    total_resolved: int
    urgent_count: int
    next_sla_deadline: Optional[datetime] = None


class AutoDispatchToggleRequest(BaseModel):
    enabled: bool


class AutoDispatchStatusResponse(BaseModel):
    enabled: bool
    unassigned_p3_p4_count: int
    market: str
    dispatched_count: Optional[int] = 0
    dispatched_ticket_codes: Optional[List[str]] = []


class SimulateAiAlertRequest(BaseModel):
    priority: Optional[str] = "P3"
    category: Optional[str] = "Optical Telemetry"
    description: Optional[str] = None
    region: Optional[str] = None


# --- Field Operations, Live Tracking & Customer OTP Schemas ---

class LocationPingCreate(BaseModel):
    latitude: float
    longitude: float
    accuracy: Optional[float] = 10.0
    speed: Optional[float] = 0.0
    heading: Optional[float] = 0.0
    battery_level: Optional[float] = None
    is_mock: Optional[bool] = False
    recorded_at: Optional[datetime] = None

    @model_validator(mode='before')
    @classmethod
    def map_aliases_and_prevalidate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Map legacy frontend alias keys if present and canonical not provided
            if 'accuracy_meters' in data and 'accuracy' not in data:
                data['accuracy'] = data['accuracy_meters']
            if 'speed_mps' in data and 'speed' not in data:
                data['speed'] = data['speed_mps']
            if 'heading_degrees' in data and 'heading' not in data:
                data['heading'] = data['heading_degrees']
        return data


class LocationPingResponse(BaseModel):
    id: int
    tracking_session_id: int
    engineer_id: int
    market_id: str
    latitude: float
    longitude: float
    accuracy: float
    speed: float
    heading: float
    is_mock: bool = False
    recorded_at: datetime
    received_at: datetime

    class Config:
        from_attributes = True


class FieldAssignmentResponse(BaseModel):
    id: int
    market_id: str
    ticket_id: int
    ticket_code: Optional[str] = None
    engineer_id: int
    engineer_name: Optional[str] = None
    engineer_phone: Optional[str] = None
    status: str
    assigned_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    en_route_at: Optional[datetime] = None
    arrived_at: Optional[datetime] = None
    work_started_at: Optional[datetime] = None
    otp_requested_at: Optional[datetime] = None
    otp_verified_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    notes: Optional[str] = None

    customer_id: Optional[int] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_locality: Optional[str] = None
    service_address: Optional[str] = None
    service_latitude: Optional[float] = None
    service_longitude: Optional[float] = None

    ticket_category: Optional[str] = None
    ticket_priority: Optional[str] = None
    ticket_description: Optional[str] = None
    sla_deadline: Optional[datetime] = None

    current_latitude: Optional[float] = None
    current_longitude: Optional[float] = None
    location_status: Optional[str] = "UNAVAILABLE"
    eta_minutes: Optional[int] = None

    class Config:
        from_attributes = True


class FieldAssignmentTransitionRequest(BaseModel):
    target_status: str
    notes: Optional[str] = None


class CompleteJobRequest(BaseModel):
    resolution_notes: Optional[str] = None
    customer_signature_confirmed: Optional[bool] = True
    notes: Optional[str] = None


class OtpRequestResponse(BaseModel):
    assignment_id: int
    id: Optional[int] = None
    status: str
    expires_in_seconds: int
    message: str

    @model_validator(mode="after")
    def set_id_alias(self):
        if self.id is None:
            self.id = self.assignment_id
        return self


class OtpVerifyRequest(BaseModel):
    otp: Optional[str] = None
    otp_code: Optional[str] = None

    def get_otp_code(self) -> str:
        return (self.otp or self.otp_code or "").strip()


class OtpVerifyResponse(BaseModel):
    verified: bool
    assignment_id: int
    id: Optional[int] = None
    status: str
    message: str
    attempts_remaining: Optional[int] = None

    @model_validator(mode="after")
    def set_id_alias(self):
        if self.id is None:
            self.id = self.assignment_id
        return self


class FieldOperationsSummaryResponse(BaseModel):
    market_id: str
    active_engineers: int
    en_route: int
    on_site: int
    available: int
    offline: int
    active_jobs: int
    sla_at_risk: int


class EngineerWorkloadResponse(BaseModel):
    id: int
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    market_id: str
    region: str
    status: str
    location_status: str
    current_latitude: Optional[float] = None
    current_longitude: Optional[float] = None
    last_ping_at: Optional[datetime] = None
    active_tickets_count: int
    max_capacity: int
    current_job_id: Optional[int] = None
    current_job_ticket: Optional[str] = None
    current_job_status: Optional[str] = None
    current_job_eta_minutes: Optional[int] = None

    # Workload & Capacity additions (Section 22)
    engineer_id: Optional[int] = None
    active_jobs: int = 0
    max_active_jobs: int = 10
    available_capacity: int = 0
    capacity_status: str = "AVAILABLE"

    class Config:
        from_attributes = True

class ConfigureCapacityRequest(BaseModel):
    max_capacity: int = Field(..., ge=1, le=50, description="Maximum active jobs capacity")



class OtpDetail(BaseModel):
    available: bool = False
    code: Optional[str] = None
    expires_at: Optional[str] = None
    remaining_seconds: Optional[int] = None


class RouteWaypoint(BaseModel):
    lat: float
    lng: float


class RouteResponse(BaseModel):
    distance_meters: float
    distance_km: float
    duration_seconds: int
    eta_minutes: int
    provider: str
    polyline: Optional[str] = None
    waypoints: List[RouteWaypoint] = []
    cached: bool = False


class CustomerTrackingResponse(BaseModel):
    ticket_id: int
    ticket_code: str
    status: str
    category: str
    priority: str
    assigned_engineer_name: Optional[str] = None
    assigned_engineer_phone: Optional[str] = None
    engineer_status: Optional[str] = None
    location_status: str
    engineer_latitude: Optional[float] = None
    engineer_longitude: Optional[float] = None
    service_latitude: Optional[float] = None
    service_longitude: Optional[float] = None
    service_address: Optional[str] = None
    eta_minutes: Optional[int] = None
    otp_code: Optional[str] = None
    otp_expires_in_seconds: Optional[int] = None
    notes: Optional[str] = None
    assignment_id: Optional[int] = None
    tracking_active: bool = False
    job_status: Optional[str] = None
    otp: Optional[OtpDetail] = None


# ─── Service Provisioning & Automated Service Delivery Schemas ────────────────

class ServicePlanItem(BaseModel):
    plan_id: str
    name: str
    account_type: str
    billing_type: str
    price_inr: float
    speed_mbps: int
    data_quota_gb: float
    daily_quota_gb: float
    validity_days: int
    segment: str
    service_vlan: int
    description: str


class ProvisioningOrderCreateRequest(BaseModel):
    customer_id: int
    plan_id: str
    idempotency_key: Optional[str] = None
    assigned_device_id: Optional[int] = None
    assigned_device_code: Optional[str] = None
    assigned_port_id: Optional[int] = None
    execution_mode: Optional[str] = "SIMULATED"


class ProvisioningOrderApproveRequest(BaseModel):
    notes: Optional[str] = None


class ProvisioningOrderCancelRequest(BaseModel):
    reason: str


class ProvisioningLogResponse(BaseModel):
    id: int
    order_id: int
    market_id: str
    from_status: str
    to_status: str
    action: str
    actor_email: Optional[str] = None
    actor_role: Optional[str] = None
    message: str
    execution_mode: str
    details: Optional[Dict[str, Any]] = None
    timestamp: datetime

    class Config:
        from_attributes = True


class ServiceActivationOrderResponse(BaseModel):
    id: int
    order_code: str
    idempotency_key: str
    market_id: str
    customer_id: int
    crm_customer_id: Optional[str] = None
    customer_name: str
    customer_code: str
    account_type: str
    service_address: Optional[str] = None
    locality: str
    serviceability_status: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    plan_id: str
    plan_name: str
    plan_price: float
    speed_mbps: int
    data_quota_gb: float
    validity_days: int
    assigned_device_id: Optional[int] = None
    assigned_device_code: Optional[str] = None
    assigned_port_id: Optional[int] = None
    olt_device_code: Optional[str] = None
    service_vlan: Optional[int] = None
    radius_username: Optional[str] = None
    status: str
    customer_lifecycle_status: str
    subscription_status: str
    execution_mode: str
    provider_name: str
    telemetry_source: str
    failure_code: Optional[str] = None
    failure_reason: Optional[str] = None
    retry_count: int
    max_retries: int
    requested_by_email: Optional[str] = None
    approved_by_email: Optional[str] = None
    approved_at: Optional[datetime] = None
    provisioned_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancellation_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    billing_account_number: Optional[str] = None
    billing_subscription_code: Optional[str] = None
    logs: Optional[List[ProvisioningLogResponse]] = []

    class Config:
        from_attributes = True


class CustomerEligibilityResponse(BaseModel):
    eligible: bool
    customer_id: int
    customer_code: str
    customer_name: str
    market_id: str
    locality: str
    plan: Optional[ServicePlanItem] = None
    suggested_device_id: Optional[int] = None
    suggested_device_code: Optional[str] = None
    suggested_port_id: Optional[int] = None
    olt_device_code: Optional[str] = None
    blocking_errors: List[str] = []
    prerequisites: List[str] = []
    reasons: List[str] = []


# ─── SentinelOS Finance & Billing Schemas ─────────────────────────────────────

class BillingAccountResponse(BaseModel):
    id: Any
    account_number: str
    customer_id: Optional[int] = None
    crm_customer_id: Optional[Any] = None
    customer_name: Optional[str] = None
    customer_code: Optional[str] = None
    market_id: str
    billing_type: str
    currency: str
    balance: float
    credit_limit: float
    status: str
    payment_term_days: int
    auto_pay_enabled: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class BillingAccountCreateRequest(BaseModel):
    customer_id: int
    billing_type: Optional[str] = "POSTPAID"
    currency: Optional[str] = "INR"
    credit_limit: Optional[float] = 0.0
    payment_term_days: Optional[int] = 15


class BillingSubscriptionResponse(BaseModel):
    id: Any
    subscription_code: str
    billing_account_id: Any
    customer_id: int
    crm_customer_id: Optional[Any] = None
    customer_name: Optional[str] = None
    market_id: str
    plan_id: str
    plan_name: str
    billing_type: str
    billing_frequency: str
    unit_price: float
    currency: str
    status: str
    start_date: datetime
    current_period_start: datetime
    current_period_end: datetime
    next_billing_date: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancellation_reason: Optional[str] = None
    provisioning_order_id: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class BillingSubscriptionCreateRequest(BaseModel):
    billing_account_id: Optional[str] = None
    customer_id: int
    plan_id: str
    billing_frequency: Optional[str] = "MONTHLY"
    start_date: Optional[datetime] = None
    provisioning_order_id: Optional[int] = None


class SubscriptionTransitionRequest(BaseModel):
    target_status: str  # ACTIVE, SUSPENDED, CANCELLED
    reason: Optional[str] = None


class InvoiceLineItemResponse(BaseModel):
    id: Any
    invoice_id: int
    subscription_id: Optional[Any] = None
    description: str
    quantity: float
    unit_price: float
    amount: float
    tax_rate: float
    tax_amount: float
    discount_amount: float
    total_amount: float
    currency: str

    class Config:
        from_attributes = True


class InvoiceDetailResponse(BaseModel):
    id: int
    market_id: str
    invoice_code: str
    customer_id: int
    customer_name: Optional[str] = None
    customer_code: Optional[str] = None
    billing_account_id: Optional[Any] = None
    subscription_id: Optional[Any] = None
    plan_name: str
    transaction_type: str
    payment_method: str
    billed_amount: float
    subtotal: float
    tax_amount: float
    discount_amount: float
    total_amount: float
    paid_amount: float
    outstanding_amount: float
    currency: str
    due_date: datetime
    paid_date: Optional[datetime] = None
    status: str
    billing_period_start: Optional[datetime] = None
    billing_period_end: Optional[datetime] = None
    idempotency_key: Optional[str] = None
    notes: Optional[str] = None
    created_at: datetime
    line_items: List[InvoiceLineItemResponse] = []

    class Config:
        from_attributes = True


class InvoiceCreateRequest(BaseModel):
    subscription_id: str
    billing_period_start: Optional[datetime] = None
    billing_period_end: Optional[datetime] = None
    discount_amount: Optional[float] = 0.0
    tax_rate: Optional[float] = 18.0
    notes: Optional[str] = None
    idempotency_key: Optional[str] = None


class PaymentAllocationResponse(BaseModel):
    id: Any
    payment_id: Any
    invoice_id: int
    invoice_code: Optional[str] = None
    allocated_amount: float
    currency: str
    allocated_at: datetime

    class Config:
        from_attributes = True


class PaymentResponse(BaseModel):
    id: Any
    payment_reference: str
    billing_account_id: Any
    customer_id: int
    customer_name: Optional[str] = None
    customer_code: Optional[str] = None
    market_id: str
    amount: float
    currency: str
    payment_method: str
    provider_name: str
    provider_transaction_id: Optional[str] = None
    status: str
    allocated_amount: float
    unallocated_amount: float
    failure_reason: Optional[str] = None
    is_simulated: bool
    payment_date: datetime
    created_at: datetime
    allocations: List[PaymentAllocationResponse] = []

    class Config:
        from_attributes = True


class PaymentRecordRequest(BaseModel):
    billing_account_id: Optional[str] = None
    customer_id: Optional[int] = None
    amount: float
    currency: Optional[str] = "INR"
    payment_method: Optional[str] = "UPI"
    invoice_id: Optional[int] = None
    idempotency_key: str
    provider_name: Optional[str] = "SIMULATED_GATEWAY"
    auto_allocate: Optional[bool] = True


class CreditNoteResponse(BaseModel):
    id: Any
    credit_note_code: str
    billing_account_id: Any
    invoice_id: Optional[int] = None
    market_id: str
    amount: float
    currency: str
    reason: str
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class CreditNoteCreateRequest(BaseModel):
    billing_account_id: str
    invoice_id: Optional[int] = None
    amount: float
    reason: str


class RefundRecordResponse(BaseModel):
    id: Any
    refund_code: str
    payment_id: Any
    billing_account_id: Any
    market_id: str
    amount: float
    currency: str
    reason: str
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class RefundCreateRequest(BaseModel):
    payment_id: str
    amount: float
    reason: str


class JournalLineResponse(BaseModel):
    id: Any
    account_code: str
    account_name: str
    debit: float
    credit: float
    currency: str

    class Config:
        from_attributes = True


class JournalEntryResponse(BaseModel):
    id: Any
    entry_number: str
    market_id: str
    reference_type: str
    reference_id: str
    description: str
    posted_at: datetime
    is_reversed: bool
    created_at: datetime
    lines: List[JournalLineResponse] = []
    total_debit: Optional[float] = None
    total_credit: Optional[float] = None
    is_balanced: Optional[bool] = None

    class Config:
        from_attributes = True


class LedgerBalanceResponse(BaseModel):
    account_code: str
    account_name: str
    total_debit: float
    total_credit: float
    net_balance: float
    currency: str


class VoucherRedeemRequest(BaseModel):
    customer_id: int
    voucher_code: str
    voucher_type: str
    idempotency_key: Optional[str] = None
    invoice_id: Optional[int] = None


class VoucherRedemptionResponse(BaseModel):
    id: Any
    voucher_code: str
    voucher_type: str
    customer_id: int
    customer_name: Optional[str] = None
    customer_code: Optional[str] = None
    market_id: str
    status: str
    benefit_details: Dict[str, Any] = {}
    financial_credit_amount: float
    invoice_id: Optional[int] = None
    provisioning_order_id: Optional[int] = None
    failure_reason: Optional[str] = None
    redeemed_at: datetime

    class Config:
        from_attributes = True


class FinanceOverviewResponse(BaseModel):
    currency: str
    date_range: str
    total_invoiced_amount: float
    total_collected_amount: float
    total_receivables: float
    overdue_invoices_count: int
    overdue_invoices_balance: float
    active_subscriptions_count: int
    active_billing_accounts_count: int
    recent_payments_count: int
    recent_payments: List[PaymentResponse] = []
    recent_invoices: List[InvoiceDetailResponse] = []
    gateway_status: Dict[str, Any] = {}


class BillingRunJobResponse(BaseModel):
    status: str
    market_id: str
    subscriptions_evaluated: int
    invoices_generated: int
    total_amount_billed: float
    currency: str
    timestamp: datetime


class GatewayStatusResponse(BaseModel):
    live_payments_available: bool
    active_provider: str
    configured_gateways: List[str]
    mode: str
    reason: str




class PaymentOrderCreateRequest(BaseModel):
    invoice_id: int
    amount: Optional[Decimal] = None
    payment_method: Optional[str] = "UPI"


class PaymentOrderResponse(BaseModel):
    payment_id: str
    payment_reference: str
    order_id: str
    gateway_order_id: str
    amount: float
    currency: str
    invoice_id: int
    invoice_code: str
    key_id: str
    is_simulated: bool
    status: str


class StorageDiagnosticResponse(BaseModel):
    active_provider: str
    is_configured: bool
    storage_mode: str
    storage_path_or_bucket: str
    allowed_mimes: List[str]
    max_size_mb: int


class CustomerBillingSummaryResponse(BaseModel):
    customer_id: int
    customer_code: str
    customer_name: str
    billing_account: Optional[BillingAccountResponse] = None
    active_subscriptions: List[BillingSubscriptionResponse] = []
    recent_invoices: List[InvoiceDetailResponse] = []
    recent_payments: List[PaymentResponse] = []
    total_outstanding: float = 0.0
    account_balance: float = 0.0


# ─── SentinelOS Background Billing Scheduler & Notification Schemas ───────────

class BillingJobRunResponse(BaseModel):
    id: str
    job_type: str
    market_id: Optional[str] = None
    status: str
    attempt_count: int
    start_time: datetime
    finish_time: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    records_processed: int = 0
    records_succeeded: int = 0
    records_failed: int = 0
    records_skipped: int = 0
    error_details: Optional[str] = None
    summary_metadata: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True


class BillingJobRunsListResponse(BaseModel):
    items: List[BillingJobRunResponse]
    total: int
    page: int
    page_size: int
    pages: int


class ScheduledJobDescriptor(BaseModel):
    job_type: str
    name: str
    description: str
    cadence: str
    next_run_estimated: Optional[str] = None
    is_locked: bool = False
    lock_info: Optional[Dict[str, Any]] = None
    last_run: Optional[BillingJobRunResponse] = None
    allowed_roles: List[str] = ["Admin", "SUPER_ADMIN", "Revenue"]


class JobRunManualRequest(BaseModel):
    market_id: Optional[str] = None


class JobReconciliationRequest(BaseModel):
    action: str = "RECONCILE"  # "RECONCILE" or "RETRY"
    reason: Optional[str] = "Manual operator reconciliation"


class NotificationOutboxItemResponse(BaseModel):
    id: str
    market_id: str
    customer_id: Optional[int] = None
    recipient_masked: str
    channel: str
    event_type: str
    message_preview: str
    status: str
    attempt_count: int
    max_attempts: int
    last_error_sanitized: Optional[str] = None
    idempotency_key: str
    is_simulated: bool
    scheduled_at: datetime
    sent_at: Optional[datetime] = None
    created_at: datetime


class NotificationOutboxListResponse(BaseModel):
    items: List[NotificationOutboxItemResponse]
    total: int
    page: int
    page_size: int
    pages: int
    summary_by_status: Dict[str, int] = {}


class SchedulerOverviewResponse(BaseModel):
    total_runs: int
    successful_runs: int
    failed_runs: int
    running_jobs: int
    last_completed_at: Optional[datetime] = None
    active_locks: int = 0
    outbox_queued: int = 0
    outbox_failed: int = 0
    outbox_sent: int = 0
    jobs: List[ScheduledJobDescriptor] = []
    runner: Optional[Dict[str, Any]] = None


class SchedulerRunnerStatusResponse(BaseModel):
    runner_enabled: bool
    is_running: bool
    is_leader: bool
    runner_id: str
    timezone: str
    started_at: Optional[str] = None
    last_heartbeat: Optional[str] = None
    coordination_backend: str
    active_jobs_count: int
    schedules: List[Dict[str, Any]] = []
    recent_errors: List[Dict[str, Any]] = []


class SchedulerRunnerToggleRequest(BaseModel):
    enabled: bool


# SentinelOS Carrier Inventory & Asset Management Schemas
from app.schemas_inventory import *
from app.schemas_tariff import *
from app.schemas_voucher import *
