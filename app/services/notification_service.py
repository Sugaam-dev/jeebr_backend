"""
app/services/notification_service.py

SentinelOS Real Notification Provider Boundary & Durable Outbox Service.
Decouples customer communication (SMS, WhatsApp, Email) from financial transaction commits.
Supports:
1. Event types: OTP, PAYMENT_CONFIRMATION, OVERDUE_REMINDER, SUSPENSION_WARNING, RESTORATION.
2. Durable PostgreSQL/Supabase outbox queue with status tracking (QUEUED, SENT, DELIVERED, FAILED, RETRYING).
3. Pluggable provider adapter (Twilio / Fast2SMS / Generic REST SMS) failing closed when credentials are absent.
4. Deterministic Simulated Notification Provider for testing and local development.
5. Strict PII masking: Never logs raw OTPs, customer passwords, or unmasked sensitive metadata.
"""

import os
import uuid
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session

from app.models_finance import NotificationOutbox

logger = logging.getLogger("sentinel.notification_service")


class NotificationProviderError(Exception):
    """Raised when notification delivery encounters a provider failure."""
    pass


class NotificationProviderNotConfiguredError(NotificationProviderError):
    """Raised when live notification provider credentials are missing."""
    pass


class BaseNotificationProvider(ABC):
    """Abstract interface for external customer notification gateways."""

    @abstractmethod
    def send(
        self,
        recipient: str,
        message: str,
        event_type: str,
        idempotency_key: str
    ) -> Dict[str, Any]:
        """Dispatches notification and returns provider receipt dictionary."""
        pass

    @abstractmethod
    def is_live_configured(self) -> bool:
        """Checks if production API credentials are configured."""
        pass

    @abstractmethod
    def provider_name(self) -> str:
        """Returns provider identifier name."""
        pass


class SimulatedNotificationProvider(BaseNotificationProvider):
    """
    Deterministic simulated notification gateway.
    Explicitly labeled: is_simulated=True is recorded on all dispatched items.
    """

    def send(
        self,
        recipient: str,
        message: str,
        event_type: str,
        idempotency_key: str
    ) -> Dict[str, Any]:
        msg_id = f"sim_sms_{uuid.uuid4().hex[:12]}"
        # Mask recipient for log privacy
        masked_recip = f"{recipient[:3]}****{recipient[-3:]}" if len(recipient) >= 7 else "***"
        logger.info(
            f"[NOTIFICATION_SIMULATED] Dispatched {event_type} to {masked_recip} | ID: {msg_id}"
        )
        return {
            "success": True,
            "provider_message_id": msg_id,
            "provider_name": "SIMULATED_SMS_GATEWAY",
            "is_simulated": True,
            "status": "DELIVERED",
            "delivered_at": datetime.utcnow().isoformat()
        }

    def is_live_configured(self) -> bool:
        return False

    def provider_name(self) -> str:
        return "SIMULATED_SMS_GATEWAY"


class RealSMSProviderAdapter(BaseNotificationProvider):
    """
    Production-ready SMS provider gateway adapter (Twilio or HTTP REST SMS gateway).
    Fails closed when API keys are unconfigured.
    """

    def __init__(self):
        self.api_key = os.getenv("SMS_PROVIDER_API_KEY", "").strip()
        self.sender_id = os.getenv("SMS_SENDER_ID", "SENTINEL").strip()
        self.endpoint = os.getenv("SMS_PROVIDER_ENDPOINT", "").strip()

    def is_live_configured(self) -> bool:
        return bool(self.api_key and (self.endpoint or os.getenv("TWILIO_ACCOUNT_SID")))

    def send(
        self,
        recipient: str,
        message: str,
        event_type: str,
        idempotency_key: str
    ) -> Dict[str, Any]:
        if not self.is_live_configured():
            raise NotificationProviderNotConfiguredError(
                "Live SMS provider credentials not configured in environment. Delivery blocked."
            )
        try:
            import httpx
            # Example HTTP REST SMS payload contract
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Idempotency-Key": idempotency_key
            }
            payload = {
                "sender": self.sender_id,
                "recipient": recipient,
                "message": message,
                "event_type": event_type
            }
            with httpx.Client(timeout=8.0) as client:
                res = client.post(self.endpoint, json=payload, headers=headers)
                if res.status_code in (200, 201, 202):
                    body = res.json()
                    return {
                        "success": True,
                        "provider_message_id": body.get("message_id", f"sms_{uuid.uuid4().hex[:10]}"),
                        "provider_name": "REAL_SMS_GATEWAY",
                        "is_simulated": False,
                        "status": "SENT"
                    }
                else:
                    raise NotificationProviderError(f"SMS gateway returned HTTP {res.status_code}: {res.text}")
        except Exception as e:
            logger.error(f"[SMS_PROVIDER] Delivery failed: {e}")
            raise NotificationProviderError(f"SMS provider communication failure: {e}")

    def provider_name(self) -> str:
        return "REAL_SMS_GATEWAY"


