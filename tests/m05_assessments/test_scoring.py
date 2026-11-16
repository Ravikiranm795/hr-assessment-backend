"""grade_auto: pure scoring definitions (spec M5, 'Scoring definitions')."""

from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.modules.m05_assessments.scoring_defs import grade_auto


def _q(type_, marks=2, answer_key=None, options=None, scoring_mode="auto"):
    return SimpleNamespace(
        type=type_, marks=Decimal(str(marks)), answer_key=answer_key, options=options or [], scoring_mode=scoring_mode
    )


def _opts(*flags):
    return [SimpleNamespace(id=uuid4(), is_correct=flag) for flag in flags]


def test_single_choice_full_marks_only_for_the_correct_option():
    opts = _opts(True, False, False)
    q = _q("single_choice", marks=3, options=opts)
    assert grade_auto(q, {"option_id": str(opts[0].id)}) == Decimal("3.00")
    assert grade_auto(q, {"option_id": str(opts[1].id)}) == Decimal("0.00")
    assert grade_auto(q, {"option_id": str(uuid4())}) == Decimal("0.00")
    assert grade_auto(q, None) == Decimal("0.00")
    assert grade_auto(q, {}) == Decimal("0.00")


def test_grade_auto_multi_choice_all_or_nothing_and_partial():
    opts = _opts(True, True, False, False)
    ids = [str(o.id) for o in opts]
    strict = _q("multi_choice", marks=4, options=opts, answer_key={"partial": False})
    assert grade_auto(strict, {"option_ids": ids[:2]}) == Decimal("4.00")
    assert grade_auto(strict, {"option_ids": ids[:1]}) == Decimal("0.00")  # missing one
    assert grade_auto(strict, {"option_ids": [ids[0], ids[1], ids[2]]}) == Decimal("0.00")  # extra wrong

    default = _q("multi_choice", marks=4, options=opts, answer_key=None)
    assert grade_auto(default, {"option_ids": ids[:1]}) == Decimal("0.00")  # all-or-nothing is the default

    partial = _q("multi_choice", marks=4, options=opts, answer_key={"partial": True})
    assert grade_auto(partial, {"option_ids": ids[:2]}) == Decimal("4.00")
    assert grade_auto(partial, {"option_ids": ids[:1]}) == Decimal("2.00")  # one of two correct
    assert grade_auto(partial, {"option_ids": [ids[0], ids[2]]}) == Decimal("0.00")  # +1 right -1 wrong
    assert grade_auto(partial, {"option_ids": ids[2:]}) == Decimal("0.00")  # never negative
    assert grade_auto(partial, {"option_ids": []}) == Decimal("0.00")


def test_fill_blank_case_insensitive_trimmed():
    key = {"blanks": [{"accepted": ["Paris", "paris france"]}, {"accepted": ["France"]}]}
    q = _q("fill_blank", marks=2, answer_key=key)
    assert grade_auto(q, {"blanks": ["  PARIS ", "france"]}) == Decimal("2.00")
    assert grade_auto(q, {"blanks": ["Paris", "Spain"]}) == Decimal("1.00")  # marks split evenly
    assert grade_auto(q, {"blanks": ["Lyon"]}) == Decimal("0.00")  # missing blank counts as wrong
    assert grade_auto(q, {"blanks": []}) == Decimal("0.00")
    assert grade_auto(q, ["Paris", "France"]) == Decimal("2.00")  # bare list accepted


def test_fill_blank_case_sensitive_flag_and_rounding():
    key = {"blanks": [{"accepted": ["NaN"]}, {"accepted": ["x"]}, {"accepted": ["y"]}], "case_sensitive": True}
    q = _q("fill_blank", marks=1, answer_key=key)
    assert grade_auto(q, {"blanks": ["nan", "x", "y"]}) == Decimal("0.67")
    assert grade_auto(q, {"blanks": ["NaN", "x", "y"]}) == Decimal("1.00")
    assert grade_auto(q, {"blanks": [" NaN ", "x", "z"]}) == Decimal("0.67")  # still trimmed


def test_code_gap_whitespace_normalized():
    key = {"gaps": [{"accepted": ["range( 3 )"]}, {"accepted": ["print"]}]}
    q = _q("code_gap", marks=2, answer_key=key)
    assert grade_auto(q, {"gaps": ["range(   3 )", "print"]}) == Decimal("2.00")
    assert grade_auto(q, {"gaps": ["range(\n3 )", "Print"]}) == Decimal("1.00")  # code is case-sensitive
    insensitive = _q("code_gap", marks=2, answer_key={**key, "case_sensitive": False})
    assert grade_auto(insensitive, {"gaps": ["range( 3 )", "PRINT"]}) == Decimal("2.00")


def test_ai_and_manual_questions_have_no_auto_marks():
    assert grade_auto(_q("short_answer", scoring_mode="ai"), {"text": "anything"}) == Decimal("0.00")
    gap = _q("code_gap", answer_key={"gaps": [{"accepted": ["a"]}]}, scoring_mode="ai")
    assert grade_auto(gap, {"gaps": ["a"]}) == Decimal("0.00")
