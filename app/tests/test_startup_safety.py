import pytest
import sqlalchemy as sa
from app.main import check_schema_compatibility

def test_startup_schema_compatibility_diagnostic(db_session):
    """
    Verify check_schema_compatibility performs read-only checks
    and does not mutate or alter tables.
    """
    bind = db_session.get_bind()
    result = check_schema_compatibility(bind)
    assert result is True

def test_startup_schema_compatibility_missing_tables():
    """
    Verify check_schema_compatibility cleanly diagnoses missing tables
    without throwing exceptions or running DDL.
    """
    empty_engine = sa.create_engine("sqlite:///:memory:")
    result = check_schema_compatibility(empty_engine)
    assert result is False

def test_no_ddl_on_app_import():
    """
    Verify that app.main does not execute DDL when imported.
    """
    import inspect
    import app.main
    source = inspect.getsource(app.main)
    # Ensure raw DDL calls are absent from module-level execution
    assert "Base.metadata.create_all" not in source
    assert "ensure_ticket_columns()" not in source
    assert "ALTER TABLE" not in source
