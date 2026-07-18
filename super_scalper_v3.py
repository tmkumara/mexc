"""
super_scalper.py — v3: adds MARKET REGIME detection to kill fake signals.

Regime layer (3 independent votes):
  1. ADX (Wilder, 14)        : > adx_min  -> real trend strength
  2. Choppiness Index (14)   : < chop_max -> market is trending, not ranging
  3. Band expansion ratio    : channel width now vs its recent average
                               > expand_min -> volatility breakout, not grind

regime() returns: "TRENDING" | "RANGING" | "TRANSITION"
  - TRENDING  : >= 2 of 3 votes pass  -> trade signals allowed
  - RANGING   : 0 votes                -> block ALL entries
  - TRANSITION: 1 vote                 -> only allow flip signals with
                                          max strength (early trend catch)

Everything from v2 (SuperTrend + Keltner + AO + strength) unchanged.
"""

from dataclasses import dataclass
import numpy as np
import pandas as pd


@dataclass
class ScalperConfig:
    # SuperTrend
    atr_period: int = 10
    atr_mult: float = 2.5
    # Keltner Channel
    kc_ema: int = 20
    kc_atr_period: int = 14
    kc_mult: float = 2.0
    entry_zone: float = 0.45
    slope_lookback: int = 3
    # Awesome Oscillator
    ao_fast: int = 5
    ao_slow: int = 34
    # Strength
    strength_lookback: int = 5
    # --- Regime detection ---
    adx_period: int = 14
    adx_min: float = 22.0       # classic threshold: <20 chop, >25 trend
    chop_period: int = 14
    chop_max: float = 50.0      # CI > 61.8 = strong range, < 38.2 = strong trend
    expand_period: int = 20     # band width average lookback
    expand_min: float = 1.10    # width must be >= 110% of its recent avg


