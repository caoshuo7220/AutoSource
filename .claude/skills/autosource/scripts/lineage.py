"""AutoSource 数据血缘模块：溯源表与"来源搜索"归因（自 postprocess 拆分）。

行级血缘全部确定性推导——关联键是源 URL ↔ 留痕结果 URL，脚本 join 生成，
LLM 零新增职责（2026-08-19 定案）。本模块被 postprocess 使用。
"""
import csv
import json
from pathlib import Path

from evidence import (find_bounded, line_query, result_urls, strip_bold,
                      strip_citation_anchors)

LINEAGE_CSV_HEADER = ["阶段", "分类节点", "查询词", "返回结果数", "结果URL", "是否收录",
                      "收录条目名称", "收录理由", "备注"]


def first_query_by_source(kept: list[dict], sliced_evidence: str) -> dict[str, str]:
    """每个收录源 URL（剥引用锚点后）→ 在切片留痕中首次出现行的查询词。

    首次出现 ≈ 发现时刻：验证搜索的首次出现即其定向验证查询，
    增量发现的首次出现即撞见它的那次搜索。多出处完整真相见溯源表。

    匹配优先走结构化结果解析（与 build_lineage 同路径）：留痕里结果 URL
    常带 #数字 引用锚点，剥锚点后的子串在原文中会被边界匹配判为"更长 URL
    的前缀"而丢失归因；结构化路径先剥锚点再比对，与溯源表口径一致。
    结构化结果中没有的 URL（仅摘要文本提及）走原文边界匹配兜底：
    先试未剥锚点原形，再试剥锚点形态。
    """
    originals = {strip_citation_anchors(str(s.get("url") or "")): str(s.get("url") or "")
                 for s in kept}
    wanted = set(originals) - {""}
    result: dict[str, str] = {}
    for line in sliced_evidence.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        query = line_query(payload)
        for url in result_urls(payload):
            stripped = strip_citation_anchors(url)
            if stripped and stripped in wanted and stripped not in result:
                result[stripped] = query
        pending = wanted - result.keys()
        if pending:
            clean = strip_bold(line)      # 每行归一一次（逐候选重复剥是收尾的主要开销）
            for stripped in pending:
                if (find_bounded(originals[stripped], clean)
                        or find_bounded(stripped, clean)):
                    result[stripped] = query
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
        query = line_query(payload)
        phase, node, results_count = journal_map.get(query, ("", "", ""))
        for url in result_urls(payload):
            stripped = strip_citation_anchors(url)
            name, reason = collected.get(stripped, ("", ""))
            rows.append([phase, node, query, results_count, stripped,
                         "是" if name else "否", name, reason, ""])
            if name:
                covered.add(stripped)
    # 回退：收录了但不在任何结构化结果数组中的 URL（仅出现在摘要文本）
    lines = [(line, strip_bold(line)) for line in sliced_evidence.splitlines()]
    for stripped, (name, reason) in collected.items():
        if stripped in covered:
            continue
        for line, clean in lines:
            if not find_bounded(stripped, clean):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            query = line_query(payload)
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
