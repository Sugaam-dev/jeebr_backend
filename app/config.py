import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = 'PMRG AI Overlay for PMRG Internet'
    API_V1_STR: str = '/api'
    
    # PostgreSQL Configuration (Supabase / Production)
    DATABASE_URL: str = os.getenv('DATABASE_URL', '')
    
    # JWT Auth Configuration
    SECRET_KEY: str = os.getenv('SECRET_KEY', 'super-secret-key-2026-govern-452154215')
    ALGORITHM: str = 'HS256'
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours
    
    # CORS
    BACKEND_CORS_ORIGINS: list[str] = [
        'http://localhost:5173',
        'http://127.0.0.1:5173',
        'http://localhost:3000',
        'http://127.0.0.1:3000',
        '*'
    ]

    model_config = {
        "case_sensitive": True,
        "env_file": ".env",
        "extra": "ignore"
    }

settings = Settings()
