import os
from pydantic_settings import BaseSettings

INSECURE_DEFAULT_KEYS = {
    'super-secret-jwt-key-for-jeebr-poc-change-in-production',
    'super-secret-key-2026-govern-452154215',
    'secret',
    'changeme',
    'jwt-secret-key'
}

class Settings(BaseSettings):
    PROJECT_NAME: str = 'SentinelOS — Enterprise AI Governance (PMRG Solution LLP)'
    API_V1_STR: str = '/api'
    ENVIRONMENT: str = os.getenv('ENVIRONMENT', 'development')  # 'production', 'staging', 'development'
    
    # PostgreSQL Configuration (Supabase / Production)
    DATABASE_URL: str = os.getenv('DATABASE_URL', '')
    
    # JWT Auth Configuration
    SECRET_KEY: str = os.getenv('SECRET_KEY', 'super-secret-key-2026-govern-452154215')
    ALGORITHM: str = 'HS256'
    ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv('ACCESS_TOKEN_EXPIRE_MINUTES', '60'))  # 60 minutes
    
    # Cookie security configuration
    COOKIE_SECURE: bool = os.getenv('COOKIE_SECURE', 'false').lower() in ('true', '1', 'yes')
    COOKIE_SAMESITE: str = os.getenv('COOKIE_SAMESITE', 'lax')  # 'lax', 'strict', 'none'
    
    # CORS - Explicit origin allowlist (Wildcards forbidden with credentials)
    BACKEND_CORS_ORIGINS: list[str] = [
        origin.strip() for origin in os.getenv(
            'CORS_ORIGINS',
            'http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000,https://sentinel-os-beta.vercel.app'
        ).split(',') if origin.strip()
    ]

    model_config = {
        "case_sensitive": True,
        "env_file": ".env",
        "extra": "ignore"
    }

settings = Settings()

# Enforce production secret key integrity
is_production = settings.ENVIRONMENT.lower() in ('production', 'prod')
if is_production:
    if not settings.SECRET_KEY or settings.SECRET_KEY in INSECURE_DEFAULT_KEYS or len(settings.SECRET_KEY) < 32:
        raise RuntimeError(
            "FATAL SECURITY VIOLATION: In production, a cryptographically secure SECRET_KEY (minimum 32 characters) "
            "must be provided via environment variables. Known default or insecure fallback secrets are strictly prohibited."
        )
