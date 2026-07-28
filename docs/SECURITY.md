# Security model

## What this program can do

- Query public blockchain/indexer/RPC endpoints.
- Query public EUR prices.
- Calculate an indicative target allocation.
- Print or send the resulting report to an allowlisted Telegram chat.

## What this program cannot do

- Access a recovery seed or private key.
- Unlock the Trezor.
- Sign, approve, or broadcast a transaction.
- Move funds automatically.

There is intentionally no Trezor Connect signing dependency and no exchange
API integration.

## Sensitive but public data

An XPUB cannot spend Bitcoin, but it reveals every derived address, balance,
and transaction associated with that account. Ethereum and Solana addresses
also expose their on-chain activity. Risks include:

- loss of financial privacy;
- provider correlation of an IP address with a wallet;
- Telegram reports disclosing portfolio values;
- accidental publication through Git.

Controls:

- `.env` is ignored by Git;
- provider failures omit account identifiers;
- Telegram bot mode requires numeric chat allowlisting;
- no report contains wallet addresses or XPUBs;
- HTTPS is required for non-local provider URLs;
- stale or missing prices stop planning rather than silently guessing.

For stronger privacy, use a private Blockbook/RPC provider, route traffic
appropriately, and restrict file permissions:

```bash
chmod 600 .env
```

## Telegram token

A Bot API token grants control of the bot. If exposed, revoke it immediately
in BotFather and generate a replacement. Never place it directly in a command
line because process listings and shell history may record it.

The project reads `TELEGRAM_BOT_TOKEN` from `.env` or the process environment.

## Financial accuracy

The output is a plan, not an executable quote. It intentionally fails if a
required balance or price cannot be read. Even a successful run cannot know:

- the future execution price;
- bid/ask spread and market impact;
- exact exchange, withdrawal, bridge, gas, or miner fees;
- tax basis and tax consequences;
- venue minimum size and precision;
- whether a configured account list is complete.

Always reconcile balances with Trezor Suite and re-run immediately before
trading. Verify every address and transaction on the Trezor display.

## Public repository checklist

Before every commit:

```bash
git status --short
git grep -nE '(seed|mnemonic|private[_ -]?key|TELEGRAM_BOT_TOKEN=.+)'
```

Inspect staged changes with `git diff --cached`. Secret scanning is a useful
backstop, not permission to commit sensitive data.
