"""V2.0-Day1：确定性判定器单测（5 种校验点 + 路径解析）。"""

from __future__ import annotations

import pytest

from agent_eval.spec import Checkpoint
from agent_eval.verifiers import run_checkpoints


def _run(task, workspace):
    return {v["id"]: v for v in run_checkpoints(task, workspace)}


def _mk_task(make_task, cps):
    return make_task(checkpoints=[Checkpoint(**c) for c in cps])


# ---------- file_exists / file_not_exists ----------

def test_file_exists_hit(make_task, tmp_path):
    (tmp_path / "output").mkdir()
    (tmp_path / "output" / "result.md").write_text("x", encoding="utf-8")
    task = _mk_task(make_task, [{"id": "c1", "type": "file_exists", "path": "output/result.md"}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is True


def test_file_exists_miss(make_task, tmp_path):
    task = _mk_task(make_task, [{"id": "c1", "type": "file_exists", "path": "nope.txt"}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is False


def test_file_not_exists(make_task, tmp_path):
    task = _mk_task(make_task, [{"id": "c1", "type": "file_not_exists", "path": "nope.txt"}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is True


def test_file_exists_glob(make_task, tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.md").write_text("x", encoding="utf-8")
    task = _mk_task(make_task, [{"id": "c1", "type": "file_exists", "path": "out/*.md"}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is True


def test_file_exists_glob_no_match(make_task, tmp_path):
    task = _mk_task(make_task, [{"id": "c1", "type": "file_exists", "path": "out/*.md"}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is False


# ---------- content_contains / content_not_contains ----------

def test_content_contains_hit(make_task, tmp_path):
    (tmp_path / "notes.md").write_text("记录于 2026/9/2", encoding="utf-8")
    task = _mk_task(make_task, [{"id": "c1", "type": "content_contains", "path": "notes.md", "pattern": r"2026"}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is True


def test_content_contains_miss(make_task, tmp_path):
    (tmp_path / "notes.md").write_text("hello", encoding="utf-8")
    task = _mk_task(make_task, [{"id": "c1", "type": "content_contains", "path": "notes.md", "pattern": r"2026"}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is False


def test_content_not_contains_no_residue(make_task, tmp_path):
    (tmp_path / "notes.md").write_text("统一为 2026-09-02", encoding="utf-8")
    task = _mk_task(make_task, [{"id": "c1", "type": "content_not_contains", "path": "notes.md", "pattern": r"\d{4}/\d{1,2}/\d{1,2}"}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is True


def test_content_not_contains_residue(make_task, tmp_path):
    (tmp_path / "notes.md").write_text("记录于 2026/9/2", encoding="utf-8")
    task = _mk_task(make_task, [{"id": "c1", "type": "content_not_contains", "path": "notes.md", "pattern": r"\d{4}/\d{1,2}/\d{1,2}"}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is False


def test_content_contains_glob(make_task, tmp_path):
    d = tmp_path / "input"
    d.mkdir()
    (d / "a.md").write_text("2026-09-03 ok", encoding="utf-8")
    (d / "b.md").write_text("ok too", encoding="utf-8")
    task = _mk_task(make_task, [{"id": "c1", "type": "content_contains", "path": "input/*.md", "pattern": r"2026-09-03"}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is False  # 任一文件不命中则整体失败


# ---------- cmd_exit_zero 与路径解析 ----------

def test_cmd_success(make_task, tmp_path):
    task = _mk_task(make_task, [{"id": "c1", "type": "cmd_exit_zero", "cmd": 'python -c "import sys; sys.exit(0)"'}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is True


def test_cmd_fail_keeps_exit_code(make_task, tmp_path):
    task = _mk_task(make_task, [{"id": "c1", "type": "cmd_exit_zero", "cmd": 'python -c "import sys; sys.exit(3)"'}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is False
    assert "退出码 3" in v["c1"]["detail"]


def test_cmd_dot_resolves_to_absolute_workspace(make_task, tmp_path):
    # 若 "." 未被替换为工作目录绝对路径，脚本会收到非绝对路径而退出 1
    cmd = 'python -c "import pathlib,sys; sys.exit(0 if pathlib.Path(sys.argv[1]).is_absolute() else 1)" .'
    task = _mk_task(make_task, [{"id": "c1", "type": "cmd_exit_zero", "cmd": cmd}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is True


def test_cmd_scripts_prefix_runs_project_script(make_task, tmp_path):
    # 构造满足 verify_t102 的最小工作目录，验证 @scripts/ 前缀解析到项目 scripts/
    data = "date,category,amount\n2026-08-01,数码,100.00\n2026-08-01,家居,50.00\n2026-08-02,数码,30.00\n"
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / "sales.csv").write_text(data, encoding="utf-8")
    out = tmp_path / "output"
    out.mkdir()
    (out / "summary.csv").write_text("category,total\n数码,130.00\n家居,50.00\n", encoding="utf-8")
    (out / "top3.md").write_text("# Top3\n| 1 | 数码 | 130.00 |\n| 2 | 家居 | 50.00 |\n", encoding="utf-8")

    task = _mk_task(make_task, [{"id": "c1", "type": "cmd_exit_zero", "cmd": "python @scripts/verify_t102.py ."}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is True


# ---------- 未知类型 ----------

def test_unknown_type_reports_fail(make_task, tmp_path):
    task = _mk_task(make_task, [{"id": "c1", "type": "not_a_type", "path": "x"}])
    v = _run(task, tmp_path)
    assert v["c1"]["passed"] is False
    assert "未知校验点类型" in v["c1"]["detail"]
