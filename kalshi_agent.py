#!/usr/bin/env python3
"""
Kalshi Agentic Trading Bot
Claude reasons about markets, searches for current data, and trades autonomously.

Usage:
    python kalshi_agent.py              # Run one full cycle
    python kalshi_agent.py --dry-run    # Reason and log but don't execute
    python kalshi_agent.py --status     # Show current positions and P&L only

Designed to be triggered by Windows Task Scheduler.
See scheduler_setup.md for setup instructions.
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
import logging
import argparse
import sqlite3
import httpx
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# ===============================================================
# CONFIGURATION — edit these or set as environment variables
# ===============================================================
BOT_DIR = Path(os.environ.get("KALSHI_BOT_DIR", str(Path(__file__).parent)))

KALSHI_API_KEY = os.environ.get("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY_PATH = os.environ.get(
    "KALSHI_PRIVATE_KEY_PATH",
    str(BOT_DIR / ".kalshi" / "private_key.pem"),
)
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# --- ntfy.sh notification topic (for remote log access) -------
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"


def ntfy(title: str, message: str, priority: str = "default", tags: str = ""):
    """Post a status update to ntfy.sh. Best-effort, never blocks."""
    if not NTFY_TOPIC:
        return
    try:
        headers = {
            "Title": title[:200],
            "Priority": priority,  # min, low, default, high, urgent
        }
        if tags:
            headers["Tags"] = tags
        httpx.post(NTFY_URL, headers=headers, content=message[:4000].encode("utf-8"), timeout=5)
    except Exception:
        pass  # Notifications are best-effort

# --- Risk Parameters ------------------------------------------
MAX_PORTFOLIO_RISK = 0.80      # Never deploy more than 80% of balance
MAX_SINGLE_TRADE_RISK = 0.25   # No single trade > 25% of available budget
MIN_TRADE_DOLLARS = 0.50       # Don't bother with tiny trades
MAX_OPEN_POSITIONS = 8         # Hard cap on concurrent positions
STOP_LOSS_PCT = 0.60           # Exit if position value drops to 60% of cost

# --- Paths ----------------------------------------------------
DB_PATH = BOT_DIR / "kalshi_bot.db"
LOG_PATH = BOT_DIR / "kalshi_agent.log"

# ===============================================================
# LOGGING
# ===============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("kalshi_agent")

# Force stdout to UTF-8 on Windows to handle box-drawing characters
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ===============================================================
# KALSHI API CLIENT
# ===============================================================
_private_key = None


def _load_private_key():
    global _private_key
    if _private_key is not None:
        return _private_key
    with open(os.path.expanduser(KALSHI_PRIVATE_KEY_PATH), "rb") as f:
        _private_key = serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )
    return _private_key


def _auth_headers(method: str, path: str) -> dict:
    pk = _load_private_key()
    ts = str(int(time.time() * 1000))
    message = f"{ts}{method.upper()}{path}".encode("utf-8")
    signature = pk.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _api(method: str, path: str, params=None, body=None, auth=True) -> dict:
    url = f"{KALSHI_BASE_URL}{path}"
    full_path = f"/trade-api/v2{path}"
    headers = _auth_headers(method, full_path) if auth else {"Accept": "application/json"}
    resp = httpx.request(method, url, headers=headers, params=params, json=body, timeout=20)
    resp.raise_for_status()
    return resp.json()


# ===============================================================
# ACCOUNT HELPERS (with correct cents→dollars conversion)
# ===============================================================
def get_balance() -> dict:
    data = _api("GET", "/portfolio/balance")
    raw_balance = data.get("balance", 0)
    raw_portfolio = data.get("portfolio_value", 0)
    return {
        "cash": round(raw_balance / 100, 2),
        "portfolio_value": round(raw_portfolio / 100, 2),
        "total": round((raw_balance + raw_portfolio) / 100, 2),
    }


def get_positions() -> list:
    data = _api("GET", "/portfolio/positions")
    positions = data.get("market_positions") or data.get("positions", [])
    result = []
    for p in positions:
        # New API: position_fp is signed (positive = YES, negative = NO)
        position_fp = float(p.get("position_fp") or 0)
        legacy_yes  = float(p.get("yes_count_fp") or p.get("yes_count") or 0)
        legacy_no   = float(p.get("no_count_fp")  or p.get("no_count")  or 0)
        if position_fp > 0:
            yes_count, no_count = position_fp, 0.0
        elif position_fp < 0:
            yes_count, no_count = 0.0, abs(position_fp)
        else:
            yes_count, no_count = legacy_yes, legacy_no
        # API now returns market_exposure_dollars directly in dollars
        exposure = float(p.get("market_exposure_dollars") or 0)
        if exposure == 0 and p.get("market_exposure"):
            exposure = round(float(p.get("market_exposure")) / 100, 2)
        realized_pnl = float(p.get("realized_pnl_dollars") or 0)
        if realized_pnl == 0 and p.get("realized_pnl"):
            realized_pnl = round(float(p.get("realized_pnl")) / 100, 2)
        result.append({
            "ticker": p.get("ticker"),
            "yes_count": yes_count,
            "no_count": no_count,
            "exposure": exposure,
            "realized_pnl": realized_pnl,
        })
    # Filter to only positions with actual holdings
    return [p for p in result if p["yes_count"] > 0 or p["no_count"] > 0]


def get_open_orders() -> list:
    data = _api("GET", "/portfolio/orders", params={"status": "resting"})
    return data.get("orders", [])


def get_market(ticker: str) -> dict:
    data = _api("GET", f"/markets/{ticker}", auth=False)
    m = data.get("market", data)
    # Prices come back as strings like "0.8700" — already in dollars
    return {
        "ticker": m.get("ticker"),
        "title": m.get("title"),
        "subtitle": m.get("subtitle"),
        "status": m.get("status"),
        "result": m.get("result"),
        "yes_bid": _to_float(m.get("yes_bid_dollars") or m.get("yes_bid")),
        "yes_ask": _to_float(m.get("yes_ask_dollars") or m.get("yes_ask")),
        "no_bid": _to_float(m.get("no_bid_dollars") or m.get("no_bid")),
        "no_ask": _to_float(m.get("no_ask_dollars") or m.get("no_ask")),
        "last_price": _to_float(m.get("last_price_dollars") or m.get("last_price")),
        "volume": _to_float(m.get("volume_fp") or m.get("volume")),
        "volume_24h": _to_float(m.get("volume_24h_fp") or m.get("volume_24h")),
        "open_interest": _to_float(m.get("open_interest_fp") or m.get("open_interest")),
        "close_time": m.get("close_time"),
        "rules": m.get("rules_primary", ""),
    }


def get_markets(series: str = "", status: str = "open", limit: int = 100) -> list:
    params = {"status": status, "limit": limit}
    if series:
        params["series_ticker"] = series
    data = _api("GET", "/markets", params=params, auth=False)
    markets = data.get("markets", [])
    result = []
    for m in markets:
        result.append({
            "ticker": m.get("ticker"),
            "title": m.get("title"),
            "subtitle": m.get("subtitle"),
            "yes_bid": _to_float(m.get("yes_bid_dollars") or m.get("yes_bid")),
            "yes_ask": _to_float(m.get("yes_ask_dollars") or m.get("yes_ask")),
            "last_price": _to_float(m.get("last_price_dollars") or m.get("last_price")),
            "volume_24h": _to_float(m.get("volume_24h_fp") or m.get("volume_24h")),
            "open_interest": _to_float(m.get("open_interest_fp") or m.get("open_interest")),
            "close_time": m.get("close_time"),
        })
    return result


def place_order(ticker: str, side: str, count: int, yes_price: float, dry_run: bool = False) -> dict:
    """Place a limit order. yes_price is always expressed as the YES price in dollars (0.0–1.0)."""
    log.info(
        f"{'[DRY Run] ' if dry_run else ''}ORDER: BUY {count}x {side.upper()} "
        f"on {ticker} @ yes_price=${yes_price:.4f}"
    )
    if dry_run:
        return {"status": "DRY_RUN", "ticker": ticker, "side": side, "count": count}
    body = {
        "ticker": ticker,
        "type": "limit",
        "action": "buy",
        "side": side,
        "count": count,
        "yes_price_dollars": f"{yes_price:.4f}",
    }
    try:
        data = _api("POST", "/portfolio/orders", body=body)
        order = data.get("order", data)
        log.info(f"  ✅ PLACED: order_id={order.get('order_id')}")
        return {"status": "PLACED", "order_id": order.get("order_id"), "ticker": ticker}
    except httpx.HTTPStatusError as e:
        log.error(f"  ❌ FAILED ({e.response.status_code}): {e.response.text}")
        return {"status": "FAILED", "error": e.response.text}


def sell_position(ticker: str, side: str, count: int, yes_price: float, dry_run: bool = False) -> dict:
    """Exit an existing position by selling."""
    log.info(
        f"{'[DRY RUN] ' if dry_run else ''}EXIT: SELL {count}x {side.upper()} "
        f"on {ticker} @ yes_price=${yes_price:.4f}"
    )
    if dry_run:
        return {"status": "DRY_RUN"}
    body = {
        "ticker": ticker,
        "type": "limit",
        "action": "sell",
        "side": side,
        "count": count,
        "yes_price_dollars": f"{yes_price:.4f}",
    }
    try:
        data = _api("POST", "/portfolio/orders", body=body)
        order = data.get("order", data)
        log.info(f"  ✅ EXIT PLACED: order_id={order.get('order_id')}")
        return {"status": "PLACED", "order_id": order.get("order_id")}
    except httpx.HTTPStatusError as e:
        log.error(f"  ❌ EXIT FAILED ({e.response.status_code}): {e.response.text}")
        return {"status": "FAILED", "error": e.response.text}


# ===============================================================
# DATABASE — persistent trade journal
# ===============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            count INTEGER NOT NULL,
            entry_yes_price REAL NOT NULL,
            entry_cost REAL NOT NULL,
            thesis TEXT,
            confidence TEXT,
            entered_at TEXT NOT NULL,
            exited_at TEXT,
            exit_yes_price REAL,
            exit_proceeds REAL,
            pnl REAL,
            outcome TEXT,
            status TEXT DEFAULT 'open'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ran_at TEXT NOT NULL,
            cash_before REAL,
            cash_after REAL,
            positions_before INTEGER,
            positions_after INTEGER,
            trades_placed INTEGER,
            exits_placed INTEGER,
            agent_summary TEXT,
            dry_run INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def log_trade(ticker, side, count, yes_price, cost, thesis, confidence):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO trades (ticker, side, count, entry_yes_price, entry_cost, thesis, confidence, entered_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (ticker, side, count, yes_price, cost, thesis, confidence,
          datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()


def log_exit(ticker, exit_price, proceeds):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE trades
        SET exited_at = ?, exit_yes_price = ?, exit_proceeds = ?,
            pnl = exit_proceeds - entry_cost, status = 'exited'
        WHERE ticker = ? AND status = 'open'
    """, (datetime.now(timezone.utc).isoformat(), exit_price, proceeds, ticker))
    conn.commit()
    conn.close()


