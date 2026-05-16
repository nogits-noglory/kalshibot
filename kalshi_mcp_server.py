#!/usr/bin/env python3
"""Kalshi MCP server — exposes trading tools to Claude."""

import os
import sys
import json
import time
import base64
import httpx
import threading
from typing import Optional
from mcp.server.fastmcp import FastMCP
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

KALSHI_API_KEY = os.environ.get("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_BASE_URL = os.environ.get("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")

_private_key = None

def _load_private_key():
    global _private_key
    if _private_key is not None:
        return _private_key
    key_path = KALSHI_PRIVATE_KEY_PATH
    if not key_path:
        print("ERROR: Set KALSHI_PRIVATE_KEY_PATH to your RSA private key PEM file.", file=sys.stderr)
        sys.exit(1)
    with open(os.path.expanduser(key_path), "rb") as f:
        _private_key = serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )
    return _private_key


def _auth_headers(method: str, path: str) -> dict:
    pk = _load_private_key()
    ts = str(int(time.time() * 1000))
    if path.startswith("http"):
        from urllib.parse import urlparse
        path = urlparse(path).path
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


def _api_get(path, params=None):
    full_path = f"/trade-api/v2{path}"
    url = f"{KALSHI_BASE_URL}{path}"
    headers = _auth_headers("GET", full_path)
    resp = httpx.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _api_post(path, body):
    full_path = f"/trade-api/v2{path}"
    url = f"{KALSHI_BASE_URL}{path}"
    headers = _auth_headers("POST", full_path)
    resp = httpx.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _api_delete(path):
    full_path = f"/trade-api/v2{path}"
    url = f"{KALSHI_BASE_URL}{path}"
    headers = _auth_headers("DELETE", full_path)
    resp = httpx.delete(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _public_get(path, params=None):
    url = f"{KALSHI_BASE_URL}{path}"
    resp = httpx.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


mcp = FastMCP(
    "Kalshi Trading",
    instructions=(
        "You are a Kalshi prediction market trading agent. "
        "Use these tools to check balances, browse markets, analyze orderbooks, "
        "and place/cancel orders. Always check balance before trading. "
        "Be cautious with position sizing — never risk more than the user's budget."
    ),
)


@mcp.tool()
def get_balance() -> str:
    """Get your Kalshi account balance and portfolio value. Always call this before placing trades."""
    try:
        data = _api_get("/portfolio/balance")
        balance = data.get("balance_dollars") or data.get("balance", 0)
        portfolio = data.get("portfolio_value_dollars") or data.get("portfolio_value", 0)
        return json.dumps({
            "balance_dollars": balance,
            "portfolio_value_dollars": portfolio,
            "raw": data,
        }, indent=2)
    except Exception as e:
        return f"Error fetching balance: {e}"


@mcp.tool()
def get_positions() -> str:
    """Get all current open positions in your portfolio."""
    try:
        data = _api_get("/portfolio/positions")
        positions = data.get("market_positions") or data.get("positions", [])
        if not positions:
            return "No open positions."
        result = []
        for p in positions:
            position_fp = float(p.get("position_fp") or 0)
            if position_fp > 0:
                yes_count, no_count = position_fp, 0
            elif position_fp < 0:
                yes_count, no_count = 0, abs(position_fp)
            else:
                yes_count = float(p.get("yes_count_fp") or p.get("yes_count") or 0)
                no_count  = float(p.get("no_count_fp")  or p.get("no_count")  or 0)
            if yes_count == 0 and no_count == 0:
                continue
            exposure = float(p.get("market_exposure_dollars") or 0)
            realized = float(p.get("realized_pnl_dollars") or 0)
            result.append({
                "ticker": p.get("ticker"),
                "yes_count": yes_count,
                "no_count": no_count,
                "market_exposure_dollars": exposure,
                "realized_pnl_dollars": realized,
            })
        if not result:
            return "No open positions."
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error fetching positions: {e}"


@mcp.tool()
def get_open_orders() -> str:
    """Get all currently open/resting orders."""
    try:
        data = _api_get("/portfolio/orders", params={"status": "resting"})
        orders = data.get("orders", [])
        if not orders:
            return "No open orders."
        result = []
        for o in orders:
            result.append({
                "order_id": o.get("order_id"),
                "ticker": o.get("ticker"),
                "side": o.get("side"),
                "action": o.get("action"),
                "type": o.get("type"),
                "yes_price_dollars": o.get("yes_price_dollars") or o.get("yes_price"),
                "remaining_count": o.get("remaining_count_fp") or o.get("remaining_count"),
                "created_time": o.get("created_time"),
            })
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error fetching orders: {e}"


@mcp.tool()
def search_markets(
    query: str = "",
    series_ticker: str = "",
    event_ticker: str = "",
    status: str = "open",
    limit: int = 10,
) -> str:
    """Search and browse Kalshi markets.

    Args:
        query: Free text search (e.g. "GDP", "bitcoin", "fed rate")
        series_ticker: Filter by series (e.g. "KXGDP", "KXFED", "KXCPI", "KXBTC")
        event_ticker: Filter by specific event (e.g. "KXGDP-26APR30")
        status: Market status filter — "open", "closed", "settled"
        limit: Max results (1-200)
    """
    try:
        params = {"limit": min(limit, 200), "status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        data = _public_get("/markets", params=params)
        markets = data.get("markets", [])
        if query:
            q = query.lower()
            markets = [m for m in markets if q in m.get("title", "").lower()
                       or q in m.get("ticker", "").lower()
                       or q in m.get("subtitle", "").lower()]
        result = []
        for m in markets[:limit]:
            result.append({
                "ticker": m.get("ticker"),
                "title": m.get("title"),
                "subtitle": m.get("subtitle"),
                "yes_bid": m.get("yes_bid_dollars"),
                "yes_ask": m.get("yes_ask_dollars"),
                "last_price": m.get("last_price_dollars"),
                "volume_24h": m.get("volume_24h_fp"),
                "open_interest": m.get("open_interest_fp"),
                "close_time": m.get("close_time"),
                "expiration_time": m.get("expected_expiration_time"),
            })
        return json.dumps({"count": len(result), "markets": result}, indent=2)
    except Exception as e:
        return f"Error searching markets: {e}"


@mcp.tool()
def get_market(ticker: str) -> str:
    """Get detailed info about a specific market by its ticker (e.g. KXGDP-26APR30-T2.0)."""
    try:
        data = _public_get(f"/markets/{ticker}")
        m = data.get("market", data)
        return json.dumps({
            "ticker": m.get("ticker"),
            "title": m.get("title"),
            "subtitle": m.get("subtitle"),
            "status": m.get("status"),
            "result": m.get("result"),
            "yes_bid": m.get("yes_bid_dollars"),
            "yes_ask": m.get("yes_ask_dollars"),
            "no_bid": m.get("no_bid_dollars"),
            "no_ask": m.get("no_ask_dollars"),
            "last_price": m.get("last_price_dollars"),
            "volume": m.get("volume_fp"),
            "volume_24h": m.get("volume_24h_fp"),
            "open_interest": m.get("open_interest_fp"),
            "close_time": m.get("close_time"),
            "expiration_time": m.get("expected_expiration_time"),
            "rules": m.get("rules_primary"),
            "rules_secondary": m.get("rules_secondary"),
        }, indent=2)
    except Exception as e:
        return f"Error fetching market: {e}"


@mcp.tool()
def get_orderbook(ticker: str) -> str:
    """Get the full orderbook (bids and asks) for a market."""
    try:
        data = _public_get(f"/markets/{ticker}/orderbook")
        book = data.get("orderbook_fp") or data.get("orderbook", {})
        yes_side = book.get("yes_dollars") or book.get("yes", [])
        no_side = book.get("no_dollars") or book.get("no", [])
        best_yes_bid = yes_side[-1] if yes_side else None
        best_yes_ask = no_side[0] if no_side else None
        return json.dumps({
            "ticker": ticker,
            "best_yes_bid": best_yes_bid,
            "best_no_bid": best_yes_ask,
            "yes_levels": len(yes_side),
            "no_levels": len(no_side),
            "yes_bids": yes_side[-10:],
            "no_bids": no_side[:10],
        }, indent=2)
    except Exception as e:
        return f"Error fetching orderbook: {e}"


@mcp.tool()
def get_event(event_ticker: str) -> str:
    """Get info about an event (a group of related markets, e.g. KXGDP-26APR30)."""
    try:
        data = _public_get(f"/events/{event_ticker}")
        event = data.get("event", data)
        markets = event.get("markets", [])
        market_summaries = []
        for m in markets:
            market_summaries.append({
                "ticker": m.get("ticker"),
                "subtitle": m.get("subtitle"),
                "yes_bid": m.get("yes_bid_dollars"),
                "yes_ask": m.get("yes_ask_dollars"),
                "last_price": m.get("last_price_dollars"),
            })
        return json.dumps({
            "event_ticker": event.get("event_ticker"),
            "title": event.get("title"),
            "category": event.get("category"),
            "num_markets": len(market_summaries),
            "markets": market_summaries,
        }, indent=2)
    except Exception as e:
        return f"Error fetching event: {e}"


@mcp.tool()
def get_market_history(ticker: str, limit: int = 20) -> str:
    """Get recent trade history for a market."""
    try:
        data = _public_get(f"/markets/{ticker}/trades", params={"limit": min(limit, 100)})
        trades = data.get("trades", [])
        result = []
        for t in trades:
            result.append({
                "price": t.get("yes_price_dollars") or t.get("yes_price"),
                "count": t.get("count_fp") or t.get("count"),
                "taker_side": t.get("taker_side"),
                "created_time": t.get("created_time"),
            })
        return json.dumps({"ticker": ticker, "trades": result}, indent=2)
    except Exception as e:
        return f"Error fetching history: {e}"


@mcp.tool()
def place_order(
    ticker: str,
    side: str,
    action: str = "buy",
    count: int = 1,
    order_type: str = "limit",
    yes_price_dollars: str = "",
    no_price_dollars: str = "",
    expiration_ts: Optional[int] = None,
) -> str:
    """Place an order on a Kalshi market.

    IMPORTANT: Always check balance first with get_balance().

    Args:
        ticker: Market ticker (e.g. "KXGDP-26APR30-T2.0")
        side: "yes" or "no"
        action: "buy" or "sell"
        count: Number of contracts (integer)
        order_type: "limit" or "market"
        yes_price_dollars: Limit price as dollar string (e.g. "0.5400").
                          For NO buys: you pay (1.00 - yes_price) per contract.
        no_price_dollars: Alternative — set the NO price directly
        expiration_ts: Optional Unix timestamp for order expiration
    """
    try:
        body = {
            "ticker": ticker,
            "type": order_type,
            "action": action,
            "side": side,
            "count": count,
        }
        if yes_price_dollars:
            body["yes_price_dollars"] = yes_price_dollars
        elif no_price_dollars:
            body["no_price_dollars"] = no_price_dollars
        if expiration_ts:
            body["expiration_ts"] = expiration_ts
        data = _api_post("/portfolio/orders", body)
        order = data.get("order", data)
        return json.dumps({
            "status": "ORDER PLACED",
            "order_id": order.get("order_id"),
            "ticker": order.get("ticker"),
            "side": order.get("side"),
            "action": order.get("action"),
            "type": order.get("type"),
            "yes_price": order.get("yes_price_dollars"),
            "count": order.get("count_fp") or order.get("count"),
            "created_time": order.get("created_time"),
        }, indent=2)
    except httpx.HTTPStatusError as e:
        return f"Order FAILED ({e.response.status_code}): {e.response.text}"
    except Exception as e:
        return f"Order FAILED: {e}"


@mcp.tool()
def cancel_order(order_id: str) -> str:
    """Cancel a resting order by its order ID."""
    try:
        data = _api_delete(f"/portfolio/orders/{order_id}")
        return json.dumps({"status": "ORDER CANCELLED", "order_id": order_id, "raw": data}, indent=2)
    except httpx.HTTPStatusError as e:
        return f"Cancel FAILED ({e.response.status_code}): {e.response.text}"
    except Exception as e:
        return f"Cancel FAILED: {e}"


@mcp.tool()
def get_fills(ticker: str = "", limit: int = 20) -> str:
    """Get recent fills (executed trades) for your account."""
    try:
        params = {"limit": min(limit, 100)}
        if ticker:
            params["ticker"] = ticker
        data = _api_get("/portfolio/fills", params=params)
        fills = data.get("fills", [])
        result = []
        for f in fills:
            result.append({
                "ticker": f.get("ticker"),
                "side": f.get("side"),
                "action": f.get("action"),
                "count": f.get("count_fp") or f.get("count"),
                "yes_price": f.get("yes_price_dollars") or f.get("yes_price"),
                "fee": f.get("fee_cost"),
                "created_time": f.get("created_time"),
            })
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error fetching fills: {e}"


@mcp.tool()
def get_exchange_status() -> str:
    """Check if the Kalshi exchange is currently open for trading."""
    try:
        data = _public_get("/exchange/status")
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error: {e}"


# bot introspection tools
import sqlite3 as _sqlite3
from pathlib import Path as _Path
from datetime import datetime as _dt

_BOT_DIR_DEFAULT = _Path(os.environ.get("KALSHI_BOT_DIR", str(_Path(__file__).parent)))
_DB_PATH = _BOT_DIR_DEFAULT / "kalshi_bot.db"
_AGENT_LOG = _BOT_DIR_DEFAULT / "kalshi_agent.log"
_WATCHDOG_LOG = _BOT_DIR_DEFAULT / "kalshi_watchdog.log"


@mcp.tool()
def get_bot_log_tail(which: str = "watchdog", lines: int = 50) -> str:
    """Read the last N lines of either bot log file.

    Args:
        which: "watchdog" (every 30 min) or "agent" (twice daily)
        lines: number of trailing lines to return (max 500)
    """
    try:
        path = _WATCHDOG_LOG if which == "watchdog" else _AGENT_LOG
        if not path.exists():
            return f"Log file not found: {path}"
        lines = min(max(lines, 1), 500)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-lines:]
        return "".join(tail) or "(log empty)"
    except Exception as e:
        return f"Error reading log: {e}"


@mcp.tool()
def get_bot_status() -> str:
    """High-level health check: when did the bot last run, what does it know?

    Returns a summary including last cycle time, last watchdog run, recent
    decisions, and trade journal counts.
    """
    try:
        out = []
        out.append("=== KALSHI BOT STATUS ===")

        # Log file freshness
        for label, path in [("agent", _AGENT_LOG), ("watchdog", _WATCHDOG_LOG)]:
            if path.exists():
                mtime = _dt.fromtimestamp(path.stat().st_mtime)
                age = (_dt.now() - mtime).total_seconds() / 60
                out.append(f"  {label} log: last modified {mtime.strftime('%Y-%m-%d %H:%M')} ({age:.0f} min ago)")
            else:
                out.append(f"  {label} log: NOT FOUND at {path}")

        # DB stats
        if _DB_PATH.exists():
            conn = _sqlite3.connect(_DB_PATH)
            try:
                trades_open = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
                trades_total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
                cycles_total = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
                last_cycle = conn.execute("SELECT ran_at, agent_summary FROM cycles ORDER BY id DESC LIMIT 1").fetchone()
                out.append(f"  DB: {trades_open} open trades, {trades_total} total, {cycles_total} agent cycles")
                if last_cycle:
                    out.append(f"  Last agent run: {last_cycle[0]}")
                    out.append(f"  Last summary: {last_cycle[1] or '(none)'}")
            except _sqlite3.OperationalError as e:
                out.append(f"  DB exists but missing tables: {e}")
            conn.close()
        else:
            out.append(f"  DB: NOT FOUND at {_DB_PATH}")

        return "\n".join(out)
    except Exception as e:
        return f"Error fetching bot status: {e}"


@mcp.tool()
def get_recent_decisions(limit: int = 10) -> str:
    """Get the most recent watchdog decisions (exit/hold/watch from Claude).

    Args:
        limit: max number of decisions to return (1-50)
    """
    try:
        if not _DB_PATH.exists():
            return "No DB found yet — bot hasn't logged any decisions."
        limit = min(max(limit, 1), 50)
        conn = _sqlite3.connect(_DB_PATH)
        try:
            rows = conn.execute("""
                SELECT decided_at, ticker, trigger, decision, reasoning
                FROM watchdog_decisions
                ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        except _sqlite3.OperationalError:
            conn.close()
            return "No watchdog decisions table — bot hasn't triggered any alerts yet."
        conn.close()
        if not rows:
            return "No watchdog decisions logged yet."
        out = ["=== RECENT WATCHDOG DECISIONS ==="]
        for r in rows:
            out.append(f"\n  [{r[0]}] {r[1]}")
            out.append(f"    Trigger:  {r[2]}")
            out.append(f"    Decision: {r[3]}")
            out.append(f"    Reason:   {r[4]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error fetching decisions: {e}"


@mcp.tool()
def get_trade_history(limit: int = 20, status: str = "all") -> str:
    """Get the bot's trade journal.

    Args:
        limit: max trades to return (1-100)
        status: "open", "exited", or "all"
    """
    try:
        if not _DB_PATH.exists():
            return "No DB found — bot hasn't recorded any trades. Note: trades placed manually through chat are NOT in this DB."
        limit = min(max(limit, 1), 100)
        conn = _sqlite3.connect(_DB_PATH)
        try:
            if status == "all":
                rows = conn.execute("""
                    SELECT entered_at, ticker, side, count, entry_yes_price, entry_cost,
                           thesis, confidence, status, pnl
                    FROM trades ORDER BY id DESC LIMIT ?
                """, (limit,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT entered_at, ticker, side, count, entry_yes_price, entry_cost,
                           thesis, confidence, status, pnl
                    FROM trades WHERE status=? ORDER BY id DESC LIMIT ?
                """, (status, limit)).fetchall()
        except _sqlite3.OperationalError:
            conn.close()
            return "No trades table yet."
        conn.close()
        if not rows:
            return f"No trades found with status={status}."
        out = [f"=== TRADE HISTORY ({status}) ==="]
        for r in rows:
            pnl_str = f"  pnl=${r[9]:.2f}" if r[9] is not None else ""
            out.append(
                f"\n  [{r[8]:6s}] {r[0][:16]} {r[1]} {r[2]} x{r[3]} "
                f"@${r[4]:.2f}  cost=${r[5]:.2f}  conf={r[7]}{pnl_str}"
            )
            if r[6]:
                out.append(f"    thesis: {r[6][:120]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error fetching trades: {e}"


@mcp.tool()
def get_recent_cycles(limit: int = 10) -> str:
    """Get the most recent agent cycle summaries (twice-daily runs).

    Args:
        limit: max cycles to return (1-30)
    """
    try:
        if not _DB_PATH.exists():
            return "No DB found yet."
        limit = min(max(limit, 1), 30)
        conn = _sqlite3.connect(_DB_PATH)
        try:
            rows = conn.execute("""
                SELECT ran_at, cash_before, cash_after, trades_placed, exits_placed,
                       agent_summary, dry_run
                FROM cycles ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        except _sqlite3.OperationalError:
            conn.close()
            return "No cycles table yet."
        conn.close()
        if not rows:
            return "No agent cycles logged yet."
        out = ["=== RECENT AGENT CYCLES ==="]
        for r in rows:
            dry = " [DRY]" if r[6] else ""
            out.append(
                f"\n  [{r[0][:16]}{dry}] cash ${r[1]:.2f} → ${r[2]:.2f}  "
                f"entries={r[3]}  exits={r[4]}"
            )
            if r[5]:
                out.append(f"    summary: {r[5][:200]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error fetching cycles: {e}"


def main():
    if not KALSHI_API_KEY:
        print("ERROR: Set KALSHI_API_KEY environment variable.", file=sys.stderr)
        sys.exit(1)
    if not KALSHI_PRIVATE_KEY_PATH:
        print("ERROR: Set KALSHI_PRIVATE_KEY_PATH environment variable.", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(os.path.expanduser(KALSHI_PRIVATE_KEY_PATH)):
        print(f"ERROR: Private key file not found: {KALSHI_PRIVATE_KEY_PATH}", file=sys.stderr)
        sys.exit(1)
    _load_private_key()
    print(f"✅ Kalshi MCP server ready (key: {KALSHI_API_KEY[:8]}...)", file=sys.stderr)
    def _keepalive():
        while True:
            time.sleep(60)
            print(f"[keepalive] {time.strftime('%H:%M:%S')} — server alive", file=sys.stderr)
    threading.Thread(target=_keepalive, daemon=True).start()

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()