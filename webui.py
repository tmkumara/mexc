"""Read-only dashboard for the live NWE-RSI scanner and legacy signal history."""

import asyncio
from hashlib import pbkdf2_hmac
import json
from hmac import compare_digest, digest
from urllib.parse import parse_qs
import os
import sqlite3
from secrets import token_bytes
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

load_dotenv()

WEBUI_TOKEN = os.getenv("WEBUI_TOKEN", "")
AUTH_COOKIE = "mexc_dashboard_session"
PUBLIC_PATH = "/mexc/dashboard/"
CREDENTIAL_FILE = Path(os.getenv("MEXC_DASHBOARD_CREDENTIAL_FILE", "/etc/mexc-dashboard/credentials.json"))
LOCAL_SESSION_KEY = token_bytes(32)
PORT = int(os.getenv("WEBUI_PORT", 6060))
DB_PATH = os.getenv("DB_PATH", "signals.db")

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
            "total": 0, "wins": 0, "losses": 0, "breakevens": 0,
            "pending": 0, "expired": 0,
            "win_rate": 0.0, "net_roi": 0.0, "best": 0.0, "worst": 0.0,
            "longs": 0, "shorts": 0,
        }

    if since:
        rows = _query(
            "SELECT * FROM signals WHERE generated_at >= ?",
            (since.isoformat(),),
        )
    else:
        rows = _query("SELECT * FROM signals")

    total = len(rows)
    wins = [r for r in rows if r.get("status") == "win"]
    losses = [r for r in rows if r.get("status") == "loss"]
    breakevens = [r for r in rows if r.get("status") == "breakeven"]
    pending = [r for r in rows if r.get("status") == "pending"]
    expired = [r for r in rows if r.get("status") == "expired"]
    longs = [r for r in rows if r.get("direction") == "LONG"]
    shorts = [r for r in rows if r.get("direction") == "SHORT"]

    closed = len(wins) + len(losses)
    win_rate = (len(wins) / closed * 100) if closed else 0.0
    net_roi = sum(r.get("pnl_roi") or 0 for r in rows if r.get("status") in ("win", "loss", "breakeven"))
    best = max((r.get("pnl_roi") or 0 for r in wins), default=0.0)
    worst = min((r.get("pnl_roi") or 0 for r in losses), default=0.0)

    return {
        "total": total,
        "wins": len(wins), "losses": len(losses), "breakevens": len(breakevens),
        "pending": len(pending), "expired": len(expired),
        "win_rate": round(win_rate, 1),
        "net_roi": round(net_roi, 1),
        "best": round(best, 1), "worst": round(worst, 1),
        "longs": len(longs), "shorts": len(shorts),
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


def get_order_watches() -> list[dict]:
    if not _table_exists("nwe_order_watch"):
        return []
    columns = {row["name"] for row in _query("PRAGMA table_info(nwe_order_watch)")}
    timeframe = "timeframe" if "timeframe" in columns else "'4H' AS timeframe"
    return _query(f"""
        SELECT symbol, {timeframe}, direction, entry, leverage, signal_at, status, trigger_price, notified_at
        FROM nwe_order_watch ORDER BY id DESC LIMIT 50
    """)


def get_runtime_status() -> dict:
    return {
        "db_exists": _db_exists(),
        "alerts_today": _count_table("nwe_order_watch", "signal_at >= ?", (
            datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0).isoformat(),)),
        "watching": _count_table("nwe_order_watch", "status IN ('watching','triggered')"),
        "reminders_sent": _count_table("nwe_order_watch", "status = 'notified'"),
    }


def get_strategy_config() -> dict:
    from nwe_rsi_public_alerts import NAME, SYMBOLS, LEVERAGE, ROI

    now = datetime.now(timezone.utc)
    next_scan = now.replace(minute=18, second=0, microsecond=0)
    if next_scan <= now:
        next_scan += timedelta(hours=1)
    local = next_scan.astimezone(timezone(timedelta(hours=5,minutes=30)))
    return {
        "strategy": NAME, "timeframe": "1H / 4H", "coins": len(SYMBOLS),
        "next_scan_lkt": local.strftime("%b %d · %I:%M %p LKT"),
        "leverage": LEVERAGE, "tp_roi_pct": 100*ROI,
        "dry_run": _safe_config_value("DRY_RUN", True),
    }


def build_payload() -> dict:
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = now - timedelta(days=7)

    return {
        "today": get_stats(today),
        "week": get_stats(week),
        "alltime": get_stats(),
        "recent": get_recent_signals(30),
        "order_watches": get_order_watches(),
        "runtime": get_runtime_status(),
        "config": get_strategy_config(),
        "server_time": now.strftime("%Y-%m-%d %H:%M UTC"),
        "push_interval_seconds": WS_PUSH_SECONDS,
    }


