# Hardware wallet rebalancer

A portfolio monitor which tracks holdings against desired split of 
- BTC: 50%
- ETH: 25%
- SOL: 15%
- LINK: 10%
and recommends rebalances when %s deviate more than 5 percent. Notifies of adjustments through a Telegram bot, and searches for the best deals from available brokers and fiat on-ramp platforms. 

Tracks performance of this rebalancing strategy vs. a buy & hold, accounting for new deposits and increasing both by the net increment in portfolio value. 

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

Fill in env vars with XPUBS from Trezor Suite / other hardware wallets. For these holdings in particular, 
- Bitcoin: open each funded account, select **Details**, then **Show public
  key**. Include every used account and account type.
- Ethereum and LINK: use the verified Ethereum receive address. LINK is read
  from the official Ethereum-mainnet contract.
- Solana: use each verified receive address. List delegated stake-account
  addresses separately in `HWR_SOLANA_STAKE_ACCOUNTS`.

No pins, recovery seeds, or private keys are stored, so only your account history can be seen if you leak these variables. Still, try to avoid this. 

## Usage

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
`--no-telegram` only for local troubleshooting. You can also set fixed fees and JSON exports with. For Bitvavo, Kraken, Coinbase, and OKX, there are .env vars with the default fees. For Banxa, Invity, Mercuryo, Anycoin Direct, BTC Direct, and MoonPay, they are fetched and calculated dynamically.

```bash
python main.py --fee-bps 75
python main.py --json
```

The planner adds the stated deposit, estimates fees on gross buys and sells,
and solves the target allocation using the remaining post-fee value. It only
plans orders; it does not execute them.

When orders are needed, it ranks the top three available quotes for each coin from the previously stated providers. You can configure constraints on payment methods in the .env. 

Quotes can omit funding, withdrawal, network, spread, tax, minimum-size, and rounding costs. Always verify the provider's final preview before trading.


## Performance tracking
To have daily granularity in benchmarking this rebalancing strategy vs the buy & hold, the balances are checked daily at 20:00 Europe/Amsterdam:

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

## Testing

Run all tests with:

```bash
python -m unittest discover -v
```


