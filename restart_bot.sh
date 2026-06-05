#!/usr/bin/env bash
set -euo pipefail

cd /opt/signals

echo "Stopping mexc-bot..."
sudo systemctl stop mexc-bot || true

echo "Backing up mexc_bot.log..."
mkdir -p logs/archive

if [ -f mexc_bot.log ]; then
  ts=$(date +"%Y%m%d_%H%M%S")
  cp mexc_bot.log "logs/archive/mexc_bot_${ts}.log"
  : > mexc_bot.log
  echo "Backup created: logs/archive/mexc_bot_${ts}.log"
else
  touch mexc_bot.log
  echo "Created new mexc_bot.log"
fi

echo "Starting mexc-bot..."
sudo systemctl start mexc-bot

echo "Done. Following logs..."
sudo journalctl -u mexc-bot -f
