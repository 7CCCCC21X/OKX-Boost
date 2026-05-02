"""Telegram bot that watches a BSC factory contract for `DistributorCreated`
events and reports the token contract + amount of tokens funded into the
freshly-deployed distributor.

Two background workers run side by side:

1. Chain monitor — polls `eth_getLogs` for new `DistributorCreated` events
   from the factory and pushes alerts to `TELEGRAM_CHAT_ID`.
2. Telegram listener — long-polls `getUpdates` and serves commands
   (`/menu`, `/check`, `/status`, `/lang`, `/help`, `/id`) plus inline
   button callbacks. Only Telegram user IDs in `TELEGRAM_WHITELIST` get
   replies. Each user has their own zh/en preference.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from web3 import Web3
from web3.exceptions import BlockNotFound, TransactionNotFound
from web3.types import EventData, LogReceipt

from i18n import LANG_LABEL, LANGS, t

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("distributor-bot")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_RPC_URL_TEMPLATE = os.getenv("RPC_URL", "https://bsc-dataseed.bnbchain.org")
RPC_API_KEY = os.getenv("RPC_API_KEY", "").strip()


def _resolve_rpc_url(template: str, api_key: str) -> str:
    if "{API_KEY}" not in template:
        return template
    if not api_key:
        log.error(
            "RPC_URL contains {API_KEY} placeholder but RPC_API_KEY is empty. "
            "Either set RPC_API_KEY or paste the full URL into RPC_URL."
        )
        sys.exit(1)
    return template.replace("{API_KEY}", api_key)


def _redact(url: str) -> str:
    return url.replace(RPC_API_KEY, "***") if RPC_API_KEY else url


_URL_RE = re.compile(r"https?://[^\s'\"<>]+")


def _scrub_for_user(text: str) -> str:
    """Scrub RPC URLs / keys from user-facing error messages.

    `requests` exceptions like ``403 Client Error: Forbidden for url:
    https://rpc.example.com/<KEY>`` would otherwise leak the endpoint
    and credential into a Telegram reply, which can be screenshotted.
    """
    s = _redact(text)
    return _URL_RE.sub("<rpc>", s)


RPC_URL = _resolve_rpc_url(_RPC_URL_TEMPLATE, RPC_API_KEY)
FACTORY_ADDRESS = Web3.to_checksum_address(
    os.getenv("FACTORY_ADDRESS", "0x000310fa98E36191ec79de241d72C6CA093EAfD3")
)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "180"))  # default 3 min
BLOCK_LOOKBACK = int(os.getenv("BLOCK_LOOKBACK", "20"))
MAX_BLOCK_RANGE = int(os.getenv("MAX_BLOCK_RANGE", "1000"))
MIN_POLL_INTERVAL = float(os.getenv("MIN_POLL_INTERVAL", "5"))
MAX_POLL_INTERVAL = float(os.getenv("MAX_POLL_INTERVAL", "3600"))


def _parse_decimal(raw: str, default: str) -> Decimal:
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        log.warning("Invalid decimal %r, falling back to %s", raw, default)
        return Decimal(default)


# Skip broadcasts whose funding amount is below this threshold (in token units,
# already divided by 10**decimals). Set to 0 to disable filtering.
MIN_TOKEN_AMOUNT: Decimal = _parse_decimal(
    os.getenv("MIN_TOKEN_AMOUNT", "1000"), "1000"
)
EXPLORER_TX = os.getenv("EXPLORER_TX", "https://bscscan.com/tx/")
EXPLORER_ADDR = os.getenv("EXPLORER_ADDR", "https://bscscan.com/address/")
EXPLORER_TOKEN = os.getenv("EXPLORER_TOKEN", "https://bscscan.com/token/")
STATE_FILE = Path(os.getenv("STATE_FILE", ".bot_state.json"))
USER_LANG_FILE = Path(os.getenv("USER_LANG_FILE", ".user_lang.json"))
RUNTIME_CONFIG_FILE = Path(os.getenv("RUNTIME_CONFIG_FILE", ".runtime_config.json"))
DISTRIBUTORS_FILE = Path(os.getenv("DISTRIBUTORS_FILE", ".distributors.json"))
SUBSCRIBERS_FILE = Path(os.getenv("SUBSCRIBERS_FILE", ".subscribers.json"))

# Optional one-shot backfill on startup: scan this many blocks back from head
# to populate the distributor store so existing distributors are watched for
# TimeSet events. 0 disables (only NEW DistributorCreated events are tracked).
BACKFILL_BLOCKS = int(os.getenv("BACKFILL_BLOCKS", "0"))

# Cap on addresses sent in a single eth_getLogs call (free RPCs choke on big
# address arrays). The store is chunked into batches of this size.
LOGS_ADDRESS_CHUNK = int(os.getenv("LOGS_ADDRESS_CHUNK", "100"))

DEFAULT_LANG = os.getenv("DEFAULT_LANG", "zh").strip().lower()
if DEFAULT_LANG not in LANGS:
    log.warning("DEFAULT_LANG=%r is not supported, falling back to 'zh'", DEFAULT_LANG)
    DEFAULT_LANG = "zh"


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
TIMESET_TOPIC = (
    "0xc9b314c8a07c5f83e76af625ee63e74d2ec57a51f82a471792a9799bda395e40"
)

ERC20_ABI = json.loads(
    """[
    {"constant":true,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
    {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}
    ]"""
)

# Minimal ABI used to ask an unknown distributor which token it serves.
DISTRIBUTOR_TOKEN_ABI = json.loads(
    """[
    {"inputs":[],"name":"token","outputs":[{"name":"","type":"address"}],"stateMutability":"view","type":"function"}
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
DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", re.IGNORECASE)
BOT_STARTED_AT = time.time()
LAST_BLOCK_LOCK = threading.Lock()
LAST_BLOCK_SEEN = {"value": 0}


# ---------------------------------------------------------------------------
# Runtime config (persisted, mutable at runtime via Telegram commands)
# ---------------------------------------------------------------------------


_config_lock = threading.Lock()
_runtime_config: dict[str, Any] = {"poll_interval": POLL_INTERVAL}


def _load_runtime_config() -> None:
    if not RUNTIME_CONFIG_FILE.exists():
        return
    try:
        data = json.loads(RUNTIME_CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load runtime config: %s", exc)
        return
    interval = data.get("poll_interval")
    if isinstance(interval, (int, float)):
        clamped = max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, float(interval)))
        with _config_lock:
            _runtime_config["poll_interval"] = clamped


def _save_runtime_config() -> None:
    try:
        RUNTIME_CONFIG_FILE.write_text(json.dumps(_runtime_config))
    except OSError as exc:
        log.warning("Could not persist runtime config: %s", exc)


def get_poll_interval() -> float:
    with _config_lock:
        return float(_runtime_config["poll_interval"])


def set_poll_interval(seconds: float) -> float:
    """Clamp and persist a new poll interval. Returns the value actually set."""
    value = max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, float(seconds)))
    with _config_lock:
        _runtime_config["poll_interval"] = value
        _save_runtime_config()
    log.info("Poll interval changed to %.1fs", value)
    return value


