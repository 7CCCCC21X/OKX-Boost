"""Telegram bot that watches a BSC factory contract for `DistributorCreated`
events and reports the token contract + amount of tokens funded into the
freshly-deployed distributor.

Event signature (from the user's screenshot):
    DistributorCreated(
        address indexed owner,
        address indexed operator,
        address token,
        address distributorAddress,
    )
Topic0 = 0xe31b7f4b4f3b6042afb5723869d989be921bea013625e326792f25a623ea6c20

The amount of tokens shown in the alert is taken from the ERC-20 `Transfer`
log inside the same transaction whose `to` address equals the distributor
that was just created.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from web3 import Web3
from web3.exceptions import BlockNotFound
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def must_have_telegram_creds() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error(
            "Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID. Copy .env.example to "
            ".env and fill them in."
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
    STATE_FILE.write_text(json.dumps({"last_block": block}))


def short_addr(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"


def format_amount(raw: int, decimals: int) -> str:
    if decimals == 0:
        return f"{raw:,}"
    value = Decimal(raw) / (Decimal(10) ** decimals)
    quantized = value.quantize(Decimal(1)) if value == value.to_integral() else value.normalize()
    return f"{quantized:,f}"


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            log.error("Telegram error %s: %s", r.status_code, r.text)
    except requests.RequestException as exc:
        log.error("Telegram request failed: %s", exc)


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
# Event handling
# ---------------------------------------------------------------------------


def _to_hex(value: Any) -> str:
    """Return a 0x-prefixed lowercase hex string regardless of web3 version."""
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    s = str(value).lower()
    return s if s.startswith("0x") else "0x" + s


def find_funding_amount(
    receipt_logs: list[LogReceipt], token: str, distributor: str
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


def handle_event(w3: Web3, event: EventData) -> None:
    args = event["args"]
    owner = args["owner"]
    operator = args["operator"]
    token = args["token"]
    distributor = args["distributorAddress"]
    tx_hash = event["transactionHash"].hex()

    receipt = w3.eth.get_transaction_receipt(tx_hash)
    amount_raw = find_funding_amount(receipt["logs"], token, distributor)

    meta = get_token_meta(w3, token)
    symbol = meta["symbol"]
    name = meta["name"]
    decimals = meta["decimals"]

    if amount_raw is not None:
        amount_str = f"{format_amount(amount_raw, decimals)} {symbol}"
    else:
        amount_str = "(no funding transfer in this tx)"

    msg = (
        "🚀 <b>New Distributor Deployed</b>\n"
        f"<b>Token:</b> {name} ({symbol})\n"
        f"<b>Token Contract:</b> <a href=\"{EXPLORER_TOKEN}{token}\">{token}</a>\n"
        f"<b>Amount:</b> {amount_str}\n"
        f"<b>Distributor:</b> <a href=\"{EXPLORER_ADDR}{distributor}\">{short_addr(distributor)}</a>\n"
        f"<b>Owner:</b> <a href=\"{EXPLORER_ADDR}{owner}\">{short_addr(owner)}</a>\n"
        f"<b>Operator:</b> <a href=\"{EXPLORER_ADDR}{operator}\">{short_addr(operator)}</a>\n"
        f"<b>Block:</b> {event['blockNumber']}\n"
        f"<b>Tx:</b> <a href=\"{EXPLORER_TX}{tx_hash}\">{short_addr(tx_hash)}</a>"
    )
    log.info(
        "DistributorCreated token=%s distributor=%s amount=%s tx=%s",
        token, distributor, amount_str, tx_hash,
    )
    send_telegram(msg)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main() -> None:
    must_have_telegram_creds()

    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        log.error("Cannot reach RPC at %s", RPC_URL)
        sys.exit(1)

    factory = w3.eth.contract(address=FACTORY_ADDRESS, abi=[DISTRIBUTOR_CREATED_ABI])
    event_cls = factory.events.DistributorCreated

    head = w3.eth.block_number
    last_block = load_last_block(default=max(0, head - BLOCK_LOOKBACK))
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
                    event = event_cls().process_log(raw)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Failed to decode log: %s", exc)
                    continue
                handle_event(w3, event)

            last_block = to_block
            save_last_block(last_block)
        except BlockNotFound:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("Bye.")
            return
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            log.exception("Loop error: %s", exc)
            time.sleep(POLL_INTERVAL * 2)
        else:
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
