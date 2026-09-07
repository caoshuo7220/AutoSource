"""AutoSource 2.0 状态层：state.json 读写、schema 校验、原子写入、树校验与增量写回。

状态仅记录事实、不记录判断（设计文档第二章）：converged 布尔与理由不持久化，
phase（运行状态）例外。本模块被 orchestrator / converge / deliver 共享，只依赖
Python 标准库；不感知搜索来源（实现规格第四章约束）。
"""
import json
import os
import re
from pathlib import Path
from typing import Optional

from evidence import strip_citation_anchors

PHASES = ("init", "running", "converged", "failed")
# 实体 kind 枚举（实现规格第二章 entities 契约）
ENTITY_KINDS = ("机构", "厂商", "产品", "项目", "规范")

# 探索维度词类白名单（设计文档"角度池"六类词汇的枚举集合）：
# 修订裁决时校验 proposed.dims——不合规词剔除、全不合规则拒绝该修订。
DIM_VOCABULARY = frozenset((
    "论文", "专利", "列表", "排名", "数据库", "标准", "仓库", "合集",
    "官方文档", "手册", "知识库", "白皮书", "数据集", "开放数据",
    "社区", "博客", "资讯平台", "标准组织", "监管机构", "政府部门",
    "行业协会", "厂商", "研究机构", "大学", "评测机构", "基金会", "公共数据平台",
))

_DIRTY_RE = re.compile(r'[\\/:*?"<>|\s]+')


class StateError(ValueError):
    """状态文件结构/契约错误（区别于文件不存在的 IO 错误）。"""


def new_state(domain: str, nodes: list[dict]) -> dict:
    """--init 后的初始状态：phase=running，源集合为空，pending 空，统计归零。

    init 节点输出无 entities/angles，脚本补齐空数组（契约字段完整）。
    节点列表做列表级复制——状态持有独立副本，后续写回不污染调用方数据。
    """
    nodes = [{key: (list(value) if isinstance(value, list) else value)
              for key, value in node.items()} for node in nodes]
    for node in nodes:
        node.setdefault("entities", [])
        node.setdefault("angles", [])
    return {
        "domain": domain,
        "phase": "running",
        "structure": {"nodes": nodes},
        "sources": [],
        "pending_batch": {},
        "pending_revisions": [],
        "exploration": {
            "search_history": [],
            "gaps": [],
            "loop_stats": {"batch_count": 0, "consecutive_no_new": 0,
                           "consecutive_no_proposal": 0, "failed_queries": 0},
        },
    }


