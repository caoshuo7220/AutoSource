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

PHASES = ("init", "running", "converged", "failed")
# 实体 kind 枚举（实现规格第二章 entities 契约）
ENTITY_KINDS = ("机构", "厂商", "产品", "项目", "规范")

_DIRTY_RE = re.compile(r'[\\/:*?"<>|\s]+')


class StateError(ValueError):
    """状态文件结构/契约错误（区别于文件不存在的 IO 错误）。"""


def new_state(domain: str, nodes: list[dict]) -> dict:
    """--init 后的初始状态：phase=running，源集合为空，pending 空，统计归零。

    init 节点输出无 entities/angles，脚本补齐空数组（契约字段完整）。
    """
    for node in nodes:
        node.setdefault("entities", [])
        node.setdefault("angles", [])
    return {
        "domain": domain,
        "phase": "running",
        "structure": {"nodes": nodes},
        "sources": [],
        "pending_batch": {},
        "exploration": {
            "search_history": [],
            "gaps": [],
            "loop_stats": {"batch_count": 0, "consecutive_no_new": 0,
                           "failed_queries": 0},
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
    exploration = state.get("exploration")
    _check_shape(isinstance(exploration, dict), "exploration 应为对象")
    _check_shape(isinstance(exploration.get("search_history"), list),
                 "exploration.search_history 应为数组")
    _check_shape(isinstance(exploration.get("gaps"), list),
                 "exploration.gaps 应为数组")
    stats = exploration.get("loop_stats")
    _check_shape(isinstance(stats, dict)
                 and all(isinstance(stats.get(k), int) for k in
                         ("batch_count", "consecutive_no_new", "failed_queries")),
                 "exploration.loop_stats 应为三个整数计数字段")
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


def apply_writeback(state: dict, new_nodes: list[dict],
                    new_entities: list[dict], new_terms: list[dict]) -> dict:
    """extract 的 new_nodes / new_entities / new_terms 增量写回 structure。

    逐条校验后追加（去重），单条校验不通过不阻断其余条目；返回 rejected 明细。
    new_nodes 先落（同批新节点可被后续实体/词条引用）。写回后结构仍满足树校验。
    """
    nodes = state["structure"]["nodes"]
    by_name = node_map(nodes)
    rejected: list[dict] = []

    for index, item in enumerate(new_nodes or []):
        if not isinstance(item, dict):
            rejected.append({"index": index, "name": "", "reason": "非对象"})
            continue
        name = str(item.get("name") or "")
        parent = str(item.get("parent") or "")
        if not name:
            rejected.append({"index": index, "name": name, "reason": "缺 name"})
            continue
        if name in by_name:
            rejected.append({"index": index, "name": name, "reason": "节点名重复"})
            continue
        if parent == "":
            if any(n["parent"] == "" for n in nodes):
                rejected.append({"index": index, "name": name,
                                 "reason": "根节点已存在，不允许第二个根节点"})
                continue
        elif parent not in by_name:
            rejected.append({"index": index, "name": name,
                             "reason": f"parent「{parent}」不存在"})
            continue
        node = {"name": name, "parent": parent,
                "terms": item.get("terms") if isinstance(item.get("terms"), list) else [],
                "entities": [], "dims": item.get("dims") if isinstance(item.get("dims"), list) else [],
                "angles": []}
        nodes.append(node)
        by_name[name] = node

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
