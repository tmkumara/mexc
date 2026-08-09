from reports import _stats, _format_report


def _sig(status: str, pnl_roi: float, direction: str = "LONG") -> dict:
    return {"status": status, "pnl_roi": pnl_roi, "direction": direction}


def test_stats_counts_breakeven_separately_from_win_and_loss():
    signals = [
        _sig("win", 7.0),
        _sig("win", 7.0),
        _sig("loss", -10.0),
        _sig("breakeven", 0.0),
        _sig("pending", None),
        _sig("expired", 0.0),
    ]
    s = _stats(signals)
    assert s["wins"] == 2
    assert s["losses"] == 1
    assert s["breakevens"] == 1
    assert s["pending"] == 1
    assert s["expired"] == 1
    assert s["total"] == 6


def test_win_rate_excludes_breakeven_from_the_ratio():
    signals = [_sig("win", 7.0), _sig("loss", -10.0), _sig("breakeven", 0.0)]
    s = _stats(signals)
    assert s["win_rate"] == 50.0   # 1 win / (1 win + 1 loss), not /3


def test_net_roi_includes_breakeven():
    signals = [_sig("win", 7.0), _sig("loss", -10.0), _sig("breakeven", 0.0)]
    s = _stats(signals)
    assert s["net_roi"] == -3.0


def test_format_report_shows_breakeven_line():
    signals = [_sig("win", 7.0), _sig("breakeven", 0.0)]
    text = _format_report("Test Report", signals)
    assert "Breakeven" in text