def get_open_trade_tickers() -> set:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT ticker FROM trades WHERE status = 'open'").fetchall()
    conn.close()
    return {r[0] for r in rows}


def log_cycle(cash_before, cash_after, pos_before, pos_after, trades, exits, summary, dry_run):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO cycles (ran_at, cash_before, cash_after, positions_before, positions_after,
                            trades_placed, exits_placed, agent_summary, dry_run)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (datetime.now(timezone.utc).isoformat(), cash_before, cash_after,
          pos_before, pos_after, trades, exits, summary, int(dry_run)))
    conn.commit()
    conn.close()


# ===============================================================
# STOP-LOSS CHECKER — runs before agent, no AI needed
# ===============================================================
def check_stop_losses(positions: list, open_trade_tickers: set, dry_run: bool) -> int:
    """Exit positions that have fallen below stop-loss threshold."""
    exits = 0
    conn = sqlite3.connect(DB_PATH)
    for pos in positions:
        ticker = pos["ticker"]
        if ticker not in open_trade_tickers:
            continue
        row = conn.execute(
            "SELECT entry_cost, side, count FROM trades WHERE ticker=? AND status='open'",
            (ticker,)
        ).fetchone()
        if not row:
            continue
        entry_cost, side, count = row
        try:
            market = get_market(ticker)
        except Exception:
            continue
        # Current value of the position
        if side == "yes":
            current_price = market.get("yes_bid") or 0
        else:
            current_price = (1 - (market.get("yes_ask") or 1))
        current_value = current_price * count
        if entry_cost > 0 and current_value / entry_cost < STOP_LOSS_PCT:
            log.warning(
                f"STOP LOSS triggered on {ticker}: "
                f"entry=${entry_cost:.2f}, current=${current_value:.2f} "
                f"({current_value/entry_cost:.0%} of cost)"
            )
            sell_price = current_price * 0.99  # Slightly below bid to fill
            result = sell_position(ticker, side, int(count), sell_price, dry_run=dry_run)
            if result.get("status") in ("PLACED", "DRY_RUN"):
                log_exit(ticker, sell_price, current_value)
                exits += 1
    conn.close()
    return exits


