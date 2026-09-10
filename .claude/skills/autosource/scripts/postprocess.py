"""AutoSource 后处理流水线：证据终检 → 清单并入 → 去重 → 导出 CSV → 统计 → 清理。

分工原则：LLM 只做语义判断（拆解/清单/搜索/提取），一切确定性环节由脚本保证。
当前契约（docs/04 存储架构改造，2026-08-31 验收）：
- 数据落盘走 MCP 四工具（store.jsonl，见 store.py），元数据走 manifest.json
  （模型 Write 两次：阶段 0-1 声明版、阶段 5 最终核对态）
- 收尾由 finalize 工具调用本模块 fold()——读 store + manifest 组装等价
  raw.json 后复用 run_pipeline() 全链路（逻辑一行不改，只换入口）
- 旧 raw.json CLI 路径保留兼容（历史轮次/排障）

run_pipeline() 全链路：
- 证据终检（evidence.check_grounded）：候选 URL 必须作为完整 URL 出现在证据
  留痕中（PostToolUse hook 系统记录，边界匹配，截短不放行）——结构上杜绝
  意外编造（转写错误/凭记忆补 URL）；留痕无写保护，不防对抗性篡改。
  入库即验由 store.record_sources 承担（当场拒绝、可修正重传）；收尾终检兜底
- 粒度声明归一化与计数（不拒绝——产量优先，粒度/子站问题由后续子站合并处理）
- 知识清单：verified=true 且字段齐全的清单项并入 sources（LLM 不手工复制）；
  清单验证率（自洽性指标）与未验证清单进 stdout
- 域名 + 名称联合去重；CSV 用标准库导出（UTF-8 BOM，转义交给标准库），
  写入时剥离 URL 尾部的引用序号锚点（#数字，markdown 引用记号）
- 各节点候选数与体裁分布写 stats CSV（纯清单统计表：每节点一行 + 总计行）
- 零提取审计与收尾护栏（2026-09-09）：增量/扩量零提取按结果域名分类
  （已收/垃圾域/疑似漏收），enforce_quotas 时拦截配额不足（每节点增量搜索
  ≥16）、拒收无留痕（zero_reason）、理由与域名证据矛盾——fold/finalize 路径
  开启，旧 CLI 兼容路径关闭
- 数据血缘（lineage.py）：从切片留痕与最终收录 join 生成溯源.csv，数据源清单
  加"来源搜索"列（首次出现查询词）——行级溯源全部确定性推导，LLM 零新增职责
- 按 run bundle 结构归档：交付物在运行目录根（数据源清单；分析报告由模型
  收尾时写入），intermediate/ 放排障与对比材料；随后删除会话临时文件

模块结构（2026-09-02 拆分）：evidence.py（证据链与留痕）、lineage.py（血缘与
归因）、report.py（报告注入与命名）、本模块（流水线编排 fold/run + CLI）；
store.py 与 mcp_server.py 按需引用，导入面经本模块重导出保持兼容。

用法:
    python postprocess.py --prepare                    # 流程开始：预留唯一运行目录并打印路径
    python postprocess.py <运行目录>/raw.json ...      # 旧 raw.json 契约（CLI 兼容路径；新契约由 finalize 折叠）
    python postprocess.py --rename-report <输出目录>    # 阶段 6 最后：报告命名（内容由模型写入）
    python postprocess.py outputs/raw.json ...         # 兼容旧固定路径（父目录非 run_ 时走原逻辑）
    可选参数: [--evidence-log PATH] [--out-dir DIR] [--keep-raw]

raw.json 结构（fold 组装等价结构；CLI 兼容路径由调用方提供）:
    {
      "domain": "领域词（用于目录命名）",
      "nodes": ["全部叶子节点"],
      "model": "模型名（可选，fold 路径随 manifest_input.json 归档留存）",
      "knowledge": [
        {"name": ..., "node": 叶子节点, "verified": true,
         "category_path": "完整层级路径", "source_type": ..., "url": ...,
         "description": ..., "reason": ...},
        {"name": ..., "node": ..., "verified": false, "note": "疑似无效机构|未找到官方入口"}
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
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from evidence import (RUN_DIR_RE, check_grounded, default_evidence_log,
                      extract_strings, line_query, query_in_evidence,
                      result_urls, run_evidence_log, slice_evidence,
                      strip_citation_anchors)
from lineage import build_lineage, first_query_by_source, write_lineage_csv
from report import finalize_report
# 条目粒度契约与节点推导归存储层（store），编排层从这里取——依赖方向单向向下
# （mcp_server → store/postprocess → evidence/lineage/report，无环）。
from store import (GRANULARITY_LEVELS, canonicalize_source_type, leaf_node,
                   is_garbage_domain, load_store)

# 重导出（test_postprocess 的导入面）：query_in_evidence 本模块未用，仅作兼容出口。
__all__ = ["AutoSourceError", "check_grounded", "check_granularity", "deduplicate",
           "default_evidence_log", "finalize_report", "fold", "leaf_node",
           "prepare_run_dir", "query_in_evidence", "run_pipeline", "run_evidence_log",
           "sanitize_domain", "slice_evidence", "strip_citation_anchors"]


class AutoSourceError(ValueError):
    """业务规则失败（区别于输入损坏的 ValueError）：搜索过却零候选、防截断哨兵等。

    继承 ValueError 保持既有调用方（CLI 捕获、测试断言）兼容；需要区分业务失败
    与输入错误时可按本类型精确捕获。
    """

SOURCE_CSV_HEADER = ["数据源名称", "分类路径", "数据源类型", "粒度", "访问地址", "简要说明", "来源搜索"]
STATS_CSV_HEADER = ["分类节点", "候选数", "体裁分布"]
JOURNAL_CSV_HEADER = ["阶段", "节点", "查询词", "返回链接数", "提取候选数", "验证通过", "证据缺失", "零提取理由"]

# 哨兵 1（2026-09-09 收尾护栏）：每节点增量发现搜索下限——配额缩水/谎报过不了收尾
MIN_INCREMENTAL_SEARCHES = 16


def prepare_run_dir(base: Path, now: Optional[datetime] = None) -> str:
    """预留本次运行目录（run_{时间戳}/，同秒加 _N 后缀）并返回路径字符串。

    流程开始时调用（--prepare）：目录在流程开头即存在，阶段 0-1 把 manifest.json
    写入其中，收尾时 fold 组装等价 raw.json、脚本读领域词把目录重命名为最终交付
    目录。同时写入 .session_id 会话标记——hook 按标记把证据留痕写进本目录的
    evidence.jsonl（运行级归属，outputs/ 顶层零平铺文件）；收尾时随目录
    清理。无会话 ID 环境变量（手动调用）则不写标记、留痕走旧共享路径。
    """
    now = now or datetime.now()
    timestamp = now.strftime("%Y-%m-%d-%H%M%S")
    run_dir = base / f"run_{timestamp}"
    counter = 1
    while run_dir.exists():
        run_dir = base / f"run_{timestamp}_{counter}"
        counter += 1
    run_dir.mkdir(parents=True)
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session_id:
        (run_dir / ".session_id").write_text(session_id, encoding="utf-8")
    return str(run_dir)


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


def _format_type_dist(types: dict[str, int]) -> str:
    """体裁分布格式化：数量降序（同数按体裁名升序）拼成 "体裁:数" 串。"""
    return "; ".join(
        f"{t}:{c}"
        for t, c in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def write_stats_csv(path: Path, summary: dict) -> None:
    """写清单统计 CSV：每行一个分类节点（候选数/体裁分布），末尾一行总计。

    纯清单统计表——运行级信息不贴行：领域/时间戳在文件名，模型在
    manifest_input.json（fold 路径），过程健康指标（证据校验移除/清单验证等）
    在 stdout 汇总。
    """
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(STATS_CSV_HEADER)
        for node, info in summary["per_node"].items():
            writer.writerow([node, info["count"], _format_type_dist(info["types"])])
        writer.writerow(["总计", summary["kept"], _format_type_dist(summary["total_types"])])


def write_journal_csv(path: Path, journal_rows: list[list]) -> None:
    """写搜索日志 CSV：每行一次搜索，证据缺失列标注查询词不在留痕中的行。"""
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(JOURNAL_CSV_HEADER)
        writer.writerows(journal_rows)


def compute_stats(kept: list[dict], nodes: list[str]) -> dict:
    """按去重后的源计算各节点候选数与体裁分布；empty_nodes = 零候选的节点。

    total_types 为全领域体裁合并分布（含未匹配节点的源），供 stats 总计行。
    """
    per_node = {n: {"count": 0, "types": {}} for n in nodes}
    unmatched = 0
    total_types: dict[str, int] = {}
    for s in kept:
        source_type = s.get("source_type") or "未标注"
        total_types[source_type] = total_types.get(source_type, 0) + 1
        node = leaf_node(s.get("category_path", ""), nodes)
        if node is None:
            unmatched += 1
            continue
        per_node[node]["count"] += 1
        per_node[node]["types"][source_type] = per_node[node]["types"].get(source_type, 0) + 1
    empty_nodes = [n for n in nodes if per_node[n]["count"] == 0]
    return {"per_node": per_node, "empty_nodes": empty_nodes,
            "unmatched": unmatched, "total_types": total_types}


def _phase_group(phase: str) -> str:
    """journal phase 归组（前缀容忍）。

    2026-09-01 实测：phase 是模型自由文本——"验证搜索"曾被缩写为"验证"、
    "增量发现"为"增量"，字面全等匹配导致选题分布 0/0 与 verified 一致性
    警告误报。按前缀归组为 验证/增量/扩量，无法归组返回空串。
    """
    p = str(phase or "")
    for prefix, group in (("验证", "验证"), ("增量", "增量"), ("扩量", "扩量")):
        if p.startswith(prefix):
            return group
    return ""


def classify_query_scope(query: str, domain: str, nodes: list[str]) -> str:
    """选题分类（后验统计）：查询词含领域词或节点名 → 框架内；否则 → 非框架内。

    2026-09-01 实体选题放开后的验证度量。判定键只有一个——领域词/节点名是否
    逐字出现，完全确定性（选题意图是语义判断，脚本只做可确定的二分）。
    已知近似：英文角度词、抽象词查询归非框架内——本度量按趋势读，不按绝对值。
    空查询归框架内（保守侧，无从判定）。
    """
    q = str(query or "")
    if not q:
        return "框架内"
    if domain and domain in q:
        return "框架内"
    for n in nodes:
        if str(n) and str(n) in q:
            return "框架内"
    return "非框架内"


def _zero_extraction_audit(zero_rows: list[tuple], urls_by_query: dict[str, list],
                           kept_domains: set[str]) -> dict:
    """零提取审计（哨兵 2/3 的数据面）：逐条分类结果域名并核对留痕理由。

    zero_rows = [(node, query, zero_reason), ...]（增量/扩量轮提取为 0 的行）。
    返回 {total, categorized, suspects, violations}：
    - categorized：已收/垃圾域/混合/疑似漏收/无留痕URL 计数
    - suspects：疑似漏收行（域名不在清单且非垃圾域）——审计清单，不拦截
    - violations：客观矛盾——缺 zero_reason；理由声称已收但域名不在清单；
      理由声称垃圾域但域名非垃圾域——enforce 时拦截（拒收必须留痕可核）
    """
    categorized: dict[str, int] = {}
    suspects: list[tuple] = []
    violations: list[tuple] = []

    def bump(key: str) -> None:
        categorized[key] = categorized.get(key, 0) + 1

    for node, query, reason in zero_rows:
        domains = {_domain(u) for u in urls_by_query.get(query, [])}
        if not domains:
            bump("无留痕URL")
            if not reason:
                violations.append((node, query, reason, "无 zero_reason"))
            continue
        kept_hits = sum(1 for d in domains if d in kept_domains)
        garbage_hits = sum(1 for d in domains if is_garbage_domain(d))
        if kept_hits == len(domains):
            cat = "已收"
        elif garbage_hits == len(domains):
            cat = "垃圾域"
        elif kept_hits + garbage_hits == len(domains):
            cat = "混合"
        else:
            cat = "疑似漏收"
        bump(cat)
        if not reason:
            violations.append((node, query, reason, "无 zero_reason"))
        elif ("已收" in reason or "重复" in reason) and kept_hits == 0:
            violations.append((node, query, reason,
                               f"理由声称已收/重复，但结果域名 {sorted(domains)} 不在最终清单"))
        elif "垃圾" in reason and garbage_hits == 0:
            violations.append((node, query, reason,
                               f"理由声称垃圾域，但结果域名 {sorted(domains)} 非垃圾域"))
        if cat == "疑似漏收":
            suspects.append((node, query, "; ".join(sorted(domains)), reason))
    return {"total": len(zero_rows), "categorized": categorized,
            "suspects": suspects, "violations": violations}


def _check_node_quota(journal: list, nodes: list[str]) -> None:
    """哨兵 1：每节点增量发现搜索 ≥ MIN_INCREMENTAL_SEARCHES。

    仅对已启动增量发现的运行校验（存在增量行才检查）——纯清单验证运行
    （零增量行）与防截断哨兵的豁免口径一致；扩量轮不计入基底配额。
    """
    counts = {n: 0 for n in nodes}
    for j in journal:
        if not isinstance(j, dict):
            continue
        if _phase_group(str(j.get("phase") or "")) != "增量":
            continue
        node = leaf_node(str(j.get("node") or ""), nodes)
        if node:
            counts[node] += 1
    if sum(counts.values()) == 0:
        return
    short = [(n, counts[n]) for n in nodes if counts[n] < MIN_INCREMENTAL_SEARCHES]
    if short:
        raise AutoSourceError(
            f"节点增量搜索未达标（每节点应 ≥{MIN_INCREMENTAL_SEARCHES} 次）："
            + "；".join(f"{n} {c} 次（缺 {MIN_INCREMENTAL_SEARCHES - c}）" for n, c in short)
            + "——补搜并补录 record_search 后重跑 finalize（运行目录未被重命名）")


def _check_zero_reason_violations(audit: dict) -> None:
    """哨兵 2/3 拦截：零提取拒收必须留痕（zero_reason）且与结果域名证据一致。"""
    violations = audit["violations"]
    if not violations:
        return
    raise AutoSourceError(
        f"零提取留痕缺失或与证据矛盾（{len(violations)} 条）：拒收必须逐条说明理由且"
        "可核对——该收的补 record_sources，确不收的补录 zero_reason（重传同"
        "phase+node+query 行，末次覆盖）后重跑 finalize：\n"
        + "\n".join(f"  - [{node}] {query}（{problem}）"
                    for node, query, _reason, problem in violations))


def run_pipeline(raw_path: str, out_dir: str = "outputs", keep_raw: bool = False,
                 evidence_log: Optional[str] = None,
                 now: Optional[datetime] = None,
                 enforce_quotas: bool = False) -> dict:
    """执行完整后处理流水线，返回汇总统计（供 stdout 展示与 stats CSV）。

    enforce_quotas（2026-09-09 收尾护栏）：fold/finalize 路径开启三哨兵——
    ① 每节点增量搜索 ≥16；② 增量/扩量零提取必须带 zero_reason（拒收留痕）；
    ③ 理由与结果域名证据一致（声称已收须域名在清单、声称垃圾域须命中黑名单）。
    旧 CLI 兼容路径（历史轮次复盘重跑）缺省关闭，不拦。
    """
    raw = Path(raw_path)
    if not raw.exists():
        raise FileNotFoundError(f"输入文件不存在: {raw_path}")

    data = json.loads(raw.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "sources" not in data:
        raise ValueError('raw.json 结构错误：应为 {"domain", "nodes", "sources", ...} 对象')
    if not isinstance(data.get("nodes"), list):
        raise ValueError("raw.json 缺少 nodes 字段（叶子节点列表，用于空节点检测与 stats）")

    domain = sanitize_domain(data.get("domain", ""))
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

    # 证据校验：候选 URL 必须逐字出现在证据留痕中（PostToolUse hook 系统记录）。
    # 运行级归属优先（run_*/evidence.jsonl，hook 按会话标记写入），否则回退
    # 会话级共享路径（旧流程/手动调用）
    log_path = Path(evidence_log) if evidence_log else (
        run_evidence_log(raw.parent) or Path(default_evidence_log()))
    if (valid or merged or journal) and not log_path.exists():
        raise FileNotFoundError(
            f"证据留痕不存在: {log_path}（PostToolUse hook 未启用或未生效？"
            "没有证据链就不放行候选，这是设计使然）")
    # 留痕由 hook 追加，偶见非法 UTF-8 字节（hook 侧编码损坏）——读取容错，
    # 损坏行 decode 后仍非 JSON，由下游 ValueError 跳过逻辑处理；证据校验语义不变
    evidence = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    # 查询词精确比对用：解析留痕收集全部 JSON 字符串值（一次解析，逐行复用）
    evidence_strings = extract_strings(evidence) if journal else set()

    all_candidates = valid + merged
    # 收录政策（2026-09-10）：垃圾域/低价值聚合平台收尾过滤——与入库即拒双层
    # （历史数据与 CLI 路径不经入库闸门，此处兜底）。过滤在证据校验之前：
    # 垃圾候选无需证据链背书、也不占用"URL 不在留痕"的误导性拒因。
    candidates_pre_filter = len(all_candidates)
    all_candidates = [s for s in all_candidates
                      if not is_garbage_domain(_domain(str(s.get("url") or "")))]
    garbage_filtered = candidates_pre_filter - len(all_candidates)
    # 体裁归一（封闭词表）：MCP 路径的 store 行入库时已归一一次，此处对三来源
    # 汇合（store/CLI raw/knowledge manifest）统一再归一——幂等，双入口口径一致
    # （knowledge 与 CLI 路径不经 record_sources）。表外词（落「其他」）在归一
    # 发生的这一处按原始词计数——审计语义 = 本次运行实际发生的表外词，与来源无关。
    unmapped_types: dict[str, int] = {}
    for s in all_candidates:
        raw_type = str(s.get("source_type") or "").strip()
        source_type = canonicalize_source_type(raw_type)
        s["source_type"] = source_type
        if source_type == "其他" and raw_type != "其他":
            unmapped_types[raw_type] = unmapped_types.get(raw_type, 0) + 1
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
    verification_claims = 0  # journal 声称的验证通过次数（验证搜索/扩量轮行）
    zero_search_rows: list[tuple] = []  # 增量/扩量轮提取为 0 的行（零提取审计/哨兵 2/3）
    # 选题分类统计（2026-09-01 实体选题放开后的验证度量）：只统计增量发现/扩量轮行，
    # 验证搜索行按定义就是实体查询，已被 verified 字段覆盖、不参与分类
    in_framework = {"count": 0, "extracted": 0}
    out_framework = {"count": 0, "extracted": 0, "by_node": {}}
    for j in journal:
        if not isinstance(j, dict):
            journal_skipped += 1
            continue
        query = str(j.get("query") or "")
        phase = str(j.get("phase") or "")
        if query:
            journal_queries.add(query)
            journal_map.setdefault(query, (phase, str(j.get("node") or ""), j.get("results", "")))
        missing = "是" if (query and query not in evidence_strings) else "否"
        # verified：一个字段一个事实——验证通过与顺路新源（extracted）分开记账
        # （2026-09-01 两轮口径不一致治理：交换机轮把验证通过计入 extracted）
        raw_verified = j.get("verified", "")
        verified_ok = raw_verified is True or str(raw_verified).strip().lower() in ("true", "1", "是")
        if verified_ok and _phase_group(phase) in ("验证", "扩量"):
            verification_claims += 1
        if _phase_group(phase) in ("增量", "扩量"):
            try:
                extracted = int(j.get("extracted") or 0)
            except (TypeError, ValueError):
                extracted = 0
            if extracted <= 0:
                zero_search_rows.append(
                    (str(j.get("node") or ""), query, str(j.get("zero_reason") or "")))
            if classify_query_scope(query, domain, nodes) == "框架内":
                in_framework["count"] += 1
                in_framework["extracted"] += extracted
            else:
                out_framework["count"] += 1
                out_framework["extracted"] += extracted
                node = str(j.get("node") or "")
                c, e = out_framework["by_node"].get(node, (0, 0))
                out_framework["by_node"][node] = (c + 1, e + extracted)
        journal_rows.append([
            phase,
            j.get("node", ""),
            query,
            j.get("results", ""),
            j.get("extracted", ""),
            "是" if verified_ok else "",
            missing,
            j.get("zero_reason", ""),
        ])

    # 证据留痕切片（会话级 → 运行级）——血缘表与"来源搜索"列的归因基础
    sliced, slice_kept, slice_skipped = slice_evidence(evidence, journal_queries)

    # 零提取审计与收尾护栏（2026-09-09）：按查询词从留痕取结果 URL 域名，
    # 与最终清单/垃圾域黑名单交叉分类；enforce_quotas 时拦截客观矛盾
    # （哨兵 1 配额、哨兵 2 拒收留痕、哨兵 3 理由与域名证据一致）
    urls_by_query: dict[str, list] = {}
    for line in sliced.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        q = line_query(payload)
        if q:
            urls_by_query.setdefault(q, []).extend(result_urls(payload))
    kept_domains = {_domain(str(s.get("url") or "")) for s in kept}
    zero_audit = _zero_extraction_audit(zero_search_rows, urls_by_query, kept_domains)
    if enforce_quotas:
        _check_node_quota(journal, nodes)
        _check_zero_reason_violations(zero_audit)

    unverified = [
        (str(item.get("name") or "未命名"), str(item.get("note") or "未说明"))
        for item in knowledge
        if isinstance(item, dict) and not item.get("verified")
    ]

    # 失败路径：搜索过（journal 非空）却 0 候选 → 疑似搜索工具异常，
    # 中止并保留 raw.json 供人工检查（两路方案失败路径，见 docs/02 附录决策集）。
    # 按过滤前计数判断——全部候选被政策过滤是合法结果，不得误报工具异常。
    if candidates_pre_filter == 0 and journal_rows:
        raise AutoSourceError(
            "搜索过（journal 非空）但候选为 0——疑似搜索工具异常，"
            "按方案中止处理；raw.json 已保留供人工检查")

    now = now or datetime.now()
    # prepare 预留的运行目录：复用 run_ 名内的时间戳（= 运行开始时刻），
    # 收尾时把目录重命名为 {领域词}_{时间戳}（命名与旧逻辑一致）
    prepared_timestamp = None
    run_match = RUN_DIR_RE.match(raw.parent.name)
    if run_match:
        prepared_timestamp = run_match.group(1)
    timestamp = prepared_timestamp or now.strftime("%Y-%m-%d-%H%M%S")

    summary = {
        "domain": domain,
        "timestamp": timestamp,
        "total_found": candidates_pre_filter,  # 过滤前计数：候选总数 = 过滤移除 + 最终收录（算术自洽）
        "removed_duplicates": removed,
        "ungrounded": ungrounded,
        "kept": len(kept),
        "invalid": invalid,
        "unmatched": node_stats["unmatched"],
        "total_types": node_stats["total_types"],
        "empty_nodes": node_stats["empty_nodes"],
        "per_node": node_stats["per_node"],
        "outdir": "",
        "list_verified": list_verified,
        "verification_mismatch": (verification_claims, len(merged))
                                 if verification_claims < len(merged) else None,
        "in_framework": in_framework,
        "out_framework": out_framework,
        "knowledge_missing": not knowledge,
        "incomplete": incomplete,
        "unverified": unverified,
        "rejected": [(str(s.get("name") or "未命名"), str(s.get("url") or "")) for s in rejected],
        "single_count": single_count,
        "granularity_missing": granularity_missing,
        "unmapped_types": unmapped_types,
        "journal_count": len(journal_rows),
        "journal_skipped": journal_skipped,
        "sources_broken": sources_broken,
        "zero_audit": zero_audit,
        "garbage_filtered": garbage_filtered,
    }

    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    outdir = base / f"{domain}_{timestamp}"
    counter = 1
    while outdir.exists():
        outdir = base / f"{domain}_{timestamp}_{counter}"
        counter += 1
    if prepared_timestamp:
        # 预留目录整体改名（raw.json 随目录移动），无需新建
        raw.parent.rename(outdir)
        raw = outdir / raw.name
    else:
        outdir.mkdir(parents=True)

    write_source_csv(outdir / f"{domain}_{timestamp}_数据源清单.csv", kept,
                     first_query_by_source(kept, sliced))
    summary["outdir"] = str(outdir)

    # 排障材料入 intermediate/；stats 属对比/原料类材料一并放入
    # （2026-08-31 起根目录只留数据源清单，分析报告.md 由模型收尾时写入）
    intermediate = outdir / "intermediate"
    intermediate.mkdir()
    write_stats_csv(intermediate / f"{domain}_{timestamp}_stats.csv", summary)
    if journal_rows:
        write_journal_csv(intermediate / f"{domain}_{timestamp}_搜索日志.csv", journal_rows)
    if sliced:
        (intermediate / "evidence_log.jsonl").write_text(sliced, encoding="utf-8")
    lineage_rows = build_lineage(kept, sliced, journal_map) if sliced else []
    if lineage_rows:
        write_lineage_csv(intermediate / f"{domain}_{timestamp}_溯源.csv", lineage_rows)
    summary["lineage_rows"] = len(lineage_rows)
    summary["evidence_slice_kept"] = slice_kept
    summary["evidence_slice_skipped"] = slice_skipped

    # 删除会话临时文件（--keep-raw 时保留；无修正重跑环节，运行到此结束）。
    # 会话标记与运行级证据随目录重命名来到 outdir——收尾完成即失效，一并清理；
    # （log_path 为预留目录路径时已随重命名失效，exists() 自然为假）
    if not keep_raw:
        raw.unlink(missing_ok=True)
        if log_path.exists():
            log_path.unlink(missing_ok=True)
        (outdir / ".session_id").unlink(missing_ok=True)
        (outdir / "evidence.jsonl").unlink(missing_ok=True)

    return summary


def fold(run_dir: str, *, out_dir: str = "outputs", evidence_log: Optional[str] = None,
         now: Optional[datetime] = None, enforce_quotas: bool = True) -> dict:
    """finalize 折叠：读 store + manifest，组装等价 raw.json 后走完整流水线（docs/04）。

    store.jsonl 承载增量条目、搜索日志与清单核对结果（type=knowledge，2026-09-08
    架构修订：核对结果随验证过程落库，废除阶段 5 一次性转写 manifest）；manifest.json
    承载 domain/nodes/model/知识清单声明态（阶段 0-1）。组装是确定性环节，由脚本
    完成——复用 run_pipeline() 全链路，逻辑一行不改，只换入口。

    搜索日志按（phase, node, query）末次胜出组装（2026-09-09 收尾护栏配套）——
    补录 zero_reason 时重传同键行即覆盖，append-only 语义不变。
    防截断哨兵：store 来源为 0 且搜索提取合计 > 0 → 拒绝折叠、显式报错（模型
    违约未调用 record_sources 时失败响亮，不再静默丢数据）。
    清单了结哨兵：声明清单项在 store 中无核对记录 → 拒绝折叠并点名（漏调
    record_knowledge 同样响亮；2026-09-08 前归档的 manifest 最终核对态走旧路径兜底）。
    收尾护栏三哨兵（enforce_quotas，2026-09-09）：配额（每节点增量搜索 ≥16）、
    拒收留痕（零提取必须带 zero_reason）、理由与域名证据一致——在 run_pipeline
    内、目录重命名前拦截。
    失败发生在目录重命名之前——运行目录保持 run_ 原名，修正后可安全重跑（幂等）。
    成功后 store/manifest 归档进 intermediate/（store_input.jsonl / manifest_input.json）。
    """
    run_dir_path = Path(run_dir)
    manifest_path = run_dir_path / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"manifest.json 不存在: {manifest_path}（阶段 0-1 应先 Write manifest）")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"manifest.json 损坏: {e}")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("nodes"), list):
        raise ValueError('manifest.json 结构错误：应为 {"domain", "nodes", ...} 对象')

    store_path = run_dir_path / "store.jsonl"
    records, bad_lines = load_store(store_path) if store_path.exists() else ([], 0)

    # store 行含内部字段（type/node/ts/source_type_raw）——组装等价 raw 时剥离；
    # store 原件（含 source_type_raw 审计留痕）由 intermediate/store_input.jsonl 保留
    sources = [{k: v for k, v in r.items() if k not in ("type", "node", "ts", "source_type_raw")}
               for r in records if r.get("type") == "source"]
    searches = [r for r in records if r.get("type") == "search"]
    # 搜索日志按（phase, node, query）末次胜出组装（2026-09-09 收尾护栏配套）：
    # 补录 zero_reason 时重传同键行即覆盖——append-only 语义不变，重复行不进 journal
    journal_latest: dict[tuple, dict] = {}
    for s in searches:
        journal_latest[(str(s.get("phase") or ""), str(s.get("node") or ""),
                        str(s.get("query") or ""))] = {
            "phase": s.get("phase", ""), "node": s.get("node", ""),
            "query": s.get("query", ""), "results": s.get("results", ""),
            "extracted": s.get("extracted", ""), "verified": s.get("verified", ""),
            "zero_reason": s.get("zero_reason", ""),
        }
    journal = list(journal_latest.values())
    extracted_total = 0
    for j in journal:
        # 防截断哨兵只对增量/扩量轮计数（2026-09-02 收窄触发域）：
        # 验证搜索的提取走 manifest 不进 store.sources，纯清单零增量是合法运行
        if _phase_group(str(j.get("phase") or "")) not in ("增量", "扩量"):
            continue
        try:
            extracted_total += int(j["extracted"])
        except (TypeError, ValueError):
            pass
    knowledge_list = manifest.get("knowledge") if isinstance(manifest.get("knowledge"), list) else []
    # 清单核对结果（2026-09-08 架构修订）：核对结果随验证过程经 record_knowledge
    # 落库，fold 从 store 取末次记录与声明清单对账——废除"会话暂存 + 阶段 5
    # 一次性转写 manifest"（214051 实证漏写 60 个 verified 字段的事故类别）。
    declared = knowledge_list
    declared_names = {str(k.get("name")) for k in declared
                      if isinstance(k, dict) and k.get("name")}
    knowledge_records = [r for r in records if r.get("type") == "knowledge"]
    latest: dict[str, dict] = {}
    for r in knowledge_records:
        name = str(r.get("name") or "")
        if name:
            latest[name] = r  # 同名重录 = 状态更新（append-only，末次胜出）
    unrecorded = sorted(declared_names - set(latest))
    # 旧路径兜底：manifest 清单带核对字段（verified/url——2026-09-08 前归档的
    # 最终核对态）且 store 无核对记录 → 照旧并入（历史归档可复盘，新旧流程并存）
    manifest_carries_state = any(
        isinstance(k, dict) and ("verified" in k or k.get("url")) for k in declared)
    if knowledge_records:
        if unrecorded:
            raise AutoSourceError(
                f"清单项未了结：{len(unrecorded)} 项在 store 中无核对记录"
                f"（漏调 record_knowledge）——补录后重跑 finalize："
                + "、".join(unrecorded))
        # source_type 回填原始词（入库时已归一）——收尾统一归一处在 run_pipeline，
        # 表外词审计按原始词计数（与 record_sources 的入库反馈口径互补）
        knowledge_list = [
            {k: v for k, v in r.items() if k not in ("type", "ts")}
            | {"source_type": r.get("source_type_raw") or r.get("source_type", "")}
            for r in latest.values()
        ]
    elif manifest_carries_state:
        knowledge_list = declared
    elif declared_names:
        raise AutoSourceError(
            f"清单项未了结：清单 {len(declared_names)} 项均无核对记录"
            f"（漏调 record_knowledge）——补录后重跑 finalize："
            + "、".join(sorted(declared_names)))
    knowledge_any_verified = any(
        isinstance(k, dict) and k.get("verified") and k.get("name") and k.get("url")
        for k in knowledge_list)
    if not sources and extracted_total > 0 and not knowledge_any_verified:
        raise AutoSourceError(
            f"来源未入库：增量/扩量搜索提取合计 {extracted_total} 条，但 store 中来源为 0——"
            "疑似未调用 record_sources；修正后重跑 finalize（运行目录未被重命名）")

    data = {
        "domain": manifest.get("domain", ""),
        "nodes": [str(n) for n in manifest["nodes"]],
        "model": manifest.get("model", ""),
        "knowledge": knowledge_list,
        "journal": journal,
        "sources": sources,
    }
    raw_path = run_dir_path / "raw.json"
    raw_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    summary = run_pipeline(str(raw_path), out_dir=out_dir, evidence_log=evidence_log,
                           now=now, enforce_quotas=enforce_quotas)

    # run() 成功时目录已整体重命名——store/manifest 随目录移动，路径重新指向
    # 新目录后归档进 intermediate/ 并删除原件（2026-09-01 修复：此前 store 漏归档，
    # 交付目录根残留 store.jsonl——根目录只留数据源清单，报告由模型收尾写入）
    outdir = Path(summary["outdir"])
    intermediate = outdir / "intermediate"
    store_path = outdir / "store.jsonl"
    manifest_path = outdir / "manifest.json"
    shutil.move(str(store_path), intermediate / "store_input.jsonl")
    shutil.move(str(manifest_path), intermediate / "manifest_input.json")
    summary["store_bad_lines"] = bad_lines
    return summary


def summary_text(summary: dict) -> str:
    """汇总统计的可读文本（CLI stdout 与 MCP finalize 工具共用同一份口径）。"""
    lines = [f"领域: {summary['domain']}",
             f"输出目录: {summary['outdir']}",
             f"候选总数: {summary['total_found']}  去重移除: {summary['removed_duplicates']}"
             f"  证据校验移除: {summary['ungrounded']}"
             f"  最终收录: {summary['kept']}"]
    if summary.get("garbage_filtered"):
        lines.append(f"垃圾域过滤移除: {summary['garbage_filtered']} 条"
                     "（名单见 store.GARBAGE_DOMAINS）")
    if summary["invalid"]:
        lines.append(f"无效记录(缺名称/URL): {summary['invalid']}")
    if summary["unmatched"]:
        lines.append(f"警告: {summary['unmatched']} 条记录的分类路径未匹配到任何节点")
    if summary["rejected"]:
        lines.append("证据校验移除明细（网址不在搜索结果留痕中，未进入清单）:")
        for name, url in summary["rejected"]:
            lines.append(f"  - {name}: {url}")
    if summary["granularity_missing"]:
        lines.append(f"警告: {summary['granularity_missing']} 条缺 granularity 声明，按合集级处理")
    if summary.get("unmapped_types"):
        detail = "、".join(f"{w}×{c}" for w, c in sorted(
            summary["unmapped_types"].items(), key=lambda kv: (-kv[1], kv[0])))
        lines.append(f"表外词兜底: {sum(summary['unmapped_types'].values())} 条（{detail}）"
                     "——落『其他』，高频词可补别名进 store.SOURCE_TYPE_ALIASES")
    lines.append(f"清单核对: 验证通过 {summary['list_verified']} 项")
    mismatch = summary.get("verification_mismatch")
    if mismatch:
        claims, merged = mismatch
        lines.append("警告: 搜索日志验证通过标记数与清单验证通过数不一致——"
                     f"journal 声称 {claims} 次 < 清单实际并入 {merged} 项"
                     f"（差 {merged - claims}）：verified 记账漏填（复盘时注意）")
    if summary.get("in_framework") is not None:
        f, o = summary["in_framework"], summary["out_framework"]
        f_avg = f"{f['extracted'] / f['count']:.1f}" if f["count"] else "0"
        o_avg = f"{o['extracted'] / o['count']:.1f}" if o["count"] else "0"
        lines.append(f"选题分布: 含领域词 {f['count']} 次（每搜 {f_avg}）/ "
                     f"不含领域词 {o['count']} 次（每搜 {o_avg}）"
                     "（不含领域词含实体选题/英文角度词，按趋势读）")
        if o["by_node"]:
            lines.append("不含领域词节点分布: " + "; ".join(
                f"{n} {c} 次（每搜 {e / c:.1f}）" for n, (c, e) in o["by_node"].items()))
    if summary["knowledge_missing"]:
        lines.append("警告: manifest 无 knowledge 字段（本次无权威源清单，退化为纯增量模式，**本次无底线保证**）")
    if summary["incomplete"]:
        lines.append(f"警告: {summary['incomplete']} 条 verified 清单项缺 name/url，未并入")
    if summary["sources_broken"]:
        lines.append("警告: sources 字段不是列表，已按空处理")
    if summary.get("store_bad_lines"):
        lines.append(f"警告: store.jsonl 有 {summary['store_bad_lines']} 行损坏被跳过")
    if summary["journal_skipped"]:
        lines.append(f"警告: {summary['journal_skipped']} 条 journal 记录结构损坏被跳过")
    if summary["unverified"]:
        lines.append("未验证清单（人工交接单）:")
        for name, note in summary["unverified"]:
            lines.append(f"  - {name}（{note}）")
    else:
        lines.append("未验证清单: 无")
    if summary["journal_count"]:
        lines.append(f"搜索日志: {summary['journal_count']} 次搜索（见 intermediate/搜索日志.csv）")
    zero = summary.get("zero_audit")
    if zero and zero["total"]:
        cat_str = " / ".join(f"{k} {v}" for k, v in zero["categorized"].items()) \
            if zero["categorized"] else "（分类无数据）"
        lines.append(f"零提取审计: {zero['total']} 条零提取（{cat_str}）")
        suspects = zero["suspects"]
        if suspects:
            lines.append("疑似漏收清单（结果域名不在清单且非垃圾域——供人工复核）:")
            for node, query, domains, reason in suspects[:30]:
                lines.append(f"  - [{node}] {query} | 域名: {domains} | 理由: {reason or '无'}")
            if len(suspects) > 30:
                lines.append(f"  …等共 {len(suspects)} 条（完整清单见搜索日志的零提取理由列）")
    if summary["outdir"]:
        if summary.get("lineage_rows"):
            lines.append(f"数据血缘: 溯源.csv（{summary['lineage_rows']} 行，见 intermediate/）")
        lines.append(f"证据留痕切片: 保留 {summary['evidence_slice_kept']} 行 / 跳过 {summary['evidence_slice_skipped']} 行"
                     f"（见 intermediate/）")
    lines.append("各节点:")
    for node, info in summary["per_node"].items():
        dist = _format_type_dist(info["types"])
        suffix = f" ({dist})" if dist else ""
        lines.append(f"  {node}: {info['count']} 条{suffix}")
    empty = summary["empty_nodes"]
    lines.append(f"无结果节点: {'、'.join(empty) if empty else '无'}")
    return "\n".join(lines)


def _print_summary(summary: dict) -> None:
    print(summary_text(summary))


def main() -> None:
    # Windows 控制台默认 GBK，stdout 中文会乱码——统一转 UTF-8（失败则保持默认）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="AutoSource 后处理流水线")
    parser.add_argument("raw_json", nargs="?", help="raw.json 路径（阶段 5 写入运行目录内；--prepare 模式下省略）")
    parser.add_argument("--prepare", action="store_true",
                        help="预留本次运行目录（outputs/run_{时间戳}/，每次运行唯一）并打印路径——流程开始时调用")
    parser.add_argument("--evidence-log", default=None,
                        help="证据留痕文件（PostToolUse hook 自动记录；缺省按会话隔离命名 outputs/search_log_{会话}.jsonl）")
    parser.add_argument("--out-dir", default="outputs", help="输出根目录（默认 outputs）")
    parser.add_argument("--keep-raw", action="store_true", help="保留 raw.json 与证据留痕不删除")
    parser.add_argument("--rename-report", metavar="DIR",
                        help="把 DIR/分析报告.md 重命名为 {目录名}_分析报告.md（报告内容由模型写入，文件名由脚本命名）——阶段 6 写报告后调用")
    args = parser.parse_args()

    if args.prepare:
        print(prepare_run_dir(Path(args.out_dir)))
        return
    if args.rename_report:
        try:
            print(f"分析报告已重命名: {finalize_report(args.rename_report)}")
        except (FileNotFoundError, FileExistsError) as e:
            print(f"错误: {e}", file=sys.stderr)
            sys.exit(1)
        return
    if not args.raw_json:
        parser.error("需要 raw.json 路径（或使用 --prepare 预留运行目录）")

    try:
        summary = run_pipeline(args.raw_json, out_dir=args.out_dir, keep_raw=args.keep_raw,
                               evidence_log=args.evidence_log)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    _print_summary(summary)


if __name__ == "__main__":
    main()
