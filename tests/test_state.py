"""state.py 的单元测试：状态读写、schema 校验、原子写入、树校验、增量写回与修订池。

修订池契约（实现规格 L2 修订）：extract 的 new_nodes 经证据闸门入池
（evidence_urls ≥ 门槛条、且 first_seen_batch 在近 K 批窗口内），
review 裁决后由脚本应用（accept：dims 词类白名单 + 树校验 + 单轮采纳上限；
merge：terms 并入、dims 不并入；reject：丢弃），应用后池清空。
"""
import json
from pathlib import Path

import pytest

from state import (StateError, adjudicate_revisions, apply_writeback,
                   ensure_angle_in_dims, leaf_names, load_state, mark_angles,
                   new_state, node_map, path_of, pool_revisions, sanitize_domain,
                   save_state, validate_tree)

TREE = [
    {"name": "交换机", "parent": "", "terms": ["交换机"], "entities": [],
     "dims": [], "angles": []},
    {"name": "数据中心交换机", "parent": "交换机",
     "terms": ["数据中心交换机"], "entities": [], "dims": ["官方文档"],
     "angles": ["官方文档"]},
    {"name": "园区交换机", "parent": "交换机", "terms": ["园区交换机"],
     "entities": [], "dims": ["厂商文档"], "angles": []},
]


def _src(url: str, batch: int, query: str = "某查询") -> dict:
    return {"name": f"源-{url}", "url": url, "source_type": "官方文档",
            "granularity": "合集级", "node": "数据中心交换机", "description": "",
            "first_seen_batch": batch, "first_seen_query": query}


DEFAULT_EVIDENCE = ("https://a.example/1", "https://a.example/2")


def _proposal(name="新节点", parent="交换机", dims=("官方文档",),
              terms=("新词",), evidence=DEFAULT_EVIDENCE) -> dict:
    proposal = {"name": name, "parent": parent, "terms": list(terms),
                "dims": list(dims)}
    if evidence is not None:
        proposal["evidence_urls"] = list(evidence)
    return proposal


# ---------- 状态读写与树校验（既有契约） ----------

def test_new_state_defaults():
    """--init 后的初始状态：phase=running、三集合为空、pending 空、统计归零。"""
    state = new_state("交换机", TREE)
    assert state["domain"] == "交换机"
    assert state["phase"] == "running"
    assert state["sources"] == []
    assert state["pending_batch"] == {}
    assert state["pending_revisions"] == []
    assert state["exploration"]["search_history"] == []
    assert state["exploration"]["gaps"] == []
    assert state["exploration"]["loop_stats"] == {
        "batch_count": 0, "consecutive_no_new": 0,
        "consecutive_no_proposal": 0, "failed_queries": 0}


def test_new_state_fills_entities_angles():
    """init 输出无 entities/angles，脚本补齐空数组（缺省字段不报错）。"""
    nodes = [{"name": "交换机", "parent": "", "terms": ["交换机"], "dims": []}]
    state = new_state("交换机", nodes)
    assert state["structure"]["nodes"][0]["entities"] == []
    assert state["structure"]["nodes"][0]["angles"] == []


def test_save_load_roundtrip(tmp_path):
    state = new_state("交换机", TREE)
    path = tmp_path / "state.json"
    save_state(path, state)
    assert load_state(path) == state


def test_save_state_atomic_no_tmp_leftover(tmp_path):
    """原子写入：写临时文件 + 原子替换，完成后目录内只有 state.json。"""
    path = tmp_path / "state.json"
    save_state(path, new_state("交换机", TREE))
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_save_state_preserves_content_on_overwrite(tmp_path):
    """覆盖写入整体替换：旧字段不残留。"""
    path = tmp_path / "state.json"
    state = new_state("交换机", TREE)
    state["sources"].append({"name": "旧条目", "url": "https://old.example/"})
    save_state(path, state)
    state2 = new_state("交换机", TREE)
    save_state(path, state2)
    assert load_state(path)["sources"] == []


def test_load_state_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_state(tmp_path / "state.json")


def test_load_state_bad_json_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{不是JSON", encoding="utf-8")
    with pytest.raises(StateError):
        load_state(path)


def test_load_state_bad_shape_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"domain": "交换机"}), encoding="utf-8")
    with pytest.raises(StateError):
        load_state(path)


def test_load_state_bad_phase_raises(tmp_path):
    state = new_state("交换机", TREE)
    state["phase"] = "flying"
    path = tmp_path / "state.json"
    save_state(path, state)
    with pytest.raises(StateError):
        load_state(path)


