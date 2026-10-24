import os
import pytest
from app.config import settings
from conftest import assert_safe_test_db, TEST_DATABASE_URL

def test_database_url_is_strictly_isolated():
    """
    Ensure the active test DATABASE_URL is SQLite and not a remote database.
    """
    assert settings.ENVIRONMENT == "testing"
    assert settings.DATABASE_URL.startswith("sqlite")
    # Must pass safety assertion without raising RuntimeError
    assert_safe_test_db(settings.DATABASE_URL)

def test_remote_supabase_url_fails_closed():
    """
    Verify that assert_safe_test_db strictly fails closed when given a remote Supabase URL.
    """
    fake_remote_urls = [
        "postgresql://postgres:pass@aws-0-ap-south-1.pooler.supabase.com:6543/postgres",
        "postgresql://postgres:pass@db.xyz.supabase.co:5432/postgres",
        "postgres://user:token@db.example.supabase.com:5432/postgres"
    ]
    for remote_url in fake_remote_urls:
        with pytest.raises(RuntimeError) as exc_info:
            assert_safe_test_db(remote_url)
        assert "CRITICAL TEST ISOLATION FAILURE" in str(exc_info.value)

def test_db_session_writes_to_isolated_database(db_session):
    """
    Verify that database writes during tests target the isolated SQLite database engine.
    """
    from app.models import User
    bind = db_session.get_bind()
    assert "sqlite" in bind.dialect.name.lower()
    
    # Query user count to ensure isolated schema is functional
    users = db_session.query(User).all()
    assert len(users) > 0
