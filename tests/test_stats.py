"""V2.0-Day3：多 run 采样统计单测。"""

from __future__ import annotations

from agent_eval.stats import summarize_scores


def test_empty():
    s = summarize_scores([])
    assert s == {"n": 0, "best": 0.0, "mean": 0.0, "std": 0.0, "pass_rate": 0.0}


def test_single():
    s = summarize_scores([1.0])
    assert s["n"] == 1 and s["best"] == 1.0 and s["mean"] == 1.0 and s["std"] == 0.0
    assert s["pass_rate"] == 1.0


def test_known_values():
    s = summarize_scores([1.0, 1.0, 0.0])
    assert s["best"] == 1.0
    assert s["mean"] == 0.667
    assert round(s["std"], 3) == 0.471  # sqrt(((0.333^2)*2 + (0.667^2))/3)
    assert s["pass_rate"] == 0.667


def test_all_zero():
    s = summarize_scores([0.0, 0.0])
    assert s["best"] == 0.0 and s["mean"] == 0.0 and s["std"] == 0.0
    assert s["pass_rate"] == 0.0


def test_mixed():
    s = summarize_scores([1.2, 0.8, 0.0, 1.2])
    assert s["best"] == 1.2
    assert round(s["mean"], 3) == 0.8
    assert s["pass_rate"] == 0.75
