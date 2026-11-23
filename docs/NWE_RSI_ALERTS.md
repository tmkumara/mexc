# 4H Nadaraya–Watson Envelope + RSI public alerts

The public Telegram scanner uses completed 4H MEXC candles for the 40 contracts listed in nwe_rsi_public_alerts.py. NWE uses the trailing non-repainting Gaussian endpoint method with bandwidth 8, multiplier 3, 500 trailing closes and 499 residuals. The strategy reads raw Wilder RSI(14). A long needs a lower-band excursion with RSI at or below 30 and subsequent close inside. A short needs an upper-band excursion with RSI at or above 70 and subsequent close inside.

The alert uses the opening price of the 15-minute candle 15 minutes after the signal as an entry reference. The target reference models +30% net ROI on initial margin at 20x using 6 bps fees and 2 bps slippage per side before funding. The scanner sends alerts only and does not track fills, auto-close positions, place orders or model liquidation. No stop or time exit is prescribed. Legacy signal reports and private CHoCH review continue separately. The existing Telegram pause, dry-run and daily-cap controls apply.

The production workflow restarts the bot and dashboard after a push to main. Roll back by reverting the release commit and pushing the revert through the same workflow. Keep the server configuration backup protected.

## Envelope attribution

Nadaraya–Watson Envelope [LuxAlgo], © LuxAlgo, licensed CC BY-NC-SA 4.0 (https://creativecommons.org/licenses/by-nc-sa/4.0/). The Python adaptation in nwe_rsi_local.py implements the supplied endpoint calculation, omits the repainting chart mode, and adds local 4H Wilder RSI and envelope re-entry rules. The adapted module is distributed under CC BY-NC-SA 4.0. This release is for the channel's confirmed noncommercial use.
