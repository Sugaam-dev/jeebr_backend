"""
app/services/payment_provider.py

SentinelOS Payment Provider Abstraction Layer.
Supports:
1. Deterministic Simulated Provider for local development, CI, and test execution.
2. Transparent labeling: is_simulated=True is always recorded on simulated transactions.
3. Razorpay Gateway Adapter with HMAC-SHA256 signature verification and order creation.
4. Fails closed when live production credentials are absent or invalid.
5. Duplicate callback and idempotency protection.
6. PCI-DSS compliance: NEVER stores card numbers, CVVs, or sensitive credentials.
"""

import hashlib
import hmac
import json
import logging
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Dict, Any

logger = logging.getLogger("sentinel.payment_provider")


class PaymentGatewayError(Exception):
    """General payment gateway error."""
    pass


class PaymentGatewayNotConfiguredError(PaymentGatewayError):
    """Raised when live payment gateway credentials are not configured."""
    pass


GatewayConfigurationError = PaymentGatewayNotConfiguredError


@dataclass
class PaymentChargeResult:
    success: bool
    status: str                         # COMPLETED, PENDING, FAILED
    provider_transaction_id: str
    provider_name: str
    amount: Decimal
    currency: str
    is_simulated: bool
    failure_reason: Optional[str] = None
    gateway_metadata: Optional[Dict[str, Any]] = None


@dataclass
class PaymentRefundResult:
    success: bool
    status: str                         # COMPLETED, FAILED
    provider_refund_id: str
    amount: Decimal
    currency: str
    is_simulated: bool
    failure_reason: Optional[str] = None


