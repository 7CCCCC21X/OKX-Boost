"""Unit tests for the pure helpers in bot.py.

These cover parsing, formatting, manual ABI decoding, env coercion, and the
Telegram message splitter — the logic most prone to silent regressions. No
network or RPC is touched; importing bot.py only reads env + defines symbols.

Run with: TELEGRAM_TOKEN=x TELEGRAM_CHAT_ID=1 python -m pytest tests/
"""
from __future__ import annotations

import os
from decimal import Decimal

os.environ.setdefault("TELEGRAM_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "1")

import bot  # noqa: E402


# --- duration parsing/formatting -------------------------------------------

def test_parse_duration_units():
    assert bot.parse_duration("30") == 30
    assert bot.parse_duration("30s") == 30
    assert bot.parse_duration("3m") == 180
    assert bot.parse_duration("1h") == 3600
    assert bot.parse_duration("1.5m") == 90


def test_parse_duration_invalid():
    assert bot.parse_duration("") is None
    assert bot.parse_duration("abc") is None
    assert bot.parse_duration("10d") is None


def test_format_duration_roundtrip():
    assert bot.format_duration(30) == "30s"
    assert bot.format_duration(180) == "3m"
    assert bot.format_duration(3600) == "1h"
    assert bot.format_duration(90) == "90s"


# --- amount formatting ------------------------------------------------------

def test_format_amount_decimals():
    assert bot.format_amount(0, 18) == "0"
    assert bot.format_amount(10**18, 18) == "1"
    assert bot.format_amount(1500 * 10**18, 18) == "1,500"


def test_format_amount_zero_decimals():
    assert bot.format_amount(1234567, 0) == "1,234,567"


# --- env coercion -----------------------------------------------------------

def test_env_int_valid_and_invalid(monkeypatch=None):
    os.environ["X_TEST_INT"] = "42"
    assert bot._env_int("X_TEST_INT", "7") == 42
    os.environ["X_TEST_INT"] = "not-a-number"
    assert bot._env_int("X_TEST_INT", "7") == 7
    del os.environ["X_TEST_INT"]
    assert bot._env_int("X_TEST_INT", "9") == 9


def test_env_float_invalid_fallback():
    os.environ["X_TEST_FLOAT"] = "bad"
    assert bot._env_float("X_TEST_FLOAT", "2.5") == 2.5
    del os.environ["X_TEST_FLOAT"]


def test_clean_env_value_strips_angle_brackets():
    assert bot._clean_env_value("  <https://rpc.example/x>  ") == "https://rpc.example/x"
    assert bot._clean_env_value("plain") == "plain"


# --- manual ABI decoding ----------------------------------------------------

def _word(n: int) -> str:
    return f"{n:064x}"


def test_decode_timeset():
    start, end = 1_700_000_000, 1_700_086_400
    raw = {"data": "0x" + _word(start) + _word(end)}
    assert bot.decode_timeset(raw) == (start, end)


def test_decode_timeset_too_short():
    assert bot.decode_timeset({"data": "0x" + _word(1)}) is None


def test_decode_withdrawn():
    addr_int = 0x00000000000000000000000000000000000000aB
    amount = 5 * 10**18
    raw = {"data": "0x" + _word(addr_int) + _word(amount)}
    to_addr, decoded_amount = bot.decode_withdrawn(raw)
    assert decoded_amount == amount
    assert to_addr.lower().endswith("ab")


def test_find_funding_amount_matches_transfer():
    token = "0x" + "1" * 40
    distributor = "0x" + "2" * 40
    logs = [{
        "address": token,
        "topics": [
            bot.TRANSFER_TOPIC,
            "0x" + "0" * 64,
            "0x" + "0" * 24 + distributor[2:],
        ],
        "data": "0x" + _word(777),
    }]
    assert bot.find_funding_amount(logs, token, distributor) == 777


def test_find_funding_amount_none_when_no_match():
    token = "0x" + "1" * 40
    distributor = "0x" + "2" * 40
    assert bot.find_funding_amount([], token, distributor) is None


# --- message splitting ------------------------------------------------------

def test_split_message_short_is_single_chunk():
    assert bot._split_message("hello") == ["hello"]


def test_split_message_respects_limit():
    text = "\n".join(f"line {i}" for i in range(500))
    chunks = bot._split_message(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    # No data lost: rejoining yields the same set of lines.
    assert "\n".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_split_message_hard_splits_long_line():
    text = "x" * 250
    chunks = bot._split_message(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


# --- threshold formatting ---------------------------------------------------

def test_format_threshold_trims_zeros():
    assert bot.format_threshold(Decimal("1000")) == "1,000"
    assert bot.format_threshold(Decimal("1000.50")) == "1,000.5"
