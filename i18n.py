"""Bilingual (zh / en) translation table.

`t(lang, key, **kwargs)` returns the localized string with `kwargs`
substituted via `str.format`. Missing keys fall back to English.
"""
from __future__ import annotations

LANGS = ("zh", "en")
DEFAULT_FALLBACK = "en"

LANG_LABEL = {"zh": "🇨🇳 中文", "en": "🇺🇸 English"}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        # Help / menu
        "help": (
            "<b>Distributor Monitor Bot</b>\n"
            "Commands:\n"
            "  /menu — show interactive menu\n"
            "  /check &lt;tx_hash&gt; — check whether a tx hit DistributorCreated\n"
            "  /status — bot status\n"
            "  /lang — switch language\n"
            "  /id — your Telegram id and chat id\n"
            "  /help — this help"
        ),
        "menu_title": "<b>Menu</b> — choose an action:",
        "menu_check_hint": (
            "Send <code>/check &lt;tx_hash&gt;</code> to check a transaction.\n"
            "Example: <code>/check 0x72e7b61f8ac3415468fbabeaea3e215bf86cc8ee6a1ace096091883fe001d2d1</code>"
        ),
        "btn_status": "📊 Status",
        "btn_help": "❓ Help",
        "btn_lang": "🌐 Language",
        "btn_check": "🔍 How to /check",
        "btn_close": "✖️ Close",

        # Language switcher
        "lang_choose": "Pick your language:",
        "lang_set": "✅ Language set to English.",

        # Alerts (broadcast)
        "alert_title": "🚀 <b>New Distributor Deployed</b>",
        "hit_title": "✅ <b>HIT — DistributorCreated</b>",
        "field_token": "Token",
        "field_token_contract": "Token Contract",
        "field_amount": "Amount",
        "field_distributor": "Distributor",
        "field_owner": "Owner",
        "field_operator": "Operator",
        "field_block": "Block",
        "field_tx": "Tx",
        "no_funding": "(no funding transfer in this tx)",

        # /check
        "check_invalid": "❌ Invalid transaction hash. Expected format: 0x + 64 hex chars.",
        "check_not_found": "❌ Transaction not found: <code>{tx}</code>",
        "check_pending": "⏳ Pending or unknown: <code>{tx}</code>",
        "check_reverted": (
            "⚪ <b>Not a hit</b> — transaction reverted.\n"
            "<a href=\"{url}\">{tx}</a>"
        ),
        "check_no_event": (
            "⚪ <b>Not a hit</b> — no DistributorCreated event from "
            "<code>{factory}</code> in this tx.\n"
            "<a href=\"{url}\">{tx}</a>"
        ),
        "check_usage": "Usage: <code>/check &lt;tx_hash&gt;</code>",
        "rpc_error": "⚠️ RPC error: {err}",

        # /status
        "status_title": "<b>Bot status</b>",
        "status_factory": "Factory",
        "status_chain": "Chain ID",
        "status_head": "Head block",
        "status_last_processed": "Last processed",
        "status_uptime": "Uptime",
        "status_whitelist": "Whitelist size",
        "status_open": "OPEN",
        "status_lang": "Your language",

        # /id
        "id_title": "<b>Your IDs</b>",
        "id_label": "User id",
        "chat_label": "Chat id",

        # Misc
        "unknown_cmd": "Unknown command. Try /help.",
        "callback_acknowledged": "Done",
    },
    "zh": {
        # Help / menu
        "help": (
            "<b>Distributor 监听机器人</b>\n"
            "命令:\n"
            "  /menu — 显示交互式菜单\n"
            "  /check &lt;交易哈希&gt; — 检查交易是否命中 DistributorCreated\n"
            "  /status — 查看机器人状态\n"
            "  /lang — 切换语言\n"
            "  /id — 我的 Telegram ID 和 Chat ID\n"
            "  /help — 显示帮助"
        ),
        "menu_title": "<b>菜单</b> — 请选择操作:",
        "menu_check_hint": (
            "发送 <code>/check &lt;交易哈希&gt;</code> 检查交易。\n"
            "示例: <code>/check 0x72e7b61f8ac3415468fbabeaea3e215bf86cc8ee6a1ace096091883fe001d2d1</code>"
        ),
        "btn_status": "📊 状态",
        "btn_help": "❓ 帮助",
        "btn_lang": "🌐 语言",
        "btn_check": "🔍 如何使用 /check",
        "btn_close": "✖️ 关闭",

        # Language switcher
        "lang_choose": "请选择语言:",
        "lang_set": "✅ 已切换为中文。",

        # Alerts (broadcast)
        "alert_title": "🚀 <b>检测到新 Distributor 部署</b>",
        "hit_title": "✅ <b>命中 — DistributorCreated</b>",
        "field_token": "代币",
        "field_token_contract": "代币合约",
        "field_amount": "数量",
        "field_distributor": "Distributor 地址",
        "field_owner": "Owner",
        "field_operator": "Operator",
        "field_block": "区块",
        "field_tx": "交易",
        "no_funding": "(本笔交易未发现转账)",

        # /check
        "check_invalid": "❌ 无效的交易哈希。格式应为 0x + 64 位十六进制字符。",
        "check_not_found": "❌ 找不到此交易: <code>{tx}</code>",
        "check_pending": "⏳ 交易待确认或未知: <code>{tx}</code>",
        "check_reverted": (
            "⚪ <b>未命中</b> — 交易已 revert。\n"
            "<a href=\"{url}\">{tx}</a>"
        ),
        "check_no_event": (
            "⚪ <b>未命中</b> — 此交易中未找到来自 "
            "<code>{factory}</code> 的 DistributorCreated 事件。\n"
            "<a href=\"{url}\">{tx}</a>"
        ),
        "check_usage": "用法: <code>/check &lt;交易哈希&gt;</code>",
        "rpc_error": "⚠️ RPC 错误: {err}",

        # /status
        "status_title": "<b>机器人状态</b>",
        "status_factory": "工厂合约",
        "status_chain": "链 ID",
        "status_head": "最新区块",
        "status_last_processed": "已处理至",
        "status_uptime": "运行时长",
        "status_whitelist": "白名单数量",
        "status_open": "未启用",
        "status_lang": "当前语言",

        # /id
        "id_title": "<b>你的 ID</b>",
        "id_label": "用户 ID",
        "chat_label": "聊天 ID",

        # Misc
        "unknown_cmd": "未知命令,请试试 /help。",
        "callback_acknowledged": "已处理",
    },
}


def t(lang: str, key: str, **kwargs: object) -> str:
    table = TRANSLATIONS.get(lang) or TRANSLATIONS[DEFAULT_FALLBACK]
    template = table.get(key)
    if template is None:
        template = TRANSLATIONS[DEFAULT_FALLBACK].get(key, key)
    return template.format(**kwargs) if kwargs else template
