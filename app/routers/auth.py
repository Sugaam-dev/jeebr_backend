from app.services.rate_limiter import get_trustworthy_client_ip
from datetime import datetime
from typing import List, Optional, Tuple
from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.config import settings
from app.database import get_db
from app.models import User, Role, Resource
from app.markets import get_current_market
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

    # Assign default market assignment for self-registered customer/viewer
    from app.models import UserMarketAssignment
    db.add(UserMarketAssignment(user_id=new_user.id, market_id="mumbai", is_primary=True))
    db.commit()

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
    client_ip = get_trustworthy_client_ip(request)
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
    is_prod = settings.ENVIRONMENT.lower() in ("production", "prod")
    if is_prod or not getattr(settings, "ENABLE_DEMO_LOGIN", False):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found"
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

def _build_user_response(u: User, db: Session, target_rank: Optional[int] = None) -> UserResponse:
    from app.markets import get_user_authorized_markets
    auth_markets, primary_m = get_user_authorized_markets(u, db)
    return UserResponse(
        id=u.id,
        email=u.email,
        full_name=u.full_name,
        role=u.role,
        is_active=u.is_active,
        rank=target_rank if target_rank is not None else get_role_rank(u.role, db),
        permissions=get_user_permissions(db, u),
        market_ids=auth_markets,
        primary_market=primary_m
    )

def sync_user_market_assignments(
    user_id: int,
    market_ids: Optional[List[str]],
    primary_market: Optional[str],
    creator: User,
    db: Session
) -> Tuple[List[str], Optional[str]]:
    """
    Validate and persist market assignments in user_market_assignments table.
    Enforces privilege boundary:
    - Super Admin can assign any valid market in MARKETS.
    - Non-Super-Admin can ONLY assign markets that they themselves are authorized for.
    - Unknown or unsupported market identifiers are rejected with HTTP 400.
    """
    from app.markets import MARKETS, get_user_authorized_markets
    from app.models import UserMarketAssignment

    if market_ids is None:
        return ([], None)

    # 1. Clean and validate requested market IDs
    clean_markets = []
    for m in market_ids:
        if not m or not isinstance(m, str):
            continue
        m_clean = m.strip().lower()
        if m_clean not in MARKETS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid or unsupported market identifier: '{m}'"
            )
        if m_clean not in clean_markets:
            clean_markets.append(m_clean)

    # 2. Check privilege boundary
    requester_is_super = is_super_admin(creator.role)
    if not requester_is_super:
        creator_markets, _ = get_user_authorized_markets(creator, db)
        for m in clean_markets:
            if m not in creator_markets:
                log_security_event(
                    db=db,
                    action=f"Privilege escalation attempt by {creator.email}: cannot grant unauthorized market '{m}'",
                    decision="ACCESS_DENIED",
                    user=creator,
                    endpoint="/api/auth/users",
                    method="POST"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Forbidden: You do not have authorization to grant market '{m}'. Authorized markets: {creator_markets}"
                )

    # 3. Resolve primary market
    clean_primary = primary_market.strip().lower() if primary_market and isinstance(primary_market, str) else None
    if clean_primary and clean_primary not in clean_markets:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Primary market '{clean_primary}' must be one of the assigned markets: {clean_markets}"
        )
    if not clean_primary and len(clean_markets) == 1:
        clean_primary = clean_markets[0]

    # 4. Persist in database
    db.query(UserMarketAssignment).filter(UserMarketAssignment.user_id == user_id).delete()
    for m in clean_markets:
        assignment = UserMarketAssignment(
            user_id=user_id,
            market_id=m,
            is_primary=(m == clean_primary),
            created_by_id=creator.id
        )
        db.add(assignment)
    db.commit()

    return (clean_markets, clean_primary)

@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _build_user_response(current_user, db)

@router.get("/users", response_model=List[UserResponse])
def get_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    users = db.query(User).order_by(User.id.asc()).all()
    return [_build_user_response(u, db) for u in users]

