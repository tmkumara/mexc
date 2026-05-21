"""
Dashboard for the MEXC Signal Bot.

Run:
    python webui.py

URL:
    http://<server>:6060/?token=<WEBUI_TOKEN>

Set in .env:
    WEBUI_TOKEN=1997
    WEBUI_PORT=6060

This dashboard reads from SQLite directly and shows:
    - Signal performance
    - Recent signals
    - Pending breakout/retest setups
    - Current strategy configuration
    - WebSocket/cache configuration

Frontend communication:
    - Primary: WebSocket /ws?token=<WEBUI_TOKEN>
    - Fallback: REST /api/data?token=<WEBUI_TOKEN>
"""

import asyncio
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

WEBUI_TOKEN = os.getenv("WEBUI_TOKEN", "mexc123")
PORT        = int(os.getenv("WEBUI_PORT", 6060))
DB_PATH     = os.getenv("DB_PATH", "signals.db")

WS_PUSH_SECONDS = int(os.getenv("WEBUI_WS_PUSH_SECONDS", "5"))

app = FastAPI(docs_url=None, redoc_url=None)


# ── optional config loader ────────────────────────────────────────

def _safe_config_value(name: str, default: Any = None) -> Any:
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


# ── database helpers ──────────────────────────────────────────────

def _db_exists() -> bool:
    return Path(DB_PATH).exists()


def _query(sql: str, params: tuple = ()) -> list[dict]:
    if not _db_exists():
        return []

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    try:
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _query_one(sql: str, params: tuple = ()) -> dict | None:
    if not _db_exists():
        return None

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    try:
        row = con.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _table_exists(table_name: str) -> bool:
    row = _query_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return row is not None


def _count_table(table_name: str, where_sql: str = "", params: tuple = ()) -> int:
    if not _table_exists(table_name):
        return 0

    sql = f"SELECT COUNT(*) AS count FROM {table_name}"

    if where_sql:
        sql += f" WHERE {where_sql}"

    row = _query_one(sql, params)
    return int(row["count"]) if row else 0


def _iso_to_display(value: str | None) -> str:
    if not value:
        return "—"

    try:
        raw = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(value)[:16].replace("T", " ") + " UTC"


def _format_price(value: Any) -> str:
    if value is None:
        return "—"

    try:
        f = float(value)
        if abs(f) >= 1:
            return f"{f:.4f}"
        return f"{f:.8f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value)


# ── stats helpers ─────────────────────────────────────────────────

def get_stats(since: datetime | None = None) -> dict:
    if not _table_exists("signals"):
        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "pending": 0,
            "expired": 0,
            "win_rate": 0.0,
            "net_roi": 0.0,
            "best": 0.0,
            "worst": 0.0,
            "longs": 0,
            "shorts": 0,
        }

    if since:
        rows = _query(
            "SELECT * FROM signals WHERE generated_at >= ?",
            (since.isoformat(),),
        )
    else:
        rows = _query("SELECT * FROM signals")

    total   = len(rows)
    wins    = [r for r in rows if r.get("status") == "win"]
    losses  = [r for r in rows if r.get("status") == "loss"]
    pending = [r for r in rows if r.get("status") == "pending"]
    expired = [r for r in rows if r.get("status") == "expired"]
    longs   = [r for r in rows if r.get("direction") == "LONG"]
    shorts  = [r for r in rows if r.get("direction") == "SHORT"]

    closed   = len(wins) + len(losses)
    win_rate = (len(wins) / closed * 100) if closed else 0.0
    net_roi  = sum(r.get("pnl_roi") or 0 for r in rows if r.get("status") in ("win", "loss"))
    best     = max((r.get("pnl_roi") or 0 for r in wins), default=0.0)
    worst    = min((r.get("pnl_roi") or 0 for r in losses), default=0.0)

    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "pending": len(pending),
        "expired": len(expired),
        "win_rate": round(win_rate, 1),
        "net_roi": round(net_roi, 1),
        "best": round(best, 1),
        "worst": round(worst, 1),
        "longs": len(longs),
        "shorts": len(shorts),
    }


