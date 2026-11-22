# 4H Strong CHoCH research

Use Python 3.10 or newer. The detector, local CSV commands, evaluation, and SVG
renderer use the standard library. Open the SVG files in a browser.

## Causal market structure

- A source candle timestamp is its open time. A pivot timestamp is its origin
  candle open. Confirmation and break timestamps are the relevant candle close.
  A newly confirmed swing is recorded immediately and eligible from the next
  closed candle onward. UTC timestamps without offsets are interpreted as UTC.
- Pivots need strictly higher highs or lower lows than both neighboring windows.
  Equal extrema fail the relevant test. Outside bars that qualify as both are
  skipped. Confirmed local pivots are exported even when filtered out externally.
- ATR uses Wilder smoothing, seeded with the mean of the first 14 true ranges.
  The first true range is high minus low. Significance uses ATR through the
  confirmation candle. The first confirmed pivot with positive ATR seeds the
  significant swing series. Subsequent swings must move in the pivot direction
  by at least 1.5 ATR from the previous opposite significant pivot. Same-kind pivots
  can replace the active endpoint only when strictly more extreme and still
  significant relative to that same opposite anchor, if one exists. Equal
  same-kind levels keep the earlier endpoint. There is no extra near-equality
  tolerance. Significance makes a swing eligible for structural selection.
  An external role is recorded only when the swing becomes a continuation
  boundary or protection.
- Four alternating significant swings with strictly higher highs and lows, or
  lower highs and lows, plus a close crossing the latest directional level
  establish the initial trend and seed its continuation boundary. Mixed or equal
  comparisons remain unknown. In a bullish trend the continuation boundary
  is the external high. In a bearish trend it is the external low.
- A later confirmed significant pivot can move the boundary only outward
  and only at or beyond the most recent qualifying break close. A lower
  bullish high or a higher bearish low leaves the boundary in place.
  A BOS requires a strict close through the armed boundary from the other
  side or equality. It consumes that boundary, which remains a reference
  until an outward pivot arms the next one. Each pivot can cause one
  structural break.
- At establishment or BOS, the latest active opposite significant pivot
  can become the protected side of the external structure if its origin
  follows the breached pivot and it was already confirmed before the break
  candle. A prior structural break or any close through that origin since
  confirmation disqualifies it. A newer qualified origin updates protection.
  Otherwise existing protection persists, if any. A later confirmation
  alone cannot backfill a level on an earlier breakout.
- The state can be unknown, bullish or bearish with protection, or bullish or
  bearish without protection. An unprotected state can be superseded by a
  confirmed opposite four-swing trend setup when all four pivots originated
  after the current trend establishment and the close breaks its directional
  level. That event establishes a new trend and starts a new continuation count.
  CHoCH requires a protected level.
- A close crossing an established protected level takes precedence over BOS
  and emits one candidate. It clears protection and resets the trend and
  continuation count. A new establishment requires a target pivot originating
  after that CHoCH candle. Earlier confirmed swing context stays in history.
  An armed continuation break has priority over an opposite unprotected trend
  setup. New pivots are applied after break decisions for the current candle.
- Strong candidates require a directional candle body of at least 0.8 ATR,
  a close at least 0.2 ATR beyond the level, body/range of at least 0.6,
  directional closing position of at least 0.7, positive ATR, and at least
  one continuation BOS. Break quality uses ATR through the break close.
  All failed conditions are exported. Weak candidates still change the
  structure state because the protected level was broken.

## Internal structure

The internal and external detectors consume the same completed 4H candles.
Each owns its trend, continuation boundary, protection, and break history.
Both use the same 3-left/3-right pivot confirmation and Wilder ATR. Internal
swings require a positive move from the previous opposite pivot after ATR
warm-up, with no minimum ATR multiple. Consecutive same-kind pivots retain
the more extreme endpoint. Trend establishment still requires four alternating
swings with directional progression and a close across the trend-side level.

After establishment, each newer accepted pivot on the continuation side can
replace the internal boundary at a different price, including a move inward.
Strict close crossings consume each boundary at most once. Internal
protection is the confirmed opposite pullback after the broken pivot, assigned
only when a local leg qualifies at establishment or internal BOS. An invalid
or unconfirmed pullback leaves existing protection intact or leaves the
state unprotected. An internal CHoCH requires a close through protection
and resets only the internal trend. Its classification has no Strong flag.
An initial or opposite trend establishment is recorded separately from BOS
and CHoCH.

`internal_swings.csv`, `internal_states.csv`, and `internal_events.csv`
record the local layer. The existing external event columns are retained.
Both event streams carry scope, classification, ATR, and protection evidence.
Charts show only internal levels associated with a plotted break to limit
overlap. Squares mark internal BOS and triangles mark internal CHoCH.

