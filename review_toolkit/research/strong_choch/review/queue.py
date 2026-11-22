"""Read and validate the immutable review queue."""

from dataclasses import dataclass
import math
from pathlib import Path
import re
from types import MappingProxyType

from .io import read_rows, utc


LABELS = {
    "vc": "VALID_CHOCH", "nc": "NOT_CHOCH", "vb": "VALID_BOS",
    "ob": "ORDINARY_BREAK", "am": "AMBIGUOUS",
}
BUTTONS = {"vc": "Valid CHoCH", "nc": "Not CHoCH", "vb": "Valid BOS",
           "ob": "Ordinary Break", "am": "Ambiguous"}
CLASSIFICATIONS = {"", "EXTERNAL_CHOCH", "STRONG_EXTERNAL_CHOCH", "EXTERNAL_BOS",
                   "EXTERNAL_TREND_ESTABLISHED"}
CASE_PATTERN = r"[A-Z0-9]{1,12}_\d{8}_\d{4}_[UD]"
REQUIRED = {"symbol", "timestamp", "direction", "close", "reason", "frozen_classification",
            "shadow_classification", "frozen_prior_state", "shadow_prior_state"}


@dataclass(frozen=True)
class Case:
    case_id: str
    event_id: str
    symbol: str
    timestamp: str
    direction: str
    evidence: MappingProxyType


def load_queue(path: Path):
    cases = {}
    for row in read_rows(path):
        if REQUIRED - row.keys() or None in row or any(value is None for value in row.values()):
            raise ValueError("Review queue is missing required case fields")
        symbol = row["symbol"]
        stamp = utc(row["timestamp"])
        direction = row["direction"]
        if (not re.fullmatch(r"[A-Z0-9]{1,12}_USDT", symbol) or direction not in ("up", "down")
                or stamp.hour % 4 or stamp.minute or stamp.second or stamp.microsecond
                or not math.isfinite(float(row["close"])) or float(row["close"]) <= 0
                or any(row[f"{engine}_classification"] not in CLASSIFICATIONS
                       for engine in ("frozen", "shadow"))):
            raise ValueError("Review queue contains invalid candle evidence")
        suffix = direction[0].upper()
        case_id = f"{symbol.removesuffix('_USDT')}_{stamp:%Y%m%d_%H%M}_{suffix}"
        event_id = f"{symbol}:4h:{stamp.isoformat()}:{direction}"
        if case_id in cases or row.get("case_id", case_id) != case_id or row.get("event_id", event_id) != event_id:
            raise ValueError(f"Duplicate or inconsistent case identity: {case_id}")
        cases[case_id] = Case(case_id, event_id, symbol, stamp.isoformat(), direction, MappingProxyType(row))
    if not cases:
        raise ValueError("Review queue is empty")
    priority = {"changed": 1, "named_audit": 2, "sampled_unchanged_phase_2_6": 3}
    return tuple(sorted(cases.values(), key=lambda case: (
        0 if case.evidence.get("review_focus") else priority.get(case.evidence["reason"], 4),
        case.timestamp, case.symbol, case.direction,
    )))
