"""AutoSource 后处理流水线：证据校验 → 清单并入 → 去重 → 导出 CSV → 效果统计 → 清理。

分工原则：LLM（编排层）只负责语义环节，唯一产出的中间产物是 raw.json；
本脚本保证其余所有确定性环节：

- 证据校验（grounded check）：候选 URL 必须能逐字出现在证据留痕中（留痕由
  PostToolUse hook 在每次 WebSearch/WebFetch 时由系统自动记录，模型不可篡改），
  否则拒绝该条并计数——从结构上杜绝模型编造 URL
- 粒度矛盾检测：候选声明 granularity 为合集级/站点级但 URL 呈文档形态（PDF、
  新闻/问答/评测深链等确定性信号）→ 拒绝并报明细——结构性防"名实不符"
  （如条目名"XX 文档中心"却指向一份 PDF）；单篇级条目放行但计数进 stats
- 知识清单：verified=true 且字段齐全的清单项自动并入 sources（LLM 不手工复制）；
  计算清单验证率（自洽性指标），未验证清单进 stdout 报告
- 搜索日志：journal 的每个查询词必须逐字出现在证据留痕中（不在则标注"证据缺失"），
  生成 搜索日志.csv 供人工复盘
- 输出目录与时间戳由脚本生成（模型没有时钟，禁止模型编造）
- 域名 + 名称联合去重
- 用 csv 标准库导出数据源清单（UTF-8 BOM，转义交给标准库）
- 计算各节点候选数与体裁分布，写 stats CSV（含空节点检测、清单验证列）
- 删除中间产物 raw.json 与证据留痕

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
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

SOURCE_CSV_HEADER = ["数据源名称", "分类路径", "数据源类型", "粒度", "访问地址", "简要说明"]
STATS_CSV_HEADER = ["领域", "时间戳", "分类节点", "候选数", "体裁分布", "无结果节点",
                    "总候选数", "去重移除", "证据校验移除", "粒度矛盾移除", "清单验证",
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


def check_grounded(sources: list[dict], evidence: str) -> tuple[list[dict], list[dict]]:
    """证据校验：URL 必须逐字出现在证据留痕中。返回 (通过, 被拒)。"""
    kept: list[dict] = []
    rejected: list[dict] = []
    for s in sources:
        if s.get("url", "") in evidence:
            kept.append(s)
        else:
            rejected.append(s)
    return kept, rejected


# 文档形态 URL 的确定性信号（保守口径：宁可漏检，不可误杀榜单/门户等合法合集页面）
DOC_EXTENSIONS = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".md", ".txt")
SINGLE_CONTENT_PATTERNS = [
    r"/news/.+",             # 新闻深链（e.huawei.com/news/2024/...）
    r"news-detail",          # 新闻详情页（3onedata news-detail.aspx）
    r"pressrelease",         # 新闻稿页（socionextus.com/pressreleases/...）
    r"press-releases",
    r"/articles?/",          # 单篇文章路径
    r"/article\?id=",        # 文章详情（cww.net.cn/article?id=...）
    r"/questions/.+",        # 单条问答（zhiliao.h3c.com/questions/dispcont/...）
    r"answers/detail",       # 单条 KB（kb.netgear.com/app/answers/detail/a_id/...）
    r"/contact(_us)?(/|$)",  # 联系页（官网/门户条目指向联系页必错）
    r"/(issues|pulls)(/|$)",  # GitHub 仓库子页
    r"/(blob|tree)/",        # GitHub 单文件/子目录视图
    r"/doc/.+",              # 单文档页（support.huawei.com/zh/doc/EDOC...）
    r"/document/.+",         # 单报告页（lightcounting.com/document/...）
    r"/reviews?/.+",         # 单篇评测深链（storagereview.com/review/...）
    r"review/\d",            # 评测文章分页（servethehome.com/...-review/3/）
]
GRANULARITY_LEVELS = ("合集级", "站点级", "单篇级")


def is_document_url(url: str) -> bool:
    """确定性检测：URL 是否指向单份文档/单条内容页（与"体系级入口"矛盾）。"""
    path = urlparse(url).path.lower()
    if path.endswith(DOC_EXTENSIONS):
        return True
    return any(re.search(p, path) for p in SINGLE_CONTENT_PATTERNS)


def check_granularity(sources: list[dict]) -> tuple[list[dict], list[dict], int, int]:
    """粒度矛盾检测：声明合集级/站点级但 URL 呈文档形态 → 拒绝。

    返回 (通过, 被拒, 单篇级数, 缺声明数)：
    - granularity 缺失或非法视为合集级（P-002 默认），计数告警
    - 单篇级条目放行但计数——例外条款的使用量进 stats，滥用可见
    """
    kept: list[dict] = []
    rejected: list[dict] = []
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
            kept.append(s)
        elif is_document_url(str(s.get("url") or "")):
            rejected.append(s)
        else:
            kept.append(s)
    return kept, rejected, single_count, missing_count


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


def write_source_csv(path: Path, sources: list[dict]) -> None:
    """写数据源清单 CSV：UTF-8 BOM（utf-8-sig），转义由 csv 标准库保证。"""
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(SOURCE_CSV_HEADER)
        for s in sources:
            writer.writerow([
                s.get("name", ""),
                s.get("category_path", ""),
                s.get("source_type", ""),
                s.get("granularity", ""),
                s.get("url", ""),
                s.get("description", ""),
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
                summary["granularity_rejected_count"],
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

    all_candidates = valid + merged
    grounded, rejected = check_grounded(all_candidates, evidence)
    ungrounded = len(rejected)

    # 粒度矛盾检测：声明合集级/站点级但 URL 呈文档形态 → 拒绝（结构性防"名实不符"）
    granularity_ok, granularity_rejected, single_count, granularity_missing = \
        check_granularity(grounded)

    kept = deduplicate(granularity_ok)
    removed = len(granularity_ok) - len(kept)
    node_stats = compute_stats(kept, nodes)

    journal_rows: list[list] = []
    journal_skipped = 0
    for j in journal:
        if not isinstance(j, dict):
            journal_skipped += 1
            continue
        query = str(j.get("query") or "")
        missing = "是" if (query and query not in evidence) else ""
        journal_rows.append([
            j.get("phase", ""),
            j.get("node", ""),
            query,
            j.get("results", ""),
            j.get("extracted", ""),
            missing,
        ])

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

    base = Path(out_dir)
    base.mkdir(parents=True, exist_ok=True)
    outdir = base / f"{domain}_{timestamp}"
    counter = 1
    while outdir.exists():
        outdir = base / f"{domain}_{timestamp}_{counter}"
        counter += 1
    outdir.mkdir(parents=True)

    write_source_csv(outdir / f"{domain}_{timestamp}_数据源清单.csv", kept)
    if journal_rows:
        write_journal_csv(outdir / f"{domain}_{timestamp}_搜索日志.csv", journal_rows)

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
        "outdir": str(outdir),
        "model": model,
        "elapsed_seconds": round(time.perf_counter() - start, 1),
        "list_verified": list_verified,
        "knowledge_missing": not knowledge,
        "incomplete": incomplete,
        "unverified": unverified,
        "rejected": [(str(s.get("name") or "未命名"), str(s.get("url") or "")) for s in rejected],
        "granularity_rejected_count": len(granularity_rejected),
        "granularity_rejected": [
            (str(s.get("name") or "未命名"), str(s.get("granularity") or "合集级"),
             str(s.get("url") or "")) for s in granularity_rejected],
        "single_count": single_count,
        "granularity_missing": granularity_missing,
        "journal_count": len(journal_rows),
        "journal_skipped": journal_skipped,
        "sources_broken": sources_broken,
    }
    write_stats_csv(outdir / f"{domain}_{timestamp}_stats.csv", summary)

    # 有被拒条目（证据校验或粒度矛盾）时保留中间产物，便于修正 URL 后重跑
    # （否则按 --keep-raw 处理）
    if not keep_raw and ungrounded == 0 and not granularity_rejected:
        raw.unlink(missing_ok=True)
        if log_path.exists():
            log_path.unlink(missing_ok=True)

    return summary


def _print_summary(summary: dict) -> None:
    print(f"领域: {summary['domain']}")
    print(f"输出目录: {summary['outdir']}")
    print(f"候选总数: {summary['total_found']}  去重移除: {summary['removed_duplicates']}"
          f"  证据校验移除: {summary['ungrounded']}  粒度矛盾移除: {summary['granularity_rejected_count']}"
          f"  最终收录: {summary['kept']}")
    if summary["invalid"]:
        print(f"无效记录(缺名称/URL): {summary['invalid']}")
    if summary["unmatched"]:
        print(f"警告: {summary['unmatched']} 条记录的分类路径未匹配到任何节点")
    if summary["rejected"]:
        print("证据校验移除明细:")
        for name, url in summary["rejected"]:
            print(f"  - {name}: {url}")
        print("（raw.json 与证据留痕已保留——核对修正 URL 后重跑本命令即可补入）")
    if summary["granularity_rejected"]:
        print("粒度矛盾移除明细（声明合集级/站点级但 URL 是单份文档）:")
        for name, g, url in summary["granularity_rejected"]:
            print(f"  - {name}（{g}）: {url}")
        print("（raw.json 与证据留痕已保留——修正为体系入口 URL 或改声明单篇级后重跑即可补入）")
    if summary["granularity_missing"]:
        print(f"警告: {summary['granularity_missing']} 条缺 granularity 声明，按合集级处理")
    if summary["single_count"]:
        print(f"单篇级收录: {summary['single_count']} 条（P-002 例外条款使用情况，见 stats.csv）")
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
