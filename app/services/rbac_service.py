"""
SentinelOS RBAC Architecture & Controlled Permission Registry
Implements:
SUPER_ADMIN (rank 100) -> ADMIN (rank 80) -> CONFIGURABLE OPERATIONAL ROLES (rank 50) -> CUSTOMER (20) -> VIEWER (10)
"""
from typing import Dict, List, Optional, Any
from sqlalchemy.orm import Session
from sqlalchemy import text

# Controlled Canonical Permission Registry
PERMISSION_REGISTRY: List[Dict[str, str]] = [
    # Ticket permissions
    {"code": "ticket.view", "name": "View Tickets", "category": "ticket", "description": "View ticket records subject to resource scope"},
    {"code": "ticket.create", "name": "Create Tickets", "category": "ticket", "description": "Raise new support or dispatch tickets"},
    {"code": "ticket.approve", "name": "Approve Tickets", "category": "ticket", "description": "Approve gated P1/P2 tickets for field dispatch"},
    {"code": "ticket.assign", "name": "Assign Tickets", "category": "ticket", "description": "Assign tickets to field engineers subject to capacity"},
    {"code": "ticket.update", "name": "Update Tickets", "category": "ticket", "description": "Update notes, priority, and ticket metadata"},
    {"code": "ticket.resolve", "name": "Resolve Tickets", "category": "ticket", "description": "Mark tickets as resolved and release capacity"},
    {"code": "ticket.track", "name": "Track Tickets", "category": "ticket", "description": "Track field engineer dispatch and location"},

    # Engineer & Field Operations
    {"code": "engineer.location.view", "name": "View Engineer Location", "category": "engineer", "description": "View live field engineer GPS locations"},
    {"code": "engineer.location.update", "name": "Transmit Location", "category": "engineer", "description": "Transmit GPS location pings from field device"},
    {"code": "engineer.job.accept", "name": "Accept Job", "category": "engineer", "description": "Acknowledge and accept assigned field job"},
    {"code": "engineer.job.start", "name": "Start Job", "category": "engineer", "description": "Transition job to EN_ROUTE, ARRIVED, WORKING"},
    {"code": "engineer.job.complete", "name": "Complete Job", "category": "engineer", "description": "Complete job verification and customer OTP sign-off"},
    {"code": "engineer.workload.view", "name": "View Engineer Workload", "category": "engineer", "description": "View engineer active workloads and capacity"},
    {"code": "engineer.workload.configure", "name": "Configure Workload", "category": "engineer", "description": "Configure maximum job capacity limits"},

    # Telemetry & OLT
    {"code": "telemetry.view", "name": "View Telemetry", "category": "telemetry", "description": "View optical and network performance metrics"},
    {"code": "olt.view", "name": "View OLT Devices", "category": "telemetry", "description": "View optical line terminal ports and hardware"},
    {"code": "olt.health", "name": "View OLT Health", "category": "telemetry", "description": "View OLT alarms, power levels, and degradation"},

    # User Management
    {"code": "user.view", "name": "View Users", "category": "user", "description": "List platform users and profiles"},
    {"code": "user.create", "name": "Create Users", "category": "user", "description": "Provision new user accounts with permitted roles"},
    {"code": "user.update", "name": "Update Users", "category": "user", "description": "Update user account information and roles"},
    {"code": "user.disable", "name": "Disable Users", "category": "user", "description": "Deactivate user accounts"},

    # Role Management
    {"code": "role.view", "name": "View Roles", "category": "role", "description": "View system roles and permission definitions"},
    {"code": "role.create", "name": "Create Roles", "category": "role", "description": "Create custom operational roles below Admin"},
    {"code": "role.update", "name": "Update Roles", "category": "role", "description": "Update custom operational role permissions"},
    {"code": "role.delete", "name": "Delete Roles", "category": "role", "description": "Delete custom operational roles"},
    {"code": "role.assign", "name": "Assign Roles", "category": "role", "description": "Assign authorized roles to platform users"},

    # System & Audit
    {"code": "system.health", "name": "System Health", "category": "system", "description": "View platform health diagnostics and status"},
    {"code": "system.configure", "name": "Configure System", "category": "system", "description": "Configure system-wide parameters and toggles"},
    {"code": "audit.view", "name": "View Audit Logs", "category": "audit", "description": "View cryptographic audit trail and governance logs"},
]

