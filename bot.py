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
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

DEFAULT_FACTORY = "0x000310fa98E36191ec79de241d72C6CA093EAfD3"
DEFAULT_BSC_RPC = "https://bsc-dataseed.bnbchain.org"
DEFAULT_BSC_EXPLORERS = {
    "tx": "https://bscscan.com/tx/",
    "addr": "https://bscscan.com/address/",
    "token": "https://bscscan.com/token/",
}
DEFAULT_DISPLAY_NAMES = {
    "bsc": "BSC",
    "eth": "Ethereum",
    "arb": "Arbitrum",
    "base": "Base",
    "polygon": "Polygon",
    "op": "Optimism",
}

_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_REDACT_KEYS: list[str] = []


def _register_redaction(secret: str) -> None:
    if secret and secret not in _REDACT_KEYS:
        _REDACT_KEYS.append(secret)


def _redact(text: str) -> str:
    out = text
    for secret in _REDACT_KEYS:
        out = out.replace(secret, "***")
    return out


def _scrub_for_user(text: str) -> str:
    """Scrub RPC URLs / keys from user-facing error messages.

    `requests` exceptions like ``403 Client Error: Forbidden for url:
    https://rpc.example.com/<KEY>`` would otherwise leak the endpoint
    and credential into a Telegram reply, which can be screenshotted.
    """
    return _URL_RE.sub("<rpc>", _redact(text))


def _clean_env_value(raw: str) -> str:
    """Strip whitespace and accidental angle-bracket wrapping.

    Some Railway / dashboard paste flows end up with values like
    ``<https://rpc.ankr.com/...>`` instead of the bare URL, which makes
    `requests` reject the URL with "No connection adapters were found
    for '<https...>'". Tolerate the wrapping so the bot still boots.
    """
    s = raw.strip()
    while s.startswith("<") and s.endswith(">") and len(s) >= 2:
        s = s[1:-1].strip()
    return s


def _chain_env(
    key: str,
    suffix: str,
    *,
    legacy_fallback: bool = False,
    default: str = "",
) -> str:
    """Read `<KEY>_<SUFFIX>`, then optionally fall back to bare `<SUFFIX>`."""
    val = _clean_env_value(os.getenv(f"{key.upper()}_{suffix}", ""))
    if val:
        return val
    if legacy_fallback:
        legacy = _clean_env_value(os.getenv(suffix, ""))
        if legacy:
            return legacy
    return default


@dataclass(frozen=True)
class ChainCtx:
    key: str
    display_name: str
    w3: Web3
    factory_address: str
    factory_event_cls: Any
    explorer_tx: str
    explorer_addr: str
    explorer_token: str
    state_file: Path
    distributors_file: Path


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def _parse_chat_ids(raw: str) -> list[str]:
    """Comma-separated default broadcast targets. Each kept as a string so
    Telegram receives the exact id (negative ints for groups/channels)."""
    return [x.strip() for x in raw.split(",") if x.strip()]


DEFAULT_CHAT_IDS = _parse_chat_ids(TELEGRAM_CHAT_ID)
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
    os.getenv("MIN_TOKEN_AMOUNT", "30000"), "30000"
)
USER_LANG_FILE = Path(os.getenv("USER_LANG_FILE", ".user_lang.json"))
RUNTIME_CONFIG_FILE = Path(os.getenv("RUNTIME_CONFIG_FILE", ".runtime_config.json"))
SUBSCRIBERS_FILE = Path(os.getenv("SUBSCRIBERS_FILE", ".subscribers.json"))

# Per-chain state files use this dir + the chain key.
STATE_DIR = Path(os.getenv("STATE_DIR", ".")).resolve()

# Two promo cards appended to every broadcast as inline buttons. Override
# (or blank out) any of them via env vars.
FOOTER_BTN1_TEXT = os.getenv("FOOTER_BTN1_TEXT", "Okx钱包 45%返佣开通联系@xiaoc888")
FOOTER_BTN1_URL = os.getenv("FOOTER_BTN1_URL", "https://t.me/xiaoc888")
FOOTER_BTN2_TEXT = os.getenv("FOOTER_BTN2_TEXT", "Boost数据看板")
FOOTER_BTN2_URL = os.getenv("FOOTER_BTN2_URL", "https://dune.com/0xxiaoc/okx-dex-boost")

# Optional one-shot backfill on startup: scan this many blocks back from head
# to populate the distributor store so existing distributors are watched for
# TimeSet events. 0 disables (only NEW DistributorCreated events are tracked).
BACKFILL_BLOCKS = int(os.getenv("BACKFILL_BLOCKS", "0"))

# When a TimeSet event arrives from a distributor we've never seen (bot was
# down, or it was created before BACKFILL_BLOCKS), scan the factory's
# DistributorCreated history this many blocks backward from the TimeSet
# block to recover the original token/owner/operator/funding tx. Set to 0
# to disable the reverse trace (the distributor will still be persisted
# with just the token address from `distributor.token()`).
REVERSE_LOOKUP_BLOCKS = int(os.getenv("REVERSE_LOOKUP_BLOCKS", "200000"))

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
    "0xcf9068cf0507f6c18ee38fd73ba24a528f514f0e73ad08229b6db0541071d48d"
)
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
TIMESET_TOPIC = (
    "0xc9b314c8a07c5f83e76af625ee63e74d2ec57a51f82a471792a9799bda395e40"
)
WITHDRAWN_TOPIC = (
    "0x7084f5476618d8e60b11ef0d7d3f06914655adb8793e28ff7f018d4c76d505d5"
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
        {"indexed": False, "name": "initialTotalAmount", "type": "uint256"},
    ],
    "name": "DistributorCreated",
    "type": "event",
}

TX_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
CHAIN_KEY_RE = re.compile(r"^[a-z0-9_-]+$")
DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", re.IGNORECASE)
BOT_STARTED_AT = time.time()
LAST_BLOCK_LOCK = threading.Lock()
LAST_BLOCK_SEEN: dict[str, int] = {}
# chain_key -> True when a /skip command asked the monitor to fast-forward
# its last_block to the chain head, dropping any unscanned backlog. The
# monitor thread consumes (and clears) the flag at the top of its loop.
SKIP_REQUEST: dict[str, bool] = {}


def request_skip_to_head(chain_key: str) -> None:
    """Mark `chain_key` so its monitor loop jumps last_block to head."""
    with LAST_BLOCK_LOCK:
        SKIP_REQUEST[chain_key] = True


def consume_skip_request(chain_key: str) -> bool:
    """Return True (and clear the flag) if a skip was requested for `chain_key`."""
    with LAST_BLOCK_LOCK:
        return SKIP_REQUEST.pop(chain_key, False)


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
# Distributor store: chain_key -> addr_lc -> {token, owner, operator, block, tx}
# Built from DistributorCreated events (live + optional backfill). Used to
# watch the right addresses for TimeSet events and to look up which token
# a TimeSet belongs to. Persisted per-chain so a distributor address that
# happens to collide across chains stays cleanly separated.
# ---------------------------------------------------------------------------


_distributors: dict[str, dict[str, dict[str, Any]]] = {}
_distributors_lock = threading.Lock()

# Negative cache for the reverse lookup: addresses that emitted a TimeSet log
# but turned out not to expose `token()` (i.e. unrelated contracts with a
# colliding event signature). Cached per-chain so we don't pay an eth_call
# per poll cycle to re-confirm they're not distributors.
_non_distributor_cache: dict[str, set[str]] = {}
_non_distributor_lock = threading.Lock()


def _is_non_distributor(ctx_key: str, addr: str) -> bool:
    with _non_distributor_lock:
        bucket = _non_distributor_cache.get(ctx_key)
        return bucket is not None and addr.lower() in bucket


def _mark_non_distributor(ctx_key: str, addr: str) -> None:
    with _non_distributor_lock:
        _non_distributor_cache.setdefault(ctx_key, set()).add(addr.lower())


