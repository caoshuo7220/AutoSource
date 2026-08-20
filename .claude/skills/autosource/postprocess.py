"""AutoSource 后处理流水线：证据校验 → 清单并入 → 去重 → 导出 CSV → 效果统计 → 清理。

分工原则：LLM（编排层）只负责语义环节，唯一产出的中间产物是 raw.json；
本脚本保证其余所有确定性环节：

- 证据校验（grounded check）：候选 URL 必须作为完整 URL 出现在证据留痕中（留痕由
  PostToolUse hook 在每次 WebSearch 时由系统自动记录，记录过程在
  harness 侧、模型不参与），否则拒绝该条并计数——从结构上杜绝模型**意外**编造
  URL（转写错误、凭记忆补写；边界匹配，截短为父路径/裸域名不放行）。
  留痕文件本身无写保护，该机制不防对抗性篡改。
  被拒条目不进入清单（明细打印在 stdout、计数见 stats 证据校验移除列），
  不影响其余产出，运行到此结束（无修正重跑环节）
- 粒度声明归一化与计数（不拒绝）：granularity 缺失或非法视为合集级，
  单篇级计数进 stats——产量优先，粒度/子站问题由后续子站合并功能处理
- 知识清单：verified=true 且字段齐全的清单项自动并入 sources（LLM 不手工复制）；
  计算清单验证率（自洽性指标），未验证清单进 stdout 报告
- 搜索日志：journal 的每个查询词必须作为完整 JSON 字符串值精确出现在证据留痕中
  （截短/改写即标注"证据缺失"），生成 搜索日志.csv 供人工复盘
- 输出目录与时间戳由脚本生成（模型没有时钟，禁止模型编造）
- 域名 + 名称联合去重
- 用 csv 标准库导出数据源清单（UTF-8 BOM，转义交给标准库）；写入时剥离 URL
  尾部的引用序号锚点（#数字，markdown 引用记号；单词锚点保留）
- 计算各节点候选数与体裁分布，写 stats CSV（含空节点检测、清单验证列）
- 数据血缘：从切片留痕与最终收录 join 生成 溯源.csv（每行一条搜索结果，
  正查"返回了什么、收录了哪几条"、反查"出自哪个搜索词"），数据源清单加
  "来源搜索"列（首次出现查询词）——行级溯源全部确定性推导，LLM 零新增职责
- 按 run bundle 结构归档：交付物在运行目录根（数据源清单/stats/搜索日志/溯源），
  `intermediate/` 子目录放输入快照与**本运行切片**后的证据留痕，`manifest.json`
  记录每个文件的用途/生成方/sha256（不可变性与可审计性）；随后删除会话临时文件

用法:
    python postprocess.py <raw.json> [--evidence-log PATH] [--out-dir DIR] [--keep-raw]

raw.json 结构:
    {
      "domain": "领域词（用于目录命名）",
      "nodes": ["全部叶子节点"],
      "model": "模型名（可选，写入 stats CSV 供溯源）",
      "knowledge": [
        {"name": ..., "node": 叶子节点, "verified": true,
         "category_path": "完整层级路径", "source_type": ..., "url": ...,
         "description": ..., "reason": ...},
        {"name": ..., "node": ..., "verified": false, "note": "疑似无效机构|已尽力"}
      ],
      "journal": [
        {"phase": "验证搜索", "node": ..., "query": "查询词原文",
         "results": 返回链接数, "extracted": 提取候选数}
      ],
      "sources": [  # 仅增量发现条目
        {"name": ..., "category_path": "层级1-层级2-叶子节点",
         "source_type": ..., "url": ..., "description": ..., "reason": ...}
      ]
    }
"""

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

SOURCE_CSV_HEADER = ["数据源名称", "分类路径", "数据源类型", "粒度", "访问地址", "简要说明", "来源搜索"]
LINEAGE_CSV_HEADER = ["阶段", "分类节点", "查询词", "返回结果数", "结果URL", "是否收录",
                      "收录条目名称", "收录理由", "备注"]