def get_notification_provider() -> BaseNotificationProvider:
    """Factory returning active notification provider."""
    adapter = RealSMSProviderAdapter()
    if adapter.is_live_configured():
        return adapter
    return SimulatedNotificationProvider()


# ─── Durable Outbox Orchestration ─────────────────────────────────────────────

def enqueue_notification(
    db: Session,
    market_id: str,
    recipient: str,
    channel: str,
    event_type: str,
    message: str,
    customer_id: Optional[int] = None,
    idempotency_key: Optional[str] = None
) -> NotificationOutbox:
    """
    Enqueues a customer notification into the durable database outbox.
    Executed in the calling transaction to ensure durability.
    """
    key = idempotency_key or f"notif_{uuid.uuid4().hex}"
    existing = db.query(NotificationOutbox).filter(NotificationOutbox.idempotency_key == key).first()
    if existing:
        return existing

    provider = get_notification_provider()
    record = NotificationOutbox(
        market_id=market_id,
        customer_id=customer_id,
        recipient=recipient,
        channel=channel,
        event_type=event_type,
        message=message,
        status="QUEUED",
        attempt_count=0,
        max_attempts=3,
        idempotency_key=key,
        is_simulated=not provider.is_live_configured(),
        scheduled_at=datetime.utcnow(),
        created_at=datetime.utcnow()
    )
    db.add(record)
    return record


def process_notification_outbox(db: Session, batch_size: int = 50) -> Dict[str, Any]:
    """
    Processes pending outbox items. Dispatches via provider and tracks retries.
    """
    now = datetime.utcnow()
    pending = db.query(NotificationOutbox).filter(
        NotificationOutbox.status.in_(["QUEUED", "RETRYING"]),
        NotificationOutbox.scheduled_at <= now,
        NotificationOutbox.attempt_count < NotificationOutbox.max_attempts
    ).order_by(NotificationOutbox.created_at.asc()).limit(batch_size).all()

    provider = get_notification_provider()
    succeeded = 0
    failed = 0

    for item in pending:
        item.attempt_count += 1
        try:
            res = provider.send(
                recipient=item.recipient,
                message=item.message,
                event_type=item.event_type,
                idempotency_key=item.idempotency_key
            )
            item.status = "SENT"
            item.sent_at = datetime.utcnow()
            item.last_error = None
            succeeded += 1
        except Exception as e:
            logger.warning(f"[OUTBOX] Failed to send {item.id} (attempt {item.attempt_count}): {e}")
            item.last_error = str(e)[:500]
            if item.attempt_count >= item.max_attempts:
                item.status = "FAILED"
            else:
                item.status = "RETRYING"
                # Exponential backoff: 30s * 2^attempts
                delay_sec = 30 * (2 ** (item.attempt_count - 1))
                item.scheduled_at = now + timedelta(seconds=delay_sec)
            failed += 1

    if pending:
        db.commit()

    return {
        "processed": len(pending),
        "succeeded": succeeded,
        "failed": failed,
        "provider": provider.provider_name(),
        "is_simulated": not provider.is_live_configured()
    }
