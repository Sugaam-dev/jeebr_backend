from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Header, Query
from pydantic import BaseModel

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

def get_current_market(
    x_market_id: Optional[str] = Header(None, alias="X-Market-Id"),
    market_id: Optional[str] = Query(None)
) -> str:
    chosen = (x_market_id or market_id or "mumbai").strip().lower()
    return chosen if chosen in MARKETS else "mumbai"

router = APIRouter(prefix="/markets", tags=["Markets"])

@router.get("", response_model=List[MarketConfig])
def list_markets():
    return list(MARKETS.values())

@router.get("/{market_id}", response_model=MarketConfig)
def get_market(market_id: str):
    m_id = market_id.lower()
    if m_id in MARKETS:
        return MARKETS[m_id]
    return MARKETS["mumbai"]