# ===============================================================
# MARKET SNAPSHOT — what we feed the agent
# ===============================================================
SCAN_SERIES = [
    # Daily/short-term — quick turnarounds
    "KXBTC",        # Bitcoin daily
    "KXETH",        # Ethereum daily
    "KXINX",        # S&P 500
    "KXNDAQ",       # Nasdaq
    # Economic events — conviction bets
    "KXFED",        # Fed rate decisions
    "KXCPI",        # CPI prints
    "KXGDP",        # GDP
    "KXNFP",        # Nonfarm payrolls
    "KXPCE",        # PCE inflation
    "KXJOBLESS",    # Jobless claims
]


def build_market_snapshot(held_tickers: set) -> str:
    """Build a concise market snapshot for the agent to reason about."""
    lines = []
    lines.append(f"=== KALSHI MARKET SNAPSHOT — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} ===\n")

    for series in SCAN_SERIES:
        try:
            markets = get_markets(series=series, limit=20)
            if not markets:
                continue
            # Filter to liquid, near-term markets
            viable = [
                m for m in markets
                if m.get("yes_ask") is not None
                and (m.get("volume_24h") or 0) >= 50
                and m.get("close_time")
            ]
            if not viable:
                continue
            lines.append(f"\n[{series}]")
            for m in viable[:8]:
                held_flag = " [HELD]" if m["ticker"] in held_tickers else ""
                mid = ((m.get("yes_bid") or 0) + (m.get("yes_ask") or 0)) / 2
                lines.append(
                    f"  {m['ticker']:45s} mid={mid:.2f}  "
                    f"vol24h={int(m.get('volume_24h') or 0):>6}  "
                    f"closes={m.get('close_time', '')[:10]}{held_flag}"
                )
                if m.get("subtitle"):
                    lines.append(f"    → {m['subtitle']}")
        except Exception as e:
            lines.append(f"\n[{series}] ERROR: {e}")

    return "\n".join(lines)


