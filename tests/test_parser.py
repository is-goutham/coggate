import pytest

from src.core.parser import parse_answer_grammar


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("/coggate answer q1=A", {"q1": "A"}),
        ("/coggate answer q1=A q2=C q3=B", {"q1": "A", "q2": "C", "q3": "B"}),
        ("  /COGGATE   ANSWER   Q1=a Q2=c  ", {"q1": "A", "q2": "C"}),
        ("/coggate\tanswer\tq1=A\tq2=B", {"q1": "A", "q2": "B"}),
    ],
)
def test_parse_answer_grammar_accepts_exact_commands(
    comment: str, expected: dict[str, str]
) -> None:
    assert parse_answer_grammar(comment) == expected


@pytest.mark.parametrize(
    "comment",
    [
        "",
        "please /coggate answer q1=A",
        "/coggate answer q1=A thanks",
        "/coggate answer",
        "/coggate answer q1",
        "/coggate answer q1=D",
        "/coggate answer q4=A",
        "/coggate answer q1=A q1=B",
        "/coggate answer q1=Aq2=B",
        "/coggate answer q1=A, q2=B",
        "/coggate answers q1=A",
    ],
)
def test_parse_answer_grammar_rejects_non_exact_commands(comment: str) -> None:
    assert parse_answer_grammar(comment) is None

