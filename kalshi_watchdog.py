#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kalshi Watchdog — Hybrid position monitor.
Runs every 30 minutes via Task Scheduler. Free until something triggers.

Logic:
  1. Poll all open positions (no AI, no cost)
  2. If a position approaches stop-loss OR profit target threshold:
       → Call Claude with current market context
       → Claude decides: exit now, hold, or watch
  3. Execute Claude's decision
  4. Send Windows toast notification on any action

Cost: ~$0.02-0.05 only when a position moves significantly. Otherwise free.

Usage:
    python kalshi_watchdog.py
    python kalshi_watchdog.py --dry-run
    python kalshi_watchdog.py --status
"""

import os
import sys

# Force UTF-8 on Windows console BEFORE any logging setup
import io
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'buffer'):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
import json
import time
import base64
import sqlite3
import logging
import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import httpx
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# ===============================================================
# CONFIGURATION
# ===============================================================
BOT_DIR = Path(os.environ.get("KALSHI_BOT_DIR", str(Path(__file__).parent)))

KALSHI_API_KEY = os.environ.get("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY_PATH = os.environ.get(
    "KALSHI_PRIVATE_KEY_PATH",
    str(BOT_DIR / ".kalshi" / "private_key.pem"),
)
KALSHI_BASE_URL   = "https://api.elections.kalshi.com/trade-api/v2"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# --- ntfy.sh notification topic -------------------------------
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

# --- GitHub Gist status (for remote health checks) ------------
GIST_TOKEN = os.environ.get("GITHUB_GIST_TOKEN", "")
GIST_ID    = os.environ.get("GITHUB_GIST_ID", "")


def update_gist(content: str):
    """Overwrite status.txt in the configured gist. Best-effort."""
    if not (GIST_TOKEN and GIST_ID):
        return
    try:
        httpx.patch(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={
                "Authorization": f"token {GIST_TOKEN}",
                "Accept": "application/vnd.github+json",
            },
            json={"files": {"status.txt": {"content": content[:50000]}}},
            timeout=10,
        )
    except Exception:
        pass


def ntfy(title: str, message: str, priority: str = "default", tags: str = ""):
    """Post a status update to ntfy.sh. Best-effort, never blocks."""
    if not NTFY_TOPIC:
        return
    try:
        headers = {"Title": title[:200], "Priority": priority}
        if tags:
            headers["Tags"] = tags
        httpx.post(NTFY_URL, headers=headers, content=message[:4000].encode("utf-8"), timeout=5)
    except Exception:
        pass

# --- Trigger Thresholds ---------------------------------------
ALERT_STOP_LOSS_PCT  = 0.55   # Wake Claude if position drops to 55% of cost
ALERT_PROFIT_PCT     = 0.75   # Wake Claude if position reaches 75% of max payout
HARD_STOP_LOSS_PCT   = 0.35   # Emergency exit WITHOUT Claude if drops to 35%
MIN_DAYS_TO_HOLD     = 0.25   # Don't exit within 6h of resolution — let it ride

LOG_PATH = BOT_DIR / "kalshi_watchdog.log"
DB_PATH  = BOT_DIR / "kalshi_bot.db"

# ===============================================================
# LOGGING
# ===============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("watchdog")

# Force stdout to UTF-8 on Windows to handle box-drawing characters
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ===============================================================
# KALSHI API
# ===============================================================
_private_key = None


def _load_private_key():
    global _private_key
    if _private_key:
        return _private_key
    with open(os.path.expanduser(KALSHI_PRIVATE_KEY_PATH), "rb") as f:
        _private_key = serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )
    return _private_key


def _auth_headers(method, path):
    pk = _load_private_key()
    ts = str(int(time.time() * 1000))
    message = f"{ts}{method.upper()}{path}".encode("utf-8")
    sig = pk.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _api(method, path, params=None, body=None, auth=True):
    url = f"{KALSHI_BASE_URL}{path}"
    headers = _auth_headers(method, f"/trade-api/v2{path}") if auth else {"Accept": "application/json"}
    resp = httpx.request(method, url, headers=headers, params=params, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_balance():
    data = _api("GET", "/portfolio/balance")
    return round(data.get("balance", 0) / 100, 2)


def get_positions():
    data = _api("GET", "/portfolio/positions")
    raw = data.get("market_positions") or data.get("positions", [])
    result = []
    for p in raw:
        # New API: position_fp is a single signed value
        # Positive = YES contracts, negative = NO contracts
        position_fp = float(p.get("position_fp") or 0)
        # Fall back to old fields if present
        legacy_yes = float(p.get("yes_count_fp") or p.get("yes_count") or 0)
        legacy_no  = float(p.get("no_count_fp")  or p.get("no_count")  or 0)

        if position_fp > 0:
            yes, no = position_fp, 0.0
        elif position_fp < 0:
            yes, no = 0.0, abs(position_fp)
        else:
            yes, no = legacy_yes, legacy_no

        if yes == 0 and no == 0:
            continue
        # Use total_traded_dollars as our cost basis when DB record is missing.
        # For active (unclosed) positions, this equals what we paid in.
        entry_cost_api = float(p.get("total_traded_dollars") or 0)
        # If realized_pnl is non-zero, we've sold some — adjust to get current cost basis
        realized_pnl = float(p.get("realized_pnl_dollars") or 0)
        # market_exposure_dollars is the current cost basis on still-held shares
        cost_basis = float(p.get("market_exposure_dollars") or 0) or entry_cost_api
        # Compute average entry yes_price per contract
        count = yes if yes > 0 else no
        avg_entry_yes = None
        if count > 0 and cost_basis > 0:
            cost_per = cost_basis / count
            avg_entry_yes = cost_per if yes > 0 else (1 - cost_per)
        result.append({
            "ticker":           p.get("ticker"),
            "yes_count":        yes,
            "no_count":         no,
            "cost_basis":       cost_basis,
            "avg_entry_yes":    avg_entry_yes,
        })
    return result


def get_market(ticker):
    data = _api("GET", f"/markets/{ticker}", auth=False)
    m = data.get("market", data)
    def p(v):
        try: return float(v)
        except: return None
    return {
        "ticker":     m.get("ticker"),
        "title":      m.get("title", ""),
        "subtitle":   m.get("subtitle", ""),
        "status":     m.get("status"),
        "yes_bid":    p(m.get("yes_bid_dollars") or m.get("yes_bid")),
        "yes_ask":    p(m.get("yes_ask_dollars") or m.get("yes_ask")),
        "no_bid":     p(m.get("no_bid_dollars")  or m.get("no_bid")),
        "no_ask":     p(m.get("no_ask_dollars")  or m.get("no_ask")),
        "last_price": p(m.get("last_price_dollars") or m.get("last_price")),
        "volume_24h": float(m.get("volume_24h_fp") or m.get("volume_24h") or 0),
        "close_time": m.get("close_time", ""),
        "rules":      m.get("rules_primary", ""),
    }


def place_sell(ticker, side, count, yes_price, dry_run=False):
    log.info(
        f"{'[DRY RUN] ' if dry_run else ''}EXIT: SELL {count}x {side.upper()} "
        f"on {ticker} @ yes={yes_price:.4f}"
    )
    if dry_run:
        return True
    body = {
        "ticker": ticker, "type": "limit", "action": "sell",
        "side": side, "count": int(count),
        "yes_price_dollars": f"{yes_price:.4f}",
    }
    try:
        _api("POST", "/portfolio/orders", body=body)
        log.info("  ✅ Exit order placed")
        return True
    except httpx.HTTPStatusError as e:
        log.error(f"  ❌ Exit failed ({e.response.status_code}): {e.response.text}")
        return False


# ===============================================================
# DATABASE
# ===============================================================
def get_open_trades():
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT ticker, side, count, entry_yes_price, entry_cost, thesis FROM trades WHERE status='open'"
    ).fetchall()
    conn.close()
    return [{"ticker": r[0], "side": r[1], "count": r[2],
             "entry_yes_price": r[3], "entry_cost": r[4], "thesis": r[5] or ""} for r in rows]


def mark_exited(ticker, exit_price, proceeds):
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """UPDATE trades SET status='exited', exited_at=?, exit_yes_price=?,
           exit_proceeds=?, pnl=exit_proceeds-entry_cost
           WHERE ticker=? AND status='open'""",
        (datetime.now(timezone.utc).isoformat(), exit_price, proceeds, ticker)
    )
    conn.commit()
    conn.close()


def log_watchdog_decision(ticker, trigger, decision, reasoning):
    if not DB_PATH.exists():
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchdog_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT, trigger TEXT, decision TEXT,
            reasoning TEXT, decided_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO watchdog_decisions (ticker, trigger, decision, reasoning, decided_at) VALUES (?,?,?,?,?)",
        (ticker, trigger, decision, reasoning, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()


HOLD_COOLDOWN_HOURS = 4  # Don't re-ask Claude after "hold" for this many hours


def get_last_decision(ticker: str):
    """Return the most recent watchdog decision for a ticker, or None."""
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("""
            SELECT decision, decided_at FROM watchdog_decisions
            WHERE ticker=? ORDER BY id DESC LIMIT 1
        """, (ticker,)).fetchone()
        conn.close()
        return {"decision": row[0], "decided_at": row[1]} if row else None
    except Exception:
        return None


def is_on_cooldown(ticker: str) -> bool:
    """Return True if we should skip Claude for this ticker this cycle."""
    last = get_last_decision(ticker)
    if not last:
        return False
    if last["decision"] == "watch":
        return False  # Always re-evaluate watch
    if last["decision"] == "exit":
        return True   # Already decided, order should be resting
    if last["decision"] == "hold":
        try:
            decided = datetime.fromisoformat(last["decided_at"].replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - decided).total_seconds() / 3600
            return age_hours < HOLD_COOLDOWN_HOURS
        except Exception:
            return False
    return False
# ===============================================================
def notify(title, message):
    # Push to ntfy.sh (remote visibility)
    ntfy(title, message, priority="high", tags="warning")
    # Also fire local Windows toast
    try:
        script = (
            f'[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null\n'
            f'$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)\n'
            f'$template.SelectSingleNode("//text[@id=\'1\']").InnerText = "{title}"\n'
            f'$template.SelectSingleNode("//text[@id=\'2\']").InnerText = "{message}"\n'
            f'$toast = [Windows.UI.Notifications.ToastNotification]::new($template)\n'
            f'[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("KalshiBot").Show($toast)'
        )
        subprocess.run(["powershell", "-Command", script], capture_output=True, timeout=5)
    except Exception:
        pass


# ===============================================================
# HELPERS
# ===============================================================
def days_until(iso_str):
    if not iso_str:
        return 999
    try:
        target = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return (target - datetime.now(timezone.utc)).total_seconds() / 86400
    except Exception:
        return 999


def compute_metrics(trade, market):
    side  = trade["side"]
    count = trade["count"]

    if side == "yes":
        current_price = market.get("yes_bid") or 0
    else:
        current_price = market.get("no_bid") or 0
        if current_price == 0:
            current_price = 1 - (market.get("yes_ask") or 1)

    current_value = current_price * count
    max_payout    = 1.0 * count
    entry_cost    = trade["entry_cost"]

    return {
        "current_price": current_price,
        "current_value": current_value,
        "max_payout":    max_payout,
        "pct_of_cost":   current_value / entry_cost if entry_cost > 0 else 0,
        "pct_of_max":    current_value / max_payout if max_payout > 0 else 0,
        "days_to_close": days_until(market.get("close_time")),
    }


# ===============================================================
# CLAUDE DECISION ENGINE
# ===============================================================
WATCHDOG_SYSTEM_PROMPT = """You are a Kalshi prediction market risk manager.
A position has triggered a watchdog alert. Decide whether to exit or hold.

