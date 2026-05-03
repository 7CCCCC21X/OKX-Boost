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
            "  /check &lt;chain&gt; &lt;tx_hash&gt; — check whether a tx hit DistributorCreated. "
            "Available chains: <code>{chains}</code>\n"
            "  /activate [chat_id] — start broadcasting alerts in this chat (or to a given chat_id)\n"
            "  /deactivate [chat_id] — stop broadcasting alerts in this chat\n"
            "  /subs — list all chats receiving alerts\n"
            "  /preview [chain] — send a sample alert (with footer buttons) here for testing\n"
            "  /interval [value] — show or change the poll interval (e.g. 3m, 30s)\n"
            "  /status — bot status (per-chain)\n"
            "  /lang — switch language\n"
            "  /id — your Telegram id and chat id\n"
            "  /help — this help\n"
            "\n"
            "💡 Subscribers receive alerts from every monitored chain; each alert is labeled with its chain."
        ),
        "menu_title": (
            "<b>Menu</b> — choose an action:\n"
            "<i>Watching chains: {chains}</i>"
        ),
        "menu_check_hint": (
            "Just paste a tx hash and tap a chain on the picker — easiest way.\n"
            "Or type <code>/check &lt;chain&gt; &lt;tx_hash&gt;</code> to skip the picker.\n"
            "Available chains: <code>{chains}</code>\n"
            "Example: <code>/check bsc 0x72e7b61f8ac3415468fbabeaea3e215bf86cc8ee6a1ace096091883fe001d2d1</code>"
        ),
        "btn_status": "📊 Status",
        "btn_help": "❓ Help",
        "btn_lang": "🌐 Language",
        "btn_check": "🔍 How to /check",
        "btn_interval": "⏱ Interval",
        "btn_close": "✖️ Close",
        "btn_cancel": "✖️ Cancel",

        # Language switcher
        "lang_choose": "Pick your language:",
        "lang_set": "✅ Language set to English.",

        # Broadcast alert (compact format used when an event fires)
        "broadcast_alert": (
            "🚀 <b>[{chain_name}] OKX Boost — new token</b>\n\n"
            "🪙 <b>Token:</b> {token_name} ({token_symbol})\n"
            "💰 <b>Amount:</b> {amount}\n"
            "📜 <b>Contract:</b> <code>{token_contract}</code>\n"
            "⏱ <b>Time:</b> {time}\n"
            "🔗 <a href=\"{tx_url}\">View transaction</a>"
        ),
        "timeset_alert": (
            "⏰ <b>[{chain_name}] Claim time set</b>\n\n"
            "🪙 <b>Token:</b> {token_name} ({token_symbol})\n"
            "💰 <b>Amount:</b> {amount}\n"
            "📜 <b>Contract:</b> <code>{token_contract}</code>\n"
            "🎯 <b>Distributor:</b> <code>{distributor}</code>\n"
            "🟢 <b>Start:</b> {start_time}\n"
            "🔴 <b>End:</b> {end_time}\n"
            "🔗 <a href=\"{tx_url}\">View transaction</a>"
        ),
        "timeset_hit": (
            "✅ <b>[{chain_name}] HIT — Claim time set</b>\n\n"
            "🪙 <b>Token:</b> {token_name} ({token_symbol})\n"
            "💰 <b>Amount:</b> {amount}\n"
            "📜 <b>Contract:</b> <code>{token_contract}</code>\n"
            "🎯 <b>Distributor:</b> <code>{distributor}</code>\n"
            "🟢 <b>Start:</b> {start_time}\n"
            "🔴 <b>End:</b> {end_time}\n"
            "🔗 <a href=\"{tx_url}\">View transaction</a>"
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
            "❌ Transaction not found on [{chain_name}]: <code>{tx}</code>\n"
            "RPC reports: chain id <code>{chain}</code>, head block <code>{head}</code>\n"
            "\n"
            "Likely reasons:\n"
            "• You picked the wrong chain — try a different one "
            "(BSC=56, Ethereum=1, Polygon=137, Arbitrum=42161, Base=8453)\n"
            "• The RPC node hasn't synced this block yet — try again in a moment\n"
            "• Free public RPCs sometimes drop recent receipts under load"
        ),
        "check_pending": "⏳ Pending or unknown: <code>{tx}</code>",
        "check_reverted": (
            "⚪ <b>Not a hit</b> — transaction reverted.\n"
            "<a href=\"{url}\">{tx}</a>"
        ),
        "check_no_event": (
            "⚪ <b>Not a hit</b> on [{chain_name}] — no DistributorCreated (from "
            "<code>{factory}</code>) or TimeSet event in this tx.\n"
            "<a href=\"{url}\">{tx}</a>"
        ),
        "check_usage": "Usage: <code>/check &lt;chain&gt; &lt;tx_hash&gt;</code>\nOr paste a bare tx hash and tap a chain.\nAvailable chains: <code>{chains}</code>",
        "check_unknown_chain": "❌ Unknown chain <code>{chain}</code>. Available: <code>{chains}</code>",
        "check_pick_chain": (
            "<b>Pick a chain to query:</b>\n"
            "<code>{tx}</code>"
        ),
        "check_expired": "⌛ Picker expired — please resend the tx hash.",
        "check_canceled": "Canceled",
        "checking": "Querying on {chain}…",
        "rpc_error": "⚠️ RPC error: {err}",

        # /status
        "status_title": "<b>Bot status</b>",
        "status_factory": "Factory",
        "status_chain": "Chain ID",
        "status_chains": "Chains",
        "status_head": "Head block",
        "status_last_processed": "Last processed",
        "status_distributors": "Distributors tracked",
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

        # /activate /deactivate /subs
        "activate_success": "✅ Alerts activated for chat <code>{chat}</code>.",
        "activate_already": "ℹ️ Chat <code>{chat}</code> is already activated.",
        "activate_invalid": "❌ Invalid chat id. Usage: <code>/activate</code> or <code>/activate &lt;chat_id&gt;</code>.",
        "deactivate_success": "✅ Alerts deactivated for chat <code>{chat}</code>.",
        "deactivate_not_active": "ℹ️ Chat <code>{chat}</code> wasn't activated.",
        "subs_title": "<b>Broadcast targets</b>",
        "subs_default_header": "Default:",
        "subs_default": "Default: {chat}",
        "subs_extra": "Activated chats:",
        "subs_empty": "No additional chats activated.",
        "preview_label": (
            "🔧 <i>预览测试 — 示例数据,非真实事件 / "
            "Preview test — sample data, not a real event</i>"
        ),
        "preview_unknown_chain": "❌ Unknown chain <code>{chain}</code>. Available: <code>{chains}</code>",

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
            "  /check &lt;链&gt; &lt;交易哈希&gt; — 检查交易是否命中 DistributorCreated。"
            "可用链: <code>{chains}</code>\n"
            "  /activate [chat_id] — 在当前聊天激活推送(或为指定 chat_id)\n"
            "  /deactivate [chat_id] — 停用当前聊天的推送\n"
            "  /subs — 查看所有接收推送的聊天\n"
            "  /preview [链] — 发送示例推送(含底部按钮)到当前聊天用于测试\n"
            "  /interval [值] — 查看或修改查询频率(如 3m、30s)\n"
            "  /status — 查看机器人状态(按链分段)\n"
            "  /lang — 切换语言\n"
            "  /id — 我的 Telegram ID 和 Chat ID\n"
            "  /help — 显示帮助\n"
            "\n"
            "💡 订阅者会收到所有监听链的推送,每条消息都带链标签。"
        ),
        "menu_title": (
            "<b>菜单</b> — 请选择操作:\n"
            "<i>正在监听: {chains}</i>"
        ),
        "menu_check_hint": (
            "直接粘贴交易哈希,然后在弹出的卡片上点击链 — 最方便。\n"
            "或者输入 <code>/check &lt;链&gt; &lt;交易哈希&gt;</code> 跳过卡片。\n"
            "可用链: <code>{chains}</code>\n"
            "示例: <code>/check bsc 0x72e7b61f8ac3415468fbabeaea3e215bf86cc8ee6a1ace096091883fe001d2d1</code>"
        ),
        "btn_status": "📊 状态",
        "btn_help": "❓ 帮助",
        "btn_lang": "🌐 语言",
        "btn_check": "🔍 如何使用 /check",
        "btn_interval": "⏱ 查询频率",
        "btn_close": "✖️ 关闭",
        "btn_cancel": "✖️ 取消",

        # Language switcher
        "lang_choose": "请选择语言:",
        "lang_set": "✅ 已切换为中文。",

        # Broadcast alert (compact format used when an event fires)
        "broadcast_alert": (
            "🚀 <b>[{chain_name}] OKX Boost 新增代币</b>\n\n"
            "🪙 <b>代币:</b> {token_name} ({token_symbol})\n"
            "💰 <b>数量:</b> {amount}\n"
            "📜 <b>合约:</b> <code>{token_contract}</code>\n"
            "⏱ <b>时间:</b> {time}\n"
            "🔗 <a href=\"{tx_url}\">查看交易</a>"
        ),
        "timeset_alert": (
            "⏰ <b>[{chain_name}] 设置领取时间</b>\n\n"
            "🪙 <b>代币:</b> {token_name} ({token_symbol})\n"
            "💰 <b>数量:</b> {amount}\n"
            "📜 <b>合约:</b> <code>{token_contract}</code>\n"
            "🎯 <b>Distributor:</b> <code>{distributor}</code>\n"
            "🟢 <b>开始:</b> {start_time}\n"
            "🔴 <b>结束:</b> {end_time}\n"
            "🔗 <a href=\"{tx_url}\">查看交易</a>"
        ),
        "timeset_hit": (
            "✅ <b>[{chain_name}] 命中 — 设置领取时间</b>\n\n"
            "🪙 <b>代币:</b> {token_name} ({token_symbol})\n"
            "💰 <b>数量:</b> {amount}\n"
            "📜 <b>合约:</b> <code>{token_contract}</code>\n"
            "🎯 <b>Distributor:</b> <code>{distributor}</code>\n"
            "🟢 <b>开始:</b> {start_time}\n"
            "🔴 <b>结束:</b> {end_time}\n"
            "🔗 <a href=\"{tx_url}\">查看交易</a>"
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
            "❌ [{chain_name}] 找不到此交易: <code>{tx}</code>\n"
            "当前 RPC: 链 ID <code>{chain}</code>,最新区块 <code>{head}</code>\n"
            "\n"
            "可能原因:\n"
            "• 选错链了,试试别的 "
            "(BSC=56,Ethereum=1,Polygon=137,Arbitrum=42161,Base=8453)\n"
            "• RPC 节点尚未同步到该区块,稍后重试\n"
            "• 免费公共 RPC 在高负载下偶尔会返回不到最近的收据"
        ),
        "check_pending": "⏳ 交易待确认或未知: <code>{tx}</code>",
        "check_reverted": (
            "⚪ <b>未命中</b> — 交易已 revert。\n"
            "<a href=\"{url}\">{tx}</a>"
        ),
        "check_no_event": (
            "⚪ <b>[{chain_name}] 未命中</b> — 此交易中未找到来自 "
            "<code>{factory}</code> 的 DistributorCreated 事件,也没有 TimeSet 事件。\n"
            "<a href=\"{url}\">{tx}</a>"
        ),
        "check_usage": "用法: <code>/check &lt;链&gt; &lt;交易哈希&gt;</code>\n或直接粘贴交易哈希,然后点击想查询的链。\n可用链: <code>{chains}</code>",
        "check_unknown_chain": "❌ 未知链 <code>{chain}</code>。可用: <code>{chains}</code>",
        "check_pick_chain": (
            "<b>请选择查询链:</b>\n"
            "<code>{tx}</code>"
        ),
        "check_expired": "⌛ 卡片已过期,请重新发送交易哈希。",
        "check_canceled": "已取消",
        "checking": "正在 {chain} 上查询…",
        "rpc_error": "⚠️ RPC 错误: {err}",

        # /status
        "status_title": "<b>机器人状态</b>",
        "status_factory": "工厂合约",
        "status_chain": "链 ID",
        "status_chains": "监听链",
        "status_head": "最新区块",
        "status_last_processed": "已处理至",
        "status_distributors": "已记录 distributor",
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

        # /activate /deactivate /subs
        "activate_success": "✅ 已为聊天 <code>{chat}</code> 激活推送。",
        "activate_already": "ℹ️ 聊天 <code>{chat}</code> 已经激活。",
        "activate_invalid": "❌ 无效的 chat id。用法:<code>/activate</code> 或 <code>/activate &lt;chat_id&gt;</code>。",
        "deactivate_success": "✅ 已为聊天 <code>{chat}</code> 停用推送。",
        "deactivate_not_active": "ℹ️ 聊天 <code>{chat}</code> 之前未激活。",
        "subs_title": "<b>推送目标</b>",
        "subs_default_header": "默认:",
        "subs_default": "默认: {chat}",
        "subs_extra": "已激活聊天:",
        "subs_empty": "没有额外激活的聊天。",
        "preview_label": (
            "🔧 <i>预览测试 — 示例数据,非真实事件 / "
            "Preview test — sample data, not a real event</i>"
        ),
        "preview_unknown_chain": "❌ 未知链 <code>{chain}</code>。可用: <code>{chains}</code>",

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
