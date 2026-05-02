# Distributor Deployment Telegram Bot

Watches a factory contract on BNB Smart Chain (default
`0x000310fa98E36191ec79de241d72C6CA093EAfD3`) for `DistributorCreated`
events and pushes a Telegram alert containing:

- Token name / symbol
- Token contract address
- Amount of tokens funded into the new distributor (read from the matching
  `Transfer` log inside the same transaction)
- Distributor address, owner, operator
- Block number and tx hash, with bscscan links

Two background workers run together:

- **Chain monitor** — polls `eth_getLogs` for `DistributorCreated` and
  broadcasts new alerts to `TELEGRAM_CHAT_ID`.
- **Telegram listener** — long-polls `getUpdates` and serves commands.

## Commands

Only Telegram user IDs in `TELEGRAM_WHITELIST` get replies — everyone
else is silently ignored.

| Command | Description |
| --- | --- |
| `/check <tx_hash>` | Inspect a tx and report whether it hit `DistributorCreated`. On hit, returns the same details as a live alert. |
| `/status` | Last processed block, head block, uptime, whitelist size. |
| `/id` | Returns your Telegram user id and the chat id (handy for whitelist setup). |
| `/help` | Help text. |

The event ABI used:

```
DistributorCreated(
    address indexed owner,
    address indexed operator,
    address token,
    address distributorAddress
)
// topic0 = 0xe31b7f4b4f3b6042afb5723869d989be921bea013625e326792f25a623ea6c20
```

## Local setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) and grab the
   token. Add the bot to the target chat / channel and obtain the chat id
   (e.g. talk to [@userinfobot](https://t.me/userinfobot) or call
   `getUpdates`).
2. Install dependencies and run:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env   # edit it
   python bot.py
   ```

The bot persists the last processed block in `STATE_FILE` so restarts
don't double-send or miss events.

## Railway deployment

The repo includes `Procfile`, `runtime.txt`, and `railway.json` — Railway
will pick them up out of the box.

1. **New Project → Deploy from GitHub** and pick this repo / branch.
2. In the service **Variables** tab, set at minimum:

   ```
   TELEGRAM_TOKEN=...
   TELEGRAM_CHAT_ID=...
   TELEGRAM_WHITELIST=11111111,22222222
   ```

   Optionally override `RPC_URL`, `FACTORY_ADDRESS`, etc.
3. **Persist last-processed block across restarts (recommended).** Railway's
   filesystem is ephemeral. Attach a volume to the service (e.g. mounted at
   `/data`) and set:

   ```
   STATE_FILE=/data/bot_state.json
   ```

   Without a volume, the bot still works but will lose its position on every
   redeploy and re-scan only the last `BLOCK_LOOKBACK` blocks. Bumping
   `BLOCK_LOOKBACK` is a tradeoff: deeper backfill means duplicate alerts on
   first boot.
4. Deploy. Railway runs `python bot.py` with `restartPolicyType=ON_FAILURE`
   (configured in `railway.json`), so a crash auto-restarts.
5. Open Telegram and send `/help` from a whitelisted account to verify.

### Getting your user id for the whitelist

There's a chicken-and-egg problem: `/id` only works once you're already in
the whitelist. Easiest paths:

- DM [@userinfobot](https://t.me/userinfobot) — it tells you your id.
- Or temporarily set `TELEGRAM_WHITELIST=` (empty) to accept all, send
  `/id`, then add yourself and remove the empty value.

## Configuration reference

All settings are environment variables (see `.env.example`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | — | Bot token (required) |
| `TELEGRAM_CHAT_ID` | — | Destination chat for alerts (required) |
| `TELEGRAM_WHITELIST` | — | Comma-separated user IDs allowed to use commands. Empty = open. |
| `RPC_URL` | BSC public RPC | Any EVM JSON-RPC endpoint. Use `{API_KEY}` placeholder for templating. |
| `RPC_API_KEY` | — | Optional. Substituted into `RPC_URL` wherever `{API_KEY}` appears. |
| `FACTORY_ADDRESS` | `0x000310fa…EAfD3` | Contract to watch |
| `POLL_INTERVAL` | `5` | Seconds between polls |
| `BLOCK_LOOKBACK` | `20` | Blocks to scan on first run when no state file exists |
| `MAX_BLOCK_RANGE` | `1000` | Cap per `eth_getLogs` call |
| `STATE_FILE` | `.bot_state.json` | Where to persist `last_block` |
| `EXPLORER_TX` / `EXPLORER_ADDR` / `EXPLORER_TOKEN` | bscscan | URL prefixes used in messages |

## RPC providers

The bot only needs a JSON-RPC endpoint — no BscScan / Etherscan API key
is required. Two ways to plug in credentials:

```ini
# A) Full URL inline
RPC_URL=https://bnb-mainnet.g.alchemy.com/v2/abc123def456

# B) Placeholder + separate key (cleaner for env vars / rotation)
RPC_URL=https://bnb-mainnet.g.alchemy.com/v2/{API_KEY}
RPC_API_KEY=abc123def456
```

Tested URL templates:

| Provider | URL template |
| --- | --- |
| Alchemy | `https://bnb-mainnet.g.alchemy.com/v2/{API_KEY}` |
| QuickNode | `https://your-endpoint.bsc.quiknode.pro/{API_KEY}/` |
| Ankr | `https://rpc.ankr.com/bsc/{API_KEY}` |
| GetBlock | `https://go.getblock.io/{API_KEY}` |
| NodeReal | `https://bsc-mainnet.nodereal.io/v1/{API_KEY}` |
| Public BSC | `https://bsc-dataseed.bnbchain.org` (no key, rate-limited) |

## Notes

- Free public RPCs throttle `eth_getLogs` — for production, use a paid
  endpoint via the templates above.
- The bot decodes ERC-20 metadata via on-chain calls and caches it per
  token to keep per-event RPC usage low.