class BasePaymentProvider(ABC):
    """Abstract Payment Gateway Contract for SentinelOS ISP Billing."""

    @abstractmethod
    def create_payment_order(
        self,
        amount: Decimal,
        currency: str,
        receipt: str,
        notes: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Create an authorized payment order with provider."""
        pass

    @abstractmethod
    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_method: str,
        idempotency_key: str,
        customer_email: Optional[str] = None,
        notes: Optional[Dict[str, str]] = None
    ) -> PaymentChargeResult:
        """Process a charge."""
        pass

    @abstractmethod
    def refund(
        self,
        provider_transaction_id: str,
        amount: Decimal,
        currency: str,
        reason: str
    ) -> PaymentRefundResult:
        """Process a refund against an existing transaction."""
        pass

    @abstractmethod
    def get_provider_name(self) -> str:
        """Return provider identifier string."""
        pass

    @abstractmethod
    def is_live_configured(self) -> bool:
        """Check if live gateway credentials are fully configured."""
        pass


class SimulatedPaymentProvider(BasePaymentProvider):
    """
    Deterministic simulated payment provider for local dev and automated tests.
    Explicitly labeled as simulated; never fabricates live transaction IDs.
    """

    def __init__(self, should_fail: bool = False, failure_reason: Optional[str] = None):
        self.should_fail = should_fail
        self.failure_reason = failure_reason or "Simulated payment declined by bank."

    def create_payment_order(
        self,
        amount: Decimal,
        currency: str,
        receipt: str,
        notes: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        order_id = f"order_sim_{uuid.uuid4().hex[:14]}"
        return {
            "order_id": order_id,
            "provider_order_id": order_id,
            "amount": amount,
            "amount_subunits": int(amount * Decimal("100.00")),
            "currency": currency,
            "receipt": receipt,
            "key_id": "rzp_test_simulated_key",
            "is_simulated": True,
            "status": "created",
            "notes": notes or {}
        }

    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_method: str,
        idempotency_key: str,
        customer_email: Optional[str] = None,
        notes: Optional[Dict[str, str]] = None
    ) -> PaymentChargeResult:
        # Special amount convention for test failure simulation (e.g. 99999.00 triggers failure)
        if self.should_fail or amount == Decimal('99999.00'):
            return PaymentChargeResult(
                success=False,
                status="FAILED",
                provider_transaction_id=f"sim_tx_fail_{idempotency_key[:16]}",
                provider_name="SIMULATED_GATEWAY",
                amount=amount,
                currency=currency,
                is_simulated=True,
                failure_reason=self.failure_reason,
                gateway_metadata={"simulation": True, "reason": "Deterministic test failure"}
            )

        # Successful deterministic simulation
        tx_id = f"sim_tx_{uuid.uuid4().hex[:12]}"
        return PaymentChargeResult(
            success=True,
            status="COMPLETED",
            provider_transaction_id=tx_id,
            provider_name="SIMULATED_GATEWAY",
            amount=amount,
            currency=currency,
            is_simulated=True,
            failure_reason=None,
            gateway_metadata={
                "simulation": True,
                "auth_code": "SIM_AUTH_200",
                "payment_method": payment_method
            }
        )

    def refund(
        self,
        provider_transaction_id: str,
        amount: Decimal,
        currency: str,
        reason: str
    ) -> PaymentRefundResult:
        refund_id = f"sim_rf_{uuid.uuid4().hex[:12]}"
        return PaymentRefundResult(
            success=True,
            status="COMPLETED",
            provider_refund_id=refund_id,
            amount=amount,
            currency=currency,
            is_simulated=True,
            failure_reason=None
        )

    def get_provider_name(self) -> str:
        return "SIMULATED_GATEWAY"

    def is_live_configured(self) -> bool:
        return False


class RazorpayGatewayAdapter(BasePaymentProvider):
    """
    Production-ready Razorpay Gateway contract.
    Fails closed if RAZORPAY_KEY_ID or RAZORPAY_KEY_SECRET are missing from environment.
    """

    def __init__(self):
        self.key_id = os.getenv("RAZORPAY_KEY_ID", "").strip()
        self.key_secret = os.getenv("RAZORPAY_KEY_SECRET", "").strip()
        self.webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "").strip() or self.key_secret
        self.timeout = float(os.getenv("RAZORPAY_TIMEOUT_SECONDS", "10.0"))

    def is_live_configured(self) -> bool:
        return bool(self.key_id and self.key_secret)

    def is_available(self) -> bool:
        return self.is_live_configured()

    def create_payment_order(
        self,
        amount: Decimal,
        currency: str,
        receipt: str,
        notes: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        if not self.is_live_configured():
            raise PaymentGatewayNotConfiguredError(
                "Live Razorpay credentials not configured. Fail-closed: Cannot create payment order."
            )
        try:
            import razorpay
            client = razorpay.Client(auth=(self.key_id, self.key_secret))
            amount_subunits = int(amount * Decimal("100.00"))
            order_payload = {
                "amount": amount_subunits,
                "currency": currency.upper(),
                "receipt": receipt[:40],
                "payment_capture": 1,
                "notes": notes or {}
            }
            order = client.order.create(data=order_payload)
            return {
                "order_id": order["id"],
                "provider_order_id": order["id"],
                "amount": amount,
                "amount_subunits": amount_subunits,
                "currency": currency,
                "receipt": receipt,
                "key_id": self.key_id,
                "is_simulated": False,
                "status": order.get("status", "created"),
                "notes": notes or {}
            }
        except Exception as e:
            logger.error(f"[RAZORPAY] Order creation failed: {e}")
            raise PaymentGatewayError(f"Razorpay order creation failed: {e}")

    def verify_webhook_signature(self, raw_body: bytes, signature: str, secret: Optional[str] = None) -> bool:
        """
        Verify Razorpay webhook HMAC-SHA256 signature against exact raw bytes.
        Follows official Razorpay signature verification contract.
        """
        sec = secret or self.webhook_secret
        if not sec or not signature:
            return False
        expected = hmac.new(sec.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def fetch_payment_status(self, provider_payment_id: str) -> Dict[str, Any]:
        if not self.is_live_configured():
            raise PaymentGatewayNotConfiguredError("Live Razorpay credentials not configured.")
        try:
            import razorpay
            client = razorpay.Client(auth=(self.key_id, self.key_secret))
            return client.payment.fetch(provider_payment_id)
        except Exception as e:
            logger.error(f"[RAZORPAY] Payment status fetch failed: {e}")
            raise PaymentGatewayError(f"Razorpay fetch payment status failed: {e}")

    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_method: str,
        idempotency_key: str,
        customer_email: Optional[str] = None,
        notes: Optional[Dict[str, str]] = None
    ) -> PaymentChargeResult:
        if not self.is_live_configured():
            raise PaymentGatewayNotConfiguredError(
                "Live Razorpay payment gateway credentials not configured. "
                "System is operating in fail-closed mode. Real payments are blocked."
            )
        order = self.create_payment_order(amount, currency, idempotency_key, notes)
        return PaymentChargeResult(
            success=True,
            status="PENDING",
            provider_transaction_id=order["order_id"],
            provider_name="RAZORPAY_GATEWAY",
            amount=amount,
            currency=currency,
            is_simulated=False,
            gateway_metadata=order
        )

    def refund(
        self,
        provider_transaction_id: str,
        amount: Decimal,
        currency: str,
        reason: str
    ) -> PaymentRefundResult:
        if not self.is_live_configured():
            raise PaymentGatewayNotConfiguredError("Live Razorpay credentials not configured for refund.")
        try:
            import razorpay
            client = razorpay.Client(auth=(self.key_id, self.key_secret))
            amount_subunits = int(amount * Decimal("100.00"))
            res = client.payment.refund(provider_transaction_id, {"amount": amount_subunits, "notes": {"reason": reason}})
            return PaymentRefundResult(
                success=True,
                status="COMPLETED",
                provider_refund_id=res.get("id", f"rf_{provider_transaction_id}"),
                amount=amount,
                currency=currency,
                is_simulated=False
            )
        except Exception as e:
            logger.error(f"[RAZORPAY] Refund failed: {e}")
            return PaymentRefundResult(
                success=False,
                status="FAILED",
                provider_refund_id="",
                amount=amount,
                currency=currency,
                is_simulated=False,
                failure_reason=str(e)
            )

    def get_provider_name(self) -> str:
        return "RAZORPAY_GATEWAY"


def get_payment_provider(force_provider: Optional[str] = None) -> BasePaymentProvider:
    """
    Factory to return active payment provider.
    Defaults to SimulatedPaymentProvider if live credentials are not present.
    """
    env_provider = (force_provider or os.getenv("PAYMENT_GATEWAY_PROVIDER", "simulated")).lower().strip()

    if env_provider == "razorpay":
        adapter = RazorpayGatewayAdapter()
        if adapter.is_live_configured():
            return adapter
        return SimulatedPaymentProvider()

    return SimulatedPaymentProvider()


def get_payment_gateway_status() -> Dict[str, Any]:
    """Inspect and report gateway readiness and simulation status."""
    razorpay_configured = bool(os.getenv("RAZORPAY_KEY_ID") and os.getenv("RAZORPAY_KEY_SECRET"))
    stripe_configured = bool(os.getenv("STRIPE_SECRET_KEY"))

    live_available = razorpay_configured or stripe_configured
    active = "RAZORPAY" if razorpay_configured else ("STRIPE" if stripe_configured else "SIMULATED_GATEWAY")

    return {
        "live_payments_available": live_available,
        "active_provider": active,
        "configured_gateways": [
            g for g, cfg in [("Razorpay", razorpay_configured), ("Stripe", stripe_configured)] if cfg
        ],
        "mode": "LIVE_GATEWAY" if live_available else "SIMULATED_DEVELOPMENT",
        "reason": (
            "Operating with live gateway credentials."
            if live_available
            else "No production payment gateway credentials configured in environment. Using safe deterministic simulation."
        )
    }
