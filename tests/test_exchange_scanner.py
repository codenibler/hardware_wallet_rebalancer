from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

import requests

from wallet_rebalancer.exchange_scanner import (
    ExchangeScanError,
    ExchangeScanner,
    INVITY_PROVIDERS,
)
from wallet_rebalancer.models import ASSETS, TradeInstruction


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError("exchange request failed")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses) -> None:
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append(("GET", args, kwargs))
        return self.responses.pop(0)

    def post(self, *args, **kwargs):
        self.calls.append(("POST", args, kwargs))
        return self.responses.pop(0)


def _bitvavo_payload():
    return [
        {
            "market": f"{asset}-EUR",
            "bid": "99.9",
            "ask": "100",
            "askSize": "100",
            "bidSize": "100",
        }
        for asset in ASSETS
    ]


def _kraken_payload():
    pair_names = {
        "BTC": "XXBTZEUR",
        "ETH": "XETHZEUR",
        "SOL": "SOLEUR",
        "LINK": "LINKEUR",
    }
    return {
        "error": [],
        "result": {
            pair_names[asset]: {
                "a": ["99.8", "1", "100"],
                "b": ["99.7", "1", "100"],
            }
            for asset in ASSETS
        },
    }


def _coinbase_payload():
    return {
        "asks": [["100.1", "100", 1]],
        "bids": [["100.0", "100", 1]],
    }


def _okx_payload():
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        "code": "0",
        "msg": "",
        "data": [
            {
                "instId": f"{asset}-EUR",
                "askPx": "99.9",
                "askSz": "100",
                "bidPx": "99.8",
                "bidSz": "100",
                "ts": str(timestamp_ms),
            }
            for asset in ASSETS
        ],
    }


def _invity_offer(
    provider: str,
    *,
    side: str,
    fiat_amount: str,
    payment_method: str = "sepa",
    payment_method_name: str = "SEPA",
):
    crypto_field = (
        "receiveStringAmount" if side == "BUY" else "cryptoStringAmount"
    )
    return {
        "exchange": (
            f"{provider}-sell"
            if side == "SELL" and provider == "btcdirect"
            else provider
        ),
        "fiatCurrency": "EUR",
        "fiatStringAmount": fiat_amount,
        crypto_field: "1",
        "rate": fiat_amount,
        "minCrypto": "0.1",
        "maxCrypto": "10",
        "paymentMethod": payment_method,
        "paymentMethodName": payment_method_name,
    }


