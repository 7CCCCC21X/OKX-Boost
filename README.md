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

- **Chain monitor** — each iteration:
  1. Polls `eth_getLogs` for new `DistributorCreated` events from the
     factory and pushes a compact alert (token, amount, tx, time).
  2. Polls every distributor in the local store for `TimeSet` events
     and pushes a "claim time set" alert that names the token, the
     distributor, and the start/end timestamps.
- **Telegram listener** — long-polls `getUpdates` and serves commands.

The distributor → token map is persisted in `DISTRIBUTORS_FILE` so the
bot can correlate TimeSet events to the right token across restarts.
Set `BACKFILL_BLOCKS` > 0 on first deploy to backfill the store with
distributors created before the bot started.

## Commands

Only Telegram user IDs in `TELEGRAM_WHITELIST` get replies — everyone
else is silently ignored. The bot is bilingual (zh / en); the default
language is set via `DEFAULT_LANG` and each user can override their own
preference with `/lang` or via the menu.

| Command | Description |
| --- | --- |
| `/menu` | Interactive inline-button menu (status / help / check hint / language switch). |
| `/check <tx_hash>` | Inspect a tx and report whether it hit `DistributorCreated` or `TimeSet`. On hit, returns the same details as a live alert. **Tip:** sending a bare tx hash (no `/check` prefix) does the same thing. |
| `/activate [chat_id]` | Add the current chat (or the given chat_id) to the broadcast list. Whitelisted users only — add the bot to a group, send `/activate`, and alerts start flowing into that group. |
| `/deactivate [chat_id]` | Remove the current chat (or given chat_id) from the broadcast list. |
| `/subs` | List all chats currently receiving alerts. |
| `/preview` | Send a sample DistributorCreated and TimeSet alert (with footer buttons) to the current chat. Use it to verify formatting after changing `FOOTER_BTN*` env vars or translations. Sends to the current chat only — does not fan out. |
| `/interval [value]` | Show or change how often the chain monitor polls. Accepts `30s`, `3m`, `1h`, or a plain number of seconds. With no argument, opens an inline picker. Default is 3 minutes; bounded by `MIN_POLL_INTERVAL` / `MAX_POLL_INTERVAL`. |
| `/status` | Factory, chain id, head block, last processed, uptime, current poll interval, whitelist size, your language. |
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
    address distributorAddress,
    uint256 initialTotalAmount
)
// topic0 = 0xcf9068cf0507f6c18ee38fd73ba24a528f514f0e73ad08229b6db0541071d48d
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
| `POLL_INTERVAL` | `180` | Default seconds between polls. Override at runtime via `/interval`. |
| `MIN_POLL_INTERVAL` | `5` | Lower bound for `/interval`. |
| `MAX_POLL_INTERVAL` | `3600` | Upper bound for `/interval`. |
| `BLOCK_LOOKBACK` | `20` | Blocks to scan on first run when no state file exists |
| `MAX_BLOCK_RANGE` | `1000` | Cap per `eth_getLogs` call |
| `MIN_TOKEN_AMOUNT` | `1000` | Skip DistributorCreated broadcast when funding amount (in token units) is below this. `/check` always shows the result. TimeSet alerts ignore this filter. Set to `0` to disable. |
| `DISTRIBUTORS_FILE` | `.distributors.json` | Where the distributor → token map is persisted. |
| `BACKFILL_BLOCKS` | `0` | One-shot scan on startup to populate the distributor store with pre-existing distributors. `0` = skip. |
| `LOGS_ADDRESS_CHUNK` | `100` | Max addresses per `eth_getLogs` call when polling TimeSet. |
| `STATE_FILE` | `.bot_state.json` | Where to persist `last_block` |
| `DEFAULT_LANG` | `zh` | Default UI language: `zh` or `en`. |
| `USER_LANG_FILE` | `.user_lang.json` | Where per-user `/lang` choices are stored. |
| `RUNTIME_CONFIG_FILE` | `.runtime_config.json` | Where the current `/interval` value is stored. |
| `SUBSCRIBERS_FILE` | `.subscribers.json` | Where extra `/activate`-d chats are stored. |
| `FOOTER_BTN1_TEXT` / `FOOTER_BTN1_URL` | OKX rebate contact | Inline-button card appended to every broadcast. |
| `FOOTER_BTN2_TEXT` / `FOOTER_BTN2_URL` | Dune dashboard | Inline-button card appended to every broadcast. |
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
