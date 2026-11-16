"""grade_auto: pure scoring definitions (spec M5, 'Scoring definitions')."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.modules.m05_assessments.scoring_defs import grade_auto


def _q(type_, marks=2, answer_key=None, options=None, scoring_mode="auto"):
    return SimpleNamespace(
        type=type_,
        marks=Decimal(str(marks)),
        answer_key=answer_key,
        options=options or [],
        scoring_mode=scoring_mode,
    )


def _opts(*flags):
    return [SimpleNamespace(id=uuid4(), is_correct=flag) for flag in flags]


def test_grade_auto_multi_choice_all_or_nothing_and_partial():
    opts = _opts(True, True, False, False)
    ids = [str(o.id) for o in opts]
    strict = _q("multi_choice", marks=4, options=opts, answer_key={"partial": False})
    default = _q("multi_choice", marks=4, options=opts)
    partial = _q("multi_choice", marks=4, options=opts, answer_key={"partial": True})
    cases = [
        (strict, ids[:2], "4.00"),
        (strict, ids[:1], "0.00"),
        (strict, ids[:3], "0.00"),
        (default, ids[:1], "0.00"),
        (partial, ids[:2], "4.00"),
        (partial, ids[:1], "2.00"),
        (partial, [ids[0], ids[2]], "0.00"),  # +1 right -1 wrong
        (partial, ids[2:], "0.00"),  # never negative
    ]
    for question, picked, expected in cases:
        assert grade_auto(question, {"option_ids": picked}) == Decimal(expected)


def test_fill_blank_case_insensitive_trimmed():
    key = {"blanks": [{"accepted": ["Paris", "paris france"]}, {"accepted": ["France"]}]}
    q = _q("fill_blank", answer_key=key)
    assert grade_auto(q, {"blanks": ["  PARIS ", "france"]}) == Decimal("2.00")
    assert grade_auto(q, {"blanks": ["Paris", "Spain"]}) == Decimal("1.00")  # split evenly
    assert grade_auto(q, {"blanks": ["Lyon"]}) == Decimal("0.00")  # missing blank is wrong
    sensitive = _q("fill_blank", marks=1, answer_key={**key, "case_sensitive": True})
    assert grade_auto(sensitive, {"blanks": ["paris", "France"]}) == Decimal("0.50")


def _single():
    opts = _opts(True, False)
    return _q("single_choice", marks=3, options=opts), str(opts[0].id), str(opts[1].id)


GAP_KEY = {"gaps": [{"accepted": ["range( 3 )"]}, {"accepted": ["print"]}]}


@pytest.mark.parametrize(
    ("question", "answer", "expected"),
    [
        (_q("code_gap", answer_key=GAP_KEY), {"gaps": ["range(   3 )", "print"]}, "2.00"),  # whitespace
        (
            _q("code_gap", answer_key=GAP_KEY),
            {"gaps": ["range(\n3 )", "Print"]},
            "1.00",
        ),  # code is case-sensitive
        (
            _q("code_gap", answer_key={**GAP_KEY, "case_sensitive": False}),
            {"gaps": ["range( 3 )", "PRINT"]},
            "2.00",
        ),
        (_q("code_gap", answer_key=GAP_KEY, scoring_mode="ai"), {"gaps": ["x"]}, "0.00"),
        (_q("short_answer", scoring_mode="ai"), {"text": "anything"}, "0.00"),
        (_q("fill_blank", answer_key=GAP_KEY), None, "0.00"),
    ],
)
def test_grade_auto_other_types(question, answer, expected):
    assert grade_auto(question, answer) == Decimal(expected)


def test_single_choice_full_marks_only_for_the_correct_option():
    q, right, wrong = _single()
    assert grade_auto(q, {"option_id": right}) == Decimal("3.00")
    assert grade_auto(q, {"option_id": wrong}) == Decimal("0.00")
    assert grade_auto(q, {"option_id": str(uuid4())}) == Decimal("0.00")