# ===============================================================
# CLAUDE AGENT
# ===============================================================
SYSTEM_PROMPT = """You are an autonomous prediction market trading agent on Kalshi.

Your job: analyze available markets, research current data using web search, and decide which trades to make.

TRADING PHILOSOPHY:
- Mix of quick-turnaround bets (BTC daily, index weekly) for steady compounding AND high-conviction event bets (Fed, CPI, jobs) for bigger gains
- Quality over quantity — 2 great trades beats 8 mediocre ones
- Only trade when you have genuine edge from information or reasoning, not just because markets exist
- Be willing to skip an entire cycle if nothing looks good

STRATEGY GUIDANCE:
- For crypto/index dailies: search for current price, momentum, and news before deciding
- For economic events: search for the latest nowcasts, analyst forecasts, and recent data. Compare to what the market is pricing
- Bid/ask spread is your enemy on small accounts — only trade markets with tight spreads and reasonable volume
- Near-certain outcomes (>90%) have tiny upside; near-impossible outcomes (<10%) are usually correctly priced. The 30–70% range is where edge lives

POSITION SIZING (budget provided each run):
- High confidence: up to 25% of available budget per trade
- Medium confidence: up to 15%
- Speculative: up to 8%
- Never exceed the per-trade cap even if very confident — size up by count, not by breaking rules

OUTPUT FORMAT:
You must respond with a JSON object and nothing else:
{
  "trades": [
    {
      "ticker": "KXBTC-...",
      "side": "yes" or "no",
      "count": 1,
      "yes_price": 0.6200,
      "confidence": "high" | "medium" | "speculative",
      "thesis": "Brief reason — what you found and why it gives edge"
    }
  ],
  "exits": [
    {
      "ticker": "KXGDP-...",
      "reason": "Why exiting early"
    }
  ],
  "summary": "1-2 sentence summary of your reasoning this cycle"
}

If no good trades exist, return {"trades": [], "exits": [], "summary": "No edge found this cycle — [reason]"}
"""


