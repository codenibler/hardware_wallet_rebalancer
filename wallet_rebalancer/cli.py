"""Command-line and Telegram interfaces."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv

from .config import AppConfig, load_config
from .exchange_scanner import ExchangeScanner
from .models import Holdings, PriceBook
from .planner import build_plan
from .providers import ProviderError, PublicDataClient
from .reporting import render_json, render_order_message, render_text
from .telegram import TelegramClient, discover_chats, run_bot
from .tracking import (
    DEFAULT_CHART_DIR,
    DEFAULT_DATA_PATH,
    DEFAULT_START_DATE,
    record_rebalance,
    render_performance,
)


def _non_negative_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a decimal number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative finite number")
    return parsed


def _positive_decimal(value: str) -> Decimal:
    parsed = _non_negative_decimal(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _prompt_top_up() -> Decimal:
    while True:
        print(
            "Enter new EUR top-up amount [0]: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        try:
            raw_value = input()
        except EOFError as exc:
            raise ValueError(
                "No top-up input received; rerun interactively or use --no-prompt"
            ) from exc

        if not raw_value.strip():
            return Decimal("0")

        try:
            return _non_negative_decimal(raw_value.strip())
        except argparse.ArgumentTypeError as exc:
            print(f"Invalid amount: {exc}. Please try again.", file=sys.stderr)


def _parse_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD format") from exc


def _load_holdings_snapshot(path: Path) -> Holdings:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Holdings snapshot must be a JSON object")
    amounts = raw.get("amounts", raw)
    if not isinstance(amounts, dict):
        raise ValueError("Holdings snapshot amounts must be an object")
    fetched_at = (
        _parse_datetime(raw["fetched_at"], "fetched_at")
        if "fetched_at" in raw
        else datetime.now(timezone.utc)
    )
    return Holdings(
        amounts=amounts,
        pending_bitcoin=Decimal(str(raw.get("pending_bitcoin", "0"))),
        fetched_at=fetched_at,
    )


def _load_price_snapshot(path: Path) -> PriceBook:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Price snapshot must be a JSON object")
    prices = raw.get("prices_eur", raw)
    if not isinstance(prices, dict):
        raise ValueError("Price snapshot prices_eur must be an object")
    as_of = (
        _parse_datetime(raw["as_of"], "as_of")
        if "as_of" in raw
        else datetime.now(timezone.utc)
    )
    return PriceBook(
        prices_eur=prices,
        as_of=as_of,
        source=str(raw.get("source", f"Local file {path.name}")),
    )


def _plan_from_args(
    args: argparse.Namespace,
    config: AppConfig,
    *,
    top_up_override: Decimal | None = None,
):
    holdings, prices = _portfolio_inputs(args, config)
    threshold = (
        args.threshold
        if getattr(args, "threshold", None) is not None
        else config.policy.threshold
    )
    fee_bps = (
        args.fee_bps
        if getattr(args, "fee_bps", None) is not None
        else config.policy.estimated_fee_bps
    )
    top_up = (
        top_up_override
        if top_up_override is not None
        else getattr(args, "top_up", Decimal("0"))
    )
    return build_plan(
        holdings,
        prices,
        top_up_eur=top_up,
        threshold=threshold,
        estimated_fee_bps=fee_bps,
        target_weights=config.policy.target_weights,
    )


def _portfolio_inputs(
    args: argparse.Namespace,
    config: AppConfig,
) -> tuple[Holdings, PriceBook]:
    client = PublicDataClient(config)
    holdings = (
        _load_holdings_snapshot(args.holdings_file)
        if getattr(args, "holdings_file", None)
        else client.fetch_holdings()
    )
    prices = (
        _load_price_snapshot(args.prices_file)
        if getattr(args, "prices_file", None)
        else client.fetch_prices()
    )
    return holdings, prices


def _add_policy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--threshold",
        type=_non_negative_decimal,
        help="Override max absolute drift trigger as a decimal",
    )
    parser.add_argument(
        "--fee-bps",
        type=_positive_decimal,
        help="Override estimated fee bps on every euro bought or sold",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hardware-wallet-rebalancer",
        description=(
            "Read public Trezor account balances and produce a non-executing "
            "50% BTC / 25% ETH / 15% SOL / 10% LINK rebalance plan."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Run one portfolio check")
    _add_policy_args(check)
    check.add_argument(
        "--no-prompt",
        action="store_true",
        help="Use a zero top-up without prompting (for automation)",
    )
    check.add_argument(
        "--holdings-file",
        type=Path,
        help="Use a local JSON amount snapshot instead of live balances",
    )
    check.add_argument(
        "--prices-file",
        type=Path,
        help="Use a local JSON price snapshot instead of CoinGecko",
    )
    check.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )
    check.add_argument(
        "--no-telegram",
        action="store_true",
        help="Do not send the report to the configured Telegram chat",
    )

    discover = subparsers.add_parser(
        "discover-telegram",
        help="List chat IDs from messages already sent to the bot",
    )
    discover.add_argument(
        "--token-env",
        default="TELEGRAM_BOT_TOKEN",
        help="Environment variable containing the bot token",
    )

    bot = subparsers.add_parser(
        "bot",
        help="Run allowlisted Telegram /check long polling",
    )
    _add_policy_args(bot)

    track = subparsers.add_parser(
        "track",
        help="Record a completed rebalance and update performance charts",
    )
    track.add_argument(
        "--holdings-file",
        type=Path,
        help="Use a local JSON amount snapshot instead of live balances",
    )
    track.add_argument(
        "--prices-file",
        type=Path,
        help="Use a local JSON price snapshot instead of CoinGecko",
    )
    track.add_argument(
        "--start-date",
        type=_parse_date,
        default=DEFAULT_START_DATE,
        help=(
            "Buy-and-hold benchmark start in YYYY-MM-DD format "
            f"(default: {DEFAULT_START_DATE})"
        ),
    )
    track.add_argument(
        "--data-file",
        type=Path,
        default=DEFAULT_DATA_PATH,
        help=f"Persistent tracking JSON (default: {DEFAULT_DATA_PATH})",
    )
    track.add_argument(
        "--charts-dir",
        type=Path,
        default=DEFAULT_CHART_DIR,
        help=f"Chart and CSV output directory (default: {DEFAULT_CHART_DIR})",
    )
    track.add_argument(
        "--note",
        default="",
        help="Optional one-line description of this completed rebalance",
    )
    track.add_argument(
        "--json",
        action="store_true",
        help="Emit the latest performance summary as JSON",
    )

    return parser


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not configured")
    return value


def _render_orders_with_venues(plan) -> str:
    if not getattr(plan, "trades", ()):
        return render_order_message(plan)
    try:
        venues = ExchangeScanner().fetch_markets()
    except (RuntimeError, ValueError) as exc:
        return render_order_message(plan, venue_error=str(exc))
    return render_order_message(plan, venues=venues)


def _check_command(args: argparse.Namespace) -> int:
    config = load_config()
    args.top_up = Decimal("0") if args.no_prompt else _prompt_top_up()
    plan = _plan_from_args(args, config)
    text_report = render_text(plan)
    print(
        render_json(plan) if args.json else text_report,
        end="" if args.json else "\n",
    )

    if not args.no_telegram:
        token = _require_env("TELEGRAM_BOT_TOKEN")
        chat_id = _require_env("TELEGRAM_CHAT_ID")
        TelegramClient(token).send_message(
            chat_id,
            _render_orders_with_venues(plan),
        )
    return 0


def _discover_command(args: argparse.Namespace) -> int:
    client = TelegramClient(_require_env(args.token_env))
    chats = discover_chats(client)
    if not chats:
        print(
            "No pending chats found. Open the bot in Telegram, send /start, "
            "then run this command again."
        )
        return 1
    for chat in chats:
        print(
            f"chat_id={chat['chat_id']} type={chat['type']} "
            f"label={chat['label']}"
        )
    return 0


def _bot_command(args: argparse.Namespace) -> int:
    config = load_config()
    token = _require_env("TELEGRAM_BOT_TOKEN")
    allowed_raw = _require_env("TELEGRAM_ALLOWED_CHAT_IDS")
    try:
        allowed = {
            int(value.strip())
            for value in allowed_raw.split(",")
            if value.strip()
        }
    except ValueError as exc:
        raise ValueError(
            "TELEGRAM_ALLOWED_CHAT_IDS must contain numeric IDs"
        ) from exc

    def check_callback(top_up: Decimal) -> str:
        return _render_orders_with_venues(
            _plan_from_args(args, config, top_up_override=top_up)
        )

    print("Telegram bot is running. Press Ctrl-C to stop.")
    try:
        run_bot(
            TelegramClient(token),
            allowed_chat_ids=allowed,
            check_callback=check_callback,
        )
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def _track_command(args: argparse.Namespace) -> int:
    config = load_config()
    holdings, prices = _portfolio_inputs(args, config)
    summary = record_rebalance(
        holdings,
        prices,
        data_path=args.data_file,
        chart_dir=args.charts_dir,
        start_date=args.start_date,
        note=args.note,
    )
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2))
    else:
        print(render_performance(summary))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            return _check_command(args)
        if args.command == "discover-telegram":
            return _discover_command(args)
        if args.command == "bot":
            return _bot_command(args)
        if args.command == "track":
            return _track_command(args)
        raise AssertionError("unreachable")
    except (
        FileNotFoundError,
        ValueError,
        ProviderError,
        RuntimeError,
        json.JSONDecodeError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