def test_load_state_bad_revision_raises(tmp_path):
    state = new_state("交换机", TREE)
    state["pending_revisions"] = [{"revision_id": 1}]
    path = tmp_path / "state.json"
    save_state(path, state)
    with pytest.raises(StateError):
        load_state(path)


def test_validate_tree_ok():
    validate_tree(TREE)


def test_validate_tree_single_root():
    """单根约束：第二个 parent="" 的节点拒绝。"""
    nodes = TREE + [{"name": "另一根", "parent": "", "terms": [], "dims": []}]
    with pytest.raises(StateError, match="根节点"):
        validate_tree(nodes)


def test_validate_tree_duplicate_names():
    nodes = TREE + [{"name": "园区交换机", "parent": "交换机", "terms": [],
                     "dims": []}]
    with pytest.raises(StateError, match="重复"):
        validate_tree(nodes)


def test_validate_tree_unknown_parent():
    nodes = TREE + [{"name": "孤儿", "parent": "不存在的节点", "terms": [],
                     "dims": []}]
    with pytest.raises(StateError, match="parent"):
        validate_tree(nodes)


def test_validate_tree_no_root():
    nodes = [{"name": "交换机", "parent": "空", "terms": [], "dims": []}]
    with pytest.raises(StateError, match="根节点"):
        validate_tree(nodes)


def test_leaf_names_excludes_internal():
    assert leaf_names(TREE) == {"数据中心交换机", "园区交换机"}


def test_node_map_and_path_of():
    assert set(node_map(TREE)) == {"交换机", "数据中心交换机", "园区交换机"}
    assert path_of("数据中心交换机", TREE) == "交换机-数据中心交换机"
    assert path_of("交换机", TREE) == "交换机"


# ---------- new_entities / new_terms 增量写回（既有契约） ----------

def test_apply_writeback_entities_and_terms():
    state = new_state("交换机", TREE)
    result = apply_writeback(
        state,
        new_entities=[{"name": "SONiC", "kind": "项目", "node": "数据中心交换机"}],
        new_terms=[{"term": "ToR", "node": "数据中心交换机"}],
    )
    assert result["rejected"] == []
    node = node_map(state["structure"]["nodes"])["数据中心交换机"]
    assert node["entities"] == [{"name": "SONiC", "kind": "项目"}]
    assert "ToR" in node["terms"]


def test_apply_writeback_dedup():
    state = new_state("交换机", TREE)
    apply_writeback(
        state,
        new_entities=[{"name": "SONiC", "kind": "项目", "node": "数据中心交换机"},
                      {"name": "SONiC", "kind": "项目", "node": "数据中心交换机"}],
        new_terms=[{"term": "ToR", "node": "数据中心交换机"},
                   {"term": "ToR", "node": "数据中心交换机"}],
    )
    node = node_map(state["structure"]["nodes"])["数据中心交换机"]
    assert node["entities"] == [{"name": "SONiC", "kind": "项目"}]
    assert node["terms"].count("ToR") == 1


def test_apply_writeback_reject_unknown_node_entity():
    state = new_state("交换机", TREE)
    result = apply_writeback(
        state,
        new_entities=[{"name": "X", "kind": "厂商", "node": "不存在的节点"}],
        new_terms=[],
    )
    assert len(result["rejected"]) == 1
    assert result["rejected"][0]["name"] == "X"


def test_apply_writeback_reject_bad_entity_kind():
    """kind 枚举契约：非五类取值拒绝（实现规格第二章 entities.kind 枚举）。"""
    state = new_state("交换机", TREE)
    result = apply_writeback(
        state,
        new_entities=[{"name": "X", "kind": "神秘物体", "node": "数据中心交换机"}],
        new_terms=[],
    )
    assert len(result["rejected"]) == 1
    assert "kind" in result["rejected"][0]["reason"]


# ---------- 修订池：证据闸门（实现规格 L2 修订） ----------

def test_pool_revisions_gate_missing_evidence_urls():
    state = new_state("交换机", TREE)
    result = pool_revisions(state, [_proposal(evidence=None)], batch_id=5,
                            evidence_min=2, window_k=4)
    assert result["pooled"] == 0
    assert len(result["rejected"]) == 1
    assert "evidence_urls" in result["rejected"][0]["reason"]
    assert state["pending_revisions"] == []


def test_pool_revisions_gate_insufficient_evidence():
    """有效证据不足门槛（1 < 2）→ 拒绝；且结构不变化。"""
    state = new_state("交换机", TREE)
    state["sources"].append(_src("https://a.example/1", batch=5))
    result = pool_revisions(
        state, [_proposal(evidence=("https://a.example/1",))],
        batch_id=5, evidence_min=2, window_k=4)
    assert result["pooled"] == 0
    assert "证据不足" in result["rejected"][0]["reason"]
    assert state["pending_revisions"] == []


