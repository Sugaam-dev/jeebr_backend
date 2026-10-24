"""
app/services/provisioning_catalog.py

Authoritative Tariff & Service Plan Catalog for SentinelOS Provisioning.
Reuses existing configured telecom plans from seed data without data fabrication.
"""

from typing import List, Optional, Dict, Any

CANONICAL_SERVICE_PLANS = [
    # Prepaid Consumer Plans
    {
        "plan_id": "PLAN-PRE-HERO-1.5G",
        "name": "Hero Unlimited 1.5GB/Day (28d)",
        "account_type": "RESIDENTIAL",
        "billing_type": "PREPAID",
        "price_inr": 299.0,
        "speed_mbps": 50,
        "data_quota_gb": 42.0,  # 1.5 GB/day * 28d
        "daily_quota_gb": 1.5,
        "validity_days": 28,
        "segment": "Prepaid - Daily Unlimited",
        "service_vlan": 101,
        "description": "Popular daily unlimited consumer pack with standard high-speed broadband and unlimited voice."
    },
    {
        "plan_id": "PLAN-PRE-SUPER-5G",
        "name": "Super 5G 2GB/Day + Hotstar (28d)",
        "account_type": "RESIDENTIAL",
        "billing_type": "PREPAID",
        "price_inr": 349.0,
        "speed_mbps": 100,
        "data_quota_gb": 56.0,  # 2.0 GB/day * 28d
        "daily_quota_gb": 2.0,
        "validity_days": 28,
        "segment": "Prepaid - 5G High Speed",
        "service_vlan": 102,
        "description": "High-speed 100 Mbps broadband bundle with daily 2GB quota and bundled OTT streaming."
    },
    {
        "plan_id": "PLAN-PRE-VALUE-VOICE",
        "name": "Voice & Value 1GB/Day (28d)",
        "account_type": "RESIDENTIAL",
        "billing_type": "PREPAID",
        "price_inr": 239.0,
        "speed_mbps": 30,
        "data_quota_gb": 28.0,
        "daily_quota_gb": 1.0,
        "validity_days": 28,
        "segment": "Prepaid - Value Voice",
        "service_vlan": 103,
        "description": "Economical voice and light data pack for essential connectivity."
    },
    {
        "plan_id": "PLAN-PRE-TRUE-5G-56D",
        "name": "True 5G Unlimited 2GB/Day (56d)",
        "account_type": "RESIDENTIAL",
        "billing_type": "PREPAID",
        "price_inr": 579.0,
        "speed_mbps": 100,
        "data_quota_gb": 112.0,
        "daily_quota_gb": 2.0,
        "validity_days": 56,
        "segment": "Prepaid - 5G High Speed",
        "service_vlan": 104,
        "description": "Extended 56-day high-speed package for medium-term prepaid subscribers."
    },
    {
        "plan_id": "PLAN-PRE-SAVER-84D",
        "name": "Super Saver 1.5GB/Day (84d)",
        "account_type": "RESIDENTIAL",
        "billing_type": "PREPAID",
        "price_inr": 719.0,
        "speed_mbps": 50,
        "data_quota_gb": 126.0,
        "daily_quota_gb": 1.5,
        "validity_days": 84,
        "segment": "Prepaid - Long-term Bundle",
        "service_vlan": 105,
        "description": "Quarterly long-term value bundle delivering consistent daily bandwidth."
    },
    {
        "plan_id": "PLAN-PRE-ANNUAL-5G",
        "name": "Annual 5G All-Access 2.5GB/Day (365d)",
        "account_type": "RESIDENTIAL",
        "billing_type": "PREPAID",
        "price_inr": 2999.0,
        "speed_mbps": 150,
        "data_quota_gb": 912.5,
        "daily_quota_gb": 2.5,
        "validity_days": 365,
        "segment": "Prepaid - Long-term Bundle",
        "service_vlan": 106,
        "description": "Full-year premium 365-day subscription with priority queueing and high data allowance."
    },

    # Postpaid Individual & Family Plans
    {
        "plan_id": "PLAN-POST-INFINITY-75G",
        "name": "Postpaid Individual Infinity 75GB",
        "account_type": "RESIDENTIAL",
        "billing_type": "POSTPAID",
        "price_inr": 499.0,
        "speed_mbps": 100,
        "data_quota_gb": 75.0,
        "daily_quota_gb": 0.0,
        "validity_days": 30,
        "segment": "Postpaid - Individual Infinity",
        "service_vlan": 201,
        "description": "Fixed 30-day billing cycle individual postpaid connection with 75GB rollover pool."
    },
    {
        "plan_id": "PLAN-POST-FAMILY-150G",
        "name": "Postpaid Family Plus (3 SIMs) 150GB",
        "account_type": "RESIDENTIAL",
        "billing_type": "POSTPAID",
        "price_inr": 999.0,
        "speed_mbps": 200,
        "data_quota_gb": 150.0,
        "daily_quota_gb": 0.0,
        "validity_days": 30,
        "segment": "Postpaid - Family Plus",
        "service_vlan": 202,
        "description": "High-throughput 200 Mbps family plan with shared 150GB data bucket and priority Care SLA."
    },

    # Enterprise & SME Dedicated Plans
    {
        "plan_id": "PLAN-POST-CORP-500M",
        "name": "Postpaid Enterprise Corporate ILL 500M",
        "account_type": "ENTERPRISE",
        "billing_type": "POSTPAID",
        "price_inr": 12000.0,
        "speed_mbps": 500,
        "data_quota_gb": 1000.0,
        "daily_quota_gb": 0.0,
        "validity_days": 30,
        "segment": "Postpaid - Enterprise ILL",
        "service_vlan": 301,
        "description": "Symmetric 1:1 Internet Leased Line (ILL) with 99.9% uptime SLA, dedicated VLAN, and BGP peering."
    },
    {
        "plan_id": "PLAN-POST-PREMIUM-1G",
        "name": "Postpaid Premium Dedicated 1Gbps",
        "account_type": "ENTERPRISE",
        "billing_type": "POSTPAID",
        "price_inr": 22000.0,
        "speed_mbps": 1000,
        "data_quota_gb": 2500.0,
        "daily_quota_gb": 0.0,
        "validity_days": 30,
        "segment": "Postpaid - Enterprise ILL",
        "service_vlan": 302,
        "description": "Ultra-capacity Gigabit enterprise circuit with dual homing, dedicated BNG profile, and 4-hour MTTR."
    }
]

PLAN_BY_ID: Dict[str, Dict[str, Any]] = {p["plan_id"]: p for p in CANONICAL_SERVICE_PLANS}


def get_all_service_plans(account_type: Optional[str] = None, billing_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieve filtered service plans from the canonical catalog."""
    plans = CANONICAL_SERVICE_PLANS
    if account_type:
        plans = [p for p in plans if p["account_type"] == account_type or p["account_type"] == "ALL" or account_type == "ALL"]
    if billing_type:
        plans = [p for p in plans if p["billing_type"].upper() == billing_type.upper()]
    return plans


def get_service_plan_by_id(plan_id: str) -> Optional[Dict[str, Any]]:
    """Look up a specific service plan by ID."""
    return PLAN_BY_ID.get(plan_id)
