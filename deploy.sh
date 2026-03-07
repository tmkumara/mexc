#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# MEXC Signal Bot — Ubuntu 22.04 deployment script
# Usage:  bash deploy.sh
# ─────────────────────────────────────────────────────────────────────
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="mexc-signal-bot"
PYTHON="python3"
VENV="$APP_DIR/.venv"

echo "=== MEXC Signal Bot Deployment ==="
echo "App directory: $APP_DIR"

# ── 1. System packages ───────────────────────────────────────────
echo "[1/5] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv git

# ── 2. Python virtual environment ───────────────────────────────
echo "[2/5] Setting up Python virtual environment..."
$PYTHON -m venv "$VENV"
source "$VENV/bin/activate"
pip install --upgrade pip -q
pip install -r "$APP_DIR/requirements.txt" -q
deactivate
echo "Virtual environment ready at $VENV"

# ── 3. .env file ─────────────────────────────────────────────────
if [ ! -f "$APP_DIR/.env" ]; then
    echo ""
    echo "[3/5] Creating .env — please fill in your credentials:"
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo ""
    read -rp "  Telegram Bot Token : " TOKEN
    read -rp "  Telegram Channel ID: " CHANNEL_ID
    sed -i "s|your_bot_token_here|$TOKEN|g"       "$APP_DIR/.env"
    sed -i "s|your_channel_id_here|$CHANNEL_ID|g" "$APP_DIR/.env"
    echo ".env saved."
else
    echo "[3/5] .env already exists — skipping."
fi

# ── 4. Systemd service ───────────────────────────────────────────
echo "[4/5] Installing systemd service..."

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=MEXC Futures Signal Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/python $APP_DIR/main.py
Restart=always
RestartSec=10
StandardOutput=append:$APP_DIR/mexc_bot.log
StandardError=append:$APP_DIR/mexc_bot.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

# ── 5. Start ─────────────────────────────────────────────────────
echo "[5/5] Starting service..."
sudo systemctl restart "$SERVICE_NAME"
sleep 3
sudo systemctl status "$SERVICE_NAME" --no-pager

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Deployment complete!"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl status $SERVICE_NAME"
echo "    sudo systemctl restart $SERVICE_NAME"
echo "    sudo systemctl stop $SERVICE_NAME"
echo "    tail -f $APP_DIR/mexc_bot.log"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