# ── API endpoints ─────────────────────────────────────────────────

def _proxy_login(request) -> bool:
    return request.headers.get("x-forwarded-prefix") == PUBLIC_PATH.rstrip("/")


def _credentials():
    try:
        return json.loads(CREDENTIAL_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _valid_admin(params) -> bool:
    record = _credentials()
    if not record:
        return False
    username = params.get("username", [""])[0]
    password = params.get("password", [""])[0]
    candidate = pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(record["salt"]), 310000)
    return compare_digest(username.encode(), record["username"].encode()) and compare_digest(
        candidate, bytes.fromhex(record["hash"]))


def _session_key(request):
    if not _proxy_login(request):
        return LOCAL_SESSION_KEY
    record = _credentials()
    return bytes.fromhex(record["session_key"]) if record else None


def _session_value(key, expiry) -> str:
    return f"{expiry}."+digest(key, str(expiry).encode(), "sha256").hex()


def _valid_token(token: str) -> bool:
    return bool(WEBUI_TOKEN and compare_digest(token.encode(), WEBUI_TOKEN.encode()))


def _authorized(request, token: str = "") -> bool:
    if not _proxy_login(request) and _valid_token(token):
        return True
    cookie = request.cookies.get(AUTH_COOKIE, "")
    expiry, _, _ = cookie.partition(".")
    if not expiry.isdecimal() or len(expiry) > 12 or int(expiry) <= time.time():
        return False
    key = _session_key(request)
    return bool(key and compare_digest(cookie.encode(), _session_value(key, int(expiry)).encode()))


def _auth(request: Request, token: str):
    if not _authorized(request, token):
        raise HTTPException(status_code=401, detail="Dashboard sign-in required")


@app.get("/api/data")
async def api_data(request: Request, token: str = Query("")):
    _auth(request, token)
    return JSONResponse(build_payload(), headers=PRIVATE_HEADERS)


@app.get("/api/health")
async def api_health(request: Request, token: str = Query("")):
    _auth(request, token)

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

    if not _authorized(websocket, token):
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
<title>MEXC Futures Dashboard</title>
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
  grid-template-columns: repeat(3, 1fr);
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