def save_state(path: Path, state: dict) -> None:
    """原子写入：写临时文件 + 原子替换（崩溃不损坏状态，实现规格第八章）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _check_shape(condition: bool, message: str) -> None:
    if not condition:
        raise StateError(f"state.json 结构错误: {message}")


def load_state(path: Path) -> dict:
    """读 state.json 并校验 schema 与树结构；坏文件抛出 StateError。

    文件不存在的 IO 错误原样上抛（与状态损坏区分，调用方给出运行目录提示）。
    """
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StateError(f"state.json 无法读取或不是合法 JSON: {exc}") from exc
    _check_shape(isinstance(state, dict), "顶层应为对象")
    _check_shape(isinstance(state.get("domain"), str), "domain 应为字符串")
    _check_shape(state.get("phase") in PHASES, f"phase 应为 {PHASES} 之一")
    structure = state.get("structure")
    _check_shape(isinstance(structure, dict), "structure 应为对象")
    nodes = structure.get("nodes")
    # phase=failed 的状态允许空结构（init 失败的保留现场）；其余状态须有节点
    _check_shape(isinstance(nodes, list)
                 and (bool(nodes) or state.get("phase") == "failed"),
                 "structure.nodes 应为非空数组")
    for node in nodes:
        _check_shape(isinstance(node, dict), "节点应为对象")
        for key in ("name", "parent"):
            _check_shape(isinstance(node.get(key), str), f"节点缺 {key} 字段")
        for key in ("terms", "entities", "dims", "angles"):
            _check_shape(isinstance(node.get(key), list), f"节点缺 {key} 字段")
    _check_shape(isinstance(state.get("sources"), list), "sources 应为数组")
    pending = state.get("pending_batch")
    _check_shape(isinstance(pending, dict), "pending_batch 应为对象")
    if pending:
        _check_shape(isinstance(pending.get("batch_id"), int)
                     and isinstance(pending.get("queries"), list),
                     "pending_batch 应为 {batch_id, queries}")
    revisions = state.get("pending_revisions")
    _check_shape(isinstance(revisions, list), "pending_revisions 应为数组")
    for revision in revisions:
        _check_shape(isinstance(revision, dict), "修订条目应为对象")
        _check_shape(isinstance(revision.get("revision_id"), int)
                     and isinstance(revision.get("proposed"), dict)
                     and isinstance(revision.get("evidence_urls"), list)
                     and isinstance(revision.get("evidence_batch"), int)
                     and isinstance(revision.get("evidence_query"), str),
                     "修订条目缺 revision_id/proposed/evidence_urls/evidence_batch/evidence_query 字段")
    exploration = state.get("exploration")
    _check_shape(isinstance(exploration, dict), "exploration 应为对象")
    _check_shape(isinstance(exploration.get("search_history"), list),
                 "exploration.search_history 应为数组")
    _check_shape(isinstance(exploration.get("gaps"), list),
                 "exploration.gaps 应为数组")
    stats = exploration.get("loop_stats")
    _check_shape(isinstance(stats, dict)
                 and all(isinstance(stats.get(k), int) for k in
                         ("batch_count", "consecutive_no_new",
                          "consecutive_no_proposal", "failed_queries")),
                 "exploration.loop_stats 应为四个整数计数字段")
    if nodes:
        validate_tree(nodes)
    return state


def validate_tree(nodes: list[dict]) -> None:
    """树校验（实现规格第二章）：单根、节点名唯一、parent 可解析、无孤儿。"""
    if not nodes:
        raise StateError("领域结构为空")
    names = [node["name"] for node in nodes]
    if len(set(names)) != len(names):
        raise StateError("节点名重复")
    roots = [n for n in names if nodes_by_name(nodes, n)["parent"] == ""]
    if len(roots) != 1:
        raise StateError("领域结构应有且仅有一个根节点（parent 为空字符串）")
    for node in nodes:
        if node["parent"] and node["parent"] not in names:
            raise StateError(f"节点「{node['name']}」的 parent「{node['parent']}」不存在")


def nodes_by_name(nodes: list[dict], name: str) -> Optional[dict]:
    for node in nodes:
        if node["name"] == name:
            return node
    return None


def node_map(nodes: list[dict]) -> dict[str, dict]:
    return {node["name"]: node for node in nodes}


def leaf_names(nodes: list[dict]) -> set[str]:
    """叶子节点集合（无子节点的节点）——source 的 node 必须是叶子（实现规格第二章）。"""
    parent_names = {node["parent"] for node in nodes if node["parent"]}
    return {node["name"] for node in nodes if node["name"] not in parent_names}


def path_of(node: str, nodes: list[dict]) -> str:
    """完整分类路径 = 从根到该节点的路径，各节点名用 - 连接，末段与 node 一致。"""
    by_name = node_map(nodes)
    chain = []
    current = node
    while current:
        chain.append(current)
        current = by_name[current]["parent"]
    return "-".join(reversed(chain))


def apply_writeback(state: dict, new_entities: list[dict],
                    new_terms: list[dict]) -> dict:
    """extract 的 new_entities / new_terms 增量写回 structure。

    逐条校验后追加（去重），单条校验不通过不阻断其余条目；返回 rejected 明细。
    new_nodes 不在此处理——修订建议经证据闸门入池（pool_revisions），
    由 review 裁决后经 adjudicate_revisions 应用。
    """
    nodes = state["structure"]["nodes"]
    by_name = node_map(nodes)
    rejected: list[dict] = []

    for index, item in enumerate(new_entities or []):
        if not isinstance(item, dict):
            rejected.append({"index": index, "name": "", "reason": "非对象"})
            continue
        name = str(item.get("name") or "")
        kind = str(item.get("kind") or "")
        node_name = str(item.get("node") or "")
        if not name:
            rejected.append({"index": index, "name": name, "reason": "缺 name"})
            continue
        if kind not in ENTITY_KINDS:
            rejected.append({"index": index, "name": name,
                             "reason": f"kind「{kind}」不在 {ENTITY_KINDS} 枚举内"})
            continue
        if node_name not in by_name:
            rejected.append({"index": index, "name": name,
                             "reason": f"归属节点「{node_name}」不存在"})
            continue
        entity = {"name": name, "kind": kind}
        if entity not in by_name[node_name]["entities"]:
            by_name[node_name]["entities"].append(entity)

    for index, item in enumerate(new_terms or []):
        if not isinstance(item, dict):
            rejected.append({"index": index, "name": "", "reason": "非对象"})
            continue
        term = str(item.get("term") or "")
        node_name = str(item.get("node") or "")
        if not term:
            rejected.append({"index": index, "name": term, "reason": "缺 term"})
            continue
        if node_name not in by_name:
            rejected.append({"index": index, "name": term,
                             "reason": f"归属节点「{node_name}」不存在"})
            continue
        if term not in by_name[node_name]["terms"]:
            by_name[node_name]["terms"].append(term)

    return {"rejected": rejected}


def pool_revisions(state: dict, proposals: list[dict], batch_id: int,
                   evidence_min: int, window_k: int) -> dict:
    """extract 的修订建议经证据闸门后入池（实现规格 L2 修订）。

    证据规则：evidence_urls 逐条须存在于已入库来源（URL 剥锚点相等），且其
    first_seen_batch 落在 [batch_id - window_k, batch_id] 窗口内——历史批次来源
    不作为证据，由此"连续 K 批无新增来源"在逻辑上蕴含"无有效提案"。有效证据数
    ≥ evidence_min 才准予入池；被闸门拒绝的提案不进入裁决。返回 {pooled, rejected}。
    """
    pending = state["pending_revisions"]
    next_id = max((r["revision_id"] for r in pending), default=0) + 1
    by_url: dict[str, tuple[int, str]] = {}
    for source in state["sources"]:
        url = strip_citation_anchors(str(source.get("url") or ""))
        if url and url not in by_url:
            by_url[url] = (int(source.get("first_seen_batch") or 0),
                           str(source.get("first_seen_query") or ""))
    window_start = batch_id - window_k
    rejected: list[dict] = []
    pooled = 0
    for item in proposals or []:
        name = str(item.get("name") or "") if isinstance(item, dict) else ""
        if not isinstance(item, dict) or not name:
            rejected.append({"name": name, "reason": "非对象或缺 name"})
            continue
        evidence = item.get("evidence_urls")
        if not isinstance(evidence, list) or not evidence:
            rejected.append({"name": name,
                             "reason": "缺 evidence_urls（新节点必须锚定已入库来源）"})
            continue
        valid: list[tuple[str, tuple[int, str]]] = []
        for url in evidence:
            hit = by_url.get(strip_citation_anchors(str(url)))
            if hit is not None and window_start <= hit[0] <= batch_id:
                valid.append((str(url), hit))
        if len(valid) < evidence_min:
            rejected.append({"name": name,
                             "reason": f"证据不足：{len(valid)} 条窗口内来源 < {evidence_min}"
                                       f"（须为近 {window_k} 批新增）"})
            continue
        pending.append({
            "revision_id": next_id,
            "proposed": {
                "name": name,
                "parent": str(item.get("parent") or ""),
                "terms": item.get("terms") if isinstance(item.get("terms"), list) else [],
                "dims": item.get("dims") if isinstance(item.get("dims"), list) else [],
            },
            "evidence_urls": [url for url, _ in valid],
            "evidence_batch": batch_id,
            "evidence_query": valid[0][1][1],  # 首条有效证据的发现查询（溯源）
            "status": "pending",
        })
        next_id += 1
        pooled += 1
    return {"pooled": pooled, "rejected": rejected}


def adjudicate_revisions(state: dict, decisions: list[dict], accept_max: int) -> dict:
    """按 review 裁决应用修订池，应用后清空（实现规格 L2 修订）。

    accept：dims 词类白名单（不合规词剔除、全不合规拒绝）+ 树校验（重名/双根/
    parent 可解析）通过后入树，受单轮采纳上限约束；merge：terms 并入 merge_into
    节点（去重，不并入 dims）；reject：丢弃。裁决结果不持久化。返回
    {accepted, merged, rejected}。
    """
    nodes = state["structure"]["nodes"]
    by_name = node_map(nodes)
    by_id = {r["revision_id"]: r for r in state["pending_revisions"]}
    accepted = merged = 0
    rejected: list[dict] = []
    for decision in decisions:
        revision = by_id[decision["revision_id"]]
        proposed = revision["proposed"]
        name = proposed["name"]
        if decision.get("decision") == "accept":
            if accepted >= accept_max:
                rejected.append({"name": name, "reason": f"超出单轮采纳上限 {accept_max}"})
                continue
            dims = [d for d in proposed.get("dims", []) if d in DIM_VOCABULARY]
            if not dims:
                rejected.append({"name": name, "reason": "dims 均不在探索维度词类白名单内"})
                continue
            parent = proposed.get("parent")
            if parent == "":
                rejected.append({"name": name, "reason": "根节点已存在，不允许第二个根节点"})
                continue
            if name in by_name:
                rejected.append({"name": name, "reason": "节点名重复"})
                continue
            if parent not in by_name:
                rejected.append({"name": name, "reason": f"parent「{parent}」不存在"})
                continue
            nodes.append({"name": name, "parent": parent,
                          "terms": proposed.get("terms")
                          if isinstance(proposed.get("terms"), list) else [],
                          "entities": [], "dims": dims, "angles": []})
            by_name[name] = nodes[-1]
            accepted += 1
        elif decision.get("decision") == "merge":
            target = by_name.get(decision.get("merge_into") or "")
            if target is None:
                rejected.append({"name": name,
                                 "reason": f"merge_into「{decision.get('merge_into')}」不存在"})
                continue
            for term in proposed.get("terms") or []:
                if term not in target["terms"]:
                    target["terms"].append(term)
            merged += 1
        else:
            rejected.append({"name": name,
                             "reason": str(decision.get("note") or "reject")})
    state["pending_revisions"] = []
    return {"accepted": accepted, "merged": merged, "rejected": rejected}


def ensure_angle_in_dims(nodes: list[dict], node_name: str, angle: str) -> bool:
    """plan 输出某 query 的 angle 不在其节点 dims 时加入 dims（声明该维度需要搜索）。

    返回是否新增（实现规格第二章：--plan 时写入）。
    """
    node = nodes_by_name(nodes, node_name)
    if node is None or not angle or angle in node["dims"]:
        return False
    node["dims"].append(angle)
    return True


def mark_angles(nodes: list[dict], queries: list[dict]) -> None:
    """--commit 规则：仅 status=done 的 query 其 angle 加入节点 angles（已搜索）。

    failed 的 angle 保持在 dims - angles，后续 plan 可补搜，不视为已覆盖。
    """
    for query in queries:
        if query.get("status") != "done":
            continue
        node = nodes_by_name(nodes, str(query.get("node") or ""))
        angle = str(query.get("angle") or "")
        if node is not None and angle and angle not in node["angles"]:
            node["angles"].append(angle)


def sanitize_domain(domain: str) -> str:
    """领域词 → 目录名安全前缀：去掉路径非法字符与空白，限长。"""
    cleaned = _DIRTY_RE.sub("_", str(domain)).strip("_")
    return cleaned[:30] or "未命名领域"
