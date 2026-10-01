from __future__ import annotations

import re

_ANSWER_COMMAND = re.compile(
    r"/coggate\s+answer(?P<answers>(?:\s+q[1-3]=[A-C])+)",
    re.IGNORECASE,
)
_ANSWER_PAIR = re.compile(r"q[1-3]=[A-C]", re.IGNORECASE)


def parse_answer_grammar(comment_text: str) -> dict[str, str] | None:
    """Parse an exact CogGate answer command, rejecting malformed or duplicate answers."""
    match = _ANSWER_COMMAND.fullmatch(comment_text.strip())
    if match is None:
        return None

    pairs = _ANSWER_PAIR.findall(match.group("answers"))
    answers: dict[str, str] = {}
    for pair in pairs:
        question_id, option = pair.split("=", maxsplit=1)
        normalized_id = question_id.lower()
        if normalized_id in answers:
            return None
        answers[normalized_id] = option.upper()

    return answers or None