def _load_distributors_for(ctx: ChainCtx) -> None:
    """Load `ctx`'s distributors file into the in-memory store.

    Auto-migrates the legacy chain-less `.distributors.json` file into the
    chain-scoped path the first time the bot boots in multi-chain mode.
    """
    path = ctx.distributors_file
    legacy = Path(os.getenv("DISTRIBUTORS_FILE", ".distributors.json"))
    if not path.exists() and ctx.key == "bsc" and legacy.exists() and legacy != path:
        try:
            path.write_text(legacy.read_text())
            log.info("Migrated legacy distributors file %s -> %s", legacy, path)
        except OSError as exc:
            log.warning("Could not migrate legacy distributors file: %s", exc)

    bucket: dict[str, dict[str, Any]] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not load distributors file %s: %s", path, exc)
            data = {}
        if isinstance(data, dict):
            for addr, info in data.items():
                if not isinstance(info, dict):
                    continue
                raw_amount = info.get("amount_raw")
                raw_block = info.get("block")
                bucket[addr.lower()] = {
                    "token": info.get("token", ""),
                    "owner": info.get("owner", ""),
                    "operator": info.get("operator", ""),
                    "block": int(raw_block) if raw_block is not None else 0,
                    "tx": info.get("tx", ""),
                    "amount_raw": int(raw_amount) if raw_amount is not None else None,
                }
    with _distributors_lock:
        _distributors[ctx.key] = bucket


def _save_distributors_for(ctx: ChainCtx) -> None:
    try:
        ctx.distributors_file.write_text(json.dumps(_distributors.get(ctx.key, {})))
    except OSError as exc:
        log.warning("Could not persist distributors for %s: %s", ctx.key, exc)


def add_distributor(ctx: ChainCtx, address: str, info: dict[str, Any]) -> bool:
    """Add to the per-chain store. Returns True if newly added."""
    key = address.lower()
    with _distributors_lock:
        bucket = _distributors.setdefault(ctx.key, {})
        if key in bucket:
            return False
        bucket[key] = info
    _save_distributors_for(ctx)
    return True


def update_distributor(ctx: ChainCtx, address: str, extras: dict[str, Any]) -> None:
    """Merge new fields into an existing stored distributor and persist."""
    key = address.lower()
    with _distributors_lock:
        rec = _distributors.get(ctx.key, {}).get(key)
        if rec is None:
            return
        rec.update(extras)
    _save_distributors_for(ctx)


def get_distributor(ctx: ChainCtx, address: str) -> dict[str, Any] | None:
    with _distributors_lock:
        bucket = _distributors.get(ctx.key) or {}
        return bucket.get(address.lower())


def list_distributor_addresses(ctx: ChainCtx) -> list[str]:
    with _distributors_lock:
        bucket = _distributors.get(ctx.key) or {}
        return [Web3.to_checksum_address(a) for a in bucket.keys()]


def distributor_count(ctx: ChainCtx) -> int:
    with _distributors_lock:
        return len(_distributors.get(ctx.key) or {})


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
    if not TELEGRAM_TOKEN or not DEFAULT_CHAT_IDS:
        log.error(
            "Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID. Copy .env.example to "
            ".env and fill them in (or set them in Railway). TELEGRAM_CHAT_ID "
            "may be a single id or a comma-separated list."
        )
        sys.exit(1)


def load_last_block(ctx: ChainCtx, default: int) -> int:
    """Load the last processed block for `ctx`, migrating legacy file if needed."""
    path = ctx.state_file
    legacy = Path(os.getenv("STATE_FILE", ".bot_state.json"))
    if not path.exists() and ctx.key == "bsc" and legacy.exists() and legacy != path:
        try:
            path.write_text(legacy.read_text())
            log.info("Migrated legacy state file %s -> %s", legacy, path)
        except OSError as exc:
            log.warning("Could not migrate legacy state file: %s", exc)

    if path.exists():
        try:
            return int(json.loads(path.read_text())["last_block"])
        except (ValueError, KeyError, json.JSONDecodeError):
            log.warning("State file %s corrupt, ignoring.", path)
    return default


def save_last_block(ctx: ChainCtx, block: int) -> None:
    try:
        ctx.state_file.write_text(json.dumps({"last_block": block}))
    except OSError as exc:
        log.warning("Could not persist state to %s: %s", ctx.state_file, exc)


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


DISPLAY_TZ = timezone(timedelta(hours=8))


