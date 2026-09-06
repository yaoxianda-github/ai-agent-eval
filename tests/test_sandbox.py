"""V2.0-Day2：命令沙箱单测（子进程 + 超时强杀 + 输出截断）。"""

from __future__ import annotations

from agent_eval.sandbox import run_command_sandboxed


def test_success(tmp_path):
    r = run_command_sandboxed('python -c "print(\'hi\')"', tmp_path, timeout_s=10)
    assert r.ok is True
    assert r.exit_code == 0
    assert "hi" in r.stdout


def test_failure_exit_code(tmp_path):
    r = run_command_sandboxed('python -c "import sys; sys.exit(3)"', tmp_path, timeout_s=10)
    assert r.ok is False
    assert r.exit_code == 3


def test_runs_in_cwd(tmp_path):
    r = run_command_sandboxed(
        'python -c "import pathlib; pathlib.Path(\'made.txt\').write_text(\'x\')"',
        tmp_path,
        timeout_s=10,
    )
    assert r.ok is True
    assert (tmp_path / "made.txt").exists()


def test_timeout_kills(tmp_path):
    r = run_command_sandboxed('python -c "import time; time.sleep(5)"', tmp_path, timeout_s=1)
    assert r.timed_out is True
    assert r.ok is False
    assert "超时" in r.error


def test_output_truncated(tmp_path):
    r = run_command_sandboxed(
        'python -c "print(\'x\'*200000)"', tmp_path, timeout_s=10, max_output_chars=1000
    )
    # 截断后长度受控（1000 + 截断提示）
    assert len(r.stdout) <= 1000 + 60
    assert "已截断" in r.stdout


def test_bad_command_reports_error(tmp_path):
    r = run_command_sandboxed('python -c "raise RuntimeError(\'boom\')"', tmp_path, timeout_s=10)
    # 命令自身抛异常 → stderr 有 traceback，exit_code 非 0
    assert r.ok is False
    assert r.exit_code != 0
    assert "boom" in r.stderr


def test_gbk_output_decoded_not_mojibake(tmp_path):
    """GBK 编码的子进程输出应被正确解码，而不是变成 U+FFFD �（历史乱码根因）。"""
    from agent_eval.sandbox import _decode_bytes

    gbk = "'ls' 不是内部或外部命令，也不是可运行的程序".encode("gbk")
    assert _decode_bytes(gbk) == "'ls' 不是内部或外部命令，也不是可运行的程序"

    utf8 = "正常中文输出 ✅".encode("utf-8")
    assert _decode_bytes(utf8) == "正常中文输出 ✅"

    # 未知字节兜底不抛异常且不含孤立代理
    out = _decode_bytes(b"\xff\xfe\x01\x02")
    assert isinstance(out, str)
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in out)

    # 空输出
    assert _decode_bytes(b"") == ""


def test_command_gbk_stdout_roundtrip(tmp_path):
    """真实命令路径：stdout 经二进制文件回读，中文不损坏。"""
    from agent_eval.sandbox import run_command_sandboxed

    r = run_command_sandboxed("echo '你好 hello'", tmp_path)
    assert r.exit_code == 0
    assert "你好" in r.stdout
    assert "\ufffd" not in r.stdout
