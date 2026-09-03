"""V2.0-Day1：评分器单测。"""

from __future__ import annotations

import pytest

from agent_eval.scoring import score_task
from agent_eval.spec import TaskSpec


def _verdicts(passed_list: list[bool]) -> list[dict]:
    return [{"id": f"c{i+1}", "passed": p} for i, p in enumerate(passed_list)]


def test_all_passed(make_task):
    task = make_task(weight=1.0)
    m = score_task(task, _verdicts([True, True, True]))
    assert m["score"] == 1.0
    assert m["pass_rate"] == 1.0
    assert m["passed"] == 3 and m["total"] == 3


def test_partial_passed_weighted(make_task):
    task = make_task(weight=1.2)
    m = score_task(task, _verdicts([True, True, False]))
    # scoring 内部将通过率 round 到 3 位
    assert m["pass_rate"] == 0.667
    assert m["score"] == 0.8  # 1.2 × 2/3 = 0.8


def test_all_failed(make_task):
    task = make_task(weight=2.0)
    m = score_task(task, _verdicts([False, False]))
    assert m["score"] == 0.0


def test_empty_verdicts_zero(make_task):
    task = make_task(weight=1.5)
    m = score_task(task, [])
    assert m["score"] == 0.0
    assert m["pass_rate"] == 0.0
    assert m["total"] == 0


def test_weight_full_pass(make_task):
    task = make_task(weight=2.0)
    m = score_task(task, _verdicts([True]))
    assert m["score"] == 2.0
