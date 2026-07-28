# Hardware wallet rebalancer

A read-only Trezor portfolio monitor and transaction planner for:

- 50% BTC
- 25% ETH
- 15% SOL
- 10% LINK

The program reads public blockchain balances, obtains current EUR prices, and
checks whether any asset is outside a configurable allocation band. It never
asks the Trezor to sign, never broadcasts transactions, and must never receive
a recovery seed, PIN, passphrase, or private key.

> Coins are recorded on their blockchains, not inside the Trezor. The exported
> public account identifiers let this program monitor those blockchain balances
> while the device remains disconnected.

## Features

- Bitcoin account-wide lookup using one or more XPUBs.
- Native ETH and Ethereum-mainnet LINK lookup for multiple Ethereum accounts.
- Native SOL lookup for multiple primary and explicitly listed stake accounts.
- 5-percentage-point maximum drift threshold by default.
- Fee-aware indicative buy/sell calculations.
- Interactive top-up input for new EUR capital.
- Human-readable and JSON output.
- Default ordered Telegram delivery and an allowlisted `/check [top_up]` bot.
- Deterministic offline demo and unit tests.

No automatic trade execution is included by design.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` using [the export instructions](docs/SETUP.md), then run:

```bash
python run_rebalancer.py
python run_rebalancer.py --fee-bps 10
python run_rebalancer.py --json
```

Each check asks:

```text
Enter new EUR top-up amount [0]:
```

Enter the new amount, or press Enter to use zero. For unattended runs, pass
`--no-prompt`; this explicitly uses a zero top-up.

Every successful check sends an ordered proposed-trade list to
`TELEGRAM_CHAT_ID` by default. Red SELL orders appear first, followed by green
BUY orders. Use `--no-telegram` only for offline demos or local
troubleshooting.

The top-up is uninvested EUR cash that is not already included in the fetched
wallet balances. The planner:

1. values the current holdings;
2. adds the top-up;
3. estimates costs on gross buys and sells;
4. solves the post-fee target portfolio;
5. reports asset units and EUR notionals at the same price snapshot.

If no threshold is breached and no top-up is supplied, it states that no
rebalance is needed. If the allocation is within its band but a top-up is
supplied, it keeps the “no threshold rebalance” result and provides a separate
top-up deployment plan.

## Offline demo

The demo makes no provider or wallet calls:

```bash
python run_rebalancer.py \
  --holdings-file examples/demo_holdings.json \
  --prices-file examples/demo_prices.json \
  --no-telegram
```

Enter `1000` when prompted to reproduce the top-up example.

## Telegram

The bot token and chat IDs belong in an ignored `.env` file, never in source:

```bash
cp .env.example .env
```

After messaging the bot with `/start`, discover the private chat ID:

```bash
python -m wallet_rebalancer discover-telegram
```

For one-shot delivery, run the normal check:

```bash
python run_rebalancer.py
```

Enter the top-up when prompted; the report is sent automatically.

For an allowlisted long-running bot:

```bash
python -m wallet_rebalancer bot
```

Then send `/check` or `/check 1000`. Bot mode refuses to start without
`TELEGRAM_ALLOWED_CHAT_IDS`.

If a bot token has ever appeared in chat, source control, logs, or screenshots,
revoke it in BotFather and generate a new one before use.

## Important scope limits

- LINK means the official ERC-20 LINK contract on Ethereum mainnet.
- ETH deposited into staking, lending, bridging, or other smart contracts is
  not the same as liquid native ETH at the configured address and is not
  automatically discovered.
- SOL in separately created stake accounts is counted only when those account
  addresses are listed in `HWR_SOLANA_STAKE_ACCOUNTS`.
- Assets held on exchanges, other networks, passphrase wallets, or unlisted
  accounts are not visible.
- Public providers learn the queried addresses or XPUB. Self-hosted/private
  providers improve privacy and reliability.
- “Precise” quantities are precise only for the fetched balances, one market
  snapshot, and the configured cost estimate. Market movement, spread, gas,
  withdrawal fees, minimum order sizes, taxes, and rounding change execution.

Read [SECURITY.md](docs/SECURITY.md) before entering real public identifiers.

## Validation

```bash
python -m unittest discover -v
```

## Data interfaces

The implementation follows the current official documentation for:

- [Trezor XPUB export and privacy](https://trezor.io/learn/supported-assets/bitcoin/what-is-a-public-key-xpub)
- [Trezor Ethereum and ERC-20 accounts](https://trezor.io/learn/supported-assets/ethereum-layer-2-EVM/ethereum-erc-20-tokens-on-trezor)
- [Trezor Solana receive addresses](https://trezor.io/learn/supported-assets/solana/managing-solana-tokens-in-trezor-suite)
- [Trezor Blockbook API V2](https://github.com/trezor/blockbook/blob/master/docs/api.md)
- [Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/)
- [Solana `getBalance`](https://solana.com/docs/rpc/http/getbalance)
- [CoinGecko keyless public API](https://docs.coingecko.com/docs/keyless-public-api)
- [Telegram Bot API](https://core.telegram.org/bots/api)
