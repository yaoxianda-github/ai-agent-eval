"""search_kb（RAG 检索工具）与 dsh session 解析（黑盒后端模型层）单测（V2.4）。"""

from __future__ import annotations

import json
from pathlib import Path

from agent_eval.backends.deepseek_harness import parse_dsh_session
from agent_eval.tools import run_tool


def _mk_kb(tmp_path: Path) -> Path:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "手册-甲.md").write_text(
        "# 甲产品\n\n## 质保\n\n整机质保 24 个月，电池 12 个月。\n", encoding="utf-8"
    )
    (kb / "手册-乙.md").write_text(
        "# 乙产品\n\n## 质保\n\n整机质保 36 个月，提供 7×24 售后。\n", encoding="utf-8"
    )
    (kb / "政策.txt").write_text(
        "通用政策：所有产品整机质保最低 24 个月。\n", encoding="utf-8"
    )
    return kb


def test_search_kb_hits_target_file(tmp_path):
    kb = _mk_kb(tmp_path)
    ok, obs = run_tool("search_kb", {"query": "乙产品 整机质保 多少个月"}, kb.parent)
    assert ok
    assert "[kb/手册-乙.md:" in obs
    assert "36" in obs
    # 来源定位包含相对路径与行区间
    assert "得分" in obs


def test_search_kb_missing_kb_dir(tmp_path):
    ok, obs = run_tool("search_kb", {"query": "任何查询"}, tmp_path)
    assert not ok
    assert "kb/" in obs


def test_search_kb_empty_query(tmp_path):
    ok, obs = run_tool("search_kb", {"query": "  "}, tmp_path)
    assert not ok
    assert "query" in obs


def test_search_kb_no_hit(tmp_path):
    kb = _mk_kb(tmp_path)
    ok, obs = run_tool("search_kb", {"query": "不存在的词xyzq"}, kb.parent)
    assert not ok
    assert "未检索到" in obs


# ---------- dsh session 解析 ----------

_SESSION_LINES = [
    {"type": "session", "id": "s1", "cwd": "/tmp/ws"},
    {"type": "user/message", "time": 1000, "data": {
        "content": [{"type": "text", "text": "查询乙产品质保月数，写入 output/answer.txt"}]}},
    {"type": "assistant/message", "time": 2000, "data": {
        "message": {
            "role": "assistant",
            "source": {"kind": "model", "provider": "deepseek-official", "model": "deepseek-v4-flash"},
            "content": [
                {"type": "reasoning", "text": "先检索知识库"},
                {"type": "tool-call", "id": "call_1", "name": "bash",
                 "arguments": '{"command": "echo hello"}'},
            ],
        }}},
    {"type": "tool/call", "time": 2001, "data": {"callId": "call_1", "name": "bash",
                                                  "arguments": '{"command": "echo hello"}'}},
    {"type": "tool/result", "time": 2002, "data": {
        "message": {
            "source": {"kind": "tool", "callId": "call_1"},
            "content": [{"type": "tool-result", "content": [{"type": "text", "text": "hello"}]}],
        }}},
    {"type": "assistant/message", "time": 3000, "data": {
        "message": {
            "role": "assistant",
            "source": {"kind": "model", "model": "deepseek-v4-flash"},
            "content": [{"type": "text", "text": "答案：36"}]},
    }},
]


def _write_session(path: Path):
    with open(path, "w", encoding="utf-8") as f:
        for line in _SESSION_LINES:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    # 用 zstd CLI 压缩（无 CLI 时跳过集成，只测解析逻辑）
    import shutil
    import subprocess

    zc = shutil.which("zstd")
    if zc:
        zf = path.with_name(path.name + ".zst")
        subprocess.run([zc, "-f", str(path), "-o", str(zf)], check=False)
        zf.rename(path.with_name(path.name + ".zstd"))


def test_parse_dsh_session_structure(tmp_path, monkeypatch):
    """解析逻辑：reasoning/decision/final/tool 四类节点齐全，callId 正确映射工具名。"""
    from agent_eval.backends import deepseek_harness as dsh

    fake = "\n".join(json.dumps(l, ensure_ascii=False) for l in _SESSION_LINES).encode()
    monkeypatch.setattr(dsh, "_decompress_zstd", lambda p: fake)
    traces = parse_dsh_session(tmp_path / "x.zstd")

    kinds = [(t["kind"], t.get("phase") or t.get("category") or "", t.get("tool") or "") for t in traces]
    assert kinds == [
        ("llm", "reasoning", ""),
        ("llm", "decision", "bash"),
        ("tool", "tool", "bash"),
        ("llm", "final", ""),
    ]
    assert traces[1]["output"] == '{"command": "echo hello"}'
    assert traces[2]["observation"] == "hello"
    assert traces[2]["category"] == "tool"
    assert traces[0]["model"] == "deepseek-v4-flash"


def test_parse_dsh_session_real_zstd(tmp_path):
    """集成：真实 .zstd 文件可解压解析（依赖系统 zstd，缺失则跳过）。"""
    import shutil

    if not shutil.which("zstd"):
        return
    p = tmp_path / "session.jsonl"
    _write_session(p)
    zf = tmp_path / "session.jsonl.zstd"
    assert zf.exists()
    traces = parse_dsh_session(zf)
    assert any(t["kind"] == "llm" for t in traces)
    assert any(t["kind"] == "tool" for t in traces)


def test_parse_dsh_session_missing_file(tmp_path):
    assert parse_dsh_session(tmp_path / "none.zstd") == []


def test_session_dir_encoding_rule(tmp_path, monkeypatch):
    """workspace 绝对路径编码规则：剥开头 / 再替换 /→-，前缀后缀各 --。"""
    from agent_eval.backends import deepseek_harness as dsh

    ws = Path("/Users/foo/proj/results/runs/abc/workspace")
    dsh_home = tmp_path
    expected = tmp_path / "sessions" / "--Users-foo-proj-results-runs-abc-workspace--"
    # 无目录 → None
    assert dsh._session_dir_for(ws, dsh_home) is None
    # 目录存在且含 session-* 子目录 → 返回最新
    sub = expected / "session-old"
    sub.mkdir(parents=True)
    (sub / "session.jsonl.zstd").write_text("x", encoding="utf-8")
    assert dsh._session_dir_for(ws, dsh_home) == sub