def parse_duration(raw: str) -> float | None:
    """Parse '30', '30s', '3m', '1h' into seconds. None on failure."""
    m = DURATION_RE.match(raw)
    if not m:
        return None
    n = float(m.group(1))
    suffix = (m.group(2) or "s").lower()
    if suffix == "s":
        return n
    if suffix == "m":
        return n * 60
    if suffix == "h":
        return n * 3600
    return None


def format_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s % 3600 == 0 and s >= 3600:
        return f"{s // 3600}h"
    if s % 60 == 0 and s >= 60:
        return f"{s // 60}m"
    return f"{s}s"


# ---------------------------------------------------------------------------
# Per-user language store
# ---------------------------------------------------------------------------


_user_lang: dict[int, str] = {}
_user_lang_lock = threading.Lock()


def _load_user_lang() -> None:
    if not USER_LANG_FILE.exists():
        return
    try:
        data = json.loads(USER_LANG_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load user lang file: %s", exc)
        return
    for k, v in data.items():
        try:
            uid = int(k)
        except ValueError:
            continue
        if v in LANGS:
            _user_lang[uid] = v


def _save_user_lang() -> None:
    try:
        USER_LANG_FILE.write_text(
            json.dumps({str(k): v for k, v in _user_lang.items()})
        )
    except OSError as exc:
        log.warning("Could not persist user lang: %s", exc)


def get_user_lang(user_id: int | None) -> str:
    if user_id is None:
        return DEFAULT_LANG
    with _user_lang_lock:
        return _user_lang.get(user_id, DEFAULT_LANG)


def set_user_lang(user_id: int, lang: str) -> None:
    if lang not in LANGS:
        return
    with _user_lang_lock:
        _user_lang[user_id] = lang
        _save_user_lang()


# ---------------------------------------------------------------------------
# Distributor store: addr -> {token, owner, operator, block, tx}
# Built from DistributorCreated events (live + optional backfill). Used to
# watch the right addresses for TimeSet events and to look up which token
# a TimeSet belongs to.
# ---------------------------------------------------------------------------


_distributors: dict[str, dict[str, Any]] = {}
_distributors_lock = threading.Lock()


def _load_distributors() -> None:
    if not DISTRIBUTORS_FILE.exists():
        return
    try:
        data = json.loads(DISTRIBUTORS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load distributors file: %s", exc)
        return
    if not isinstance(data, dict):
        return
    for addr, info in data.items():
        if not isinstance(info, dict):
            continue
        _distributors[addr.lower()] = {
            "token": info.get("token", ""),
            "owner": info.get("owner", ""),
            "operator": info.get("operator", ""),
            "block": int(info.get("block", 0)),
            "tx": info.get("tx", ""),
        }


def _save_distributors() -> None:
    try:
        DISTRIBUTORS_FILE.write_text(json.dumps(_distributors))
    except OSError as exc:
        log.warning("Could not persist distributors: %s", exc)


def add_distributor(address: str, info: dict[str, Any]) -> bool:
    """Add to the store. Returns True if newly added."""
    key = address.lower()
    with _distributors_lock:
        if key in _distributors:
            return False
        _distributors[key] = info
        _save_distributors()
    return True


def get_distributor(address: str) -> dict[str, Any] | None:
    with _distributors_lock:
        return _distributors.get(address.lower())


def list_distributor_addresses() -> list[str]:
    with _distributors_lock:
        # Return checksummed addresses for eth_getLogs
        return [Web3.to_checksum_address(a) for a in _distributors.keys()]


def _chunked(seq: list[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# Subscribers: extra chat_ids that receive broadcast alerts on top of
# TELEGRAM_CHAT_ID. Whitelisted users opt a chat in via /activate.
# ---------------------------------------------------------------------------


_subscribers: set[int] = set()
_subscribers_lock = threading.Lock()


def _load_subscribers() -> None:
    if not SUBSCRIBERS_FILE.exists():
        return
    try:
        data = json.loads(SUBSCRIBERS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not load subscribers: %s", exc)
        return
    if not isinstance(data, list):
        return
    for x in data:
        try:
            _subscribers.add(int(x))
        except (ValueError, TypeError):
            continue


def _save_subscribers() -> None:
    try:
        SUBSCRIBERS_FILE.write_text(json.dumps(sorted(_subscribers)))
    except OSError as exc:
        log.warning("Could not persist subscribers: %s", exc)


def add_subscriber(chat_id: int) -> bool:
    with _subscribers_lock:
        if chat_id in _subscribers:
            return False
        _subscribers.add(chat_id)
        _save_subscribers()
    return True


def remove_subscriber(chat_id: int) -> bool:
    with _subscribers_lock:
        if chat_id not in _subscribers:
            return False
        _subscribers.discard(chat_id)
        _save_subscribers()
    return True


def list_subscribers() -> list[int]:
    with _subscribers_lock:
        return sorted(_subscribers)


# ---------------------------------------------------------------------------
# Misc helpers
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
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    s = str(value).lower()
    return s if s.startswith("0x") else "0x" + s


def format_block_time(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def format_threshold(value: Decimal) -> str:
    """Render the min-amount threshold trimmed of trailing zeros."""
    if value == value.to_integral():
        return f"{int(value):,}"
    return f"{value.normalize():,f}"


def format_uptime(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# ---------------------------------------------------------------------------
# Telegram primitives
# ---------------------------------------------------------------------------


def _telegram_base() -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def telegram_send(
    chat_id: str | int,
    text: str,
    *,
    reply_to: int | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{_telegram_base()}/sendMessage", json=payload, timeout=15)
        if r.status_code != 200:
            log.error("Telegram error %s: %s", r.status_code, r.text)
    except requests.RequestException as exc:
        log.error("Telegram request failed: %s", exc)


def telegram_edit(
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{_telegram_base()}/editMessageText", json=payload, timeout=15)
        if r.status_code != 200:
            log.error("Telegram edit error %s: %s", r.status_code, r.text)
    except requests.RequestException as exc:
        log.error("Telegram edit request failed: %s", exc)


def telegram_answer_callback(callback_id: str, text: str = "") -> None:
    try:
        requests.post(
            f"{_telegram_base()}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text},
            timeout=15,
        )
    except requests.RequestException as exc:
        log.warning("answerCallbackQuery failed: %s", exc)


def telegram_set_my_commands() -> None:
    """Register the / popup command list (bilingual descriptions)."""
    commands = [
        {"command": "menu", "description": "菜单 / Menu"},
        {"command": "check", "description": "检查交易 / Check tx"},
        {"command": "activate", "description": "激活推送 / Activate alerts here"},
        {"command": "deactivate", "description": "停用推送 / Deactivate alerts here"},
        {"command": "subs", "description": "查看订阅 / List subscribers"},
        {"command": "interval", "description": "查询频率 / Poll interval"},
        {"command": "status", "description": "状态 / Status"},
        {"command": "lang", "description": "切换语言 / Switch language"},
        {"command": "help", "description": "帮助 / Help"},
        {"command": "id", "description": "我的 ID / My IDs"},
    ]
    try:
        r = requests.post(
            f"{_telegram_base()}/setMyCommands",
            json={"commands": commands},
            timeout=15,
        )
        if r.status_code != 200:
            log.warning("setMyCommands failed: %s", r.text)
    except requests.RequestException as exc:
        log.warning("setMyCommands request failed: %s", exc)


def broadcast_alert(text: str) -> None:
    targets: list[Any] = []
    seen: set[str] = set()

    def _add(target: Any) -> None:
        key = str(target).strip()
        if not key or key in seen:
            return
        seen.add(key)
        targets.append(target)

    if TELEGRAM_CHAT_ID:
        _add(TELEGRAM_CHAT_ID)
    for sub in list_subscribers():
        _add(sub)

    if not targets:
        log.warning(
            "Broadcast suppressed: no TELEGRAM_CHAT_ID and no subscribers."
        )
        return
    for chat_id in targets:
        telegram_send(chat_id, text)


# ---------------------------------------------------------------------------
# Inline keyboards
# ---------------------------------------------------------------------------


def main_menu_keyboard(lang: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": t(lang, "btn_status"), "callback_data": "status"},
                {"text": t(lang, "btn_help"), "callback_data": "help"},
            ],
            [
                {"text": t(lang, "btn_check"), "callback_data": "check_hint"},
                {"text": t(lang, "btn_interval"), "callback_data": "interval_menu"},
            ],
            [
                {"text": t(lang, "btn_lang"), "callback_data": "lang_menu"},
                {"text": t(lang, "btn_close"), "callback_data": "close"},
            ],
        ]
    }


def lang_menu_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": LANG_LABEL["zh"], "callback_data": "set_lang:zh"},
                {"text": LANG_LABEL["en"], "callback_data": "set_lang:en"},
            ],
            [{"text": "⬅️", "callback_data": "menu"}],
        ]
    }


INTERVAL_PRESETS = (30, 60, 180, 300, 900, 1800, 3600)  # 30s,1m,3m,5m,15m,30m,1h


def interval_menu_keyboard() -> dict[str, Any]:
    row1 = [
        {"text": format_duration(s), "callback_data": f"set_interval:{s}"}
        for s in INTERVAL_PRESETS[:4]
    ]
    row2 = [
        {"text": format_duration(s), "callback_data": f"set_interval:{s}"}
        for s in INTERVAL_PRESETS[4:]
    ]
    return {
        "inline_keyboard": [
            row1,
            row2,
            [{"text": "⬅️", "callback_data": "menu"}],
        ]
    }


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
    lang: str,
    title_key: str,
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
        amount_str = t(lang, "no_funding")

    return (
        f"{t(lang, title_key)}\n"
        f"<b>{t(lang, 'field_token')}:</b> {name} ({symbol})\n"
        f"<b>{t(lang, 'field_token_contract')}:</b> "
        f"<a href=\"{EXPLORER_TOKEN}{token}\">{token}</a>\n"
        f"<b>{t(lang, 'field_amount')}:</b> {amount_str}\n"
        f"<b>{t(lang, 'field_distributor')}:</b> "
        f"<a href=\"{EXPLORER_ADDR}{distributor}\">{short_addr(distributor)}</a>\n"
        f"<b>{t(lang, 'field_owner')}:</b> "
        f"<a href=\"{EXPLORER_ADDR}{owner}\">{short_addr(owner)}</a>\n"
        f"<b>{t(lang, 'field_operator')}:</b> "
        f"<a href=\"{EXPLORER_ADDR}{operator}\">{short_addr(operator)}</a>\n"
        f"<b>{t(lang, 'field_block')}:</b> {block_number}\n"
        f"<b>{t(lang, 'field_tx')}:</b> "
        f"<a href=\"{EXPLORER_TX}{tx_hash}\">{short_addr(tx_hash)}</a>"
    )


def format_broadcast_alert(
    *,
    lang: str,
    token: str,
    amount_raw: int | None,
    meta: dict[str, Any],
    tx_hash: str,
    block_time: str,
) -> str:
    name = html.escape(str(meta["name"]))
    symbol_raw = str(meta["symbol"])
    symbol = html.escape(symbol_raw)
    decimals = meta["decimals"]

    if amount_raw is not None:
        amount_str = f"{format_amount(amount_raw, decimals)} {symbol}"
    else:
        amount_str = t(lang, "no_funding")

    return t(
        lang, "broadcast_alert",
        token_name=name,
        token_symbol=symbol,
        token_contract=token,
        amount=amount_str,
        tx_url=f"{EXPLORER_TX}{tx_hash}",
        time=block_time,
    )


def handle_event(w3: Web3, event: EventData) -> None:
    args = event["args"]
    tx_hash = _to_hex(event["transactionHash"])
    receipt = w3.eth.get_transaction_receipt(tx_hash)

    token = args["token"]
    distributor = args["distributorAddress"]
    amount_raw = find_funding_amount(receipt["logs"], token, distributor)
    meta = get_token_meta(w3, token)

    # Always remember the distributor → token mapping so we can correlate
    # later events (TimeSet, etc.) — even if this distributor is below the
    # broadcast threshold.
    add_distributor(distributor, {
        "token": token,
        "owner": args["owner"],
        "operator": args["operator"],
        "block": event["blockNumber"],
        "tx": tx_hash,
    })

    if amount_raw is None:
        amount_human = Decimal(0)
    else:
        amount_human = Decimal(amount_raw) / (Decimal(10) ** int(meta["decimals"]))

    if MIN_TOKEN_AMOUNT > 0 and amount_human < MIN_TOKEN_AMOUNT:
        log.info(
            "Filtered DistributorCreated: amount=%s %s < threshold=%s tx=%s",
            amount_human, meta["symbol"], MIN_TOKEN_AMOUNT, tx_hash,
        )
        return

    try:
        block_ts = int(w3.eth.get_block(event["blockNumber"])["timestamp"])
        block_time = format_block_time(block_ts)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch block timestamp: %s", exc)
        block_time = "?"

    msg = format_broadcast_alert(
        lang=DEFAULT_LANG,
        token=token,
        amount_raw=amount_raw,
        meta=meta,
        tx_hash=tx_hash,
        block_time=block_time,
    )
    log.info(
        "DistributorCreated token=%s distributor=%s amount=%s tx=%s",
        token, distributor, amount_human, tx_hash,
    )
    broadcast_alert(msg)


# ---------------------------------------------------------------------------
# TimeSet (claim window) handling
# ---------------------------------------------------------------------------


def decode_timeset(raw_log: LogReceipt) -> tuple[int, int] | None:
    """TimeSet has two non-indexed uint64 args concatenated in `data`."""
    data_hex = _to_hex(raw_log["data"])[2:]  # strip 0x
    if len(data_hex) < 128:
        return None
    try:
        start_time = int(data_hex[0:64], 16)
        end_time = int(data_hex[64:128], 16)
    except ValueError:
        return None
    return start_time, end_time


def lookup_distributor_token(w3: Web3, distributor: str) -> str | None:
    """Resolve a distributor's underlying token, store first then on-chain."""
    info = get_distributor(distributor)
    if info and info.get("token"):
        return info["token"]
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(distributor),
            abi=DISTRIBUTOR_TOKEN_ABI,
        )
        return contract.functions.token().call()
    except Exception as exc:  # noqa: BLE001
        log.debug("token() call failed for %s: %s", distributor, exc)
        return None


def format_timeset_alert(
    *,
    lang: str,
    template_key: str = "timeset_alert",
    token: str | None,
    meta: dict[str, Any],
    distributor: str,
    start_time: int,
    end_time: int,
    tx_hash: str,
) -> str:
    name = html.escape(str(meta.get("name", "?")))
    symbol = html.escape(str(meta.get("symbol", "?")))
    token_label = token or t(lang, "timeset_unknown_token")
    return t(
        lang, template_key,
        token_name=name,
        token_symbol=symbol,
        token_contract=token_label,
        distributor=distributor,
        start_time=format_block_time(start_time),
        end_time=format_block_time(end_time),
        tx_url=f"{EXPLORER_TX}{tx_hash}",
    )


def handle_timeset(w3: Web3, raw_log: LogReceipt) -> None:
    distributor = raw_log["address"]
    info = get_distributor(distributor)
    if info is None:
        log.warning(
            "TimeSet from unknown distributor %s — skip (not in store)",
            distributor,
        )
        return

    decoded = decode_timeset(raw_log)
    if decoded is None:
        log.warning("Could not decode TimeSet log from %s", distributor)
        return
    start_time, end_time = decoded
    tx_hash = _to_hex(raw_log["transactionHash"])
    token = info["token"]
    meta = get_token_meta(w3, token)

    msg = format_timeset_alert(
        lang=DEFAULT_LANG,
        token=token,
        meta=meta,
        distributor=distributor,
        start_time=start_time,
        end_time=end_time,
        tx_hash=tx_hash,
    )
    log.info(
        "TimeSet token=%s distributor=%s start=%d end=%d tx=%s",
        token, distributor, start_time, end_time, tx_hash,
    )
    broadcast_alert(msg)


# ---------------------------------------------------------------------------
# /check
# ---------------------------------------------------------------------------


def check_transaction(
    w3: Web3, factory_event_cls: Any, tx_hash: str, lang: str
) -> str:
    if not TX_HASH_RE.fullmatch(tx_hash):
        return t(lang, "check_invalid")

    try:
        chain_id: Any = w3.eth.chain_id
    except Exception:  # noqa: BLE001
        chain_id = "?"
    try:
        head_block: Any = w3.eth.block_number
    except Exception:  # noqa: BLE001
        head_block = "?"

    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except TransactionNotFound:
        return t(
            lang, "check_not_found",
            tx=tx_hash, chain=chain_id, head=head_block,
        )
    except Exception as exc:  # noqa: BLE001
        return t(lang, "rpc_error", err=_scrub_for_user(str(exc)))

    if receipt is None:
        return t(lang, "check_pending", tx=tx_hash)
    if receipt.get("status") != 1:
        return t(lang, "check_reverted", tx=tx_hash, url=f"{EXPLORER_TX}{tx_hash}")

    factory_lc = FACTORY_ADDRESS.lower()
    created_topic = DISTRIBUTOR_CREATED_TOPIC.lower()
    timeset_topic = TIMESET_TOPIC.lower()

    created_matches = []
    timeset_matches = []
    for raw in receipt["logs"]:
        topics = raw.get("topics") or []
        if not topics:
            continue
        topic0 = _to_hex(topics[0])
        if topic0 == created_topic and raw["address"].lower() == factory_lc:
            try:
                created_matches.append(factory_event_cls().process_log(raw))
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not decode DistributorCreated log: %s", exc)
        elif topic0 == timeset_topic:
            timeset_matches.append(raw)

    if not created_matches and not timeset_matches:
        return t(
            lang, "check_no_event",
            factory=FACTORY_ADDRESS,
            tx=tx_hash,
            url=f"{EXPLORER_TX}{tx_hash}",
        )

    parts: list[str] = []
    for ev in created_matches:
        args = ev["args"]
        parts.append(
            format_distributor_alert(
                w3,
                lang=lang,
                title_key="hit_title",
                owner=args["owner"],
                operator=args["operator"],
                token=args["token"],
                distributor=args["distributorAddress"],
                block_number=ev["blockNumber"],
                tx_hash=tx_hash,
                receipt_logs=receipt["logs"],
            )
        )
    for raw in timeset_matches:
        decoded = decode_timeset(raw)
        if decoded is None:
            continue
        start_time, end_time = decoded
        distributor = raw["address"]
        token = lookup_distributor_token(w3, distributor)
        meta = (
            get_token_meta(w3, token)
            if token
            else {"name": "?", "symbol": "?", "decimals": 18}
        )
        parts.append(
            format_timeset_alert(
                lang=lang,
                template_key="timeset_hit",
                token=token,
                meta=meta,
                distributor=distributor,
                start_time=start_time,
                end_time=end_time,
                tx_hash=tx_hash,
            )
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Status text
# ---------------------------------------------------------------------------


def build_status_text(w3: Web3, lang: str, user_id: int) -> str:
    try:
        head_block = str(w3.eth.block_number)
    except Exception as exc:  # noqa: BLE001
        head_block = f"err: {exc}"
    try:
        chain_id = str(w3.eth.chain_id)
    except Exception:  # noqa: BLE001
        chain_id = "?"
    with LAST_BLOCK_LOCK:
        seen = LAST_BLOCK_SEEN["value"]
    uptime = format_uptime(int(time.time() - BOT_STARTED_AT))
    user_lang_label = LANG_LABEL.get(get_user_lang(user_id), get_user_lang(user_id))

    interval = get_poll_interval()
    threshold = (
        format_threshold(MIN_TOKEN_AMOUNT)
        if MIN_TOKEN_AMOUNT > 0
        else t(lang, "status_open")
    )
    return (
        f"{t(lang, 'status_title')}\n"
        f"<b>{t(lang, 'status_factory')}:</b> <code>{FACTORY_ADDRESS}</code>\n"
        f"<b>{t(lang, 'status_chain')}:</b> <code>{chain_id}</code>\n"
        f"<b>{t(lang, 'status_head')}:</b> <code>{head_block}</code>\n"
        f"<b>{t(lang, 'status_last_processed')}:</b> <code>{seen}</code>\n"
        f"<b>{t(lang, 'status_uptime')}:</b> {uptime}\n"
        f"<b>{t(lang, 'status_interval')}:</b> {format_duration(interval)}\n"
        f"<b>{t(lang, 'status_min_amount')}:</b> {threshold}\n"
        f"<b>{t(lang, 'status_whitelist')}:</b> "
        f"{len(WHITELIST) if WHITELIST else t(lang, 'status_open')}\n"
        f"<b>{t(lang, 'status_lang')}:</b> {user_lang_label}"
    )


def build_id_text(lang: str, user_id: int, chat_id: int) -> str:
    return (
        f"{t(lang, 'id_title')}\n"
        f"<b>{t(lang, 'id_label')}:</b> <code>{user_id}</code>\n"
        f"<b>{t(lang, 'chat_label')}:</b> <code>{chat_id}</code>"
    )


def build_interval_text(lang: str) -> str:
    current = get_poll_interval()
    return t(
        lang, "interval_current",
        seconds=int(current),
        pretty=format_duration(current),
        min=format_duration(MIN_POLL_INTERVAL),
        max=format_duration(MAX_POLL_INTERVAL),
    )


# ---------------------------------------------------------------------------
# Telegram dispatch
# ---------------------------------------------------------------------------


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

    lang = get_user_lang(user_id)
    head, _, rest = text.partition(" ")
    cmd = head.split("@", 1)[0].lower()
    arg = rest.strip()

    if cmd in ("/start", "/help"):
        telegram_send(chat_id, t(lang, "help"), reply_to=msg_id)
        return

    if cmd == "/menu":
        telegram_send(
            chat_id,
            t(lang, "menu_title"),
            reply_to=msg_id,
            reply_markup=main_menu_keyboard(lang),
        )
        return

    if cmd == "/lang":
        telegram_send(
            chat_id,
            t(lang, "lang_choose"),
            reply_to=msg_id,
            reply_markup=lang_menu_keyboard(),
        )
        return

    if cmd == "/id":
        telegram_send(chat_id, build_id_text(lang, user_id, chat_id), reply_to=msg_id)
        return

    if cmd == "/status":
        telegram_send(chat_id, build_status_text(w3, lang, user_id), reply_to=msg_id)
        return

    if cmd == "/activate":
        target = chat_id
        if arg:
            try:
                target = int(arg.split()[0])
            except ValueError:
                telegram_send(chat_id, t(lang, "activate_invalid"), reply_to=msg_id)
                return
        if add_subscriber(target):
            telegram_send(
                chat_id, t(lang, "activate_success", chat=target), reply_to=msg_id,
            )
        else:
            telegram_send(
                chat_id, t(lang, "activate_already", chat=target), reply_to=msg_id,
            )
        return

    if cmd == "/deactivate":
        target = chat_id
        if arg:
            try:
                target = int(arg.split()[0])
            except ValueError:
                telegram_send(chat_id, t(lang, "activate_invalid"), reply_to=msg_id)
                return
        if remove_subscriber(target):
            telegram_send(
                chat_id, t(lang, "deactivate_success", chat=target), reply_to=msg_id,
            )
        else:
            telegram_send(
                chat_id, t(lang, "deactivate_not_active", chat=target), reply_to=msg_id,
            )
        return

    if cmd == "/subs":
        subs = list_subscribers()
        lines = [t(lang, "subs_title")]
        if TELEGRAM_CHAT_ID:
            lines.append(t(lang, "subs_default", chat=TELEGRAM_CHAT_ID))
        if subs:
            lines.append(t(lang, "subs_extra"))
            for s in subs:
                lines.append(f"  • <code>{s}</code>")
        else:
            lines.append(t(lang, "subs_empty"))
        telegram_send(chat_id, "\n".join(lines), reply_to=msg_id)
        return

    if cmd == "/check":
        if not arg:
            telegram_send(chat_id, t(lang, "check_usage"), reply_to=msg_id)
            return
        m = TX_HASH_RE.search(arg)
        target = m.group(0) if m else arg
        result = check_transaction(w3, factory_event_cls, target.lower(), lang)
        telegram_send(chat_id, result, reply_to=msg_id)
        return

    if cmd == "/interval":
        if not arg:
            telegram_send(
                chat_id, build_interval_text(lang),
                reply_to=msg_id, reply_markup=interval_menu_keyboard(),
            )
            return
        secs = parse_duration(arg)
        if secs is None:
            telegram_send(
                chat_id,
                t(lang, "interval_invalid",
                  min=format_duration(MIN_POLL_INTERVAL),
                  max=format_duration(MAX_POLL_INTERVAL)),
                reply_to=msg_id,
            )
            return
        if secs < MIN_POLL_INTERVAL or secs > MAX_POLL_INTERVAL:
            telegram_send(
                chat_id,
                t(lang, "interval_out_of_range",
                  min=format_duration(MIN_POLL_INTERVAL),
                  max=format_duration(MAX_POLL_INTERVAL)),
                reply_to=msg_id,
            )
            return
        applied = set_poll_interval(secs)
        telegram_send(
            chat_id,
            t(lang, "interval_set",
              seconds=int(applied), pretty=format_duration(applied)),
            reply_to=msg_id,
        )
        return

    if cmd.startswith("/"):
        telegram_send(chat_id, t(lang, "unknown_cmd"), reply_to=msg_id)
        return

    # Not a command — accept a bare tx hash (or any message containing one)
    # and run /check on the first match.
    m = TX_HASH_RE.search(text)
    if m:
        result = check_transaction(w3, factory_event_cls, m.group(0).lower(), lang)
        telegram_send(chat_id, result, reply_to=msg_id)


def handle_callback_query(w3: Web3, callback: dict[str, Any]) -> None:
    callback_id = callback.get("id")
    user = callback.get("from") or {}
    user_id = user.get("id")
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    message_id = message.get("message_id")

    if not callback_id:
        return
    if user_id is None or not is_whitelisted(user_id):
        telegram_answer_callback(callback_id, "Forbidden")
        return
    if chat_id is None or message_id is None:
        telegram_answer_callback(callback_id)
        return

    lang = get_user_lang(user_id)

    if data == "menu":
        telegram_answer_callback(callback_id)
        telegram_edit(
            chat_id, message_id, t(lang, "menu_title"),
            reply_markup=main_menu_keyboard(lang),
        )
        return

    if data == "help":
        telegram_answer_callback(callback_id)
        telegram_edit(
            chat_id, message_id, t(lang, "help"),
            reply_markup=main_menu_keyboard(lang),
        )
        return

    if data == "status":
        telegram_answer_callback(callback_id)
        telegram_edit(
            chat_id, message_id, build_status_text(w3, lang, user_id),
            reply_markup=main_menu_keyboard(lang),
        )
        return

    if data == "check_hint":
        telegram_answer_callback(callback_id)
        telegram_edit(
            chat_id, message_id, t(lang, "menu_check_hint"),
            reply_markup=main_menu_keyboard(lang),
        )
        return

    if data == "lang_menu":
        telegram_answer_callback(callback_id)
        telegram_edit(
            chat_id, message_id, t(lang, "lang_choose"),
            reply_markup=lang_menu_keyboard(),
        )
        return

    if data == "interval_menu":
        telegram_answer_callback(callback_id)
        telegram_edit(
            chat_id, message_id, build_interval_text(lang),
            reply_markup=interval_menu_keyboard(),
        )
        return

    if data.startswith("set_interval:"):
        try:
            secs = float(data.split(":", 1)[1])
        except ValueError:
            telegram_answer_callback(callback_id)
            return
        applied = set_poll_interval(secs)
        telegram_answer_callback(
            callback_id,
            t(lang, "interval_set",
              seconds=int(applied), pretty=format_duration(applied)),
        )
        telegram_edit(
            chat_id, message_id, build_interval_text(lang),
            reply_markup=interval_menu_keyboard(),
        )
        return

    if data.startswith("set_lang:"):
        new_lang = data.split(":", 1)[1]
        if new_lang in LANGS:
            set_user_lang(user_id, new_lang)
        telegram_answer_callback(callback_id, t(new_lang, "lang_set"))
        telegram_edit(
            chat_id, message_id, t(new_lang, "menu_title"),
            reply_markup=main_menu_keyboard(new_lang),
        )
        return

    if data == "close":
        telegram_answer_callback(callback_id)
        try:
            requests.post(
                f"{_telegram_base()}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
                timeout=15,
            )
        except requests.RequestException as exc:
            log.warning("deleteMessage failed: %s", exc)
        return

    telegram_answer_callback(callback_id)


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


def _record_distributor_only(event: EventData) -> None:
    """Add a distributor to the store without broadcasting (used by backfill)."""
    args = event["args"]
    add_distributor(args["distributorAddress"], {
        "token": args["token"],
        "owner": args["owner"],
        "operator": args["operator"],
        "block": event["blockNumber"],
        "tx": _to_hex(event["transactionHash"]),
    })


def backfill_distributors(
    w3: Web3, factory_event_cls: Any, head_block: int, blocks_back: int,
) -> None:
    if blocks_back <= 0:
        return
    start = max(0, head_block - blocks_back)
    log.info(
        "Backfilling distributors from block %s to %s (range=%d)",
        start, head_block, blocks_back,
    )
    cursor = start
    added_before = len(_distributors)
    while cursor <= head_block:
        end = min(head_block, cursor + MAX_BLOCK_RANGE - 1)
        try:
            logs = w3.eth.get_logs({
                "fromBlock": cursor,
                "toBlock": end,
                "address": FACTORY_ADDRESS,
                "topics": [DISTRIBUTOR_CREATED_TOPIC],
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("Backfill chunk %s-%s failed: %s", cursor, end, exc)
            cursor = end + 1
            continue
        for raw in logs:
            try:
                event = factory_event_cls().process_log(raw)
            except Exception as exc:  # noqa: BLE001
                log.warning("Backfill: failed to decode log: %s", exc)
                continue
            _record_distributor_only(event)
        cursor = end + 1
    added = len(_distributors) - added_before
    log.info("Backfill complete: %d new distributors (total %d)", added, len(_distributors))


def _poll_timeset(w3: Web3, from_block: int, to_block: int) -> None:
    addresses = list_distributor_addresses()
    if not addresses:
        return
    target_topic = TIMESET_TOPIC.lower()
    for chunk in _chunked(addresses, LOGS_ADDRESS_CHUNK):
        try:
            logs = w3.eth.get_logs({
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": chunk,
                "topics": [TIMESET_TOPIC],
            })
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "TimeSet getLogs failed (chunk size %d, blocks %s-%s): %s",
                len(chunk), from_block, to_block, exc,
            )
            continue
        for raw in logs:
            topics = raw.get("topics") or []
            if not topics or _to_hex(topics[0]) != target_topic:
                continue
            try:
                handle_timeset(w3, raw)
            except Exception as exc:  # noqa: BLE001
                log.exception("handle_timeset error: %s", exc)


def chain_monitor(w3: Web3, factory_event_cls: Any) -> None:
    head = w3.eth.block_number
    last_block = load_last_block(default=max(0, head - BLOCK_LOOKBACK))
    with LAST_BLOCK_LOCK:
        LAST_BLOCK_SEEN["value"] = last_block
    log.info(
        "Watching factory %s on chain id %s, starting from block %s (head=%s)",
        FACTORY_ADDRESS, w3.eth.chain_id, last_block, head,
    )

    if BACKFILL_BLOCKS > 0:
        try:
            backfill_distributors(w3, factory_event_cls, head, BACKFILL_BLOCKS)
        except Exception as exc:  # noqa: BLE001
            log.exception("Backfill failed: %s", exc)

    log.info("Tracking %d distributors at startup", len(_distributors))

    while True:
        interval = get_poll_interval()
        try:
            head = w3.eth.block_number
            if head <= last_block:
                time.sleep(interval)
                continue

            from_block = last_block + 1
            to_block = min(head, from_block + MAX_BLOCK_RANGE - 1)

            # 1. DistributorCreated from the factory
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

            # 2. TimeSet from any known distributor (incl. ones just added above)
            _poll_timeset(w3, from_block, to_block)

            last_block = to_block
            save_last_block(last_block)
            with LAST_BLOCK_LOCK:
                LAST_BLOCK_SEEN["value"] = last_block
        except BlockNotFound:
            time.sleep(interval)
        except KeyboardInterrupt:
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Chain monitor error: %s", exc)
            time.sleep(interval * 2)
        else:
            time.sleep(interval)


def telegram_listener(w3: Web3, factory_event_cls: Any) -> None:
    offset: int | None = None
    log.info(
        "Telegram listener started (whitelist=%s, default_lang=%s)",
        sorted(WHITELIST) if WHITELIST else "OPEN — accepting all users",
        DEFAULT_LANG,
    )
    while True:
        try:
            params: dict[str, Any] = {
                "timeout": 30,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            }
            if offset is not None:
                params["offset"] = offset
            r = requests.get(
                f"{_telegram_base()}/getUpdates",
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
                try:
                    if "message" in upd:
                        handle_command(w3, factory_event_cls, upd["message"])
                    elif "callback_query" in upd:
                        handle_callback_query(w3, upd["callback_query"])
                except Exception as exc:  # noqa: BLE001
                    log.exception("Update handler error: %s", exc)
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
    _load_user_lang()
    _load_runtime_config()
    _load_distributors()
    _load_subscribers()

    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        log.error("Cannot reach RPC at %s", _redact(RPC_URL))
        sys.exit(1)
    log.info("Connected to RPC: %s", _redact(RPC_URL))

    factory = w3.eth.contract(address=FACTORY_ADDRESS, abi=[DISTRIBUTOR_CREATED_ABI])
    factory_event_cls = factory.events.DistributorCreated

    telegram_set_my_commands()

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
    for th in threads:
        th.start()

    try:
        while True:
            for th in threads:
                if not th.is_alive():
                    log.error("Worker %s died, exiting so the platform can restart us.", th.name)
                    sys.exit(1)
            time.sleep(10)
    except KeyboardInterrupt:
        log.info("Bye.")


if __name__ == "__main__":
    main()
