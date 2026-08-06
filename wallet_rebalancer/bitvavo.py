"""Authenticated Bitvavo REST client and execution-only configuration."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import requests

from .models import ASSETS, ZERO

BITVAVO_API_BASE = "https://api.bitvavo.com/v2"
DEFAULT_TIMEOUT_SECONDS = 20.0


class BitvavoError(RuntimeError):
    """A sanitized Bitvavo API or response-validation failure."""

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class BitvavoConfig:
    operator_id: int
    withdrawal_addresses: Mapping[str, str]
    withdrawal_networks: Mapping[str, str]
    max_top_up_eur: Decimal
    max_price_deviation_bps: Decimal


@dataclass(frozen=True)
class BitvavoCredentials:
    api_key: str
    api_secret: str


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or "replace_with" in value:
        raise ValueError(f"{name} is not configured")
    return value


def _decimal_env(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default).strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def load_bitvavo_config() -> BitvavoConfig:
    """Load non-secret execution policy and fixed withdrawal allowlists."""

    try:
        operator_id = int(_required_env("HWR_BITVAVO_OPERATOR_ID"))
    except ValueError as exc:
        raise ValueError("HWR_BITVAVO_OPERATOR_ID must be an integer") from exc
    if operator_id <= 0:
        raise ValueError("HWR_BITVAVO_OPERATOR_ID must be positive")

    max_top_up = _decimal_env("HWR_BITVAVO_MAX_TOP_UP_EUR", "1000")
    max_deviation = _decimal_env(
        "HWR_BITVAVO_MAX_PRICE_DEVIATION_BPS",
        "200",
    )
    if max_top_up <= ZERO:
        raise ValueError("HWR_BITVAVO_MAX_TOP_UP_EUR must be positive")
    if max_deviation <= ZERO or max_deviation > Decimal("1000"):
        raise ValueError(
            "HWR_BITVAVO_MAX_PRICE_DEVIATION_BPS must be in (0, 1000]"
        )

    addresses = {
        asset: _required_env(f"HWR_BITVAVO_WITHDRAW_{asset}_ADDRESS")
        for asset in ASSETS
    }
    networks = {
        asset: _required_env(f"HWR_BITVAVO_WITHDRAW_{asset}_NETWORK")
        for asset in ASSETS
    }
    if any(len(value) > 500 for value in addresses.values()):
        raise ValueError("Bitvavo withdrawal addresses cannot exceed 500 characters")
    if any(len(value) > 10 for value in networks.values()):
        raise ValueError("Bitvavo withdrawal networks cannot exceed 10 characters")

    return BitvavoConfig(
        operator_id=operator_id,
        withdrawal_addresses=addresses,
        withdrawal_networks=networks,
        max_top_up_eur=max_top_up,
        max_price_deviation_bps=max_deviation,
    )


def load_readonly_credentials() -> BitvavoCredentials:
    """Load the key used for account reads and dry-run preparation."""

    return BitvavoCredentials(
        api_key=_required_env("BITVAVO_READONLY_API_KEY"),
        api_secret=_required_env("BITVAVO_READONLY_API_SECRET"),
    )


def load_trading_credentials() -> BitvavoCredentials:
    """Load the key used only after explicit execution confirmation."""

    return BitvavoCredentials(
        api_key=_required_env("BITVAVO_TRADING_API_KEY"),
        api_secret=_required_env("BITVAVO_TRADING_API_SECRET"),
    )


def _decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BitvavoError(f"Bitvavo returned an invalid {label}") from exc
    if not result.is_finite():
        raise BitvavoError(f"Bitvavo returned a non-finite {label}")
    return result


class BitvavoClient:
    """Read-only Bitvavo client with no order or withdrawal methods."""

    _writes_enabled = False

    def __init__(
        self,
        config: BitvavoConfig,
        credentials: BitvavoCredentials,
        *,
        session: requests.Session | None = None,
        base_url: str = BITVAVO_API_BASE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "hardware-wallet-rebalancer/0.1",
            }
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, object] | None = None,
        body: Mapping[str, object] | None = None,
        private: bool,
    ) -> Any:
        method = method.upper()
        if method != "GET" and not self._writes_enabled:
            raise BitvavoError(
                "The read-only Bitvavo client cannot submit write requests"
            )
        query = urlencode(
            [(key, str(value)) for key, value in (params or {}).items()]
        )
        postfix = f"?{query}" if query else ""
        request_path = f"/v2{endpoint}{postfix}"
        body_text = (
            json.dumps(body, separators=(",", ":"), ensure_ascii=True)
            if body
            else ""
        )
        headers: dict[str, str] = {}
        if private:
            timestamp = self.clock_ms()
            signed = f"{timestamp}{method}{request_path}{body_text}"
            signature = hmac.new(
                self.credentials.api_secret.encode("utf-8"),
                signed.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers.update(
                {
                    "Bitvavo-Access-Key": self.credentials.api_key,
                    "Bitvavo-Access-Signature": signature,
                    "Bitvavo-Access-Timestamp": str(timestamp),
                    "Bitvavo-Access-Window": "10000",
                }
            )
        if body_text:
            headers["Content-Type"] = "application/json"

        try:
            response = self.session.request(
                method,
                f"{self.base_url}{endpoint}{postfix}",
                headers=headers,
                data=body_text or None,
                timeout=self.timeout_seconds,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise BitvavoError(
                f"Bitvavo {method} {endpoint} request failed"
            ) from exc

        if response.status_code >= 400 or (
            isinstance(data, dict) and ("error" in data or "errorCode" in data)
        ):
            raw_code = data.get("errorCode") if isinstance(data, dict) else None
            code = raw_code if isinstance(raw_code, int) else None
            suffix = f" (error {code})" if code is not None else ""
            reason = data.get("error") if isinstance(data, dict) else None
            if isinstance(reason, str) and reason.strip():
                suffix += f": {reason.strip()[:300]}"
            raise BitvavoError(
                f"Bitvavo rejected {method} {endpoint}{suffix}",
                error_code=code,
            )
        return data

    def get_balances(self) -> dict[str, Decimal]:
        data = self._request("GET", "/balance", private=True)
        if not isinstance(data, list):
            raise BitvavoError("Bitvavo returned an invalid balance response")
        balances = {asset: ZERO for asset in (*ASSETS, "EUR")}
        for row in data:
            if not isinstance(row, dict):
                raise BitvavoError("Bitvavo returned an invalid balance row")
            symbol = str(row.get("symbol", ""))
            if symbol in balances:
                available = _decimal(row.get("available"), f"{symbol} balance")
                if available < ZERO:
                    raise BitvavoError("Bitvavo returned a negative balance")
                balances[symbol] = available
        return balances

    def get_market_fee_bps(self, asset: str) -> Decimal:
        data = self._request(
            "GET",
            "/account/fees",
            params={"market": f"{asset}-EUR"},
            private=True,
        )
        if not isinstance(data, dict):
            raise BitvavoError("Bitvavo returned an invalid fee response")
        taker = _decimal(data.get("taker"), f"{asset} taker fee")
        if taker < ZERO or taker > Decimal("0.1"):
            raise BitvavoError("Bitvavo returned an implausible taker fee")
        return taker * Decimal("10000")

    def get_market(self, asset: str) -> dict[str, object]:
        data = self._request(
            "GET",
            "/markets",
            params={"market": f"{asset}-EUR"},
            private=False,
        )
        if isinstance(data, list):
            data = data[0] if len(data) == 1 else None
        if not isinstance(data, dict):
            raise BitvavoError(f"Bitvavo returned invalid {asset}-EUR market data")
        return data

    def get_asset(self, asset: str) -> dict[str, object]:
        data = self._request(
            "GET",
            "/assets",
            params={"symbol": asset},
            private=False,
        )
        if isinstance(data, list):
            data = data[0] if len(data) == 1 else None
        if not isinstance(data, dict):
            raise BitvavoError(f"Bitvavo returned invalid {asset} asset data")
        return data

    def get_best_ask(self, asset: str) -> Decimal:
        data = self._request(
            "GET",
            "/ticker/book",
            params={"market": f"{asset}-EUR"},
            private=False,
        )
        if isinstance(data, list):
            data = data[0] if len(data) == 1 else None
        if not isinstance(data, dict):
            raise BitvavoError(f"Bitvavo returned invalid {asset}-EUR ticker data")
        ask = _decimal(data.get("ask"), f"{asset} ask")
        if ask <= ZERO:
            raise BitvavoError(f"Bitvavo returned a non-positive {asset} ask")
        return ask

class BitvavoExecutionClient(BitvavoClient):
    """Trading-key client used only during confirmed execution."""

    _writes_enabled = True

    def create_market_buy(
        self,
        asset: str,
        amount_quote_eur: Decimal,
        client_order_id: str,
    ) -> dict[str, object]:
        data = self._request(
            "POST",
            "/order",
            body={
                "market": f"{asset}-EUR",
                "side": "buy",
                "orderType": "market",
                "operatorId": self.config.operator_id,
                "clientOrderId": client_order_id,
                "amountQuote": format(amount_quote_eur, "f"),
                "responseRequired": True,
            },
            private=True,
        )
        if not isinstance(data, dict):
            raise BitvavoError("Bitvavo returned an invalid order response")
        return data

    def get_order(self, asset: str, order_id: str) -> dict[str, object]:
        data = self._request(
            "GET",
            "/order",
            params={
                "market": f"{asset}-EUR",
                "orderId": order_id,
            },
            private=True,
        )
        if not isinstance(data, dict):
            raise BitvavoError("Bitvavo returned an invalid order response")
        return data

    def withdraw(
        self,
        *,
        asset: str,
        amount: Decimal,
        idempotency_key: str,
    ) -> dict[str, object]:
        data = self._request(
            "POST",
            "/crypto/withdrawal",
            body={
                "asset": asset,
                "network": self.config.withdrawal_networks[asset],
                "address": self.config.withdrawal_addresses[asset],
                "amount": format(amount, "f"),
                "deductFeeFromAmount": True,
                "idempotencyKey": idempotency_key,
            },
            private=True,
        )
        if not isinstance(data, dict):
            raise BitvavoError("Bitvavo returned an invalid withdrawal response")
        return data
