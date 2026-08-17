"""Telegram alert formatting + dispatch for the Crypto Alert & Paper-Trading Engine.

All dynamic strings are html.escape()'d before insertion into HTML-parsed Telegram messages.
Outbound calls reuse analytics_engine's exponential backoff decorator for 429/5xx resilience.
"""
import html
import logging
import os

import requests

from analytics_engine import with_retry_backoff

TELEGRAM_BOT_TOKEN = os.environ.get("CRYPTO_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CRYPTO_TELEGRAM_CHAT_ID")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


@with_retry_backoff(max_retries=3, base_delay=1.0)
def _post_telegram(method: str, payload: dict) -> requests.Response:
    return requests.post(f"{TELEGRAM_API_BASE}/{method}", json=payload, timeout=10)


def send_telegram_message(text: str, chat_id: str = None, parse_mode: str = "HTML") -> bool:
    if not TELEGRAM_BOT_TOKEN:
        logging.error("CRYPTO_TELEGRAM_BOT_TOKEN not set; cannot send Telegram message.")
        return False
    target_chat = chat_id or TELEGRAM_CHAT_ID
    if not target_chat:
        logging.error("No Telegram chat_id available (CRYPTO_TELEGRAM_CHAT_ID unset and none provided).")
        return False
    try:
        resp = _post_telegram("sendMessage", {
            "chat_id": target_chat, "text": text, "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        })
        return resp.status_code == 200
    except Exception as e:
        logging.error(f"send_telegram_message failed: {e}")
        return False


def format_signal_alert_card(signal: dict) -> str:
    symbol = html.escape(str(signal.get("symbol", "UNKNOWN")))
    context_summary = html.escape(str(signal.get("context_summary", "N/A")))
    lines = [
        f"🚨 <b>BREAKOUT SIGNAL: {symbol}</b>",
        "",
        f"🎯 <b>Confidence Score:</b> {signal.get('confidence_score', 0)}/100",
        f"🧊 <b>Squeeze Context:</b> {context_summary}",
        f"📊 <b>RVOL:</b> {signal.get('rvol_ratio', 0.0):.2f}x (200% gate)",
        f"💰 <b>Entry (approx):</b> {signal.get('current_price', 0.0):.6g}",
        f"🛑 <b>Stop-Loss:</b> {signal.get('stop_loss', 0.0):.6g} (1.5x ATR)",
        f"🎯 <b>Take-Profit:</b> {signal.get('take_profit', 0.0):.6g} (1:3 R/R)",
        "",
        "⚡ Tap to copy paper trade command:",
        f"<code>/paper_buy {symbol} 2%</code>",
    ]
    return "\n".join(lines)


def format_trade_closed_alert(trade: dict) -> str:
    symbol = html.escape(str(trade.get("symbol", "UNKNOWN")))
    status = str(trade.get("status", "CLOSED"))
    outcome_emoji = "✅" if status == "CLOSED_TP" else ("🛑" if status == "CLOSED_SL" else "⚪")
    lines = [
        f"{outcome_emoji} <b>PAPER TRADE CLOSED: {symbol}</b>",
        "",
        f"<b>Outcome:</b> {html.escape(status.replace('_', ' '))}",
        f"<b>Entry:</b> {trade.get('entry_price', 0.0):.6g}",
        f"<b>Exit:</b> {trade.get('close_price', 0.0):.6g}",
        f"<b>PnL:</b> {trade.get('pnl_pct', 0.0):+.2f}% ({trade.get('pnl_usd', 0.0):+.2f} USD)",
    ]
    return "\n".join(lines)


def format_paper_buy_confirmation(trade: dict) -> str:
    symbol = html.escape(str(trade.get("symbol", "?")))
    lines = [
        f"✅ <b>Paper Trade Opened: {symbol}</b>",
        "",
        f"<b>Entry:</b> {trade.get('entry_price', 0.0):.6g}",
        f"<b>Quantity:</b> {trade.get('quantity', 0.0):.6g}",
        f"<b>Risk:</b> {trade.get('risk_pct', 0.0):.1f}% of virtual balance",
        f"<b>Stop-Loss:</b> {trade.get('stop_loss', 0.0):.6g}",
        f"<b>Take-Profit:</b> {trade.get('take_profit', 0.0):.6g}",
        f"<b>Trade ID:</b> #{trade.get('id', '?')}",
    ]
    return "\n".join(lines)


def format_portfolio_summary_card(summary: dict) -> str:
    lines = [
        "📈 <b>Paper Portfolio Summary</b>",
        "",
        f"💵 <b>Virtual Balance:</b> {summary.get('virtual_balance_usd', 0.0):.2f} USD",
        f"📂 <b>Open Positions:</b> {summary.get('open_count', 0)}",
        f"📊 <b>Realized PnL (all-time):</b> {summary.get('realized_pnl_usd', 0.0):+.2f} USD",
        f"🏆 <b>Win Rate:</b> {summary.get('win_rate_pct', 0.0):.1f}% ({summary.get('wins', 0)}W / {summary.get('losses', 0)}L)",
        "",
    ]
    open_positions = summary.get("open_positions", [])
    if open_positions:
        lines.append("<b>Open Positions:</b>")
        for pos in open_positions:
            symbol = html.escape(str(pos.get("symbol", "?")))
            lines.append(
                f"• <code>{symbol}</code> @ {pos.get('entry_price', 0.0):.6g} "
                f"-> {pos.get('unrealized_pnl_pct', 0.0):+.2f}% "
                f"(SL {pos.get('stop_loss', 0.0):.6g} / TP {pos.get('take_profit', 0.0):.6g})"
            )
    else:
        lines.append("No open positions.")
    return "\n".join(lines)


def dispatch_signal_alert(signal: dict) -> bool:
    return send_telegram_message(format_signal_alert_card(signal))


def dispatch_trade_closed_alert(trade: dict) -> bool:
    return send_telegram_message(format_trade_closed_alert(trade))