def test_pool_revisions_gate_outside_window():
    """证据来自窗口外历史批次（批 6 窗口 [2,6]，first_seen_batch=1 在窗口外）→ 拒绝。"""
    state = new_state("交换机", TREE)
    state["sources"].extend([
        _src("https://a.example/1", batch=1),
        _src("https://a.example/2", batch=1),
    ])
    result = pool_revisions(
        state, [_proposal(evidence=("https://a.example/1", "https://a.example/2"))],
        batch_id=6, evidence_min=2, window_k=4)
    assert result["pooled"] == 0
    assert "窗口" in result["rejected"][0]["reason"]


def test_pool_revisions_pooled_with_provenance():
    """证据充足且窗口内 → 入池，附带溯源字段；窗口下界含端点（5-4=1 可用）。"""
    state = new_state("交换机", TREE)
    state["sources"].extend([
        _src("https://a.example/1", batch=1, query="早期查询"),
        _src("https://a.example/2", batch=5, query="近期查询"),
    ])
    result = pool_revisions(
        state, [_proposal(evidence=("https://a.example/1", "https://a.example/2"))],
        batch_id=5, evidence_min=2, window_k=4)
    assert result["pooled"] == 1
    assert result["rejected"] == []
    revision = state["pending_revisions"][0]
    assert revision["revision_id"] == 1
    assert revision["proposed"]["name"] == "新节点"
    assert revision["evidence_batch"] == 5
    assert revision["evidence_query"] == "早期查询"  # 首条有效证据的发现查询
    assert revision["status"] == "pending"


def test_pool_revisions_increments_revision_id():
    """revision_id 池内自增（运行期内唯一，跨批不重复）。"""
    state = new_state("交换机", TREE)
    state["sources"].extend([_src("https://a.example/1", batch=3),
                             _src("https://a.example/2", batch=3)])
    for batch_id in (3, 4):
        pool_revisions(state, [_proposal(evidence=("https://a.example/1",
                                                   "https://a.example/2"))],
                       batch_id=batch_id, evidence_min=2, window_k=4)
    assert [r["revision_id"] for r in state["pending_revisions"]] == [1, 2]


def test_pool_revisions_strips_anchors():
    """证据 URL 剥引用锚点后与已入库来源比对（#数字 锚点不改变资源指向）。"""
    state = new_state("交换机", TREE)
    state["sources"].extend([
        _src("https://a.example/1", batch=5),
        _src("https://a.example/2", batch=5),
    ])
    result = pool_revisions(
        state, [_proposal(evidence=("https://a.example/1#3", "https://a.example/2#3#1"))],
        batch_id=5, evidence_min=2, window_k=4)
    assert result["pooled"] == 1


# ---------- 修订池：裁决应用（实现规格 L2 修订） ----------

def _pooled_state(proposals=None, batch_id=5) -> dict:
    """按 proposals 构造已入池状态：证据来源以 batch_id 入库（保证窗口内）。"""
    state = new_state("交换机", TREE)
    if proposals is None:
        proposals = [_proposal()]
    urls = set()
    for proposal in proposals:
        urls.update(proposal.get("evidence_urls") or [])
    state["sources"].extend([_src(url, batch=batch_id) for url in urls])
    pool_revisions(state, proposals, batch_id=batch_id,
                   evidence_min=2, window_k=4)
    return state


def test_adjudicate_accept_adds_node_and_clears_pool():
    state = _pooled_state()
    result = adjudicate_revisions(state, [{"revision_id": 1, "decision": "accept"}],
                                  accept_max=2)
    assert result["accepted"] == 1
    assert result["rejected"] == []
    node = node_map(state["structure"]["nodes"])["新节点"]
    assert node["parent"] == "交换机"
    assert node["dims"] == ["官方文档"]
    assert node["angles"] == []
    assert state["pending_revisions"] == []


def test_adjudicate_accept_filters_dims_vocabulary():
    """dims 不合规词剔除、合规词保留（白名单按探索维度词类）。"""
    state = _pooled_state(proposals=[_proposal(dims=("官方文档", "CM5 刷写方法"))])
    adjudicate_revisions(state, [{"revision_id": 1, "decision": "accept"}],
                         accept_max=2)
    node = node_map(state["structure"]["nodes"])["新节点"]
    assert node["dims"] == ["官方文档"]


def test_adjudicate_accept_all_dims_invalid_rejected():
    state = _pooled_state(proposals=[_proposal(dims=("CM5 刷写方法", "另一个检索词"))])
    result = adjudicate_revisions(state, [{"revision_id": 1, "decision": "accept"}],
                                  accept_max=2)
    assert result["accepted"] == 0
    assert len(result["rejected"]) == 1
    assert "白名单" in result["rejected"][0]["reason"]
    assert "新节点" not in node_map(state["structure"]["nodes"])