STATS_CSV_HEADER = ["领域", "时间戳", "分类节点", "候选数", "体裁分布", "无结果节点",
                    "总候选数", "去重移除", "证据校验移除", "清单验证",
                    "最终收录", "单篇级收录", "模型", "脚本处理耗时(秒)"]
JOURNAL_CSV_HEADER = ["阶段", "节点", "查询词", "返回链接数", "提取候选数", "证据缺失"]

DEFAULT_EVIDENCE_LOG = "outputs/search_log.jsonl"


def _domain(url: str) -> str:
    return urlparse(url).netloc


def deduplicate(sources: list[dict]) -> list[dict]:
    """按域名 + 名称去重，保留首次出现，镜像站（不同域名）保留。"""
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for s in sources:
        key = (_domain(s["url"]), s["name"])
        if key not in seen:
            seen.add(key)
            result.append(s)
    return result


def sanitize_domain(domain: str) -> str:
    """领域词 → 目录名安全前缀：去掉路径非法字符与空白，限长。"""
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", str(domain)).strip("_")
    return cleaned[:30] or "未命名领域"


def leaf_node(category_path: str, nodes: list[str]) -> Optional[str]:
    """取 category_path 对应的叶子节点：节点名的最长后缀匹配（节点名本身可含 -）。"""
    matches = [n for n in nodes if category_path == n or category_path.endswith("-" + n)]
    return max(matches, key=len) if matches else None


# 证据边界匹配的字符集：RFC 3986 的 unreserved + reserved + "%"。
# 候选 URL 必须作为完整 URL 出现在留痕中——匹配的前后相邻字符若属于该集合，
# 说明该匹配只是更长 URL 的前缀（截短为父路径/裸域名），拒绝。
URL_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~:/?#[]@!$&'()*+,;=%"
)


def _contains_bounded(needle: str, haystack: str, boundary_chars) -> bool:
    """needle 在 haystack 中的出现必须前后不与 boundary_chars 相邻（完整边界匹配）。"""
    if not needle:
        return False
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        before = haystack[start - 1] if start > 0 else ""
        after = haystack[end] if end < len(haystack) else ""
        if before not in boundary_chars and after not in boundary_chars:
            return True
        start = haystack.find(needle, end)
    return False