# Canonical Built-In System Roles with Rank & Permissions
ALL_PERMISSION_CODES = [p["code"] for p in PERMISSION_REGISTRY]

SYSTEM_ROLES: Dict[str, Dict[str, Any]] = {
    "SUPER_ADMIN": {
        "display_name": "Super Administrator",
        "description": "Unrestricted platform administrator with supreme authority across all modules and roles.",
        "rank": 100,
        "is_system": True,
        "permissions": ALL_PERMISSION_CODES
    },
    "Admin": {
        "display_name": "System Administrator",
        "description": "Full operational and organizational authority below Super Admin. Can manage lower roles and users.",
        "rank": 80,
        "is_system": True,
        "permissions": [
            p for p in ALL_PERMISSION_CODES
            if p not in ("system.configure",) # Admin has broad authority, Super Admin retains ultimate root config
        ] + ["system.configure"]  # Admin can configure operational systems
    },
    "NOC": {
        "display_name": "Network Operations Center Lead",
        "description": "Operational authority for service assurance, automated ticketing, splicing, and live field tracking.",
        "rank": 50,
        "is_system": True,
        "permissions": [
            "ticket.view", "ticket.approve", "ticket.assign", "ticket.update", "ticket.resolve", "ticket.track",
            "engineer.location.view", "engineer.workload.view",
            "telemetry.view", "olt.view", "olt.health", "audit.view", "system.health"
        ]
    },
    "Care": {
        "display_name": "Customer Care & Retention Lead",
        "description": "Authority over churn prediction, retention offers, subscriber journeys, and ticket follow-ups.",
        "rank": 50,
        "is_system": True,
        "permissions": [
            "ticket.view", "ticket.create", "ticket.update", "ticket.resolve",
            "audit.view", "system.health"
        ]
    },
    "Revenue": {
        "display_name": "Revenue Assurance Lead",
        "description": "Authority over billing ledger reconciliation, leakage remediation, and invoice audit.",
        "rank": 50,
        "is_system": True,
        "permissions": [
            "ticket.view", "audit.view", "system.health"
        ]
    },
    "Executive": {
        "display_name": "Executive & Portfolio Lead",
        "description": "High-level strategic portfolio overview, aggregated cockpit metrics, and audit view.",
        "rank": 50,
        "is_system": True,
        "permissions": [
            "ticket.view", "telemetry.view", "olt.view", "audit.view", "system.health"
        ]
    },
    "Field Engineer": {
        "display_name": "Field Service Engineer",
        "description": "Mobile field operations, GPS telemetry transmission, job execution, and OTP verification.",
        "rank": 50,
        "is_system": True,
        "permissions": [
            "ticket.view", "ticket.track",
            "engineer.location.update", "engineer.job.accept", "engineer.job.start", "engineer.job.complete",
            "engineer.workload.view"
        ]
    },
    "Customer": {
        "display_name": "Subscriber Customer",
        "description": "Self-service subscriber portal. Limited strictly to own tickets, billing, and live dispatch tracking.",
        "rank": 20,
        "is_system": True,
        "permissions": [
            "ticket.view", "ticket.create", "ticket.track"
        ]
    },
    "Viewer": {
        "display_name": "Read-Only Stakeholder",
        "description": "Read-only access to high-level dashboards without mutation or live employee tracking access.",
        "rank": 10,
        "is_system": True,
        "permissions": [
            "ticket.view", "telemetry.view", "audit.view"
        ]
    }
}

# Role aliases for normalization (case-insensitive & hyphen/underscore mapping)
ROLE_ALIASES: Dict[str, str] = {
    "super_admin": "SUPER_ADMIN",
    "super-admin": "SUPER_ADMIN",
    "superadmin": "SUPER_ADMIN",
    "super admin": "SUPER_ADMIN",
    "admin": "Admin",
    "administrator": "Admin",
    "noc": "NOC",
    "care": "Care",
    "revenue": "Revenue",
    "executive": "Executive",
    "field_engineer": "Field Engineer",
    "field-engineer": "Field Engineer",
    "fieldengineer": "Field Engineer",
    "field engineer": "Field Engineer",
    "customer": "Customer",
    "viewer": "Viewer"
}

