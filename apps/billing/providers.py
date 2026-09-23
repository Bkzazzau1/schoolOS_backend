"""Payment-provider boundary for SchoolOS SaaS billing.

Provider secrets never leave the backend. The app receives only a hosted checkout
URL/access code created by the server for a specific immutable invoice amount.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from urllib import error as urlerror
from urllib import request as urlrequest

from django.conf import settings


class PaymentProviderError(Exception):
    pass


@dataclass(frozen=True)
class CheckoutSession:
    provider: str
    authorization_url: str
    access_code: str
    reference: str


class PaystackProvider:
    code = "paystack"

    def __init__(self):
        self.secret_key = settings.PAYSTACK_SECRET_KEY.strip()
        self.base_url = settings.PAYSTACK_API_BASE_URL.rstrip("/")
        if not self.secret_key:
            raise PaymentProviderError(
                "Paystack is not configured. Set PAYSTACK_SECRET_KEY on the server."
            )

    def _json_request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urlrequest.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.secret_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlrequest.urlopen(req, timeout=20) as response:  # noqa: S310 - fixed provider host
                decoded = json.loads(response.read().decode("utf-8"))
        except (urlerror.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            raise PaymentProviderError("Paystack could not be reached or returned an invalid response.") from exc
        if not isinstance(decoded, dict) or decoded.get("status") is not True:
            message = decoded.get("message") if isinstance(decoded, dict) else None
            raise PaymentProviderError(str(message or "Paystack rejected the payment request."))
        return decoded

    def initialize_transaction(
        self,
        *,
        reference: str,
        email: str,
        amount_minor: int,
        currency: str,
        metadata: dict,
    ) -> CheckoutSession:
        payload = {
            "email": email,
            "amount": str(amount_minor),
            "currency": currency,
            "reference": reference,
            "metadata": json.dumps(metadata, separators=(",", ":")),
        }
        callback_url = settings.PAYSTACK_CALLBACK_URL.strip()
        if callback_url:
            payload["callback_url"] = callback_url
        response = self._json_request("POST", "/transaction/initialize", payload)
        data = response.get("data")
        if not isinstance(data, dict):
            raise PaymentProviderError("Paystack did not return checkout details.")
        authorization_url = data.get("authorization_url")
        access_code = data.get("access_code")
        returned_reference = data.get("reference")
        if not all(isinstance(value, str) and value for value in (
            authorization_url,
            access_code,
            returned_reference,
        )):
            raise PaymentProviderError("Paystack returned incomplete checkout details.")
        if returned_reference != reference:
            raise PaymentProviderError("Paystack returned a different transaction reference.")
        return CheckoutSession(
            provider=self.code,
            authorization_url=authorization_url,
            access_code=access_code,
            reference=returned_reference,
        )

    def verify_transaction(self, reference: str) -> dict:
        safe_reference = reference.replace("/", "")
        if safe_reference != reference:
            raise PaymentProviderError("Invalid payment reference.")
        response = self._json_request("GET", f"/transaction/verify/{safe_reference}")
        data = response.get("data")
        if not isinstance(data, dict):
            raise PaymentProviderError("Paystack did not return transaction details.")
        return data

    def valid_webhook_signature(self, raw_body: bytes, signature: str | None) -> bool:
        if not signature:
            return False
        digest = hmac.new(
            self.secret_key.encode("utf-8"),
            raw_body,
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(digest, signature.strip())


def provider_for(code: str):
    if code == PaystackProvider.code:
        return PaystackProvider()
    raise PaymentProviderError(f"Unsupported payment provider: {code}")