def check_grounded(sources: list[dict], evidence: str) -> tuple[list[dict], list[dict]]:
    """证据校验：URL 必须作为完整 URL 出现在证据留痕中。返回 (通过, 被拒)。

    旧实现为子串匹配，URL 截短为任意父路径/裸域名可通过；现按 RFC 3986 字符集
    做边界匹配，截短即拒绝。防的是意外编造（转写错误/凭记忆补 URL）；
    留痕文件本身无写保护，不防对抗性篡改。
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
    journal 缺行则对应搜索不进切片（如实标注，见 04 手册）。
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


def sha256_file(path: Path) -> str:
    """计算文件 sha256（分块读取，供运行清单校验和）。"""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def first_query_by_source(kept: list[dict], sliced_evidence: str) -> dict[str, str]:
    """每个收录源 URL（剥引用锚点后）→ 在切片留痕中首次出现行的查询词。

    首次出现 ≈ 发现时刻：验证搜索的首次出现即其定向验证查询，
    增量发现的首次出现即撞见它的那次搜索。多出处完整真相见溯源表。
    """
    wanted = {strip_citation_anchors(str(s.get("url") or "")) for s in kept}
    wanted.discard("")
    result: dict[str, str] = {}
    for line in sliced_evidence.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        query = _line_query(payload)
        for stripped in list(wanted):
            if _contains_bounded(stripped, line, URL_CHARS):
                result[stripped] = query
                wanted.remove(stripped)
    return result


def build_lineage(kept: list[dict], sliced_evidence: str,
                  journal_map: dict[str, tuple]) -> list[list]:
    """生成数据血缘行：每行 = 一次搜索的一条结构化结果。

    journal_map: query → (阶段, 节点, 返回结果数)（journal 首次匹配）。
    正查（按查询词过滤看"返回了什么、收录了哪几条"）与反查（按条目名称
    过滤看"出自哪个搜索词"）都由本表承载；收录理由从 kept 源带入；
    仅出现在摘要文本的收录 URL 走回退行（备注"摘要文本提取"）。
    未收录结果无排除理由（提取时未记录，已知边界）。
    """
    collected: dict[str, tuple[str, str]] = {}
    for s in kept:
        stripped = strip_citation_anchors(str(s.get("url") or ""))
        if stripped and stripped not in collected:
            collected[stripped] = (str(s.get("name") or ""), str(s.get("reason") or ""))

    rows: list[list] = []
    covered: set[str] = set()
    for line in sliced_evidence.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        query = _line_query(payload)
        phase, node, results_count = journal_map.get(query, ("", "", ""))
        for url in _result_urls(payload):
            stripped = strip_citation_anchors(url)
            name, reason = collected.get(stripped, ("", ""))
            rows.append([phase, node, query, results_count, stripped,
                         "是" if name else "否", name, reason, ""])
            if name:
                covered.add(stripped)
    # 回退：收录了但不在任何结构化结果数组中的 URL（仅出现在摘要文本）
    for stripped, (name, reason) in collected.items():
        if stripped in covered:
            continue
        for line in sliced_evidence.splitlines():
            if not _contains_bounded(stripped, line, URL_CHARS):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            query = _line_query(payload)
            phase, node, results_count = journal_map.get(query, ("", "", ""))
            rows.append([phase, node, query, "", stripped, "是", name, reason,
                         "摘要文本提取"])
            break
    return rows


def write_lineage_csv(path: Path, rows: list[list]) -> None:
    """写数据血缘 CSV：搜索结果与收录的对应关系（正查/反查）。"""
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(LINEAGE_CSV_HEADER)
        writer.writerows(rows)


def write_manifest(path: Path, run_meta: dict, entries: list[tuple[str, str, str]]) -> None:
    """写运行清单 manifest.json：每个文件的用途/生成方/sha256（校验和可脚本验证）。

    entries = [(相对路径, 用途, 生成方)]；manifest 自身在写完其他文件后生成，
    不列入清单。
    """
    files = [
        {"path": rel_path, "purpose": purpose, "producer": producer,
         "sha256": sha256_file(path.parent / rel_path)}
        for rel_path, purpose, producer in entries
    ]
    path.write_text(
        json.dumps({"run": run_meta, "files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8")


# 文档形态 URL 的确定性信号已按用户决策移除（2026-08-20：规则二删除，产量优先，
# 粒度/子站问题由后续"站点与子站合并"功能处理）。granularity 仅归一化与计数。
GRANULARITY_LEVELS = ("合集级", "站点级", "单篇级")


# 引用序号锚点：#N（纯数字 fragment），WebSearch 结果以 markdown 引用格式渲染
# （[标题](url#N)）时带入的记号。fragment 不发给服务器、不改变资源指向，
# 纯数字锚点是引用记号而非页面锚点——输出前剥离；单词锚点（#content）保留。
CITATION_ANCHOR_RE = re.compile(r"(#\d+)+$")


def strip_citation_anchors(url: str) -> str:
    """去掉 URL 尾部的引用序号锚点（如 ...pdf#3#1 → ...pdf）。"""
    return CITATION_ANCHOR_RE.sub("", url)


def check_granularity(sources: list[dict]) -> tuple[int, int]:
    """粒度声明归一化与计数（不拒绝——产量优先，粒度问题由后续子站合并功能处理）。

    返回 (单篇级数, 缺声明数)：granularity 缺失或非法视为合集级（P-002 默认）。
    """
    single_count = 0
    missing_count = 0
    for s in sources:
        g = str(s.get("granularity") or "")
        if g not in GRANULARITY_LEVELS:
            missing_count += 1
            g = "合集级"
        s["granularity"] = g
        if g == "单篇级":
            single_count += 1
    return single_count, missing_count


