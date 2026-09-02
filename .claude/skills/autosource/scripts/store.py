"""AutoSource 存储层：store.jsonl 追加日志 + 入库即验 + coverage 对账（docs/04）。

store 只承载两类记录：增量发现条目（type=source）与搜索日志（type=search）；
知识清单不进 store（在 manifest.json，收尾折叠时由 postprocess.fold 组装）。
分工原则：LLM 只做语义判断，持久化与校验全部由本模块（脚本）保证。

- record_sources：批次入库即验——name/url 非空、granularity 枚举（缺省/非法
  归一化为合集级）、URL 走证据链边界校验（evidence.check_grounded，
  含 # 豁免与 ? 严格）；裸 URL 精确相等才幂等跳过并计数（不归一化——见
  docs/04 裁决 8.1）；单条被拒不阻断批次，其余照常入库
- record_search：搜索日志批量追加（查询词的证据比对在收尾折叠时做，现状机制）
- coverage：每节点"已收 vs 提取"的只读计数（只测缺失，不测薄弱）

条目粒度契约（GRANULARITY_LEVELS）与节点推导（leaf_node）是存储层的契约工具——
入库校验与收尾统计共用，编排层（postprocess）从这里取（依赖方向：编排 → 存储，
单向向下；本模块不 import 编排层）。

store.jsonl 由脚本持有，模型不可见；每行一条、追加原子，崩溃最多丢最后一个批次。
"""
import json
from pathlib import Path
from typing import Optional

from evidence import check_grounded

# existing_urls 增量缓存：record_sources 每批全量读 store 建幂等集合，批数×记录数
# 增长时是 O(n²)；按 (路径, mtime) 缓存——文件被本进程以外改动（mtime 变化）时
# 自然失效重建（本服务是唯一写入方，正常场景命中缓存）。
_url_cache: dict[str, tuple[int, set[str]]] = {}


def leaf_node(category_path: str, nodes: list[str]) -> Optional[str]:
    """取 category_path 对应的叶子节点：节点名的最长后缀匹配（节点名本身可含 -）。"""
    matches = [n for n in nodes if category_path == n or category_path.endswith("-" + n)]
    return max(matches, key=len) if matches else None


# granularity 仅归一化与计数、不拒绝（产量优先，粒度/子站问题由后续
# "站点与子站合并"功能处理）；站点级已并入合集级，存量按非法值归一化。
GRANULARITY_LEVELS = ("合集级", "单篇级")


def append_records(store_path: Path, records: list[dict]) -> int:
    """追加 JSONL 记录（单次追加原子）；空批不创建文件。返回追加数。"""
    if not records:
        return 0
    with store_path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(records)


def load_store(store_path: Path) -> tuple[list[dict], int]:
    """读全部记录；坏行（非 JSON/非对象）跳过并计数。"""
    records: list[dict] = []
    bad = 0
    for line in store_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(payload, dict):
            records.append(payload)
        else:
            bad += 1
    return records, bad


def _existing_urls(store_path: Path) -> set[str]:
    """读 store 全部裸 URL（带 mtime 缓存）——幂等跳过的判断集合。"""
    key = str(store_path)
    if not store_path.exists():
        return set()
    mtime = store_path.stat().st_mtime_ns
    cached = _url_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    records, _ = load_store(store_path)
    urls = {r.get("url") for r in records}
    _url_cache[key] = (mtime, urls)
    return urls