def run_agent(balance: dict, positions: list, held_tickers: set, market_snapshot: str, dry_run: bool) -> dict:
    """Call Claude to reason about markets and return trading decisions."""
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set — cannot run agent")
        return {"trades": [], "exits": [], "summary": "ERROR: No Anthropic API key"}

    available_cash = balance["cash"] * MAX_PORTFOLIO_RISK
    max_per_trade = balance["cash"] * MAX_SINGLE_TRADE_RISK

    position_summary = ""
    if positions:
        position_summary = "\n\nCURRENT POSITIONS:\n"
        for p in positions:
            position_summary += f"  {p['ticker']}: {p['yes_count']}x YES / {p['no_count']}x NO  exposure=${p['exposure']:.2f}\n"
    else:
        position_summary = "\n\nCURRENT POSITIONS: None\n"

    user_message = f"""ACCOUNT STATUS:
Cash available: ${available_cash:.2f} (max per trade: ${max_per_trade:.2f})
Total portfolio: ${balance['total']:.2f}
Open positions: {len(positions)} / {MAX_OPEN_POSITIONS} max
{position_summary}

{market_snapshot}

Research the most promising markets above using web search before deciding.
Focus on: current prices/levels, recent news, analyst forecasts vs market pricing.
Return only the JSON decision object."""

    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "interleaved-thinking-2025-05-14",
                "content-type": "application/json",
            },
            json={
                "model": "claude-opus-4-5",
                "max_tokens": 8000,
                "thinking": {"type": "enabled", "budget_tokens": 5000},
                "tools": [
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                    }
                ],
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}],
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()

        # Extract the final text response (may follow thinking + tool use blocks)
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text = block["text"]

        if not text:
            log.error("Agent returned no text response")
            return {"trades": [], "exits": [], "summary": "ERROR: No text from agent"}

        # Strip markdown fences if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        decision = json.loads(text)
        log.info(f"Agent summary: {decision.get('summary', '')}")
        return decision

    except json.JSONDecodeError as e:
        log.error(f"Agent returned invalid JSON: {e}\nRaw: {text[:500]}")
        return {"trades": [], "exits": [], "summary": f"ERROR: JSON parse failed"}
    except Exception as e:
        log.error(f"Agent call failed: {e}")
        return {"trades": [], "exits": [], "summary": f"ERROR: {e}"}