@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def admin_create_user(
    req: AdminUserCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"])),
    current_market: str = Depends(get_current_market)
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
    phone_val = (req.phone or "+91 98200 12345").strip()
    new_user = User(
        email=clean_email,
        hashed_password=hash_password(req.password),
        full_name=user_name,
        role=target_role,
        phone=phone_val,
        is_active=bool(req.is_active)
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # 6. Operational Workforce Synchronization:
    # If the user is provisioned as 'Field Engineer', automatically provision/link
    # their Resource record so they appear in technician selection and field dispatch.
    if target_role == "Field Engineer":
        market_val = (req.market_id or current_market or "mumbai").lower().strip()
        if market_val not in ["mumbai", "kolkata"]:
            market_val = "mumbai"
        default_region = "Bandra West" if market_val == "mumbai" else "Salt Lake Sector V"
        region_val = (req.region or default_region).strip()
        coords = (19.0760, 72.8777) if market_val == "mumbai" else (22.5726, 88.3639)

        existing_res = db.query(Resource).filter(
            Resource.market_id == market_val,
            (Resource.user_id == new_user.id) | (func.lower(Resource.email) == clean_email)
        ).first()

        if existing_res:
            existing_res.user_id = new_user.id
            existing_res.name = user_name
            existing_res.phone = phone_val
            existing_res.status = "Available" if new_user.is_active else "Offline"
        else:
            new_res = Resource(
                market_id=market_val,
                name=user_name,
                email=clean_email,
                phone=phone_val,
                resource_type="FIELD",
                region=region_val,
                status="Available" if new_user.is_active else "Offline",
                active_tickets_count=0,
                max_capacity=5,
                user_id=new_user.id,
                current_latitude=coords[0],
                current_longitude=coords[1],
                last_ping_at=datetime.utcnow(),
                location_status="ACTIVE"
            )
            db.add(new_res)
        db.commit()

    # 7. Persistent Market Authorization Synchronization:
    requested_markets = req.market_ids
    if requested_markets is None and req.market_id is not None:
        requested_markets = [req.market_id] if req.market_id.strip() else []

    if requested_markets is not None:
        sync_user_market_assignments(
            user_id=new_user.id,
            market_ids=requested_markets,
            primary_market=req.primary_market or req.market_id,
            creator=current_user,
            db=db
        )

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

    return _build_user_response(new_user, db, target_rank)


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

    # Synchronize linked Resource records if applicable
    linked_resources = db.query(Resource).filter(
        (Resource.user_id == target_user.id) | (func.lower(Resource.email) == target_user.email.lower())
    ).all()

    for res in linked_resources:
        if req.full_name is not None or req.name is not None:
            res.name = target_user.full_name
        if req.phone is not None:
            res.phone = req.phone.strip()
        if req.region is not None:
            res.region = req.region.strip()
        if req.role is not None:
            if target_user.role == "Field Engineer":
                res.status = "Available" if target_user.is_active else "Offline"
            else:
                res.status = "Offline"
        if req.is_active is not None:
            if target_user.role == "Field Engineer":
                res.status = "Available" if target_user.is_active else "Offline"

    if target_user.role == "Field Engineer" and not linked_resources:
        default_region = "Bandra West"
        new_res = Resource(
            market_id=(req.market_id or "mumbai").lower().strip(),
            name=target_user.full_name,
            email=target_user.email.lower(),
            phone=getattr(target_user, "phone", None) or "+91 98200 12345",
            resource_type="FIELD",
            region=req.region or default_region,
            status="Available" if target_user.is_active else "Offline",
            active_tickets_count=0,
            max_capacity=5,
            user_id=target_user.id,
            current_latitude=19.0760,
            current_longitude=72.8777,
            last_ping_at=datetime.utcnow(),
            location_status="ACTIVE"
        )
        db.add(new_res)

    # Synchronize persistent market assignments
    requested_markets = req.market_ids
    if requested_markets is None and req.market_id is not None:
        requested_markets = [req.market_id] if req.market_id.strip() else []

    if requested_markets is not None:
        if target_user.id == current_user.id and not requester_is_super:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: Administrators cannot modify their own market assignments."
            )
        sync_user_market_assignments(
            user_id=target_user.id,
            market_ids=requested_markets,
            primary_market=req.primary_market or req.market_id,
            creator=current_user,
            db=db
        )

    db.commit()
    db.refresh(target_user)

    return _build_user_response(target_user, db)


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

    # Sync linked Resource status
    linked_resources = db.query(Resource).filter(
        (Resource.user_id == target_user.id) | (func.lower(Resource.email) == target_user.email.lower())
    ).all()
    for res in linked_resources:
        res.status = "Available" if target_user.is_active else "Offline"

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

    return _build_user_response(target_user, db)
