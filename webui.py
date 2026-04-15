"""
Simple dashboard for the MEXC Signal Bot.

Run:   python webui.py
URL:   http://<server>:8080/?token=<WEBUI_TOKEN>

Set WEBUI_TOKEN in .env (defaults to "mexc123").
"""

import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

load_dotenv()

WEBUI_TOKEN = os.getenv("WEBUI_TOKEN", "mexc123")
PORT        = int(os.getenv("WEBUI_PORT", 6060))
DB_PATH     = os.getenv("DB_PATH", "signals.db")

app = FastAPI(docs_url=None, redoc_url=None)


# ── data helpers ──────────────────────────────────────────────────

def _query(sql: str, params: tuple = ()) -> list[dict]:
    if not Path(DB_PATH).exists():
        return []
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_stats(since: datetime | None = None) -> dict:
    if since:
        rows = _query(
            "SELECT * FROM signals WHERE generated_at >= ?",
            (since.isoformat(),),
        )
    else:
        rows = _query("SELECT * FROM signals")

    total   = len(rows)
    wins    = [r for r in rows if r["status"] == "win"]
    losses  = [r for r in rows if r["status"] == "loss"]
    pending = [r for r in rows if r["status"] == "pending"]
    expired = [r for r in rows if r["status"] == "expired"]
    closed  = len(wins) + len(losses)
    win_rate = (len(wins) / closed * 100) if closed else 0
    net_roi  = sum(r["pnl_roi"] or 0 for r in rows if r["status"] in ("win", "loss"))
    best     = max((r["pnl_roi"] or 0 for r in wins),   default=0)
    worst    = min((r["pnl_roi"] or 0 for r in losses), default=0)

    return {
        "total": total, "wins": len(wins), "losses": len(losses),
        "pending": len(pending), "expired": len(expired),
        "win_rate": round(win_rate, 1), "net_roi": round(net_roi, 1),
        "best": round(best, 1), "worst": round(worst, 1),
    }


def get_recent_signals(limit: int = 30) -> list[dict]:
    return _query(
        "SELECT * FROM signals ORDER BY generated_at DESC LIMIT ?",
        (limit,),
    )


# ── API endpoints ─────────────────────────────────────────────────

def _auth(token: str):
    if token != WEBUI_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


@app.get("/api/data")
async def api_data(token: str = Query("")):
    _auth(token)
    now   = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week  = now - timedelta(days=7)
    return JSONResponse({
        "today":   get_stats(today),
        "week":    get_stats(week),
        "alltime": get_stats(),
        "recent":  get_recent_signals(30),
        "server_time": now.strftime("%Y-%m-%d %H:%M UTC"),
    })


# ── dashboard HTML ────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MEXC Bot Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:      #0d1117;
  --surface: #161b22;
  --border:  #30363d;
  --text:    #e6edf3;
  --muted:   #8b949e;
  --green:   #3fb950;
  --red:     #f85149;
  --yellow:  #e3b341;
  --blue:    #58a6ff;
}

body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Inter', sans-serif;
  font-size: 14px;
  min-height: 100vh;
}

/* ── header ── */
.header {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 12px 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
}
.logo {
  font-size: 15px; font-weight: 700;
  display: flex; align-items: center; gap: 8px;
}
.meta {
  color: var(--muted); font-size: 11px;
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
}
.status-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--green); box-shadow: 0 0 6px var(--green);
  display: inline-block; margin-right: 5px;
}

/* ── layout ── */
.container { max-width: 1200px; margin: 0 auto; padding: 16px; }

.section-title {
  font-size: 11px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: .08em;
  margin-bottom: 10px;
}

/* ── toolbar ── */
.toolbar {
  display: flex; align-items: center; gap: 8px;
  flex-wrap: wrap; margin-bottom: 16px;
}
.tabs { display: flex; gap: 6px; flex-wrap: wrap; }
.tab {
  padding: 5px 14px; border-radius: 20px;
  border: 1px solid var(--border);
  background: transparent; color: var(--muted);
  font-family: inherit; font-size: 12px; font-weight: 500;
  cursor: pointer; transition: all .15s; white-space: nowrap;
}
.tab:hover { border-color: var(--blue); color: var(--blue); }
.tab.active { background: #1f3a5c; border-color: var(--blue); color: var(--blue); }

.refresh-btn {
  padding: 5px 12px; border-radius: 20px;
  border: 1px solid var(--border);
  background: transparent; color: var(--muted);
  font-family: inherit; font-size: 12px;
  cursor: pointer; transition: all .15s; white-space: nowrap;
  margin-left: auto;
}
.refresh-btn:hover { border-color: var(--blue); color: var(--blue); }

/* ── stat cards — 4 cols desktop, 2 cols mobile ── */
.cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 20px;
}
@media (max-width: 900px) {
  .cards { grid-template-columns: repeat(4, 1fr); }
}
@media (max-width: 600px) {
  .cards { grid-template-columns: repeat(2, 1fr); gap: 8px; }
}