# ===============================================================
# EXECUTION — validate and place agent decisions
# ===============================================================
def execute_decisions(decision: dict, balance: dict, held_tickers: set, dry_run: bool) -> tuple:
    """Validate agent decisions against risk rules, then execute."""
    trades_placed = 0
    exits_placed = 0
    spent = 0.0
    available = balance["cash"] * MAX_PORTFOLIO_RISK
    max_per_trade = balance["cash"] * MAX_SINGLE_TRADE_RISK

    # Handle exits first
    for exit_rec in decision.get("exits", []):
        ticker = exit_rec.get("ticker")
        if not ticker:
            continue
        try:
            market = get_market(ticker)
            # Find our position side from DB
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute(
                "SELECT side, count FROM trades WHERE ticker=? AND status='open'",
                (ticker,)
            ).fetchone()
            conn.close()
            if not row:
                log.warning(f"Exit requested for {ticker} but no open trade in DB")
                continue
            side, count = row
            exit_price = market.get("yes_bid") if side == "yes" else (1 - (market.get("yes_ask") or 0))
            proceeds = (exit_price or 0) * count
            result = sell_position(ticker, side, int(count), exit_price or 0, dry_run=dry_run)
            if result.get("status") in ("PLACED", "DRY_RUN"):
                log_exit(ticker, exit_price, proceeds)
                exits_placed += 1
                log.info(f"  Exit: {ticker} reason={exit_rec.get('reason', '')}")
        except Exception as e:
            log.error(f"Exit failed for {ticker}: {e}")

    # Handle new entries
    for trade in decision.get("trades", []):
        ticker = trade.get("ticker")
        side = trade.get("side")
        count = trade.get("count", 1)
        yes_price = trade.get("yes_price")
        confidence = trade.get("confidence", "medium")
        thesis = trade.get("thesis", "")

        # Validation
        if not all([ticker, side, yes_price]):
            log.warning(f"Skipping malformed trade: {trade}")
            continue
        if ticker in held_tickers:
            log.info(f"Skipping {ticker} — already held")
            continue
        if side == "yes":
            cost_per = yes_price
        else:
            cost_per = 1 - yes_price
        if cost_per <= 0.02 or cost_per >= 0.98:
            log.warning(f"Skipping {ticker} — unreasonable price {yes_price}")
            continue

        trade_cost = cost_per * count

        # Cap to max per trade
        if trade_cost > max_per_trade:
            count = max(1, int(max_per_trade / cost_per))
            trade_cost = cost_per * count

        # Cap to minimum trade size
        if trade_cost < MIN_TRADE_DOLLARS:
            log.info(f"Skipping {ticker} — trade cost ${trade_cost:.2f} below minimum")
            continue

        # Budget check
        if spent + trade_cost > available:
            remaining = available - spent
            count = max(0, int(remaining / cost_per))
            if count == 0:
                log.info(f"Skipping {ticker} — budget exhausted")
                continue
            trade_cost = cost_per * count

        # Sanity check market is still open
        try:
            market = get_market(ticker)
            if market.get("status") != "open":
                log.warning(f"Skipping {ticker} — market status={market.get('status')}")
                continue
        except Exception as e:
            log.warning(f"Skipping {ticker} — could not verify market: {e}")
            continue

        result = place_order(ticker, side, count, yes_price, dry_run=dry_run)
        if result.get("status") in ("PLACED", "DRY_RUN"):
            spent += trade_cost
            trades_placed += 1
            held_tickers.add(ticker)
            if not dry_run:
                log_trade(ticker, side, count, yes_price, trade_cost, thesis, confidence)
            log.info(f"  Entry: {ticker} {side} x{count} @ {yes_price:.4f} — {thesis}")

    return trades_placed, exits_placed, spent


# ===============================================================
# STATUS DISPLAY
# ===============================================================
def show_status():
    """Print current portfolio status and trade history."""
    try:
        bal = get_balance()
        positions = get_positions()
        print(f"\n{'='*60}")
        print(f"  KALSHI BOT STATUS — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*60}")
        print(f"  Cash:            ${bal['cash']:.2f}")
        print(f"  Portfolio value: ${bal['portfolio_value']:.2f}")
        print(f"  Total:           ${bal['total']:.2f}")
        print(f"\n  Open positions: {len(positions)}")
        for p in positions:
            print(f"    {p['ticker']:45s} exposure=${p['exposure']:.2f}  pnl=${p['realized_pnl']:.2f}")

        conn = sqlite3.connect(DB_PATH)
        recent = conn.execute("""
            SELECT ticker, side, count, entry_cost, thesis, status, entered_at, pnl
            FROM trades ORDER BY entered_at DESC LIMIT 10
        """).fetchall()
        conn.close()
        if recent:
            print(f"\n  Recent trades:")
            for r in recent:
                pnl_str = f"  pnl=${r[7]:.2f}" if r[7] is not None else ""
                print(f"    [{r[5]:6s}] {r[0]:40s} {r[1]:3s} x{r[2]}  cost=${r[3]:.2f}{pnl_str}")
                print(f"           {r[4][:80] if r[4] else ''}")
        print(f"{'='*60}\n")
    except Exception as e:
        print(f"Error fetching status: {e}")


