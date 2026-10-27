#!/usr/bin/env python3
"""Omni-Channel Real-Time Push Notifier (Telegram & Discord).
Standard library only. No third-party packages required.

Dispatches instant alerts for:
  - Setup Triggers (Wyckoff Spring, Upthrust, EMA Retest)
  - Execution Fills (Entry, TP1 Scale-Out, TP2 Scale-Out, Stop-Loss)
  - Daily Performance Summary (Equity, Net P&L, Win Rate, Open Positions)
  - System Watchdog & Reconnection Alerts

Configuration:
  Set environment variables or add to `trader/credentials.json`:
    TELEGRAM_BOT_TOKEN="123456789:ABCdef..."
    TELEGRAM_CHAT_ID="987654321"
    DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."

Usage:
  python trader/live/notifier.py test "Hello from Binance Institutional Trader!"
"""

import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
CREDENTIALS_FILE = os.path.join(ROOT, "trader", "credentials.json")


def load_config():
    cfg = {
        "telegram_token": os.getenv("TELEGRAM_BOT_TOKEN"),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID"),
        "discord_webhook": os.getenv("DISCORD_WEBHOOK_URL")
    }
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                cfg["telegram_token"] = cfg["telegram_token"] or data.get("telegram_bot_token")
                cfg["telegram_chat_id"] = cfg["telegram_chat_id"] or data.get("telegram_chat_id")
                cfg["discord_webhook"] = cfg["discord_webhook"] or data.get("discord_webhook_url")
        except Exception:
            pass
    return cfg


def send_telegram(message, token=None, chat_id=None):
    cfg = load_config()
    token = token or cfg["telegram_token"]
    chat_id = chat_id or cfg["telegram_chat_id"]
    if not token or not chat_id:
        return False, "Telegram credentials not configured"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return True, "Telegram message delivered"
    except Exception as e:
        return False, f"Telegram delivery error: {e}"


def send_discord(message, webhook_url=None):
    cfg = load_config()
    webhook_url = webhook_url or cfg["discord_webhook"]
    if not webhook_url:
        return False, "Discord webhook URL not configured"

    payload = {"content": message}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return True, "Discord message delivered"
    except Exception as e:
        return False, f"Discord delivery error: {e}"


def notify(title, message, is_urgent=False):
    """Dispatches message to all configured channels (Telegram & Discord)."""
    full_msg = f"<b>{title}</b>\n\n{message}"
    plain_msg = f"**{title}**\n\n{message}"

    tg_ok, tg_status = send_telegram(full_msg)
    dc_ok, dc_status = send_discord(plain_msg)

    return {
        "telegram": {"delivered": tg_ok, "status": tg_status},
        "discord": {"delivered": dc_ok, "status": dc_status}
    }


# ---------------------------------------------------------------- event notification helpers

def notify_setup_opened(p):
    title = f"🚀 BINANCE ORDER: {p['side'].upper()} {p['symbol']}"
    msg = (
        f"<b>Setup:</b> {p['setup']}\n"
        f"<b>Entry Price:</b> ${p['entry']:,.4f}\n"
        f"<b>Stop-Loss:</b> ${p['stop']:,.4f}\n"
        f"<b>TP1 (+1.8R):</b> ${p.get('tp1_px', 0):,.4f} (Scale 40%)\n"
        f"<b>TP2 (+3.5R):</b> ${p.get('tp2_px', 0):,.4f} (Scale 30%)\n"
        f"<b>Planned Risk:</b> ${p.get('risk_usd', 50):,.2f}\n"
        f"<b>Notional:</b> ${p.get('notional', 0):,.2f} ({p.get('margin', 0):,.2f} margin)"
    )
    return notify(title, msg)


def notify_scale_out(p, tier, exit_px, realized_pnl):
    title = f"🎯 TAKE PROFIT {tier}: {p['side'].upper()} {p['symbol']}"
    msg = (
        f"<b>Filled At:</b> ${exit_px:,.4f}\n"
        f"<b>Locked P&L:</b> +${realized_pnl:,.2f}\n"
        f"<b>Action:</b> Stop moved to Breakeven (${p['stop']:,.4f})\n"
        f"<b>Remaining Qty:</b> {p['qty']:.4f} (Runner active)"
    )
    return notify(title, msg)


def notify_position_closed(p, exit_px, net_pnl, r_mult, outcome):
    is_win = net_pnl >= 0
    title = f"{'🏆 TRADE WIN' if is_win else '🛑 STOP LOSS'}: {p['side'].upper()} {p['symbol']}"
    msg = (
        f"<b>Outcome:</b> {outcome}\n"
        f"<b>Exit Price:</b> ${exit_px:,.4f}\n"
        f"<b>Net P&L:</b> {'+' if is_win else ''}${net_pnl:,.2f} ({r_mult:+.2f}R)\n"
        f"<b>Setup:</b> {p['setup']}"
    )
    return notify(title, msg)


def notify_system_alert(level, message):
    title = f"⚠️ SYSTEM ALERT [{level.upper()}]"
    return notify(title, message, is_urgent=True)


if __name__ == "__main__":
    msg = sys.argv[1] if len(sys.argv) > 1 else "Binance Quant Trader Watchdog Test Notification."
    print("[*] Sending test push notification...")
    res = notify("BINANCE TRADER TEST", msg)
    print(f"[+] Result: Telegram: {res['telegram']['status']} | Discord: {res['discord']['status']}")