def format_block_time(unix_ts: int) -> str:
    return datetime.fromtimestamp(unix_ts, tz=DISPLAY_TZ).strftime(
        "%Y-%m-%d %H:%M:%S UTC+8"
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


_chat_title_cache: dict[int, tuple[str, float]] = {}
CHAT_TITLE_TTL = 3600  # 1h — names are stable; restart-or-TTL refreshes


def _fetch_chat_title(chat_id: int) -> str | None:
    try:
        r = requests.get(
            f"{_telegram_base()}/getChat",
            params={"chat_id": chat_id},
            timeout=15,
        )
    except requests.RequestException as exc:
        log.debug("getChat request failed for %s: %s", chat_id, exc)
        return None
    if r.status_code != 200:
        log.debug("getChat %s returned %s: %s", chat_id, r.status_code, r.text[:200])
        return None
    data = r.json()
    if not data.get("ok"):
        return None
    result = data.get("result") or {}
    title = result.get("title")
    if title:
        return title
    parts = []
    if result.get("first_name"):
        parts.append(result["first_name"])
    if result.get("last_name"):
        parts.append(result["last_name"])
    if parts:
        return " ".join(parts)
    if result.get("username"):
        return f"@{result['username']}"
    return None


def get_chat_title(chat_id: Any) -> str | None:
    try:
        cid = int(chat_id)
    except (ValueError, TypeError):
        return None
    cached = _chat_title_cache.get(cid)
    now = time.time()
    if cached and now - cached[1] < CHAT_TITLE_TTL:
        return cached[0]
    title = _fetch_chat_title(cid)
    if title:
        _chat_title_cache[cid] = (title, now)
        return title
    return cached[0] if cached else None


def format_chat_label(chat_id: Any) -> str:
    """Return 'Group Name (<code>id</code>)' or '<code>id</code>' fallback."""
    title = get_chat_title(chat_id)
    if title:
        return f"{html.escape(title)} (<code>{chat_id}</code>)"
    return f"<code>{chat_id}</code>"


def telegram_set_my_commands() -> None:
    """Register the / popup command list (bilingual descriptions)."""
    commands = [
        {"command": "menu", "description": "菜单 / Menu"},
        {"command": "check", "description": "检查交易 / Check tx"},
        {"command": "activate", "description": "激活推送 / Activate alerts here"},
        {"command": "deactivate", "description": "停用推送 / Deactivate alerts here"},
        {"command": "subs", "description": "查看订阅 / List subscribers"},
        {"command": "preview", "description": "预览测试 / Preview alert"},
        {"command": "interval", "description": "查询频率 / Poll interval"},
        {"command": "skip", "description": "跳到最新区块 / Skip to latest block"},
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


def alert_footer_keyboard() -> dict[str, Any] | None:
    rows: list[list[dict[str, str]]] = []
    if FOOTER_BTN1_TEXT and FOOTER_BTN1_URL:
        rows.append([{"text": FOOTER_BTN1_TEXT, "url": FOOTER_BTN1_URL}])
    if FOOTER_BTN2_TEXT and FOOTER_BTN2_URL:
        rows.append([{"text": FOOTER_BTN2_TEXT, "url": FOOTER_BTN2_URL}])
    return {"inline_keyboard": rows} if rows else None


def broadcast_alert(text: str) -> None:
    targets: list[Any] = []
    seen: set[str] = set()

    def _add(target: Any) -> None:
        key = str(target).strip()
        if not key or key in seen:
            return
        seen.add(key)
        targets.append(target)

    for cid in DEFAULT_CHAT_IDS:
        _add(cid)
    for sub in list_subscribers():
        _add(sub)

    if not targets:
        log.warning(
            "Broadcast suppressed: no TELEGRAM_CHAT_ID and no subscribers."
        )
        return
    keyboard = alert_footer_keyboard()
    for chat_id in targets:
        telegram_send(chat_id, text, reply_markup=keyboard)


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
# /check chain picker — when the user pastes a tx hash without specifying a
# chain, we stash the hash under a short token (since callback_data is
# capped at 64 bytes and the full 0x...64hex hash plus prefix doesn't fit)
# and offer an inline keyboard to pick the chain.
# ---------------------------------------------------------------------------


_PENDING_TX_CAP = 1024
_pending_tx: dict[str, str] = {}
_pending_tx_lock = threading.Lock()


def _stash_tx(tx_hash: str) -> str:
    """Store the tx hash and return a short id usable in callback_data."""
    short = secrets.token_urlsafe(6)
    with _pending_tx_lock:
        if len(_pending_tx) >= _PENDING_TX_CAP:
            # Drop the oldest ~10% so stashing stays O(1) amortised.
            drop = max(1, _PENDING_TX_CAP // 10)
            for k in list(_pending_tx.keys())[:drop]:
                _pending_tx.pop(k, None)
        _pending_tx[short] = tx_hash
    return short


def _pop_tx(short_id: str) -> str | None:
    with _pending_tx_lock:
        return _pending_tx.pop(short_id, None)


def chain_picker_keyboard(
    chains: dict[str, ChainCtx], short_id: str, lang: str,
) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    keys = list(chains.keys())
    for i in range(0, len(keys), 2):
        row = [
            {
                "text": chains[k].display_name,
                "callback_data": f"check:{k}:{short_id}",
            }
            for k in keys[i:i + 2]
        ]
        rows.append(row)
    rows.append([{"text": t(lang, "btn_cancel"), "callback_data": f"check_cancel:{short_id}"}])
    return {"inline_keyboard": rows}


# ---------------------------------------------------------------------------
# Token metadata (cached)
# ---------------------------------------------------------------------------


_token_meta_cache: dict[str, dict[str, dict[str, Any]]] = {}
_token_meta_lock = threading.Lock()


def get_token_meta(ctx: ChainCtx, token: str) -> dict[str, Any]:
    addr_key = token.lower()
    with _token_meta_lock:
        bucket = _token_meta_cache.setdefault(ctx.key, {})
        cached = bucket.get(addr_key)
    if cached is not None:
        return cached
    contract = ctx.w3.eth.contract(
        address=Web3.to_checksum_address(token), abi=ERC20_ABI,
    )
    meta = {"symbol": "?", "name": "?", "decimals": 18}
    for key in ("symbol", "name", "decimals"):
        try:
            meta[key] = getattr(contract.functions, key)().call()
        except Exception as exc:  # noqa: BLE001 - tolerate non-conforming tokens
            log.debug("[%s] token %s %s() failed: %s", ctx.key, token, key, exc)
    with _token_meta_lock:
        _token_meta_cache.setdefault(ctx.key, {})[addr_key] = meta
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
    ctx: ChainCtx,
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
    meta = get_token_meta(ctx, token)
    symbol = meta["symbol"]
    name = meta["name"]
    decimals = meta["decimals"]

    amount_raw = find_funding_amount(receipt_logs, token, distributor)
    if amount_raw is not None:
        amount_str = f"{format_amount(amount_raw, decimals)} {symbol}"
    else:
        amount_str = t(lang, "no_funding")

    chain_label = html.escape(ctx.display_name)
    return (
        f"{t(lang, title_key)} <code>[{chain_label}]</code>\n"
        f"<b>{t(lang, 'field_token')}:</b> {name} ({symbol})\n"
        f"<b>{t(lang, 'field_token_contract')}:</b> "
        f"<a href=\"{ctx.explorer_token}{token}\">{token}</a>\n"
        f"<b>{t(lang, 'field_amount')}:</b> {amount_str}\n"
        f"<b>{t(lang, 'field_distributor')}:</b> "
        f"<a href=\"{ctx.explorer_addr}{distributor}\">{short_addr(distributor)}</a>\n"
        f"<b>{t(lang, 'field_owner')}:</b> "
        f"<a href=\"{ctx.explorer_addr}{owner}\">{short_addr(owner)}</a>\n"
        f"<b>{t(lang, 'field_operator')}:</b> "
        f"<a href=\"{ctx.explorer_addr}{operator}\">{short_addr(operator)}</a>\n"
        f"<b>{t(lang, 'field_block')}:</b> {block_number}\n"
        f"<b>{t(lang, 'field_tx')}:</b> "
        f"<a href=\"{ctx.explorer_tx}{tx_hash}\">{short_addr(tx_hash)}</a>"
    )


BILINGUAL_SEPARATOR = "\n\n──────────\n\n"


def format_broadcast_alert(
    ctx: ChainCtx,
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
        chain_name=html.escape(ctx.display_name),
        token_name=name,
        token_symbol=symbol,
        token_contract=token,
        amount=amount_str,
        tx_url=f"{ctx.explorer_tx}{tx_hash}",
        time=block_time,
    )


def format_bilingual_broadcast_alert(
    ctx: ChainCtx,
    *,
    token: str,
    amount_raw: int | None,
    meta: dict[str, Any],
    tx_hash: str,
    block_time: str,
) -> str:
    parts = [
        format_broadcast_alert(
            ctx,
            lang=lang,
            token=token,
            amount_raw=amount_raw,
            meta=meta,
            tx_hash=tx_hash,
            block_time=block_time,
        )
        for lang in LANGS
    ]
    return BILINGUAL_SEPARATOR.join(parts)


def handle_event(ctx: ChainCtx, event: EventData) -> None:
    args = event["args"]
    tx_hash = _to_hex(event["transactionHash"])
    receipt = ctx.w3.eth.get_transaction_receipt(tx_hash)

    token = args["token"]
    distributor = args["distributorAddress"]
    amount_raw = find_funding_amount(receipt["logs"], token, distributor)
    # The event now carries the funded amount directly; fall back to it when
    # no matching Transfer log was found in the receipt.
    if amount_raw is None:
        amount_raw = args.get("initialTotalAmount")
    meta = get_token_meta(ctx, token)

    # Always remember the distributor → token mapping so we can correlate
    # later events (TimeSet, etc.) — even if this distributor is below the
    # broadcast threshold.
    add_distributor(ctx, distributor, {
        "token": token,
        "owner": args["owner"],
        "operator": args["operator"],
        "block": event["blockNumber"],
        "tx": tx_hash,
        "amount_raw": amount_raw,
    })

    if amount_raw is None:
        amount_human = Decimal(0)
    else:
        amount_human = Decimal(amount_raw) / (Decimal(10) ** int(meta["decimals"]))

    if MIN_TOKEN_AMOUNT > 0 and amount_human < MIN_TOKEN_AMOUNT:
        log.info(
            "[%s] Filtered DistributorCreated: amount=%s %s < threshold=%s tx=%s",
            ctx.key, amount_human, meta["symbol"], MIN_TOKEN_AMOUNT, tx_hash,
        )
        return

    try:
        block_ts = int(ctx.w3.eth.get_block(event["blockNumber"])["timestamp"])
        block_time = format_block_time(block_ts)
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] Could not fetch block timestamp: %s", ctx.key, exc)
        block_time = "?"

    msg = format_bilingual_broadcast_alert(
        ctx,
        token=token,
        amount_raw=amount_raw,
        meta=meta,
        tx_hash=tx_hash,
        block_time=block_time,
    )
    log.info(
        "[%s] DistributorCreated token=%s distributor=%s amount=%s tx=%s",
        ctx.key, token, distributor, amount_human, tx_hash,
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


def lookup_distributor_token(ctx: ChainCtx, distributor: str) -> str | None:
    """Resolve a distributor's underlying token, store first then on-chain."""
    info = get_distributor(ctx, distributor)
    if info and info.get("token"):
        return info["token"]
    try:
        contract = ctx.w3.eth.contract(
            address=Web3.to_checksum_address(distributor),
            abi=DISTRIBUTOR_TOKEN_ABI,
        )
        return contract.functions.token().call()
    except Exception as exc:  # noqa: BLE001
        log.debug("[%s] token() call failed for %s: %s", ctx.key, distributor, exc)
        return None


def _reverse_scan_for_creation(
    ctx: ChainCtx, distributor: str, end_block: int,
) -> dict[str, Any] | None:
    """Walk backwards through factory DistributorCreated logs to find the
    creation event matching `distributor`. Returns the decoded extras
    (owner/operator/block/tx/amount_raw) or None when not found.
    """
    if REVERSE_LOOKUP_BLOCKS <= 0:
        return None

    distributor_lc = distributor.lower()
    lowest = max(0, end_block - REVERSE_LOOKUP_BLOCKS)
    cursor = end_block
    while cursor >= lowest:
        from_block = max(lowest, cursor - MAX_BLOCK_RANGE + 1)
        try:
            logs = ctx.w3.eth.get_logs({
                "fromBlock": from_block,
                "toBlock": cursor,
                "address": ctx.factory_address,
                "topics": [DISTRIBUTOR_CREATED_TOPIC],
            })
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[%s] Reverse-scan getLogs failed (%s-%s): %s",
                ctx.key, from_block, cursor, exc,
            )
            return None

        for raw in logs:
            try:
                event = ctx.factory_event_cls().process_log(raw)
            except Exception:  # noqa: BLE001
                continue
            if event["args"]["distributorAddress"].lower() != distributor_lc:
                continue
            args = event["args"]
            tx_hash = _to_hex(event["transactionHash"])
            try:
                receipt = ctx.w3.eth.get_transaction_receipt(tx_hash)
                amount_raw = find_funding_amount(
                    receipt["logs"], args["token"], distributor,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[%s] Reverse-scan receipt fetch failed for %s: %s",
                    ctx.key, tx_hash, exc,
                )
                amount_raw = None
            return {
                "owner": args["owner"],
                "operator": args["operator"],
                "block": event["blockNumber"],
                "tx": tx_hash,
                "amount_raw": amount_raw,
            }

        if from_block == 0 or cursor <= from_block:
            break
        cursor = from_block - 1

    return None


def discover_distributor(
    ctx: ChainCtx, distributor: str, block_hint: int,
) -> dict[str, Any] | None:
    """Reverse-trace an unknown distributor on TimeSet arrival.

    Steps:
      1. Confirm the contract exposes a `token()` getter (else it's some
         unrelated contract whose event signature collides with ours —
         remember it via the negative cache).
      2. Best-effort scan factory `DistributorCreated` logs backwards to
         recover the original owner/operator/funding tx/funding amount.
      3. Persist the (possibly partial) info into `_distributors` so
         future TimeSet/Withdrawn events for this distributor are
         handled normally and the JSON store survives a restart.
    """
    existing = get_distributor(ctx, distributor)
    if existing is not None:
        if existing.get("amount_raw") is not None:
            return existing
        # Record exists but the funding amount is unknown (created before the
        # bot tracked it, or an earlier partial discovery). TimeSet/Withdrawn
        # logs carry no amount, so backfill it from the creation tx.
        extras = _reverse_scan_for_creation(ctx, distributor, block_hint)
        if extras is not None:
            update_distributor(ctx, distributor, extras)
        return existing
    if _is_non_distributor(ctx.key, distributor):
        return None

    token = lookup_distributor_token(ctx, distributor)
    if not token:
        _mark_non_distributor(ctx.key, distributor)
        log.debug(
            "[%s] %s does not expose token() — not an OKX Boost distributor",
            ctx.key, distributor,
        )
        return None

    info: dict[str, Any] = {
        "token": token,
        "owner": None,
        "operator": None,
        "block": None,
        "tx": None,
        "amount_raw": None,
    }
    extras = _reverse_scan_for_creation(ctx, distributor, block_hint)
    if extras is not None:
        info.update(extras)
        log.info(
            "[%s] Reverse-traced distributor %s → token=%s tx=%s amount_raw=%s",
            ctx.key, distributor, token, info["tx"], info["amount_raw"],
        )
    else:
        log.info(
            "[%s] Discovered distributor %s → token=%s "
            "(creation tx not found within %d blocks)",
            ctx.key, distributor, token, REVERSE_LOOKUP_BLOCKS,
        )

    add_distributor(ctx, distributor, info)
    return info


def _format_amount_str(meta: dict[str, Any], amount_raw: int | None) -> str:
    """Render an amount as `<value> <SYMBOL>`, or `—` when unknown.

    The symbol is HTML-escaped because it comes from on-chain ERC20
    metadata which can technically contain `<` / `>` / `&`.
    """
    if amount_raw is None:
        return "—"
    decimals = int(meta.get("decimals", 18))
    symbol = html.escape(str(meta.get("symbol", "?")))
    return f"{format_amount(amount_raw, decimals)} {symbol}"


def format_timeset_alert(
    ctx: ChainCtx,
    *,
    lang: str,
    template_key: str = "timeset_alert",
    token: str | None,
    meta: dict[str, Any],
    distributor: str,
    start_time: int,
    end_time: int,
    tx_hash: str,
    amount_raw: int | None = None,
) -> str:
    name = html.escape(str(meta.get("name", "?")))
    symbol = html.escape(str(meta.get("symbol", "?")))
    token_label = token or t(lang, "timeset_unknown_token")
    return t(
        lang, template_key,
        chain_name=html.escape(ctx.display_name),
        token_name=name,
        token_symbol=symbol,
        token_contract=token_label,
        amount=_format_amount_str(meta, amount_raw),
        distributor=distributor,
        start_time=format_block_time(start_time),
        end_time=format_block_time(end_time),
        tx_url=f"{ctx.explorer_tx}{tx_hash}",
    )


def format_bilingual_timeset_alert(
    ctx: ChainCtx,
    *,
    template_key: str = "timeset_alert",
    token: str | None,
    meta: dict[str, Any],
    distributor: str,
    start_time: int,
    end_time: int,
    tx_hash: str,
    amount_raw: int | None = None,
) -> str:
    parts = [
        format_timeset_alert(
            ctx,
            lang=lang,
            template_key=template_key,
            token=token,
            meta=meta,
            distributor=distributor,
            start_time=start_time,
            end_time=end_time,
            tx_hash=tx_hash,
            amount_raw=amount_raw,
        )
        for lang in LANGS
    ]
    return BILINGUAL_SEPARATOR.join(parts)


def handle_timeset(ctx: ChainCtx, raw_log: LogReceipt) -> None:
    distributor = raw_log["address"]
    info = get_distributor(ctx, distributor)
    if info is None:
        info = discover_distributor(ctx, distributor, raw_log["blockNumber"])
        if info is None:
            log.debug(
                "[%s] TimeSet from %s — not an OKX Boost distributor, skip",
                ctx.key, distributor,
            )
            return

    decoded = decode_timeset(raw_log)
    if decoded is None:
        log.warning("[%s] Could not decode TimeSet log from %s", ctx.key, distributor)
        return
    start_time, end_time = decoded
    tx_hash = _to_hex(raw_log["transactionHash"])
    token = info["token"]
    meta = get_token_meta(ctx, token)

    msg = format_bilingual_timeset_alert(
        ctx,
        token=token,
        meta=meta,
        distributor=distributor,
        start_time=start_time,
        end_time=end_time,
        tx_hash=tx_hash,
        amount_raw=info.get("amount_raw"),
    )
    log.info(
        "[%s] TimeSet token=%s distributor=%s start=%d end=%d tx=%s",
        ctx.key, token, distributor, start_time, end_time, tx_hash,
    )
    broadcast_alert(msg)


# ---------------------------------------------------------------------------
# Withdrawn (owner pulls funds back from a distributor — round cancelled)
# ---------------------------------------------------------------------------


def decode_withdrawn(raw_log: LogReceipt) -> tuple[str, int] | None:
    """Withdrawn(address to, uint256 amount) — both non-indexed.

    The data field is two 32-byte words: zero-padded address followed by
    uint256. Decoded without `eth_abi` to mirror `decode_timeset`.
    """
    data_hex = _to_hex(raw_log["data"])[2:]  # strip 0x
    if len(data_hex) < 128:
        return None
    try:
        to_int = int(data_hex[0:64], 16)
        amount = int(data_hex[64:128], 16)
    except ValueError:
        return None
    to_addr = Web3.to_checksum_address("0x" + f"{to_int:040x}")
    return to_addr, amount


def format_withdrawn_alert(
    ctx: ChainCtx,
    *,
    lang: str,
    template_key: str = "withdrawn_alert",
    token: str | None,
    meta: dict[str, Any],
    distributor: str,
    to_address: str,
    amount_raw: int,
    tx_hash: str,
    block_time: str,
) -> str:
    name = html.escape(str(meta.get("name", "?")))
    symbol = html.escape(str(meta.get("symbol", "?")))
    token_label = token or t(lang, "timeset_unknown_token")
    decimals = int(meta.get("decimals", 18))
    return t(
        lang, template_key,
        chain_name=html.escape(ctx.display_name),
        token_name=name,
        token_symbol=symbol,
        token_contract=token_label,
        amount=f"{format_amount(amount_raw, decimals)} {symbol}",
        to_address=to_address,
        distributor=distributor,
        time=block_time,
        tx_url=f"{ctx.explorer_tx}{tx_hash}",
    )


def format_bilingual_withdrawn_alert(
    ctx: ChainCtx,
    *,
    template_key: str = "withdrawn_alert",
    token: str | None,
    meta: dict[str, Any],
    distributor: str,
    to_address: str,
    amount_raw: int,
    tx_hash: str,
    block_time: str,
) -> str:
    parts = [
        format_withdrawn_alert(
            ctx,
            lang=lang,
            template_key=template_key,
            token=token,
            meta=meta,
            distributor=distributor,
            to_address=to_address,
            amount_raw=amount_raw,
            tx_hash=tx_hash,
            block_time=block_time,
        )
        for lang in LANGS
    ]
    return BILINGUAL_SEPARATOR.join(parts)


def handle_withdrawn(ctx: ChainCtx, raw_log: LogReceipt) -> None:
    distributor = raw_log["address"]
    info = get_distributor(ctx, distributor)
    if info is None:
        log.warning(
            "[%s] Withdrawn from unknown distributor %s — skip (not in store)",
            ctx.key, distributor,
        )
        return

    decoded = decode_withdrawn(raw_log)
    if decoded is None:
        log.warning(
            "[%s] Could not decode Withdrawn log from %s", ctx.key, distributor,
        )
        return
    to_addr, amount_raw = decoded
    tx_hash = _to_hex(raw_log["transactionHash"])
    token = info["token"]
    meta = get_token_meta(ctx, token)

    amount_human = Decimal(amount_raw) / (Decimal(10) ** int(meta["decimals"]))
    if MIN_TOKEN_AMOUNT > 0 and amount_human < MIN_TOKEN_AMOUNT:
        log.info(
            "[%s] Filtered Withdrawn: amount=%s %s < threshold=%s tx=%s",
            ctx.key, amount_human, meta["symbol"], MIN_TOKEN_AMOUNT, tx_hash,
        )
        return

    try:
        block_ts = int(ctx.w3.eth.get_block(raw_log["blockNumber"])["timestamp"])
        block_time = format_block_time(block_ts)
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] Could not fetch block timestamp: %s", ctx.key, exc)
        block_time = "?"

    msg = format_bilingual_withdrawn_alert(
        ctx,
        token=token,
        meta=meta,
        distributor=distributor,
        to_address=to_addr,
        amount_raw=amount_raw,
        tx_hash=tx_hash,
        block_time=block_time,
    )
    log.info(
        "[%s] Withdrawn token=%s distributor=%s to=%s amount=%s tx=%s",
        ctx.key, token, distributor, to_addr, amount_human, tx_hash,
    )
    broadcast_alert(msg)


# ---------------------------------------------------------------------------
# /check
# ---------------------------------------------------------------------------


def check_transaction(ctx: ChainCtx, tx_hash: str, lang: str) -> str:
    if not TX_HASH_RE.fullmatch(tx_hash):
        return t(lang, "check_invalid")

    try:
        chain_id: Any = ctx.w3.eth.chain_id
    except Exception:  # noqa: BLE001
        chain_id = "?"
    try:
        head_block: Any = ctx.w3.eth.block_number
    except Exception:  # noqa: BLE001
        head_block = "?"

    try:
        receipt = ctx.w3.eth.get_transaction_receipt(tx_hash)
    except TransactionNotFound:
        return t(
            lang, "check_not_found",
            chain_name=html.escape(ctx.display_name),
            tx=tx_hash, chain=chain_id, head=head_block,
        )
    except Exception as exc:  # noqa: BLE001
        return t(lang, "rpc_error", err=_scrub_for_user(str(exc)))

    if receipt is None:
        return t(lang, "check_pending", tx=tx_hash)
    if receipt.get("status") != 1:
        return t(
            lang, "check_reverted",
            tx=tx_hash, url=f"{ctx.explorer_tx}{tx_hash}",
        )

    factory_lc = ctx.factory_address.lower()
    created_topic = DISTRIBUTOR_CREATED_TOPIC.lower()
    timeset_topic = TIMESET_TOPIC.lower()
    withdrawn_topic = WITHDRAWN_TOPIC.lower()

    created_matches = []
    timeset_matches = []
    withdrawn_matches = []
    for raw in receipt["logs"]:
        topics = raw.get("topics") or []
        if not topics:
            continue
        topic0 = _to_hex(topics[0])
        if topic0 == created_topic and raw["address"].lower() == factory_lc:
            try:
                created_matches.append(ctx.factory_event_cls().process_log(raw))
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[%s] Could not decode DistributorCreated log: %s",
                    ctx.key, exc,
                )
        elif topic0 == timeset_topic:
            timeset_matches.append(raw)
        elif topic0 == withdrawn_topic:
            withdrawn_matches.append(raw)

    if not created_matches and not timeset_matches and not withdrawn_matches:
        return t(
            lang, "check_no_event",
            chain_name=html.escape(ctx.display_name),
            factory=ctx.factory_address,
            tx=tx_hash,
            url=f"{ctx.explorer_tx}{tx_hash}",
        )

    parts: list[str] = []
    for ev in created_matches:
        args = ev["args"]
        parts.append(
            format_distributor_alert(
                ctx,
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
        token = lookup_distributor_token(ctx, distributor)
        meta = (
            get_token_meta(ctx, token)
            if token
            else {"name": "?", "symbol": "?", "decimals": 18}
        )
        info = discover_distributor(ctx, distributor, raw["blockNumber"])
        parts.append(
            format_timeset_alert(
                ctx,
                lang=lang,
                template_key="timeset_hit",
                token=token,
                meta=meta,
                distributor=distributor,
                start_time=start_time,
                end_time=end_time,
                tx_hash=tx_hash,
                amount_raw=info.get("amount_raw") if info else None,
            )
        )
    for raw in withdrawn_matches:
        decoded = decode_withdrawn(raw)
        if decoded is None:
            continue
        to_addr, amount_raw = decoded
        distributor = raw["address"]
        token = lookup_distributor_token(ctx, distributor)
        meta = (
            get_token_meta(ctx, token)
            if token
            else {"name": "?", "symbol": "?", "decimals": 18}
        )
        try:
            block_ts = int(ctx.w3.eth.get_block(raw["blockNumber"])["timestamp"])
            block_time = format_block_time(block_ts)
        except Exception:  # noqa: BLE001
            block_time = "?"
        parts.append(
            format_withdrawn_alert(
                ctx,
                lang=lang,
                template_key="withdrawn_hit",
                token=token,
                meta=meta,
                distributor=distributor,
                to_address=to_addr,
                amount_raw=amount_raw,
                tx_hash=tx_hash,
                block_time=block_time,
            )
        )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Status text
# ---------------------------------------------------------------------------


def _status_section(ctx: ChainCtx, lang: str) -> str:
    try:
        head_block = str(ctx.w3.eth.block_number)
    except Exception as exc:  # noqa: BLE001
        head_block = f"err: {exc}"
    try:
        chain_id = str(ctx.w3.eth.chain_id)
    except Exception:  # noqa: BLE001
        chain_id = "?"
    with LAST_BLOCK_LOCK:
        seen = LAST_BLOCK_SEEN.get(ctx.key, 0)
    return (
        f"<b>[{html.escape(ctx.display_name)}]</b>\n"
        f"  <b>{t(lang, 'status_factory')}:</b> <code>{ctx.factory_address}</code>\n"
        f"  <b>{t(lang, 'status_chain')}:</b> <code>{chain_id}</code>\n"
        f"  <b>{t(lang, 'status_head')}:</b> <code>{head_block}</code>\n"
        f"  <b>{t(lang, 'status_last_processed')}:</b> <code>{seen}</code>\n"
        f"  <b>{t(lang, 'status_distributors')}:</b> "
        f"<code>{distributor_count(ctx)}</code>"
    )


def build_status_text(
    chains: dict[str, ChainCtx], lang: str, user_id: int,
) -> str:
    sections = [_status_section(ctx, lang) for ctx in chains.values()]
    uptime = format_uptime(int(time.time() - BOT_STARTED_AT))
    user_lang_label = LANG_LABEL.get(get_user_lang(user_id), get_user_lang(user_id))

    interval = get_poll_interval()
    threshold = (
        format_threshold(MIN_TOKEN_AMOUNT)
        if MIN_TOKEN_AMOUNT > 0
        else t(lang, "status_open")
    )
    chain_keys = ", ".join(chains.keys()) or "—"
    footer = (
        f"<b>{t(lang, 'status_chains')}:</b> <code>{chain_keys}</code>\n"
        f"<b>{t(lang, 'status_uptime')}:</b> {uptime}\n"
        f"<b>{t(lang, 'status_interval')}:</b> {format_duration(interval)}\n"
        f"<b>{t(lang, 'status_min_amount')}:</b> {threshold}\n"
        f"<b>{t(lang, 'status_whitelist')}:</b> "
        f"{len(WHITELIST) if WHITELIST else t(lang, 'status_open')}\n"
        f"<b>{t(lang, 'status_lang')}:</b> {user_lang_label}"
    )
    return t(lang, "status_title") + "\n\n" + "\n\n".join(sections) + "\n\n" + footer


SAMPLE_TOKEN = "0xDf24f8c21Cb404B3031a450D8e049D6E39FC1fA5"
SAMPLE_DISTRIBUTOR = "0x9C957C50be4C2020eDe91f3965AaA9bE30de9643"
SAMPLE_TIMESET_DIST = "0x72565f6b567b492047610512352584eb6d2b0c37"
SAMPLE_TX = "0x72e7b61f8ac3415468fbabeaea3e215bf86cc8ee6a1ace096091883fe001d2d1"
SAMPLE_META = {"name": "Sample Token", "symbol": "SAMPLE", "decimals": 18}
SAMPLE_AMOUNT_RAW = 20_000_000 * 10**18


def build_preview_messages(ctx: ChainCtx, lang: str) -> list[str]:
    now = int(time.time())
    label = t(lang, "preview_label")

    distributor_msg = format_bilingual_broadcast_alert(
        ctx,
        token=SAMPLE_TOKEN,
        amount_raw=SAMPLE_AMOUNT_RAW,
        meta=SAMPLE_META,
        tx_hash=SAMPLE_TX,
        block_time=format_block_time(now),
    )
    timeset_msg = format_bilingual_timeset_alert(
        ctx,
        template_key="timeset_alert",
        token=SAMPLE_TOKEN,
        meta=SAMPLE_META,
        distributor=SAMPLE_TIMESET_DIST,
        start_time=now + 3 * 86400,
        end_time=now + 17 * 86400,
        tx_hash=SAMPLE_TX,
        amount_raw=SAMPLE_AMOUNT_RAW,
    )
    return [
        f"{label}\n{distributor_msg}",
        f"{label}\n{timeset_msg}",
    ]


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


def _chain_keys_label(chains: dict[str, ChainCtx]) -> str:
    return ", ".join(chains.keys())


def _resolve_chain_arg(
    chains: dict[str, ChainCtx], arg: str,
) -> tuple[ChainCtx | None, str]:
    """Split `<chain> <rest>` from `arg`. Returns (ctx-or-None, rest)."""
    if not arg:
        return None, ""
    head, _, rest = arg.partition(" ")
    head_lc = head.strip().lower()
    if head_lc in chains:
        return chains[head_lc], rest.strip()
    return None, arg


def handle_command(
    chains: dict[str, ChainCtx],
    default_chain: str,
    message: dict[str, Any],
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
        telegram_send(
            chat_id,
            t(lang, "help", chains=_chain_keys_label(chains)),
            reply_to=msg_id,
        )
        return

    if cmd == "/menu":
        telegram_send(
            chat_id,
            t(lang, "menu_title", chains=_chain_keys_label(chains)),
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
        telegram_send(
            chat_id, build_status_text(chains, lang, user_id), reply_to=msg_id,
        )
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
        if DEFAULT_CHAT_IDS:
            lines.append(t(lang, "subs_default_header"))
            for cid in DEFAULT_CHAT_IDS:
                lines.append(f"  • {format_chat_label(cid)}")
        if subs:
            lines.append(t(lang, "subs_extra"))
            for s in subs:
                lines.append(f"  • {format_chat_label(s)}")
        else:
            lines.append(t(lang, "subs_empty"))
        telegram_send(chat_id, "\n".join(lines), reply_to=msg_id)
        return

    if cmd == "/preview":
        ctx, _rest = _resolve_chain_arg(chains, arg)
        if arg and ctx is None:
            telegram_send(
                chat_id,
                t(lang, "preview_unknown_chain",
                  chain=html.escape(arg.split()[0]),
                  chains=_chain_keys_label(chains)),
                reply_to=msg_id,
            )
            return
        if ctx is None:
            ctx = chains[default_chain]
        keyboard = alert_footer_keyboard()
        for m in build_preview_messages(ctx, lang):
            telegram_send(chat_id, m, reply_markup=keyboard)
        return

    if cmd == "/check":
        if not arg:
            telegram_send(
                chat_id,
                t(lang, "check_usage", chains=_chain_keys_label(chains)),
                reply_to=msg_id,
            )
            return
        ctx, rest = _resolve_chain_arg(chains, arg)
        if ctx is not None:
            # Explicit chain — run directly.
            if not rest:
                telegram_send(
                    chat_id,
                    t(lang, "check_usage", chains=_chain_keys_label(chains)),
                    reply_to=msg_id,
                )
                return
            m = TX_HASH_RE.search(rest)
            target = m.group(0) if m else rest
            result = check_transaction(ctx, target.lower(), lang)
            telegram_send(chat_id, result, reply_to=msg_id)
            return
        # No chain prefix — if there's a tx hash anywhere in arg, show the
        # chain picker so the user can tap to choose. Otherwise it's
        # genuinely an unknown chain key.
        m = TX_HASH_RE.search(arg)
        if m:
            tx = m.group(0).lower()
            short = _stash_tx(tx)
            telegram_send(
                chat_id,
                t(lang, "check_pick_chain", tx=tx),
                reply_to=msg_id,
                reply_markup=chain_picker_keyboard(chains, short, lang),
            )
            return
        telegram_send(
            chat_id,
            t(lang, "check_unknown_chain",
              chain=html.escape(arg.split()[0]),
              chains=_chain_keys_label(chains)),
            reply_to=msg_id,
        )
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

    if cmd in ("/skip", "/synclatest"):
        # Fast-forward the monitor to the chain head, dropping the backlog.
        # `/skip` with no arg applies to every chain; `/skip <chain>` to one.
        ctx, _rest = _resolve_chain_arg(chains, arg)
        if arg and ctx is None:
            telegram_send(
                chat_id,
                t(lang, "skip_unknown_chain",
                  chain=html.escape(arg.split()[0]),
                  chains=_chain_keys_label(chains)),
                reply_to=msg_id,
            )
            return
        targets = [ctx] if ctx is not None else list(chains.values())
        lines = [t(lang, "skip_title")]
        for c in targets:
            request_skip_to_head(c.key)
            try:
                head = str(c.w3.eth.block_number)
            except Exception:  # noqa: BLE001
                head = "?"
            lines.append(
                t(lang, "skip_line",
                  chain=html.escape(c.display_name), head=head)
            )
        telegram_send(chat_id, "\n".join(lines), reply_to=msg_id)
        return

    if cmd.startswith("/"):
        telegram_send(chat_id, t(lang, "unknown_cmd"), reply_to=msg_id)
        return

    # Not a command — if the message contains a tx hash, offer the chain
    # picker so the user can tap to choose which chain to query.
    m = TX_HASH_RE.search(text)
    if m:
        tx = m.group(0).lower()
        short = _stash_tx(tx)
        telegram_send(
            chat_id,
            t(lang, "check_pick_chain", tx=tx),
            reply_to=msg_id,
            reply_markup=chain_picker_keyboard(chains, short, lang),
        )


def handle_callback_query(
    chains: dict[str, ChainCtx], callback: dict[str, Any],
) -> None:
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
            chat_id, message_id,
            t(lang, "menu_title", chains=_chain_keys_label(chains)),
            reply_markup=main_menu_keyboard(lang),
        )
        return

    if data == "help":
        telegram_answer_callback(callback_id)
        telegram_edit(
            chat_id, message_id, t(lang, "help", chains=_chain_keys_label(chains)),
            reply_markup=main_menu_keyboard(lang),
        )
        return

    if data == "status":
        telegram_answer_callback(callback_id)
        telegram_edit(
            chat_id, message_id, build_status_text(chains, lang, user_id),
            reply_markup=main_menu_keyboard(lang),
        )
        return

    if data == "check_hint":
        telegram_answer_callback(callback_id)
        telegram_edit(
            chat_id, message_id,
            t(lang, "menu_check_hint", chains=_chain_keys_label(chains)),
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
            chat_id, message_id,
            t(new_lang, "menu_title", chains=_chain_keys_label(chains)),
            reply_markup=main_menu_keyboard(new_lang),
        )
        return

    if data.startswith("check:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            telegram_answer_callback(callback_id)
            return
        _, chain_key, short_id = parts
        ctx = chains.get(chain_key)
        tx_hash = _pop_tx(short_id)
        if ctx is None or tx_hash is None:
            telegram_answer_callback(callback_id, t(lang, "check_expired"))
            telegram_edit(chat_id, message_id, t(lang, "check_expired"))
            return
        telegram_answer_callback(
            callback_id,
            t(lang, "checking", chain=ctx.display_name),
        )
        try:
            result = check_transaction(ctx, tx_hash, lang)
        except Exception as exc:  # noqa: BLE001
            log.exception("check_transaction error: %s", exc)
            result = t(lang, "rpc_error", err=_scrub_for_user(str(exc)))
        telegram_edit(chat_id, message_id, result)
        return

    if data.startswith("check_cancel:"):
        _, _, short_id = data.partition(":")
        _pop_tx(short_id)
        telegram_answer_callback(callback_id, t(lang, "check_canceled"))
        try:
            requests.post(
                f"{_telegram_base()}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
                timeout=15,
            )
        except requests.RequestException as exc:
            log.warning("deleteMessage failed: %s", exc)
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


def _record_distributor_only(ctx: ChainCtx, event: EventData) -> None:
    """Add a distributor to the store without broadcasting (used by backfill)."""
    args = event["args"]
    add_distributor(ctx, args["distributorAddress"], {
        "token": args["token"],
        "owner": args["owner"],
        "operator": args["operator"],
        "block": event["blockNumber"],
        "tx": _to_hex(event["transactionHash"]),
    })


def backfill_distributors(
    ctx: ChainCtx, head_block: int, blocks_back: int,
) -> None:
    if blocks_back <= 0:
        return
    start = max(0, head_block - blocks_back)
    log.info(
        "[%s] Backfilling distributors from block %s to %s (range=%d)",
        ctx.key, start, head_block, blocks_back,
    )
    cursor = start
    added_before = distributor_count(ctx)
    while cursor <= head_block:
        end = min(head_block, cursor + MAX_BLOCK_RANGE - 1)
        try:
            logs = ctx.w3.eth.get_logs({
                "fromBlock": cursor,
                "toBlock": end,
                "address": ctx.factory_address,
                "topics": [DISTRIBUTOR_CREATED_TOPIC],
            })
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[%s] Backfill chunk %s-%s failed: %s",
                ctx.key, cursor, end, exc,
            )
            cursor = end + 1
            continue
        for raw in logs:
            try:
                event = ctx.factory_event_cls().process_log(raw)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] Backfill: failed to decode log: %s", ctx.key, exc)
                continue
            _record_distributor_only(ctx, event)
        cursor = end + 1
    total = distributor_count(ctx)
    log.info(
        "[%s] Backfill complete: %d new distributors (total %d)",
        ctx.key, total - added_before, total,
    )


def _poll_timeset(ctx: ChainCtx, from_block: int, to_block: int) -> None:
    """Poll TimeSet events by topic across the whole chain.

    We deliberately drop the address filter here so distributors created
    while the bot was offline (or before BACKFILL_BLOCKS) still trigger
    alerts: `handle_timeset` will reverse-trace any unknown sender via
    `discover_distributor`. False positives (other contracts with the
    same event signature) are cheap to reject via `discover_distributor`
    and remembered in a negative cache for the rest of the session.
    """
    target_topic = TIMESET_TOPIC.lower()
    try:
        logs = ctx.w3.eth.get_logs({
            "fromBlock": from_block,
            "toBlock": to_block,
            "topics": [TIMESET_TOPIC],
        })
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "[%s] TimeSet getLogs failed (blocks %s-%s): %s",
            ctx.key, from_block, to_block, exc,
        )
        return
    for raw in logs:
        topics = raw.get("topics") or []
        if not topics or _to_hex(topics[0]) != target_topic:
            continue
        try:
            handle_timeset(ctx, raw)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] handle_timeset error: %s", ctx.key, exc)


