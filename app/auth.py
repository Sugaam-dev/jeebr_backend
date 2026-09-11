import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from app.config import settings
from app.database import get_db
from app.models import User
from app.schemas import TokenData

security = HTTPBearer(auto_error=False)

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac(
        'sha256', 
        password.encode('utf-8'), 
        salt.encode('utf-8'), 
        100000
    ).hex()
    return f"{salt}:{hashed}"

def verify_password(plain_password: str, stored_password: str) -> bool:
    try:
        salt, hashed = stored_password.split(':')
        check = hashlib.pbkdf2_hmac(
            'sha256', 
            plain_password.encode('utf-8'), 
            salt.encode('utf-8'), 
            100000
        ).hex()
        return secrets.compare_digest(check, hashed)
    except Exception:
        return False

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({
        'exp': expire,
        'iat': now
    })
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    # 1. Prefer Authorization: Bearer <token>, fallback to HttpOnly cookie "access_token"
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    else:
        cookie_token = request.cookies.get("access_token")
        if cookie_token:
            token = cookie_token

    if not token:
        raise credentials_exception
    
    try:
        # Strictly enforce HS256 algorithm validation; reject 'none' or unexpected algorithms
        payload = jwt.decode(
            token, 
            settings.SECRET_KEY, 
            algorithms=[settings.ALGORITHM],
            options={"verify_signature": True, "verify_exp": True, "verify_iat": True}
        )
        email: str = payload.get("sub")
        if not email:
            raise credentials_exception
        token_data = TokenData(email=email, role=payload.get("role"))
    except JWTError:
        raise credentials_exception

    # Always validate user existence and active status against trusted database state
    user = db.query(User).filter(User.email == token_data.email).first()
    if user is None or not user.is_active:
        raise credentials_exception
    return user

SUPPORTED_ROLES = ["Executive", "NOC", "Care", "Revenue", "Admin", "Viewer"]

# In-memory rate limiting tracker for login attempts (composite key: IP + email)
_FAILED_ATTEMPTS: dict[str, list[datetime]] = {}
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_MINUTES = 5

def check_login_rate_limit(key: str) -> None:
    now = datetime.now(timezone.utc)
    attempts = _FAILED_ATTEMPTS.get(key, [])
    # Filter attempts within lockout window
    recent = [t for t in attempts if (now - t).total_seconds() < _LOCKOUT_MINUTES * 60]
    _FAILED_ATTEMPTS[key] = recent
    if len(recent) >= _MAX_FAILED_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed login attempts. Please try again after {_LOCKOUT_MINUTES} minutes."
        )

def record_failed_login(key: str) -> None:
    now = datetime.now(timezone.utc)
    attempts = _FAILED_ATTEMPTS.get(key, [])
    attempts.append(now)
    _FAILED_ATTEMPTS[key] = attempts

def clear_failed_logins(key: str) -> None:
    _FAILED_ATTEMPTS.pop(key, None)

def log_security_event(
    db: Optional[Session] = None,
    action: str = "",
    decision: str = "",
    user: Optional[User] = None,
    endpoint: Optional[str] = None,
    method: Optional[str] = None,
    details: Optional[dict] = None,
    market_id: str = "mumbai"
) -> None:
    session = None
    own_session = False
    try:
        from app.models import AuditLog
        from app.database import SessionLocal
        if db is not None:
            session = db
        else:
            session = SessionLocal()
            own_session = True

        audit = AuditLog(
            market_id=market_id,
            recommendation_id=None,
            source_module="Security & RBAC",
            action_taken=action[:200],
            decision=decision,
            user_id=user.id if user else None,
            user_name=user.full_name if user else "Unauthenticated / System",
            user_role=user.role if user else "None",
            confidence_score=1.0,
            original_signals={"endpoint": endpoint, "method": method, **(details or {})},
            execution_result={"decision": decision, "timestamp": datetime.utcnow().isoformat()},
            timestamp=datetime.utcnow()
        )
        session.add(audit)
        session.commit()
    except Exception as e:
        if session:
            try:
                session.rollback()
            except Exception:
                pass
    finally:
        if own_session and session:
            session.close()

def require_roles(allowed_roles: List[str]):
    def role_checker(
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
    ) -> User:
        if current_user.role == "Admin" or current_user.role in allowed_roles:
            return current_user
        
        # Log ACCESS_DENIED security event
        log_security_event(
            db=db,
            action=f"Unauthorized access attempt to restricted endpoint by role '{current_user.role}'",
            decision="ACCESS_DENIED",
            user=current_user,
            details={"allowed_roles": allowed_roles, "user_role": current_user.role}
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access forbidden for role '{current_user.role}'. Requires one of: {allowed_roles}"
        )
    return role_checker

def require_not_viewer(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> User:
    """Enforce that Viewer account cannot perform mutation actions."""
    if current_user.role == "Viewer":
        log_security_event(
            db=db,
            action="Viewer mutation attempt blocked (read-only enforcement)",
            decision="ACCESS_DENIED",
            user=current_user,
            details={"reason": "Viewer role is strictly read-only"}
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: Viewer account has read-only permissions."
        )
    return current_user
