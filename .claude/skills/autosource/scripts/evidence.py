"""AutoSource 证据链模块：留痕定位、证据校验、切片与 URL 清理（自 postprocess 拆分）。

证据链是流程的安全边界——候选 URL 必须逐字出现在 hook 系统记录的留痕中
（PostToolUse hook 在每次 WebSearch 时由 harness 记录，模型不参与）。
本模块只依赖 Python 标准库，被 postprocess / store / lineage 共享。

设计要点：
- 留痕文件本身无写保护，该机制防的是意外编造（转写错误/凭记忆补 URL），
  不防对抗性篡改。
- 边界匹配：候选 URL 必须作为完整 URL 出现（RFC 3986 字符集判定前后字符），
  截短为父路径或仅域名本身的候选不予通过；`#` fragment 豁免（不改变资源主体）、`?` 保持严格。
"""
import json
import os
import re
from pathlib import Path
from typing import Optional

DEFAULT_EVIDENCE_LOG = "outputs/search_log.jsonl"

# prepare 预留的运行目录：run_{时间戳}（收尾时由脚本重命名为 {领域词}_{时间戳}）。
# 路径由脚本生成、每次运行唯一——连续/并发运行的 raw.json 不会互相覆盖
# （旧固定路径 outputs/raw.json 仍兼容，父目录不匹配本模式时走原逻辑）。
RUN_DIR_RE = re.compile(r"^run_(\d{4}-\d{2}-\d{2}-\d{6})(_\d+)?$")