def _poll_withdrawn(ctx: ChainCtx, from_block: int, to_block: int) -> None:
    addresses = list_distributor_addresses(ctx)
    if not addresses:
        return
    target_topic = WITHDRAWN_TOPIC.lower()
    for chunk in _chunked(addresses, LOGS_ADDRESS_CHUNK):
        try:
            logs = ctx.w3.eth.get_logs({
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": chunk,
                "topics": [WITHDRAWN_TOPIC],
            })
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[%s] Withdrawn getLogs failed (chunk size %d, blocks %s-%s): %s",
                ctx.key, len(chunk), from_block, to_block, exc,
            )
            continue
        for raw in logs:
            topics = raw.get("topics") or []
            if not topics or _to_hex(topics[0]) != target_topic:
                continue
            try:
                handle_withdrawn(ctx, raw)
            except Exception as exc:  # noqa: BLE001
                log.exception("[%s] handle_withdrawn error: %s", ctx.key, exc)


def chain_monitor(ctx: ChainCtx) -> None:
    head = ctx.w3.eth.block_number
    last_block = load_last_block(ctx, default=max(0, head - BLOCK_LOOKBACK))
    with LAST_BLOCK_LOCK:
        LAST_BLOCK_SEEN[ctx.key] = last_block
    log.info(
        "[%s] Watching factory %s on chain id %s, starting from block %s (head=%s)",
        ctx.key, ctx.factory_address, ctx.w3.eth.chain_id, last_block, head,
    )

    if BACKFILL_BLOCKS > 0:
        try:
            backfill_distributors(ctx, head, BACKFILL_BLOCKS)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] Backfill failed: %s", ctx.key, exc)

    log.info("[%s] Tracking %d distributors at startup", ctx.key, distributor_count(ctx))

    while True:
        interval = get_poll_interval()
        try:
            head = ctx.w3.eth.block_number
            if consume_skip_request(ctx.key) and head > last_block:
                # /skip — drop the backlog and resume from the chain head.
                log.info(
                    "[%s] Skip requested: fast-forwarding last_block %s -> %s",
                    ctx.key, last_block, head,
                )
                last_block = head
                save_last_block(ctx, last_block)
                with LAST_BLOCK_LOCK:
                    LAST_BLOCK_SEEN[ctx.key] = last_block
                time.sleep(interval)
                continue
            if head <= last_block:
                time.sleep(interval)
                continue

            from_block = last_block + 1
            to_block = min(head, from_block + MAX_BLOCK_RANGE - 1)

            # 1. DistributorCreated from the factory
            logs = ctx.w3.eth.get_logs(
                {
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "address": ctx.factory_address,
                    "topics": [DISTRIBUTOR_CREATED_TOPIC],
                }
            )
            for raw in logs:
                try:
                    event = ctx.factory_event_cls().process_log(raw)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[%s] Failed to decode log: %s", ctx.key, exc)
                    continue
                handle_event(ctx, event)

            # 2. TimeSet from any known distributor (incl. ones just added above)
            _poll_timeset(ctx, from_block, to_block)

            # 3. Withdrawn — owner pulled funds back, this round is dead
            _poll_withdrawn(ctx, from_block, to_block)

            last_block = to_block
            save_last_block(ctx, last_block)
            with LAST_BLOCK_LOCK:
                LAST_BLOCK_SEEN[ctx.key] = last_block
        except BlockNotFound:
            time.sleep(interval)
        except KeyboardInterrupt:
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] Chain monitor error: %s", ctx.key, exc)
            time.sleep(interval * 2)
        else:
            time.sleep(interval)


