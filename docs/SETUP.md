# Setup and wallet export guide

## 1. Security boundary

This project needs public account identifiers only.

Never enter or export any of these:

- wallet backup or recovery seed words;
- Shamir backup shares;
- Trezor PIN;
- passphrase;
- private key;
- signing authorization.

Trezor states that an XPUB can monitor balances without spending, but also
warns that it exposes the account's complete history and balance. Treat
`.env` as private even though the wallet identifiers contain no spending key.

## 2. Install

```bash
git clone https://github.com/codenibler/hardware_wallet_rebalancer.git
cd hardware_wallet_rebalancer
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

`.env` is ignored by Git.

## 3. Export the read-only identifiers

Use the official Trezor Suite desktop application, update it first, and verify
every displayed public identifier on the Trezor device.

Wallet identifiers may be placed in the ignored `.env` file. Multiple
identifiers use comma-separated values:

```dotenv
HWR_BITCOIN_XPUBS=xpub_or_zpub_here
HWR_ETHEREUM_ADDRESSES=0x_address_here
HWR_SOLANA_ADDRESSES=solana_address_here
HWR_SOLANA_STAKE_ACCOUNTS=
```

### Bitcoin: export every used account XPUB

For each Bitcoin account:

1. Connect the Trezor and open Trezor Suite.
2. Select the Bitcoin account in the sidebar.
3. Open **Details**.
4. Select **Show public key**.
5. Compare the value shown in Suite with the device display.
6. Copy it into `HWR_BITCOIN_XPUBS`.

Repeat this for every funded account, every account type you use (SegWit,
Taproot, Legacy SegWit, or Legacy), and every relevant standard or passphrase
wallet. One missing XPUB means an incomplete BTC total.

Official reference:
[What is a public key (XPUB)?](https://trezor.io/learn/supported-assets/bitcoin/what-is-a-public-key-xpub)

### Ethereum and LINK: export every Ethereum receive address

For each Ethereum account:

1. Select Ethereum in Trezor Suite.
2. Open **Receive** and select **Show full address**.
3. Verify the complete address on the Trezor display.
4. Copy it into `HWR_ETHEREUM_ADDRESSES`.

LINK is an ERC-20 token and shares the Ethereum account address. The program
uses the official Ethereum-mainnet LINK contract configured in
`HWR_LINK_CONTRACT`.

Official reference:
[Ethereum and ERC-20 tokens on Trezor](https://trezor.io/learn/supported-assets/ethereum-layer-2-EVM/ethereum-erc-20-tokens-on-trezor)

### Solana: export every primary address

For each Solana account:

1. Select Solana in Trezor Suite.
2. Open **Receive** and select **Show full address**.
3. Verify the address on the Trezor display.
4. Copy it into `HWR_SOLANA_ADDRESSES`.

If SOL is delegated, its balance lives in a separate Solana stake account.
Add every such public stake-account address to
`HWR_SOLANA_STAKE_ACCOUNTS`. Verify the total against the Trezor Suite
staking dashboard before relying on the monitor. Do not put an associated SPL
token account in the primary-address list.

Current Trezor documentation lists SOL support for Model T, Safe 3, Safe 5, and
Safe 7, but not Model One:
[Solana balance troubleshooting](https://trezor.io/support/troubleshooting/coins-tokens/why-your-sol-isn-t-showing-up-in-trezor-suite).

## 4. Configure policy and providers

Edit `.env`:

```dotenv
HWR_THRESHOLD=0.05
HWR_ESTIMATED_FEE_BPS=10
```

The threshold is an absolute portfolio-weight gap. At 5%, BTC triggers below
45% or above 55%; LINK triggers below 5% or above 15%.

The public defaults are convenient, not guaranteed services. For durable
operation, replace the `.env` endpoints with providers you operate or have a
service agreement with:

```dotenv
HWR_BITCOIN_BLOCKBOOK_URL=https://...
HWR_ETHEREUM_RPC_URL=https://...
HWR_SOLANA_RPC_URL=https://...
HWR_COINGECKO_URL=https://...
```

Blockbook's canonical V2 interface is documented in its
[official OpenAPI documentation](https://github.com/trezor/blockbook/blob/master/docs/api.md).

## 5. Validate with the demo and tests

```bash
python -m unittest discover -v
python run_rebalancer.py \
  --holdings-file examples/demo_holdings.json \
  --prices-file examples/demo_prices.json
```

Enter `1000` at the prompt for the example top-up, or press Enter for zero.

## 6. First live run

```bash
python run_rebalancer.py --json
python run_rebalancer.py
```

Both commands ask for the new USD top-up amount. Press Enter to use zero.

Before acting, compare every fetched asset amount with Trezor Suite. Resolve
any discrepancy first—usually it means a missing account, passphrase wallet,
network, or staking position.

## 7. Telegram setup

1. If the supplied token was ever exposed, use BotFather to revoke it and
   generate a new token.
2. Copy `.env.example` to `.env`.
3. Put the new token in `TELEGRAM_BOT_TOKEN`.
4. Open the bot in Telegram and send `/start`.
5. Run `python -m wallet_rebalancer discover-telegram`.
6. Put the resulting numeric ID in both `TELEGRAM_CHAT_ID` and
   `TELEGRAM_ALLOWED_CHAT_IDS`.

One-shot notification:

```bash
python run_rebalancer.py --send-telegram
```

Enter the new USD top-up amount when prompted.

Interactive bot:

```bash
python -m wallet_rebalancer bot
```

The bot accepts only `/help`, `/start`, `/check`, and `/check 1000` from
allowlisted chat IDs.

## 8. Continuous operation

See [OPERATIONS.md](OPERATIONS.md) for a user-level systemd bot service and a
weekly check timer. Unattended monitoring does not require the Trezor to stay
connected because all configured identifiers are public.
