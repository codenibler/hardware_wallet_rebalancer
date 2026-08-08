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
EVERSTAKE_STAKE_ADDED_TOPIC = (
    "0x7d194e8dc0f902cdc51bde00649039561dbd0b01574d671bad333436fdac7692"
)
EVERSTAKE_UNSTAKE_TOPIC = (
    "0x0750a71dce555de583ab0225a108df42b9402d22123d7cc9cd95793e43e7db0e"
)


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

        total_wei += self._fetch_everstake_deposited_wei()

        eth = Decimal(total_wei) / WEI_PER_ETH
        link = Decimal(total_link_units) / (Decimal(10) ** link_decimals)
        if eth < ZERO or link < ZERO:
            raise ProviderError("Ethereum provider produced a negative total")
        return eth, link

    def _fetch_everstake_deposited_wei(self) -> int:
        """Return net ETH principal attributed to configured wallet addresses."""

        total_wei = 0
        request_id = 2000
        pool_addresses = {
            value.lower()
            for value in self.config.wallet.staked_ethereum_addresses
        }
        if not pool_addresses:
            return 0
        for staker_address in self.config.wallet.ethereum_addresses:
            staker_topic = "0x" + staker_address[2:].lower().rjust(64, "0")
            page = 1
            seen_txids: set[str] = set()
            while True:
                history = self._get_json(
                    (
                        f"{self.config.providers.ethereum_blockbook_url}"
                        f"/api/v2/address/{staker_address}"
                    ),
                    params={
                        "details": "txids",
                        "page": page,
                        "pageSize": 1000,
                    },
                    label="Ethereum Blockbook",
                )
                txids = history.get("txids", [])
                total_pages = history.get("totalPages", 1)
                if (
                    not isinstance(txids, list)
                    or any(not isinstance(txid, str) for txid in txids)
                    or not isinstance(total_pages, int)
                    or total_pages < 1
                ):
                    raise ProviderError(
                        "Ethereum Blockbook returned malformed transaction history"
                    )
                for txid in txids:
                    if txid in seen_txids:
                        continue
                    seen_txids.add(txid)
                    response = self._post_rpc(
                        self.config.providers.ethereum_rpc_url,
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "eth_getTransactionReceipt",
                            "params": [txid],
                        },
                        label="Everstake Ethereum",
                    )
                    request_id += 1
                    receipt = response.get("result")
                    if not isinstance(receipt, dict):
                        raise ProviderError(
                            "Ethereum RPC returned a malformed transaction receipt"
                        )
                    logs = receipt.get("logs")
                    if not isinstance(logs, list):
                        raise ProviderError(
                            "Ethereum RPC returned malformed receipt logs"
                        )
                    for event in logs:
                        if not isinstance(event, dict):
                            raise ProviderError(
                                "Ethereum RPC returned a malformed event"
                            )
                        event_address = event.get("address")
                        topics = event.get("topics")
                        if (
                            not isinstance(event_address, str)
                            or event_address.lower() not in pool_addresses
                            or not isinstance(topics, list)
                            or len(topics) < 2
                            or not isinstance(topics[0], str)
                            or not isinstance(topics[1], str)
                            or topics[1].lower() != staker_topic
                        ):
                            continue
                        event_topic = topics[0].lower()
                        if event_topic not in {
                            EVERSTAKE_STAKE_ADDED_TOPIC,
                            EVERSTAKE_UNSTAKE_TOPIC,
                        }:
                            continue
                        data = event.get("data")
                        if (
                            not isinstance(data, str)
                            or not data.startswith("0x")
                            or len(data) < 66
                        ):
                            raise ProviderError(
                                "Everstake Ethereum RPC returned a malformed event"
                            )
                        try:
                            value = int(data[2:66], 16)
                        except ValueError as exc:
                            raise ProviderError(
                                "Everstake Ethereum RPC returned a malformed value"
                            ) from exc
                        if event_topic == EVERSTAKE_STAKE_ADDED_TOPIC:
                            total_wei += value
                        else:
                            total_wei -= value
                if page >= total_pages:
                    break
                page += 1

        if total_wei < 0:
            raise ProviderError("Everstake Ethereum produced a negative stake")
        return total_wei

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

    def _fetch_everstake_sol_deposited_lamports(self) -> int:
        """Return net SOL principal moved from configured wallets into pools.

        Everstake's Solana pool address holds every customer's stake, so its
        raw balance is not attributable to this wallet. Instead, walk the
        pool address's transaction history and net out SOL transfers between
        it and the configured (non-staked) wallet addresses, which yields
        deposited principal without picking up validator rewards the pool
        accrues internally.
        """

        pool_addresses = set(self.config.wallet.solana_stake_accounts)
        if not pool_addresses:
            return 0
        staker_addresses = set(self.config.wallet.solana_addresses)
        total_lamports = 0
        for pool_address in pool_addresses:
            before: str | None = None
            seen_signatures: set[str] = set()
            while True:
                params: dict[str, object] = {"limit": 1000}
                if before is not None:
                    params["before"] = before
                history = self._post_rpc(
                    self.config.providers.solana_rpc_url,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getSignaturesForAddress",
                        "params": [pool_address, params],
                    },
                    label="Everstake Solana",
                )
                entries = history.get("result")
                if not isinstance(entries, list):
                    raise ProviderError(
                        "Solana RPC returned malformed signature history"
                    )
                if not entries:
                    break
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ProviderError(
                            "Solana RPC returned a malformed signature entry"
                        )
                    signature = entry.get("signature")
                    if not isinstance(signature, str):
                        raise ProviderError(
                            "Solana RPC returned a malformed signature"
                        )
                    if signature in seen_signatures:
                        continue
                    seen_signatures.add(signature)
                    total_lamports += self._everstake_sol_transaction_delta(
                        signature,
                        pool_address=pool_address,
                        staker_addresses=staker_addresses,
                    )
                last_signature = entries[-1].get("signature")
                if not isinstance(last_signature, str) or len(entries) < 1000:
                    break
                before = last_signature

        if total_lamports < 0:
            raise ProviderError("Everstake Solana produced a negative stake")
        return total_lamports

    def _everstake_sol_transaction_delta(
        self,
        signature: str,
        *,
        pool_address: str,
        staker_addresses: set[str],
    ) -> int:
        response = self._post_rpc(
            self.config.providers.solana_rpc_url,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    signature,
                    {"encoding": "json", "maxSupportedTransactionVersion": 0},
                ],
            },
            label="Everstake Solana",
        )
        result = response.get("result")
        if result is None:
            return 0
        if not isinstance(result, dict):
            raise ProviderError("Solana RPC returned a malformed transaction")
        meta = result.get("meta")
        transaction = result.get("transaction")
        if not isinstance(meta, dict) or not isinstance(transaction, dict):
            raise ProviderError("Solana RPC returned a malformed transaction")
        if meta.get("err") is not None:
            return 0
        message = transaction.get("message")
        pre_balances = meta.get("preBalances")
        post_balances = meta.get("postBalances")
        fee = meta.get("fee")
        if (
            not isinstance(message, dict)
            or not isinstance(pre_balances, list)
            or not isinstance(post_balances, list)
            or not isinstance(fee, int)
        ):
            raise ProviderError("Solana RPC returned a malformed transaction")
        account_keys = message.get("accountKeys")
        if not isinstance(account_keys, list) or any(
            not isinstance(key, str) for key in account_keys
        ):
            raise ProviderError("Solana RPC returned malformed account keys")
        if pool_address not in account_keys:
            return 0

        net_delta = 0
        for index, key in enumerate(account_keys):
            if key not in staker_addresses:
                continue
            try:
                pre = int(pre_balances[index])
                post = int(post_balances[index])
            except (TypeError, ValueError, IndexError) as exc:
                raise ProviderError(
                    "Solana RPC returned malformed account balances"
                ) from exc
            change = post - pre
            if index == 0:
                change += fee
            net_delta += change
        return -net_delta

    def fetch_solana(self) -> Decimal:
        """Return SOL in primary wallets plus net principal staked with pools."""

        wallet_sol = sum(
            (
                self._fetch_solana_address(address)
                for address in self.config.wallet.solana_addresses
            ),
            ZERO,
        )
        staked_sol = (
            Decimal(self._fetch_everstake_sol_deposited_lamports())
            / LAMPORTS_PER_SOL
        )
        return wallet_sol + staked_sol

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