def telegram_listener(
    chains: dict[str, ChainCtx], default_chain: str,
) -> None:
    offset: int | None = None
    log.info(
        "Telegram listener started (whitelist=%s, default_lang=%s, chains=%s)",
        sorted(WHITELIST) if WHITELIST else "OPEN — accepting all users",
        DEFAULT_LANG,
        list(chains.keys()),
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
                        handle_command(chains, default_chain, upd["message"])
                    elif "callback_query" in upd:
                        handle_callback_query(chains, upd["callback_query"])
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


RPC_CONNECT_ATTEMPTS = int(os.getenv("RPC_CONNECT_ATTEMPTS", "3"))
RPC_CONNECT_BACKOFF = float(os.getenv("RPC_CONNECT_BACKOFF", "2"))


def _connect_with_retry(key: str, rpc_url: str) -> Web3 | None:
    """Try to connect; return the Web3 on success or None on failure.

    Calls `eth.chain_id` rather than `is_connected()` so the actual RPC
    error (HTTP status, body) is preserved in the log message — many
    hosted RPCs (Ankr free tier, etc.) return 403/429/maintenance pages
    that `is_connected()` swallows into a bare False.
    """
    last_err = ""
    for attempt in range(1, RPC_CONNECT_ATTEMPTS + 1):
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        try:
            chain_id = w3.eth.chain_id
        except Exception as exc:  # noqa: BLE001
            last_err = _scrub_for_user(str(exc)) or type(exc).__name__
            log.warning(
                "[%s] RPC connect attempt %d/%d failed: %s",
                key, attempt, RPC_CONNECT_ATTEMPTS, last_err,
            )
            if attempt < RPC_CONNECT_ATTEMPTS:
                time.sleep(RPC_CONNECT_BACKOFF * attempt)
            continue
        log.info(
            "[%s] Connected (chain id=%s) to RPC: %s",
            key, chain_id, _redact(rpc_url),
        )
        return w3
    log.error(
        "[%s] Cannot reach RPC at %s after %d attempts: %s",
        key, _redact(rpc_url), RPC_CONNECT_ATTEMPTS, last_err,
    )
    return None


def _build_chain_ctx(key: str) -> ChainCtx | None:
    """Build a `ChainCtx` from env. Returns None if the chain can't be
    brought up (bad config or RPC unreachable) so the caller can skip it
    instead of killing the whole bot.

    `bsc` falls back to the legacy bare vars for backward compatibility.
    """
    if not CHAIN_KEY_RE.fullmatch(key):
        log.error("Invalid chain key %r — must match %s", key, CHAIN_KEY_RE.pattern)
        return None
    legacy = key == "bsc"

    rpc_template = _chain_env(
        key, "RPC_URL",
        legacy_fallback=legacy,
        default=DEFAULT_BSC_RPC if legacy else "",
    )
    if not rpc_template:
        log.error("[%s] No RPC URL configured — set %s_RPC_URL", key, key.upper())
        return None
    api_key = _chain_env(key, "RPC_API_KEY", legacy_fallback=legacy)
    if api_key:
        _register_redaction(api_key)
    if "{API_KEY}" in rpc_template and not api_key:
        log.error(
            "[%s] %s_RPC_URL contains {API_KEY} placeholder but %s_RPC_API_KEY is empty",
            key, key.upper(), key.upper(),
        )
        return None
    rpc_url = rpc_template.replace("{API_KEY}", api_key) if api_key else rpc_template

    factory_raw = _chain_env(
        key, "FACTORY_ADDRESS",
        legacy_fallback=legacy,
        default=DEFAULT_FACTORY,
    )
    try:
        factory_address = Web3.to_checksum_address(factory_raw)
    except (ValueError, TypeError) as exc:
        log.error("[%s] Invalid FACTORY_ADDRESS %r: %s", key, factory_raw, exc)
        return None

    explorer_tx = _chain_env(
        key, "EXPLORER_TX",
        legacy_fallback=legacy,
        default=DEFAULT_BSC_EXPLORERS["tx"] if legacy else "",
    )
    explorer_addr = _chain_env(
        key, "EXPLORER_ADDR",
        legacy_fallback=legacy,
        default=DEFAULT_BSC_EXPLORERS["addr"] if legacy else "",
    )
    explorer_token = _chain_env(
        key, "EXPLORER_TOKEN",
        legacy_fallback=legacy,
        default=DEFAULT_BSC_EXPLORERS["token"] if legacy else "",
    )
    if not (explorer_tx and explorer_addr and explorer_token):
        log.warning(
            "[%s] No explorer URLs — alert links will be broken. "
            "Set %s_EXPLORER_TX / _ADDR / _TOKEN.",
            key, key.upper(),
        )

    display_name = _chain_env(
        key, "DISPLAY_NAME",
        default=DEFAULT_DISPLAY_NAMES.get(key, key.upper()),
    )

    w3 = _connect_with_retry(key, rpc_url)
    if w3 is None:
        return None

    factory = w3.eth.contract(
        address=factory_address, abi=[DISTRIBUTOR_CREATED_ABI],
    )
    factory_event_cls = factory.events.DistributorCreated

    state_file = STATE_DIR / f".bot_state.{key}.json"
    distributors_file = STATE_DIR / f".distributors.{key}.json"

    return ChainCtx(
        key=key,
        display_name=display_name,
        w3=w3,
        factory_address=factory_address,
        factory_event_cls=factory_event_cls,
        explorer_tx=explorer_tx,
        explorer_addr=explorer_addr,
        explorer_token=explorer_token,
        state_file=state_file,
        distributors_file=distributors_file,
    )


def _load_chains() -> tuple[dict[str, ChainCtx], str]:
    raw = os.getenv("CHAINS", "").strip()
    if raw:
        keys = [k.strip().lower() for k in raw.split(",") if k.strip()]
    else:
        log.info("CHAINS not set — running in single-chain mode (bsc).")
        keys = ["bsc"]

    seen: list[str] = []
    for k in keys:
        if k not in seen:
            seen.append(k)

    chains: dict[str, ChainCtx] = {}
    for key in seen:
        ctx = _build_chain_ctx(key)
        if ctx is not None:
            chains[key] = ctx

    skipped = [k for k in seen if k not in chains]
    if skipped:
        log.warning(
            "Skipped %d chain(s) that failed to initialize: %s. "
            "Bot will continue with the chains that did connect.",
            len(skipped), skipped,
        )
    if not chains:
        log.error(
            "No chains could be initialized (configured: %s). Exiting so "
            "the platform can restart us; fix the config and redeploy.",
            seen,
        )
        sys.exit(1)

    requested_default = os.getenv("DEFAULT_CHAIN", "").strip().lower()
    first_active = next(iter(chains))
    default_chain = requested_default or first_active
    if default_chain not in chains:
        log.warning(
            "DEFAULT_CHAIN=%r is not active, using %r instead.",
            requested_default or first_active, first_active,
        )
        default_chain = first_active

    log.info("Active chains: %s (default: %s)", list(chains.keys()), default_chain)
    return chains, default_chain


def main() -> None:
    must_have_telegram_creds()
    _load_user_lang()
    _load_runtime_config()
    _load_subscribers()

    chains, default_chain = _load_chains()
    for ctx in chains.values():
        _load_distributors_for(ctx)

    telegram_set_my_commands()

    threads = [
        threading.Thread(
            target=chain_monitor, args=(ctx,),
            name=f"chain-monitor-{ctx.key}", daemon=True,
        )
        for ctx in chains.values()
    ]
    threads.append(
        threading.Thread(
            target=telegram_listener, args=(chains, default_chain),
            name="tg-listener", daemon=True,
        )
    )
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