You have web search. Use it if needed — e.g. if a GDP position is dropping, search for
new economic data; if a crypto bracket is moving, check the current price. One targeted
search is usually enough. Be fast and decisive.

Respond ONLY with a JSON object, nothing else:
{
  "decision": "exit" | "hold" | "watch",
  "reasoning": "1-2 sentence explanation",
  "exit_yes_price": 0.0000
}

"exit"  — sell now at exit_yes_price (always provide this)
"hold"  — thesis intact, ignore the move
"watch" — hold but flag as high priority; will re-evaluate next cycle
"""


def ask_claude(position_context: dict, trigger: str) -> dict:
    """Ask Claude whether to exit or hold a triggered position."""
    if not ANTHROPIC_API_KEY:
        log.warning("No ANTHROPIC_API_KEY — defaulting to hold")
        return {"decision": "hold", "reasoning": "No API key", "exit_yes_price": None}

    market  = position_context["market"]
    trade   = position_context["trade"]
    metrics = position_context["metrics"]

    prompt = f"""WATCHDOG ALERT — {trigger}

Position: {trade['side'].upper()} x{trade['count']} on {market['ticker']}
Market: {market['title']} — {market['subtitle']}
Rules: {market['rules'][:200] if market['rules'] else 'N/A'}

Entry: yes_price={trade['entry_yes_price']:.4f}  cost=${trade['entry_cost']:.2f}
Now:   yes_bid={market['yes_bid']}  yes_ask={market['yes_ask']}  last={market['last_price']}
Value: ${metrics['current_value']:.2f} ({metrics['pct_of_cost']:.0%} of cost, {metrics['pct_of_max']:.0%} of max payout)
Days to close: {metrics['days_to_close']:.2f}
Volume 24h: {market['volume_24h']}

