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
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))
BLOCK_LOOKBACK = int(os.getenv("BLOCK_LOOKBACK", "20"))
MAX_BLOCK_RANGE = int(os.getenv("MAX_BLOCK_RANGE", "1000"))
EXPLORER_TX = os.getenv("EXPLORER_TX", "https://bscscan.com/tx/")
EXPLORER_ADDR = os.getenv("EXPLORER_ADDR", "https://bscscan.com/address/")
EXPLORER_TOKEN = os.getenv("EXPLORER_TOKEN", "https://bscscan.com/token/")
STATE_FILE = Path(os.getenv("STATE_FILE", ".bot_state.json"))
USER_LANG_FILE = Path(os.getenv("USER_LANG_FILE", ".user_lang.json"))

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
    telegram_send(TELEGRAM_CHAT_ID, text)


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
                {"text": t(lang, "btn_lang"), "callback_data": "lang_menu"},
            ],
            [
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


def handle_event(w3: Web3, event: EventData) -> None:
    args = event["args"]
    tx_hash = _to_hex(event["transactionHash"])
    receipt = w3.eth.get_transaction_receipt(tx_hash)

    msg = format_distributor_alert(
        w3,
        lang=DEFAULT_LANG,
        title_key="alert_title",
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
        return t(
            lang,
            "check_no_event",
            factory=FACTORY_ADDRESS,
            tx=tx_hash,
            url=f"{EXPLORER_TX}{tx_hash}",
        )

    parts = []
    for ev in matches:
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

    return (
        f"{t(lang, 'status_title')}\n"
        f"<b>{t(lang, 'status_factory')}:</b> <code>{FACTORY_ADDRESS}</code>\n"
        f"<b>{t(lang, 'status_chain')}:</b> <code>{chain_id}</code>\n"
        f"<b>{t(lang, 'status_head')}:</b> <code>{head_block}</code>\n"
        f"<b>{t(lang, 'status_last_processed')}:</b> <code>{seen}</code>\n"
        f"<b>{t(lang, 'status_uptime')}:</b> {uptime}\n"
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

    if cmd == "/check":
        if not arg:
            telegram_send(chat_id, t(lang, "check_usage"), reply_to=msg_id)
            return
        m = TX_HASH_RE.search(arg)
        target = m.group(0) if m else arg
        result = check_transaction(w3, factory_event_cls, target.lower(), lang)
        telegram_send(chat_id, result, reply_to=msg_id)
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
        except Exception as exc:  # noqa: BLE001
            log.exception("Chain monitor error: %s", exc)
            time.sleep(POLL_INTERVAL * 2)
        else:
            time.sleep(POLL_INTERVAL)


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