`internal_external_relationships.csv` measures whether a later external CHoCH
occurs within 3, 6, 12, or 24 bars after an internal BOS or CHoCH. Both
any-direction and same-direction matches are recorded. Same-candle events
are excluded from lead counts. A blank result means the horizon was incomplete
and no match was observed. An observed match still counts if the sample ends
before the full horizon. These relationships are evaluated after replay.

## Structural break discovery

Discovery runs on the same completed bars and independently confirms pivots
with the shared 3-left/3-right algorithm. It activates each swing at the
confirmation close, after processing breaks for that candle. The next close
is the first eligible break. A strict close beyond the level emits one swing
event per pivot identity. A wick or a close exactly at the level counts
toward a touch episode, provided the candle trades through that price.
Consecutive touching candles are counted once. Unbroken levels remain active.

Swing movement is the absolute difference from the latest previously
confirmed opposite pivot divided by ATR at confirmation. The event records
age since activation, prior touch episodes, distance from the preceding close,
break distance relative to ATR, and the level's position relative to the
external continuation boundary carried into the break candle. Both CHoCH
states are recorded as of the prior close. Where no boundary or positive ATR
is available the corresponding diagnostic is empty or unavailable. None of
these measurements gates a swing break.

A range starts when the last four confirmed pivots alternate, giving two
highs and two lows. The outermost high and low define the box. Both tests
on each side must be within one quarter of the box width of one another,
and all closes from the first pivot's candle through activation must be
inside the box. The quarter-width rule is a fixed geometric definition,
with no historical calibration.

Only one box may be active. Its boundaries stay fixed, and new overlapping
formations do not replace it. The box stays eligible for the same duration
it took to form, including its final eligible close. A strict outside close
consumes it. Otherwise it expires. A later pivot confirmation can seed a
new box, including on the candle that ends the previous box. An underlying
swing break and a range break may both describe the same candle. Discovery
labels neither event as CHoCH.

`discovered_levels.csv` and `discovered_ranges.csv` record the activation
and first-break timestamps. `discovered_breaks.csv` records event evidence,
including range boundaries where applicable. Use
`python -m research.strong_choch.plot --input <run> --discovery both` to
generate the full overlay and a simplified chart. Green rings group swing
breaks by candle and direction, with all prices in the marker tooltip.
Orange diamonds mark range breaks. The simplified chart keeps levels used
in external structure events and the discovery markers.

## Grouped breaks and metric ranking

Phase 2.4 reads the discovery exports after replay. One group represents one
symbol, 4H close, and direction. Confirmed swing identities use the symbol,
direction, and original pivot timestamp. A same-direction range break is
attached to the group, or starts a range-only group when there is no swing
row. Shared swing and range evidence contributes once to age and touch
metrics. Touches are summed across distinct levels, so a single candle can
contribute to more than one level's count.

The grouping exports the largest swing in ATR at pivot confirmation, the
largest close displacement in ATR on the break candle, the oldest and newest
level ages, prior touches, swing count, the number of levels already assigned
an external role by the close, and the presence of a range break. The CSV
records the prior states, the states as of the group close, and same-candle
CHoCH matches in the group's direction. Detailed JSON retains each raw row
and the same-candle CHoCH evidence.

The separate rank table orders each metric independently, descending.
Ties share the highest applicable rank and missing measurements are
unranked. These ranks describe the whole evaluation cohort retrospectively.
The grouped evidence itself is causal. Forward returns and later CHoCH
relationships are calculated separately. Same-candle matches are excluded
from the 3, 6, 12, and 24 candle lead counts.

Create a new output root and replay each symbol into its own subdirectory
using the commands below, omitting the chart step until after grouping.
Then run the grouping and chart commands from the repository root.

```powershell
$run = "strong_choch_results/my_grouped_run"
python -m research.strong_choch.group --input $run --symbol ADA_USDT --symbol BNB_USDT --symbol ETH_USDT --symbol SOL_USDT --audit "ETH_USDT=2026-08-19 16:00"
foreach ($symbol in "ADA_USDT", "BNB_USDT", "ETH_USDT", "SOL_USDT") {
    python -m research.strong_choch.plot --input "$run/$symbol" --grouped "$run/grouped_events.csv"
}
```

Repeat `--audit` to compare additional UTC candle closes. The simplified
chart displays grouped breaks, attached range breaks, major external levels,
and external Strong CHoCH. The debug chart also displays raw broken prices
and the internal and external annotations.

## Untouched validation

Phase 2.5 freezes the detector and metric file hashes, parameters, source
checksums, and lower and upper development tertiles from the 204 Phase 2.4
groups. Continuous bands are low at or below the lower cut, high above the
upper cut, and middle in between. A tie can leave a band empty. External
association and range confirmation use with/without groups.

