import math
import time
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime
import urllib.request
import urllib.parse
import json

from app.config import settings

logger = logging.getLogger(__name__)

class RouteResult:
    """Standardized route calculation result."""
    def __init__(
        self,
        distance_meters: float,
        duration_seconds: int,
        eta_minutes: int,
        provider: str,
        polyline: Optional[str] = None,
        waypoints: Optional[List[Dict[str, float]]] = None,
        cached: bool = False
    ):
        self.distance_meters = distance_meters
        self.duration_seconds = duration_seconds
        self.eta_minutes = eta_minutes
        self.provider = provider
        self.polyline = polyline
        self.waypoints = waypoints or []
        self.cached = cached

    def to_dict(self) -> Dict[str, Any]:
        return {
            "distance_meters": round(self.distance_meters, 1),
            "distance_km": round(self.distance_meters / 1000.0, 2),
            "duration_seconds": self.duration_seconds,
            "eta_minutes": self.eta_minutes,
            "provider": self.provider,
            "polyline": self.polyline,
            "waypoints": self.waypoints,
            "cached": self.cached
        }


class RoutingProvider(ABC):
    """Abstract interface for routing and ETA calculation providers."""

    @abstractmethod
    def calculate_route(
        self,
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float
    ) -> RouteResult:
        """Calculate road route between origin and destination."""
        pass


def calculate_haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate great-circle distance between two points in meters."""
    r = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


class FallbackRoutingProvider(RoutingProvider):
    """
    Resilient built-in fallback router using Haversine distance,
    Indian urban tortuosity index (1.35x), and metropolitan transit speeds (25 km/h).
    Requires zero external network dependencies and never fails.
    """

    def __init__(self, urban_factor: float = 1.35, avg_speed_kmh: float = 25.0):
        self.urban_factor = urban_factor
        self.avg_speed_kmh = avg_speed_kmh

    def calculate_route(
        self,
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float
    ) -> RouteResult:
        crow_dist_m = calculate_haversine_distance_meters(origin_lat, origin_lng, dest_lat, dest_lng)
        
        # If within 50 meters, consider arrived
        if crow_dist_m < 50.0:
            return RouteResult(
                distance_meters=crow_dist_m,
                duration_seconds=0,
                eta_minutes=0,
                provider="fallback_haversine",
                waypoints=[
                    {"lat": origin_lat, "lng": origin_lng},
                    {"lat": dest_lat, "lng": dest_lng}
                ]
            )

        road_dist_m = crow_dist_m * self.urban_factor
        speed_mps = (max(self.avg_speed_kmh, 5.0) * 1000.0) / 3600.0
        duration_sec = int(round(road_dist_m / speed_mps))
        eta_mins = max(1, int(round(duration_sec / 60.0)))

        return RouteResult(
            distance_meters=road_dist_m,
            duration_seconds=duration_sec,
            eta_minutes=eta_mins,
            provider="fallback_haversine",
            waypoints=[
                {"lat": origin_lat, "lng": origin_lng},
                {"lat": dest_lat, "lng": dest_lng}
            ]
        )


class GoogleRoutingProvider(RoutingProvider):
    """
    Google Maps Platform Directions API provider.
    Computes precise real-time road routes, live traffic delays, and turn-by-turn geometry.
    Gracefully catches any network, billing, quota, or key errors and raises for fallback handling.
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://maps.googleapis.com/maps/api/directions/json"

    def calculate_route(
        self,
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float
    ) -> RouteResult:
        if not self.api_key or self.api_key.startswith("AIzaSyFake"):
            raise ValueError("Google Maps API key not configured or simulated.")

        params = {
            "origin": f"{origin_lat},{origin_lng}",
            "destination": f"{dest_lat},{dest_lng}",
            "mode": "driving",
            "key": self.api_key
        }
        url = f"{self.base_url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "SentinelOS-Routing/1.0"})

        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError(f"Google Directions API returned HTTP {response.status}")
                data = json.loads(response.read().decode("utf-8"))

            status = data.get("status")
            if status != "OK":
                error_msg = data.get("error_message", f"Status: {status}")
                raise RuntimeError(f"Google Directions API error: {error_msg}")

            route = data["routes"][0]
            leg = route["legs"][0]
            dist_m = float(leg["distance"]["value"])
            duration_s = int(leg["duration"]["value"])
            eta_m = max(1, int(round(duration_s / 60.0)))
            polyline = route.get("overview_polyline", {}).get("points")

            return RouteResult(
                distance_meters=dist_m,
                duration_seconds=duration_s,
                eta_minutes=eta_m,
                provider="google_maps",
                polyline=polyline
            )
        except Exception as e:
            logger.warning(f"[ROUTING_PROVIDER_FAILURE] Google Directions request failed: {e}")
            raise


class RoutingService:
    """
    Unified Routing Service managing route calculation, geographical bucketing,
    caching, cost control, and graceful multi-tier fallbacks.
    """

    def __init__(
        self,
        google_api_key: Optional[str] = None,
        cache_ttl_seconds: int = 300,
        min_refresh_distance_meters: float = 150.0
    ):
        self.google_provider = GoogleRoutingProvider(google_api_key) if google_api_key else None
        self.fallback_provider = FallbackRoutingProvider()
        self.cache_ttl_seconds = cache_ttl_seconds
        self.min_refresh_distance_meters = min_refresh_distance_meters
        # In-memory LRU-like cache: key -> (timestamp, RouteResult, (origin_lat, origin_lng))
        self._cache: Dict[str, Tuple[float, RouteResult, Tuple[float, float]]] = {}

    def _make_bucket_key(self, origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float) -> str:
        # Bucket to ~100m precision (3 decimal places) to eliminate redundant calls
        return f"{round(origin_lat, 3)}:{round(origin_lng, 3)}->{round(dest_lat, 3)}:{round(dest_lng, 3)}"

    def calculate_route(
        self,
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float,
        force_refresh: bool = False
    ) -> RouteResult:
        cache_key = self._make_bucket_key(origin_lat, origin_lng, dest_lat, dest_lng)
        now = time.time()

        # 1. Check Cache
        if not force_refresh and cache_key in self._cache:
            cached_time, cached_result, last_origin = self._cache[cache_key]
            # Check TTL
            if (now - cached_time) < self.cache_ttl_seconds:
                # Check if moved significantly from last calculated origin
                moved_dist = calculate_haversine_distance_meters(origin_lat, origin_lng, last_origin[0], last_origin[1])
                if moved_dist < self.min_refresh_distance_meters:
                    return RouteResult(
                        distance_meters=cached_result.distance_meters,
                        duration_seconds=cached_result.duration_seconds,
                        eta_minutes=cached_result.eta_minutes,
                        provider=cached_result.provider,
                        polyline=cached_result.polyline,
                        waypoints=cached_result.waypoints,
                        cached=True
                    )

        # 2. Try Google Maps Provider
        if self.google_provider:
            try:
                res = self.google_provider.calculate_route(origin_lat, origin_lng, dest_lat, dest_lng)
                self._cache[cache_key] = (now, res, (origin_lat, origin_lng))
                return res
            except Exception as e:
                logger.info(f"Falling back to SentinelOS heuristic router: {e}")

        # 3. Fallback Provider (Always Works)
        res = self.fallback_provider.calculate_route(origin_lat, origin_lng, dest_lat, dest_lng)
        self._cache[cache_key] = (now, res, (origin_lat, origin_lng))
        return res


# Global singleton instance
routing_service = RoutingService(
    google_api_key=getattr(settings, 'GOOGLE_MAPS_API_KEY', None) or None
)
