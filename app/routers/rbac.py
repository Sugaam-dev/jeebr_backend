"""
SentinelOS RBAC Router: Role & Permission Management
Implements:
SUPER_ADMIN (rank 100) -> ADMIN (rank 80) -> CONFIGURABLE OPERATIONAL ROLES (< 80)
With strict privilege boundary enforcement.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User, Role, Permission, RolePermission
from app.auth import get_current_user, require_roles, require_super_admin, log_security_event
from app.schemas import (
    RoleResponse, RoleCreateRequest, RoleUpdateRequest,
    PermissionResponse, UserRoleAssignRequest, UserResponse
)
from app.services.rbac_service import (
    PERMISSION_REGISTRY, SYSTEM_ROLES, normalize_role_name,
    is_super_admin, is_admin_or_super, get_role_rank, get_user_permissions
)

router = APIRouter(prefix="/rbac", tags=["Role-Based Access Control"])

@router.get("/permissions", response_model=List[PermissionResponse])
def get_permissions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all permissions from the controlled permission registry."""
    perms = db.query(Permission).order_by(Permission.category.asc(), Permission.code.asc()).all()
    if not perms:
        return [PermissionResponse(**p) for p in PERMISSION_REGISTRY]
    return perms

@router.get("/roles", response_model=List[RoleResponse])
def get_roles(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    """List all system and custom operational roles with effective permissions."""
    roles = db.query(Role).order_by(Role.rank.desc(), Role.name.asc()).all()
    results = []
    for r in roles:
        perms = [rp.permission_code for rp in db.query(RolePermission).filter(RolePermission.role_id == r.id).all()]
        results.append(RoleResponse(
            id=r.id,
            name=r.name,
            display_name=r.display_name,
            description=r.description,
            rank=r.rank,
            is_system=r.is_system,
            permissions=perms
        ))
    return results

@router.get("/assignable-roles", response_model=List[RoleResponse])
def get_assignable_roles(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    """
    List roles that the currently authenticated administrator is authorized to assign to users.
    - Super Admin gets all assignable roles (Admin, operational roles, custom roles).
    - Admin gets only roles with rank < 80 (no Super Admin, no Admin).
    """
    requester_is_super = is_super_admin(current_user.role)
    requester_rank = get_role_rank(current_user.role, db)

    roles = db.query(Role).order_by(Role.rank.desc(), Role.name.asc()).all()
    results = []
    for r in roles:
        if not requester_is_super:
            if r.name == "SUPER_ADMIN" or r.name == "Admin" or r.rank >= requester_rank:
                continue
        perms = [rp.permission_code for rp in db.query(RolePermission).filter(RolePermission.role_id == r.id).all()]
        results.append(RoleResponse(
            id=r.id,
            name=r.name,
            display_name=r.display_name,
            description=r.description,
            rank=r.rank,
            is_system=r.is_system,
            permissions=perms
        ))
    return results

@router.post("/roles", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
def create_custom_role(
    req: RoleCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    """
    Create a new custom operational role below Admin.
    Admin can only create roles with rank < 80.
    Super Admin can create roles with rank < 100.
    """
    clean_name = req.name.strip()
    norm_name = normalize_role_name(clean_name)
    
    # 1. Protected role names check
    if norm_name in ("SUPER_ADMIN", "Admin") or clean_name.lower() in ("super_admin", "super admin", "admin"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot create role with reserved system name."
        )

    # 2. Check existence
    existing = db.query(Role).filter(Role.name.ilike(clean_name)).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Role with name '{clean_name}' already exists."
        )

    # 3. Hierarchy Privilege Boundaries (Section 8)
    requester_is_super = is_super_admin(current_user.role)
    target_rank = req.rank if req.rank is not None else 50

    if not requester_is_super:
        # Admin can only create roles with rank < 80
        if target_rank >= 80:
            log_security_event(
                db=db,
                action=f"Privilege escalation attempt by {current_user.email}: attempted to create role '{clean_name}' with rank {target_rank}",
                decision="ACCESS_DENIED",
                user=current_user,
                endpoint="/api/rbac/roles",
                method="POST"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Admin cannot create roles with rank >= 80."
            )

    # 4. Validate permissions against registry
    valid_perm_codes = {p["code"] for p in PERMISSION_REGISTRY}
    for p_code in req.permissions:
        if p_code not in valid_perm_codes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Permission '{p_code}' does not exist in controlled permission registry."
            )

    # 5. Admin can only grant permissions Admin possesses
    if not requester_is_super:
        admin_perms = set(get_user_permissions(db, current_user))
        excess_perms = set(req.permissions) - admin_perms
        if excess_perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: You cannot grant permissions you do not possess: {list(excess_perms)}"
            )

    new_role = Role(
        name=clean_name,
        display_name=req.display_name.strip() or clean_name,
        description=req.description,
        rank=target_rank,
        is_system=False
    )
    db.add(new_role)
    db.flush()

    for p_code in req.permissions:
        db.add(RolePermission(role_id=new_role.id, permission_code=p_code))

    db.commit()
    db.refresh(new_role)

    log_security_event(
        db=db,
        action=f"Custom role created: '{new_role.name}' (rank {new_role.rank}) by {current_user.email}",
        decision="ROLE_CREATED",
        user=current_user,
        endpoint="/api/rbac/roles",
        method="POST"
    )

    return RoleResponse(
        id=new_role.id,
        name=new_role.name,
        display_name=new_role.display_name,
        description=new_role.description,
        rank=new_role.rank,
        is_system=new_role.is_system,
        permissions=req.permissions
    )

@router.put("/roles/{role_id}", response_model=RoleResponse)
def update_role(
    role_id: int,
    req: RoleUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    """
    Update a custom role's display name, description, or permissions.
    Admin cannot modify SUPER_ADMIN or Admin role!
    """
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    requester_is_super = is_super_admin(current_user.role)

    # Privilege boundary: cannot edit Super Admin or equal/higher rank roles unless Super Admin
    if not requester_is_super:
        if role.name == "SUPER_ADMIN" or role.rank >= 80 or role.is_system:
            log_security_event(
                db=db,
                action=f"Unauthorized role modification attempt by {current_user.email} on '{role.name}'",
                decision="ACCESS_DENIED",
                user=current_user,
                endpoint=f"/api/rbac/roles/{role_id}",
                method="PUT"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: You do not have authority to modify protected role '{role.name}'."
            )

    if req.display_name is not None:
        role.display_name = req.display_name.strip()
    if req.description is not None:
        role.description = req.description

    if req.permissions is not None:
        valid_perm_codes = {p["code"] for p in PERMISSION_REGISTRY}
        for p in req.permissions:
            if p not in valid_perm_codes:
                raise HTTPException(status_code=400, detail=f"Permission '{p}' is not recognized.")

        if not requester_is_super:
            admin_perms = set(get_user_permissions(db, current_user))
            excess = set(req.permissions) - admin_perms
            if excess:
                raise HTTPException(
                    status_code=403,
                    detail=f"Forbidden: Cannot grant permissions you do not possess: {list(excess)}"
                )

        # Clear existing and re-assign
        db.query(RolePermission).filter(RolePermission.role_id == role.id).delete()
        for p in req.permissions:
            db.add(RolePermission(role_id=role.id, permission_code=p))

    db.commit()
    db.refresh(role)

    perms = [rp.permission_code for rp in db.query(RolePermission).filter(RolePermission.role_id == role.id).all()]
    return RoleResponse(
        id=role.id,
        name=role.name,
        display_name=role.display_name,
        description=role.description,
        rank=role.rank,
        is_system=role.is_system,
        permissions=perms
    )

@router.delete("/roles/{role_id}", status_code=status.HTTP_200_OK)
def delete_role(
    role_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    """Delete a custom operational role. System roles cannot be deleted."""
    role = db.query(Role).filter(Role.id == role_id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    if role.is_system or role.name in SYSTEM_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: System role '{role.name}' cannot be deleted."
        )

    if not is_super_admin(current_user.role) and role.rank >= 80:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Admin cannot delete roles with rank >= 80."
        )

    # Check if any users have this role
    users_with_role = db.query(User).filter(User.role == role.name).count()
    if users_with_role > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete role '{role.name}' because {users_with_role} active users are currently assigned to it."
        )

    db.query(RolePermission).filter(RolePermission.role_id == role.id).delete()
    db.delete(role)
    db.commit()

    log_security_event(
        db=db,
        action=f"Custom role '{role.name}' deleted by {current_user.email}",
        decision="ROLE_DELETED",
        user=current_user,
        endpoint=f"/api/rbac/roles/{role_id}",
        method="DELETE"
    )
    return {"status": "success", "message": f"Role '{role.name}' deleted successfully."}

@router.post("/users/{user_id}/role", response_model=UserResponse)
def assign_user_role(
    user_id: int,
    req: UserRoleAssignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    """
    Assign a role to a user.
    Admin CANNOT:
    - Assign SUPER_ADMIN
    - Promote anyone to SUPER_ADMIN
    - Modify a SUPER_ADMIN user
    """
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    target_role_name = normalize_role_name(req.role)
    target_rank = get_role_rank(target_role_name)
    current_user_rank = get_role_rank(current_user.role)
    requester_is_super = is_super_admin(current_user.role)

    # 1. Super Admin protection: Admin cannot touch a Super Admin user
    if is_super_admin(target_user.role) and not requester_is_super:
        log_security_event(
            db=db,
            action=f"Unauthorized attempt by {current_user.email} to alter Super Admin account {target_user.email}",
            decision="ACCESS_DENIED",
            user=current_user,
            endpoint=f"/api/rbac/users/{user_id}/role",
            method="POST"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Only Super Administrator can modify Super Admin accounts."
        )

    # 2. Elevation protection: Admin cannot assign SUPER_ADMIN or roles with rank >= Admin
    if not requester_is_super:
        if target_role_name == "SUPER_ADMIN" or target_rank >= current_user_rank:
            log_security_event(
                db=db,
                action=f"Privilege escalation attempt by {current_user.email}: cannot assign role '{target_role_name}'",
                decision="ACCESS_DENIED",
                user=current_user,
                endpoint=f"/api/rbac/users/{user_id}/role",
                method="POST"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: You do not have authority to grant role '{target_role_name}'."
            )

    # 3. Verify target role exists
    role_exists = db.query(Role).filter(Role.name == target_role_name).first()
    if not role_exists and target_role_name not in SYSTEM_ROLES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role '{req.role}' is not a recognized role."
        )

    old_role = target_user.role
    target_user.role = target_role_name
    db.commit()
    db.refresh(target_user)

    log_security_event(
        db=db,
        action=f"Role assignment: {target_user.email} changed from '{old_role}' to '{target_user.role}' by {current_user.email}",
        decision="ROLE_ASSIGNED",
        user=current_user,
        endpoint=f"/api/rbac/users/{user_id}/role",
        method="POST"
    )

    return UserResponse(
        id=target_user.id,
        email=target_user.email,
        full_name=target_user.full_name,
        role=target_user.role,
        is_active=target_user.is_active,
        rank=target_rank,
        permissions=get_user_permissions(db, target_user)
    )