def merge_knowledge(knowledge: list[dict]) -> tuple[list[dict], int, int]:
    """把 verified=true 且字段齐全的清单项转为 sources 条目。

    返回 (merged, list_total, incomplete)：
    - merged：可并入 sources 的条目（name/url 缺一即跳过）
    - list_total：清单总条数（仅统计 dict 条目，混入的垃圾不计入分母）
    - incomplete：verified=true 但缺 name/url 的条数（告警用）
    """
    merged: list[dict] = []
    incomplete = 0
    list_total = sum(1 for k in knowledge if isinstance(k, dict))
    for item in knowledge:
        if not isinstance(item, dict) or not item.get("verified"):
            continue
        if not item.get("name") or not item.get("url"):
            incomplete += 1
            continue
        merged.append({
            "name": item["name"],
            "category_path": item.get("category_path") or item.get("node", ""),
            "source_type": item.get("source_type", ""),
            "granularity": item.get("granularity") or "合集级",
            "url": item["url"],
            "description": item.get("description", ""),
            "reason": item.get("reason", ""),
        })
    return merged, list_total, incomplete


def write_source_csv(path: Path, sources: list[dict],
                     first_query: Optional[dict[str, str]] = None) -> None:
    """写数据源清单 CSV：UTF-8 BOM（utf-8-sig），转义由 csv 标准库保证。

    写入时剥离 URL 尾部的引用序号锚点（#数字）：证据链校验在前（严格逐字），
    清理在后且不改变资源指向——输出干净、防线不动。
    "来源搜索"列 = 该 URL 在留痕中首次出现的查询词（发现时刻溯源），
    多出处完整真相见溯源.csv。
    """
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(SOURCE_CSV_HEADER)
        for s in sources:
            url = strip_citation_anchors(str(s.get("url") or ""))
            writer.writerow([
                s.get("name", ""),
                s.get("category_path", ""),
                s.get("source_type", ""),
                s.get("granularity", ""),
                url,
                s.get("description", ""),
                (first_query or {}).get(url, ""),
            ])


def write_stats_csv(path: Path, summary: dict) -> None:
    """写效果统计 CSV：每行一个分类节点，体裁分布按数量降序合并为单列。"""
    empty_nodes = "、".join(summary["empty_nodes"])
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(STATS_CSV_HEADER)
        for node, info in summary["per_node"].items():
            dist = "; ".join(
                f"{t}:{c}"
                for t, c in sorted(info["types"].items(), key=lambda kv: (-kv[1], kv[0]))
            )
            writer.writerow([
                summary["domain"],
                summary["timestamp_display"],
                node,
                info["count"],
                dist,
                empty_nodes,
                summary["total_found"],
                summary["removed_duplicates"],
                summary["ungrounded"],
                summary["list_verified"],
                summary["kept"],
                summary["single_count"],
                summary["model"],
                summary["elapsed_seconds"],
            ])


def write_journal_csv(path: Path, journal_rows: list[list]) -> None:
    """写搜索日志 CSV：每行一次搜索，证据缺失列标注查询词不在留痕中的行。"""
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(JOURNAL_CSV_HEADER)
        writer.writerows(journal_rows)


def compute_stats(kept: list[dict], nodes: list[str]) -> dict:
    """按去重后的源计算各节点候选数与体裁分布；empty_nodes = 零候选的节点。"""
    per_node = {n: {"count": 0, "types": {}} for n in nodes}
    unmatched = 0
    for s in kept:
        node = leaf_node(s.get("category_path", ""), nodes)
        if node is None:
            unmatched += 1
            continue
        per_node[node]["count"] += 1
        source_type = s.get("source_type") or "未标注"
        per_node[node]["types"][source_type] = per_node[node]["types"].get(source_type, 0) + 1
    empty_nodes = [n for n in nodes if per_node[n]["count"] == 0]
    return {"per_node": per_node, "empty_nodes": empty_nodes, "unmatched": unmatched}