# ===============================================================
# MAIN
# ===============================================================
def main():
    parser = argparse.ArgumentParser(description="Kalshi Agentic Trading Bot")
    parser.add_argument("--dry-run", action="store_true", help="Reason and log but don't execute trades")
    parser.add_argument("--status", action="store_true", help="Show portfolio status and exit")
    args = parser.parse_args()

    init_db()

    if args.status:
        show_status()
        return

    log.info("=" * 60)
    log.info(f"Kalshi Agent starting — {'DRY RUN' if args.dry_run else 'LIVE'}")

    # Auth check
    try:
        _load_private_key()
    except Exception as e:
        log.error(f"Failed to load private key: {e}")
        sys.exit(1)

    # Fetch account state
    try:
        balance = get_balance()
        log.info(f"Balance: cash=${balance['cash']:.2f}  portfolio=${balance['portfolio_value']:.2f}  total=${balance['total']:.2f}")
    except Exception as e:
        log.error(f"Failed to fetch balance: {e}")
        sys.exit(1)

    try:
        positions = get_positions()
        log.info(f"Open positions: {len(positions)}")
    except Exception as e:
        log.warning(f"Failed to fetch positions: {e}")
        positions = []

    # Held tickers from DB (more reliable than API position counts)
    db_held = get_open_trade_tickers()
    api_held = {p["ticker"] for p in positions}
    held_tickers = db_held | api_held
    positions_before = len(held_tickers)

    # Stop-loss check (no AI needed)
    exits_from_stops = check_stop_losses(positions, held_tickers, args.dry_run)
    if exits_from_stops:
        log.info(f"Stop-loss exits: {exits_from_stops}")
        # Re-fetch positions after stops
        try:
            positions = get_positions()
            held_tickers = get_open_trade_tickers() | {p["ticker"] for p in positions}
        except Exception:
            pass

    # Skip new entries if at capacity
    if len(held_tickers) >= MAX_OPEN_POSITIONS:
        log.info(f"At max positions ({MAX_OPEN_POSITIONS}). Skipping agent scan.")
        log_cycle(balance["cash"], balance["cash"], positions_before, len(held_tickers),
                  0, exits_from_stops, "At max positions", args.dry_run)
        return

    if balance["cash"] * MAX_PORTFOLIO_RISK < MIN_TRADE_DOLLARS:
        log.info(f"Insufficient budget (${balance['cash']:.2f}). Skipping agent scan.")
        return

    # Build market snapshot
    log.info("Building market snapshot...")
    snapshot = build_market_snapshot(held_tickers)

    # Run Claude agent
    log.info("Running agent...")
    decision = run_agent(balance, positions, held_tickers, snapshot, args.dry_run)

    # Execute decisions
    trades_placed, exits_placed, spent = execute_decisions(
        decision, balance, held_tickers, args.dry_run
    )
    exits_placed += exits_from_stops

    # Fetch final balance
    try:
        balance_after = get_balance()
    except Exception:
        balance_after = balance

    log.info(f"Cycle complete: {trades_placed} entries, {exits_placed} exits, ${spent:.2f} deployed")
    log.info(f"Balance after: cash=${balance_after['cash']:.2f}  total=${balance_after['total']:.2f}")

    # -- Push summary to ntfy.sh ------------------------------
    ntfy_msg = (
        f"Cash: ${balance_after['cash']:.2f}  Total: ${balance_after['total']:.2f}\n"
        f"Entries: {trades_placed}  Exits: {exits_placed}  Deployed: ${spent:.2f}\n"
        f"Positions: {len(held_tickers)}\n\n"
        f"Summary: {decision.get('summary', 'no summary')}"
    )
    tags = "moneybag" if trades_placed > 0 or exits_placed > 0 else "eyes"
    ntfy(
        title=f"Agent {'DRY ' if args.dry_run else ''}— {trades_placed}↑ {exits_placed}↓",
        message=ntfy_msg,
        tags=tags,
    )

    log_cycle(
        balance["cash"], balance_after["cash"],
        positions_before, len(held_tickers),
        trades_placed, exits_placed,
        decision.get("summary", ""),
        args.dry_run,
    )

    log.info("=" * 60)


if __name__ == "__main__":
    main()
