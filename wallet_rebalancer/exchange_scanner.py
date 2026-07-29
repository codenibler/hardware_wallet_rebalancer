"""Fee-aware comparison of public EUR spot quotes on supported exchanges."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

import requests

from .models import ASSETS, ZERO


SUPPORTED_EXCHANGES = ("bitvavo", "kraken", "coinbase", "okx")
EXCHANGE_NAMES = {
    "bitvavo": "Bitvavo",
    "kraken": "Kraken Pro",
    "coinbase": "Coinbase Advanced",
    "okx": "OKX Europe",
}
DEFAULT_TAKER_FEE_BPS = {
    "bitvavo": Decimal("25"),
    "kraken": Decimal("80"),
    "coinbase": Decimal("60"),
    "okx": Decimal("35"),
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
    for exchange in SUPPORTED_EXCHANGES:
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

    def effective_unit_price_eur(self, side: str) -> Decimal:
        fee_rate = self.taker_fee_bps / Decimal("10000")
        if side == "BUY":
            return self.ask_eur * (Decimal("1") + fee_rate)
        if side == "SELL":
            return self.bid_eur * (Decimal("1") - fee_rate)
        raise ValueError("Trade side must be BUY or SELL")

    def execution_price_eur(self, side: str) -> Decimal:
        if side == "BUY":
            return self.ask_eur
        if side == "SELL":
            return self.bid_eur
        raise ValueError("Trade side must be BUY or SELL")

    def covers(self, side: str, amount: Decimal) -> bool:
        available = self.ask_size if side == "BUY" else self.bid_size
        return available >= amount

    def estimated_fee_eur(self, side: str, amount: Decimal) -> Decimal:
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
            "taker_fee_bps": str(self.taker_fee_bps),
            "side": side,
            "trade_amount": str(amount),
            "effective_unit_price_eur": str(
                self.effective_unit_price_eur(side)
            ),
            "estimated_fee_eur": str(self.estimated_fee_eur(side, amount)),
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
            sorted(self.quotes.get(asset, ()), key=sort_key)[:limit]
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

    def fetch_markets(self) -> VenueMarketSnapshot:
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
            try:
                venue_quotes, venue_failures = fetchers[exchange]()
                quotes.extend(venue_quotes)
                failures.extend(venue_failures)
            except _VenueError as exc:
                failures.append(str(exc))

        if not quotes:
            raise ExchangeScanError(
                "No supported exchange returned a usable EUR spot quote"
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
