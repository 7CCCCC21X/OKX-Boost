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

## Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) and grab the
   token. Add the bot to the target chat / channel and obtain the chat id
   (e.g. talk to [@userinfobot](https://t.me/userinfobot) or call
   `getUpdates`).
2. Install dependencies:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Copy the env template and fill it in:

   ```bash
   cp .env.example .env
   # edit .env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
   ```

4. Run the bot:

   ```bash
   python bot.py
   ```

The bot persists the last processed block in `.bot_state.json` so restarts
don't double-send or miss events.

## Configuration

All settings are driven by environment variables (see `.env.example`).
Switch chains by overriding `RPC_URL`, `FACTORY_ADDRESS`, and the
`EXPLORER_*` prefixes.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | — | Bot token from BotFather (required) |
| `TELEGRAM_CHAT_ID` | — | Destination chat id (required) |
| `RPC_URL` | BSC public RPC | Any EVM JSON-RPC endpoint |
| `FACTORY_ADDRESS` | `0x000310fa…EAfD3` | Contract to watch |
| `POLL_INTERVAL` | `5` | Seconds between polls |
| `BLOCK_LOOKBACK` | `20` | Blocks to scan on first run |
| `MAX_BLOCK_RANGE` | `1000` | Cap per `eth_getLogs` call |

## Notes

- Free public RPCs sometimes throttle `eth_getLogs`. If you see errors,
  point `RPC_URL` at a paid endpoint (Ankr, QuickNode, Alchemy, etc.).
- The bot decodes ERC-20 metadata via on-chain calls and caches it per
  token to keep per-event RPC usage low.
