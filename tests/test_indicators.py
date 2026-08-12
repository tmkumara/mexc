import numpy as np
import pandas as pd
import pytest

from strategy import calculate_atr


def test_atr_values():
    df = pd.DataFrame({
        "open":  [100.0, 101.0, 99.0, 102.0],
        "high":  [101.5, 102.0, 101.0, 103.0],
        "low":   [99.5, 100.0, 98.0, 101.0],
        "close": [101.0, 99.0, 102.0, 102.5],
    })
    atr = calculate_atr(df, 3)
    assert not np.isnan(atr.iloc[-1])
    assert atr.iloc[-1] > 0.0
