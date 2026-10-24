import os
import sys
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.config import settings
from app.database import Base
from app.models import (
    User, UserMarketAssignment, Node, Customer, UsageRecord, Resource, Ticket, Invoice,
    Recommendation, AuditLog, ApprovalCallLog, FieldAssignment,
    TrackingSession, LocationPing, FieldOtp, Role, Permission, RolePermission,
    NetworkDevice, OLTPort, NetworkLink, NetworkAlarm,
    DeviceMetricThreshold, DeviceMetricRecord
)

config = context.config

# Allow explicit environment variable override for isolated test/local migrations
target_url = os.getenv("ALEMBIC_DATABASE_URL") or os.getenv("DATABASE_URL") or settings.DATABASE_URL
if target_url.startswith("postgres://"):
    target_url = target_url.replace("postgres://", "postgresql://", 1)

# Remote Supabase Migration Safety Guard
is_remote_supabase = any(host in target_url.lower() for host in ("supabase.com", "supabase.co", "pooler.supabase"))
if is_remote_supabase and os.getenv("PREVENT_REMOTE_MIGRATIONS", "1") == "1":
    if not os.getenv("ALLOW_REMOTE_MIGRATIONS_CONFIRMED"):
        raise RuntimeError(
            "SAFETY VIOLATION: Execution of migrations against shared or remote Supabase is blocked. "
            "To migrate an isolated local database, specify ALEMBIC_DATABASE_URL or set DATABASE_URL."
        )

config.set_main_option("sqlalchemy.url", target_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
