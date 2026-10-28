from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker
from app.config import settings

db_url = settings.DATABASE_URL
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# Supabase Transaction Pooler: Port 6543 supports high-concurrency transaction mode,
# preventing EMAXCONNSESSION (15 max clients in session mode on port 5432).
if "pooler.supabase.com" in db_url and ":5432/" in db_url:
    db_url = db_url.replace(":5432/", ":6543/")

if db_url.startswith("sqlite"):
    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False}
    )
else:
    parsed_url = make_url(db_url)
    try:
        dialect_cls = parsed_url.get_dialect()
        is_psycopg3 = getattr(dialect_cls, "driver", "") == "psycopg"
    except Exception:
        is_psycopg3 = False

    connect_args = {
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5
    }

    # In PgBouncer / Supavisor Transaction Pooling mode (e.g. Supabase port 6543),
    # client connections are multiplexed across different backend PostgreSQL workers.
    # Psycopg 3's automatic named prepared statements (_pg3_0, _pg3_1, etc.) fail with
    # InvalidSqlStatementName or DuplicatePreparedStatement when statements are routed
    # to different backend workers. Disabling statement preparation (prepare_threshold=None)
    # is required for transaction poolers.
    if is_psycopg3 or "psycopg" in str(parsed_url.drivername):
        connect_args["prepare_threshold"] = None

    engine = create_engine(
        db_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=300,
        pool_timeout=30,
        connect_args=connect_args
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
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
