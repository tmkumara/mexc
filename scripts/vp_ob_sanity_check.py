"""
Offline sanity check: fetch recent candles for a few known-liquid pairs and
print the computed Volume Profile levels and detected Order Blocks, so the
math can be eyeballed against a chart before trusting live signals.

Run: python scripts/vp_ob_sanity_check.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mexc_client import get_klines
from order_blocks import detect_bos_choch, find_order_blocks, find_swings
from strategy import _atr_series
from volume_profile import compute_volume_profile, vp_bias
from config import (
    ATR_PERIOD,
    OB_DISPLACEMENT_ATR_MULT,
    OB_KLINE_COUNT,
    OB_SWING_LENGTH,
    OB_TF,
    VP_BINS,
    VP_HVN_MULT,
    VP_KLINE_COUNT,
    VP_LOOKBACK_BARS,
    VP_LVN_MULT,
    VP_TF,
    VP_VALUE_AREA_PCT,
)

SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]


def check_symbol(symbol: str) -> None:
    print(f"\n=== {symbol} ===")

    vp_df = get_klines(symbol, VP_TF, count=VP_KLINE_COUNT)
    if vp_df is None or vp_df.empty or len(vp_df) < VP_LOOKBACK_BARS + 1:
        print("  VP: insufficient candles")
        return
    vp_window = vp_df.iloc[:-1].tail(VP_LOOKBACK_BARS)
    vp = compute_volume_profile(
        vp_window, bins=VP_BINS, value_area_pct=VP_VALUE_AREA_PCT,
        hvn_mult=VP_HVN_MULT, lvn_mult=VP_LVN_MULT,
    )
    if vp is None:
        print("  VP: degenerate window")
        return
    close = float(vp_window["close"].iloc[-1])
    bias = vp_bias(close, vp)
    print(f"  VP({VP_TF}): POC={vp.poc:.6g} VAH={vp.vah:.6g} VAL={vp.val:.6g} "
          f"close={close:.6g} bias={bias}")
    print(f"  HVNs: {[round(h, 4) for h in vp.hvns]}")
    print(f"  LVNs: {[round(l, 4) for l in vp.lvns]}")

    ob_df = get_klines(symbol, OB_TF, count=OB_KLINE_COUNT)
    min_ob_bars = ATR_PERIOD + OB_SWING_LENGTH * 2 + 10
    if ob_df is None or ob_df.empty or len(ob_df) < min_ob_bars:
        print("  OB: insufficient candles")
        return
    ob_window = ob_df.iloc[:-1].reset_index(drop=True)
    atr = _atr_series(ob_window, ATR_PERIOD)
    swings = find_swings(ob_window, length=OB_SWING_LENGTH)
    events = detect_bos_choch(ob_window, swings)
    obs = find_order_blocks(ob_window, events, atr, displacement_atr_mult=OB_DISPLACEMENT_ATR_MULT)

    print(f"  OB({OB_TF}): {len(swings)} swings, {len(events)} structure events, {len(obs)} order blocks")
    for ob in obs[-5:]:
        print(f"    {ob.direction} [{ob.low:.6g}, {ob.high:.6g}] "
              f"formed_at_bar={ob.formed_at_bar} event={ob.structure_event}")


if __name__ == "__main__":
    for sym in SYMBOLS:
        check_symbol(sym)
