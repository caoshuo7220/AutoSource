"""AutoSource 2.0 编排入口：--init / --plan / --commit / --review / --finalize。

宿主↔脚本交接接口（实现规格第四章）：宿主是唯一能调用 websearch 的主体，
脚本是宿主的子进程——宿主按固定步骤执行"反复循环直到 --review 返回收敛"，
一切确定性判定（状态、证据、去重、收敛、失败）由脚本完成。

运行目录 outputs/run_{时间戳}/（--init 创建并打印，后续命令原样沿用）；
配置从 config.json 读取（环境变量 AUTOSOURCE_CONFIG 可覆盖路径），
输出根目录可用 AUTOSOURCE_OUTPUTS 覆盖（测试与部署隔离用）。
"""
import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from converge import fuse_triggered, last_two_batches_failing, objective_conditions
from deliver import (DEGRADED_REPORT_BODY, build_lineage, compose_report,
                     compute_stats, deduplicate_delivery, report_stats_block,
                     write_lineage_csv, write_search_log_csv, write_source_csv,
                     write_stats_csv)
from evidence import _contains_bounded, check_grounded, strip_citation_anchors, URL_CHARS
from llm_client import LLMClient, LLMError
from prompts import (build_extract_prompt, build_init_prompt, build_plan_prompt,
                     build_report_prompt, build_review_prompt)
from search_provider import HostSearchProvider
from state import (StateError, adjudicate_revisions, apply_writeback,
                   ensure_angle_in_dims, leaf_names, load_state, mark_angles,
                   new_state, node_map, pool_revisions, save_state,
                   sanitize_domain, validate_tree)

SKILL_DIR = Path(__file__).resolve().parent.parent
# 仓库根（skill 位于 .claude/skills/autosource，上两级即仓库根）
REPO_ROOT = SKILL_DIR.parents[2]
RUN_DIR_RE = re.compile(r"^run_(\d{4}-\d{2}-\d{2}-\d{6})(_\d+)?$")
GRANULARITY_LEVELS = ("合集级", "单篇级")


def outputs_root() -> Path:
    """运行产物根目录：仓库根 outputs/（1.0 历史产物已改名 outputs_1/）。"""
    env = os.environ.get("AUTOSOURCE_OUTPUTS")
    return Path(env) if env else REPO_ROOT / "outputs"


def load_config() -> dict:
    env = os.environ.get("AUTOSOURCE_CONFIG")
    path = Path(env) if env else SKILL_DIR / "config.json"
    if not path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {path}（复制 config.example.json 为 config.json 并填写 LLM 网关参数）")
    return json.loads(path.read_text(encoding="utf-8"))


def build_client(config: dict) -> LLMClient:
    """LLM 客户端工厂（测试 monkeypatch 点，运行期走 LLM 网关）。"""
    return LLMClient(config)


def _proper_noun_set(nodes: list[dict]) -> set[str]:
    """节点名 / 实体名 / 术语集合（source_type 校验用：标签不得包含具体名称）。"""
    nouns: set[str] = set()
    for node in nodes:
        nouns.add(str(node.get("name") or ""))
        nouns.update(str(term) for term in (node.get("terms") or []))
        nouns.update(str(entity.get("name") or "")
                     for entity in (node.get("entities") or []))
    return {noun for noun in nouns if noun}


def _contains_proper_noun(source_type: str, nouns: set[str]) -> Optional[str]:
    for noun in nouns:
        if noun and noun in source_type:
            return noun
    return None


def _state_path(run_dir: Path) -> Path:
    return run_dir / "state.json"


def _failed_state(domain: str) -> dict:
    return {"domain": domain, "phase": "failed", "structure": {"nodes": []},
            "sources": [], "pending_batch": {},
            "exploration": {"search_history": [], "gaps": [],
                            "loop_stats": {"batch_count": 0, "consecutive_no_new": 0,
                                           "failed_queries": 0}}}


def _declare_failure(node: str, exc: Exception) -> None:
    print(f"本次运行失败：{node} 节点调用失败，无法恢复——{exc}；"
          "已保留状态与已收录数据源。", file=sys.stderr)


