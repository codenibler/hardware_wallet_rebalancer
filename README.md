# Hardware wallet rebalancer

A read-only BTC, ETH, SOL, and LINK portfolio monitor. It compares the current
allocation with these targets:

- BTC: 50%
- ETH: 25%
- SOL: 15%
- LINK: 10%

It recommends trades and venues but never signs or submits transactions.

## Project layout

- `main.py`: fetch balances, calculate a fee-aware rebalance, rank venues, and
  send the result through Telegram.
- `tracking.py`: record portfolio performance against the fixed buy-and-hold
  benchmark and refresh the charts.
- `wallet_rebalancer/`: application code.
- `deploy/systemd/`: optional bot, report, and tracking service templates.
- `examples/`: offline balance and price snapshots.
- `tests/`: unit tests.
- `reports/`: private generated analytics; ignored by Git.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Fill in `.env`. Multiple wallet identifiers are comma-separated:

```dotenv
HWR_BITCOIN_XPUBS=xpub_or_zpub_here
HWR_ETHEREUM_ADDRESSES=0x_address_here
HWR_SOLANA_ADDRESSES=solana_address_here
HWR_SOLANA_STAKE_ACCOUNTS=
TELEGRAM_BOT_TOKEN=botfather_token
TELEGRAM_CHAT_ID=numeric_chat_id
```

Export only public account identifiers from Trezor Suite:

- Bitcoin: open each funded account, select **Details**, then **Show public
  key**. Include every used account and account type.
- Ethereum and LINK: use the verified Ethereum receive address. LINK is read
  from the official Ethereum-mainnet contract.
- Solana: use each verified receive address. List delegated stake-account
  addresses separately in `HWR_SOLANA_STAKE_ACCOUNTS`.

Never enter a recovery seed, Shamir share, PIN, passphrase, private key, or
signing authorization. An XPUB cannot spend funds, but it reveals the complete
account history, so keep `.env` private.

## Run a portfolio check

```bash
python main.py
```

The program asks for a new EUR deposit:

```text
Enter new EUR top-up amount [0]:
```

Press Enter for no deposit. For unattended checks, use:

```bash
python main.py --no-prompt
```

A successful check sends the report to Telegram by default. Use
`--no-telegram` only for local troubleshooting. Other useful options are:

```bash
python main.py --fee-bps 75
python main.py --json
```

The planner adds the stated deposit, estimates fees on gross buys and sells,
and solves the target allocation using the remaining post-fee value. It only
plans orders; it does not execute them.

When orders are needed, it ranks the top three available quotes for each coin
from Bitvavo, Kraken Pro, Coinbase Advanced, OKX Europe, Banxa, Invity,
Mercuryo, Anycoin Direct, BTC Direct, and MoonPay. Configure account-specific
taker fees and optional payment-method filters in `.env`.

Quotes can omit funding, withdrawal, network, spread, tax, minimum-size, and
rounding costs. Always verify the provider's final preview before trading.

## Performance tracking

After the initial portfolio snapshot or a completed rebalance, run:

```bash
python tracking.py --note "Completed rebalance"
```

The first run freezes the supplied coin quantities and allocation as the
July 28, 2026 buy-and-hold benchmark. Later runs value:

1. the latest real wallet balances; and
2. the benchmark quantities that have been bought and held without
   rebalancing;

using the same price snapshot. Results are written to:

- `reports/portfolio_tracking.json`
- `reports/portfolio_performance.csv`
- `reports/portfolio_value.svg`
- `reports/portfolio_returns.svg`

Entering a top-up in `main.py` creates a plan; it does not prove that the
deposit and trades were completed. After the purchased coins are visible in
the fetched wallet balances, record the completed deposit explicitly:

```bash
python tracking.py \
  --deposit-eur 1000 \
  --deposit-fee-bps 50 \
  --note "Completed €1,000 deposit"
```

If `--deposit-fee-bps` is omitted, the benchmark uses
`HWR_ESTIMATED_FEE_BPS`. The tracker records the gross cash flow, subtracts the
simulated purchase fee, buys additional benchmark units using the original
allocation, and never rebalances those units. Both strategies use chained
time-weighted returns, so deposits cannot be counted as investment
performance. The tracker refuses to record a deposit until the real wallet
value has increased at the current price snapshot.

The installed user timer runs this tracker daily at 20:00
Europe/Amsterdam:

```bash
systemctl --user status hwr-tracking.timer
journalctl --user -u hwr-tracking.service
```

If tracking fails, `hwr-tracking-notify.service` sends a Telegram warning to
investigate the journal. A missed scheduled run is performed when the computer
and user service manager become available again.

The reusable units are in `deploy/systemd/`. After changing an installed unit:

```bash
systemctl --user daemon-reload
systemctl --user restart hwr-tracking.timer
```

## Telegram commands

To discover the chat ID after sending `/start` to the bot:

```bash
python -m wallet_rebalancer discover-telegram
```

The normal `main.py` workflow is outbound-only. Optional long-running bot mode
supports allowlisted `/check` and `/check 1000` commands:

```bash
python -m wallet_rebalancer bot
```

Set `TELEGRAM_ALLOWED_CHAT_IDS` before using bot mode. If a token is exposed,
revoke it through BotFather immediately.

## Offline validation

The offline demo makes no wallet, price-provider, exchange, or Telegram calls:

```bash
python main.py \
  --holdings-file examples/demo_holdings.json \
  --prices-file examples/demo_prices.json \
  --no-telegram
```

Run all tests with:

```bash
python -m unittest discover -v
```

## Scope

- Only configured accounts are visible.
- ETH in staking, lending, bridging, or other contracts is not counted as
  liquid native ETH.
- SOL stake accounts are counted only when explicitly configured.
- Assets on exchanges, other networks, or unlisted passphrase wallets are not
  visible.
- Public providers can correlate wallet identifiers with the requesting IP.
- Reports and logs reveal portfolio values and should be treated as private.
