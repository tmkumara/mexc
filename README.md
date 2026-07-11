> **Current strategy (v14, `feature/liq-scalp-v14`): Liquidation-Aware 1m Scalp.**
> EMA(9/21/50) + rolling VWAP + RSI + volume base signal on 1m candles,
> gated by a free open-interest-derived liquidation-cluster estimator
> (`liq_estimator.py`). See `CLAUDE.md` for the full architecture and
> `docs/superpowers/plans/2026-07-11-liquidation-aware-scalp-v14.md` for
> the implementation plan. The write-up below is retained for historical
> reference only and does not describe the currently running strategy.

---

Below is the **mapped and improved strategy explanation** based on the Daily Price Action article.

The article’s main idea is:

> Higher timeframe gives direction. Lower timeframe gives entry.
> Do not trade every liquidity sweep. Trade only when context, sweep, and confirmation align. ([Daily Price Action][1])

---

## Mapped Strategy: Crypto Liquidity Sweep Retest Scalping

```text
Strategy Name:
Crypto Liquidity Sweep Retest Scalping Strategy

Inspired By:
Daily Price Action liquidity sweep reversal model.

Main Concept:
The strategy should not generate signals only because price sweeps a high or low.
A valid signal needs market structure, higher-timeframe context, a liquidity sweep, acceptance/confirmation, and preferably a retest entry.

The article explains that a valid setup starts with higher-timeframe context, usually the 1H chart, then entry is refined on the 15M chart. The higher timeframe gives direction, while the lower timeframe gives the entry.
```

---

# 1. Timeframe Mapping

## Article version

The article uses:

```text
1H chart = market direction and structure
15M chart = liquidity sweep setup and entry
```

It says the 15-minute chart is preferred because it reduces noise while still giving precise entries, although 5-minute and 1-minute can also work with proper higher-timeframe context. ([Daily Price Action][1])

## Your crypto scalping version

Use:

```text
1H timeframe = trend direction filter
15M timeframe = main setup detection
5M timeframe = optional refined entry
```

Recommended structure:

```text
Use 1H for bias.
Use 15M to detect liquidity sweep and confirmation.
Use 5M only for tighter entry if needed.
```

---

# 2. Market Structure Rule

Your idea:

```text
Long = 2 consecutive Higher Highs
Short = 2 consecutive Lower Lows
```

This can be mapped like this:

## Bullish Bias for LONG

```text
On the 1H or 15M chart, detect bullish structure.

Bullish structure is valid when:
- Price creates HH1 above the previous swing high.
- Then price creates HH2 above HH1.
- This confirms 2 consecutive Higher Highs.
```

Meaning:

```text
The market is showing upward intent.
We should only look for long setups after price sweeps sell-side liquidity below a recent low.
```

## Bearish Bias for SHORT

```text
On the 1H or 15M chart, detect bearish structure.

Bearish structure is valid when:
- Price creates LL1 below the previous swing low.
- Then price creates LL2 below LL1.
- This confirms 2 consecutive Lower Lows.
```

Meaning:

```text
The market is showing downward intent.
We should only look for short setups after price sweeps buy-side liquidity above a recent high.
```

---

# 3. OTE / Premium-Discount Filter

The article adds one important filter: **OTE zone**.

OTE means **Optimal Trade Entry**. In the article, this is the zone between the **62% and 79% Fibonacci retracement** from the recent external high to external low, or vice versa. The author says he is not interested in a reversal setup if price is not inside OTE. ([Daily Price Action][1])

For your bot, we can make this optional but useful.

## For SHORT setup

```text
If higher-timeframe trend is bearish:
- Find recent external high and external low.
- Draw Fibonacci from external high to external low.
- Mark 62% to 79% retracement as bearish OTE zone.
- Only allow short setup if price sweeps liquidity inside or near this OTE zone.
```

## For LONG setup

```text
If higher-timeframe trend is bullish:
- Find recent external low and external high.
- Draw Fibonacci from external low to external high.
- Mark 62% to 79% retracement as bullish OTE zone.
- Only allow long setup if price sweeps liquidity inside or near this OTE zone.
```

For coding, you can make this config-based:

```text
useOTEFilter = true / false
oteMin = 0.62
oteMax = 0.79
```

---

# 4. Liquidity Sweep Definition