def normalize_role_name(role: Optional[str]) -> str:
    """Normalize input role to authoritative canonical role name."""
    if not role:
        return "Viewer"
    clean = role.strip().lower().replace("_", " ").replace("-", " ")
    for alias, canonical in ROLE_ALIASES.items():
        if alias.replace("_", " ").replace("-", " ") == clean:
            return canonical
    return role.strip()

def is_super_admin(role: Optional[str]) -> bool:
    """Check if role corresponds to Super Admin."""
    if not role:
        return False
    return normalize_role_name(role) == "SUPER_ADMIN"

def is_admin_or_super(role: Optional[str]) -> bool:
    """Check if role is Admin or Super Admin."""
    if not role:
        return False
    normalized = normalize_role_name(role)
    return normalized in ("SUPER_ADMIN", "Admin")

def get_role_rank(role_name: str, db: Optional[Session] = None) -> int:
    """Retrieve hierarchical rank of a role."""
    norm = normalize_role_name(role_name)
    if norm in SYSTEM_ROLES:
        return SYSTEM_ROLES[norm]["rank"]
    if db is not None:
        try:
            from app.models import Role
            role_obj = db.query(Role).filter(Role.name == norm).first()
            if role_obj and role_obj.rank is not None:
                return role_obj.rank
        except Exception:
            pass
    return 50  # Default rank for custom operational roles

def get_default_permissions_for_role(role_name: str) -> List[str]:
    """Retrieve canonical permissions for a built-in role."""
    norm = normalize_role_name(role_name)
    if norm in SYSTEM_ROLES:
        return list(SYSTEM_ROLES[norm]["permissions"])
    return []

def ensure_rbac_seeded(db: Session) -> None:
    """Ensure all permissions and system roles are populated in the database."""
    from app.models import Role, Permission, RolePermission
    
    # 1. Seed Permissions
    existing_perms = {p.code: p for p in db.query(Permission).all()}
    for p_def in PERMISSION_REGISTRY:
        if p_def["code"] not in existing_perms:
            perm = Permission(
                code=p_def["code"],
                name=p_def["name"],
                category=p_def["category"],
                description=p_def["description"]
            )
            db.add(perm)
    db.commit()

    # 2. Seed System Roles
    existing_roles = {r.name: r for r in db.query(Role).all()}
    for role_name, role_def in SYSTEM_ROLES.items():
        if role_name not in existing_roles:
            role = Role(
                name=role_name,
                display_name=role_def["display_name"],
                description=role_def["description"],
                rank=role_def["rank"],
                is_system=True
            )
            db.add(role)
            db.flush()
            existing_roles[role_name] = role
        else:
            # Sync rank and is_system
            existing_roles[role_name].rank = role_def["rank"]
            existing_roles[role_name].is_system = True

    db.commit()

    # 3. Seed Role-Permission Mappings
    for role_name, role_def in SYSTEM_ROLES.items():
        role = existing_roles[role_name]
        current_perms = {rp.permission_code for rp in db.query(RolePermission).filter(RolePermission.role_id == role.id).all()}
        for p_code in role_def["permissions"]:
            if p_code not in current_perms:
                rp = RolePermission(role_id=role.id, permission_code=p_code)
                db.add(rp)
    db.commit()

def get_user_permissions(db: Session, user: Any) -> List[str]:
    """Get active permission codes for a user based on their role."""
    if not user:
        return []
    
    norm_role = normalize_role_name(user.role)
    if norm_role == "SUPER_ADMIN":
        return list(ALL_PERMISSION_CODES)

    from app.models import Role, RolePermission
    try:
        role = db.query(Role).filter(Role.name == norm_role).first()
        if role:
            rps = db.query(RolePermission.permission_code).filter(RolePermission.role_id == role.id).all()
            if rps:
                return [rp[0] for rp in rps]
    except Exception:
        pass

    # Fallback to static system role definition
    return get_default_permissions_for_role(norm_role)

def user_has_permission(db: Session, user: Any, permission_code: str) -> bool:
    """Check if user has specific permission code."""
    if not user:
        return False
    if is_super_admin(user.role):
        return True
    user_perms = get_user_permissions(db, user)
    return permission_code in user_perms
