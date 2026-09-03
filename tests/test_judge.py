"""LLM-as-a-Judge 判分器测试（V2.2）。

FakeClient 注入，全程不依赖真实 LLM / 网络；覆盖：通过/不通过/缺 key 降级/
无产物降级/JSON 解析容错/产物收集。
"""

from __future__ import annotations

from pathlib import Path

from agent_eval.judge import LLMJudge, _collect_artifacts, _parse_score
from agent_eval.spec import TaskSpec


class _FakeResp:
    def __init__(self, content: str) -> None:
        class _Msg:
            def __init__(self, c):
                self.content = c

        class _Choice:
            def __init__(self, c):
                self.message = _Msg(c)

        class _Usage:
            prompt_tokens = 10
            completion_tokens = 20
            total_tokens = 30

        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _FakeClient:
    """模拟 openai client：chat.completions.create(...) 返回固定内容。"""

    def __init__(self, content: str) -> None:
        self._content = content

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):  # noqa: ANN003
        return _FakeResp(self._content)


def _make_task(tmp_path: Path, verifier: str = "llm_judge") -> TaskSpec:
    return TaskSpec(
        id="TXXX",
        title="测试判分任务",
        level="L5",
        description="基于 input 输出 output/report.md",
        verifier=verifier,
        spec_path=tmp_path / "spec.yaml",
    )


def _make_artifact(workspace: Path) -> None:
    out = workspace / "output"
    out.mkdir(parents=True)
    (out / "report.md").write_text("# 周报\n\n本周完成：导出功能。\n", encoding="utf-8")


def test_collect_artifacts(tmp_path) -> None:
    _make_artifact(tmp_path)
    text = _collect_artifacts(tmp_path)
    assert "output/report.md" in text
    assert "本周完成" in text


def test_parse_score_tolerates_surrounding_text() -> None:
    data = _parse_score('好的，判分结果如下：\n{"score": 92, "passed": true, "reasoning": "内容完整"}\n以上。')
    assert data["score"] == 92
    assert data["passed"] is True


def test_judge_passed(tmp_path) -> None:
    _make_artifact(tmp_path)
    judge = LLMJudge(client=_FakeClient('{"score": 92, "passed": true, "reasoning": "内容完整、结构清晰"}'))
    v = judge.judge(_make_task(tmp_path), tmp_path)
    assert v["passed"] is True
    assert v["score"] == 0.92
    assert v["type"] == "llm_judge"
    assert "语义判分" in v["detail"]


def test_judge_failed(tmp_path) -> None:
    _make_artifact(tmp_path)
    judge = LLMJudge(client=_FakeClient('{"score": 40, "passed": false, "reasoning": "缺少风险与阻塞章节"}'))
    v = judge.judge(_make_task(tmp_path), tmp_path)
    assert v["passed"] is False
    assert v["score"] == 0.4


def test_judge_missing_key_degrades(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    _make_artifact(tmp_path)
    judge = LLMJudge()  # 无 client、无 key
    v = judge.judge(_make_task(tmp_path), tmp_path)
    assert v["passed"] is False
    assert "缺少 LLM API Key" in v["detail"]


def test_judge_no_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    judge = LLMJudge(client=_FakeClient('{"score": 90, "passed": true, "reasoning": "x"}'))
    v = judge.judge(_make_task(tmp_path), tmp_path)  # workspace 无 output/
    assert v["passed"] is False
    assert "未找到可判分的产物" in v["detail"]


def test_judge_call_failure_degrades(tmp_path) -> None:
    class _Broken:
        @property
        def chat(self):
            raise RuntimeError("网络不可达")

    _make_artifact(tmp_path)
    judge = LLMJudge(client=_Broken())
    v = judge.judge(_make_task(tmp_path), tmp_path)
    assert v["passed"] is False
    assert "判分调用失败" in v["detail"]
