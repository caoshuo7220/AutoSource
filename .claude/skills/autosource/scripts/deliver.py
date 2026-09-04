"""AutoSource 2.0 交付物生成：数据源清单 CSV、统计、搜索日志、溯源、分析报告。

由 1.0 postprocess.py / lineage.py / report.py 的有效逻辑拆分迁入（实现规格第三章）：
- 数据源清单 7 列（UTF-8 BOM，Excel 直接打开；URL 输出前剥离引用序号锚点）
- 交付去重（域名+名称：合集级优先 → first_seen_batch 最早）
- stats.csv（长表：每叶子节点一行 + 总计行）
- 搜索日志.csv（每次搜索一行：batch/node/query/结果数/提取数/去重后新增数/失败数）
- 溯源.csv（数据血缘：结果 URL ↔ 收录条目 ↔ 查询词的正查/反查，依据 raw/ 归档反查）
- 分析报告（六板块正文由 report 节点生成，"数据总览"节由脚本注入——模型不写统计数字）
"""
import csv
import json
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from evidence import _contains_bounded, strip_citation_anchors, URL_CHARS
from state import path_of

SOURCE_CSV_HEADER = ["数据源名称", "分类路径", "数据源类型", "粒度", "访问地址", "简要说明", "来源搜索"]
STATS_CSV_HEADER = ["分类节点", "候选数", "体裁分布"]
SEARCH_LOG_CSV_HEADER = ["批次", "分类节点", "查询词", "结果数", "提取数", "去重后新增数", "失败数"]
LINEAGE_CSV_HEADER = ["批次", "分类节点", "查询词", "结果URL", "是否收录", "收录条目名称", "备注"]

# 报告降级正文（report 节点重试后仍失败时使用）：六板块标题齐全、每节注明生成失败
DEGRADED_REPORT_BODY = """（正文生成失败：分析报告 LLM 调用在重试后仍失败，本次仅产出数据总览与确定性交付物。）
## 一、领域概览
（内容生成失败）
## 二、技术格局
（内容生成失败）
## 三、产业生态
（内容生成失败）
## 四、标准与规范体系
（内容生成失败）
## 五、中文与国际生态对比
（内容生成失败）
## 六、趋势观察
（内容生成失败）
"""

_GRANULARITY_RANK = {"合集级": 0, "单篇级": 1}


def _domain(url: str) -> str:
    return urlparse(url).netloc


def deduplicate_delivery(sources: list[dict]) -> list[dict]:
    """交付去重：同一"域名 + 名称"只保留一条。

    优先级：合集级 > 单篇级；同粒度取 first_seen_batch 最早者；仍并列取先出现者。
    输出保持首次出现顺序（镜像站不同域名各自保留）。
    """
    best: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for source in sources:
        key = (_domain(source.get("url") or ""), source.get("name"))
        if key in best:
            current = best[key]
            if (_GRANULARITY_RANK.get(source.get("granularity"), 0)
                    < _GRANULARITY_RANK.get(current.get("granularity"), 0)):
                best[key] = source
            elif (source.get("granularity") == current.get("granularity")
                  and source.get("first_seen_batch", 0) < current.get("first_seen_batch", 0)):
                best[key] = source
        else:
            best[key] = source
            order.append(key)
    return [best[key] for key in order]


def write_source_csv(path: Path, sources: list[dict], nodes: list[dict]) -> None:
    """写数据源清单 CSV：UTF-8 BOM，转义由 csv 标准库保证。

    访问地址输出前剥离尾部纯数字引用锚点（证据校验在前严格逐字、清理在后）；
    "来源搜索"列 = source.first_seen_query（该 URL 首次出现查询，见溯源.csv 全貌）。
    """
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(SOURCE_CSV_HEADER)
        for source in sources:
            writer.writerow([
                source.get("name", ""),
                path_of(source.get("node", ""), nodes) if source.get("node") else "",
                source.get("source_type", ""),
                source.get("granularity", ""),
                strip_citation_anchors(str(source.get("url") or "")),
                source.get("description", ""),
                source.get("first_seen_query", ""),
            ])


def compute_stats(sources: list[dict], leaves: list[str]) -> dict:
    """按去重后的源计算各叶子节点候选数与体裁分布；total_types 供总计行。"""
    per_node = {node: {"count": 0, "types": {}} for node in leaves}
    total_types: dict[str, int] = {}
    for source in sources:
        source_type = source.get("source_type") or "未标注"
        total_types[source_type] = total_types.get(source_type, 0) + 1
        node = source.get("node")
        if node in per_node:
            per_node[node]["count"] += 1
            per_node[node]["types"][source_type] = \
                per_node[node]["types"].get(source_type, 0) + 1
    return {"per_node": per_node, "total": len(sources), "total_types": total_types}


