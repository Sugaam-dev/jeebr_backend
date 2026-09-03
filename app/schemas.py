from datetime import datetime
from typing import Optional, List, Any, Dict
from pydantic import BaseModel, EmailStr

# Auth Schemas
class Token(BaseModel):
    access_token: str
    token_type: str = 'bearer'
    role: str
    user_name: str
    email: str

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
    is_active: bool

    class Config:
        from_attributes = True


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
    customer_id: int
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
    source_module: str
    target_entity_type: str
    target_entity_id: int
    target_entity_label: str
    title: str
    description: str
    recommended_action: str
    action_payload: Any = None
    confidence_score: float
    status: str
    created_at: datetime
    reviewed_by_id: Optional[int] = None
    reviewed_at: Optional[datetime] = None
    review_notes: Optional[str] = None

    class Config:
        from_attributes = True

class ApproveRejectRequest(BaseModel):
    recommendation_id: int
    notes: Optional[str] = None

class AuditLogResponse(BaseModel):
    id: int
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

