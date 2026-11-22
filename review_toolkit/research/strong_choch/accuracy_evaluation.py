"""Evaluate manually adjudicated external structure candidates."""

import argparse
from collections import Counter
import json
from pathlib import Path

from .evaluate import read_rows
from .replay import write_json


LABELS = {
    "UNREVIEWED", "CORRECT_CHoCH", "MISSED_CHoCH", "FALSE_CHoCH", "CORRECT_BOS",
    "MISSED_BOS", "ORDINARY_BREAK", "AMBIGUOUS",
}


def evaluate(rows):
    labels = Counter()
    matrix = {kind: Counter(tp=0, fp=0, fn=0, tn=0) for kind in ("choch", "bos")}
    seen = set()
    for row in rows:
        key = row["case_id"]
        if key in seen:
            raise ValueError(f"{key}: repeated case ID")
        seen.add(key)
        label = row["manual_classification"].strip()
        if label not in LABELS:
            raise ValueError(f"{key}: unsupported manual label {label!r}")
        labels[label] += 1
        if label in ("UNREVIEWED", "AMBIGUOUS"):
            continue
        detected = row["algorithm_classification"]
        choch = detected in ("EXTERNAL_CHOCH", "STRONG_EXTERNAL_CHOCH")
        bos = detected == "EXTERNAL_BOS"
        if ((label in ("CORRECT_CHoCH", "FALSE_CHoCH") and not choch)
                or (label == "MISSED_CHoCH" and choch)
                or (label == "CORRECT_BOS" and not bos)
                or (label == "MISSED_BOS" and bos)):
            raise ValueError(f"{key}: {label} conflicts with {detected}")
        for kind, actual, predicted in (
            ("choch", label in ("CORRECT_CHoCH", "MISSED_CHoCH"), choch),
            ("bos", label in ("CORRECT_BOS", "MISSED_BOS"), bos),
        ):
            matrix[kind][("t" if actual == predicted else "f") + ("p" if predicted else "n")] += 1

    outcomes = {}
    for kind, counts in matrix.items():
        tp, fp, fn, tn = (counts[key] for key in ("tp", "fp", "fn", "tn"))
        # Require more than a singleton on each side before publishing rates.
        supported = tp + fn >= 2 and fp + tn >= 2
        outcomes[kind] = dict(correctly_detected=tp, missed=fn, incorrect=fp,
                              true_negatives=tn, reviewed_positives=tp + fn, reviewed_negatives=fp + tn,
                              precision=tp / (tp + fp) if supported and tp + fp else None,
                              recall=tp / (tp + fn) if supported else None)
    return dict(total=len(seen), definitive_reviewed=sum(matrix["choch"].values()),
                unreviewed=labels["UNREVIEWED"], ambiguous_excluded=labels["AMBIGUOUS"],
                **outcomes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(read_rows(args.input))
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