The article explains that a liquidity sweep happens when price moves beyond a key high or low to take stops/resting orders, then reverses back. It also says a valid sweep needs acceptance back inside the range, not just a random wick. ([Daily Price Action][1])

## Long Liquidity Sweep

For a long setup:

```text
Price must sweep below a previous swing low.
```

Valid long sweep condition:

```text
sweepCandle.low < previousSwingLow
AND
sweepCandle.close > previousSwingLow
```

Meaning:

```text
Price broke below the old low.
Sellers thought breakdown was happening.
Stops below the low were taken.
But candle closed back above the low.
This shows sell-side liquidity was swept.
```

## Short Liquidity Sweep

For a short setup:

```text
Price must sweep above a previous swing high.
```

Valid short sweep condition:

```text
sweepCandle.high > previousSwingHigh
AND
sweepCandle.close < previousSwingHigh
```

Meaning:

```text
Price broke above the old high.
Buyers thought breakout was happening.
Stops above the high were taken.
But candle closed back below the high.
This shows buy-side liquidity was swept.
```

---

# 5. Acceptance / Confirmation Rule

This is a very important article point.

The article does **not** enter immediately on the sweep. It waits for **acceptance**. For a short, acceptance means a candle closes below the low that triggered the sweep. ([Daily Price Action][1])

We can map this to both sides.

## For SHORT

After price sweeps above a swing high:

```text
triggeringLow = low of the candle before or during the sweep structure that caused the move upward.
```

Simple code-friendly version:

```text
triggeringLow = low of the sweep candle
OR
triggeringLow = nearest minor swing low before the sweep
```

Confirmation:

```text
confirmationCandle.close < triggeringLow
```

Meaning:

```text
Price swept buy-side liquidity.
Then price accepted back below the triggering low.
This confirms bearish reversal intent.
```

## For LONG

After price sweeps below a swing low:

```text
triggeringHigh = high of the candle before or during the sweep structure that caused the move downward.
```

Simple code-friendly version:

```text
triggeringHigh = high of the sweep candle
OR
triggeringHigh = nearest minor swing high before the sweep
```

Confirmation:

```text
confirmationCandle.close > triggeringHigh
```

Meaning:

```text
Price swept sell-side liquidity.
Then price accepted back above the triggering high.
This confirms bullish reversal intent.
```

---

# 6. Your Retest Entry Rule

Your idea adds a good scalping refinement:

```text
After liquidity sweep, price comes again to that liquidity sweep zone.
Then signal.
```

The article says entry can happen on the confirmation candle close or after a small retrace. It also says waiting for a retest makes sense if there are untested imbalances/FVGs during confirmation. ([Daily Price Action][1])

So your version becomes:

```text
Do not enter immediately after sweep.
Do not enter immediately after confirmation if retest mode is enabled.
Wait for price to return to the liquidity sweep zone or confirmation imbalance zone.
Then generate signal.
```

## Long Retest Zone

```text
longSweepZoneLow = sweepCandle.low
longSweepZoneHigh = sweptSwingLow
```

Price retests the zone when:

```text
currentCandle.low <= longSweepZoneHigh
AND
currentCandle.high >= longSweepZoneLow
```

Then generate LONG only if:

```text
price touches zone
AND bullish confirmation appears
```

Bullish confirmation examples:

```text
currentCandle.close > currentCandle.open
OR
currentCandle.close > sweptSwingLow
OR
currentCandle.close > previousCandle.high
```

## Short Retest Zone

```text
shortSweepZoneLow = sweptSwingHigh
shortSweepZoneHigh = sweepCandle.high
```

Price retests the zone when:

```text
currentCandle.high >= shortSweepZoneLow
AND
currentCandle.low <= shortSweepZoneHigh
```

Then generate SHORT only if:

```text
price touches zone
AND bearish confirmation appears
```

Bearish confirmation examples:

```text
currentCandle.close < currentCandle.open
OR
currentCandle.close < sweptSwingHigh
OR
currentCandle.close < previousCandle.low
```

---

# 7. Complete Long Logic