class SuperScalper:
    def __init__(self, **kwargs):
        self.cfg = ScalperConfig(**kwargs)

    # ------------------------------------------------------------------ #
    def _atr_series(self, df, period):
        hl = df["high"] - df["low"]
        hc = (df["high"] - df["close"].shift()).abs()
        lc = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
        return tr, tr.ewm(alpha=1 / period, adjust=False).mean()

    # ------------------------------------------------------------------ #
    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        c = self.cfg

        # ============ SuperTrend ========================================
        _, atr = self._atr_series(df, c.atr_period)
        hl2 = (df["high"] + df["low"]) / 2
        upper_basic = (hl2 + c.atr_mult * atr).to_numpy()
        lower_basic = (hl2 - c.atr_mult * atr).to_numpy()

        n = len(df)
        upper, lower = upper_basic.copy(), lower_basic.copy()
        close = df["close"].to_numpy()
        st = np.full(n, np.nan)
        trend = np.ones(n, dtype=int)

        for i in range(1, n):
            if close[i - 1] <= upper[i - 1]:
                upper[i] = min(upper_basic[i], upper[i - 1])
            if close[i - 1] >= lower[i - 1]:
                lower[i] = max(lower_basic[i], lower[i - 1])
            if trend[i - 1] == 1:
                trend[i] = -1 if close[i] < lower[i] else 1
            else:
                trend[i] = 1 if close[i] > upper[i] else -1
            st[i] = lower[i] if trend[i] == 1 else upper[i]

        df["supertrend"], df["trend"] = st, trend
        flip = df["trend"].diff()
        df["signal"] = None
        df.loc[flip == 2, "signal"] = "BUY"
        df.loc[flip == -2, "signal"] = "SELL"

        # ============ Keltner Channel ===================================
        df["kc_mid"] = df["close"].ewm(span=c.kc_ema, adjust=False).mean()
        _, kc_atr = self._atr_series(df, c.kc_atr_period)
        df["kc_upper"] = df["kc_mid"] + c.kc_mult * kc_atr
        df["kc_lower"] = df["kc_mid"] - c.kc_mult * kc_atr
        band_w = (df["kc_upper"] - df["kc_lower"]).replace(0, np.nan)
        df["kc_pos"] = ((df["close"] - df["kc_lower"]) / band_w).clip(0, 1)
        df["kc_slope"] = (df["kc_mid"] - df["kc_mid"].shift(c.slope_lookback)) / kc_atr

        # ============ Awesome Oscillator ================================
        median = (df["high"] + df["low"]) / 2
        df["ao"] = (median.rolling(c.ao_fast).mean()
                    - median.rolling(c.ao_slow).mean())
        df["ao_rising"] = df["ao"] >= df["ao"].shift()

        # ============ Strength ==========================================
        agree = (np.sign(df["close"].diff()) == df["trend"]).astype(int)
        df["strength"] = agree.rolling(c.strength_lookback).sum()

        # ============ REGIME: 1) ADX ====================================
        up_move = df["high"].diff()
        dn_move = -df["low"].diff()
        plus_dm = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)
        tr, atr_adx = self._atr_series(df, c.adx_period)
        alpha = 1 / c.adx_period
        plus_di = 100 * pd.Series(plus_dm, index=df.index)\
            .ewm(alpha=alpha, adjust=False).mean() / atr_adx
        minus_di = 100 * pd.Series(minus_dm, index=df.index)\
            .ewm(alpha=alpha, adjust=False).mean() / atr_adx
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        df["adx"] = dx.ewm(alpha=alpha, adjust=False).mean()
        df["plus_di"], df["minus_di"] = plus_di, minus_di

        # ============ REGIME: 2) Choppiness Index =======================
        p = c.chop_period
        tr_sum = tr.rolling(p).sum()
        hh = df["high"].rolling(p).max()
        ll = df["low"].rolling(p).min()
        rng = (hh - ll).replace(0, np.nan)
        df["chop"] = 100 * np.log10(tr_sum / rng) / np.log10(p)

        # ============ REGIME: 3) Band expansion =========================
        df["kc_width"] = band_w
        df["expansion"] = band_w / band_w.rolling(c.expand_period).mean()

        # ============ Regime vote =======================================
        votes = (
            (df["adx"] >= c.adx_min).astype(int)
            + (df["chop"] <= c.chop_max).astype(int)
            + (df["expansion"] >= c.expand_min).astype(int)
        )
        df["regime_votes"] = votes
        df["regime"] = np.select(
            [votes >= 2, votes == 1],
            ["TRENDING", "TRANSITION"],
            default="RANGING",
        )
        return df

    # ------------------------------------------------------------------ #
    def latest_signal(self, df: pd.DataFrame) -> dict:
        row = df.iloc[-1]
        f = lambda k, d=0.0: float(row[k]) if pd.notna(row[k]) else d
        return {
            "side": row["signal"],
            "trend": "BULLISH" if row["trend"] == 1 else "BEARISH",
            "strength": int(row["strength"]) if pd.notna(row["strength"]) else 0,
            "ao": f("ao"), "ao_rising": bool(row["ao_rising"]),
            "kc_pos": f("kc_pos", 0.5), "kc_slope": f("kc_slope"),
            "kc_mid": f("kc_mid"), "kc_upper": f("kc_upper"),
            "kc_lower": f("kc_lower"),
            "adx": f("adx"), "chop": f("chop", 50.0),
            "expansion": f("expansion", 1.0),
            "regime": row["regime"],
            "regime_votes": int(row["regime_votes"]),
            "stop_loss": f("supertrend"),
            "price": f("close"),
        }

    # ------------------------------------------------------------------ #
    def confluence_ok(self, sig: dict, min_strength: int = 3) -> bool:
        # ---- REGIME GATE (first, cheapest rejection) ----
        if sig["regime"] == "RANGING":
            return False
        if sig["regime"] == "TRANSITION":
            # only take flip signals with perfect strength — early trend catch
            if sig["side"] is None or sig["strength"] < self.cfg.strength_lookback:
                return False
            min_strength = self.cfg.strength_lookback

        c = self.cfg
        # kc_slope lags price (it's EMA-based) -- at a fresh flip bar, price
        # is still down at kc_pos <= entry_zone precisely because the
        # channel midline hasn't turned yet, so requiring the pullback
        # path's fully-confirmed slope (> 0.05 / < -0.05) here made kc_pos
        # and kc_slope near-mutually-exclusive and this path never fired on
        # real data (see commit 093c22b). A flip is an early-trend catch, so
        # it only needs the channel to not already be sloping against it.
        if sig["side"] == "BUY":
            return (sig["trend"] == "BULLISH"
                    and sig["kc_pos"] <= c.entry_zone
                    and sig["kc_slope"] > -0.02
                    and (sig["ao"] > 0 or sig["ao_rising"])
                    and sig["strength"] >= min_strength)
        if sig["side"] == "SELL":
            return (sig["trend"] == "BEARISH"
                    and sig["kc_pos"] >= 1 - c.entry_zone
                    and sig["kc_slope"] < 0.02
                    and (sig["ao"] < 0 or not sig["ao_rising"])
                    and sig["strength"] >= min_strength)
        return False

    def pullback_entry_ok(self, sig: dict) -> bool:
        """Mid-trend pullback entry. Only in full TRENDING regime."""
        if sig["regime"] != "TRENDING":
            return False
        c = self.cfg
        if sig["trend"] == "BULLISH":
            return (sig["kc_pos"] <= c.entry_zone
                    and sig["kc_slope"] > 0.05 and sig["ao_rising"])
        return (sig["kc_pos"] >= 1 - c.entry_zone
                and sig["kc_slope"] < -0.05 and not sig["ao_rising"])