def cmd_init(config: dict, description: str) -> int:
    """调 LLM init 生成领域结构，创建运行目录并打印路径（实现规格第四章）。"""
    root = outputs_root()
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    run_dir = root / f"run_{timestamp}"
    counter = 1
    while run_dir.exists():
        run_dir = root / f"run_{timestamp}_{counter}"
        counter += 1
    run_dir.mkdir()

    client = build_client(config)
    try:
        data = client.chat_json(build_init_prompt(description), node="init")
        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ValueError("init 输出缺 nodes 或为空")
        nodes = []
        for item in raw_nodes:
            if not isinstance(item, dict) or not item.get("name") \
                    or not isinstance(item.get("parent"), str):
                raise ValueError("init 节点缺 name/parent 字段")
            nodes.append({"name": item["name"], "parent": item["parent"],
                          "terms": item.get("terms") if isinstance(item.get("terms"), list) else [],
                          "dims": item.get("dims") if isinstance(item.get("dims"), list) else []})
        validate_tree(nodes)
    except (LLMError, ValueError, StateError) as exc:
        save_state(_state_path(run_dir), _failed_state(sanitize_domain(description)))
        _declare_failure("init", exc)
        return 1

    domain = nodes[0]["name"]  # domain = 根节点名（由 init 从领域描述提炼）
    state = new_state(domain, nodes)
    save_state(_state_path(run_dir), state)
    print(str(run_dir))
    return 0


def _print_pending_queries(queries: list[dict]) -> None:
    print(json.dumps({"queries": [
        {"query_id": q["query_id"], "query": q["query"], "node": q["node"],
         "angle": q["angle"], "reason": q.get("reason", "")} for q in queries]},
        ensure_ascii=False))


