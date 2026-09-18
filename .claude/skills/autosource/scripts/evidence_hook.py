"""PostToolUse hook 脚本：证据留痕。

由 Claude Code 的 PostToolUse hook 在每次 WebSearch 调用完成后自动执行
（settings.json 中配置），把工具的完整输入与原始返回结果追加到证据留痕文件。

设计要点：
- 证据留痕由 harness（系统）记录，不由模型记录——模型无法选择记录什么；
  但落盘文件本身无写保护，该机制防的是意外编造（转写错误/凭记忆补 URL），
  不防对抗性篡改。
- 本脚本任何失败都不阻断工具调用（日志失败静默放行），hook 始终退出 0。
- 证据校验（evidence.py）依赖本留痕：候选 URL 必须能在留痕中
  作为完整 URL 找到（边界匹配）——本脚本写、evidence.py 读。

用法（hook 配置中）:
    python .claude/skills/autosource/scripts/evidence_hook.py
    留痕按运行目录归属（2026-08-31 分层原则修订）：--prepare 预留运行目录时
    写入 .session_id 会话标记，本脚本按标记找到本会话的运行目录、写
    run_*/evidence.jsonl（outputs/ 顶层不再平铺会话级留痕文件）；
    无匹配运行目录（未 prepare / 旧流程）时回退按会话命名的共享路径。
"""

import json
import os
import sys
from pathlib import Path

from evidence import default_evidence_log, project_root  # 留痕路径与项目根：读写两端共用的单一事实源


def run_scoped_log_path() -> Path | None:
    """按会话标记定位本会话运行目录内的留痕：outputs/run_*/.session_id == 会话 ID。

    多个匹配时取最新目录（同会话多轮的边角，取最近一次 --prepare）；
    无匹配返回 None（调用方回退 default_evidence_log）。**路径锚定项目根、
    不相对 cwd**（2026-09-18 实证：cwd 漂移会让留痕写到漂移目录下）。
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return None
    candidates = []
    for marker in (project_root() / "outputs").glob("run_*/.session_id"):
        try:
            if marker.read_text(encoding="utf-8").strip() == session_id:
                candidates.append(marker.parent)
        except OSError:
            continue
    if not candidates:
        return None
    newest = max(candidates, key=lambda d: d.stat().st_mtime)
    return newest / "evidence.jsonl"


def main() -> None:
    # 读取 hook 通过 stdin 传入的 JSON（含 tool_name / tool_input / tool_response）
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return

    if len(sys.argv) > 1:
        log_path = Path(sys.argv[1])
    else:
        log_path = run_scoped_log_path() or Path(default_evidence_log())

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 留痕失败不阻断工具调用


if __name__ == "__main__":
    main()
