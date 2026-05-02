"""Telegram bot that watches a BSC factory contract for `DistributorCreated`
events and reports the token contract + amount of tokens funded into the
freshly-deployed distributor.

Two background workers run side by side:

1. Chain monitor — polls `eth_getLogs` for new `DistributorCreated` events
   from the factory and pushes alerts to `TELEGRAM_CHAT_ID`.
2. Telegram listener — long-polls `getUpdates` and serves commands
   (`/check <txhash>`, `/status`, `/help`, `/id`). Only Telegram user IDs
   in `TELEGRAM_WHITELIST` get replies.

Event signature (from the user's screenshot):
    DistributorCreated(
        address indexed owner,
        address indexed operator,
        address token,
        address distributorAddress,
    )
Topic0 = 0xe31b7f4b4f3b6042afb5723869d989be921bea013625e326792f25a623ea6c20
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from web3 import Web3
from web3.exceptions import BlockNotFound, TransactionNotFound
from web3.types import EventData, LogReceipt

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("distributor-bot")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RPC_URL = os.getenv("RPC_URL", "https://bsc-dataseed.bnbchain.org")
FACTORY_ADDRESS = Web3.to_checksum_address(
    os.getenv("FACTORY_ADDRESS", "0x000310fa98E36191ec79de241d72C6CA093EAfD3")
)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))
BLOCK_LOOKBACK = int(os.getenv("BLOCK_LOOKBACK", "20"))
MAX_BLOCK_RANGE = int(os.getenv("MAX_BLOCK_RANGE", "1000"))
EXPLORER_TX = os.getenv("EXPLORER_TX", "https://bscscan.com/tx/")
EXPLORER_ADDR = os.getenv("EXPLORER_ADDR", "https://bscscan.com/address/")
EXPLORER_TOKEN = os.getenv("EXPLORER_TOKEN", "https://bscscan.com/token/")
STATE_FILE = Path(os.getenv("STATE_FILE", ".bot_state.json"))


def _parse_id_list(raw: str) -> set[int]:
    out: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.add(int(token))
        except ValueError:
            log.warning("Ignoring invalid id in whitelist: %r", token)
    return out


WHITELIST = _parse_id_list(os.getenv("TELEGRAM_WHITELIST", ""))

DISTRIBUTOR_CREATED_TOPIC = (
    "0xe31b7f4b4f3b6042afb5723869d989be921bea013625e326792f25a623ea6c20"
)
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

ERC20_ABI = json.loads(
    """[
    {"constant":true,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}
    ]"""
)

DISTRIBUTOR_CREATED_ABI = {
    "anonymous": False,
    "inputs": [
        {"indexed": True, "name": "owner", "type": "address"},
        {"indexed": True, "name": "operator", "type": "address"},
        {"indexed": False, "name": "token", "type": "address"},
        {"indexed": False, "name": "distributorAddress", "type": "address"},
    ],
    "name": "DistributorCreated",
    "type": "event",
}

TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
BOT_STARTED_AT = time.time()
LAST_BLOCK_LOCK = threading.Lock()
LAST_BLOCK_SEEN = {"value": 0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def must_have_telegram_creds() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID. Copy .env.example to "
            ".env and fill them in (or set them in Railway)."
        )
        sys.exit(1)


def load_last_block(default: int) -> int:
    if STATE_FILE.exists():
        try:
            return int(json.loads(STATE_FILE.read_text())["last_block"])
        except (ValueError, KeyError, json.JSONDecodeError):
            log.warning("State file corrupt, ignoring.")
    return default


def save_last_block(block: int) -> None:
    try:
        STATE_FILE.write_text(json.dumps({"last_block": block}))
    except OSError as exc:
        # Railway without a mounted volume has an ephemeral filesystem; we
        # tolerate failures here so the loop keeps running.
        log.warning("Could not persist state to %s: %s", STATE_FILE, exc)


def short_addr(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def format_amount(raw: int, decimals: int) -> str:
    if decimals == 0:
        return f"{raw:,}"
    value = Decimal(raw) / (Decimal(10) ** decimals)
    quantized = value.quantize(Decimal(1)) if value == value.to_integral() else value.normalize()
    return f"{quantized:,f}"


def _to_hex(value: Any) -> str:
    """Return a 0x-prefixed lowercase hex string regardless of web3 version."""
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    s = str(value).lower()
    return s if s.startswith("0x") else "0x" + s


# ---------------------------------------------------------------------------
# Telegram primitives
# ---------------------------------------------------------------------------


_TELEGRAM_BASE = lambda: f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def telegram_send(chat_id: str | int, text: str, reply_to: int | None = None) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    try:
        r = requests.post(f"{_TELEGRAM_BASE()}/sendMessage", json=payload, timeout=15)
        if r.status_code != 200:
            log.error("Telegram error %s: %s", r.status_code, r.text)
    except requests.RequestException as exc:
        log.error("Telegram request failed: %s", exc)


def broadcast_alert(text: str) -> None:
    telegram_send(TELEGRAM_CHAT_ID, text)


# ---------------------------------------------------------------------------
# Token metadata (cached)
# ---------------------------------------------------------------------------


_token_meta_cache: dict[str, dict[str, Any]] = {}


def get_token_meta(w3: Web3, token: str) -> dict[str, Any]:
    if token in _token_meta_cache:
        return _token_meta_cache[token]
    contract = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
    meta = {"symbol": "?", "name": "?", "decimals": 18}
    for key in ("symbol", "name", "decimals"):
        try:
            meta[key] = getattr(contract.functions, key)().call()
        except Exception as exc:  # noqa: BLE001 - tolerate non-conforming tokens
            log.debug("token %s %s() failed: %s", token, key, exc)
    _token_meta_cache[token] = meta
    return meta


# ---------------------------------------------------------------------------
# Event decoding & formatting
# ---------------------------------------------------------------------------


def find_funding_amount(
    receipt_logs: Iterable[LogReceipt], token: str, distributor: str
) -> int | None:
    """Locate the ERC-20 Transfer to `distributor` for `token` in this tx."""
    token_lc = token.lower()
    distributor_lc = distributor.lower()
    transfer_topic = TRANSFER_TOPIC.lower()
    for entry in receipt_logs:
        if entry["address"].lower() != token_lc:
            continue
        topics = entry["topics"]
        if len(topics) < 3:
            continue
        if _to_hex(topics[0]) != transfer_topic:
            continue
        to_addr = "0x" + _to_hex(topics[2])[-40:]
        if to_addr != distributor_lc:
            continue
        data_hex = _to_hex(entry["data"])
        return int(data_hex, 16) if data_hex != "0x" else 0
    return None


def format_distributor_alert(
    w3: Web3,
    *,
    title: str,
    owner: str,
    operator: str,
    token: str,
    distributor: str,
    block_number: int,
    tx_hash: str,
    receipt_logs: Iterable[LogReceipt],
) -> str:
    meta = get_token_meta(w3, token)
    symbol = meta["symbol"]
    name = meta["name"]
    decimals = meta["decimals"]

    amount_raw = find_funding_amount(receipt_logs, token, distributor)
    if amount_raw is not None:
        amount_str = f"{format_amount(amount_raw, decimals)} {symbol}"
    else:
        amount_str = "(no funding transfer in this tx)"

    return (
        f"{title}\n"
        f"<b>Token:</b> {name} ({symbol})\n"
        f"<b>Token Contract:</b> <a href=\"{EXPLORER_TOKEN}{token}\">{token}</a>\n"
        f"<b>Amount:</b> {amount_str}\n"
        f"<b>Distributor:</b> <a href=\"{EXPLORER_ADDR}{distributor}\">{short_addr(distributor)}</a>\n"
        f"<b>Owner:</b> <a href=\"{EXPLORER_ADDR}{owner}\">{short_addr(owner)}</a>\n"
        f"<b>Operator:</b> <a href=\"{EXPLORER_ADDR}{operator}\">{short_addr(operator)}</a>\n"
        f"<b>Block:</b> {block_number}\n"
        f"<b>Tx:</b> <a href=\"{EXPLORER_TX}{tx_hash}\">{short_addr(tx_hash)}</a>"
    )


def handle_event(w3: Web3, event: EventData) -> None:
    args = event["args"]
    tx_hash = _to_hex(event["transactionHash"])
    receipt = w3.eth.get_transaction_receipt(tx_hash)

    msg = format_distributor_alert(
        w3,
        title="🚀 <b>New Distributor Deployed</b>",
        owner=args["owner"],
        operator=args["operator"],
        token=args["token"],
        distributor=args["distributorAddress"],
        block_number=event["blockNumber"],
        tx_hash=tx_hash,
        receipt_logs=receipt["logs"],
    )
    log.info(
        "DistributorCreated token=%s distributor=%s tx=%s",
        args["token"], args["distributorAddress"], tx_hash,
    )
    broadcast_alert(msg)


# ---------------------------------------------------------------------------
# /check command
# ---------------------------------------------------------------------------


def check_transaction(w3: Web3, factory_event_cls: Any, tx_hash: str) -> str:
    """Inspect a transaction and report whether it hit a DistributorCreated."""
    if not TX_HASH_RE.fullmatch(tx_hash):
        return "❌ Invalid transaction hash. Expected format: 0x + 64 hex chars."

    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except TransactionNotFound:
        return f"❌ Transaction not found: <code>{tx_hash}</code>"
    except Exception as exc:  # noqa: BLE001
        return f"⚠️ RPC error: {exc}"

    if receipt is None:
        return f"⏳ Pending or unknown: <code>{tx_hash}</code>"
    if receipt.get("status") != 1:
        return (
            "⚪ <b>Not a hit</b> — transaction reverted.\n"
            f"<a href=\"{EXPLORER_TX}{tx_hash}\">{tx_hash}</a>"
        )

    factory_lc = FACTORY_ADDRESS.lower()
    target_topic = DISTRIBUTOR_CREATED_TOPIC.lower()
    matches = []
    for raw in receipt["logs"]:
        if raw["address"].lower() != factory_lc:
            continue
        topics = raw["topics"]
        if not topics or _to_hex(topics[0]) != target_topic:
            continue
        try:
            decoded = factory_event_cls().process_log(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not decode DistributorCreated log: %s", exc)
            continue
        matches.append(decoded)

    if not matches:
        return (
            "⚪ <b>Not a hit</b> — no DistributorCreated event from "
            f"{short_addr(FACTORY_ADDRESS)} in this tx.\n"
            f"<a href=\"{EXPLORER_TX}{tx_hash}\">{tx_hash}</a>"
        )

    parts = []
    for ev in matches:
        args = ev["args"]
        parts.append(
            format_distributor_alert(
                w3,
                title="✅ <b>HIT — DistributorCreated</b>",
                owner=args["owner"],
                operator=args["operator"],
                token=args["token"],
                distributor=args["distributorAddress"],
                block_number=ev["blockNumber"],
                tx_hash=tx_hash,
                receipt_logs=receipt["logs"],
            )
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Telegram command handler
# ---------------------------------------------------------------------------


HELP_TEXT = (
    "<b>Distributor Monitor Bot</b>\n"
    "Commands:\n"
    "  /check &lt;tx_hash&gt; — check whether a tx emitted DistributorCreated\n"
    "  /status — bot status (last seen block, head, uptime)\n"
    "  /id — show your Telegram user id (handy for whitelist setup)\n"
    "  /help — this help"
)


def is_whitelisted(user_id: int) -> bool:
    return not WHITELIST or user_id in WHITELIST


def handle_command(
    w3: Web3, factory_event_cls: Any, message: dict[str, Any]
) -> None:
    user = message.get("from") or {}
    user_id = user.get("id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    msg_id = message.get("message_id")

    if user_id is None or chat_id is None or not text:
        return
    if not is_whitelisted(user_id):
        log.info(
            "Ignored message from non-whitelisted user %s (%s): %r",
            user_id, user.get("username"), text[:64],
        )
        return

    # Strip optional `@BotName` suffix on commands
    head, _, rest = text.partition(" ")
    cmd = head.split("@", 1)[0].lower()
    arg = rest.strip()

    if cmd in ("/start", "/help"):
        telegram_send(chat_id, HELP_TEXT, reply_to=msg_id)
        return

    if cmd == "/id":
        telegram_send(
            chat_id,
            f"Your user id: <code>{user_id}</code>\nChat id: <code>{chat_id}</code>",
            reply_to=msg_id,
        )
        return

    if cmd == "/status":
        try:
            head_block = w3.eth.block_number
        except Exception as exc:  # noqa: BLE001
            head_block = f"err: {exc}"
        with LAST_BLOCK_LOCK:
            seen = LAST_BLOCK_SEEN["value"]
        uptime = int(time.time() - BOT_STARTED_AT)
        telegram_send(
            chat_id,
            (
                "<b>Bot status</b>\n"
                f"Factory: <code>{FACTORY_ADDRESS}</code>\n"
                f"Head block: <code>{head_block}</code>\n"
                f"Last processed: <code>{seen}</code>\n"
                f"Uptime: {uptime}s\n"
                f"Whitelist size: {len(WHITELIST) if WHITELIST else 'OPEN'}"
            ),
            reply_to=msg_id,
        )
        return

    if cmd == "/check":
        if not arg:
            telegram_send(chat_id, "Usage: /check &lt;tx_hash&gt;", reply_to=msg_id)
            return
        # Accept the first 0x...64 hex match in the argument
        m = TX_HASH_RE.search(arg)
        target = m.group(0) if m else arg
        result = check_transaction(w3, factory_event_cls, target.lower())
        telegram_send(chat_id, result, reply_to=msg_id)
        return

    # Unknown command from a whitelisted user — nudge them to /help
    if cmd.startswith("/"):
        telegram_send(chat_id, "Unknown command. Try /help.", reply_to=msg_id)


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def chain_monitor(w3: Web3, factory_event_cls: Any) -> None:
    head = w3.eth.block_number
    last_block = load_last_block(default=max(0, head - BLOCK_LOOKBACK))
    with LAST_BLOCK_LOCK:
        LAST_BLOCK_SEEN["value"] = last_block
    log.info(
        "Watching factory %s on chain id %s, starting from block %s (head=%s)",
        FACTORY_ADDRESS, w3.eth.chain_id, last_block, head,
    )

    while True:
        try:
            head = w3.eth.block_number
            if head <= last_block:
                time.sleep(POLL_INTERVAL)
                continue

            from_block = last_block + 1
            to_block = min(head, from_block + MAX_BLOCK_RANGE - 1)

            logs = w3.eth.get_logs(
                {
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "address": FACTORY_ADDRESS,
                    "topics": [DISTRIBUTOR_CREATED_TOPIC],
                }
            )
            for raw in logs:
                try:
                    event = factory_event_cls().process_log(raw)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Failed to decode log: %s", exc)
                    continue
                handle_event(w3, event)

            last_block = to_block
            save_last_block(last_block)
            with LAST_BLOCK_LOCK:
                LAST_BLOCK_SEEN["value"] = last_block
        except BlockNotFound:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            return
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            log.exception("Chain monitor error: %s", exc)
            time.sleep(POLL_INTERVAL * 2)
        else:
            time.sleep(POLL_INTERVAL)


def telegram_listener(w3: Web3, factory_event_cls: Any) -> None:
    """Long-polls Telegram getUpdates and dispatches commands."""
    offset: int | None = None
    log.info(
        "Telegram listener started (whitelist=%s)",
        sorted(WHITELIST) if WHITELIST else "OPEN — accepting all users",
    )
    while True:
        try:
            params: dict[str, Any] = {
                "timeout": 30,
                "allowed_updates": json.dumps(["message"]),
            }
            if offset is not None:
                params["offset"] = offset
            r = requests.get(
                f"{_TELEGRAM_BASE()}/getUpdates",
                params=params,
                timeout=60,
            )
            data = r.json()
            if not data.get("ok"):
                log.warning("getUpdates failed: %s", data)
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if not msg:
                    continue
                try:
                    handle_command(w3, factory_event_cls, msg)
                except Exception as exc:  # noqa: BLE001
                    log.exception("handle_command error: %s", exc)
        except KeyboardInterrupt:
            return
        except requests.RequestException as exc:
            log.warning("Telegram poll error: %s", exc)
            time.sleep(5)
        except Exception as exc:  # noqa: BLE001
            log.exception("Telegram listener error: %s", exc)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    must_have_telegram_creds()

    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        log.error("Cannot reach RPC at %s", RPC_URL)
        sys.exit(1)

    factory = w3.eth.contract(address=FACTORY_ADDRESS, abi=[DISTRIBUTOR_CREATED_ABI])
    factory_event_cls = factory.events.DistributorCreated

    threads = [
        threading.Thread(
            target=chain_monitor, args=(w3, factory_event_cls),
            name="chain-monitor", daemon=True,
        ),
        threading.Thread(
            target=telegram_listener, args=(w3, factory_event_cls),
            name="tg-listener", daemon=True,
        ),
    ]
    for t in threads:
        t.start()

    try:
        while True:
            for t in threads:
                if not t.is_alive():
                    log.error("Worker %s died, exiting so the platform can restart us.", t.name)
                    sys.exit(1)
            time.sleep(10)
    except KeyboardInterrupt:
        log.info("Bye.")


if __name__ == "__main__":
    main()
