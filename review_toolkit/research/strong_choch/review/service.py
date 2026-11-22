"""Authorization, navigation and validated review actions."""

import re

from .queue import CASE_PATTERN, LABELS


CALLBACK = re.compile(rf"r:(vc|nc|vb|ob|am|dt|pr|nx):({CASE_PATTERN}):(0|[1-9]\d{{0,8}})")


def parse_callback(payload):
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > 64:
        raise ValueError("Invalid review button")
    match = CALLBACK.fullmatch(payload)
    if not match:
        raise ValueError("Invalid review button")
    action, case_id, version = match.groups()
    return action, case_id, int(version)


class Service:
    def __init__(self, cases, storage, allowed):
        self.cases = {case.case_id: case for case in cases}
        self.order = tuple(self.cases)
        self.storage = storage
        self.allowed = frozenset(allowed)

    def authorized(self, reviewer, chat_type):
        return chat_type == "private" and reviewer in self.allowed

    def require(self, reviewer):
        if reviewer not in self.allowed:
            raise PermissionError("Reviewer is not authorized")

    def case(self, reviewer, case_id):
        self.require(reviewer)
        if case_id not in self.cases:
            raise ValueError("Unknown case ID. Use /next or /case followed by the shown ID.")
        return self.cases[case_id]

    def open_case(self, reviewer, case_id):
        case = self.case(reviewer, case_id)
        self.storage.cursor(reviewer, case_id)
        return case

    def navigate(self, reviewer, action, anchor=None):
        self.require(reviewer)
        current = anchor or self.storage.cursor(reviewer)
        index = self.order.index(current) if current in self.cases else -1
        if action == "previous":
            return self.open_case(reviewer, self.order[max(0, index - 1)])
        cycle = self.order[index + 1:] + self.order[:index + 1]
        if action == "skip":
            cycle = tuple(key for key in cycle if key != current)
        for key in cycle:
            effective = self.storage.effective(reviewer, key)
            if not effective or not effective["review_label"]:
                return self.open_case(reviewer, key)
        return None

    def decide(self, reviewer, case_id, action, version, request):
        case = self.case(reviewer, case_id)
        if action not in LABELS or type(version) is not int or version < 0:
            raise ValueError("Invalid review action")
        return self.storage.write(case, reviewer, f"callback:{request}",
                                  label=LABELS[action], expected=version)

    def note(self, reviewer, case_id, text, request):
        case = self.case(reviewer, case_id)
        if len(text) > 1000:
            raise ValueError("Notes are limited to 1,000 characters")
        return self.storage.write(case, reviewer, f"update:{request}:note", notes=text)

    def undo(self, reviewer, request):
        self.require(reviewer)
        return self.storage.undo(self.cases, reviewer, f"update:{request}:undo")
