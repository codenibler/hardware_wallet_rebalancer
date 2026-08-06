"""Threshold decision and fee-aware target trade calculation."""

from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from .models import (
    ASSETS,
    ZERO,
    AssetPlan,
    Holdings,
    PortfolioPlan,
    PriceBook,
    TradeInstruction,
)


def _solve_fee_adjusted_target(
    current_values: Mapping[str, Decimal],
    targets: Mapping[str, Decimal],
    top_up_eur: Decimal,
    fee_rate: Decimal,
) -> tuple[Decimal, dict[str, Decimal], Decimal]:
    """Solve final NAV = current NAV + cash - fees on gross traded euros."""

    pre_trade_total = sum(current_values.values(), ZERO) + top_up_eur
    post_trade_total = pre_trade_total
    for _ in range(200):
        deltas = {
            asset: targets[asset] * post_trade_total - current_values[asset]
            for asset in ASSETS
        }
        gross_traded = sum((abs(value) for value in deltas.values()), ZERO)
        fee = fee_rate * gross_traded
        updated_total = pre_trade_total - fee
        if abs(updated_total - post_trade_total) <= Decimal("0.00000001"):
            post_trade_total = updated_total
            break
        post_trade_total = updated_total
    else:  # pragma: no cover - realistic fee rates converge rapidly
        raise RuntimeError("Fee-adjusted trade plan did not converge")

    deltas = {
        asset: targets[asset] * post_trade_total - current_values[asset]
        for asset in ASSETS
    }
    gross_traded = sum((abs(value) for value in deltas.values()), ZERO)
    fee = fee_rate * gross_traded
    return post_trade_total, deltas, fee


def build_plan(
    holdings: Holdings,
    price_book: PriceBook,
    *,
    top_up_eur: Decimal | str | int | float = ZERO,
    threshold: Decimal | str | float = Decimal("0.05"),
    estimated_fee_bps: Decimal | str | float = ZERO,
    target_weights: Mapping[str, Decimal] | None = None,
) -> PortfolioPlan:
    """Build a non-executing portfolio rebalance plan from one market snapshot."""

    amounts = holdings.normalized()
    prices = price_book.normalized()
    targets = {
        asset: Decimal(str((target_weights or {}).get(asset, "0")))
        for asset in ASSETS
    }
    if not target_weights:
        from .models import TARGET_WEIGHTS

        targets = dict(TARGET_WEIGHTS)
    if any(value <= ZERO for value in targets.values()) or sum(
        targets.values(), ZERO
    ) != Decimal("1"):
        raise ValueError("Target weights must be positive and sum to 1")

    top_up = Decimal(str(top_up_eur))
    threshold_value = Decimal(str(threshold))
    fee_bps = Decimal(str(estimated_fee_bps))
    numeric_inputs = [top_up, threshold_value, fee_bps, *targets.values()]
    if any(not value.is_finite() for value in numeric_inputs):
        raise ValueError("Planning inputs must be finite")
    if not holdings.pending_bitcoin.is_finite():
        raise ValueError("Pending Bitcoin amount must be finite")
    if top_up < ZERO:
        raise ValueError("Top-up amount cannot be negative")
    if threshold_value < ZERO or threshold_value > Decimal("1"):
        raise ValueError("Threshold must be between 0 and 1")
    if fee_bps < ZERO or fee_bps > Decimal("1000"):
        raise ValueError("Estimated fee bps must be in [0, 1000]")

    current_values = {
        asset: amounts[asset] * prices[asset] for asset in ASSETS
    }
    current_total = sum(current_values.values(), ZERO)
    if current_total <= ZERO:
        raise ValueError("The current portfolio has no positive market value")

    current_weights = {
        asset: current_values[asset] / current_total for asset in ASSETS
    }
    drifts = {
        asset: current_weights[asset] - targets[asset] for asset in ASSETS
    }
    max_abs_drift = max(abs(value) for value in drifts.values())
    threshold_needed = (
        max_abs_drift > ZERO and max_abs_drift >= threshold_value
    )

    fee_rate = fee_bps / Decimal("10000")
    desired_total, trade_values, estimated_fees = _solve_fee_adjusted_target(
        current_values,
        targets,
        top_up,
        fee_rate,
    )

    # A top-up this large expands every target sleeve enough that no current
    # holding must be sold to reach the target exactly.
    minimum_buy_only_total = max(
        current_values[asset] / targets[asset] for asset in ASSETS
    )
    minimum_top_up_before_fees = max(
        ZERO,
        minimum_buy_only_total - current_total,
    )
    minimum_top_up_for_buy_only = minimum_top_up_before_fees * (
        Decimal("1") + fee_rate
    )

    asset_rows: list[AssetPlan] = []
    trades: list[TradeInstruction] = []
    for asset in ASSETS:
        desired_value = targets[asset] * desired_total
        desired_amount = desired_value / prices[asset]
        trade_value = trade_values[asset]
        asset_rows.append(
            AssetPlan(
                asset=asset,
                amount=amounts[asset],
                price_eur=prices[asset],
                current_value_eur=current_values[asset],
                current_weight=current_weights[asset],
                target_weight=targets[asset],
                drift=drifts[asset],
                desired_value_eur=desired_value,
                desired_amount=desired_amount,
                trade_value_eur=trade_value,
            )
        )
        if trade_value != ZERO:
            trades.append(
                TradeInstruction(
                    asset=asset,
                    side="BUY" if trade_value > ZERO else "SELL",
                    amount=abs(trade_value) / prices[asset],
                    notional_eur=abs(trade_value),
                    snapshot_price_eur=prices[asset],
                )
            )

    trades.sort(key=lambda trade: (trade.side != "SELL", trade.asset))
    return PortfolioPlan(
        assets=tuple(asset_rows),
        trades=tuple(trades),
        current_total_eur=current_total,
        top_up_eur=top_up,
        estimated_fee_bps=fee_bps,
        estimated_fees_eur=estimated_fees,
        desired_invested_total_eur=desired_total,
        threshold=threshold_value,
        max_abs_drift=max_abs_drift,
        threshold_rebalance_needed=threshold_needed,
        minimum_top_up_for_buy_only_eur=minimum_top_up_for_buy_only,
        prices_as_of=price_book.as_of,
        holdings_as_of=holdings.fetched_at,
        price_source=price_book.source,
        pending_bitcoin=holdings.pending_bitcoin,
    )