def record_sources(store_path: Path, entries: list, evidence_path: Path,
                   nodes: list[str]) -> dict:
    """批次入库即验：逐条校验后追加进 store。返回 {accepted, skipped, rejected}。

    rejected 每项 {index, name, url, reason}——单条被拒不阻断批次。
    幂等：裸 URL 精确相等才跳过并计数（真正去重由收尾的域名+名称联合完成）。
    node 字段由脚本按 category_path 对 nodes 的最长后缀匹配推导（模型零新增职责）。
    """
    accepted: list[dict] = []
    skipped = 0
    rejected: list[dict] = []
    evidence_ok = evidence_path.exists()
    evidence = evidence_path.read_text(encoding="utf-8", errors="replace") if evidence_ok else ""
    existing_urls = _existing_urls(store_path)

    for i, e in enumerate(entries):
        name = str(e.get("name") or "") if isinstance(e, dict) else ""
        url = str(e.get("url") or "") if isinstance(e, dict) else ""
        if not isinstance(e, dict) or not name or not url:
            rejected.append({"index": i, "name": name, "url": url, "reason": "缺 name/url"})
            continue
        if not evidence_ok:
            rejected.append({"index": i, "name": name, "url": url,
                             "reason": f"证据留痕不存在: {evidence_path}"
                                       "（PostToolUse hook 未生效？）"})
            continue
        kept, rej = check_grounded([e], evidence)
        if rej:
            rejected.append({"index": i, "name": name, "url": url,
                             "reason": "URL 不在证据留痕中"})
            continue
        if url in existing_urls:
            skipped += 1
            continue
        granularity = e.get("granularity")
        accepted.append({
            "type": "source",
            "node": leaf_node(str(e.get("category_path") or ""), nodes) or "",
            "name": name,
            "category_path": e.get("category_path", ""),
            "source_type": e.get("source_type", ""),
            "granularity": granularity if granularity in GRANULARITY_LEVELS else "合集级",
            "url": url,
            "description": e.get("description", ""),
            "reason": e.get("reason", ""),
        })
        existing_urls.add(url)
    append_records(store_path, accepted)
    if accepted:
        # 追加改变了文件 mtime——用新 mtime 更新缓存，保持幂等集合与磁盘一致
        _url_cache[str(store_path)] = (store_path.stat().st_mtime_ns, existing_urls)
    return {"accepted": len(accepted), "skipped": skipped, "rejected": rejected}


def record_search(store_path: Path, entries: list) -> int:
    """搜索日志批量追加；非 dict 条目跳过。返回追加数。"""
    rows = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        rows.append({
            "type": "search",
            "phase": str(e.get("phase") or ""),
            "node": str(e.get("node") or ""),
            "query": str(e.get("query") or ""),
            "results": e.get("results", ""),
            "extracted": e.get("extracted", ""),
            "verified": e.get("verified", ""),
        })
    return append_records(store_path, rows)


def coverage(store_path: Path, nodes: list[str]) -> list[dict]:
    """每节点 已收 vs 提取 的只读计数——只测缺失（missing = max(0, 提取-已收)）。

    missing 是粗略缺口信号（2026-09-02 评审）：提取数含去重前重复与跨节点
    顺路发现，已收数是幂等去重后的入库数，两口径天然有差——小额 missing 不
    视为遗漏，接近一整批提取量才值得怀疑漏调 record_sources。
    只测缺失、不测薄弱（体裁/来源维度单一由模型在会话内判断，依据是它刚提取
    的内容）；角度级状态不可恢复（角度多样性脚本校验 2026-08-31 裁决不做，该
    缺口以散文层治理维持，见 docs/02 讨论日志）。
    """
    records, _ = load_store(store_path) if store_path.exists() else ([], 0)
    per: dict[str, dict] = {n: {"recorded": 0, "extracted": 0} for n in nodes}
    extra: dict[str, dict] = {}

    def bucket(node: str) -> dict:
        if node in per:
            return per[node]
        return extra.setdefault(node, {"recorded": 0, "extracted": 0})

    for r in records:
        node = str(r.get("node") or "")
        if not node:
            continue
        if r.get("type") == "source":
            bucket(node)["recorded"] += 1
        elif r.get("type") == "search":
            try:
                bucket(node)["extracted"] += int(r.get("extracted") or 0)
            except (TypeError, ValueError):
                pass
    result = []
    for node, c in {**per, **extra}.items():
        result.append({"node": node, "recorded": c["recorded"],
                       "extracted": c["extracted"],
                       "missing": max(0, c["extracted"] - c["recorded"])})
    return result