class ExchangeScannerTests(unittest.TestCase):
    def test_ranks_buys_and_sells_by_fee_adjusted_execution(self) -> None:
        session = FakeSession(
            [
                FakeResponse(_bitvavo_payload()),
                FakeResponse(_kraken_payload()),
                *[FakeResponse(_coinbase_payload()) for _ in ASSETS],
                FakeResponse(_okx_payload()),
            ]
        )

        markets = ExchangeScanner(session=session).fetch_markets()

        self.assertEqual(len(session.calls), 7)
        for asset in ASSETS:
            buy_ranking = markets.rank(
                asset=asset,
                side="BUY",
                amount=Decimal("1"),
            )
            self.assertEqual(
                [quote.exchange_id for quote in buy_ranking],
                ["okx", "bitvavo", "kraken"],
            )
            self.assertEqual(
                buy_ranking[0].effective_unit_price_eur("BUY"),
                Decimal("100.24965"),
            )
            self.assertTrue(buy_ranking[0].covers("BUY", Decimal("1")))

            sell_ranking = markets.rank(
                asset=asset,
                side="SELL",
                amount=Decimal("1"),
            )
            self.assertEqual(
                [quote.exchange_id for quote in sell_ranking],
                ["bitvavo", "okx", "coinbase"],
            )
            self.assertEqual(
                sell_ranking[0].effective_unit_price_eur("SELL"),
                Decimal("99.65025"),
            )

    def test_one_failed_exchange_does_not_hide_other_results(self) -> None:
        session = FakeSession(
            [
                FakeResponse({}, status_code=500),
                FakeResponse(_kraken_payload()),
            ]
        )

        markets = ExchangeScanner(
            session=session,
            exchanges=("bitvavo", "kraken"),
        ).fetch_markets()

        self.assertTrue(markets.failures)
        ranking = markets.rank(
            asset="BTC",
            side="BUY",
            amount=Decimal("1"),
        )
        self.assertEqual(ranking[0].exchange_id, "kraken")
        self.assertIn("Bitvavo market-data request failed", markets.failures[0])

    def test_all_failed_exchanges_raise_sanitized_error(self) -> None:
        session = FakeSession([FakeResponse({}, status_code=500)])

        with self.assertRaisesRegex(ExchangeScanError, "No supported venue"):
            ExchangeScanner(
                session=session,
                exchanges=("bitvavo",),
            ).fetch_markets()

    def test_account_fee_overrides_change_effective_price(self) -> None:
        session = FakeSession([FakeResponse(_bitvavo_payload())])
        markets = ExchangeScanner(
            session=session,
            exchanges=("bitvavo",),
            taker_fee_bps={"bitvavo": "10"},
        ).fetch_markets()

        quote = markets.rank(
            asset="BTC",
            side="BUY",
            amount=Decimal("1"),
        )[0]
        self.assertEqual(quote.taker_fee_bps, Decimal("10"))
        self.assertEqual(
            quote.effective_unit_price_eur("BUY"),
            Decimal("100.100"),
        )

    def test_trade_amount_must_be_positive(self) -> None:
        markets = ExchangeScanner(
            session=FakeSession([FakeResponse(_bitvavo_payload())]),
            exchanges=("bitvavo",),
        ).fetch_markets()

        with self.assertRaisesRegex(ValueError, "positive"):
            markets.rank(asset="BTC", side="BUY", amount=Decimal("0"))

    def test_invity_adds_all_requested_broker_providers(self) -> None:
        buy_offers = [
            _invity_offer(
                provider,
                side="BUY",
                fiat_amount=str(100 + index),
            )
            for index, provider in enumerate(INVITY_PROVIDERS)
        ]
        buy_offers.append(
            _invity_offer(
                "banxa",
                side="BUY",
                fiat_amount="99",
                payment_method="bankTransfer",
                payment_method_name="Bank Transfer",
            )
        )
        sell_offers = [
            _invity_offer(
                provider,
                side="SELL",
                fiat_amount=str(100 - index),
            )
            for index, provider in enumerate(INVITY_PROVIDERS)
        ]
        session = FakeSession(
            [FakeResponse(buy_offers), FakeResponse(sell_offers)]
        )
        trades = (
            TradeInstruction(
                asset="BTC",
                side="BUY",
                amount=Decimal("1"),
                notional_eur=Decimal("100"),
                snapshot_price_eur=Decimal("100"),
            ),
            TradeInstruction(
                asset="ETH",
                side="SELL",
                amount=Decimal("1"),
                notional_eur=Decimal("100"),
                snapshot_price_eur=Decimal("100"),
            ),
        )

        markets = ExchangeScanner(
            session=session,
            exchanges=INVITY_PROVIDERS,
            invity_account_descriptor="xpub-test",
        ).fetch_markets(trades=trades)

        self.assertEqual([call[0] for call in session.calls], ["POST", "POST"])
        self.assertEqual(
            {quote.exchange_id for quote in markets.quotes["BTC"]},
            set(INVITY_PROVIDERS),
        )
        buy_ranking = markets.rank(
            asset="BTC",
            side="BUY",
            amount=Decimal("1"),
            limit=6,
        )
        self.assertEqual(buy_ranking[0].exchange_id, "banxa")
        self.assertEqual(
            buy_ranking[0].exchange_name,
            "Banxa (Bank Transfer)",
        )
        self.assertTrue(buy_ranking[0].fee_included_in_quote)
        self.assertEqual(buy_ranking[0].supported_sides, frozenset(("BUY",)))

        sell_ranking = markets.rank(
            asset="ETH",
            side="SELL",
            amount=Decimal("1"),
            limit=6,
        )
        self.assertEqual(sell_ranking[0].exchange_id, "banxa")
        self.assertEqual(
            {quote.exchange_id for quote in sell_ranking},
            set(INVITY_PROVIDERS),
        )

    def test_invity_payment_method_filter_is_applied(self) -> None:
        offers = [
            _invity_offer(
                "moonpay",
                side="BUY",
                fiat_amount="90",
                payment_method="creditCard",
                payment_method_name="Credit Card",
            ),
            _invity_offer(
                "moonpay",
                side="BUY",
                fiat_amount="100",
                payment_method="sepa",
                payment_method_name="SEPA",
            ),
        ]
        session = FakeSession([FakeResponse(offers)])
        trade = TradeInstruction(
            asset="BTC",
            side="BUY",
            amount=Decimal("1"),
            notional_eur=Decimal("100"),
            snapshot_price_eur=Decimal("100"),
        )

        markets = ExchangeScanner(
            session=session,
            exchanges=("moonpay",),
            invity_account_descriptor="xpub-test",
            payment_methods=("sepa",),
        ).fetch_markets(trades=(trade,))

        quote = markets.rank(
            asset="BTC",
            side="BUY",
            amount=Decimal("1"),
        )[0]
        self.assertEqual(quote.exchange_name, "MoonPay (SEPA)")
        request_body = session.calls[0][2]["json"]
        self.assertEqual(request_body["cryptoStringAmount"], "1")
        self.assertTrue(request_body["wantCrypto"])
        self.assertNotIn("xpub-test", str(session.calls))


if __name__ == "__main__":
    unittest.main()