def build_buy_only_plan(
    holdings: Holdings,
    price_book: PriceBook,
    *,
    top_up_eur: Decimal | str | int | float,
    threshold: Decimal | str | float = Decimal("0.05"),
    estimated_fee_bps: Decimal | str | float = ZERO,
    target_weights: Mapping[str, Decimal] | None = None,
) -> PortfolioPlan:
    """Allocate a cash deposit without selling existing wallet assets.

    The allocator raises the most underweight target sleeves first.  It is the
    weighted equivalent of filling the lowest buckets until the investable
    deposit is exhausted, so assets that are already overweight receive no
    purchase until the other sleeves catch up.
    """

    amounts = holdings.normalized()
    prices = price_book.normalized()
    if target_weights is None:
        from .models import TARGET_WEIGHTS

        targets = dict(TARGET_WEIGHTS)
    else:
        targets = {
            asset: Decimal(str(target_weights.get(asset, ZERO)))
            for asset in ASSETS
        }

    top_up = Decimal(str(top_up_eur))
    threshold_value = Decimal(str(threshold))
    fee_bps = Decimal(str(estimated_fee_bps))
    numeric_inputs = [top_up, threshold_value, fee_bps, *targets.values()]
    if any(not value.is_finite() for value in numeric_inputs):
        raise ValueError("Planning inputs must be finite")
    if top_up <= ZERO:
        raise ValueError("Buy-only planning requires a positive top-up")
    if threshold_value < ZERO or threshold_value > Decimal("1"):
        raise ValueError("Threshold must be between 0 and 1")
    if fee_bps < ZERO or fee_bps > Decimal("1000"):
        raise ValueError("Estimated fee bps must be in [0, 1000]")
    if any(value <= ZERO for value in targets.values()) or sum(
        targets.values(), ZERO
    ) != Decimal("1"):
        raise ValueError("Target weights must be positive and sum to 1")

    current_values = {
        asset: amounts[asset] * prices[asset] for asset in ASSETS
    }
    current_total = sum(current_values.values(), ZERO)
    if current_total <= ZERO:
        raise ValueError("The current portfolio has no positive market value")

    fee_rate = fee_bps / Decimal("10000")
    investable = top_up / (Decimal("1") + fee_rate)
    estimated_fees = top_up - investable

    # A sleeve's level is the total portfolio value it would imply if that
    # sleeve were exactly on target: current_value / target_weight.  Raise the
    # lowest levels together until all investable cash has been assigned.
    ranked = sorted(
        ASSETS,
        key=lambda asset: current_values[asset] / targets[asset],
    )
    level = current_values[ranked[0]] / targets[ranked[0]]
    active = [ranked[0]]
    remaining = investable
    for asset in ranked[1:]:
        next_level = current_values[asset] / targets[asset]
        active_weight = sum((targets[item] for item in active), ZERO)
        cost = (next_level - level) * active_weight
        if remaining < cost:
            level += remaining / active_weight
            remaining = ZERO
            break
        remaining -= cost
        level = next_level
        active.append(asset)
    if remaining > ZERO:
        active_weight = sum((targets[item] for item in active), ZERO)
        level += remaining / active_weight

    purchases = {
        asset: max(ZERO, targets[asset] * level - current_values[asset])
        for asset in ASSETS
    }
    # Keep Decimal arithmetic exactly self-financing even if a future target
    # configuration introduces repeating ratios.
    purchase_residual = investable - sum(purchases.values(), ZERO)
    if purchase_residual:
        purchases[active[-1]] += purchase_residual

    current_weights = {
        asset: current_values[asset] / current_total for asset in ASSETS
    }
    drifts = {
        asset: current_weights[asset] - targets[asset] for asset in ASSETS
    }
    max_abs_drift = max(abs(value) for value in drifts.values())

    asset_rows: list[AssetPlan] = []
    trades: list[TradeInstruction] = []
    for asset in ASSETS:
        desired_value = current_values[asset] + purchases[asset]
        asset_rows.append(
            AssetPlan(
                asset=asset,
                amount=amounts[asset],
                price_eur=prices[asset],
                current_value_eur=current_values[asset],
                current_weight=current_weights[asset],
                target_weight=targets[asset],
                drift=drifts[asset],
                desired_value_eur=desired_value,
                desired_amount=desired_value / prices[asset],
                trade_value_eur=purchases[asset],
            )
        )
        if purchases[asset] > ZERO:
            trades.append(
                TradeInstruction(
                    asset=asset,
                    side="BUY",
                    amount=purchases[asset] / prices[asset],
                    notional_eur=purchases[asset],
                    snapshot_price_eur=prices[asset],
                )
            )

    minimum_buy_only_total = max(
        current_values[asset] / targets[asset] for asset in ASSETS
    )
    minimum_top_up_for_exact_target = max(
        ZERO,
        minimum_buy_only_total - current_total,
    ) * (Decimal("1") + fee_rate)

    return PortfolioPlan(
        assets=tuple(asset_rows),
        trades=tuple(trades),
        current_total_eur=current_total,
        top_up_eur=top_up,
        estimated_fee_bps=fee_bps,
        estimated_fees_eur=estimated_fees,
        desired_invested_total_eur=current_total + investable,
        threshold=threshold_value,
        max_abs_drift=max_abs_drift,
        threshold_rebalance_needed=(
            max_abs_drift > ZERO and max_abs_drift >= threshold_value
        ),
        minimum_top_up_for_buy_only_eur=minimum_top_up_for_exact_target,
        prices_as_of=price_book.as_of,
        holdings_as_of=holdings.fetched_at,
        price_source=price_book.source,
        pending_bitcoin=holdings.pending_bitcoin,
    )
