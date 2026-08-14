"""AutoSource 后处理流水线：去重 → 导出 CSV → 效果统计 → 清理中间文件。

分工原则：LLM（编排层）只负责语义环节，唯一产出的中间产物是 raw.json；
本脚本保证其余所有确定性环节：

- 输出目录与时间戳由脚本生成（模型没有时钟，禁止模型编造）
- 域名 + 名称联合去重
- 用 csv 标准库导出数据源清单（UTF-8 BOM，转义交给标准库）
- 计算各节点候选数与体裁分布，写 stats CSV（含空节点检测）
- 删除中间产物 raw.json

用法:
    python postprocess.py <raw.json> [--out-dir DIR] [--keep-raw]

raw.json 结构:
    {
      "domain": "领域词（用于目录命名）",
      "nodes": ["全部叶子节点"],
      "model": "模型名（可选，写入 stats CSV 供溯源）",
      "sources": [
        {
          "name": "数据源名称",
          "category_path": "层级1-层级2-叶子节点",
          "source_type": "内容体裁",
          "url": "主入口 URL",
          "description": "简要说明"
        }
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

SOURCE_CSV_HEADER = ["数据源名称", "分类路径", "数据源类型", "访问地址", "简要说明"]
STATS_CSV_HEADER = ["领域", "时间戳", "分类节点", "候选数", "体裁分布", "无结果节点",
                    "总候选数", "去重移除", "最终收录", "模型", "脚本处理耗时(秒)"]


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
                summary["kept"],
                summary["model"],
                summary["elapsed_seconds"],
            ])


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

    # 脏数据容错：name/url 缺一即跳过并计数，不让单条坏记录中断整批
    valid: list[dict] = []
    invalid = 0
    for s in data["sources"]:
        if not isinstance(s, dict) or not s.get("name") or not s.get("url"):
            invalid += 1
            continue
        valid.append(s)

    kept = deduplicate(valid)
    removed = len(valid) - len(kept)
    node_stats = compute_stats(kept, nodes)

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

    summary = {
        "domain": domain,
        "timestamp": timestamp,
        "timestamp_display": now.strftime("%Y-%m-%d %H:%M:%S"),
        "total_found": len(valid),
        "removed_duplicates": removed,
        "kept": len(kept),
        "invalid": invalid,
        "unmatched": node_stats["unmatched"],
        "empty_nodes": node_stats["empty_nodes"],
        "per_node": node_stats["per_node"],
        "outdir": str(outdir),
        "model": model,
        "elapsed_seconds": round(time.perf_counter() - start, 1),
    }
    write_stats_csv(outdir / f"{domain}_{timestamp}_stats.csv", summary)

    if not keep_raw:
        raw.unlink(missing_ok=True)

    return summary


def _print_summary(summary: dict) -> None:
    print(f"领域: {summary['domain']}")
    print(f"输出目录: {summary['outdir']}")
    print(f"候选总数: {summary['total_found']}  去重移除: {summary['removed_duplicates']}"
          f"  最终收录: {summary['kept']}")
    if summary["invalid"]:
        print(f"无效记录(缺名称/URL): {summary['invalid']}")
    if summary["unmatched"]:
        print(f"警告: {summary['unmatched']} 条记录的分类路径未匹配到任何节点")
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
    parser = argparse.ArgumentParser(description="AutoSource 后处理流水线")
    parser.add_argument("raw_json", help="编排层产出的 raw.json 路径")
    parser.add_argument("--out-dir", default="outputs", help="输出根目录（默认 outputs）")
    parser.add_argument("--keep-raw", action="store_true", help="保留 raw.json 不删除")
    args = parser.parse_args()

    try:
        summary = run(args.raw_json, out_dir=args.out_dir, keep_raw=args.keep_raw)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)

    _print_summary(summary)


if __name__ == "__main__":
    main()
