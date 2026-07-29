"""Fee-aware comparison of public EUR spot quotes on supported exchanges."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

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
AMOUNT_DECIMALS = {"BTC": 8, "ETH": 8, "SOL": 6, "LINK": 6}


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
    taker_fee_bps: Decimal
    quoted_at: datetime
    trade_url: str

    @property
    def effective_unit_price_eur(self) -> Decimal:
        return self.ask_eur * (
            Decimal("1") + self.taker_fee_bps / Decimal("10000")
        )

    def estimated_crypto(self, purchase_eur: Decimal) -> Decimal:
        return purchase_eur / self.effective_unit_price_eur

    def covers(self, purchase_eur: Decimal) -> bool:
        return (
            self.ask_size * self.effective_unit_price_eur >= purchase_eur
        )

    def to_dict(self, purchase_eur: Decimal) -> dict[str, object]:
        return {
            "asset": self.asset,
            "exchange_id": self.exchange_id,
            "exchange_name": self.exchange_name,
            "pair": self.pair,
            "ask_eur": str(self.ask_eur),
            "ask_size": str(self.ask_size),
            "taker_fee_bps": str(self.taker_fee_bps),
            "effective_unit_price_eur": str(self.effective_unit_price_eur),
            "estimated_crypto": str(self.estimated_crypto(purchase_eur)),
            "covers_purchase_at_best_ask": self.covers(purchase_eur),
            "quoted_at": _iso(self.quoted_at),
            "trade_url": self.trade_url,
        }


@dataclass(frozen=True)
class ExchangeScanResult:
    purchase_eur: Decimal
    as_of: datetime
    rankings: Mapping[str, tuple[ExchangeQuote, ...]]
    failures: tuple[str, ...]

    def to_dict(self, *, limit: int = 3) -> dict[str, object]:
        return {
            "purchase_eur": str(self.purchase_eur),
            "as_of": _iso(self.as_of),
            "method": "best EUR ask plus configured taker fee",
            "rankings": {
                asset: [
                    quote.to_dict(self.purchase_eur)
                    for quote in self.rankings.get(asset, ())[:limit]
                ]
                for asset in ASSETS
            },
            "failures": list(self.failures),
        }


class ExchangeScanner:
    """Fetch and rank top-of-book EUR asks from Dutch-accessible venues."""

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
        quoted_at: datetime,
        trade_url: str,
    ) -> ExchangeQuote:
        ask_value = _decimal(ask, EXCHANGE_NAMES[exchange])
        bid_value = _decimal(bid, EXCHANGE_NAMES[exchange])
        size_value = _decimal(ask_size, EXCHANGE_NAMES[exchange])
        if ask_value <= ZERO or bid_value <= ZERO or size_value <= ZERO:
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
            ask_size=size_value,
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
                or not bid
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

    def scan(
        self,
        purchase_eur: Decimal | str | int | float = Decimal("1000"),
    ) -> ExchangeScanResult:
        """Rank supported exchange quotes by ask price plus taker fee."""

        try:
            purchase = Decimal(str(purchase_eur))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Purchase amount must be a decimal number") from exc
        if not purchase.is_finite() or purchase <= ZERO:
            raise ValueError("Purchase amount must be positive and finite")

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

        rankings = {
            asset: tuple(
                sorted(
                    (quote for quote in quotes if quote.asset == asset),
                    key=lambda quote: (
                        not quote.covers(purchase),
                        quote.effective_unit_price_eur,
                        quote.exchange_name,
                    ),
                )
            )
            for asset in ASSETS
        }
        return ExchangeScanResult(
            purchase_eur=purchase,
            as_of=_utc_now(),
            rankings=rankings,
            failures=tuple(failures),
        )


def render_exchange_scan(
    result: ExchangeScanResult,
    *,
    limit: int = 3,
) -> str:
    """Render top fee-adjusted buy venues for terminal or Telegram."""

    lines = [
        "DUTCH EUR CRYPTO EXCHANGE SCAN",
        (
            f"Comparing an immediate €{result.purchase_eur:,.2f} taker buy "
            "using live best asks plus configured trading fees."
        ),
        (
            "Excludes deposit, withdrawal, network, and price impact beyond "
            "the displayed best ask."
        ),
    ]
    for asset in ASSETS:
        lines.extend(["", asset])
        quotes = result.rankings.get(asset, ())[:limit]
        if not quotes:
            lines.append("No usable direct EUR quote was returned.")
            continue
        for rank, quote in enumerate(quotes, start=1):
            amount = quote.estimated_crypto(result.purchase_eur)
            depth_warning = "" if quote.covers(result.purchase_eur) else " ⚠ shallow"
            lines.append(
                f"{rank}. {quote.exchange_name}: "
                f"ask €{quote.ask_eur:,.2f}, "
                f"fee {quote.taker_fee_bps / 100:.2f}%, "
                f"effective €{quote.effective_unit_price_eur:,.2f}/{asset}, "
                f"receive≈{amount:,.{AMOUNT_DECIMALS[asset]}f} {asset}"
                f"{depth_warning}"
            )
    if result.failures:
        lines.extend(
            [
                "",
                (
                    f"Coverage note: {len(result.failures)} unavailable or "
                    "invalid venue/market quote(s) were skipped."
                ),
            ]
        )
    lines.extend(
        [
            "",
            (
                "Check the final order preview and withdrawal fee before "
                "buying; quotes can change immediately."
            ),
        ]
    )
    return "\n".join(lines)
