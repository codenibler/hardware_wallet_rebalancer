"""Fee-aware comparison of public EUR spot quotes on supported exchanges."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

import requests

from .models import ASSETS, ZERO, TradeInstruction


ORDER_BOOK_EXCHANGES = ("bitvavo", "kraken", "coinbase", "okx")
INVITY_PROVIDER_NAMES = {
    "banxa": "Banxa",
    "invity": "Invity",
    "mercuryo": "Mercuryo",
    "anycoin": "Anycoin Direct",
    "btcdirect": "BTC Direct",
    "moonpay": "MoonPay",
}
INVITY_PROVIDERS = tuple(INVITY_PROVIDER_NAMES)
SUPPORTED_EXCHANGES = ORDER_BOOK_EXCHANGES + INVITY_PROVIDERS
EXCHANGE_NAMES = {
    "bitvavo": "Bitvavo",
    "kraken": "Kraken Pro",
    "coinbase": "Coinbase Advanced",
    "okx": "OKX Europe",
    **INVITY_PROVIDER_NAMES,
}
DEFAULT_TAKER_FEE_BPS = {
    "bitvavo": Decimal("25"),
    "kraken": Decimal("80"),
    "coinbase": Decimal("60"),
    "okx": Decimal("35"),
}
INVITY_ASSET_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "LINK": "ethereum--0x514910771af9ca656af840dff83e8264ecf986ca",
}
INVITY_TRADE_URLS = {
    "banxa": "https://banxa.com",
    "invity": "https://invity.io",
    "mercuryo": "https://exchange.mercuryo.io",
    "anycoin": "https://anycoindirect.eu",
    "btcdirect": "https://btcdirect.eu",
    "moonpay": "https://www.moonpay.com/buy",
}


class ExchangeScanError(RuntimeError):
    """A sanitized market-data failure."""


class _VenueError(RuntimeError):
    pass


def _decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise _VenueError(f"{label} returned malformed market data") from exc
    if not result.is_finite():
        raise _VenueError(f"{label} returned non-finite market data")
    return result


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_taker_fee_bps(
    overrides: Mapping[str, Decimal | str | int | float] | None = None,
) -> dict[str, Decimal]:
    """Load entry-tier taker fees, allowing account-tier environment overrides."""

    configured: dict[str, Decimal] = {}
    supplied = overrides or {}
    for exchange in ORDER_BOOK_EXCHANGES:
        environment_name = f"HWR_{exchange.upper()}_TAKER_FEE_BPS"
        raw_value: object = supplied.get(
            exchange,
            os.getenv(environment_name, str(DEFAULT_TAKER_FEE_BPS[exchange])),
        )
        try:
            fee = Decimal(str(raw_value).strip())
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{environment_name} must be a decimal number") from exc
        if not fee.is_finite() or fee < ZERO or fee > Decimal("1000"):
            raise ValueError(f"{environment_name} must be between 0 and 1000")
        configured[exchange] = fee
    return configured


@dataclass(frozen=True)
class ExchangeQuote:
    asset: str
    exchange_id: str
    exchange_name: str
    pair: str
    ask_eur: Decimal
    ask_size: Decimal
    bid_eur: Decimal
    bid_size: Decimal
    taker_fee_bps: Decimal
    quoted_at: datetime
    trade_url: str
    ask_min_size: Decimal = ZERO
    bid_min_size: Decimal = ZERO
    supported_sides: frozenset[str] = frozenset(("BUY", "SELL"))
    fee_eur_override: Decimal | None = None
    fee_included_in_quote: bool = False

    def effective_unit_price_eur(self, side: str) -> Decimal:
        if side not in self.supported_sides:
            raise ValueError(f"{self.exchange_name} does not support {side}")
        fee_rate = self.taker_fee_bps / Decimal("10000")
        if side == "BUY":
            return self.ask_eur * (Decimal("1") + fee_rate)
        if side == "SELL":
            return self.bid_eur * (Decimal("1") - fee_rate)
        raise ValueError("Trade side must be BUY or SELL")

    def execution_price_eur(self, side: str) -> Decimal:
        if side not in self.supported_sides:
            raise ValueError(f"{self.exchange_name} does not support {side}")
        if side == "BUY":
            return self.ask_eur
        if side == "SELL":
            return self.bid_eur
        raise ValueError("Trade side must be BUY or SELL")

    def covers(self, side: str, amount: Decimal) -> bool:
        if side not in self.supported_sides:
            return False
        if side == "BUY":
            minimum, available = self.ask_min_size, self.ask_size
        else:
            minimum, available = self.bid_min_size, self.bid_size
        return minimum <= amount <= available

    def estimated_fee_eur(self, side: str, amount: Decimal) -> Decimal:
        if self.fee_eur_override is not None:
            return self.fee_eur_override
        return (
            amount
            * self.execution_price_eur(side)
            * self.taker_fee_bps
            / Decimal("10000")
        )

    def to_dict(self, *, side: str, amount: Decimal) -> dict[str, object]:
        return {
            "asset": self.asset,
            "exchange_id": self.exchange_id,
            "exchange_name": self.exchange_name,
            "pair": self.pair,
            "ask_eur": str(self.ask_eur),
            "ask_size": str(self.ask_size),
            "bid_eur": str(self.bid_eur),
            "bid_size": str(self.bid_size),
            "ask_min_size": str(self.ask_min_size),
            "bid_min_size": str(self.bid_min_size),
            "taker_fee_bps": str(self.taker_fee_bps),
            "supported_sides": sorted(self.supported_sides),
            "side": side,
            "trade_amount": str(amount),
            "effective_unit_price_eur": str(
                self.effective_unit_price_eur(side)
            ),
            "estimated_fee_eur": str(self.estimated_fee_eur(side, amount)),
            "fee_included_in_quote": self.fee_included_in_quote,
            "covers_trade_at_best_quote": self.covers(side, amount),
            "quoted_at": _iso(self.quoted_at),
            "trade_url": self.trade_url,
        }


@dataclass(frozen=True)
class VenueMarketSnapshot:
    as_of: datetime
    quotes: Mapping[str, tuple[ExchangeQuote, ...]]
    failures: tuple[str, ...]

    def rank(
        self,
        *,
        asset: str,
        side: str,
        amount: Decimal,
        limit: int = 3,
    ) -> tuple[ExchangeQuote, ...]:
        if asset not in ASSETS:
            raise ValueError(f"Unsupported asset: {asset}")
        if side not in {"BUY", "SELL"}:
            raise ValueError("Trade side must be BUY or SELL")
        if amount <= ZERO:
            raise ValueError("Trade amount must be positive")

        def sort_key(quote: ExchangeQuote) -> tuple[bool, Decimal, str]:
            effective = quote.effective_unit_price_eur(side)
            ranked_price = effective if side == "BUY" else -effective
            return (
                not quote.covers(side, amount),
                ranked_price,
                quote.exchange_name,
            )

        return tuple(
            sorted(
                (
                    quote
                    for quote in self.quotes.get(asset, ())
                    if side in quote.supported_sides
                ),
                key=sort_key,
            )[:limit]
        )


class ExchangeScanner:
    """Fetch fee-aware EUR bid/ask quotes from Dutch-accessible venues."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 12.0,
        exchanges: Sequence[str] = SUPPORTED_EXCHANGES,
        taker_fee_bps: Mapping[str, Decimal | str | int | float] | None = None,
        invity_account_descriptor: str | None = None,
        country: str | None = None,
        payment_methods: Sequence[str] | None = None,
    ) -> None:
        unknown = sorted(set(exchanges) - set(SUPPORTED_EXCHANGES))
        if unknown:
            raise ValueError(f"Unsupported exchanges: {unknown}")
        if not exchanges:
            raise ValueError("At least one exchange must be selected")
        self.exchanges = tuple(dict.fromkeys(exchanges))
        self.fees = load_taker_fee_bps(taker_fee_bps)
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        descriptor = (invity_account_descriptor or "").strip()
        self.invity_api_key = (
            hashlib.sha256(descriptor.encode("utf-8")).hexdigest()
            if descriptor
            else None
        )
        self.country = (
            country or os.getenv("HWR_QUOTE_COUNTRY", "NL")
        ).strip().upper()
        if len(self.country) != 2 or not self.country.isalpha():
            raise ValueError("HWR_QUOTE_COUNTRY must be a two-letter country code")
        if payment_methods is None:
            configured_methods = os.getenv("HWR_INVITY_PAYMENT_METHODS", "")
            payment_methods = configured_methods.split(",")
        self.payment_methods = frozenset(
            method.strip().lower()
            for method in payment_methods
            if method.strip()
        )
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (compatible; hardware-wallet-rebalancer/0.1; "
                    "+https://github.com/codenibler/hardware_wallet_rebalancer)"
                ),
                "Accept": "application/json",
            }
        )

    def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        label: str,
    ) -> object:
        try:
            response = self.session.get(
                url,
                params=params,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise _VenueError(f"{label} market-data request failed") from exc

    def _post_json(
        self,
        url: str,
        *,
        body: Mapping[str, object],
        headers: Mapping[str, str] | None = None,
        label: str,
    ) -> object:
        try:
            response = self.session.post(
                url,
                json=body,
                headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise _VenueError(f"{label} quote request failed") from exc

    def _quote(
        self,
        *,
        asset: str,
        exchange: str,
        ask: object,
        bid: object,
        ask_size: object,
        bid_size: object,
        quoted_at: datetime,
        trade_url: str,
    ) -> ExchangeQuote:
        ask_value = _decimal(ask, EXCHANGE_NAMES[exchange])
        bid_value = _decimal(bid, EXCHANGE_NAMES[exchange])
        ask_size_value = _decimal(ask_size, EXCHANGE_NAMES[exchange])
        bid_size_value = _decimal(bid_size, EXCHANGE_NAMES[exchange])
        if (
            ask_value <= ZERO
            or bid_value <= ZERO
            or ask_size_value <= ZERO
            or bid_size_value <= ZERO
        ):
            raise _VenueError(
                f"{EXCHANGE_NAMES[exchange]} {asset}/EUR has no usable quote"
            )
        if ask_value < bid_value:
            raise _VenueError(
                f"{EXCHANGE_NAMES[exchange]} {asset}/EUR order book is crossed"
            )
        return ExchangeQuote(
            asset=asset,
            exchange_id=exchange,
            exchange_name=EXCHANGE_NAMES[exchange],
            pair=f"{asset}-EUR",
            ask_eur=ask_value,
            ask_size=ask_size_value,
            bid_eur=bid_value,
            bid_size=bid_size_value,
            taker_fee_bps=self.fees[exchange],
            quoted_at=quoted_at,
            trade_url=trade_url,
        )

    def _fetch_bitvavo(self) -> tuple[list[ExchangeQuote], list[str]]:
        label = EXCHANGE_NAMES["bitvavo"]
        data = self._get_json(
            "https://api.bitvavo.com/v2/ticker/book",
            label=label,
        )
        if not isinstance(data, list):
            raise _VenueError(f"{label} returned an unexpected response")
        by_market = {
            str(row.get("market")): row
            for row in data
            if isinstance(row, dict)
        }
        fetched_at = _utc_now()
        quotes: list[ExchangeQuote] = []
        failures: list[str] = []
        for asset in ASSETS:
            pair = f"{asset}-EUR"
            row = by_market.get(pair)
            if row is None:
                failures.append(f"{label} does not currently quote {pair}")
                continue
            try:
                quotes.append(
                    self._quote(
                        asset=asset,
                        exchange="bitvavo",
                        ask=row.get("ask"),
                        bid=row.get("bid"),
                        ask_size=row.get("askSize"),
                        bid_size=row.get("bidSize"),
                        quoted_at=fetched_at,
                        trade_url=(
                            "https://account.bitvavo.com/markets/"
                            f"{asset}-EUR"
                        ),
                    )
                )
            except _VenueError as exc:
                failures.append(str(exc))
        return quotes, failures

    @staticmethod
    def _kraken_row(
        asset: str,
        result: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        aliases = {"BTC": ("XBT", "BTC"), "ETH": ("ETH",)}
        tokens = aliases.get(asset, (asset,))
        for pair_name, row in result.items():
            upper_name = pair_name.upper()
            if any(token in upper_name for token in tokens) and isinstance(
                row,
                dict,
            ):
                return row
        return None

    def _fetch_kraken(self) -> tuple[list[ExchangeQuote], list[str]]:
        label = EXCHANGE_NAMES["kraken"]
        data = self._get_json(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": "BTCEUR,ETHEUR,SOLEUR,LINKEUR"},
            label=label,
        )
        if not isinstance(data, dict):
            raise _VenueError(f"{label} returned an unexpected response")
        errors = data.get("error")
        result = data.get("result")
        if errors or not isinstance(result, dict):
            raise _VenueError(f"{label} returned an API error")
        fetched_at = _utc_now()
        quotes: list[ExchangeQuote] = []
        failures: list[str] = []
        for asset in ASSETS:
            row = self._kraken_row(asset, result)
            if row is None:
                failures.append(f"{label} does not currently quote {asset}-EUR")
                continue
            ask = row.get("a")
            bid = row.get("b")
            if (
                not isinstance(ask, list)
                or len(ask) < 3
                or not isinstance(bid, list)
                or len(bid) < 3
            ):
                failures.append(f"{label} {asset}/EUR returned malformed data")
                continue
            try:
                quotes.append(
                    self._quote(
                        asset=asset,
                        exchange="kraken",
                        ask=ask[0],
                        bid=bid[0],
                        ask_size=ask[2],
                        bid_size=bid[2],
                        quoted_at=fetched_at,
                        trade_url=(
                            "https://pro.kraken.com/app/trade/"
                            f"{asset.lower()}-eur"
                        ),
                    )
                )
            except _VenueError as exc:
                failures.append(str(exc))
        return quotes, failures

    def _fetch_coinbase(self) -> tuple[list[ExchangeQuote], list[str]]:
        label = EXCHANGE_NAMES["coinbase"]
        quotes: list[ExchangeQuote] = []
        failures: list[str] = []
        for asset in ASSETS:
            pair = f"{asset}-EUR"
            try:
                data = self._get_json(
                    f"https://api.exchange.coinbase.com/products/{pair}/book",
                    params={"level": 1},
                    label=f"{label} {pair}",
                )
                if not isinstance(data, dict):
                    raise _VenueError(
                        f"{label} {pair} returned an unexpected response"
                    )
                asks = data.get("asks")
                bids = data.get("bids")
                if (
                    not isinstance(asks, list)
                    or not asks
                    or not isinstance(asks[0], list)
                    or len(asks[0]) < 2
                    or not isinstance(bids, list)
                    or not bids
                    or not isinstance(bids[0], list)
                    or not bids[0]
                ):
                    raise _VenueError(f"{label} {pair} returned malformed data")
                quotes.append(
                    self._quote(
                        asset=asset,
                        exchange="coinbase",
                        ask=asks[0][0],
                        bid=bids[0][0],
                        ask_size=asks[0][1],
                        bid_size=bids[0][1],
                        quoted_at=_utc_now(),
                        trade_url=(
                            "https://www.coinbase.com/advanced-trade/spot/"
                            f"{pair}"
                        ),
                    )
                )
            except _VenueError as exc:
                failures.append(str(exc))
        return quotes, failures

    def _fetch_okx(self) -> tuple[list[ExchangeQuote], list[str]]:
        label = EXCHANGE_NAMES["okx"]
        response = self._get_json(
            "https://www.okx.com/api/v5/market/tickers",
            params={"instType": "SPOT"},
            label=label,
        )
        if not isinstance(response, dict) or response.get("code") != "0":
            raise _VenueError(f"{label} returned an API error")
        data = response.get("data")
        if not isinstance(data, list):
            raise _VenueError(f"{label} returned an unexpected response")
        by_pair = {
            str(row.get("instId")): row
            for row in data
            if isinstance(row, dict)
        }
        quotes: list[ExchangeQuote] = []
        failures: list[str] = []
        for asset in ASSETS:
            pair = f"{asset}-EUR"
            row = by_pair.get(pair)
            if row is None:
                failures.append(f"{label} does not currently quote {pair}")
                continue
            try:
                timestamp_ms = _decimal(row.get("ts"), label)
                quoted_at = datetime.fromtimestamp(
                    float(timestamp_ms / Decimal("1000")),
                    timezone.utc,
                )
                quotes.append(
                    self._quote(
                        asset=asset,
                        exchange="okx",
                        ask=row.get("askPx"),
                        bid=row.get("bidPx"),
                        ask_size=row.get("askSz"),
                        bid_size=row.get("bidSz"),
                        quoted_at=quoted_at,
                        trade_url=(
                            "https://www.okx.com/en-eu/trade-spot/"
                            f"{asset.lower()}-eur"
                        ),
                    )
                )
            except (OSError, OverflowError, _VenueError, ValueError) as exc:
                if isinstance(exc, _VenueError):
                    failures.append(str(exc))
                else:
                    failures.append(f"{label} {asset}/EUR has an invalid timestamp")
        return quotes, failures

    def _invity_headers(self) -> dict[str, str]:
        if self.invity_api_key is None:
            raise _VenueError("Invity quote comparison is not configured")
        trace_id = hashlib.sha256(
            self.invity_api_key.encode("ascii")
        ).hexdigest()
        return {
            "X-SuiteA-Api": self.invity_api_key,
            "X-Trace-Id": trace_id,
        }

    @staticmethod
    def _invity_provider_id(raw_exchange: object) -> str:
        provider = str(raw_exchange).lower()
        return (
            provider[: -len("-sell")]
            if provider.endswith("-sell")
            else provider
        )

    def _invity_quote(
        self,
        *,
        asset: str,
        side: str,
        offer: Mapping[str, object],
        requested_amount: Decimal,
    ) -> ExchangeQuote:
        provider = self._invity_provider_id(offer.get("exchange"))
        if provider not in INVITY_PROVIDER_NAMES:
            raise _VenueError("Invity returned an unknown provider")

        fiat_amount = _decimal(
            offer.get("fiatStringAmount"),
            INVITY_PROVIDER_NAMES[provider],
        )
        crypto_field = (
            "receiveStringAmount" if side == "BUY" else "cryptoStringAmount"
        )
        crypto_amount = _decimal(
            offer.get(crypto_field),
            INVITY_PROVIDER_NAMES[provider],
        )
        if fiat_amount <= ZERO or crypto_amount <= ZERO:
            raise _VenueError(
                f"{INVITY_PROVIDER_NAMES[provider]} returned an unusable quote"
            )

        effective_price = fiat_amount / crypto_amount
        market_rate = _decimal(
            offer.get("rate") or effective_price,
            INVITY_PROVIDER_NAMES[provider],
        )
        if market_rate <= ZERO:
            market_rate = effective_price
        if side == "BUY":
            included_fee = fiat_amount - (crypto_amount * market_rate)
        else:
            included_fee = (crypto_amount * market_rate) - fiat_amount
        included_fee = max(included_fee, ZERO)

        minimum = _decimal(
            offer.get("minCrypto") or ZERO,
            INVITY_PROVIDER_NAMES[provider],
        )
        maximum = _decimal(
            offer.get("maxCrypto") or requested_amount,
            INVITY_PROVIDER_NAMES[provider],
        )
        if minimum < ZERO or maximum <= ZERO or minimum > maximum:
            raise _VenueError(
                f"{INVITY_PROVIDER_NAMES[provider]} returned invalid limits"
            )

        payment_method = str(
            offer.get("paymentMethodName")
            or offer.get("paymentMethod")
            or ""
        ).strip()
        exchange_name = INVITY_PROVIDER_NAMES[provider]
        if payment_method:
            exchange_name = f"{exchange_name} ({payment_method})"

        side_set = frozenset((side,))
        if side == "BUY":
            ask_min_size, ask_size = minimum, maximum
            bid_min_size, bid_size = ZERO, ZERO
        else:
            ask_min_size, ask_size = ZERO, ZERO
            bid_min_size, bid_size = minimum, maximum

        return ExchangeQuote(
            asset=asset,
            exchange_id=provider,
            exchange_name=exchange_name,
            pair=f"{asset}-EUR",
            ask_eur=effective_price,
            ask_size=ask_size,
            bid_eur=effective_price,
            bid_size=bid_size,
            taker_fee_bps=ZERO,
            quoted_at=_utc_now(),
            trade_url=INVITY_TRADE_URLS[provider],
            ask_min_size=ask_min_size,
            bid_min_size=bid_min_size,
            supported_sides=side_set,
            fee_eur_override=included_fee,
            fee_included_in_quote=True,
        )

    def _fetch_invity(
        self,
        trades: Sequence[TradeInstruction],
        providers: frozenset[str],
    ) -> tuple[list[ExchangeQuote], list[str]]:
        headers = self._invity_headers()
        quotes: list[ExchangeQuote] = []
        failures: list[str] = []

        for trade in trades:
            crypto_id = INVITY_ASSET_IDS[trade.asset]
            if trade.side == "BUY":
                endpoint = "https://exchange.trezor.io/api/v3/buy/quotes"
                body: dict[str, object] = {
                    "receiveCurrency": crypto_id,
                    "fiatCurrency": "EUR",
                    "cryptoStringAmount": str(trade.amount),
                    "wantCrypto": True,
                    "country": self.country,
                }
            else:
                endpoint = (
                    "https://exchange.trezor.io/api/v3/sell/fiat/quotes"
                )
                body = {
                    "cryptoCurrency": crypto_id,
                    "fiatCurrency": "EUR",
                    "cryptoStringAmount": str(trade.amount),
                    "amountInCrypto": True,
                    "country": self.country,
                    "flows": ["BANK_ACCOUNT", "PAYMENT_GATE"],
                }

            data = self._post_json(
                endpoint,
                body=body,
                headers=headers,
                label=f"Invity {trade.asset} {trade.side.lower()}",
            )
            if not isinstance(data, list):
                failures.append(
                    f"Invity {trade.asset} returned an unexpected response"
                )
                continue

            candidates: dict[str, list[ExchangeQuote]] = {
                provider: [] for provider in providers
            }
            for raw_offer in data:
                if not isinstance(raw_offer, dict) or raw_offer.get("error"):
                    continue
                provider = self._invity_provider_id(
                    raw_offer.get("exchange")
                )
                if provider not in providers:
                    continue
                method = str(raw_offer.get("paymentMethod", "")).lower()
                if self.payment_methods and method not in self.payment_methods:
                    continue
                try:
                    candidates[provider].append(
                        self._invity_quote(
                            asset=trade.asset,
                            side=trade.side,
                            offer=raw_offer,
                            requested_amount=trade.amount,
                        )
                    )
                except _VenueError as exc:
                    failures.append(str(exc))

            for provider, provider_quotes in candidates.items():
                usable = [
                    quote
                    for quote in provider_quotes
                    if quote.covers(trade.side, trade.amount)
                ]
                pool = usable or provider_quotes
                if not pool:
                    failures.append(
                        f"{INVITY_PROVIDER_NAMES[provider]} did not return a "
                        f"usable {trade.asset} {trade.side.lower()} quote"
                    )
                    continue
                best = (
                    min(
                        pool,
                        key=lambda quote: quote.effective_unit_price_eur(
                            trade.side
                        ),
                    )
                    if trade.side == "BUY"
                    else max(
                        pool,
                        key=lambda quote: quote.effective_unit_price_eur(
                            trade.side
                        ),
                    )
                )
                quotes.append(best)

        return quotes, failures

    def fetch_markets(
        self,
        *,
        trades: Sequence[TradeInstruction] = (),
    ) -> VenueMarketSnapshot:
        """Fetch one reusable market snapshot for all proposed orders."""

        fetchers = {
            "bitvavo": self._fetch_bitvavo,
            "kraken": self._fetch_kraken,
            "coinbase": self._fetch_coinbase,
            "okx": self._fetch_okx,
        }
        quotes: list[ExchangeQuote] = []
        failures: list[str] = []
        for exchange in self.exchanges:
            if exchange in INVITY_PROVIDERS:
                continue
            try:
                venue_quotes, venue_failures = fetchers[exchange]()
                quotes.extend(venue_quotes)
                failures.extend(venue_failures)
            except _VenueError as exc:
                failures.append(str(exc))

        requested_invity_providers = frozenset(self.exchanges) & frozenset(
            INVITY_PROVIDERS
        )
        if requested_invity_providers and trades:
            try:
                venue_quotes, venue_failures = self._fetch_invity(
                    trades,
                    requested_invity_providers,
                )
                quotes.extend(venue_quotes)
                failures.extend(venue_failures)
            except _VenueError as exc:
                failures.append(str(exc))

        if not quotes:
            raise ExchangeScanError(
                "No supported venue returned a usable EUR quote"
            )

        return VenueMarketSnapshot(
            as_of=_utc_now(),
            quotes={
                asset: tuple(
                    quote for quote in quotes if quote.asset == asset
                )
                for asset in ASSETS
            },
            failures=tuple(failures),
        )