def run(raw_path: str, out_dir: str = "outputs", keep_raw: bool = False,
        evidence_log: Optional[str] = None,
        now: Optional[datetime] = None) -> dict:
    """执行完整后处理流水线，返回汇总统计（供 stdout 展示与 stats CSV）。"""
    start = time.perf_counter()
    raw = Path(raw_path)
    if not raw.exists():
        raise FileNotFoundError(f"输入文件不存在: {raw_path}")

    data = json.loads(raw.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "sources" not in data:
        raise ValueError('raw.json 结构错误：应为 {"domain", "nodes", "sources", ...} 对象')
    if not isinstance(data.get("nodes"), list):
        raise ValueError("raw.json 缺少 nodes 字段（叶子节点列表，用于空节点检测与 stats）")

    domain = sanitize_domain(data.get("domain", ""))
    model = str(data.get("model") or "")
    nodes = [str(n) for n in data["nodes"]]

    # 脏数据容错：sources 非 list 视为空并告警；name/url 缺一即跳过并计数
    sources_raw = data["sources"]
    sources_broken = not isinstance(sources_raw, list)
    if sources_broken:
        sources_raw = []
    valid: list[dict] = []
    invalid = 0
    for s in sources_raw:
        if not isinstance(s, dict) or not s.get("name") or not s.get("url"):
            invalid += 1
            continue
        valid.append(s)

    # 知识清单：verified 项自动并入 sources（LLM 不手工复制，消除一致性风险）
    knowledge = data.get("knowledge") if isinstance(data.get("knowledge"), list) else []
    merged, list_total, incomplete = merge_knowledge(knowledge)
    list_verified = f"{len(merged)}/{list_total}" if knowledge else "0/0"

    # journal：每次搜索一条，查询词需在证据留痕中逐字出现
    journal = data.get("journal") if isinstance(data.get("journal"), list) else []

    # 证据校验：候选 URL 必须逐字出现在证据留痕中（PostToolUse hook 系统记录）
    log_path = Path(evidence_log) if evidence_log else Path(DEFAULT_EVIDENCE_LOG)
    if (valid or merged or journal) and not log_path.exists():
        raise FileNotFoundError(
            f"证据留痕不存在: {log_path}（PostToolUse hook 未启用或未生效？"
            "没有证据链就不放行候选，这是设计使然）")
    evidence = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    # 查询词精确比对用：解析留痕收集全部 JSON 字符串值（一次解析，逐行复用）
    evidence_strings = extract_strings(evidence) if journal else set()

    all_candidates = valid + merged
    grounded, rejected = check_grounded(all_candidates, evidence)
    ungrounded = len(rejected)

    # 粒度声明归一化与计数（不拒绝）
    single_count, granularity_missing = check_granularity(grounded)

    kept = deduplicate(grounded)
    removed = len(grounded) - len(kept)
    node_stats = compute_stats(kept, nodes)

    journal_rows: list[list] = []
    journal_skipped = 0
    journal_queries: set[str] = set()
    journal_map: dict[str, tuple] = {}
    for j in journal:
        if not isinstance(j, dict):
            journal_skipped += 1
            continue
        query = str(j.get("query") or "")
        if query:
            journal_queries.add(query)
            journal_map.setdefault(query, (
                str(j.get("phase") or ""), str(j.get("node") or ""), j.get("results", "")))
        missing = "是" if (query and query not in evidence_strings) else "否"
        journal_rows.append([
            j.get("phase", ""),
            j.get("node", ""),
            query,
            j.get("results", ""),
            j.get("extracted", ""),
            missing,
        ])

    # 证据留痕切片（会话级 → 运行级）——血缘表与"来源搜索"列的归因基础
    sliced, slice_kept, slice_skipped = slice_evidence(evidence, journal_queries)

    unverified = [
        (str(item.get("name") or "未命名"), str(item.get("note") or "未说明"))
        for item in knowledge
        if isinstance(item, dict) and not item.get("verified")
    ]

    # 失败路径：搜索过（journal 非空）却 0 候选 → 疑似搜索工具异常，
    # 中止并保留 raw.json 供人工检查（方案 02-搜索方案.md 失败路径）
    if len(all_candidates) == 0 and journal_rows:
        raise ValueError(
            "搜索过（journal 非空）但候选为 0——疑似搜索工具异常，"
            "按方案中止处理；raw.json 已保留供人工检查")

    now = now or datetime.now()
    timestamp = now.strftime("%Y-%m-%d-%H%M%S")

    summary = {
        "domain": domain,
        "timestamp": timestamp,
        "timestamp_display": now.strftime("%Y-%m-%d %H:%M:%S"),
        "total_found": len(all_candidates),
        "removed_duplicates": removed,
        "ungrounded": ungrounded,
        "kept": len(kept),
        "invalid": invalid,
        "unmatched": node_stats["unmatched"],
        "empty_nodes": node_stats["empty_nodes"],
        "per_node": node_stats["per_node"],
        "outdir": "",
        "model": model,
        "elapsed_seconds": round(time.perf_counter() - start, 1),
        "list_verified": list_verified,
        "knowledge_missing": not knowledge,
        "incomplete": incomplete,
        "unverified": unverified,
        "rejected": [(str(s.get("name") or "未命名"), str(s.get("url") or "")) for s in rejected],
        "single_count": single_count,
        "granularity_missing": granularity_missing,
        "journal_count": len(journal_rows),
        "journal_skipped": journal_skipped,
        "sources_broken": sources_broken,
    }

    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    outdir = base / f"{domain}_{timestamp}"
    counter = 1
    while outdir.exists():
        outdir = base / f"{domain}_{timestamp}_{counter}"
        counter += 1
    outdir.mkdir(parents=True)

    write_source_csv(outdir / f"{domain}_{timestamp}_数据源清单.csv", kept,
                     first_query_by_source(kept, sliced))
    if journal_rows:
        write_journal_csv(outdir / f"{domain}_{timestamp}_搜索日志.csv", journal_rows)
    summary["outdir"] = str(outdir)
    write_stats_csv(outdir / f"{domain}_{timestamp}_stats.csv", summary)

    # run bundle 归档（数据工程惯例）：交付物在根，中间产物入 intermediate/，
    # manifest.json 记录每个文件的用途/生成方/sha256。
    intermediate = outdir / "intermediate"
    intermediate.mkdir()
    shutil.copy(raw, intermediate / "raw_input.json")
    manifest_entries = [
        (f"{domain}_{timestamp}_数据源清单.csv", "交付物：数据源清单", "postprocess.py"),
        (f"{domain}_{timestamp}_stats.csv", "效果统计", "postprocess.py"),
        ("intermediate/raw_input.json", "输入快照（LLM 唯一中间产物，过滤前全量）", "编排层"),
    ]
    if journal_rows:
        manifest_entries.append(
            (f"{domain}_{timestamp}_搜索日志.csv", "搜索日志（人工复盘）", "postprocess.py"))
    if sliced:
        (intermediate / "evidence_log.jsonl").write_text(sliced, encoding="utf-8")
        manifest_entries.append(
            ("intermediate/evidence_log.jsonl", "证据留痕（本运行切片）", "PostToolUse hook"))
    lineage_rows = build_lineage(kept, sliced, journal_map) if sliced else []
    if lineage_rows:
        write_lineage_csv(outdir / f"{domain}_{timestamp}_溯源.csv", lineage_rows)
        manifest_entries.append(
            (f"{domain}_{timestamp}_溯源.csv",
             "数据血缘：搜索结果与收录对应（正查/反查）", "postprocess.py"))
    summary["lineage_rows"] = len(lineage_rows)
    summary["evidence_slice_kept"] = slice_kept
    summary["evidence_slice_skipped"] = slice_skipped
    run_meta = {
        "domain": domain,
        "timestamp": summary["timestamp_display"],
        "model": model,
        "pipeline": "postprocess.py",
    }
    write_manifest(outdir / "manifest.json", run_meta, manifest_entries)
    summary["manifest_count"] = len(manifest_entries)

    # 删除会话临时文件（--keep-raw 时保留；无修正重跑环节，运行到此结束）
    if not keep_raw:
        raw.unlink(missing_ok=True)
        if log_path.exists():
            log_path.unlink(missing_ok=True)

    return summary


def _print_summary(summary: dict) -> None:
    print(f"领域: {summary['domain']}")
    print(f"输出目录: {summary['outdir']}")
    print(f"候选总数: {summary['total_found']}  去重移除: {summary['removed_duplicates']}"
          f"  证据校验移除: {summary['ungrounded']}"
          f"  最终收录: {summary['kept']}")
    if summary["invalid"]:
        print(f"无效记录(缺名称/URL): {summary['invalid']}")
    if summary["unmatched"]:
        print(f"警告: {summary['unmatched']} 条记录的分类路径未匹配到任何节点")
    if summary["rejected"]:
        print("证据校验移除明细（网址不在搜索结果留痕中，未进入清单）:")
        for name, url in summary["rejected"]:
            print(f"  - {name}: {url}")
    if summary["granularity_missing"]:
        print(f"警告: {summary['granularity_missing']} 条缺 granularity 声明，按合集级处理")
    if summary["single_count"]:
        print(f"单篇级收录: {summary['single_count']} 条（见 stats.csv）")
    print(f"清单核对: 验证通过 {summary['list_verified']} 项")
    if summary["knowledge_missing"]:
        print("警告: raw.json 无 knowledge 字段（本次无权威源清单，退化为纯增量模式，**本次无底线保证**）")
    if summary["incomplete"]:
        print(f"警告: {summary['incomplete']} 条 verified 清单项缺 name/url，未并入")
    if summary["sources_broken"]:
        print("警告: sources 字段不是列表，已按空处理")
    if summary["journal_skipped"]:
        print(f"警告: {summary['journal_skipped']} 条 journal 记录结构损坏被跳过")
    if summary["unverified"]:
        print("未验证清单（人工交接单）:")
        for name, note in summary["unverified"]:
            print(f"  - {name}（{note}）")
    else:
        print("未验证清单: 无")
    if summary["journal_count"]:
        print(f"搜索日志: {summary['journal_count']} 次搜索（见搜索日志.csv）")
    if summary["outdir"]:
        print(f"运行清单: manifest.json（{summary['manifest_count']} 个文件，含 sha256 校验和）")
        if summary.get("lineage_rows"):
            print(f"数据血缘: 溯源.csv（{summary['lineage_rows']} 行，正查/反查见表格）")
        print(f"证据留痕切片: 保留 {summary['evidence_slice_kept']} 行 / 跳过 {summary['evidence_slice_skipped']} 行"
              f"（见 intermediate/）")
    print("各节点:")
    for node, info in summary["per_node"].items():
        dist = "; ".join(
            f"{t}:{c}"
            for t, c in sorted(info["types"].items(), key=lambda kv: (-kv[1], kv[0]))
        )
        suffix = f" ({dist})" if dist else ""
        print(f"  {node}: {info['count']} 条{suffix}")
    empty = summary["empty_nodes"]
    print(f"无结果节点: {'、'.join(empty) if empty else '无'}")
    print(f"脚本处理耗时: {summary['elapsed_seconds']} 秒")


def main() -> None:
    # Windows 控制台默认 GBK，stdout 中文会乱码——统一转 UTF-8（失败则保持默认）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="AutoSource 后处理流水线")
    parser.add_argument("raw_json", help="编排层产出的 raw.json 路径")
    parser.add_argument("--evidence-log", default=DEFAULT_EVIDENCE_LOG,
                        help="证据留痕文件（PostToolUse hook 自动记录，默认 outputs/search_log.jsonl）")
    parser.add_argument("--out-dir", default="outputs", help="输出根目录（默认 outputs）")
    parser.add_argument("--keep-raw", action="store_true", help="保留 raw.json 与证据留痕不删除")
    args = parser.parse_args()

    try:
        summary = run(args.raw_json, out_dir=args.out_dir, keep_raw=args.keep_raw,
                      evidence_log=args.evidence_log)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    _print_summary(summary)


if __name__ == "__main__":
    main()
