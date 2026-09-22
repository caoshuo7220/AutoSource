"""analyze_run 的异常工具序列判定（开发侧工具里唯一带判定逻辑的一处）。

SKILL「运行期边界」：运行期唯一合法的 Bash 是 postprocess 命令（初始化 `--prepare`
与阶段 7 报告命名 `--rename-report`）。原判定把 Bash 一律算异常，于是每次运行都把
这两条合法命令报成越界——204221 轮实测 4 次里 2 次是合法的。判据改为直译那条规则。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import analyze_run   # noqa: E402


def _transcript(tmp_path: Path, calls: list[tuple]) -> Path:
    """calls = [(工具名, 入参), ...] —— 一条最小 transcript（只有工具调用）。"""
    p = tmp_path / "t.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for i, (name, inp) in enumerate(calls):
            f.write(json.dumps({
                "type": "assistant", "timestamp": "2026-01-01T00:00:00.000Z",
                "message": {"content": [{"type": "tool_use", "id": f"t{i}",
                                         "name": name, "input": inp}]}},
                ensure_ascii=False) + "\n")
    return p


def _anomaly_text(tmp_path: Path, calls: list[tuple]) -> str:
    out: list[str] = []

    def w(*a):                       # section_* 的行写入器是可变参数的
        out.append(" ".join(str(x) for x in a))

    analyze_run.section_context(_transcript(tmp_path, calls), w)
    text = "\n".join(out)
    return text.split("异常开发工具序列")[-1] if "异常开发工具序列" in text else ""


def test_legal_postprocess_commands_not_reported(tmp_path):
    text = _anomaly_text(tmp_path, [
        ("Bash", {"command": "python .claude/skills/autosource/scripts/postprocess.py "
                             "--prepare"}),
        ("Bash", {"command": "python .claude/skills/autosource/scripts/postprocess.py "
                             "--rename-report \"outputs/x\""}),
    ])
    assert text == ""


def test_other_dev_tools_still_reported(tmp_path):
    text = _anomaly_text(tmp_path, [
        ("Bash", {"command": "ls -la outputs/x"}),
        ("Edit", {"file_path": "outputs/x/分析报告.md"}),
        ("Grep", {"pattern": "verified"}),
    ])
    assert "Bash" in text and "Edit" in text and "Grep" in text