def test_adjudicate_accept_second_root_rejected():
    state = _pooled_state(proposals=[_proposal(parent="")])
    result = adjudicate_revisions(state, [{"revision_id": 1, "decision": "accept"}],
                                  accept_max=2)
    assert len(result["rejected"]) == 1
    assert "根节点" in result["rejected"][0]["reason"]


def test_adjudicate_accept_duplicate_name_rejected():
    state = _pooled_state(proposals=[_proposal(name="园区交换机")])
    result = adjudicate_revisions(state, [{"revision_id": 1, "decision": "accept"}],
                                  accept_max=2)
    assert len(result["rejected"]) == 1
    assert "重复" in result["rejected"][0]["reason"]


def test_adjudicate_accept_unknown_parent_rejected():
    state = _pooled_state(proposals=[_proposal(parent="不存在的节点")])
    result = adjudicate_revisions(state, [{"revision_id": 1, "decision": "accept"}],
                                  accept_max=2)
    assert len(result["rejected"]) == 1
    assert "parent" in result["rejected"][0]["reason"]


def test_adjudicate_accept_cap_truncates_by_revision_id():
    """单轮采纳上限：按 revision_id 升序，超出部分强制 reject。"""
    state = _pooled_state(proposals=[
        _proposal(name="节点甲"), _proposal(name="节点乙"), _proposal(name="节点丙")])
    decisions = [{"revision_id": i, "decision": "accept"} for i in (1, 2, 3)]
    result = adjudicate_revisions(state, decisions, accept_max=2)
    assert result["accepted"] == 2
    names = node_map(state["structure"]["nodes"])
    assert "节点甲" in names and "节点乙" in names
    assert "节点丙" not in names
    assert "上限" in result["rejected"][0]["reason"]


def test_adjudicate_merge_terms_only_not_dims():
    state = _pooled_state(proposals=[_proposal(name="新节点", dims=("行业标准",))])
    result = adjudicate_revisions(
        state, [{"revision_id": 1, "decision": "merge", "merge_into": "园区交换机"}],
        accept_max=2)
    assert result["merged"] == 1
    target = node_map(state["structure"]["nodes"])["园区交换机"]
    assert "新词" in target["terms"]
    assert "行业标准" not in target["dims"]  # merge 不并入 dims（不增加覆盖义务）
    assert "新节点" not in node_map(state["structure"]["nodes"])


def test_adjudicate_merge_unknown_target_rejected():
    state = _pooled_state()
    result = adjudicate_revisions(
        state, [{"revision_id": 1, "decision": "merge", "merge_into": "不存在"}],
        accept_max=2)
    assert len(result["rejected"]) == 1
    assert "merge_into" in result["rejected"][0]["reason"]


def test_adjudicate_reject_discards():
    state = _pooled_state()
    result = adjudicate_revisions(
        state, [{"revision_id": 1, "decision": "reject", "note": "过细"}],
        accept_max=2)
    assert result["rejected"][0]["reason"] == "过细"
    assert "新节点" not in node_map(state["structure"]["nodes"])
    assert state["pending_revisions"] == []


# ---------- 维度/角度与目录名（既有契约） ----------

def test_ensure_angle_in_dims():
    nodes = [{"name": "n", "parent": "", "terms": [], "entities": [],
              "dims": ["官方文档"], "angles": []}]
    assert ensure_angle_in_dims(nodes, "n", "官方文档") is False
    assert ensure_angle_in_dims(nodes, "n", "行业标准") is True
    assert "行业标准" in nodes[0]["dims"]


def test_mark_angles_done_only():
    """--commit 规则：仅 status=done 的 query 其 angle 记入 angles，failed 不记。"""
    nodes = [{"name": "n", "parent": "", "terms": [], "entities": [],
              "dims": ["A", "B"], "angles": []}]
    queries = [
        {"query_id": 1, "node": "n", "angle": "A", "status": "done"},
        {"query_id": 2, "node": "n", "angle": "B", "status": "failed"},
        {"query_id": 3, "node": "n", "angle": "A", "status": "done"},
    ]
    mark_angles(nodes, queries)
    assert nodes[0]["angles"] == ["A"]


def test_sanitize_domain():
    assert sanitize_domain("交换机") == "交换机"
    assert sanitize_domain("AI 算力 服务器") == "AI_算力_服务器"
    assert sanitize_domain('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert sanitize_domain("") == "未命名领域"
    assert len(sanitize_domain("超" * 40)) == 30
