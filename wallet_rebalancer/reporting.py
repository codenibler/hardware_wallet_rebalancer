"""Human-readable and machine-readable portfolio plan output."""

from __future__ import annotations

import json
from decimal import Decimal
from html import escape
from typing import Any

from .models import ZERO, PortfolioPlan

AMOUNT_DECIMALS = {"BTC": 8, "ETH": 8, "SOL": 8, "LINK": 6}


def _money(value: Decimal) -> str:
    return f"€{value:,.2f}"


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
        f"Current value:    {_money(plan.current_total_eur)}",
        f"Top-up capital:   {_money(plan.top_up_eur)}",
        f"Post-fee target:  {_money(plan.desired_invested_total_eur)}",
        (
            f"Estimated fees:   {_money(plan.estimated_fees_eur)} "
            f"({plan.estimated_fee_bps} bps)"
        ),
        "",
        "CURRENT AND DESIRED HOLDINGS",
        "Asset | Units now | EUR now | Weight | Target | Desired units | Desired EUR",
    ]
    for asset in plan.assets:
        lines.append(
            " | ".join(
                [
                    asset.asset,
                    _amount(asset.asset, asset.amount),
                    _money(asset.current_value_eur),
                    _percent(asset.current_weight),
                    _percent(asset.target_weight),
                    _amount(asset.asset, asset.desired_amount),
                    _money(asset.desired_value_eur),
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
                    f"{_money(trade.notional_eur)} at "
                    f"{_money(trade.snapshot_price_eur)}/{trade.asset}"
                )

        buys = sum(
            (
                trade.notional_eur
                for trade in plan.trades
                if trade.side == "BUY"
            ),
            ZERO,
        )
        sells = sum(
            (
                trade.notional_eur
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
                    f"= {_money(buys - sells + plan.estimated_fees_eur)}"
                ),
            ]
        )
        if sells > ZERO and plan.top_up_eur > ZERO:
            lines.append(
                "A buy-only exact rebalance would require a top-up of at least "
                f"{_money(plan.minimum_top_up_for_buy_only_eur)} at this snapshot."
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

    return "\n".join(lines)


def render_order_message(plan: PortfolioPlan) -> str:
    """Render proposed trades for Telegram using the configured fee estimate."""

    if plan.threshold_rebalance_needed:
        lines = [
            "Greetings cryptopian. It seems your portfolio is out of balance.",
            "",
            (
                f"The divergence of {_percent(plan.max_abs_drift)} has reached "
                f"or exceeded the {_percent(plan.threshold)} threshold. This is "
                "how to return it to the desired state."
                " The configured fee estimate is used below."
            ),
            "",
        ]
    elif plan.has_top_up:
        lines = [
            "Greetings cryptopian. Your portfolio is in balance.",
            "",
            (
                f"The divergence of {_percent(plan.max_abs_drift)} is below the "
                f"{_percent(plan.threshold)} threshold, so no threshold "
                "rebalance is needed. This is how to invest the new capital "
                "while returning to the desired allocation."
                " The configured fee estimate is used below."
            ),
            "",
        ]
    else:
        lines = [
            "Greetings cryptopian. Your portfolio is in balance.",
            "",
            (
                f"The divergence of {_percent(plan.max_abs_drift)} is below the "
                f"{_percent(plan.threshold)} threshold. No rebalancing trades "
                "are needed."
            ),
        ]

    if not plan.has_trade_plan or not plan.trades:
        return "\n".join(lines)

    fee_rate = plan.estimated_fee_bps / Decimal("10000")
    order_lines = ["These are the planned orders (not submitted):", ""]
    for trade in plan.trades:
        marker = "🔴" if trade.side == "SELL" else "🟢"
        trade_fee = trade.notional_eur * fee_rate

        order_lines.append(
            f"{marker} {trade.asset}, "
            f"{_amount(trade.asset, trade.amount)} {trade.asset}, "
            f"{_money(trade.notional_eur)}, "
            f"fee≈{_money(trade_fee)}"
        )

    lines.append(f"<pre>{escape(chr(10).join(order_lines))}</pre>")
    lines.extend(
        [
            "",
            (
                f"Estimated total fees: {_money(plan.estimated_fees_eur)} "
                f"({plan.estimated_fee_bps} bps on gross traded value)"
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
            "price_eur": str(row.price_eur),
            "current_value_eur": str(row.current_value_eur),
            "current_weight": str(row.current_weight),
            "target_weight": str(row.target_weight),
            "drift": str(row.drift),
            "desired_amount": str(row.desired_amount),
            "desired_value_eur": str(row.desired_value_eur),
            "trade_value_eur": str(row.trade_value_eur),
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
        "current_total_eur": str(plan.current_total_eur),
        "top_up_eur": str(plan.top_up_eur),
        "estimated_fee_bps": str(plan.estimated_fee_bps),
        "estimated_fees_eur": str(plan.estimated_fees_eur),
        "desired_invested_total_eur": str(plan.desired_invested_total_eur),
        "minimum_top_up_for_buy_only_eur": str(
            plan.minimum_top_up_for_buy_only_eur
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
                "notional_eur": str(trade.notional_eur),
                "snapshot_price_eur": str(trade.snapshot_price_eur),
            }
            for trade in plan.trades
        ],
    }


def render_json(plan: PortfolioPlan) -> str:
    return json.dumps(plan_to_dict(plan), indent=2) + "\n"
