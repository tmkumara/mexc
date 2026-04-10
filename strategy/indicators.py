"""
Technical indicator calculations: EMA, RSI, VWAP, Volume MA.
All functions operate on pandas Series/DataFrame and return pandas Series.
"""

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average (EWM, span-based)."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 7) -> pd.Series:
    """
    Wilder's RSI using EWM (same as most charting platforms).
    Returns values in [0, 100].
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def vwap(df: pd.DataFrame) -> pd.Series:
    """
    Intraday VWAP that resets at UTC midnight each day.

    Requires DataFrame with columns: high, low, close, volume.
    Index must be a DatetimeIndex (tz-aware UTC or localizable).
    """
    df = df.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")

    typical = (df["high"] + df["low"] + df["close"]) / 3
    dates = df.index.normalize()  # UTC midnight floor
    unique_dates = dates.unique()

    result = pd.Series(index=df.index, dtype=float)
    for d in unique_dates:
        mask = dates == d
        tp_vol = typical[mask] * df["volume"][mask]
        cum_tp_vol = tp_vol.cumsum()
        cum_vol = df["volume"][mask].cumsum()
        result[mask] = cum_tp_vol / cum_vol.where(cum_vol > 0, np.nan)

    return result


def volume_ma(series: pd.Series, period: int = 20) -> pd.Series:
    """Simple moving average of volume."""
    return series.rolling(window=period).mean()
