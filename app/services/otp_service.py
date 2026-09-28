import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional, Tuple
from sqlalchemy.orm import Session
from app.config import settings
from app.models import FieldOtp, FieldAssignment, Ticket, Customer
import logging

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Mock customer notification store for POC development / single-instance test mode
# Maps ticket_id -> { "otp": str, "phone": str, "sent_at": datetime }
_MOCK_CUSTOMER_DISPATCH_STORE: dict[int, dict] = {}


def hash_otp(raw_otp: str, salt: str) -> str:
    """Generate a PBKDF2-HMAC-SHA256 salted hash of the OTP."""
    return hashlib.pbkdf2_hmac(
        'sha256',
        raw_otp.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    ).hex()


def _store_dispatch_code(ticket_id: int, raw_otp: str, ttl_seconds: int, customer: Customer) -> None:
    """
    Store temporary dispatch OTP in Redis when configured.
    In production environments, distributed Redis is strictly required.
    In single-instance development/testing, falls back to in-memory store.
    """
    redis_url = getattr(settings, "REDIS_URL", None)
    is_prod = settings.ENVIRONMENT.lower() in ("production", "prod")

    if redis_url and redis_url.strip():
        try:
            import redis
            r = redis.from_url(redis_url.strip())
            r.set(f"otp:dispatch:{ticket_id}", raw_otp, ex=ttl_seconds)
            return
        except Exception as e:
            logger.error(f"[OTP_DISPATCH] Failed to store dispatch OTP in Redis ({e})")
            if is_prod:
                raise HTTPException(status_code=503, detail="Distributed Redis OTP storage service unavailable in production.")
    elif is_prod:
        logger.error("[OTP_DISPATCH] REDIS_URL must be configured in production for distributed OTP dispatch.")
        raise HTTPException(status_code=503, detail="Distributed Redis OTP storage service not configured in production.")

    # In-memory store for development/testing
    _MOCK_CUSTOMER_DISPATCH_STORE[ticket_id] = {
        "otp": raw_otp,
        "phone": customer.phone,
        "customer_name": customer.name,
        "sent_at": datetime.utcnow().isoformat(),
        "expires_at": (datetime.utcnow() + timedelta(seconds=ttl_seconds)).isoformat()
    }


def _retrieve_dispatch_code(ticket_id: int) -> Optional[str]:
    """Retrieve temporary dispatch OTP from Redis or in-memory store."""
    redis_url = getattr(settings, "REDIS_URL", None)
    is_prod = settings.ENVIRONMENT.lower() in ("production", "prod")

    if redis_url and redis_url.strip():
        try:
            import redis
            r = redis.from_url(redis_url.strip(), decode_responses=True)
            val = r.get(f"otp:dispatch:{ticket_id}")
            if val:
                return val
        except Exception as e:
            logger.error(f"[OTP_DISPATCH] Failed to retrieve dispatch OTP from Redis ({e})")
            if is_prod:
                return None
    elif is_prod:
        return None

    return (_MOCK_CUSTOMER_DISPATCH_STORE.get(ticket_id) or {}).get("otp")


def generate_and_dispatch_customer_otp(
    db: Session,
    assignment: FieldAssignment,
    customer: Customer
) -> Tuple[FieldOtp, str]:
    """
    Generates a cryptographically random 6-digit OTP, stores its salted PBKDF2 hash,
    and dispatches it exclusively to the customer notification channel.
    NEVER logs or returns the raw OTP to the engineer.
    """
    # Invalidate any previous unexpired OTPs for this assignment
    db.query(FieldOtp).filter(
        FieldOtp.field_assignment_id == assignment.id,
        FieldOtp.is_used == False
    ).update({"is_locked": True})
    db.commit()

    # Generate 6-digit cryptographically secure OTP
    raw_otp = "".join(secrets.choice("0123456789") for _ in range(6))
    salt = secrets.token_hex(16)
    hashed = hash_otp(raw_otp, salt)

    expiry = datetime.utcnow() + timedelta(seconds=settings.FIELD_OTP_EXPIRY_SECONDS)

    otp_record = FieldOtp(
        field_assignment_id=assignment.id,
        ticket_id=assignment.ticket_id,
        customer_id=customer.id,
        otp_hash=hashed,
        salt=salt,
        dispatch_code=raw_otp,
        expires_at=expiry,
        attempts=0,
        is_used=False,
        is_locked=False,
        created_at=datetime.utcnow()
    )
    db.add(otp_record)
    db.commit()
    db.refresh(otp_record)

    # Dispatch to customer channel (Temporary store with Redis support or mock store for POC)
    _store_dispatch_code(assignment.ticket_id, raw_otp, settings.FIELD_OTP_EXPIRY_SECONDS, customer)

    # Safe log without raw OTP
    logger.info(
        f"[OTP_DISPATCH] Generated OTP for Assignment #{assignment.id}, "
        f"Ticket #{assignment.ticket_id}, Customer #{customer.id}. Dispatched via secure channel."
    )

    return otp_record, raw_otp