.badge-breakeven {
  background: rgba(245, 200, 75, .12);
  color: var(--yellow);
  border: 1px solid rgba(245, 200, 75, .2);
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
      <div class="logo"><span>📡</span> MEXC Futures Dashboard</div>
      <div class="logo-sub">1H / 4H Nadaraya–Watson Envelope + RSI</div>
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

  <div class="section-title">Legacy signal performance</div>
  <div class="grid stats-grid">
    <div class="card"><div class="card-label">Signals</div><div class="card-value white" id="c-total">—</div><div class="card-small">Total selected period</div></div>
    <div class="card"><div class="card-label">Wins</div><div class="card-value green" id="c-wins">—</div><div class="card-small">Closed in profit</div></div>
    <div class="card"><div class="card-label">Losses</div><div class="card-value red" id="c-losses">—</div><div class="card-small">Closed by stop</div></div>
    <div class="card"><div class="card-label">Breakeven</div><div class="card-value yellow" id="c-breakeven">—</div><div class="card-small">Stopped at entry</div></div>
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
    <div class="card"><div class="card-label">Alerts today</div><div class="card-value green" id="r-active-long">—</div><div class="card-small">UTC day, registered alerts</div></div>
    <div class="card"><div class="card-label">Watching now</div><div class="card-value red" id="r-active-short">—</div><div class="card-small">Manually marked orders</div></div>
    <div class="card"><div class="card-label">Reminders sent</div><div class="card-value yellow" id="r-active">—</div><div class="card-small">Completed breakeven watches</div></div>
  </div>

  <div class="section-title">Current Strategy Setup</div>
  <div class="grid config-grid">
    <div class="card"><div class="card-label">Timeframe</div><div class="card-value cyan" id="cfg-tf">—</div><div class="card-small">Completed NWE + RSI candles</div></div>
    <div class="card"><div class="card-label">Contracts</div><div class="card-value purple" id="cfg-quality">—</div><div class="card-small">MEXC USDT perpetuals</div></div>
    <div class="card"><div class="card-label">Next scan</div><div class="card-value green" id="cfg-confirm">—</div><div class="card-small" id="cfg-confirm-sub">—</div></div>
    <div class="card"><div class="card-label">Target reference</div><div class="card-value orange" id="cfg-rr">—</div><div class="card-small" id="cfg-rr-sub">—</div></div>
  </div>

  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="panel-title">Order Watch</div>
        <div class="panel-subtitle">Telegram alerts and manual breakeven watches</div>
      </div>
      <span class="badge badge-config" id="pending-count">—</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Dir</th>
            <th>Status</th>
            <th>Entry</th>
            <th>Leverage</th>
            <th>Trigger price</th>
            <th>Signal close UTC</th>
            <th>Reminder UTC</th>
          </tr>
        </thead>
        <tbody id="pendingRows"></tbody>
      </table>
    </div>
  </section>

  <section class="panel">
    <div class="panel-head">
      <div>
        <div class="panel-title">Legacy Signals</div>
        <div class="panel-subtitle">Historical scanner outcomes</div>
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
  return `${protocol}//${location.host}${base}/ws`;
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
    const res = await fetch(`${base}/api/data`);

    if (res.status === 401) {
      location.reload();
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
  renderPendingSetups();
  renderSignals();
}

function renderStats() {
  const s = data[period];

  set("c-total", s.total);
  set("c-wins", s.wins);
  set("c-losses", s.losses);
  set("c-breakeven", s.breakevens);
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

  set("r-active-long", r.alerts_today);
  set("r-active-short", r.watching);
  set("r-active", r.reminders_sent);
}

function renderConfig() {
  const c = data.config;

  set("cfg-tf", c.timeframe);
  set("cfg-quality", c.coins);
  set("cfg-confirm", c.next_scan_lkt);
  set("cfg-confirm-sub", c.dry_run ? "Dry run" : "Public Telegram alerts");
  set("cfg-rr", `+${c.tp_roi_pct}%`);
  set("cfg-rr-sub", `${c.leverage}× modeled net before funding · no initial stop`);
}

function renderPendingSetups() {
  const rows = data.order_watches || [];
  set("pending-count", rows.length + " recorded");

  const tbody = document.getElementById("pendingRows");

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="8"><div class="empty">No NWE alerts recorded yet.</div></td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map(r => {
    const dir = (r.direction || "").toLowerCase();
    const sym = (r.symbol || "").replace("_USDT", "/USDT");

    return `
      <tr>
        <td><strong>${sym}</strong> · ${r.timeframe}</td>
        <td><span class="badge badge-${dir}">${r.direction}</span></td>
        <td>${r.status || "—"}</td>
        <td>${fmtNum(r.entry)}</td>
        <td>${r.leverage}×</td>
        <td>${fmtNum(r.trigger_price)}</td>
        <td>${r.signal_at || "—"}</td>
        <td>${r.notified_at || "—"}</td>
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


LOGIN_HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>MEXC dashboard sign-in</title>
<body style="background:#101623;color:#f1f5ff;font:16px system-ui;max-width:25rem;margin:12vh auto;padding:1rem">
<h1>MEXC Dashboard</h1>
<form method="post">
<label for="token">Dashboard password</label><br>
<input id="token" name="token" type="password" autocomplete="current-password" required
       style="margin:8px 0;padding:8px;width:100%;box-sizing:border-box">
<button type="submit">Sign in</button>
</form></body></html>"""

ADMIN_LOGIN_HTML = LOGIN_HTML.replace(
    '<label for="token">', '<label for="username">Username</label><br>'
    '<input id="username" name="username" autocomplete="username" required '
    'style="margin:8px 0;padding:8px;width:100%;box-sizing:border-box"><br><label for="token">'
).replace('name="token"', 'name="password"')

PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _sign_in(request):
    response = RedirectResponse("./", status_code=303, headers=PRIVATE_HEADERS)
    proxied = _proxy_login(request)
    expiry = int(time.time())+86400
    response.set_cookie(AUTH_COOKIE, _session_value(_session_key(request),expiry), max_age=86400, httponly=True,
                        path=PUBLIC_PATH if proxied else "/",
                        secure=proxied or request.url.scheme=="https", samesite="strict")
    return response


@app.get("/")
async def index(request: Request, token: str = Query("")):
    if not _proxy_login(request) and _valid_token(token):
        return _sign_in(request)
    form = ADMIN_LOGIN_HTML if _proxy_login(request) else LOGIN_HTML
    return HTMLResponse(HTML if _authorized(request) else form, headers=PRIVATE_HEADERS)


@app.post("/")
async def login(request: Request):
    body = await request.body()
    if len(body) <= 1024:
        params = parse_qs(body.decode("utf-8", errors="replace"))
        authorized = (await asyncio.to_thread(_valid_admin, params) if _proxy_login(request)
                      else _valid_token(params.get("token", [""])[0]))
        if authorized:
            return _sign_in(request)
    form = ADMIN_LOGIN_HTML if _proxy_login(request) else LOGIN_HTML
    return HTMLResponse(form.replace("<form", '<p role="alert">Incorrect dashboard password.</p><form'),
                        status_code=401, headers=PRIVATE_HEADERS)


if __name__ == "__main__":
    print(f"Dashboard listening on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
