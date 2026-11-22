"""Compare explicit human labels against both detector classifications."""

from collections import defaultdict

from ..accuracy_evaluation import evaluate as evaluate_phase6
from .io import write_json, write_rows


CHOCH = {"EXTERNAL_CHOCH", "STRONG_EXTERNAL_CHOCH"}
HISTORY_COLUMNS = (
    "id", "case_id", "event_id", "symbol", "timeframe", "event_timestamp", "reviewer_id",
    "review_label", "reviewed_at", "review_version", "notes", "action", "undo_target",
    "prior_label", "prior_notes",
)
DECISION_COLUMNS = HISTORY_COLUMNS[1:11]


def effective_history(history, case_ids):
    latest = {}
    for row in history:
        if row["case_id"] in case_ids:
            latest[row["case_id"], row["reviewer_id"]] = row
    return list(latest.values())


def report(cases, history):
    latest = effective_history(history, {case.case_id for case in cases})
    by_case = defaultdict(set)
    for row in latest:
        if row["review_label"]:
            by_case[row["case_id"]].add(row["review_label"])
    disagreements = sorted(key for key, labels in by_case.items() if len(labels) > 1)
    agreed = {key: next(iter(labels)) for key, labels in by_case.items() if len(labels) == 1}

    engines = {}
    for engine in ("frozen", "shadow"):
        inputs = []
        for case in cases:
            verdict = agreed.get(case.case_id)
            prediction = case.evidence[f"{engine}_classification"]
            if verdict == "VALID_CHOCH":
                label = "CORRECT_CHoCH" if prediction in CHOCH else "MISSED_CHoCH"
            elif verdict in ("NOT_CHOCH", "VALID_BOS", "ORDINARY_BREAK"):
                label = "FALSE_CHoCH" if prediction in CHOCH else "ORDINARY_BREAK"
            else:
                label = "AMBIGUOUS" if verdict == "AMBIGUOUS" else "UNREVIEWED"
            inputs.append(dict(case_id=case.case_id, algorithm_classification=prediction,
                               manual_classification=label))
        outcome = evaluate_phase6(inputs)
        engines[engine] = outcome["choch"]
    strong = [case.case_id for case in cases
              if (case.evidence["frozen_classification"] == "STRONG_EXTERNAL_CHOCH")
              != (case.evidence["shadow_classification"] == "STRONG_EXTERNAL_CHOCH")]
    ambiguous = sum(label == "AMBIGUOUS" for label in agreed.values())
    return dict(total_cases=len(cases), reviewed_cases=len(by_case),
                unreviewed_cases=len(cases) - len(by_case), ambiguous_cases=ambiguous,
                definitive_cases=len(agreed) - ambiguous, reviewer_disagreements=disagreements,
                strong_classification_disagreements=strong, strong_accuracy=None, **engines)


def export(output, cases, storage):
    history = storage.history()
    latest = effective_history(history, {case.case_id for case in cases})
    summary = report(cases, history)
    output.mkdir(parents=True, exist_ok=False)
    write_rows(output / "review_decisions.csv", DECISION_COLUMNS,
               (exported(row, DECISION_COLUMNS) for row in latest))
    write_rows(output / "review_history.csv", HISTORY_COLUMNS,
               (exported(row, HISTORY_COLUMNS) for row in history))
    columns = tuple(summary["frozen"])
    write_rows(output / "frozen_shadow_accuracy.csv", ("detector", *columns),
               (dict(detector=key, **summary[key]) for key in ("frozen", "shadow")))
    write_json(output / "review_summary.json", summary)
    return summary


def exported(row, columns):
    projected = {key: row[key] for key in columns}
    for key in ("notes", "prior_notes"):
        value = projected.get(key)
        if value and value.lstrip().startswith(("=", "+", "-", "@")):
            projected[key] = "'" + value
    return projected