```text
LONG SETUP:

1. Detect bullish higher-timeframe context.
   - Use 1H trend.
   - Confirm 2 consecutive Higher Highs.
   - Optional: also require higher lows.

2. Wait for price to pull back into discount/OTE zone.
   - Optional filter.
   - Bullish OTE zone = 62% to 79% retracement of the recent bullish impulse.

3. Detect sell-side liquidity sweep.
   - Find recent swing low.
   - Sweep candle low breaks below that swing low.
   - Sweep candle closes back above that swing low.

4. Mark liquidity sweep zone.
   - Zone low = sweep candle wick low.
   - Zone high = swept swing low.

5. Wait for bullish acceptance.
   - Price must close above the triggering high.
   - Triggering high can be the sweep candle high or nearest minor swing high.

6. Wait for retest.
   - Price returns to the liquidity sweep zone.
   - Do not generate duplicate signals from the same zone.

7. Generate LONG signal when:
   - Bullish structure exists.
   - Sell-side liquidity sweep exists.
   - Acceptance above triggering high exists.
   - Price retests sweep zone.
   - Bullish confirmation candle appears.

8. Risk management:
   - Stop loss below sweep wick low plus buffer.
   - Take profit at recent high, next liquidity pool, or minimum 2R.
```

---

# 8. Complete Short Logic

```text
SHORT SETUP:

1. Detect bearish higher-timeframe context.
   - Use 1H trend.
   - Confirm 2 consecutive Lower Lows.
   - Optional: also require lower highs.

2. Wait for price to pull back into premium/OTE zone.
   - Optional filter.
   - Bearish OTE zone = 62% to 79% retracement of the recent bearish impulse.

3. Detect buy-side liquidity sweep.
   - Find recent swing high.
   - Sweep candle high breaks above that swing high.
   - Sweep candle closes back below that swing high.

4. Mark liquidity sweep zone.
   - Zone low = swept swing high.
   - Zone high = sweep candle wick high.

5. Wait for bearish acceptance.
   - Price must close below the triggering low.
   - Triggering low can be the sweep candle low or nearest minor swing low.

6. Wait for retest.
   - Price returns to the liquidity sweep zone.
   - Do not generate duplicate signals from the same zone.

7. Generate SHORT signal when:
   - Bearish structure exists.
   - Buy-side liquidity sweep exists.
   - Acceptance below triggering low exists.
   - Price retests sweep zone.
   - Bearish confirmation candle appears.

8. Risk management:
   - Stop loss above sweep wick high plus buffer.
   - Take profit at recent low, next liquidity pool, or minimum 2R.
```

---

# 9. Important Difference Between Your Idea and Article

Your original idea:

```text
2 HH → liquidity sweep → price returns to sweep zone → long
2 LL → liquidity sweep → price returns to sweep zone → short
```

Improved with article mapping:

```text
Higher-timeframe trend/context
→ price reaches OTE/premium-discount zone
→ liquidity builds above/below swing level
→ price sweeps that level
→ candle closes back inside
→ acceptance confirms reversal
→ optional retest entry
→ signal
```

So the final strategy is stronger because it avoids random sweep signals.

---

# 10. Claude Code Strategy Explanation

You can paste this directly to Claude Code:

