from typing import Optional, Dict, Any, List, Tuple
from fastapi import APIRouter, Header, Query, Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func
from jose import jwt, JWTError

from app.config import settings
from app.database import get_db
from app.models import User, Resource, Customer
from app.services.rbac_service import is_super_admin, normalize_role_name

security = HTTPBearer(auto_error=False)

class MarketConfig(BaseModel):
    id: str
    city: str
    label: str
    localities: List[str]
    default_node: str
    accent_color: str
    description: str

MARKETS: Dict[str, MarketConfig] = {
    "mumbai": MarketConfig(
        id="mumbai",
        city="Mumbai",
        label="Market: Mumbai",
        localities=[
            "Bandra West", "Andheri East", "BKC", "Powai", 
            "Lower Parel", "Dadar", "Malad West", "Thane West",
            "Juhu", "Worli", "Borivali", "Ghatkopar"
        ],
        default_node="OLT-BND-01",
        accent_color="#2563EB",
        description="Mumbai Metropolitan Region Optical & FTTH Network"
    ),
    "kolkata": MarketConfig(
        id="kolkata",
        city="Kolkata",
        label="Market: Kolkata",
        localities=[
            "Salt Lake Sector V", "Park Street", "New Town", "Ballygunge",
            "Howrah", "Jadavpur", "Behala", "Dum Dum",
            "Alipore", "Gariahat", "Rajarhat", "Shyambazar"
        ],
        default_node="OLT-SLK-01",
        accent_color="#0D9488",
        description="Kolkata Metropolitan & IT Corridor Optical Network"
    )
}

USER_MARKET_ASSIGNMENTS: Dict[str, Any] = {
    "noc@pmrg.in": {"markets": ["mumbai", "kolkata"], "primary": "mumbai"},
    "care@pmrg.in": {"markets": ["mumbai"], "primary": "mumbai"},
    "revenue@pmrg.in": {"markets": ["mumbai"], "primary": "mumbai"},
    "executive@pmrg.in": {"markets": ["mumbai", "kolkata"], "primary": "mumbai"},
    "client@pmrgsolution.com": {"markets": ["mumbai"], "primary": "mumbai"},
    "admin@pmrg.in": {"markets": ["mumbai", "kolkata"], "primary": "mumbai"},
    # Explicit server-side assignments for legitimate operational test accounts
    "admin_prov_test@pmrg.in": {"markets": ["mumbai", "kolkata"], "primary": "mumbai"},
    "admin@test.sentinel": {"markets": ["mumbai", "kolkata"], "primary": "mumbai"},
    "noc@test.sentinel": {"markets": ["mumbai", "kolkata"], "primary": "mumbai"},
}

def get_user_from_request(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
    db: Session
) -> Optional[User]:
    """Helper to extract and authenticate user from Bearer header, cookie, or query param."""
    token = None
    if credentials and credentials.credentials:
        token = credentials.credentials
    elif request:
        cookie_token = request.cookies.get("access_token")
        if cookie_token:
            token = cookie_token
        else:
            query_token = request.query_params.get("token")
            if query_token:
                token = query_token

    if not token:
        return None

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            options={"verify_signature": True, "verify_exp": True, "verify_iat": True}
        )
        email = payload.get("sub")
        if not email:
            return None
        return db.query(User).filter(User.email == email, User.is_active == True).first()
    except Exception:
        return None

