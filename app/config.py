import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = 'PMRG AI Overlay for Jeebr Internet'
    API_V1_STR: str = '/api'
    
    # PostgreSQL Configuration
    DATABASE_URL: str = os.getenv(
        'DATABASE_URL', 
        'postgresql://postgres:root@localhost:5432/jeebr_db'
    )
    
    # JWT Auth Configuration
    SECRET_KEY: str = os.getenv('SECRET_KEY', 'pmrg-jeebr-secret-super-key-2026-governed-ai')
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

    class Config:
        case_sensitive = True

settings = Settings()
