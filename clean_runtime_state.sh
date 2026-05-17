#!/usr/bin/env bash

set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-mexc-bot}"
PROJECT_DIR="${PROJECT_DIR:-/opt/signals}"
VENV_DIR="${VENV_DIR:-venv}"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " MEXC Bot Runtime State Cleaner"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Service     : ${SERVICE_NAME}"
echo "Project dir : ${PROJECT_DIR}"
echo "Venv dir    : ${VENV_DIR}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ ! -d "${PROJECT_DIR}" ]; then
  echo "❌ Project directory not found: ${PROJECT_DIR}"
  exit 1
fi

cd "${PROJECT_DIR}"

if [ ! -d "${VENV_DIR}" ]; then
  echo "❌ Virtual environment not found: ${PROJECT_DIR}/${VENV_DIR}"
  exit 1
fi

echo "⏹ Stopping service: ${SERVICE_NAME}"
sudo systemctl stop "${SERVICE_NAME}"

echo "🐍 Activating virtual environment"
# shellcheck disable=SC1090
source "${VENV_DIR}/bin/activate"

echo "🧹 Expiring waiting setups and active signals"

python - <<'PY'
import sqlite3
from datetime import datetime, timezone
from config import DB_PATH

now = datetime.now(timezone.utc).isoformat()

con = sqlite3.connect(DB_PATH)
cur = con.cursor()

cur.execute("""
    UPDATE pending_setups
    SET status = 'expired',
        updated_at = ?
    WHERE status = 'waiting'
""", (now,))

expired_setups = cur.rowcount

cur.execute("""
    UPDATE signals
    SET status = 'expired',
        pnl_roi = 0.0,
        closed_at = ?
    WHERE status = 'pending'
""", (now,))

expired_signals = cur.rowcount

con.commit()
con.close()

print(f"✅ Expired waiting setups: {expired_setups}")
print(f"✅ Expired active signals: {expired_signals}")
PY

echo "▶️ Starting service: ${SERVICE_NAME}"
sudo systemctl start "${SERVICE_NAME}"

echo "📌 Service status:"
sudo systemctl --no-pager status "${SERVICE_NAME}" || true

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Runtime state cleaned and bot restarted"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "To watch logs, run:"
echo "journalctl -u ${SERVICE_NAME} -f"