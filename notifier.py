"""
Pushover push notifications for real (live) trade events.

Only fires when PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN are set in config.
Dry-run orders are never notified — this is intentionally live-only.
"""

import requests
import config

PUSHOVER_API = "https://api.pushover.net/1/messages.json"

ENABLED = bool(config.PUSHOVER_USER_KEY and config.PUSHOVER_APP_TOKEN)


def send(title: str, message: str, priority: int = 0) -> bool:
    """
    Send a Pushover notification. Returns True on success.
    priority: -1 = quiet, 0 = normal, 1 = high (bypasses quiet hours)
    """
    if not ENABLED or not config.PUSHOVER_USER_KEY or not config.PUSHOVER_APP_TOKEN:
        return False
    try:
        resp = requests.post(
            PUSHOVER_API,
            data={
                "token":    config.PUSHOVER_APP_TOKEN,
                "user":     config.PUSHOVER_USER_KEY,
                "title":    title,
                "message":  message,
                "priority": priority,
            },
            timeout=6,
        )
        return resp.status_code == 200
    except Exception:
        return False


def notify_buy(ticker: str, player: str, side: str, contracts: int,
               price_cents: int, cost_usd: float, edge: float) -> bool:
    return send(
        "Kalshi — BUY executed",
        f"{player}\n"
        f"{contracts} {side.upper()} @ {price_cents}¢  cost=${cost_usd:.2f}\n"
        f"Edge: {edge:+.1%}  [{ticker}]",
    )


def notify_sell(ticker: str, side: str, contracts: int,
                entry_cents: int, exit_cents: int,
                pnl_usd: float, reason: str) -> bool:
    reason_labels = {
        "stop_loss":   "STOP-LOSS",
        "profit_take": "PROFIT-TAKE",
        "manual":      "MANUAL SELL",
    }
    label    = reason_labels.get(reason, reason.upper())
    pnl_sign = "+" if pnl_usd >= 0 else ""
    priority = 1 if reason == "stop_loss" else 0
    return send(
        f"Kalshi — {label}",
        f"{ticker}\n"
        f"{contracts} {side.upper()}  entry={entry_cents}¢ → exit={exit_cents}¢\n"
        f"P&L: {pnl_sign}${pnl_usd:.2f}",
        priority=priority,
    )


def notify_error(context: str, error: str) -> bool:
    return send(
        "Kalshi — Scanner error",
        f"{context}: {error}",
        priority=-1,
    )