.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px;
  transition: border-color .2s;
}
.card:hover { border-color: #484f58; }
.card-label {
  font-size: 10px; color: var(--muted); font-weight: 600;
  text-transform: uppercase; letter-spacing: .07em; margin-bottom: 6px;
}
.card-value { font-size: 26px; font-weight: 700; line-height: 1; }

@media (max-width: 600px) {
  .card { padding: 12px; }
  .card-value { font-size: 22px; }
}

.green  { color: var(--green); }
.red    { color: var(--red); }
.blue   { color: var(--blue); }
.white  { color: var(--text); }

.wr-bar  { height: 3px; background: var(--border); border-radius: 2px; margin-top: 7px; overflow: hidden; }
.wr-fill { height: 100%; background: var(--green); border-radius: 2px; transition: width .6s; }

/* ── signal list ── */
.sig-list { display: flex; flex-direction: column; gap: 8px; }

.sig-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 14px;
  display: grid;
  grid-template-columns: auto 1fr auto auto;
  align-items: center;
  gap: 10px 14px;
  transition: border-color .2s;
}
.sig-card:hover { border-color: #484f58; }

/* left accent by direction */
.sig-card.long  { border-left: 3px solid var(--green); }
.sig-card.short { border-left: 3px solid var(--red); }

.sig-id    { color: var(--muted); font-size: 11px; min-width: 28px; }
.sig-main  { min-width: 0; }
.sig-sym   { font-weight: 700; font-size: 14px; }
.sig-meta  { font-size: 11px; color: var(--muted); margin-top: 2px;
             white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sig-prices { text-align: right; font-size: 12px; line-height: 1.8; white-space: nowrap; }
.sig-status { text-align: right; }

.badge {
  display: inline-block; padding: 3px 9px; border-radius: 10px;
  font-size: 11px; font-weight: 600; letter-spacing: .03em;
}
.badge-win     { background: #0a3d1f; color: var(--green); }
.badge-loss    { background: #3d1111; color: var(--red); }
.badge-pending { background: #1f2d3d; color: var(--blue); }
.badge-expired { background: #1c1c1c; color: var(--muted); }
.badge-long    { background: #0a3d1f; color: var(--green); }
.badge-short   { background: #3d1111; color: var(--red); }

.roi-val { font-size: 13px; font-weight: 600; margin-top: 4px; }

/* mobile: stack prices and status below main row */
@media (max-width: 480px) {
  .sig-card {
    grid-template-columns: auto 1fr;
    grid-template-rows: auto auto;
  }
  .sig-prices { grid-column: 2; text-align: left; }
  .sig-status { grid-column: 2; text-align: left; display: flex; gap: 8px; align-items: center; }
}

/* ── empty state ── */
.empty {
  text-align: center; padding: 40px 20px;
  color: var(--muted); font-size: 13px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px;
}

/* ── loading overlay ── */
#loading {
  position: fixed; inset: 0; background: var(--bg);
  display: flex; align-items: center; justify-content: center;
  z-index: 99; font-size: 14px; color: var(--muted);
}
</style>
</head>
<body>

<div id="loading">Loading…</div>

<div class="header">
  <div class="logo">
    <span>📡</span> MEXC Signal Bot
  </div>
  <div class="meta">
    <span><span class="status-dot"></span>Live</span>
    <span id="serverTime">—</span>
    <span id="nextRefresh"></span>
  </div>
</div>

<div class="container">

  <!-- toolbar -->
  <div class="toolbar">
    <div class="tabs">
      <button class="tab active" data-period="today"   onclick="setPeriod('today')">Today</button>
      <button class="tab"        data-period="week"    onclick="setPeriod('week')">7 Days</button>
      <button class="tab"        data-period="alltime" onclick="setPeriod('alltime')">All Time</button>
    </div>
    <button class="refresh-btn" onclick="load()">↻ Refresh</button>
  </div>

  <!-- stat cards -->
  <div class="section-title">Performance</div>
  <div class="cards">
    <div class="card"><div class="card-label">Signals</div><div class="card-value white" id="c-total">—</div></div>
    <div class="card"><div class="card-label">Wins</div><div class="card-value green" id="c-wins">—</div></div>
    <div class="card"><div class="card-label">Losses</div><div class="card-value red" id="c-losses">—</div></div>
    <div class="card"><div class="card-label">Pending</div><div class="card-value blue" id="c-pending">—</div></div>
    <div class="card">
      <div class="card-label">Win Rate</div>
      <div class="card-value green" id="c-winrate">—</div>
      <div class="wr-bar"><div class="wr-fill" id="wr-fill" style="width:0%"></div></div>
    </div>
    <div class="card"><div class="card-label">Net ROI</div><div class="card-value" id="c-roi">—</div></div>
    <div class="card"><div class="card-label">Best</div><div class="card-value green" id="c-best">—</div></div>
    <div class="card"><div class="card-label">Worst</div><div class="card-value red" id="c-worst">—</div></div>
  </div>

  <!-- signal list -->
  <div class="section-title" style="margin-top:4px">Recent Signals</div>
  <div id="siglist"></div>

</div>

<script>
const TOKEN = new URLSearchParams(location.search).get("token") || "";
let period  = "today";
let data    = null;
let countdown = 30;
let timer;

function setPeriod(p) {
  period = p;
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.dataset.period === p)
  );
  if (data) render();
}

async function load() {
  try {
    // Use relative URL so it works under any path prefix (e.g. /dashboard/)
    const base = location.pathname.replace(/\/+$/, '');
    const res = await fetch(`${base}/api/data?token=${TOKEN}`);
    if (res.status === 401) {
      document.body.innerHTML = "<div style='padding:2rem;color:#f85149;font-family:sans-serif'>401 — Invalid token. Add ?token=YOUR_TOKEN to the URL.</div>";
      return;
    }
    data = await res.json();
    render();
    document.getElementById("loading").style.display = "none";
    document.getElementById("serverTime").textContent = data.server_time;
  } catch(e) { console.error(e); }
  resetCountdown();
}

function render() {
  const s = data[period];

  set("c-total",   s.total);
  set("c-wins",    s.wins);
  set("c-losses",  s.losses);
  set("c-pending", s.pending);
  set("c-winrate", s.win_rate + "%");
  document.getElementById("wr-fill").style.width = s.win_rate + "%";

  const roi = s.net_roi;
  const roiEl = document.getElementById("c-roi");
  roiEl.textContent = (roi >= 0 ? "+" : "") + roi + "%";
  roiEl.className   = "card-value " + (roi >= 0 ? "green" : "red");

  set("c-best",  "+" + s.best + "%");
  set("c-worst", s.worst + "%");

  renderSignals(data.recent);
}

function renderSignals(list) {
  const el = document.getElementById("siglist");
  if (!list.length) {
    el.innerHTML = `<div class="empty">No signals recorded yet.</div>`;
    return;
  }

  el.innerHTML = `<div class="sig-list">` + list.map(r => {
    const dir    = r.direction.toLowerCase();
    const sym    = r.symbol.replace("_USDT", "/USDT");
    const roi    = r.pnl_roi != null ? (r.pnl_roi >= 0 ? "+" : "") + r.pnl_roi.toFixed(1) + "%" : null;
    const roiCls = r.pnl_roi > 0 ? "green" : r.pnl_roi < 0 ? "red" : "muted";
    const time   = r.generated_at ? r.generated_at.slice(0,16).replace("T"," ") + " UTC" : "—";

    return `
    <div class="sig-card ${dir}">
      <div class="sig-id">#${r.id}</div>
      <div class="sig-main">
        <div class="sig-sym">
          <span class="badge badge-${dir}">${r.direction}</span>
          &nbsp;${sym}
        </div>
        <div class="sig-meta">${time}</div>
      </div>
      <div class="sig-prices">
        <div>Entry <strong>$${fmt(r.entry_price)}</strong></div>
        <div style="color:var(--green)">TP &nbsp;$${fmt(r.tp_price)}</div>
        <div style="color:var(--red)">SL &nbsp;$${fmt(r.sl_price)}</div>
      </div>
      <div class="sig-status">
        <span class="badge badge-${r.status}">${r.status.toUpperCase()}</span>
        ${roi ? `<div class="roi-val ${roiCls}">${roi}</div>` : ""}
      </div>
    </div>`;
  }).join("") + `</div>`;
}

function fmt(n) {
  if (n == null) return "—";
  const f = parseFloat(n);
  return f >= 1 ? f.toFixed(4) : f.toPrecision(5);
}

function set(id, val) { document.getElementById(id).textContent = val; }

function resetCountdown() {
  countdown = 30;
  clearInterval(timer);
  timer = setInterval(() => {
    countdown--;
    document.getElementById("nextRefresh").textContent = `↻ ${countdown}s`;
    if (countdown <= 0) load();
  }, 1000);
}

load();
</script>
</body>
</html>
"""


@app.get("/")
async def index(token: str = Query("")):
    if token != WEBUI_TOKEN:
        return HTMLResponse(
            "<div style='font-family:monospace;padding:2rem;color:#f85149'>"
            "401 — Invalid token. Add <code>?token=YOUR_TOKEN</code> to the URL.</div>",
            status_code=401,
        )
    return HTMLResponse(HTML)


if __name__ == "__main__":
    print(f"Dashboard → http://0.0.0.0:{PORT}/?token={WEBUI_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
