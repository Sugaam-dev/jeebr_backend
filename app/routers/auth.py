from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import User, Role
from app.schemas import (
    Token, LoginRequest, SignupRequest, UserResponse,
    AdminUserCreateRequest, AdminUserUpdateRequest, UserStatusUpdateRequest
)
from app.auth import (
    verify_password, hash_password, create_access_token, get_current_user,
    require_roles, check_login_rate_limit, record_failed_login,
    clear_failed_logins, log_security_event
)
from app.services.rbac_service import (
    normalize_role_name, is_super_admin, get_role_rank, get_user_permissions, SYSTEM_ROLES
)

router = APIRouter(prefix="/auth", tags=["Authentication"])

def _set_auth_cookie(response: Response, token: str) -> None:
    """Set secure HttpOnly authentication cookie."""
    response.set_cookie(
        key="access_token",
        value=token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        path="/"
    )

@router.post("/signup", response_model=Token, status_code=status.HTTP_201_CREATED)
def signup(req: SignupRequest, response: Response, db: Session = Depends(get_db)):
    # 1. Validation: check if email already exists
    existing_user = db.query(User).filter(User.email == req.email.lower().strip()).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this email address already exists"
        )
    
    # 2. Validate password strength
    if len(req.password.strip()) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters in length"
        )

    # 3. Validate name
    if not req.full_name or len(req.full_name.strip()) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Full name is required (minimum 2 characters)"
        )

    # 4. Registration Security Policy (Section 4 & 5)
    # Self-registration is strictly restricted to allowed non-privileged roles: Viewer, Customer
    ALLOWED_PUBLIC_REGISTRATION_ROLES = ["Viewer", "Customer"]
    requested_role_raw = req.role.strip() if req.role else "Viewer"
    requested_role = normalize_role_name(requested_role_raw)

    if requested_role not in ALLOWED_PUBLIC_REGISTRATION_ROLES:
        log_security_event(
            db=db,
            action=f"Privilege escalation blocked during public signup: attempt to register as '{req.role}'",
            decision="ACCESS_DENIED",
            user=None,
            endpoint="/api/auth/signup",
            method="POST",
            details={"requested_role": req.role, "email": req.email}
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Privileged roles ({req.role}) cannot be self-registered. Public registration is restricted to: {', '.join(ALLOWED_PUBLIC_REGISTRATION_ROLES)}. Please contact your administrator."
        )

    user_role = requested_role

    # 5. Create user with hashed password
    new_user = User(
        email=req.email.lower().strip(),
        hashed_password=hash_password(req.password),
        full_name=req.full_name.strip(),
        role=user_role,
        is_active=True
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    log_security_event(
        db=db,
        action=f"User signup: {new_user.email} with role {new_user.role}",
        decision="LOGIN_SUCCESS",
        user=new_user,
        endpoint="/api/auth/signup",
        method="POST"
    )

    # 6. Issue access token and return Token with active permissions and rank
    token = create_access_token({"sub": new_user.email, "role": new_user.role})
    _set_auth_cookie(response, token)
    return Token(
        access_token=token,
        role=new_user.role,
        user_name=new_user.full_name,
        email=new_user.email,
        rank=get_role_rank(new_user.role),
        permissions=get_user_permissions(db, new_user)
    )


@router.post("/login", response_model=Token)
def login(req: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    email_key = req.email.lower().strip()
    rate_key = f"{client_ip}:{email_key}"
    
    # Check brute-force rate limit with composite key
    check_login_rate_limit(rate_key)

    user = db.query(User).filter(User.email == email_key).first()
    if not user or not verify_password(req.password, user.hashed_password):
        record_failed_login(rate_key)
        log_security_event(
            db=db,
            action=f"Failed login attempt from IP {client_ip}",
            decision="LOGIN_FAILURE",
            user=None,
            endpoint="/api/auth/login",
            method="POST",
            details={"ip": client_ip, "email_length": len(email_key)}
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"}
        )

    clear_failed_logins(rate_key)
    
    log_security_event(
        db=db,
        action=f"Successful login for {user.email} ({user.role})",
        decision="LOGIN_SUCCESS",
        user=user,
        endpoint="/api/auth/login",
        method="POST"
    )

    token = create_access_token({"sub": user.email, "role": user.role})
    _set_auth_cookie(response, token)
    return Token(
        access_token=token,
        role=user.role,
        user_name=user.full_name,
        email=user.email,
        rank=get_role_rank(user.role),
        permissions=get_user_permissions(db, user)
    )

@router.post("/logout")
def logout(response: Response, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Clear HttpOnly authentication cookie and log logout security event."""
    response.delete_cookie(key="access_token", path="/")
    log_security_event(
        db=db,
        action=f"User logout: {current_user.email}",
        decision="LOGOUT",
        user=current_user,
        endpoint="/api/auth/logout",
        method="POST"
    )
    return {"status": "logged_out", "message": "Successfully logged out"}

@router.post("/demo-login/{role}", response_model=Token)
def demo_login(role: str, response: Response, db: Session = Depends(get_db)):
    if settings.ENVIRONMENT.lower() in ("production", "prod"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Demo login endpoint is strictly disabled in production environments."
        )

    valid_roles = ["SUPER_ADMIN", "Executive", "NOC", "Care", "Revenue", "Admin", "Field Engineer", "Customer", "Viewer"]
    normalized_input = normalize_role_name(role)
    matched_role = next((r for r in valid_roles if normalize_role_name(r).lower() == normalized_input.lower()), None)
    if not matched_role:
        raise HTTPException(status_code=400, detail=f"Invalid demo role. Valid roles: {valid_roles}")

    # Query user matching canonical or legacy capitalization
    user = db.query(User).filter(
        (User.role == matched_role) | (User.role == normalized_input)
    ).first()
    
    if not user:
        # Fallback query for Super Admin
        if matched_role == "SUPER_ADMIN":
            user = db.query(User).filter(User.email == "superadmin@pmrg.in").first()

    if not user:
        raise HTTPException(status_code=404, detail=f"No seeded user found for role {matched_role}")

    token = create_access_token({"sub": user.email, "role": user.role})
    _set_auth_cookie(response, token)
    return Token(
        access_token=token,
        role=user.role,
        user_name=user.full_name,
        email=user.email,
        rank=get_role_rank(user.role),
        permissions=get_user_permissions(db, user)
    )

@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        is_active=current_user.is_active,
        rank=get_role_rank(current_user.role),
        permissions=get_user_permissions(db, current_user)
    )

@router.get("/users", response_model=List[UserResponse])
def get_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    users = db.query(User).order_by(User.id.asc()).all()
    return [
        UserResponse(
            id=u.id,
            email=u.email,
            full_name=u.full_name,
            role=u.role,
            is_active=u.is_active,
            rank=get_role_rank(u.role),
            permissions=get_user_permissions(db, u)
        )
        for u in users
    ]

@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def admin_create_user(
    req: AdminUserCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    target_role = normalize_role_name(req.role)
    target_rank = get_role_rank(target_role, db)
    requester_rank = get_role_rank(current_user.role, db)
    requester_is_super = is_super_admin(current_user.role)

    # 1. Verify target role exists
    role_exists = db.query(Role).filter(Role.name == target_role).first()
    if not role_exists and target_role not in SYSTEM_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Role '{req.role}' is not a recognized role."
        )

    # 2. Privilege boundary: Admin cannot create Super Admin, Admin, or any role with rank >= requester_rank
    if not requester_is_super:
        if is_super_admin(target_role) or target_role == "SUPER_ADMIN" or target_rank >= requester_rank or target_role == "Admin":
            log_security_event(
                db=db,
                action=f"Privilege escalation attempt by {current_user.email}: cannot create user with role '{target_role}'",
                decision="ACCESS_DENIED",
                user=current_user,
                endpoint="/api/auth/users",
                method="POST"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You are not authorized to create a user with this role. You do not have the authority to create user accounts with role '{target_role}'."
            )

    # 3. Duplicate email check -> 409 Conflict
    clean_email = req.email.lower().strip()
    existing = db.query(User).filter(User.email == clean_email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email/user ID already exists."
        )

    # 4. Password validation
    if len(req.password.strip()) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 6 characters in length."
        )

    # 5. Persist user
    user_name = (req.full_name or req.name or "").strip()
    new_user = User(
        email=clean_email,
        hashed_password=hash_password(req.password),
        full_name=user_name,
        role=target_role,
        is_active=bool(req.is_active)
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    log_security_event(
        db=db,
        action=f"User provisioned: {new_user.email} with role {new_user.role} by {current_user.email}",
        decision="USER_CREATED",
        user=current_user,
        endpoint="/api/auth/users",
        method="POST",
        details={
            "created_user_email": new_user.email,
            "created_user_role": new_user.role,
            "created_user_status": "Active" if new_user.is_active else "Disabled"
        }
    )

    return UserResponse(
        id=new_user.id,
        email=new_user.email,
        full_name=new_user.full_name,
        role=new_user.role,
        is_active=new_user.is_active,
        rank=target_rank,
        permissions=get_user_permissions(db, new_user)
    )


@router.put("/users/{user_id}", response_model=UserResponse)
def admin_update_user(
    user_id: int,
    req: AdminUserUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    """
    Update permitted user attributes (Name, Role, Status).
    Enforces privilege boundaries:
    - Admin cannot modify Super Admin
    - Admin cannot self-escalate or change their own role
    - Admin cannot assign Super Admin, Admin, or roles with rank >= Admin
    """
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    requester_is_super = is_super_admin(current_user.role)
    current_user_rank = get_role_rank(current_user.role, db)

    # Protection: Admin cannot modify Super Admin account
    if is_super_admin(target_user.role) and not requester_is_super:
        log_security_event(
            db=db,
            action=f"Unauthorized attempt by {current_user.email} to modify Super Admin account {target_user.email}",
            decision="ACCESS_DENIED",
            user=current_user,
            endpoint=f"/api/auth/users/{user_id}",
            method="PUT"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Only Super Administrator can modify Super Admin accounts."
        )

    # Self-escalation prevention: cannot change own role
    if target_user.id == current_user.id and req.role is not None:
        target_role_norm = normalize_role_name(req.role)
        if target_role_norm != current_user.role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Administrators cannot modify their own role."
            )

    # Role change
    if req.role is not None:
        new_role = normalize_role_name(req.role)
        new_rank = get_role_rank(new_role, db)

        if not requester_is_super:
            if is_super_admin(new_role) or new_role == "SUPER_ADMIN" or new_rank >= current_user_rank or new_role == "Admin":
                log_security_event(
                    db=db,
                    action=f"Privilege escalation attempt by {current_user.email}: cannot assign role '{new_role}'",
                    decision="ACCESS_DENIED",
                    user=current_user,
                    endpoint=f"/api/auth/users/{user_id}",
                    method="PUT"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You are not authorized to assign this role."
                )

        role_exists = db.query(Role).filter(Role.name == new_role).first()
        if not role_exists and new_role not in SYSTEM_ROLES:
            raise HTTPException(status_code=404, detail=f"Role '{req.role}' is not recognized.")

        old_role = target_user.role
        target_user.role = new_role
        log_security_event(
            db=db,
            action=f"User role changed: {target_user.email} from '{old_role}' to '{target_user.role}' by {current_user.email}",
            decision="USER_ROLE_CHANGED",
            user=current_user,
            endpoint=f"/api/auth/users/{user_id}",
            method="PUT"
        )

    if req.full_name is not None or req.name is not None:
        name_val = (req.full_name or req.name or "").strip()
        if name_val:
            target_user.full_name = name_val

    if req.is_active is not None:
        if is_super_admin(target_user.role) and not requester_is_super:
            raise HTTPException(status_code=403, detail="Forbidden: Cannot modify Super Admin status.")
        if target_user.id == current_user.id and not req.is_active:
            raise HTTPException(status_code=400, detail="Cannot disable your own user account.")
        target_user.is_active = bool(req.is_active)
        status_str = "Active" if target_user.is_active else "Disabled"
        log_security_event(
            db=db,
            action=f"User status updated: {target_user.email} status set to '{status_str}' by {current_user.email}",
            decision="USER_STATUS_UPDATED",
            user=current_user,
            endpoint=f"/api/auth/users/{user_id}",
            method="PUT"
        )

    db.commit()
    db.refresh(target_user)

    return UserResponse(
        id=target_user.id,
        email=target_user.email,
        full_name=target_user.full_name,
        role=target_user.role,
        is_active=target_user.is_active,
        rank=get_role_rank(target_user.role, db),
        permissions=get_user_permissions(db, target_user)
    )


@router.patch("/users/{user_id}/status", response_model=UserResponse)
def toggle_user_status(
    user_id: int,
    req: UserStatusUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    """
    Disable or activate a user account.
    Admin cannot disable Super Admin or their own account.
    """
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    requester_is_super = is_super_admin(current_user.role)
    if is_super_admin(target_user.role) and not requester_is_super:
        raise HTTPException(status_code=403, detail="Forbidden: Cannot modify Super Admin status.")

    if target_user.id == current_user.id and not req.is_active:
        raise HTTPException(status_code=400, detail="Cannot disable your own user account.")

    target_user.is_active = bool(req.is_active)
    db.commit()
    db.refresh(target_user)

    status_str = "Active" if target_user.is_active else "Disabled"
    log_security_event(
        db=db,
        action=f"User status updated: {target_user.email} set to '{status_str}' by {current_user.email}",
        decision="USER_STATUS_UPDATED",
        user=current_user,
        endpoint=f"/api/auth/users/{user_id}/status",
        method="PATCH"
    )

    return UserResponse(
        id=target_user.id,
        email=target_user.email,
        full_name=target_user.full_name,
        role=target_user.role,
        is_active=target_user.is_active,
        rank=get_role_rank(target_user.role, db),
        permissions=get_user_permissions(db, target_user)
    )