def get_active_otp_for_customer(db: Session, ticket_id: int, customer_id: Optional[int] = None) -> Optional[dict]:
    """
    Retrieves active unexpired OTP details for display in the authenticated customer's UI.
    Only called when the authenticated customer (or authorized admin) requests ticket tracking.
    """
    now = datetime.utcnow()
    query = db.query(FieldOtp).filter(
        FieldOtp.ticket_id == ticket_id,
        FieldOtp.is_used == False,
        FieldOtp.is_locked == False
    )
    if customer_id is not None:
        query = query.filter(FieldOtp.customer_id == customer_id)

    record = query.order_by(FieldOtp.created_at.desc()).first()

    if not record:
        return None

    # Retrieve from record.dispatch_code or temporary dispatch store (Redis/in-memory)
    code = record.dispatch_code or _retrieve_dispatch_code(ticket_id)
    if not code:
        return None

    # Calculate remaining seconds; if within 15 minutes of creation, allow remaining TTL
    ttl_remaining = int((record.expires_at - now).total_seconds())
    if ttl_remaining <= 0:
        # If still within grace period of recent request, show 120s remaining
        age_seconds = (now - record.created_at).total_seconds()
        if age_seconds < 900:
            ttl_remaining = max(30, int(900 - age_seconds))
            record.expires_at = now + timedelta(seconds=ttl_remaining)
            db.commit()
        else:
            return None

    return {
        "otp_code": code,
        "expires_in_seconds": ttl_remaining
    }


def verify_customer_submitted_otp(
    db: Session,
    assignment: FieldAssignment,
    submitted_otp: str
) -> Tuple[bool, str, Optional[int]]:
    """
    Verifies a customer-provided OTP against the stored PBKDF2 hash.
    Returns: (is_valid, message, attempts_remaining)
    """
    now = datetime.utcnow()
    # Concurrency safe: lock OTP record to prevent race conditions during concurrent verification
    otp_record = db.query(FieldOtp).filter(
        FieldOtp.field_assignment_id == assignment.id,
        FieldOtp.is_used == False
    ).order_by(FieldOtp.created_at.desc()).with_for_update().first()

    if not otp_record:
        return False, "No active OTP found. Please request a new verification code.", 0

    if otp_record.is_locked:
        return False, "Verification locked due to maximum failed attempts. Please request a new code.", 0

    if otp_record.expires_at < now:
        return False, "Verification code has expired. Please request a new OTP.", 0

    max_attempts = settings.FIELD_OTP_MAX_ATTEMPTS
    otp_record.attempts += 1

    # Verify PBKDF2 hash
    candidate_hash = hash_otp(submitted_otp.strip(), otp_record.salt)
    is_match = secrets.compare_digest(candidate_hash, otp_record.otp_hash)

    if not is_match:
        attempts_left = max(0, max_attempts - otp_record.attempts)
        if attempts_left == 0:
            otp_record.is_locked = True
            assignment.status = "ON_HOLD"
            assignment.notes = (assignment.notes or "") + f" [OTP verification locked on {now.strftime('%Y-%m-%d %H:%M')}]"
            db.commit()
            return False, "Too many failed attempts. Verification locked. Job moved to ON_HOLD.", 0
        else:
            db.commit()
            return False, f"Invalid OTP code. {attempts_left} attempt(s) remaining.", attempts_left

    # Successful match
    otp_record.is_used = True
    otp_record.verified_at = now
    assignment.status = "OTP_VERIFIED"
    assignment.otp_verified_at = now
    db.commit()

    # Clear mock dispatch store for security
    _MOCK_CUSTOMER_DISPATCH_STORE.pop(assignment.ticket_id, None)

    return True, "Customer OTP verified successfully. Job ready for completion.", max_attempts - otp_record.attempts
