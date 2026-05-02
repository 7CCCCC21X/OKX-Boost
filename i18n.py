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
            "  /interval [value] — show or change the poll interval (e.g. 3m, 30s)\n"
            "  /status — bot status\n"
            "  /lang — switch language\n"
            "  /id — your Telegram id and chat id\n"
            "  /help — this help\n"
            "\n"
            "💡 You can also just paste a tx hash — no /check prefix needed."
        ),
        "menu_title": "<b>Menu</b> — choose an action:",
        "menu_check_hint": (
            "Just paste a tx hash and the bot will check it — "
            "the <code>/check</code> prefix is optional.\n"
            "Example: <code>0x72e7b61f8ac3415468fbabeaea3e215bf86cc8ee6a1ace096091883fe001d2d1</code>"
        ),
        "btn_status": "📊 Status",
        "btn_help": "❓ Help",
        "btn_lang": "🌐 Language",
        "btn_check": "🔍 How to /check",
        "btn_interval": "⏱ Interval",
        "btn_close": "✖️ Close",

        # Language switcher
        "lang_choose": "Pick your language:",
        "lang_set": "✅ Language set to English.",

        # Broadcast alert (compact format used when an event fires)
        "broadcast_alert": (
            "OKX Boost factory: new token\n"
            "Token: {token_name} ({token_symbol})\n"
            "Token Contract: {token_contract}\n"
            "Amount: {amount}\n"
            "Transaction:\n"
            "{tx_url}\n"
            "Time: {time}"
        ),
        "timeset_alert": (
            "⏰ Claim time set\n"
            "Token: {token_name} ({token_symbol})\n"
            "Token Contract: {token_contract}\n"
            "Distributor: {distributor}\n"
            "Start: {start_time}\n"
            "End: {end_time}\n"
            "Transaction:\n"
            "{tx_url}"
        ),
        "timeset_hit": (
            "✅ <b>HIT — Claim time set</b>\n"
            "Token: {token_name} ({token_symbol})\n"
            "Token Contract: {token_contract}\n"
            "Distributor: {distributor}\n"
            "Start: {start_time}\n"
            "End: {end_time}\n"
            "Transaction:\n"
            "{tx_url}"
        ),
        "timeset_unknown_token": "Unknown",
        # /check titles still use the old detailed format below
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
        "check_not_found": (
            "❌ Transaction not found: <code>{tx}</code>\n"
            "RPC reports: chain id <code>{chain}</code>, head block <code>{head}</code>\n"
            "\n"
            "Likely reasons:\n"
            "• Your RPC is on a different chain than the tx "
            "(BSC=56, Ethereum=1, Polygon=137, Arbitrum=42161)\n"
            "• The RPC node hasn't synced this block yet — try again in a moment\n"
            "• Free public RPCs sometimes drop recent receipts under load"
        ),
        "check_pending": "⏳ Pending or unknown: <code>{tx}</code>",
        "check_reverted": (
            "⚪ <b>Not a hit</b> — transaction reverted.\n"
            "<a href=\"{url}\">{tx}</a>"
        ),
        "check_no_event": (
            "⚪ <b>Not a hit</b> — no DistributorCreated (from "
            "<code>{factory}</code>) or TimeSet event in this tx.\n"
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
        "status_interval": "Poll interval",
        "status_min_amount": "Min token amount",
        "status_whitelist": "Whitelist size",
        "status_open": "OPEN",
        "status_lang": "Your language",

        # /interval
        "interval_current": (
            "<b>Poll interval</b>\n"
            "Current: <b>{pretty}</b> ({seconds}s)\n"
            "Allowed range: {min} – {max}\n"
            "\n"
            "Pick a preset below, or send "
            "<code>/interval &lt;value&gt;</code> "
            "(e.g. <code>/interval 3m</code>)."
        ),
        "interval_set": "✅ Poll interval set to <b>{pretty}</b> ({seconds}s).",
        "interval_invalid": (
            "❌ Invalid value. Examples: <code>30s</code>, <code>3m</code>, "
            "<code>1h</code>, or a plain number of seconds.\n"
            "Allowed range: {min} – {max}."
        ),
        "interval_out_of_range": (
            "❌ Value out of range. Allowed: {min} – {max}."
        ),

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
            "  /interval [值] — 查看或修改查询频率(如 3m、30s)\n"
            "  /status — 查看机器人状态\n"
            "  /lang — 切换语言\n"
            "  /id — 我的 Telegram ID 和 Chat ID\n"
            "  /help — 显示帮助\n"
            "\n"
            "💡 直接发送交易哈希即可检查,无需 /check 前缀。"
        ),
        "menu_title": "<b>菜单</b> — 请选择操作:",
        "menu_check_hint": (
            "直接粘贴交易哈希即可检查,<code>/check</code> 前缀可省略。\n"
            "示例: <code>0x72e7b61f8ac3415468fbabeaea3e215bf86cc8ee6a1ace096091883fe001d2d1</code>"
        ),
        "btn_status": "📊 状态",
        "btn_help": "❓ 帮助",
        "btn_lang": "🌐 语言",
        "btn_check": "🔍 如何使用 /check",
        "btn_interval": "⏱ 查询频率",
        "btn_close": "✖️ 关闭",

        # Language switcher
        "lang_choose": "请选择语言:",
        "lang_set": "✅ 已切换为中文。",

        # Broadcast alert (compact format used when an event fires)
        "broadcast_alert": (
            "OKX Boost合约地址 新增代币\n"
            "代币: {token_name} ({token_symbol})\n"
            "代币合约: {token_contract}\n"
            "数量: {amount}\n"
            "交易哈希:\n"
            "{tx_url}\n"
            "时间: {time}"
        ),
        "timeset_alert": (
            "⏰ 设置领取时间\n"
            "代币: {token_name} ({token_symbol})\n"
            "代币合约: {token_contract}\n"
            "Distributor: {distributor}\n"
            "开始时间: {start_time}\n"
            "结束时间: {end_time}\n"
            "交易哈希:\n"
            "{tx_url}"
        ),
        "timeset_hit": (
            "✅ <b>命中 — 设置领取时间</b>\n"
            "代币: {token_name} ({token_symbol})\n"
            "代币合约: {token_contract}\n"
            "Distributor: {distributor}\n"
            "开始时间: {start_time}\n"
            "结束时间: {end_time}\n"
            "交易哈希:\n"
            "{tx_url}"
        ),
        "timeset_unknown_token": "未知",
        # /check 详细格式仍保留下面的标题
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
        "check_not_found": (
            "❌ 找不到此交易: <code>{tx}</code>\n"
            "当前 RPC: 链 ID <code>{chain}</code>,最新区块 <code>{head}</code>\n"
            "\n"
            "可能原因:\n"
            "• RPC 与交易所在链不一致 "
            "(BSC=56,Ethereum=1,Polygon=137,Arbitrum=42161)\n"
            "• RPC 节点尚未同步到该区块,稍后重试\n"
            "• 免费公共 RPC 在高负载下偶尔会返回不到最近的收据"
        ),
        "check_pending": "⏳ 交易待确认或未知: <code>{tx}</code>",
        "check_reverted": (
            "⚪ <b>未命中</b> — 交易已 revert。\n"
            "<a href=\"{url}\">{tx}</a>"
        ),
        "check_no_event": (
            "⚪ <b>未命中</b> — 此交易中未找到来自 "
            "<code>{factory}</code> 的 DistributorCreated 事件,也没有 TimeSet 事件。\n"
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
        "status_interval": "查询频率",
        "status_min_amount": "最低代币数量",
        "status_whitelist": "白名单数量",
        "status_open": "未启用",
        "status_lang": "当前语言",

        # /interval
        "interval_current": (
            "<b>查询频率</b>\n"
            "当前: <b>{pretty}</b>({seconds} 秒)\n"
            "允许范围: {min} – {max}\n"
            "\n"
            "在下方选择预设,或发送 "
            "<code>/interval &lt;值&gt;</code>"
            "(如 <code>/interval 3m</code>)。"
        ),
        "interval_set": "✅ 查询频率已设为 <b>{pretty}</b>({seconds} 秒)。",
        "interval_invalid": (
            "❌ 无效的值。示例:<code>30s</code>、<code>3m</code>、"
            "<code>1h</code>,或纯数字(秒)。\n"
            "允许范围: {min} – {max}。"
        ),
        "interval_out_of_range": (
            "❌ 超出允许范围。允许: {min} – {max}。"
        ),

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
