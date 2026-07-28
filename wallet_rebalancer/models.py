"""Domain models shared by providers, planning, and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping


ASSETS = ("BTC", "ETH", "SOL", "LINK")
TARGET_WEIGHTS = {
    "BTC": Decimal("0.50"),
    "ETH": Decimal("0.25"),
    "SOL": Decimal("0.15"),
    "LINK": Decimal("0.10"),
}
ZERO = Decimal("0")


def decimal_map(
    values: Mapping[str, Decimal | str | int | float],
    *,
    require_all: bool = True,
) -> dict[str, Decimal]:
    """Normalize an asset map while rejecting unexpected or negative values."""

    unexpected = sorted(set(values) - set(ASSETS))
    if unexpected:
        raise ValueError(f"Unexpected assets: {unexpected}")
    if require_all:
        missing = sorted(set(ASSETS) - set(values))
        if missing:
            raise ValueError(f"Missing assets: {missing}")

    try:
        normalized = {
            asset: Decimal(str(values.get(asset, ZERO))) for asset in ASSETS
        }
    except (ArithmeticError, ValueError) as exc:
        raise ValueError("Asset amounts must be valid decimal numbers") from exc
    if any(not value.is_finite() for value in normalized.values()):
        raise ValueError("Asset amounts must be finite")
    if any(value < ZERO for value in normalized.values()):
        raise ValueError("Asset amounts cannot be negative")
    return normalized


@dataclass(frozen=True)
class Holdings:
    amounts: Mapping[str, Decimal | str | int | float]
    pending_bitcoin: Decimal = ZERO
    fetched_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def normalized(self) -> dict[str, Decimal]:
        return decimal_map(self.amounts)


@dataclass(frozen=True)
class PriceBook:
    prices_eur: Mapping[str, Decimal | str | int | float]
    as_of: datetime
    source: str = "CoinGecko"

    def normalized(self) -> dict[str, Decimal]:
        prices = decimal_map(self.prices_eur)
        if any(value <= ZERO for value in prices.values()):
            raise ValueError("All prices must be strictly positive")
        return prices


@dataclass(frozen=True)
class TradeInstruction:
    asset: str
    side: str
    amount: Decimal
    notional_eur: Decimal
    snapshot_price_eur: Decimal


@dataclass(frozen=True)
class AssetPlan:
    asset: str
    amount: Decimal
    price_eur: Decimal
    current_value_eur: Decimal
    current_weight: Decimal
    target_weight: Decimal
    drift: Decimal
    desired_value_eur: Decimal
    desired_amount: Decimal
    trade_value_eur: Decimal


@dataclass(frozen=True)
class PortfolioPlan:
    assets: tuple[AssetPlan, ...]
    trades: tuple[TradeInstruction, ...]
    current_total_eur: Decimal
    top_up_eur: Decimal
    estimated_fee_bps: Decimal
    estimated_fees_eur: Decimal
    desired_invested_total_eur: Decimal
    threshold: Decimal
    max_abs_drift: Decimal
    threshold_rebalance_needed: bool
    minimum_top_up_for_buy_only_eur: Decimal
    prices_as_of: datetime
    holdings_as_of: datetime
    price_source: str
    pending_bitcoin: Decimal = ZERO

    @property
    def has_top_up(self) -> bool:
        return self.top_up_eur > ZERO

    @property
    def has_trade_plan(self) -> bool:
        return self.threshold_rebalance_needed or self.has_top_up
