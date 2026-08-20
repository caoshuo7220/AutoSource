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
    python .claude/skills/autosource/log_tool.py outputs/search_log.jsonl
"""

import json
import sys
from pathlib import Path


def main() -> None:
    # 读取 hook 通过 stdin 传入的 JSON（含 tool_name / tool_input / tool_response）
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return

    log_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("outputs/search_log.jsonl")

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 留痕失败不阻断工具调用


if __name__ == "__main__":
    main()