def get_recent_signals(limit: int = 30) -> list[dict]:
    if not _table_exists("signals"):
        return []

    rows = _query(
        """
        SELECT *
        FROM signals
        ORDER BY generated_at DESC
        LIMIT ?
        """,
        (limit,),
    )

    for row in rows:
        row["display_time"] = _iso_to_display(row.get("generated_at"))
        row["entry_display"] = _format_price(row.get("entry_price"))
        row["tp_display"] = _format_price(row.get("tp_price"))
        row["sl_display"] = _format_price(row.get("sl_price"))

    return rows


def get_pending_setups(limit: int = 30) -> list[dict]:
    if not _table_exists("pending_setups"):
        return []

    rows = _query(
        """
        SELECT
            id,
            symbol,
            direction,
            status,
            trend_tf,
            entry_tf,
            sweep_type,
            sweep_level,
            ob_type,
            ob_low,
            ob_high,
            target_price,
            sl_price,
            rr_estimate,
            score,
            setup_time,
            expires_at,
            created_at,
            updated_at,
            fired_signal_id
        FROM pending_setups
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )

    for row in rows:
        row["created_display"] = _iso_to_display(row.get("created_at"))
        row["expires_display"] = _iso_to_display(row.get("expires_at"))
        row["level_display"] = _format_price(row.get("sweep_level"))
        row["zone_low_display"] = _format_price(row.get("ob_low"))
        row["zone_high_display"] = _format_price(row.get("ob_high"))
        row["tp_display"] = _format_price(row.get("target_price"))
        row["sl_display"] = _format_price(row.get("sl_price"))

    return rows


def get_runtime_status() -> dict:
    waiting_setups = _count_table("pending_setups", "status = 'waiting'")
    expired_setups = _count_table("pending_setups", "status = 'expired'")
    invalidated_setups = _count_table("pending_setups", "status = 'invalidated'")
    fired_setups = _count_table("pending_setups", "status = 'fired'")
    active_signals = _count_table("signals", "status = 'pending'")

    return {
        "db_exists": _db_exists(),
        "waiting_setups": waiting_setups,
        "expired_setups": expired_setups,
        "invalidated_setups": invalidated_setups,
        "fired_setups": fired_setups,
        "active_signals": active_signals,
    }


def get_strategy_config() -> dict:
    ws_test_symbols = _safe_config_value("WS_TEST_SYMBOLS", [])

    if isinstance(ws_test_symbols, list):
        ws_mode = "Full coin pool" if len(ws_test_symbols) == 0 else ", ".join(ws_test_symbols)
    else:
        ws_mode = str(ws_test_symbols)

    return {
        "strategy": _safe_config_value("STRATEGY_NAME", "Breakout Retest EMA/VWAP Scalper"),
        "trend_tf": _safe_config_value("TREND_TF", "—"),
        "entry_tf": _safe_config_value("ENTRY_TF", "—"),
        "entry_kline_count": _safe_config_value("ENTRY_KLINE_COUNT", "—"),
        "monitor_kline_count": _safe_config_value("MONITOR_KLINE_COUNT", "—"),
        "top_n_coins": _safe_config_value("TOP_N_COINS", "—"),
        "min_volume_usd": _safe_config_value("COIN_POOL_MIN_VOLUME_USD", "—"),
        "breakout_lookback": _safe_config_value("BREAKOUT_LOOKBACK", "—"),
        "retest_max_candles": _safe_config_value("RETEST_MAX_CANDLES", "—"),
        "ema_period": _safe_config_value("EMA_PERIOD", "—"),
        "vwap_lookback": _safe_config_value("VWAP_LOOKBACK_BARS", "—"),
        "atr_period": _safe_config_value("ATR_PERIOD", "—"),
        "min_rr": _safe_config_value("MIN_RR", "—"),
        "target_rr": _safe_config_value("TARGET_RR", "—"),
        "max_rr": _safe_config_value("MAX_RR", "—"),
        "min_score": _safe_config_value("MIN_SIGNAL_SCORE", "—"),
        "setups_per_scan": _safe_config_value("SETUPS_PER_SCAN", "—"),
        "signals_per_scan": _safe_config_value("SIGNALS_PER_SCAN", "—"),
        "max_concurrent_signals": _safe_config_value("MAX_CONCURRENT_SIGNALS", "—"),
        "cooldown_minutes": _safe_config_value("SIGNAL_COOLDOWN_MINUTES", "—"),
        "scan_workers": _safe_config_value("SCAN_WORKERS", "—"),
        "websocket_enabled": _safe_config_value("ENABLE_WEBSOCKET", False),
        "websocket_url": _safe_config_value("MEXC_WS_URL", "—"),
        "websocket_symbols": ws_mode,
        "candle_cache_limit": _safe_config_value("CANDLE_CACHE_LIMIT", "—"),
        "leverage": _safe_config_value("LEVERAGE", "—"),
    }


def build_payload() -> dict:
    now   = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week  = now - timedelta(days=7)

    return {
        "today": get_stats(today),
        "week": get_stats(week),
        "alltime": get_stats(),
        "recent": get_recent_signals(30),
        "setups": get_pending_setups(30),
        "runtime": get_runtime_status(),
        "config": get_strategy_config(),
        "server_time": now.strftime("%Y-%m-%d %H:%M UTC"),
        "push_interval_seconds": WS_PUSH_SECONDS,
    }


# ── API endpoints ─────────────────────────────────────────────────

def _auth(token: str):
    if token != WEBUI_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


@app.get("/api/data")
async def api_data(token: str = Query("")):
    _auth(token)
    return JSONResponse(build_payload())


@app.get("/api/health")
async def api_health(token: str = Query("")):
    _auth(token)

    now = datetime.now(timezone.utc)

    return JSONResponse({
        "ok": True,
        "server_time": now.strftime("%Y-%m-%d %H:%M UTC"),
        "runtime": get_runtime_status(),
        "config": get_strategy_config(),
    })


@app.websocket("/ws")
async def ws_dashboard(websocket: WebSocket):
    token = websocket.query_params.get("token", "")

    if token != WEBUI_TOKEN:
        await websocket.close(code=1008)
        return

    await websocket.accept()

    try:
        while True:
            await websocket.send_json(build_payload())
            await asyncio.sleep(WS_PUSH_SECONDS)

    except WebSocketDisconnect:
        return

    except Exception:
        try:
            await websocket.close()
        except Exception:
            pass


# ── dashboard HTML ────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MEXC Bot Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:      #080b12;
  --surface: #111827;
  --surface2:#0f172a;
  --card:    rgba(17, 24, 39, 0.88);
  --border:  #263244;
  --text:    #e5edf7;
  --muted:   #8fa1b7;
  --green:   #35d07f;
  --red:     #ff5c6c;
  --yellow:  #f5c84b;
  --blue:    #5aa7ff;
  --cyan:    #2dd4bf;
  --purple:  #a78bfa;
  --orange:  #fb923c;
}

body {
  background:
    radial-gradient(circle at top left, rgba(90, 167, 255, .16), transparent 35%),
    radial-gradient(circle at top right, rgba(45, 212, 191, .11), transparent 32%),
    var(--bg);
  color: var(--text);
  font-family: 'Inter', sans-serif;
  font-size: 14px;
  min-height: 100vh;
}

.header {
  background: rgba(17, 24, 39, .86);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border);
  padding: 14px 18px;
  position: sticky;
  top: 0;
  z-index: 20;
}

.header-inner {
  max-width: 1300px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.logo {
  font-size: 16px;
  font-weight: 800;
  display: flex;
  align-items: center;
  gap: 9px;
}

.logo-sub {
  color: var(--muted);
  font-size: 11px;
  font-weight: 500;
  margin-top: 2px;
}

.meta {
  color: var(--muted);
  font-size: 11px;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.status-pill {
  border: 1px solid rgba(53, 208, 127, .35);
  background: rgba(53, 208, 127, .09);
  color: var(--green);
  padding: 5px 10px;
  border-radius: 999px;
  font-weight: 700;
}

.status-pill.disconnected {
  border-color: rgba(255, 92, 108, .35);
  background: rgba(255, 92, 108, .09);
  color: var(--red);
}

.status-pill.connecting {
  border-color: rgba(245, 200, 75, .35);
  background: rgba(245, 200, 75, .09);
  color: var(--yellow);
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 10px currentColor;
  display: inline-block;
  margin-right: 6px;
}

.container {
  max-width: 1300px;
  margin: 0 auto;
  padding: 18px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.tab, .refresh-btn {
  padding: 7px 14px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: rgba(15, 23, 42, .8);
  color: var(--muted);
  font-family: inherit;
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  transition: all .15s;
  white-space: nowrap;
}

.tab:hover, .refresh-btn:hover {
  border-color: var(--blue);
  color: var(--blue);
}

.tab.active {
  background: rgba(90, 167, 255, .16);
  border-color: var(--blue);
  color: var(--blue);
}

.refresh-btn {
  margin-left: auto;
}

.grid {
  display: grid;
  gap: 12px;
}

.stats-grid {
  grid-template-columns: repeat(4, 1fr);
  margin-bottom: 18px;
}

.runtime-grid {
  grid-template-columns: repeat(4, 1fr);
  margin-bottom: 18px;
}

.config-grid {
  grid-template-columns: repeat(4, 1fr);
  margin-bottom: 18px;
}

@media (max-width: 1000px) {
  .stats-grid, .runtime-grid, .config-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 580px) {
  .stats-grid, .runtime-grid, .config-grid {
    grid-template-columns: 1fr;
  }
  .refresh-btn {
    margin-left: 0;
  }
}

.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 15px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, .18);
}

.card:hover {
  border-color: #3a4b66;
}

.card-label {
  font-size: 10px;
  color: var(--muted);
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: .08em;
  margin-bottom: 8px;
}

.card-value {
  font-size: 27px;
  font-weight: 800;
  line-height: 1;
}

.card-small {
  font-size: 13px;
  color: var(--muted);
  margin-top: 8px;
}

.section-title {
  font-size: 11px;
  font-weight: 800;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .09em;
  margin: 20px 0 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.green  { color: var(--green); }
.red    { color: var(--red); }
.blue   { color: var(--blue); }
.yellow { color: var(--yellow); }
.cyan   { color: var(--cyan); }
.purple { color: var(--purple); }
.orange { color: var(--orange); }
.white  { color: var(--text); }
.muted  { color: var(--muted); }

.wr-bar {
  height: 4px;
  background: var(--border);
  border-radius: 4px;
  margin-top: 10px;
  overflow: hidden;
}

.wr-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--green), var(--cyan));
  border-radius: 4px;
  transition: width .6s;
}

.panel {
  background: rgba(17, 24, 39, .76);
  border: 1px solid var(--border);
  border-radius: 16px;
  overflow: hidden;
  margin-bottom: 18px;
}

.panel-head {
  padding: 13px 15px;
  border-bottom: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  gap: 10px;
  align-items: center;
}

.panel-title {
  font-weight: 800;
  font-size: 13px;
}

.panel-subtitle {
  color: var(--muted);
  font-size: 11px;
  margin-top: 2px;
}

.table-wrap {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  min-width: 850px;
}

th, td {
  padding: 11px 13px;
  border-bottom: 1px solid rgba(38, 50, 68, .7);
  text-align: left;
  vertical-align: middle;
  font-size: 12px;
}

th {
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .07em;
  font-size: 10px;
  font-weight: 800;
  background: rgba(15, 23, 42, .72);
}

tr:hover td {
  background: rgba(90, 167, 255, .035);
}

.badge {
  display: inline-block;
  padding: 4px 9px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 800;
  letter-spacing: .04em;
  text-transform: uppercase;
}

.badge-win, .badge-long, .badge-fired {
  background: rgba(53, 208, 127, .12);
  color: var(--green);
  border: 1px solid rgba(53, 208, 127, .2);
}

.badge-loss, .badge-short, .badge-invalidated {
  background: rgba(255, 92, 108, .12);
  color: var(--red);
  border: 1px solid rgba(255, 92, 108, .2);
}

.badge-pending, .badge-waiting {
  background: rgba(90, 167, 255, .12);
  color: var(--blue);
  border: 1px solid rgba(90, 167, 255, .2);
}

.badge-expired {
  background: rgba(143, 161, 183, .10);
  color: var(--muted);
  border: 1px solid rgba(143, 161, 183, .16);
}

.badge-config {
  background: rgba(167, 139, 250, .12);
  color: var(--purple);
  border: 1px solid rgba(167, 139, 250, .2);
}

.empty {
  text-align: center;
  padding: 34px 20px;
  color: var(--muted);
  font-size: 13px;
}

.price-stack {
  line-height: 1.65;
  white-space: nowrap;
}

#loading {
  position: fixed;
  inset: 0;
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 99;
  font-size: 14px;
  color: var(--muted);
}
</style>
</head>
<body>

<div id="loading">Loading…</div>

<header class="header">
  <div class="header-inner">
    <div>
      <div class="logo"><span>📡</span> MEXC Signal Bot</div>
      <div class="logo-sub">Breakout Retest + EMA/VWAP + ATR RR</div>
    </div>
    <div class="meta">
      <span class="status-pill connecting" id="wsStatus"><span class="status-dot"></span>Connecting</span>
      <span id="serverTime">—</span>
      <span id="lastUpdate">—</span>
    </div>
  </div>
</header>

<main class="container">

  <div class="toolbar">
    <div class="tabs">
      <button class="tab active" data-period="today" onclick="setPeriod('today')">Today</button>
      <button class="tab" data-period="week" onclick="setPeriod('week')">7 Days</button>
      <button class="tab" data-period="alltime" onclick="setPeriod('alltime')">All Time</button>
    </div>
    <button class="refresh-btn" onclick="manualRefresh()">↻ Refresh</button>
  </div>

  <div class="section-title">Performance</div>
  <div class="grid stats-grid">
    <div class="card"><div class="card-label">Signals</div><div class="card-value white" id="c-total">—</div><div class="card-small">Total selected period</div></div>
    <div class="card"><div class="card-label">Wins</div><div class="card-value green" id="c-wins">—</div><div class="card-small">Closed in profit</div></div>
    <div class="card"><div class="card-label">Losses</div><div class="card-value red" id="c-losses">—</div><div class="card-small">Closed by stop</div></div>
    <div class="card"><div class="card-label">Pending Signals</div><div class="card-value blue" id="c-pending">—</div><div class="card-small">Active trade outcomes</div></div>
    <div class="card">
      <div class="card-label">Win Rate</div>
      <div class="card-value green" id="c-winrate">—</div>
      <div class="wr-bar"><div class="wr-fill" id="wr-fill" style="width:0%"></div></div>
    </div>
    <div class="card"><div class="card-label">Net ROI</div><div class="card-value" id="c-roi">—</div><div class="card-small">Leverage adjusted</div></div>
    <div class="card"><div class="card-label">Best</div><div class="card-value green" id="c-best">—</div><div class="card-small">Best signal ROI</div></div>
    <div class="card"><div class="card-label">Worst</div><div class="card-value red" id="c-worst">—</div><div class="card-small">Worst signal ROI</div></div>
  </div>

  <div class="section-title">Runtime State</div>
  <div class="grid runtime-grid">
    <div class="card"><div class="card-label">Waiting Retests</div><div class="card-value blue" id="r-waiting">—</div><div class="card-small">Breakouts waiting for retest</div></div>
    <div class="card"><div class="card-label">Active Signals</div><div class="card-value yellow" id="r-active">—</div><div class="card-small">Open signal outcomes</div></div>
    <div class="card"><div class="card-label">Expired Setups</div><div class="card-value muted" id="r-expired">—</div><div class="card-small">Retests timed out</div></div>
    <div class="card"><div class="card-label">Invalidated Setups</div><div class="card-value red" id="r-invalidated">—</div><div class="card-small">Breakout failed before entry</div></div>
  </div>

  <div class="section-title">Current Strategy Setup</div>
  <div class="grid config-grid">
    <div class="card"><div class="card-label">Timeframes</div><div class="card-value cyan" id="cfg-tf">—</div><div class="card-small">Trend / Entry</div></div>
    <div class="card"><div class="card-label">Quality Filter</div><div class="card-value purple" id="cfg-quality">—</div><div class="card-small">Min score / setups per scan</div></div>
    <div class="card"><div class="card-label">Market WebSocket</div><div class="card-value green" id="cfg-ws">—</div><div class="card-small" id="cfg-ws-sub">—</div></div>
    <div class="card"><div class="card-label">RR Model</div><div class="card-value orange" id="cfg-rr">—</div><div class="card-small" id="cfg-rr-sub">—</div></div>
  </div>

  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="panel-title">Pending / Recent Breakout Setups</div>
        <div class="panel-subtitle">Breakout setups waiting, fired, expired, or invalidated</div>
      </div>
      <span class="badge badge-config" id="setup-count">—</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Symbol</th>
            <th>Dir</th>
            <th>Status</th>
            <th>Score</th>
            <th>RR</th>
            <th>Level / Zone</th>
            <th>TP / SL</th>
            <th>Expires</th>
          </tr>
        </thead>
        <tbody id="setupRows"></tbody>
      </table>
    </div>
  </section>

  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="panel-title">Recent Signals</div>
        <div class="panel-subtitle">Telegram signals and their outcomes</div>
      </div>
      <span class="badge badge-config" id="signal-count">—</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Symbol</th>
            <th>Dir</th>
            <th>Status</th>
            <th>Entry</th>
            <th>TP / SL</th>
            <th>ROI</th>
            <th>Generated</th>
          </tr>
        </thead>
        <tbody id="signalRows"></tbody>
      </table>
    </div>
  </section>

</main>

<script>
const TOKEN = new URLSearchParams(location.search).get("token") || "";
let period = "today";
let data = null;
let socket = null;
let reconnectTimer = null;
let reconnectDelayMs = 2000;
let lastMessageAt = null;

function setPeriod(p) {
  period = p;
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.dataset.period === p)
  );
  if (data) render();
}

function wsUrl() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const base = location.pathname.replace(/\/+$/, "");
  return `${protocol}//${location.host}${base}/ws?token=${encodeURIComponent(TOKEN)}`;
}

function setWsStatus(state) {
  const el = document.getElementById("wsStatus");
  el.classList.remove("disconnected", "connecting");

  if (state === "live") {
    el.innerHTML = `<span class="status-dot"></span>WS Live`;
  } else if (state === "connecting") {
    el.classList.add("connecting");
    el.innerHTML = `<span class="status-dot"></span>Connecting`;
  } else {
    el.classList.add("disconnected");
    el.innerHTML = `<span class="status-dot"></span>Disconnected`;
  }
}

function connectSocket() {
  clearTimeout(reconnectTimer);
  setWsStatus("connecting");

  try {
    socket = new WebSocket(wsUrl());
  } catch (e) {
    console.error(e);
    scheduleReconnect();
    return;
  }

  socket.onopen = () => {
    reconnectDelayMs = 2000;
    setWsStatus("live");
  };

  socket.onmessage = event => {
    try {
      data = JSON.parse(event.data);
      lastMessageAt = new Date();
      render();
      document.getElementById("loading").style.display = "none";
      document.getElementById("serverTime").textContent = data.server_time;
      document.getElementById("lastUpdate").textContent = `Updated ${lastMessageAt.toLocaleTimeString()}`;
      setWsStatus("live");
    } catch (e) {
      console.error(e);
    }
  };

  socket.onerror = () => {
    setWsStatus("disconnected");
  };

  socket.onclose = () => {
    setWsStatus("disconnected");
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    reconnectDelayMs = Math.min(reconnectDelayMs * 1.5, 15000);
    connectSocket();
  }, reconnectDelayMs);
}

async function manualRefresh() {
  try {
    const base = location.pathname.replace(/\/+$/, "");
    const res = await fetch(`${base}/api/data?token=${encodeURIComponent(TOKEN)}`);

    if (res.status === 401) {
      document.body.innerHTML = "<div style='padding:2rem;color:#ff5c6c;font-family:sans-serif'>401 — Invalid token. Add ?token=YOUR_TOKEN to the URL.</div>";
      return;
    }

    data = await res.json();
    lastMessageAt = new Date();
    render();
    document.getElementById("loading").style.display = "none";
    document.getElementById("serverTime").textContent = data.server_time;
    document.getElementById("lastUpdate").textContent = `Manual ${lastMessageAt.toLocaleTimeString()}`;
  } catch (e) {
    console.error(e);
  }
}

function render() {
  if (!data) return;

  renderStats();
  renderRuntime();
  renderConfig();
  renderSetups();
  renderSignals();
}

function renderStats() {
  const s = data[period];

  set("c-total", s.total);
  set("c-wins", s.wins);
  set("c-losses", s.losses);
  set("c-pending", s.pending);
  set("c-winrate", s.win_rate + "%");

  document.getElementById("wr-fill").style.width = s.win_rate + "%";

  const roi = s.net_roi;
  const roiEl = document.getElementById("c-roi");
  roiEl.textContent = (roi >= 0 ? "+" : "") + roi + "%";
  roiEl.className = "card-value " + (roi >= 0 ? "green" : "red");

  set("c-best", "+" + s.best + "%");
  set("c-worst", s.worst + "%");
}

function renderRuntime() {
  const r = data.runtime;

  set("r-waiting", r.waiting_setups);
  set("r-active", r.active_signals);
  set("r-expired", r.expired_setups);
  set("r-invalidated", r.invalidated_setups);
}

function renderConfig() {
  const c = data.config;

  set("cfg-tf", `${c.trend_tf} / ${c.entry_tf}`);
  set("cfg-quality", `${c.min_score} / ${c.setups_per_scan}`);
  set("cfg-ws", c.websocket_enabled ? "ON" : "OFF");
  set("cfg-ws-sub", `Cache ${c.candle_cache_limit} candles`);
  set("cfg-rr", `${c.target_rr}R`);
  set("cfg-rr-sub", `ATR${c.atr_period} + EMA${c.ema_period}/VWAP${c.vwap_lookback}`);
}

function renderSetups() {
  const rows = data.setups || [];
  set("setup-count", rows.length + " rows");

  const tbody = document.getElementById("setupRows");

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="9"><div class="empty">No setups recorded yet.</div></td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map(r => {
    const dir = (r.direction || "").toLowerCase();
    const status = (r.status || "").toLowerCase();
    const sym = (r.symbol || "").replace("_USDT", "/USDT");

    return `
      <tr>
        <td>#${r.id}</td>
        <td><strong>${sym}</strong><br><span class="muted">${r.trend_tf}/${r.entry_tf}</span></td>
        <td><span class="badge badge-${dir}">${r.direction}</span></td>
        <td><span class="badge badge-${status}">${r.status}</span></td>
        <td><strong>${fmtNum(r.score)}</strong></td>
        <td>${fmtNum(r.rr_estimate)}</td>
        <td class="price-stack">
          <span class="muted">Level</span> ${r.level_display}<br>
          <span class="muted">Zone</span> ${r.zone_low_display} - ${r.zone_high_display}
        </td>
        <td class="price-stack">
          <span class="green">TP</span> ${r.tp_display}<br>
          <span class="red">SL</span> ${r.sl_display}
        </td>
        <td>${r.expires_display}</td>
      </tr>
    `;
  }).join("");
}

function renderSignals() {
  const rows = data.recent || [];
  set("signal-count", rows.length + " rows");

  const tbody = document.getElementById("signalRows");

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="8"><div class="empty">No signals recorded yet.</div></td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map(r => {
    const dir = (r.direction || "").toLowerCase();
    const status = (r.status || "").toLowerCase();
    const sym = (r.symbol || "").replace("_USDT", "/USDT");
    const roi = r.pnl_roi != null ? (r.pnl_roi >= 0 ? "+" : "") + Number(r.pnl_roi).toFixed(1) + "%" : "—";
    const roiCls = r.pnl_roi > 0 ? "green" : r.pnl_roi < 0 ? "red" : "muted";

    return `
      <tr>
        <td>#${r.id}</td>
        <td><strong>${sym}</strong></td>
        <td><span class="badge badge-${dir}">${r.direction}</span></td>
        <td><span class="badge badge-${status}">${r.status}</span></td>
        <td>${r.entry_display}</td>
        <td class="price-stack">
          <span class="green">TP</span> ${r.tp_display}<br>
          <span class="red">SL</span> ${r.sl_display}
        </td>
        <td class="${roiCls}"><strong>${roi}</strong></td>
        <td>${r.display_time}</td>
      </tr>
    `;
  }).join("");
}

function fmtNum(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (Number.isNaN(n)) return value;
  return n.toFixed(1).replace(".0", "");
}

function set(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

connectSocket();
manualRefresh();
</script>
</body>
</html>
"""


@app.get("/")
async def index(token: str = Query("")):
    if token != WEBUI_TOKEN:
        return HTMLResponse(
            "<div style='font-family:monospace;padding:2rem;color:#ff5c6c'>"
            "401 — Invalid token. Add <code>?token=YOUR_TOKEN</code> to the URL.</div>",
            status_code=401,
        )

    return HTMLResponse(HTML)


if __name__ == "__main__":
    print(f"Dashboard → http://0.0.0.0:{PORT}/?token={WEBUI_TOKEN}")
    print(f"WebSocket → ws://0.0.0.0:{PORT}/ws?token={WEBUI_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")