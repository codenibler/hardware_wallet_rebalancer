"""Human-readable and machine-readable portfolio plan output."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .models import ASSETS, ZERO, PortfolioPlan


AMOUNT_DECIMALS = {"BTC": 8, "ETH": 8, "SOL": 8, "LINK": 6}


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _percent(value: Decimal, *, signed: bool = False) -> str:
    prefix = "+" if signed and value > ZERO else ""
    return f"{prefix}{value * 100:.2f}%"


def _amount(asset: str, value: Decimal) -> str:
    return f"{value:,.{AMOUNT_DECIMALS[asset]}f}"


def _utc(value: object) -> str:
    return getattr(value, "isoformat")().replace("+00:00", "Z")


def render_text(plan: PortfolioPlan) -> str:
    """Render a compact report suitable for a terminal or Telegram."""

    if plan.threshold_rebalance_needed:
        status = "REBALANCE NEEDED"
        reason = (
            f"Maximum allocation drift {_percent(plan.max_abs_drift)} "
            f"meets/exceeds the {_percent(plan.threshold)} threshold."
        )
    elif plan.has_top_up:
        status = "NO THRESHOLD REBALANCE NEEDED — TOP-UP PLAN AVAILABLE"
        reason = (
            f"Maximum allocation drift {_percent(plan.max_abs_drift)} is below "
            f"the {_percent(plan.threshold)} threshold. The transactions below "
            "deploy the requested top-up while returning exactly to target at "
            "snapshot prices."
        )
    else:
        status = "NO REBALANCE NEEDED"
        reason = (
            f"Maximum allocation drift {_percent(plan.max_abs_drift)} is below "
            f"the {_percent(plan.threshold)} threshold."
        )

    lines = [
        "HARDWARE WALLET PORTFOLIO CHECK",
        f"STATUS: {status}",
        reason,
        "",
        f"Holdings fetched: {_utc(plan.holdings_as_of)}",
        f"Prices as of:     {_utc(plan.prices_as_of)} ({plan.price_source})",
        f"Current value:    {_money(plan.current_total_usd)}",
        f"Top-up capital:   {_money(plan.top_up_usd)}",
        f"Post-fee target:  {_money(plan.desired_invested_total_usd)}",
        f"Estimated fees:   {_money(plan.estimated_fees_usd)}",
        "",
        "CURRENT AND DESIRED HOLDINGS",
        "Asset | Units now | USD now | Weight | Target | Desired units | Desired USD",
    ]
    for asset in plan.assets:
        lines.append(
            " | ".join(
                [
                    asset.asset,
                    _amount(asset.asset, asset.amount),
                    _money(asset.current_value_usd),
                    _percent(asset.current_weight),
                    _percent(asset.target_weight),
                    _amount(asset.asset, asset.desired_amount),
                    _money(asset.desired_value_usd),
                ]
            )
        )

    if plan.has_trade_plan:
        lines.extend(["", "INDICATIVE TRANSACTIONS"])
        if not plan.trades:
            lines.append("No transaction is above the configured display minimum.")
        else:
            for trade in plan.trades:
                lines.append(
                    f"{trade.side:4} {_amount(trade.asset, trade.amount)} "
                    f"{trade.asset} for approximately "
                    f"{_money(trade.notional_usd)} at "
                    f"{_money(trade.snapshot_price_usd)}/{trade.asset}"
                )

        buys = sum(
            (
                trade.notional_usd
                for trade in plan.trades
                if trade.side == "BUY"
            ),
            ZERO,
        )
        sells = sum(
            (
                trade.notional_usd
                for trade in plan.trades
                if trade.side == "SELL"
            ),
            ZERO,
        )
        lines.extend(
            [
                "",
                f"Gross buys:  {_money(buys)}",
                f"Gross sells: {_money(sells)}",
                (
                    "Cash check: gross buys - gross sells + estimated fees "
                    f"= {_money(buys - sells + plan.estimated_fees_usd)}"
                ),
            ]
        )
        if sells > ZERO and plan.top_up_usd > ZERO:
            lines.append(
                "A buy-only exact rebalance would require a top-up of at least "
                f"{_money(plan.minimum_top_up_for_buy_only_usd)} at this snapshot."
            )

    if plan.pending_bitcoin != ZERO:
        lines.extend(
            [
                "",
                (
                    "Pending BTC reported separately: "
                    f"{_amount('BTC', plan.pending_bitcoin)} BTC. "
                    "See config to choose whether it is included."
                ),
            ]
        )

    lines.extend(
        [
            "",
            "SAFETY",
            (
                "This is a read-only plan, not a signed or broadcast transaction. "
                "Amounts use one price snapshot. Recheck prices, venue fees, spread, "
                "tax impact, network/gas costs, and every destination on the Trezor "
                "screen before executing."
            ),
        ]
    )
    return "\n".join(lines)


def plan_to_dict(plan: PortfolioPlan) -> dict[str, Any]:
    """Return a JSON-safe audit representation with decimals kept as strings."""

    assets = {}
    for row in plan.assets:
        assets[row.asset] = {
            "amount": str(row.amount),
            "price_usd": str(row.price_usd),
            "current_value_usd": str(row.current_value_usd),
            "current_weight": str(row.current_weight),
            "target_weight": str(row.target_weight),
            "drift": str(row.drift),
            "desired_amount": str(row.desired_amount),
            "desired_value_usd": str(row.desired_value_usd),
            "trade_value_usd": str(row.trade_value_usd),
        }
    return {
        "status": (
            "rebalance_needed"
            if plan.threshold_rebalance_needed
            else "no_rebalance_needed"
        ),
        "top_up_plan_included": plan.has_top_up,
        "threshold": str(plan.threshold),
        "max_abs_drift": str(plan.max_abs_drift),
        "current_total_usd": str(plan.current_total_usd),
        "top_up_usd": str(plan.top_up_usd),
        "estimated_fees_usd": str(plan.estimated_fees_usd),
        "desired_invested_total_usd": str(plan.desired_invested_total_usd),
        "minimum_top_up_for_buy_only_usd": str(
            plan.minimum_top_up_for_buy_only_usd
        ),
        "holdings_as_of": _utc(plan.holdings_as_of),
        "prices_as_of": _utc(plan.prices_as_of),
        "price_source": plan.price_source,
        "pending_bitcoin": str(plan.pending_bitcoin),
        "assets": assets,
        "trades": [
            {
                "side": trade.side,
                "asset": trade.asset,
                "amount": str(trade.amount),
                "notional_usd": str(trade.notional_usd),
                "snapshot_price_usd": str(trade.snapshot_price_usd),
            }
            for trade in plan.trades
        ],
    }


def render_json(plan: PortfolioPlan) -> str:
    return json.dumps(plan_to_dict(plan), indent=2) + "\n"
