# Distributor Deployment Telegram Bot

Watches the OKX Boost factory contract (default
`0x000310fa98E36191ec79de241d72C6CA093EAfD3`) on **one or more EVM chains**
(BSC, Ethereum, Arbitrum, Base, …) for `DistributorCreated` events and
pushes a bilingual (zh + en) Telegram alert containing:

- The chain the event was on (every alert is labeled `[BSC]` / `[Base]` / …)
- Token name / symbol
- Token contract address
- Amount of tokens funded into the new distributor (read from the matching
  `Transfer` log inside the same transaction)
- Distributor address, owner, operator
- Block number and tx hash, with explorer links scoped to that chain

Workers running together:

- **Chain monitor** — one thread per configured chain. Each iteration:
  1. Polls `eth_getLogs` for new `DistributorCreated` events from that
     chain's factory and pushes a compact alert (token, amount, tx, time).
  2. Polls every distributor in the per-chain store for `TimeSet` events
     and pushes a "claim time set" alert that names the token, the
     distributor, and the start/end timestamps.
- **Telegram listener** — long-polls `getUpdates` and serves commands.
  Shared across all chains; routes `/check <chain> <tx>` to the right
  chain's RPC.

State is persisted per chain under `STATE_DIR`:
`<STATE_DIR>/.bot_state.<chain>.json` (last processed block) and
`<STATE_DIR>/.distributors.<chain>.json` (distributor → token map). The
bot auto-migrates pre-multi-chain `.bot_state.json` /
`.distributors.json` files into the `bsc` slot on first boot. Set
`BACKFILL_BLOCKS` > 0 on first deploy to backfill the store with
distributors created before the bot started (applied to every chain).

## Commands

Only Telegram user IDs in `TELEGRAM_WHITELIST` get replies — everyone
else is silently ignored. The bot is bilingual (zh / en); the default
language is set via `DEFAULT_LANG` and each user can override their own
preference with `/lang` or via the menu.

| Command | Description |
| --- | --- |
| `/menu` | Interactive inline-button menu (status / help / check hint / language switch). |
| `/check <chain> <tx_hash>` | Inspect a tx on the given chain and report whether it hit `DistributorCreated` or `TimeSet`. On hit, returns the same details as a live alert. The chain prefix is required (e.g. `/check bsc 0x…`, `/check eth 0x…`). |
| `/activate [chat_id]` | Add the current chat (or the given chat_id) to the broadcast list. Whitelisted users only — add the bot to a group, send `/activate`, and alerts from every monitored chain start flowing into that group. |
| `/deactivate [chat_id]` | Remove the current chat (or given chat_id) from the broadcast list. |
| `/subs` | List all chats currently receiving alerts. |
| `/preview [chain]` | Send a sample DistributorCreated and TimeSet alert (with footer buttons) to the current chat, rendered against the given chain (default: `DEFAULT_CHAIN`). Use it to verify formatting after changing `FOOTER_BTN*` env vars or translations. Sends to the current chat only — does not fan out. |
| `/interval [value]` | Show or change how often the chain monitor polls. Accepts `30s`, `3m`, `1h`, or a plain number of seconds. With no argument, opens an inline picker. Default is 3 minutes; bounded by `MIN_POLL_INTERVAL` / `MAX_POLL_INTERVAL`. The same interval applies to every chain monitor. |
| `/status` | Per-chain section (factory, chain id, head, last processed, distributors tracked) plus global section (uptime, interval, whitelist, language). |
| `/lang` | Switch your language (zh / en). |
| `/id` | Returns your Telegram user id and the chat id (handy for whitelist setup). |
| `/help` | Help text. |

The bot also calls `setMyCommands` at startup so the `/` popup in
Telegram clients lists these commands with bilingual descriptions.

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

