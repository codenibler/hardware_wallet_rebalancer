"""Read-only public-chain balance and market-price providers."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import AppConfig
from .models import ASSETS, ZERO, Holdings, PriceBook


SATOSHIS_PER_BTC = Decimal("100000000")
WEI_PER_ETH = Decimal("1000000000000000000")
LAMPORTS_PER_SOL = Decimal("1000000000")
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "LINK": "chainlink",
}
TRANSIENT_HTTP_STATUSES = (429, 500, 502, 503, 504)
PROVIDER_RETRY_COUNT = 3
PROVIDER_RETRY_BACKOFF_SECONDS = 1


class ProviderError(RuntimeError):
    """A sanitized provider failure that never includes wallet identifiers."""


def _decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProviderError(f"{label} returned a malformed numeric value") from exc
    if not result.is_finite():
        raise ProviderError(f"{label} returned a non-finite numeric value")
    return result


class PublicDataClient:
    """Fetch balances without connecting to or signing with the Trezor."""

    def __init__(
        self,
        config: AppConfig,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.config = config
        self.session = session or self._retrying_session()
        self.timeout_seconds = timeout_seconds
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; hardware-wallet-rebalancer/0.1; "
                    "+https://github.com/codenibler/hardware_wallet_rebalancer)"
                ),
                "Accept": "application/json",
            }
        )

    @staticmethod
    def _retrying_session() -> requests.Session:
        """Return a session that retries safe provider reads transiently."""

        retry = Retry(
            total=PROVIDER_RETRY_COUNT,
            connect=PROVIDER_RETRY_COUNT,
            read=PROVIDER_RETRY_COUNT,
            status=PROVIDER_RETRY_COUNT,
            allowed_methods=frozenset(("GET",)),
            status_forcelist=TRANSIENT_HTTP_STATUSES,
            backoff_factor=PROVIDER_RETRY_BACKOFF_SECONDS,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session = requests.Session()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _get_json(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        label: str,
    ) -> dict[str, Any]:
        try:
            response = self.session.get(
                url,
                params=params,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except requests.HTTPError as exc:
            status = (
                exc.response.status_code
                if exc.response is not None
                else None
            )
            status_text = (
                f" returned HTTP {status}"
                if status
                else " request failed"
            )
            raise ProviderError(
                f"{label}{status_text}; wallet identifier omitted"
            ) from exc
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(
                f"{label} request failed; wallet identifier omitted"
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(f"{label} returned an unexpected response")
        return data

    def _post_rpc(
        self,
        url: str,
        payload: dict[str, object],
        *,
        label: str,
    ) -> dict[str, Any]:
        try:
            response = self.session.post(
                url,
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(
                f"{label} request failed; wallet identifier omitted"
            ) from exc
        if not isinstance(data, dict) or data.get("error"):
            raise ProviderError(f"{label} RPC returned an error")
        return data

    def fetch_bitcoin(self) -> tuple[Decimal, Decimal]:
        """Return confirmed and net unconfirmed Bitcoin across all XPUBs."""

        total_confirmed_sats = ZERO
        total_unconfirmed_sats = ZERO
        base = self.config.providers.bitcoin_blockbook_url
        for xpub in self.config.wallet.bitcoin_xpubs:
            encoded = quote(xpub, safe="")
            data = self._get_json(
                f"{base}/api/v2/xpub/{encoded}",
                params={"details": "basic"},
                label="Bitcoin Blockbook",
            )
            total_confirmed_sats += _decimal(
                data.get("balance"),
                "Bitcoin Blockbook",
            )
            total_unconfirmed_sats += _decimal(
                data.get("unconfirmedBalance", "0"),
                "Bitcoin Blockbook",
            )

        confirmed = total_confirmed_sats / SATOSHIS_PER_BTC
        pending = total_unconfirmed_sats / SATOSHIS_PER_BTC
        included = (
            confirmed + pending
            if self.config.policy.include_unconfirmed_bitcoin
            else confirmed
        )
        if included < ZERO:
            raise ProviderError("Bitcoin provider produced a negative total")
        return included, pending

    def fetch_ethereum_and_link(self) -> tuple[Decimal, Decimal]:
        """Return native ETH and official mainnet LINK via Ethereum JSON-RPC."""

        rpc_url = self.config.providers.ethereum_rpc_url
        contract = self.config.providers.link_contract
        chain_response = self._post_rpc(
            rpc_url,
            {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
            label="Ethereum",
        )
        try:
            chain_id = int(str(chain_response.get("result")), 16)
        except (TypeError, ValueError) as exc:
            raise ProviderError("Ethereum RPC returned an invalid chain ID") from exc
        if chain_id != 1:
            raise ProviderError(
                f"Ethereum RPC is on chain ID {chain_id}, expected mainnet 1"
            )

        decimals_response = self._post_rpc(
            rpc_url,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "eth_call",
                "params": [{"to": contract, "data": "0x313ce567"}, "latest"],
            },
            label="Ethereum LINK",
        )
        try:
            link_decimals = int(str(decimals_response.get("result")), 16)
        except (TypeError, ValueError) as exc:
            raise ProviderError("LINK contract returned invalid decimals") from exc
        if link_decimals != 18:
            raise ProviderError(
                f"Configured LINK contract reports {link_decimals} decimals, expected 18"
            )

        total_wei = 0
        total_link_units = 0
        for request_id, address in enumerate(
            self.config.wallet.ethereum_addresses,
            start=10,
        ):
            eth_response = self._post_rpc(
                rpc_url,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "eth_getBalance",
                    "params": [address, "latest"],
                },
                label="Ethereum",
            )
            balance_of_data = "0x70a08231" + address[2:].lower().rjust(64, "0")
            link_response = self._post_rpc(
                rpc_url,
                {
                    "jsonrpc": "2.0",
                    "id": request_id + 1000,
                    "method": "eth_call",
                    "params": [
                        {"to": contract, "data": balance_of_data},
                        "latest",
                    ],
                },
                label="Ethereum LINK",
            )
            try:
                total_wei += int(str(eth_response.get("result")), 16)
                total_link_units += int(str(link_response.get("result")), 16)
            except (TypeError, ValueError) as exc:
                raise ProviderError(
                    "Ethereum RPC returned a malformed balance"
                ) from exc

        eth = Decimal(total_wei) / WEI_PER_ETH
        link = Decimal(total_link_units) / (Decimal(10) ** link_decimals)
        if eth < ZERO or link < ZERO:
            raise ProviderError("Ethereum provider produced a negative total")
        return eth, link

    def _fetch_solana_address(self, address: str) -> Decimal:
        data = self._post_rpc(
            self.config.providers.solana_rpc_url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getBalance",
                "params": [address, {"commitment": "finalized"}],
            },
            label="Solana",
        )
        result = data.get("result")
        if not isinstance(result, dict) or "value" not in result:
            raise ProviderError("Solana RPC returned an unexpected response")
        lamports = _decimal(result["value"], "Solana RPC")
        if lamports < ZERO:
            raise ProviderError("Solana RPC produced a negative balance")
        return lamports / LAMPORTS_PER_SOL

    def fetch_solana(self) -> Decimal:
        """Return SOL in primary and explicitly configured stake accounts."""

        addresses = (
            *self.config.wallet.solana_addresses,
            *self.config.wallet.solana_stake_accounts,
        )
        return sum(
            (self._fetch_solana_address(address) for address in addresses),
            ZERO,
        )

    def fetch_holdings(self) -> Holdings:
        btc, pending_btc = self.fetch_bitcoin()
        eth, link = self.fetch_ethereum_and_link()
        sol = self.fetch_solana()
        return Holdings(
            amounts={"BTC": btc, "ETH": eth, "SOL": sol, "LINK": link},
            pending_bitcoin=pending_btc,
            fetched_at=datetime.now(timezone.utc),
        )

    def fetch_prices(self) -> PriceBook:
        ids = ",".join(COINGECKO_IDS.values())
        headers: dict[str, str] = {}
        api_key = os.getenv("COINGECKO_API_KEY", "").strip()
        if api_key:
            headers["x-cg-demo-api-key"] = api_key
        try:
            response = self.session.get(
                f"{self.config.providers.coingecko_url}/simple/price",
                params={
                    "ids": ids,
                    "vs_currencies": "eur",
                    "include_last_updated_at": "true",
                    "precision": "full",
                },
                headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError("CoinGecko price request failed") from exc
        if not isinstance(data, dict):
            raise ProviderError("CoinGecko returned an unexpected response")

        prices: dict[str, Decimal] = {}
        updated_times: list[datetime] = []
        for asset in ASSETS:
            coin_id = COINGECKO_IDS[asset]
            quote_data = data.get(coin_id)
            if not isinstance(quote_data, dict):
                raise ProviderError(f"CoinGecko omitted {asset}")
            price = _decimal(quote_data.get("eur"), f"CoinGecko {asset}")
            if price <= ZERO:
                raise ProviderError(f"CoinGecko returned a non-positive {asset} price")
            timestamp = quote_data.get("last_updated_at")
            if not isinstance(timestamp, (int, float)) or timestamp <= 0:
                raise ProviderError(f"CoinGecko omitted the {asset} update time")
            prices[asset] = price
            updated_times.append(datetime.fromtimestamp(timestamp, timezone.utc))

        as_of = min(updated_times)
        age_seconds = (datetime.now(timezone.utc) - as_of).total_seconds()
        if age_seconds < -60:
            raise ProviderError("CoinGecko price timestamp is in the future")
        if age_seconds > self.config.policy.max_price_age_seconds:
            raise ProviderError(
                f"CoinGecko prices are stale ({age_seconds:.0f}s old); "
                "no trade plan generated"
            )
        return PriceBook(
            prices_eur=prices,
            as_of=as_of,
            source="CoinGecko EUR simple/price",
        )
