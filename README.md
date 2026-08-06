# Hardware wallet rebalancer

A portfolio monitor which tracks holdings against desired split of 
- BTC: 50%
- ETH: 25%
- SOL: 15%
- LINK: 10%
and recommends rebalances when allocation deviates more than 5%. It notifies
you of adjustments through a Telegram bot and can execute a manually deposited
top-up through Bitvavo.

Tracks performance of this rebalancing strategy vs. a buy & hold. Every
portfolio check compares the balances from the configured XPUBs and wallet
addresses with the preceding snapshot. When all tracked asset balances are
unchanged or higher and at least one increased, their value is treated as a
contribution and buy-and-hold invests it in its fixed starting allocation.

Run checks and performance tracking whenever you choose. By default, there is
a scheduled non-interactive portfolio check at 20:00 CET every day. To preview
or execute a manually deposited Bitvavo top-up, run `python main.py` and answer
the two prompts.

*Note: I personally do not currently rebalance when the portfolio deviates >5% from intended holdings. In order to avoid excess transaction costs, I make a weekly deposit into my investment.*

*This weekly deposit is allocated in a way which rebalances the portfolio by buying more or less of each cryptocurrency. This way, you do not pay fees on depositing AND rebalancing, and have the same fees as a buy & hold portfolio, whilst conserving the diversification gains from periodic rebalancing.* 

*Eventually, with a large portfolio, this is impossible, but if you plan to make recurring deposits into your investment, at least a little bit of the rebalancing fees can be covered by depositing intelligently.*

## Project layout
- `main.py`: interactively preview or execute a deposited Bitvavo top-up;
  command-line options continue to support automated portfolio checks.
- `tracking.py`: record portfolio performance, detect unambiguous incoming
  assets, and refresh the charts.
- `wallet_rebalancer/`: application code.
- `deploy/systemd/`: optional manually started bot and report service
  templates.
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

The program first asks for the mode, then the EUR amount already deposited in
Bitvavo:

```text
Run mode — Demo or Live? [Demo]: Demo
Enter EUR deposit amount: 250
```

Press Enter at the first prompt to choose Demo safely. Demo uses current wallet
and Bitvavo account data to show the exact proposed purchases and destinations,
but the trading credentials are never loaded and nothing is submitted. Typing
`Live` executes the displayed buy-only plan and withdrawals after all safety
checks pass.

For a non-interactive, zero-deposit portfolio check, use:

```bash
python main.py --no-prompt
```

A successful check sends the report to Telegram by default. Use
`--no-telegram` only for local troubleshooting. You can also override the
estimated fee rate or export JSON:

```bash
python main.py --fee-bps 75
python main.py --json
```

The scheduled `check` workflow estimates a portfolio rebalance and records its
snapshot, but never executes orders. Bare `python main.py` launches the
interactive Bitvavo workflow below.

## Execute a deposited top-up through Bitvavo

This workflow assumes the EUR has already been deposited manually and is
available in Bitvavo. It reads the current hardware-wallet holdings, allocates
only the supplied EUR amount among underweight assets, buys them on Bitvavo,
and withdraws only the newly purchased quantities. It never submits sells.

One-time Bitvavo setup:

1. Enable crypto withdrawals and add a fixed, personally owned destination
   address for BTC, ETH, SOL, and LINK to the Bitvavo address book. Verify each
   destination and network. A fixed BTC receive address is required for full
   automation; address reuse has a privacy tradeoff.
2. Create an IP-allowlisted read-only API key with view permission. It handles
   account reads and dry-run previews through a client that cannot place orders
   or withdrawals.
3. Create a second IP-allowlisted execution key with view, trade, and
   withdrawal permissions. It is loaded only after `--confirm`; view permission
   is required to reconcile submitted orders. Bitvavo API withdrawals bypass
   2FA and email confirmation, so do not use an unrestricted key.
4. Add the `BITVAVO_*` and `HWR_BITVAVO_*` values shown in `.env.example` to
   `.env`. Never paste the secret into a command, chat, log, or committed file.

The normal interface is:

```bash
python main.py
```

Choose Demo for a read-only preview or Live for actual execution, then enter
the deposited EUR amount. The direct commands remain available for advanced
automation:

```bash
python -m wallet_rebalancer bitvavo-top-up 250
python -m wallet_rebalancer bitvavo-top-up 250 --confirm
```

Before any order, the command checks the available EUR balance, account fee
tier, live market status, minimum order and withdrawal amounts, withdrawal
status, configured network, and the difference between Bitvavo's ask and the
planning price. The default maximum input is €1,000 and the default price
deviation limit is 200 basis points; both are configurable in `.env`.

Execution progress is written atomically to
`reports/bitvavo_executions.json`. Market orders and withdrawals have stable
idempotency identifiers. If a request fails after execution may have started,
the run is marked `manual_review` and future runs are blocked. Reconcile the
orders and withdrawals in Bitvavo first, then unblock execution with:

```bash
python -m wallet_rebalancer bitvavo-acknowledge RUN_UUID
```

The next normal portfolio check will see the incoming wallet units and record
the contribution in performance tracking.


## Performance tracking

Every portfolio check, including scheduled `main.py --no-prompt` runs, records
a performance snapshot. It values
unambiguous net incoming units at the snapshot price and adds that value to
buy-and-hold's fixed starting allocation, so a completed purchase that has
reached a configured wallet needs no manually entered EUR amount.

`tracking.py` can also record a performance-only snapshot:

```bash
python tracking.py --note "Manual snapshot"
```

Run `python main.py --no-prompt` separately whenever you want a non-interactive
rebalancing report. The templates in `deploy/systemd/` are optional manually
started services; they do not schedule checks or tracking.

For safety, an increase is not classified as a deposit if any tracked asset
decreased since the prior snapshot. That pattern can be a rebalance, sale, or
withdrawal and remains unclassified rather than corrupting the comparison. Use
`tracking.py --deposit-eur ... --deposit-fee-bps ...` for an explicit
correction in that case.

## Telegram commands

To discover the chat ID after sending `/start` to the bot:

```bash
python -m wallet_rebalancer discover-telegram
```

The scheduled `main.py --no-prompt` workflow is outbound-only. Optional
long-running bot mode supports allowlisted `/check` and `/check 1000` commands:

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