def _type_dist(types: dict[str, int]) -> str:
    return "; ".join(f"{t}:{c}"
                     for t, c in sorted(types.items(), key=lambda kv: (-kv[1], kv[0])))


def write_stats_csv(path: Path, summary: dict) -> None:
    """写清单统计 CSV：每叶子节点一行（节点名/候选数/体裁分布），末尾一行总计。"""
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(STATS_CSV_HEADER)
        for node, info in summary["per_node"].items():
            writer.writerow([node, info["count"], _type_dist(info["types"])])
        writer.writerow(["总计", summary["total"], _type_dist(summary["total_types"])])


def write_search_log_csv(path: Path, history: list[dict]) -> None:
    """写搜索日志 CSV：每次搜索一行（实现规格第七章）。"""
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(SEARCH_LOG_CSV_HEADER)
        for row in history:
            writer.writerow([
                row.get("batch", ""), row.get("node", ""), row.get("query", ""),
                row.get("result_count", ""), row.get("extracted", ""),
                row.get("new_count", ""), row.get("failed", ""),
            ])


def _batch_number(name: str) -> int:
    try:
        return int(name.split("_", 1)[1].split(".", 1)[0])
    except (IndexError, ValueError):
        return 0


def build_lineage(state: dict, delivered: list[dict], raw_dir: Path) -> list[list]:
    """生成数据血缘行：每行 = 一次搜索的一条结果（正查/反查同一张表承载）。

    依据 raw/batch_*.json 归档的原始结果反查：结果 URL（剥锚点）与交付清单
    （剥锚点）join 得到收录关系；收录了但不在结构化 results[].url 中的 URL
    （仅出现在 title/snippet 文本）走文本边界匹配回退，备注"摘要文本提及"。
    """
    collected: dict[str, tuple[str, str]] = {}
    for source in delivered:
        url = strip_citation_anchors(str(source.get("url") or ""))
        if url and url not in collected:
            collected[url] = (str(source.get("name") or ""), str(source.get("node") or ""))

    rows: list[list] = []
    covered: set[str] = set()
    for batch_file in sorted(raw_dir.glob("batch_*.json")):
        batch = _batch_number(batch_file.name)
        try:
            entries = json.loads(batch_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("failed"):
                continue
            results = entry.get("results")
            if not isinstance(results, list):
                continue
            query = str(entry.get("query") or "")
            node = str(entry.get("node") or "")
            for item in results:
                if not isinstance(item, dict):
                    continue
                url = strip_citation_anchors(str(item.get("url") or ""))
                if not url:
                    continue
                name, _ = collected.get(url, ("", ""))
                rows.append([batch, node, query, url, "是" if name else "否", name, ""])
                if name:
                    covered.add(url)
            # 回退：仅出现在 title/snippet 文本中的收录 URL（证据链允许，结构化解析找不到）
            for url, (name, _) in collected.items():
                if url in covered or not _contains_bounded(
                        url, json.dumps(entry, ensure_ascii=False), URL_CHARS):
                    continue
                rows.append([batch, node, query, url, "是", name, "摘要文本提及"])
                covered.add(url)
    return rows


def write_lineage_csv(path: Path, rows: list[list]) -> None:
    """写数据血缘 CSV：搜索结果与收录的对应关系（正查/反查）。"""
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(LINEAGE_CSV_HEADER)
        writer.writerows(rows)


def report_stats_block(sources: list[dict], leaves: list[str]) -> str:
    """从交付清单生成"数据总览"段——报告统计数字由脚本生成、模型不写数字。

    数字口径以去重后的交付清单为准（模型运行记忆中的提取数是去重前口径）。
    """
    summary = compute_stats(sources, leaves)
    collection = sum(1 for s in sources if s.get("granularity") == "合集级")
    single = len(sources) - collection
    node_dist = "; ".join(f"{node}: {summary['per_node'][node]['count']}"
                          for node in leaves)
    return "\n".join([
        "## 数据总览",
        "",
        f"- 数据源总数：{len(sources)} 条（合集级 {collection} / 单篇级 {single}）",
        f"- 分类节点：{len(leaves)} 个",
        "- 节点分布：" + node_dist,
        "- 体裁分布：" + _type_dist(summary["total_types"]),
        "",
    ])


def compose_report(domain: str, body: str, stats_block: str) -> str:
    """组装分析报告：标题 + 说明 + 数据总览（脚本注入）+ 六板块正文（report 节点）。"""
    return "\n".join([
        f"# {domain} 领域分析报告",
        "> 本报告基于本次自动发现的数据源生成；统计数字见下方\"数据总览\"（由脚本生成）。"
        "领域判断为模型解读，不保证事实准确。",
        "",
        stats_block.rstrip(),
        body.rstrip(),
        "",
    ])
