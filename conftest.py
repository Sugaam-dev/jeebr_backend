import os
import shutil
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 1. Define isolated local SQLite database paths
BASELINE_DB_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "test_baseline.db"))
TEST_DB_FILE = os.path.abspath(os.path.join(os.path.dirname(__file__), "test_backend_isolated.db"))
TEST_DATABASE_URL = f"sqlite:///{TEST_DB_FILE}"

# 2. Safety fail-closed validator
def assert_safe_test_db(url: str):
    forbidden_patterns = ["supabase.co", "supabase.com", "pooler.supabase", "aws-0-"]
    for pattern in forbidden_patterns:
        if pattern in url.lower():
            raise RuntimeError(
                f"CRITICAL TEST ISOLATION FAILURE: Test suite detected remote database URL: {url}. "
                "Execution aborted immediately to prevent mutations to shared/production data."
            )
    if not url.startswith("sqlite"):
        raise RuntimeError(f"CRITICAL TEST ISOLATION FAILURE: Expected isolated SQLite database, got {url}")

assert_safe_test_db(TEST_DATABASE_URL)

# 3. Enforce isolated test environment variables BEFORE importing app modules
os.environ["ENVIRONMENT"] = "testing"
os.environ["PREVENT_REMOTE_MIGRATIONS"] = "1"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ENABLE_DEMO_LOGIN"] = "true"

from app.config import settings
settings.ENVIRONMENT = "testing"
settings.DATABASE_URL = TEST_DATABASE_URL
settings.ENABLE_DEMO_LOGIN = True

import app.database as app_db
import app.seed as app_seed
import app.main as app_main
from app.main import app as fastapi_app

# 4. Create isolated SQLite engine and session factory
test_engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=test_engine)

# 5. Patch app.database, app.seed, and app.main to use isolated engine/session
app_db.engine = test_engine
app_db.SessionLocal = TestingSessionLocal
app_seed.engine = test_engine
app_seed.SessionLocal = TestingSessionLocal
app_main.engine = test_engine

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        try:
            db.rollback()
        except Exception:
            pass
        db.close()

fastapi_app.dependency_overrides[app_db.get_db] = override_get_db

# 6. Session-scoped fixture to create baseline once, and module fixture to reset DB per module
@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    assert_safe_test_db(settings.DATABASE_URL)
    
    # Clean up any stale files
    for p in (BASELINE_DB_FILE, TEST_DB_FILE):
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
                
    # Build baseline database
    baseline_url = f"sqlite:///{BASELINE_DB_FILE}"
    baseline_engine = create_engine(baseline_url, connect_args={"check_same_thread": False})
    baseline_SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=baseline_engine)
    
    app_db.engine = baseline_engine
    app_db.SessionLocal = baseline_SessionLocal
    app_seed.engine = baseline_engine
    app_seed.SessionLocal = baseline_SessionLocal
    
    app_db.Base.metadata.create_all(bind=baseline_engine)
    app_seed.seed_database()
    
    baseline_engine.dispose()
    
    # Initialize active test DB from clean baseline
    shutil.copyfile(BASELINE_DB_FILE, TEST_DB_FILE)
    
    app_db.engine = test_engine
    app_db.SessionLocal = TestingSessionLocal
    app_seed.engine = test_engine
    app_seed.SessionLocal = TestingSessionLocal
    app_main.engine = test_engine
    
    yield
    
    # Teardown
    test_engine.dispose()
    for p in (BASELINE_DB_FILE, TEST_DB_FILE):
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass

@pytest.fixture(scope="module", autouse=True)
def reset_module_database():
    test_engine.dispose()
    if os.path.exists(BASELINE_DB_FILE):
        shutil.copyfile(BASELINE_DB_FILE, TEST_DB_FILE)
    yield

@pytest.fixture
def db_session():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