def get_user_authorized_markets(user: Optional[User], db: Session) -> Tuple[List[str], Optional[str]]:
    """
    Resolve trusted server-side authorized markets and primary market for a user.
    Returns:
        (authorized_markets: List[str], primary_market: Optional[str])
    """
    if not user:
        return ([], None)

    # 1. SUPER_ADMIN: Universal access to all registered markets
    if is_super_admin(user.role):
        return (list(MARKETS.keys()), None)

    email_clean = user.email.lower().strip()

    # 2. Check explicit server-side user assignments dictionary
    if email_clean in USER_MARKET_ASSIGNMENTS:
        cfg = USER_MARKET_ASSIGNMENTS[email_clean]
        if isinstance(cfg, dict):
            return (cfg.get("markets", []), cfg.get("primary"))
        elif isinstance(cfg, list):
            return (cfg, cfg[0] if len(cfg) == 1 else None)

    # 3. Check Resource table (Field Engineers & Internal Technicians)
    # SEC-S1-02: Query by canonical foreign key Resource.user_id == user.id first
    resources = db.query(Resource).filter(Resource.user_id == user.id).all()

    if not resources:
        # SEC-S1-02: Email-based migration fallback ONLY when no user_id assignment exists.
        # Strict validation:
        # a) Fail closed if another Resource row with this email is bound to a different user_id
        conflicting = db.query(Resource).filter(
            func.lower(Resource.email) == email_clean,
            Resource.user_id.is_not(None),
            Resource.user_id != user.id
        ).first()

        if not conflicting:
            # b) Only match legacy Resource rows whose user_id IS NULL
            legacy_resources = db.query(Resource).filter(
                func.lower(Resource.email) == email_clean,
                Resource.user_id.is_(None)
            ).all()

            if legacy_resources:
                legacy_markets = list(dict.fromkeys(
                    r.market_id.lower().strip() for r in legacy_resources
                    if r.market_id and r.market_id.lower().strip() in MARKETS
                ))
                # c) If ambiguous (multiple different markets for unassigned legacy records), fail closed
                if len(legacy_markets) == 1:
                    resources = legacy_resources
                # If len(legacy_markets) > 1, mapping is ambiguous -> fail closed (resources remains empty)

    if resources:
        res_markets = list(dict.fromkeys(
            r.market_id.lower().strip() for r in resources
            if r.market_id and r.market_id.lower().strip() in MARKETS
        ))
        if res_markets:
            primary = res_markets[0] if len(res_markets) == 1 else None
            return (res_markets, primary)

    # 4. Check Customer table (Subscribers)
    cust = db.query(Customer).filter(func.lower(Customer.email) == email_clean).first()
    if cust and cust.market_id:
        m = cust.market_id.lower().strip()
        if m in MARKETS:
            return ([m], m)

    # 5. SEC-S1-01: Zero-trust termination - unassigned user has NO authorized markets.
    # Role-only market access fallback is completely removed.
    return ([], None)

def get_current_market(
    request: Request,
    x_market_id: Optional[str] = Header(None, alias="X-Market-Id"),
    market_id: Optional[str] = Query(None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db)
) -> str:
    # Check both Header, Query, and case-insensitive request headers
    raw_market = x_market_id or market_id or request.headers.get("x-market-id")
    chosen = raw_market.strip().lower() if raw_market and raw_market.strip() else None

    # 1. Reject explicitly supplied unknown or unsupported market identifiers with HTTP 400
    if chosen is not None and chosen not in MARKETS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid or unsupported market identifier: '{chosen}'"
        )

    # 2. Extract authenticated user
    user = get_user_from_request(request, credentials, db)

    # SEC-S1-05: Require authentication for market-scoped resource resolution
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required to access market-scoped resources.",
            headers={"WWW-Authenticate": "Bearer"}
        )

    # 3. Preserve existing SUPER_ADMIN privilege model
    if is_super_admin(user.role):
        if chosen is not None:
            return chosen
        return "mumbai"

    # 4. Non-super-admin: Resolve authorized markets from trusted server-side user assignments
    authorized_markets, primary_market = get_user_authorized_markets(user, db)

    # If the request specifies X-Market-Id:
    if chosen is not None:
        if chosen not in authorized_markets:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"User '{user.email}' is not authorized to access market '{chosen}'"
            )
        return chosen

    # If the header is absent:
    if not authorized_markets:
        # User has no authorized market: deny access. Never silently assign a market.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User '{user.email}' has no authorized operational markets."
        )

    if len(authorized_markets) == 1:
        # User has exactly one authorized market: use that market
        return authorized_markets[0]

    # User has multiple authorized markets:
    # If user has a designated primary market, use it:
    if primary_market and primary_market in authorized_markets:
        return primary_market

    # A market-specific operation requires a choice among multiple authorized markets: return HTTP 400
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Multiple markets authorized. Explicit X-Market-Id header required."
    )

router = APIRouter(prefix="/markets", tags=["Markets"])

@router.get("", response_model=List[MarketConfig])
def list_markets():
    return list(MARKETS.values())

@router.get("/{market_id}", response_model=MarketConfig)
def get_market(market_id: str):
    m_id = market_id.lower()
    if m_id in MARKETS:
        return MARKETS[m_id]
    raise HTTPException(status_code=404, detail=f"Market '{market_id}' not found.")