Original thesis: {trade['thesis'] or 'Not recorded'}

Search for relevant current data if needed, then decide: exit or hold?"""

    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 1024,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "system": WATCHDOG_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text = block["text"]

        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        decision = json.loads(text)
        log.info(f"  Claude: {decision['decision']} — {decision['reasoning']}")
        return decision

    except Exception as e:
        log.error(f"  Claude call failed: {e} — defaulting to hold")
        return {"decision": "hold", "reasoning": f"API error: {e}", "exit_yes_price": None}


# ===============================================================
# MAIN
# ===============================================================
def run(dry_run=False):
    log.info("-" * 55)
    log.info(f"Watchdog {'(DRY RUN) ' if dry_run else ''}— {datetime.now().strftime('%H:%M:%S')}")

    try:
        _load_private_key()
        balance = get_balance()
        log.info(f"Cash: ${balance:.2f}")
    except Exception as e:
        log.error(f"Auth/balance failed: {e}")
        return

    try:
        positions = get_positions()
    except Exception as e:
        log.error(f"Failed to fetch positions: {e}")
        return

    # Fetch resting orders so we don't place duplicate exits
    try:
        orders_data = _api("GET", "/portfolio/orders", params={"status": "resting"})
        resting_orders = orders_data.get("orders", [])
        tickers_with_exit = {
            o["ticker"] for o in resting_orders
            if o.get("action") == "sell"
        }
        if tickers_with_exit:
            log.info(f"  Tickers with resting exit orders: {tickers_with_exit}")
    except Exception as e:
        log.warning(f"Could not fetch open orders: {e}")
        tickers_with_exit = set()

    if not positions:
        log.info("No open positions.")
        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        update_gist(
            f"KALSHI WATCHDOG — last run: {ts_now}\n"
            f"================================================\n"
            f"Cash: ${balance:.2f}\n"
            f"No open positions.\n"
        )
        ntfy(
            title=f"Watchdog: ${balance:.2f} cash, 0 positions",
            message=f"Cash: ${balance:.2f}\nNo open positions.",
            priority="default",
            tags="white_check_mark",
        )
        return

    open_trades = {t["ticker"]: t for t in get_open_trades()}
    actions = 0

    for pos in positions:
        ticker = pos["ticker"]
        side   = "yes" if pos["yes_count"] > 0 else "no"
        count  = pos["yes_count"] if side == "yes" else pos["no_count"]

        trade = open_trades.get(ticker)
        if not trade:
            # Fall back to API-derived data (no DB record available)
            if pos.get("cost_basis", 0) > 0:
                trade = {
                    "ticker":          ticker,
                    "side":            side,
                    "count":           count,
                    "entry_yes_price": pos.get("avg_entry_yes") or 0,
                    "entry_cost":      pos["cost_basis"],
                    "thesis":          "(reconstructed from API — no DB record)",
                }
                log.info(f"  {ticker}: using API-derived cost basis ${trade['entry_cost']:.2f}")
            else:
                log.info(f"  {ticker}: no DB record and no API cost data, skipping")
                continue

        try:
            market = get_market(ticker)
        except Exception as e:
            log.warning(f"  {ticker}: market fetch failed — {e}")
            continue

        if market.get("status") != "active":
            continue

        # Override side/count from DB (more reliable)
        trade = {**trade, "side": side, "count": count}
        metrics = compute_metrics(trade, market)
        dtc     = metrics["days_to_close"]

        log.info(
            f"  {ticker}: {side} x{count}  "
            f"${metrics['current_value']:.2f} "
            f"({metrics['pct_of_cost']:.0%} cost  {metrics['pct_of_max']:.0%} max)  "
            f"{dtc:.2f}d"
        )

        # Within 6h of close — let it ride regardless
        if dtc < MIN_DAYS_TO_HOLD:
            log.info(f"    → <{MIN_DAYS_TO_HOLD*24:.0f}h to close, letting ride")
            continue

        # -- Hard stop — no Claude ------------------------------
        if metrics["pct_of_cost"] < HARD_STOP_LOSS_PCT:
            if ticker in tickers_with_exit:
                log.info(f"    Hard stop triggered but exit already resting for {ticker}, skipping")
                continue
            log.warning(f"    HARD STOP at {metrics['pct_of_cost']:.0%}")
            cp       = metrics["current_price"]
            yes_exit = cp * 0.97 if side == "yes" else 1 - (cp * 0.97)
            success  = place_sell(ticker, side, count, yes_exit, dry_run)
            if success:
                tickers_with_exit.add(ticker)
                if not dry_run:
                    mark_exited(ticker, yes_exit, cp * 0.97 * count)
                notify("KalshiBot Hard Stop", f"{ticker} exited at {metrics['pct_of_cost']:.0%} of cost")
                actions += 1
            continue

        # -- Soft triggers — consult Claude --------------------
        trigger = None
        if metrics["pct_of_cost"] < ALERT_STOP_LOSS_PCT:
            trigger = f"STOP-LOSS ALERT: at {metrics['pct_of_cost']:.0%} of cost (threshold {ALERT_STOP_LOSS_PCT:.0%})"
        elif metrics["pct_of_max"] >= ALERT_PROFIT_PCT:
            trigger = f"PROFIT TARGET: at {metrics['pct_of_max']:.0%} of max payout (threshold {ALERT_PROFIT_PCT:.0%})"

        if not trigger:
            continue  # Position healthy, no action needed

        # Skip if a resting exit order already exists for this ticker
        if ticker in tickers_with_exit:
            log.info(f"    Resting exit order already exists for {ticker}, skipping Claude")
            continue

        # Skip if Claude already decided recently (cooldown)
        if is_on_cooldown(ticker):
            last = get_last_decision(ticker)
            log.info(f"    {ticker} on cooldown (last: {last['decision']} at {last['decided_at'][:16]})")
            continue

        log.info(f"    !! {trigger} — consulting Claude")
        context  = {"market": market, "trade": trade, "metrics": metrics}
        decision = ask_claude(context, trigger)
        log_watchdog_decision(ticker, trigger, decision["decision"], decision["reasoning"])

        if decision["decision"] == "exit":
            exit_yes = decision.get("exit_yes_price")
            if exit_yes is None:
                cp       = metrics["current_price"]
                exit_yes = cp * 0.99 if side == "yes" else 1 - (cp * 0.99)
            success = place_sell(ticker, side, count, exit_yes, dry_run)
            if success:
                proceeds = (exit_yes if side == "yes" else 1 - exit_yes) * count
                if not dry_run:
                    mark_exited(ticker, exit_yes, proceeds)
                notify("KalshiBot Exit", f"{ticker} — {decision['reasoning'][:80]}")
                actions += 1

        elif decision["decision"] == "watch":
            notify("KalshiBot Watch", f"{ticker}: {decision['reasoning'][:80]}")
        # "hold" — do nothing

    log.info(f"Done. Actions taken: {actions}")
    log.info("-" * 55)

    # -- Heartbeat — post to ntfy AND update gist for health check --
    try:
        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        positions_lines = []
        for p in positions:
            side = "YES" if p["yes_count"] > 0 else "NO"
            count = int(p["yes_count"] or p["no_count"])
            positions_lines.append(f"  {p['ticker']:<35} {side} x{count}")
        positions_summary = "\n".join(positions_lines) if positions_lines else "  (none)"

        # Try to compute portfolio value
        try:
            bal = _api("GET", "/portfolio/balance")
            cash_val = round(bal.get("balance", 0) / 100, 2)
            port_val = round(bal.get("portfolio_value", 0) / 100, 2)
            total    = round(cash_val + port_val, 2)
        except Exception:
            cash_val, port_val, total = balance, 0, balance

        gist_content = (
            f"KALSHI WATCHDOG — last run: {ts_now}\n"
            f"================================================\n"
            f"Cash:            ${cash_val:.2f}\n"
            f"Portfolio value: ${port_val:.2f}\n"
            f"Total:           ${total:.2f}\n"
            f"Positions: {len(positions)}\n"
            f"Actions this run: {actions}\n"
            f"------------------------------------------------\n"
            f"{positions_summary}\n"
            f"------------------------------------------------\n"
            f"(updated by kalshi_watchdog.py)\n"
        )
        update_gist(gist_content)

        priority = "default" if actions == 0 else "high"
        tags = "white_check_mark" if actions == 0 else "rotating_light"
        ntfy(
            title=f"Watchdog: ${total:.2f} total, {len(positions)} pos, {actions} actions",
            message=f"Cash: ${cash_val:.2f}\nPort: ${port_val:.2f}\n{positions_summary}",
            priority=priority,
            tags=tags,
        )
    except Exception as e:
        log.warning(f"Heartbeat failed: {e}")


def show_status():
    try:
        balance   = get_balance()
        positions = get_positions()
        trades    = {t["ticker"]: t for t in get_open_trades()}
        print(f"\n{'-'*60}")
        print(f"  WATCHDOG STATUS  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"  Cash: ${balance:.2f}  |  Positions: {len(positions)}")
        for pos in positions:
            t     = pos["ticker"]
            side  = "yes" if pos["yes_count"] > 0 else "no"
            count = pos["yes_count"] if side == "yes" else pos["no_count"]
            trade = trades.get(t, {})
            try:
                market  = get_market(t)
                tr      = {**trade, "side": side, "count": count}
                metrics = compute_metrics(tr, market) if trade else {}
                print(
                    f"  {t:45s} {side} x{int(count)}  "
                    f"${metrics.get('current_value', 0):.2f} "
                    f"({metrics.get('pct_of_cost', 0):.0%} cost  "
                    f"{metrics.get('pct_of_max', 0):.0%} max  "
                    f"{metrics.get('days_to_close', 0):.1f}d)"
                )
            except Exception:
                print(f"  {t:45s} {side} x{int(count)}")
        print(f"{'-'*60}\n")
    except Exception as e:
        print(f"Error: {e}")


def main():
    parser = argparse.ArgumentParser(description="Kalshi Hybrid Watchdog")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status",  action="store_true")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()