```text
Implement a crypto scalping signal strategy based on liquidity sweep reversal logic.

Use multi-timeframe analysis:
- 1H timeframe for higher-timeframe market structure and directional bias.
- 15M timeframe for liquidity sweep detection and signal generation.
- Optional 5M timeframe for refined entry confirmation.

The strategy should follow the Daily Price Action style liquidity sweep reversal model:
1. Determine higher-timeframe structure.
2. Wait for price to reach a meaningful retracement area such as OTE.
3. Detect a liquidity sweep of a previous swing high or swing low.
4. Wait for acceptance back inside the range.
5. Optionally wait for price to retest the sweep zone.
6. Generate a long or short signal only when all conditions align.

LONG logic:
- Confirm bullish structure using 2 consecutive Higher Highs on the selected structure timeframe.
- Optional: also require higher lows.
- Optional OTE filter: price should pull back into the 62% to 79% retracement zone of the latest bullish impulse.
- Detect a sell-side liquidity sweep:
  - Find a recent valid swing low.
  - A sweep candle is valid when its low breaks below the swing low and its close returns back above the swing low.
- Create the long sweep zone:
  - zoneLow = sweep candle low.
  - zoneHigh = swept swing low.
- Wait for bullish acceptance:
  - Price must close above the triggering high.
  - triggeringHigh can be the sweep candle high or nearest minor swing high before the sweep.
- Retest entry mode:
  - Do not signal immediately after the sweep.
  - Wait for price to come back into the long sweep zone.
  - When price touches the zone and closes bullishly, generate a LONG signal.
- Stop loss:
  - Below the sweep candle low plus configurable buffer.
- Take profit:
  - Recent swing high, next buy-side liquidity pool, or minimum 2R.

SHORT logic:
- Confirm bearish structure using 2 consecutive Lower Lows on the selected structure timeframe.
- Optional: also require lower highs.
- Optional OTE filter: price should pull back into the 62% to 79% retracement zone of the latest bearish impulse.
- Detect a buy-side liquidity sweep:
  - Find a recent valid swing high.
  - A sweep candle is valid when its high breaks above the swing high and its close returns back below the swing high.
- Create the short sweep zone:
  - zoneLow = swept swing high.
  - zoneHigh = sweep candle high.
- Wait for bearish acceptance:
  - Price must close below the triggering low.
  - triggeringLow can be the sweep candle low or nearest minor swing low before the sweep.
- Retest entry mode:
  - Do not signal immediately after the sweep.
  - Wait for price to come back into the short sweep zone.
  - When price touches the zone and closes bearishly, generate a SHORT signal.
- Stop loss:
  - Above the sweep candle high plus configurable buffer.
- Take profit:
  - Recent swing low, next sell-side liquidity pool, or minimum 2R.

Important rules:
- Never signal only because a candle swept liquidity.
- A sweep without acceptance is not valid.
- A sweep without higher-timeframe context is low quality.
- Avoid sideways markets.
- Avoid duplicate signals from the same sweep zone.
- Each sweep zone should have a status:
  - CREATED
  - ACCEPTED
  - RETESTED
  - SIGNAL_GENERATED
  - INVALIDATED
- Invalidate a long zone if price closes below the sweep wick low.
- Invalidate a short zone if price closes above the sweep wick high.
- Add detailed logs for every decision.

Required logs:
- Higher timeframe structure detected.
- Bullish/bearish bias confirmed.
- OTE zone calculated.
- Price entered OTE zone.
- Swing high/swing low detected.
- Liquidity sweep detected.
- Sweep zone created.
- Acceptance confirmed.
- Retest detected.
- Signal generated.
- Zone invalidated.
- Duplicate signal skipped.
```

---

# 11. Cleaner Bot State Flow

For coding, the best way is to treat each setup like a state machine:

```text
WAITING_FOR_STRUCTURE
↓
WAITING_FOR_OTE
↓
WAITING_FOR_SWEEP
↓
WAITING_FOR_ACCEPTANCE
↓
WAITING_FOR_RETEST
↓
SIGNAL_GENERATED
```

For invalidation:

```text
If long setup and price closes below sweep wick low:
    setup = INVALIDATED

If short setup and price closes above sweep wick high:
    setup = INVALIDATED
```

This will make your bot cleaner than just checking everything randomly candle by candle.

---

# 12. Simple Example

## Long example

```text
BTC 1H creates two Higher Highs.
The bot marks bullish bias.

BTC pulls back into the 62%-79% retracement zone.

On 15M, price drops below a recent swing low at 64,000.
The wick goes to 63,850.
But the candle closes back above 64,000.

This is a sell-side liquidity sweep.

The bot creates a long zone:
63,850 - 64,000

Later price closes above the triggering high.
Acceptance is confirmed.

Then price comes back to 63,850 - 64,000.
A bullish candle forms from that zone.

The bot logs:
LONG SIGNAL GENERATED
Entry: retest candle close
SL: below 63,850
TP: recent high or 2R
```

## Short example

```text
BTC 1H creates two Lower Lows.
The bot marks bearish bias.

BTC pulls back into the 62%-79% retracement zone.

On 15M, price rises above a recent swing high at 67,000.
The wick goes to 67,180.
But the candle closes back below 67,000.

This is a buy-side liquidity sweep.

The bot creates a short zone:
67,000 - 67,180

Later price closes below the triggering low.
Acceptance is confirmed.

Then price comes back to 67,000 - 67,180.
A bearish candle forms from that zone.

The bot logs:
SHORT SIGNAL GENERATED
Entry: retest candle close
SL: above 67,180
TP: recent low or 2R
```

This is still a trading strategy idea, not a guaranteed profitable system. For crypto futures, test it in dry-run/backtest first, especially because low-timeframe sweeps can fail during high volatility and news.

[1]: https://dailypriceaction.com/blog/liquidity-sweep-reversals/ "How To Trade Liquidity Sweep Reversals (15-Minute Strategy)"
