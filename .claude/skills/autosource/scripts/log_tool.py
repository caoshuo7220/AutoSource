"""PostToolUse hook 脚本：证据留痕。

由 Claude Code 的 PostToolUse hook 在每次 WebSearch 调用完成后自动执行
（settings.json 中配置），把工具的完整输入与原始返回结果追加到证据留痕文件。

设计要点：
- 证据留痕由 harness（系统）记录，不由模型记录——模型无法选择记录什么；
  但落盘文件本身无写保护，该机制防的是意外编造（转写错误/凭记忆补 URL），
  不防对抗性篡改。
- 本脚本任何失败都不阻断工具调用（日志失败静默放行），hook 始终退出 0。
- postprocess.py 的 grounded 校验依赖本留痕：候选 URL 必须能在留痕中
  作为完整 URL 找到（边界匹配）。

用法（hook 配置中）:
    python .claude/skills/autosource/scripts/log_tool.py
    留痕路径由脚本按会话自动命名（CLAUDE_CODE_SESSION_ID 环境变量），
    并行运行各写各的会话文件、各删各的——互不销毁对方证据。
"""

import json
import os
import sys
from pathlib import Path


def default_log_path() -> Path:
    """留痕默认路径：按会话隔离（并行运行互不删除对方留痕）。

    hook 子进程继承 CLAUDE_CODE_SESSION_ID 环境变量；无该变量时回退到
    共享旧路径（兼容手动调用/测试）。
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session_id:
        return Path("outputs") / f"search_log_{session_id}.jsonl"
    return Path("outputs/search_log.jsonl")


def main() -> None:
    # 读取 hook 通过 stdin 传入的 JSON（含 tool_name / tool_input / tool_response）
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return

    log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_log_path()

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 留痕失败不阻断工具调用


if __name__ == "__main__":
    main()