def default_evidence_log() -> str:
    """默认证据留痕路径：按会话隔离（并行运行互不删除对方留痕）。

    与 evidence_hook.py 的命名规则一致：hook 按 CLAUDE_CODE_SESSION_ID 写
    会话文件，postprocess 读同一会话文件、也只删同一会话文件——并行
    运行的证据链互不干扰（2026-08-26 实证：共享文件被并行运行的
    postprocess 删除，另一运行证据链断裂）。
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session_id:
        return f"outputs/search_log_{session_id}.jsonl"
    return DEFAULT_EVIDENCE_LOG


def run_evidence_log(run_dir: Path) -> Optional[Path]:
    """运行目录内的证据留痕（运行级归属，2026-08-31 分层原则修订）。

    hook 按 .session_id 标记写入 run_*/evidence.jsonl；存在则收尾优先用它，
    否则回退会话级共享路径（旧流程/未 prepare 场景）。仅对 run_ 前缀目录
    生效——旧固定路径（outputs/raw.json）不受影响。
    """
    if not RUN_DIR_RE.match(run_dir.name):
        return None
    p = run_dir / "evidence.jsonl"
    return p if p.exists() else None


# 证据边界匹配的字符集：RFC 3986 的 unreserved + reserved + "%"。
# 候选 URL 必须作为完整 URL 出现在留痕中——匹配的前后相邻字符若属于该集合，
# 说明该匹配只是更长 URL 的前缀（截短为父路径或仅域名本身），拒绝。
# 例外：`#` 是 fragment 分隔符（fragment 不发给服务器、不改变资源主体），
# 候选以 `#` 结尾视为完整资源 URL、判定通过；`?` 是 query 分隔符（会改变内容），不豁免。
URL_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:/?#[]@!$&'()*+,;=%"
)


def _contains_bounded(needle: str, haystack: str, boundary_chars) -> bool:
    """needle 在 haystack 中的出现必须前后不与 boundary_chars 相邻（完整边界匹配）。

    `#` 是 fragment 分隔符（fragment 不改变资源主体）：needle 以 `#` 结尾视为
    完整资源 URL 而非"更长 URL 的前缀"，判定通过；`?` 是 query 分隔符（会改变内容），
    仍按 boundary_chars 严格拒绝。before 侧不豁免。
    """
    if not needle:
        return False
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        before = haystack[start - 1] if start > 0 else ""
        after = haystack[end] if end < len(haystack) else ""
        after_ok = after not in boundary_chars or after == "#"
        if before not in boundary_chars and after_ok:
            return True
        start = haystack.find(needle, end)
    return False


def check_grounded(sources: list[dict], evidence: str) -> tuple[list[dict], list[dict]]:
    """证据校验：URL 必须作为完整 URL 出现在证据留痕中。返回 (通过, 未通过)。

    按 RFC 3986 字符集做边界匹配，截短为父路径或仅域名本身的候选不予通过。防的是意外
    编造（转写错误/凭记忆补 URL）；留痕文件本身无写保护，不防对抗性篡改。
    """
    kept: list[dict] = []
    rejected: list[dict] = []
    for s in sources:
        if _contains_bounded(str(s.get("url") or ""), evidence, URL_CHARS):
            kept.append(s)
        else:
            rejected.append(s)
    return kept, rejected


def _collect_strings(node, out: set) -> None:
    """递归收集 JSON 载荷里的全部字符串值。"""
    if isinstance(node, str):
        out.add(node)
    elif isinstance(node, dict):
        for value in node.values():
            _collect_strings(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_strings(value, out)


def extract_strings(evidence: str) -> set:
    """解析证据留痕 JSONL，收集全部字符串值（供查询词精确比对）。"""
    strings: set = set()
    for line in evidence.splitlines():
        try:
            _collect_strings(json.loads(line), strings)
        except ValueError:
            continue  # 留痕应逐行有效 JSON，容错跳过损坏行
    return strings


def query_in_evidence(query: str, evidence: str) -> bool:
    """查询词必须作为完整 JSON 字符串值出现在留痕中（精确相等，截短/改写不算）。"""
    return bool(query) and query in extract_strings(evidence)


def _line_query(payload: dict) -> str:
    """取留痕行的查询词：tool_input.query，缺省时取 tool_response.query。"""
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict) and tool_input.get("query"):
        return str(tool_input["query"])
    tool_response = payload.get("tool_response")
    if isinstance(tool_response, dict) and tool_response.get("query"):
        return str(tool_response["query"])
    return ""


def _result_urls(payload: dict) -> list[str]:
    """提取一次搜索的结构化结果 URL（兼容 results[].url 与 results[].content[].url）。"""
    urls: list[str] = []
    results = payload.get("tool_response", {}).get("results")
    if not isinstance(results, list):
        return urls
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("url"):
            urls.append(str(item["url"]))
        content = item.get("content")
        if isinstance(content, list):
            for entry in content:
                if isinstance(entry, dict) and entry.get("url"):
                    urls.append(str(entry["url"]))
    return urls


def slice_evidence(evidence: str, queries: set[str]) -> tuple[str, int, int]:
    """按本运行查询词集合切片证据留痕（会话级 → 运行级）。

    只保留 tool_input.query（缺省时取 tool_response.query）命中本运行查询词
    集合的行；损坏行跳过计数。返回 (切片文本, 保留行数, 跳过行数)。
    journal 缺行则对应搜索不进切片（如实标注，见 02 日志）。
    """
    kept: list[str] = []
    skipped = 0
    for line in evidence.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        query = _line_query(payload)
        if query and query in queries:
            kept.append(line)
    return ("\n".join(kept) + "\n" if kept else ""), len(kept), skipped


# 引用序号锚点：#N（纯数字 fragment），WebSearch 结果以 markdown 引用格式渲染
# （[标题](url#N)）时带入的记号。fragment 不发给服务器、不改变资源指向，
# 纯数字锚点是引用记号而非页面锚点——输出前剥离；单词锚点（#content）保留。
CITATION_ANCHOR_RE = re.compile(r"(#\d+)+$")


def strip_citation_anchors(url: str) -> str:
    """去掉 URL 尾部的引用序号锚点（如 ...pdf#3#1 → ...pdf）。"""
    return CITATION_ANCHOR_RE.sub("", url)
