from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas import Token, LoginRequest, SignupRequest, UserResponse
from app.auth import (
    verify_password, hash_password, create_access_token, get_current_user,
    require_roles, check_login_rate_limit, record_failed_login,
    clear_failed_logins, log_security_event
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

    # 4. Normalize role: public registration CANNOT create Admin or privileged accounts
    # Any public signup is strictly forced to 'Viewer' role
    user_role = "Viewer"

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

    # 6. Issue access token and set HttpOnly cookie
    token = create_access_token({"sub": new_user.email, "role": new_user.role})
    _set_auth_cookie(response, token)
    return Token(
        access_token=token,
        role=new_user.role,
        user_name=new_user.full_name,
        email=new_user.email
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
        email=user.email
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
    valid_roles = ["Executive", "NOC", "Care", "Revenue", "Admin"]
    matched_role = next((r for r in valid_roles if r.lower() == role.lower()), None)
    if not matched_role:
        raise HTTPException(status_code=400, detail=f"Invalid demo role. Valid roles: {valid_roles}")

    user = db.query(User).filter(User.role == matched_role).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"No seeded user found for role {matched_role}")

    token = create_access_token({"sub": user.email, "role": user.role})
    _set_auth_cookie(response, token)
    return Token(
        access_token=token,
        role=user.role,
        user_name=user.full_name,
        email=user.email
    )

@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user

@router.get("/users", response_model=List[UserResponse])
def get_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_roles(["Admin"]))
):
    return db.query(User).order_by(User.id.asc()).all()

