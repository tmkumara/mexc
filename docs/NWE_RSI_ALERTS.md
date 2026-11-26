# 1H / 4H Nadaraya–Watson Envelope + RSI public alerts

The public Telegram scanner uses completed 1H and 4H MEXC candles for the 40 contracts listed in nwe_rsi_public_alerts.py. NWE uses the trailing non-repainting Gaussian endpoint method with bandwidth 8, multiplier 3, 500 trailing closes and 499 residuals. The strategy reads raw Wilder RSI(14). A long needs a lower-band excursion with RSI at or below 30 and subsequent close inside. A short needs an upper-band excursion with RSI at or above 70 and subsequent close inside.

The alert uses the opening price of the 15-minute candle 15 minutes after the signal as an entry reference. The scanner runs hourly at :18 UTC. It checks 4H first at shared closes and sends at most one alert per coin and close. Both timeframes share the daily cap. The target reference models +30% net ROI on initial margin at 20x using 6 bps fees and 2 bps slippage per side before funding. The scanner sends alerts only and does not track fills, auto-close positions, place orders or model liquidation. No stop or time exit is prescribed. Legacy signal reports and private CHoCH review continue separately. The existing Telegram pause, dry-run and daily-cap controls apply.

The production workflow restarts the bot and dashboard after a push to main. Roll back by reverting the release commit and pushing the revert through the same workflow. Keep the server configuration backup protected.

## Envelope attribution

Nadaraya–Watson Envelope [LuxAlgo], © LuxAlgo, licensed CC BY-NC-SA 4.0 (https://creativecommons.org/licenses/by-nc-sa/4.0/). The Python adaptation in nwe_rsi_local.py implements the supplied endpoint calculation, omits the repainting chart mode, and adds local 4H Wilder RSI and envelope re-entry rules. The adapted module is distributed under CC BY-NC-SA 4.0. This release is for the channel's confirmed noncommercial use.

## Manual order watch

New channel alerts include **Mark order placed**. Telegram channel admins can press it to start monitoring the alert entry using MEXC's public last price. The button changes to **Stop watching**. Marking confirms the operator's intent and does not verify an exchange fill. Alerts already posted before this release do not acquire buttons.

The watcher checks once a minute. At 20x, a 1% favorable move from the alert reference triggers a reminder to move the stop to the actual entry. The reminder replies to the original alert and the persisted watch ends after successful delivery. A trigger is retained if the Telegram send fails so the watcher can retry. The bot does not authenticate with the trading API, place or edit an order, or know whether the position is still open. Short-lived threshold touches between polls can be missed.