The bot persists each chain's last processed block under `STATE_DIR`
(default `.`), as `<STATE_DIR>/.bot_state.<chain>.json`, so restarts
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
   CHAINS=bsc,eth,arb,base
   BSC_RPC_URL=...    ETH_RPC_URL=...
   ARB_RPC_URL=...    BASE_RPC_URL=...
   ETH_EXPLORER_TX=https://etherscan.io/tx/    # ditto _ADDR / _TOKEN
   ARB_EXPLORER_TX=https://arbiscan.io/tx/     # ditto
   BASE_EXPLORER_TX=https://basescan.org/tx/   # ditto
   ```

   Leave `CHAINS` unset to fall back to single-chain mode (uses the legacy
   `RPC_URL` / `FACTORY_ADDRESS` / `EXPLORER_*` vars, treated as `bsc`).
3. **Persist last-processed block across restarts (recommended).** Railway's
   filesystem is ephemeral. Attach a volume to the service (e.g. mounted at
   `/data`) and set:

   ```
   STATE_DIR=/data
   ```

   The bot writes one file per chain under that directory
   (`.bot_state.<chain>.json`, `.distributors.<chain>.json`). Without a
   volume, the bot still works but will lose its position on every
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

### Global

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | — | Bot token (required) |
| `TELEGRAM_CHAT_ID` | — | Destination chat for alerts (required). All chains' alerts go here, each labeled. |
| `TELEGRAM_WHITELIST` | — | Comma-separated user IDs allowed to use commands. Empty = open. |
| `CHAINS` | (single-chain mode) | Comma-separated chain keys to monitor (e.g. `bsc,eth,arb,base`). Leave empty to use the legacy single-chain `RPC_URL` / `FACTORY_ADDRESS` / `EXPLORER_*` vars (treated as `bsc`). |
| `DEFAULT_CHAIN` | first key in `CHAINS` | Chain used by `/preview` when no chain argument is given. |
| `POLL_INTERVAL` | `180` | Default seconds between polls. Applies to every chain monitor. Override at runtime via `/interval`. |
| `MIN_POLL_INTERVAL` | `5` | Lower bound for `/interval`. |
| `MAX_POLL_INTERVAL` | `3600` | Upper bound for `/interval`. |
| `BLOCK_LOOKBACK` | `20` | Blocks to scan on first run per chain when no state file exists. |
| `MAX_BLOCK_RANGE` | `1000` | Cap per `eth_getLogs` call. |
| `MIN_TOKEN_AMOUNT` | `1000` | Skip DistributorCreated broadcast when funding amount (in token units) is below this. `/check` always shows the result. TimeSet alerts ignore this filter. Set to `0` to disable. Applies globally. |
| `BACKFILL_BLOCKS` | `0` | One-shot scan on startup per chain to populate the distributor store with pre-existing distributors. `0` = skip. |
| `LOGS_ADDRESS_CHUNK` | `100` | Max addresses per `eth_getLogs` call when polling TimeSet. |
| `STATE_DIR` | `.` | Directory holding per-chain state files (`.bot_state.<chain>.json`, `.distributors.<chain>.json`). Point at a Railway volume to survive restarts. |
| `DEFAULT_LANG` | `zh` | Default UI language: `zh` or `en`. Broadcasts always include both. |
| `USER_LANG_FILE` | `.user_lang.json` | Where per-user `/lang` choices are stored. |
| `RUNTIME_CONFIG_FILE` | `.runtime_config.json` | Where the current `/interval` value is stored. |
| `SUBSCRIBERS_FILE` | `.subscribers.json` | Where extra `/activate`-d chats are stored. |
| `FOOTER_BTN1_TEXT` / `FOOTER_BTN1_URL` | OKX rebate contact | Inline-button card appended to every broadcast. |
| `FOOTER_BTN2_TEXT` / `FOOTER_BTN2_URL` | Dune dashboard | Inline-button card appended to every broadcast. |

### Per-chain (set for each key K listed in `CHAINS`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `<K>_RPC_URL` | — for non-bsc; `bsc-dataseed.bnbchain.org` for bsc | Any EVM JSON-RPC endpoint. Use `{API_KEY}` placeholder for templating. |
| `<K>_RPC_API_KEY` | — | Optional. Substituted into `<K>_RPC_URL` wherever `{API_KEY}` appears. |
| `<K>_FACTORY_ADDRESS` | `0x000310fa…EAfD3` | Contract to watch on this chain. |
| `<K>_EXPLORER_TX` | bscscan for bsc; — for others | URL prefix for tx links. |
| `<K>_EXPLORER_ADDR` | bscscan for bsc; — for others | URL prefix for address links. |
| `<K>_EXPLORER_TOKEN` | bscscan for bsc; — for others | URL prefix for token links. |
| `<K>_DISPLAY_NAME` | built-in (BSC, Ethereum, Arbitrum, Base, Polygon, Optimism) or upper-cased key | Shown in alert labels (`[BSC]`, `[Ethereum]`, …). |

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
