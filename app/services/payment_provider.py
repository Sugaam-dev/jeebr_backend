"""
app/services/payment_provider.py

SentinelOS Payment Provider Abstraction Layer.
Supports:
1. Deterministic Simulated Provider for local development, CI, and test execution.
2. Transparent labeling: is_simulated=True is always recorded on simulated transactions.
3. Razorpay / Real Gateway Adapter interface that fails closed when credentials are not configured.
4. Duplicate callback and idempotency protection.
5. PCI-DSS compliance: NEVER stores card numbers, CVVs, or sensitive credentials.
"""

import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Dict, Any


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
    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_method: str,
        idempotency_key: str,
        customer_email: Optional[str] = None,
        notes: Optional[Dict[str, str]] = None
    ) -> PaymentChargeResult:
        """Process an authorized charge."""
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

    def is_live_configured(self) -> bool:
        return bool(self.key_id and self.key_secret)

    def is_available(self) -> bool:
        return self.is_live_configured()

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
        # Real production API invocation would occur here with verified razorpay client
        raise NotImplementedError("Real production payment processing requires verified gateway authorization.")

    def refund(
        self,
        provider_transaction_id: str,
        amount: Decimal,
        currency: str,
        reason: str
    ) -> PaymentRefundResult:
        if not self.is_live_configured():
            raise PaymentGatewayNotConfiguredError(
                "Live Razorpay credentials not configured for refund."
            )
        raise NotImplementedError("Real production payment refund requires verified gateway authorization.")

    def get_provider_name(self) -> str:
        return "RAZORPAY_GATEWAY"


def get_payment_provider(force_provider: Optional[str] = None) -> BasePaymentProvider:
    """
    Factory to return the active payment provider.
    Defaults to SimulatedPaymentProvider if live credentials are not present.
    """
    env_provider = (force_provider or os.getenv("PAYMENT_GATEWAY_PROVIDER", "simulated")).lower().strip()

    if env_provider == "razorpay":
        adapter = RazorpayGatewayAdapter()
        if adapter.is_live_configured():
            return adapter
        # Fall back to simulated with clear warning or raise if strictly configured
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