The four local sources end at the November 1, 2026 00:00 UTC close. They
contain no untouched target-symbol coverage. A November 1–22 pilot requires
126 new 4H candles per symbol. Its 60 warm-up candles begin October 22 at
00:00 UTC and are already available. The pilot is short and is suitable for
checking coverage and workflow. Prefer a longer untouched period for inference.

Obtain local exports separately and combine any warm-up and holdout segments
into a single contiguous, chronological OHLCV CSV per symbol. The four paths
under `validation_inputs/raw` below are expected inputs to supply. The
preparation command validates and aggregates 15m candles without downloading.
If the inputs are already 4H or 1H, change `--source-timeframe` accordingly.

```powershell
$root = "strong_choch_results/phase_2_5"
$inputRoot = "validation_inputs"
New-Item -ItemType Directory -Force "$inputRoot/prepared" | Out-Null
foreach ($symbol in "ADA_USDT", "BNB_USDT", "ETH_USDT", "SOL_USDT") {
    python -m research.strong_choch.data --symbol $symbol --input "$inputRoot/raw/${symbol}_15m.csv" --source-timeframe 15m --as-of 2026-11-22 --output "$inputRoot/prepared/${symbol}_4h.csv"
    if ($LASTEXITCODE) { throw "Preparation failed for $symbol" }
}
python -m research.strong_choch.validation evaluate --freeze "$root/algorithm_fingerprint.json" --source "ADA_USDT=$inputRoot/prepared/ADA_USDT_4h.csv" --source "BNB_USDT=$inputRoot/prepared/BNB_USDT_4h.csv" --source "ETH_USDT=$inputRoot/prepared/ETH_USDT_4h.csv" --source "SOL_USDT=$inputRoot/prepared/SOL_USDT_4h.csv" --start 2026-11-01 --end 2026-11-22 --provenance "Describe the export source and how the evaluation dates were kept untouched" --output "$root/november_pilot"
```

The evaluator enforces coverage, frozen source code, and no overlap with
consulted candles. Exactly 60 preceding candles are used for warm-up and
are excluded from the evaluation. Forward windows stop at the evaluation
end. Metrics and comparisons are reported pooled and by symbol, direction,
and their combination. Baseline candles are sampled without replacement
within each symbol and direction with a frozen seed and the same sample
count as grouped events. They use the same 4H closes and forward outcomes.

Uncertainty intervals describe the mean directional return using 500
resamples of UTC calendar-week blocks, pooled across symbols for each
cohort. Fewer than six nonempty weeks produce no interval. Weekly blocks
preserve within-week dependence and same-week cross-symbol clustering.
Forward windows near a week boundary can still overlap across blocks.

## Local historical run

The existing local 15m exports for SOL, BNB, ETH, and ADA have continuous
coverage from 2026-07-24 00:00 UTC through the 2026-11-01 00:00 UTC close.
Each file produces 600 4H candles. The example uses the first 60 for warm-up
and the remaining 540 for evaluation. XRP was unavailable. These are short,
single-period local datasets with no independent exchange reconciliation.

Run from the repository root. Choose a new output directory for each rerun.

```powershell
foreach ($coin in 'SOL', 'BNB', 'ETH', 'ADA') {
    $symbol = "${coin}_USDT"
    $run = "strong_choch_results/my_run/$symbol"
    python -m research.strong_choch.replay --symbol $symbol --timeframe 4h --source-timeframe 15m --input "backtest_results/${symbol}_15m.csv" --start 2026-08-03 --end 2026-11-01 --as-of 2026-11-01 --output $run
    if ($LASTEXITCODE) { break }
    python -m research.strong_choch.evaluate --input $run
    if ($LASTEXITCODE) { break }
    python -m research.strong_choch.plot --input $run
    if ($LASTEXITCODE) { break }
}
```

For a separate local data-preparation step, use
`python -m research.strong_choch.data --help`. It accepts complete 15m, 1h,
or 4h CSVs, writes a new 4H CSV, and performs no downloading. Aggregation
requires 16, four, or one consecutive source candles, respectively.
Incomplete tails fail by default. To discard only a trailing partial bar or
group, pass `--drop-incomplete-final`. Interior gaps always fail.

`forward_evaluation.csv` reports direction-adjusted returns from the break
close to the close 3, 6, 12, or 24 candles later. Maximum favorable and
adverse excursions are positive magnitudes from the break close to the most
extreme subsequent prices in each window, floored at zero. Incomplete windows
are counted and excluded from the corresponding means. The event candle's
intrabar range is excluded. No trades are simulated.

The overview chart includes warm-up candles. Focus charts use up to 45
candles before and 24 after the event. A dotted line connects a pivot origin
to the candle in which it became observable. A dashed connector shows a later
external activation where applicable. The origin marker is hollow.
`structure_states.csv` records the state, external high and low, and whether
the continuation boundary is armed at each candle close. Confirmed swings
include their first external activation timestamp when they take an external role.