def cmd_plan(config: dict, run_dir: Path) -> int:
    """调 LLM plan 生成查询词，写入 pending_batch（status=pending）后再输出。

    pending_batch 非空时（--plan 后中断）不调 LLM，直接重印已有批次。
    """
    try:
        state = load_state(_state_path(run_dir))
    except (StateError, FileNotFoundError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    if state["phase"] != "running":
        print(f"错误: phase={state['phase']}，--plan 仅 running 状态可调用", file=sys.stderr)
        return 1
    pending = state["pending_batch"]
    if pending:
        _print_pending_queries(pending["queries"])
        return 0

    conv = config["converge"]
    nodes = state["structure"]["nodes"]
    by_name = node_map(nodes)
    client = build_client(config)
    try:
        data = client.chat_json(
            build_plan_prompt(nodes, state["exploration"]["gaps"],
                              state["exploration"]["search_history"][-10:]),
            node="plan")
        queries = data.get("queries")
        if not isinstance(queries, list) \
                or not conv["queries_per_batch_min"] <= len(queries) <= conv["queries_per_batch_max"]:
            raise ValueError(f"plan 查询数应在 {conv['queries_per_batch_min']}-"
                             f"{conv['queries_per_batch_max']} 之间，实际 {len(queries) if isinstance(queries, list) else '非列表'}")
        for item in queries:
            if not isinstance(item, dict) or not item.get("query") \
                    or not isinstance(item.get("angle"), str) or not item.get("angle"):
                raise ValueError("plan 查询缺 query/angle 字段")
            if item.get("node") not in by_name:
                raise ValueError(f"plan 查询的 node「{item.get('node')}」不存在")
    except (LLMError, ValueError) as exc:
        state["phase"] = "failed"
        save_state(_state_path(run_dir), state)
        _declare_failure("plan", exc)
        return 1

    # plan 的新 angle 记入节点 dims（声明该维度需要搜索）
    for item in queries:
        ensure_angle_in_dims(nodes, item["node"], item["angle"])
    batch_id = state["exploration"]["loop_stats"]["batch_count"] + 1
    state["pending_batch"] = {
        "batch_id": batch_id,
        "queries": [{"query_id": i + 1, "query": item["query"],
                     "node": item["node"], "angle": item["angle"],
                     "reason": item.get("reason", ""),
                     "status": "pending", "attempts": 0}
                    for i, item in enumerate(queries)],
    }
    save_state(_state_path(run_dir), state)
    _print_pending_queries(state["pending_batch"]["queries"])
    return 0


def cmd_commit(config: dict, run_dir: Path, results_path: Path) -> int:
    """处理 pending_batch：extract + 证据校验 + 去重 + 更新 state，清空 pending。

    顺序（实现规格第四/八章）：归档 raw → 经 SearchProvider 取批 → 逐 query 判定
    status → extract（仅 done 结果）→ 增量写回 → 候选逐条校验入库 → 统计与失败判定
    → 原子保存。pending 为空时无操作（幂等）。
    """
    try:
        state = load_state(_state_path(run_dir))
    except (StateError, FileNotFoundError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    if state["phase"] != "running":
        print(f"错误: phase={state['phase']}，--commit 仅 running 状态可调用", file=sys.stderr)
        return 1
    pending = state["pending_batch"]
    if not pending:
        print("无待处理批次（pending_batch 为空，--commit 幂等无操作）")
        return 0
    batch_id = pending["batch_id"]
    conv = config["converge"]

    # 搜索结果文件为证据唯一依据，先经 SearchProvider 取批（不直接读文件）
    provider = HostSearchProvider(results_path)
    try:
        batch = provider.fetch(pending["queries"])
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误: {exc}（状态未改动，补齐结果文件后可重跑）", file=sys.stderr)
        return 1

    # node 补注（溯源归档用）+ 原始结果归档（每批一次，覆盖重跑也一致）
    pending_by_id = {q["query_id"]: q for q in pending["queries"]}
    for entry in batch:
        entry["node"] = str((pending_by_id.get(entry["query_id"]) or {}).get("node") or "")
    raw_dir = run_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    (raw_dir / f"batch_{batch_id}.json").write_text(
        json.dumps(batch, ensure_ascii=False), encoding="utf-8")

    # 逐 query 判定 status：failed=true → failed；否则 done（results 允许为空）
    for entry, query in zip(batch, pending["queries"]):
        query["status"] = "failed" if entry.get("failed") else "done"
        query["attempts"] = int(entry.get("attempts") or 0)
    done_entries = [e for e in batch if not e.get("failed")]
    failed_count = len(batch) - len(done_entries)

    # extract：仅 done 的 query 结果（failed 不参与提取）
    client = build_client(config)
    try:
        if done_entries:
            extracted = client.chat_json(build_extract_prompt(done_entries),
                                         node="extract")
        else:
            extracted = {"sources": [], "new_entities": [], "new_terms": [],
                         "new_nodes": []}
        required = ("sources", "new_entities", "new_terms", "new_nodes")
        if not isinstance(extracted, dict) or any(k not in extracted for k in required):
            raise ValueError(f"extract 输出缺字段（需 {required}）")
    except (LLMError, ValueError) as exc:
        state["phase"] = "failed"
        save_state(_state_path(run_dir), state)
        _declare_failure("extract", exc)
        return 1

    nodes = state["structure"]["nodes"]
    leaves = leaf_names(nodes)
    # new_entities / new_terms 即时写回；new_nodes 走证据闸门入修订池（L2 修订）
    writeback = apply_writeback(state, extracted["new_entities"],
                                extracted["new_terms"])
    proper_nouns = _proper_noun_set(nodes)

    # 候选逐条校验：字段 → 证据（批文件全文边界匹配）→ node 叶子 → 归因 → 去重 → 入库
    evidence_text = results_path.read_text(encoding="utf-8", errors="replace")
    entry_texts = {e["query_id"]: json.dumps(e, ensure_ascii=False) for e in done_entries}
    done_order = [e["query_id"] for e in done_entries]
    existing_stripped = {strip_citation_anchors(s["url"]) for s in state["sources"]}
    rejected: list[dict] = []
    skipped_dup = 0
    new_sources: list[dict] = []
    per_query_new = {qid: 0 for qid in done_order}
    per_query_extracted = {qid: 0 for qid in done_order}
    for candidate in extracted["sources"]:
        if not isinstance(candidate, dict):
            rejected.append({"name": "", "url": "", "reason": "非对象"})
            continue
        name = str(candidate.get("name") or "")
        url = str(candidate.get("url") or "")
        if not name or not url:
            rejected.append({"name": name, "url": url, "reason": "缺 name/url"})
            continue
        kept, _ = check_grounded([{"url": url}], evidence_text)
        if not kept:
            rejected.append({"name": name, "url": url, "reason": "URL 不在证据留痕中"})
            continue
        node = str(candidate.get("node") or "")
        if node not in leaves:
            rejected.append({"name": name, "url": url,
                             "reason": f"node「{node}」非叶子节点或不存在"})
            continue
        source_type = str(candidate.get("source_type") or "")
        matched = _contains_proper_noun(source_type, proper_nouns)
        if matched:
            rejected.append({"name": name, "url": url,
                             "reason": f"source_type「{source_type}」包含具体名称「{matched}」，"
                                       "标签应只取词类词汇"})
            continue
        # 归因：最早命中的 done query（first_seen 语义；证据已过必有命中）
        first_qid = next((qid for qid in done_order
                          if _contains_bounded(url, entry_texts[qid], URL_CHARS)),
                         done_order[0])
        per_query_extracted[first_qid] += 1
        stripped = strip_citation_anchors(url)
        if stripped in existing_stripped:
            skipped_dup += 1
            continue
        granularity = candidate.get("granularity") \
            if candidate.get("granularity") in GRANULARITY_LEVELS else "合集级"
        source = {"name": name, "url": url,
                  "source_type": str(candidate.get("source_type") or ""),
                  "granularity": granularity, "node": node,
                  "description": str(candidate.get("description") or ""),
                  "first_seen_batch": batch_id,
                  "first_seen_query": str(pending_by_id[first_qid]["query"])}
        state["sources"].append(source)
        existing_stripped.add(stripped)
        new_sources.append(source)
        per_query_new[first_qid] += 1

    # 已搜索角度：仅 done 的 query 记入 angles（failed 保持 dims - angles，可补搜）
    mark_angles(nodes, pending["queries"])

    # 结构修订：提案经证据闸门入池（证据不足的提案拒绝，不进入裁决）
    proposal_result = pool_revisions(state, extracted["new_nodes"], batch_id,
                                     conv["revision_evidence_min"], conv["k"])

    # 搜索历史（每 query 一行，pending 顺序）与循环统计
    history = []
    for entry, query in zip(batch, pending["queries"]):
        results = entry.get("results")
        history.append({"batch": batch_id, "query": str(query["query"]),
                        "node": str(query["node"]),
                        "result_count": len(results) if isinstance(results, list) else 0,
                        "extracted": per_query_extracted.get(query["query_id"], 0),
                        "new_count": per_query_new.get(query["query_id"], 0),
                        "failed": 1 if entry.get("failed") else 0})
    state["exploration"]["search_history"].extend(history)

    stats = state["exploration"]["loop_stats"]
    stats["batch_count"] += 1
    stats["failed_queries"] += failed_count
    if not done_entries:
        stats["consecutive_no_new"] = 0  # 无搜索证据的批次不作收敛证据
    elif not new_sources:
        stats["consecutive_no_new"] += 1
    else:
        stats["consecutive_no_new"] = 0
    if not done_entries:
        stats["consecutive_no_proposal"] = 0
    elif not proposal_result["pooled"]:
        # 被证据闸门拒绝的提案不重置计数——无据提案不能阻止停止侧达成
        stats["consecutive_no_proposal"] += 1
    else:
        stats["consecutive_no_proposal"] = 0

    # 失败判定：存活熔断 → 失败率（批次处理完成后判定，状态保留）
    abort_reason = ""
    if fuse_triggered(stats["batch_count"], conv["fuse_batch_limit"]):
        abort_reason = (f"存活熔断：总批次数达到上限 {conv['fuse_batch_limit']}，"
                        "判定收敛判据失效（未收敛）")
    elif last_two_batches_failing(state["exploration"]["search_history"],
                                  conv["fail_rate_threshold"]):
        abort_reason = (f"连续 2 批失败率 ≥ {conv['fail_rate_threshold']:.0%}，"
                        "判定搜索服务异常")
    if abort_reason:
        state["phase"] = "failed"

    state["pending_batch"] = {}
    save_state(_state_path(run_dir), state)

    print(f"批次 {batch_id} 处理完成：查询 {len(batch)} 条（成功 {len(done_entries)}"
          f" / 失败 {failed_count}），新增来源 {len(new_sources)} 条"
          f"（证据拒绝 {len(rejected)} / 重复跳过 {skipped_dup}"
          f" / 写回拒绝 {len(writeback['rejected'])}），"
          f"修订入池 {proposal_result['pooled']} 条"
          f"（证据拒绝 {len(proposal_result['rejected'])}），"
          f"累计批次 {stats['batch_count']}，连续无新增 {stats['consecutive_no_new']}，"
          f"连续无新提案 {stats['consecutive_no_proposal']}，"
          f"累计失败查询 {stats['failed_queries']}")
    for item in rejected + writeback["rejected"] + proposal_result["rejected"]:
        print(f"  拒绝: {item.get('name') or item.get('url') or '(空)'} —— {item['reason']}")
    if abort_reason:
        print(f"本次运行失败：{abort_reason}（已保留状态与已收录数据源）")
        return 1
    return 0


def cmd_review(config: dict, run_dir: Path) -> int:
    """调 LLM review、整体替换 gaps、客观覆盖校验，判定收敛（实现规格第四章）。

    客观覆盖一票否决：未达成时 LLM 的 converged 无效；达成时 LLM 确认是最后一关。
    """
    try:
        state = load_state(_state_path(run_dir))
    except (StateError, FileNotFoundError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    if state["phase"] != "running":
        print(f"错误: phase={state['phase']}，--review 仅 running 状态可调用", file=sys.stderr)
        return 1
    conv = config["converge"]
    nodes = state["structure"]["nodes"]
    leaves = [n["name"] for n in nodes if n["name"] in leaf_names(nodes)]
    by_name = node_map(nodes)
    source_summary = compute_stats(state["sources"], leaves)

    client = build_client(config)
    pending_ids = {r["revision_id"] for r in state["pending_revisions"]}
    # review 输出校验：gaps 上限、revisions 覆盖完整性、decision 枚举、merge_into 可解析；
    # 违反视为契约违反整体重试（最多 llm.retry 次），重试耗尽按节点失败处理
    last_error: Exception | None = None
    for _ in range(int(config["llm"]["retry"]) + 1):
        try:
            data = client.chat_json(
                build_review_prompt(nodes, source_summary,
                                    state["exploration"]["search_history"],
                                    state["pending_revisions"]),
                node="review")
            gaps = data.get("gaps")
            revisions = data.get("revisions")
            if not isinstance(gaps, list) or not isinstance(data.get("converged"), bool) \
                    or not isinstance(revisions, list):
                raise ValueError("review 输出缺 gaps 列表、converged 布尔或 revisions 列表")
            for item in gaps:
                if not isinstance(item, dict) or not item.get("description") \
                        or item.get("node") not in by_name:
                    raise ValueError(f"review 缺口条目非法: {item}")
            if len(gaps) > conv["gaps_max"]:
                raise ValueError(f"gaps 数量 {len(gaps)} 超过上限 {conv['gaps_max']}")
            covered = set()
            for item in revisions:
                if not isinstance(item, dict) or item.get("revision_id") not in pending_ids:
                    raise ValueError(f"revisions 含非法 revision_id: {item}")
                if item.get("decision") not in ("accept", "merge", "reject"):
                    raise ValueError(f"decision 非法: {item}")
                if item.get("decision") == "merge" \
                        and item.get("merge_into") not in by_name:
                    raise ValueError(f"merge_into「{item.get('merge_into')}」不存在")
                covered.add(item["revision_id"])
            if covered != pending_ids:
                raise ValueError(f"revisions 未覆盖全部待裁决修订（缺 {pending_ids - covered}）")
            break
        except (LLMError, ValueError) as exc:
            last_error = exc
    else:
        state["phase"] = "failed"
        save_state(_state_path(run_dir), state)
        _declare_failure("review", last_error)
        return 1

    # 结构修订裁决：脚本应用（白名单/树校验/单轮上限由裁决函数执行），应用后池清空
    adjudication = adjudicate_revisions(state, revisions, conv["revision_accept_max"])
    if adjudication["accepted"] or adjudication["merged"] or adjudication["rejected"]:
        print(f"修订裁决: 采纳 {adjudication['accepted']} / 归并 {adjudication['merged']}"
              f" / 拒绝 {len(adjudication['rejected'])}", file=sys.stderr)
        for item in adjudication["rejected"]:
            print(f"  裁决拒绝: {item['name']} —— {item['reason']}", file=sys.stderr)

    # gaps 整体替换（反馈闭环：每轮 review 更新，plan 据此补搜）
    state["exploration"]["gaps"] = gaps
    met, unmet = objective_conditions(state, conv["k"])
    if not met:
        save_state(_state_path(run_dir), state)
        print(json.dumps({"converged": False,
                          "reason": "客观覆盖条件未达成：" + "；".join(unmet)},
                         ensure_ascii=False))
        return 0
    if data["converged"]:
        state["phase"] = "converged"
    save_state(_state_path(run_dir), state)
    print(json.dumps({"converged": bool(data["converged"]),
                      "reason": str(data.get("reason") or "")}, ensure_ascii=False))
    return 0


def cmd_finalize(config: dict, run_dir: Path) -> int:
    """生成交付物并重命名运行目录。前置条件 phase=converged（实现规格第四/七章）。"""
    try:
        state = load_state(_state_path(run_dir))
    except (StateError, FileNotFoundError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    if state["phase"] != "converged":
        print(f"错误: phase={state['phase']}，--finalize 前置条件为 phase=converged",
              file=sys.stderr)
        return 1

    nodes = state["structure"]["nodes"]
    leaves = [n["name"] for n in nodes if n["name"] in leaf_names(nodes)]
    delivered = deduplicate_delivery(state["sources"])

    match = RUN_DIR_RE.match(run_dir.name)
    timestamp = match.group(1) if match else datetime.now().strftime("%Y-%m-%d-%H%M%S")
    domain_name = sanitize_domain(state["domain"])
    target = outputs_root() / f"{domain_name}_{timestamp}"
    counter = 1
    while target.exists():
        target = outputs_root() / f"{domain_name}_{timestamp}_{counter}"
        counter += 1
    run_dir.rename(target)

    intermediate = target / "intermediate"
    intermediate.mkdir()
    if (target / "raw").exists():
        (target / "raw").rename(intermediate / "raw")
    else:
        (intermediate / "raw").mkdir()
    (target / "state.json").rename(intermediate / "state.json")
    (target / "search_results.json").unlink(missing_ok=True)  # 已归档 raw/，不留工作副本

    prefix = f"{domain_name}_{timestamp}"
    write_source_csv(target / f"{prefix}_数据源清单.csv", delivered, nodes)
    summary = compute_stats(delivered, leaves)
    write_stats_csv(intermediate / f"{prefix}_stats.csv", summary)
    write_search_log_csv(intermediate / f"{prefix}_搜索日志.csv",
                         state["exploration"]["search_history"])
    lineage_rows = build_lineage(state, delivered, intermediate / "raw")
    write_lineage_csv(intermediate / f"{prefix}_溯源.csv", lineage_rows)

    # 报告：六板块正文由 report 节点生成（重试耗尽则降级，CSV/stats 不受影响）
    client = build_client(config)
    try:
        body = client.chat_text(build_report_prompt(nodes, delivered,
                                                    state["exploration"]["search_history"]),
                                node="report")
    except LLMError as exc:
        body = DEGRADED_REPORT_BODY
        print(f"注意: 报告正文生成失败（{exc}），已降级为数据总览 + 六板块占位")
    report = compose_report(state["domain"], body,
                            report_stats_block(delivered, leaves))
    (target / f"{prefix}_分析报告.md").write_text(report, encoding="utf-8")

    print(f"输出目录: {target}")
    print(f"收录数据源: {len(delivered)} 条（交付去重后，原始候选 {len(state['sources'])} 条）")
    print(f"搜索批次: {state['exploration']['loop_stats']['batch_count']} 批，"
          f"失败查询 {state['exploration']['loop_stats']['failed_queries']} 次")
    print("交付物: 数据源清单.csv / 分析报告.md（intermediate/ 含 stats、搜索日志、溯源、状态快照与原始结果归档）")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    # Windows 控制台默认 GBK，stdout 中文会乱码——统一转 UTF-8（失败则保持默认）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(prog="orchestrator",
                                     description="AutoSource 2.0 编排入口")
    parser.add_argument("--init", metavar="领域描述",
                        help="调 LLM init 生成领域结构，创建运行目录并打印路径")
    parser.add_argument("--plan", metavar="RUN_DIR",
                        help="调 LLM plan 生成查询词，写入 pending_batch 后输出")
    parser.add_argument("--commit", nargs=2, metavar=("RUN_DIR", "SEARCH_RESULTS"),
                        help="处理 pending_batch 与搜索结果文件，更新状态")
    parser.add_argument("--review", metavar="RUN_DIR",
                        help="调 LLM review、整体替换 gaps、客观覆盖校验、判定收敛")
    parser.add_argument("--finalize", metavar="RUN_DIR",
                        help="生成交付物并重命名运行目录（前置 phase=converged）")
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    if args.init is not None:
        return cmd_init(config, args.init)
    if args.plan:
        return cmd_plan(config, Path(args.plan))
    if args.commit:
        return cmd_commit(config, Path(args.commit[0]), Path(args.commit[1]))
    if args.review:
        return cmd_review(config, Path(args.review))
    if args.finalize:
        return cmd_finalize(config, Path(args.finalize))
    parser.error("需要指定子命令（--init / --plan / --commit / --review / --finalize）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
